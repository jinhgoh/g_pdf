"""Nonblocking, serial RPC to a dedicated PDF process.

MuPDF objects never cross process boundaries. Pending display requests are
coalesced, and search only schedules one page at a time so navigation can yield.
"""

from collections import OrderedDict, deque
import multiprocessing as mp
import queue

from PyQt6.QtCore import QObject, QTimer, pyqtSignal


def _worker_entry(requests, responses):
    from .engine import worker_main

    worker_main(requests, responses)


class PdfBridge(QObject):
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        context = mp.get_context("spawn")
        self.requests = context.Queue()
        self.responses = context.Queue()
        self.process = context.Process(target=_worker_entry, args=(self.requests, self.responses), daemon=True)
        self.process.start()
        self.pending = deque()
        self.active = None
        self.sequence = 0
        self.closed = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(16)

    def submit(self, command, payload, callback, *, key=None, background=False):
        self.sequence += 1
        item = ({"id": self.sequence, "command": command, "payload": payload}, callback, key, background)
        if key:
            self.pending = deque(x for x in self.pending if x[2] != key)
        if background:
            self.pending.append(item)
        else:
            # Display/edit commands go ahead of background page-by-page search.
            index = next((i for i, x in enumerate(self.pending) if x[3]), len(self.pending))
            self.pending.insert(index, item)
        self._dispatch()
        return self.sequence

    def cancel_pending(self, key=None):
        self.pending = deque(x for x in self.pending if key is not None and x[2] != key)

    def _dispatch(self):
        if not self.active and self.pending and not self.closed:
            self.active = self.pending.popleft()
            self.requests.put(self.active[0])

    def _poll(self):
        if self.closed:
            return
        if not self.process.is_alive():
            self.timer.stop()
            self.pending.clear()
            self.active = None
            self.failed.emit("The PDF process stopped unexpectedly. Restart Gpdf to reopen your document.")
            return
        # One in-flight command bounds IPC and avoids stale render backlogs.
        try:
            response = self.responses.get_nowait()
        except queue.Empty:
            return
        if self.active and response.get("id") == self.active[0]["id"]:
            callback = self.active[1]
            self.active = None
            callback(response)
        self._dispatch()

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        self.timer.stop()
        self.pending.clear()
        # Let an idle worker close files and discard temporary snapshots normally.
        # Quit does not wait behind expensive page rendering or a full search.
        if self.process.is_alive():
            self.requests.put(None)
            self.process.join(timeout=0.5)
            if self.process.is_alive():
                self.process.terminate()
        self.process.join(timeout=1)
        for channel in (self.requests, self.responses):
            channel.cancel_join_thread()
            channel.close()


class PageCache:
    """Byte-budgeted LRU; the current viewport has its own QImage."""

    def __init__(self, max_bytes=128 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.bytes_used = 0
        self.entries = OrderedDict()

    def get(self, key):
        value = self.entries.get(key)
        if value is not None:
            self.entries.move_to_end(key)
        return value

    def put(self, key, value):
        size = len(value["samples"])
        if key in self.entries:
            self.bytes_used -= len(self.entries.pop(key)["samples"])
        if size > self.max_bytes:
            return
        self.entries[key] = value
        self.bytes_used += size
        while self.bytes_used > self.max_bytes:
            _, evicted = self.entries.popitem(last=False)
            self.bytes_used -= len(evicted["samples"])

    def clear(self):
        self.entries.clear()
        self.bytes_used = 0
