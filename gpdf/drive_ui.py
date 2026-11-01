"""Background Drive tasks and the desktop window's explicit cloud workflow."""

import queue
import threading

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMessageBox


class DriveWindowMixin:
    """Keep network I/O separate from both the GUI and the PDF worker."""

    def _init_drive(self):
        self.drive_document = None
        self.drive_client = None
        self._drive_owner = None
        self._drive_config = None
        self._drive_clients = []
        self._drive_task = None
        self._drive_timer = QTimer(self)
        self._drive_timer.setInterval(40)
        self._drive_timer.timeout.connect(self._poll_drive)

    def _start_drive_task(self, work, done, message, failed=None):
        if self._drive_task is not None:
            return False
        cancel = threading.Event()
        results = queue.Queue(maxsize=1)

        def run():
            from .drive_auth import DriveError
            try:
                results.put((True, work(cancel)))
            except DriveError as error:
                results.put((False, str(error)))
            except Exception:
                # Never surface HTTP bodies, auth codes, tokens or URLs.
                results.put((False, "The Google Drive operation failed. Your open edits are still available."))

        thread = threading.Thread(target=run, name="Google Drive transfer", daemon=True)
        self._drive_task = (thread, cancel, results, done, failed)
        thread.start()
        self._drive_timer.start()
        self.drive_status.setText(message)
        self.drive_status.show()
        self._update_actions()
        return True

    def _poll_drive(self):
        task = self._drive_task
        if task is None or task[0].is_alive() or self.busy:
            return
        ok, result = task[2].get_nowait()
        self._drive_task = None
        self._drive_timer.stop()
        self.drive_status.hide()
        self._update_actions()
        if ok:
            task[3](result)
        else:
            if task[4]:
                task[4]()
            QMessageBox.warning(self, "Google Drive", result)

    def cancel_drive(self):
        if self._drive_task:
            self._drive_task[1].set()
            self.drive_status.setText("Cancelling Google Drive operation…")

    def open_drive(self):
        if self.busy or self.worker_dead or self._drive_task:
            return
        from .drive_auth import DriveAuth, DriveError, OAuthConfig
        from .drive import DriveClient
        if self._drive_config is None:
            try:
                self._drive_config = OAuthConfig.for_app()
            except DriveError as error:
                QMessageBox.warning(self, "Google sign-in unavailable", str(error))
                return

        # A separate auth session prevents a new browser account choice from
        # changing the destination account of an already-open PDF.
        client = DriveClient(DriveAuth(self._drive_config))
        self._drive_clients.append(client)

        def work(cancel):
            ids = client.auth.authorize(cancel)
            if len(ids) != 1:
                raise DriveError("Choose one PDF in Google Drive.")
            return client.download(ids[0], cancel)

        def dispose():
            self._dispose_drive_client(client)

        def downloaded(document):
            self._leave_document(
                lambda: self._open(document.path, drive=(client, document)),
                on_cancel=dispose,
            )

        self._start_drive_task(work, downloaded, "Choose a PDF in your browser…", dispose)

    def _release_drive_document(self):
        client = self._drive_owner
        self.drive_client = self.drive_document = None
        self._drive_owner = None
        if client:
            self._dispose_drive_client(client)

    def _dispose_drive_client(self, client):
        client.auth.clear()
        try:
            client.close()
        except Exception:
            # Retain ownership for another cleanup attempt during shutdown.
            QMessageBox.warning(self, "Temporary PDF cleanup",
                                "A temporary Drive copy could not be removed. Gpdf will retry on exit. "
                                "A local copy may remain if another program has locked it.")
            return
        if client in self._drive_clients:
            self._drive_clients.remove(client)

    def _upload_drive(self, after=None, on_failure=None):
        client, document = self.drive_client, self.drive_document
        revision, session = self.revision, self.session
        self.dirty = True  # A local snapshot alone is not a completed cloud save.

        def uploaded(result):
            if (session, revision) == (self.session, self.revision):
                self.dirty = False
            self._update_actions()
            QMessageBox.information(self, "Saved to Google Drive",
                                    "An edited PDF copy was saved in My Drive. The original was not replaced.")
            if after:
                # New edits during upload must still be saved or discarded.
                self._leave_document(after, on_cancel=on_failure)

        self._start_drive_task(lambda cancel: client.upload(document, cancel), uploaded,
                               "Saving an edited copy to Google Drive…", on_failure)
        self.request_render()

    def disconnect_drive(self):
        if self.busy or self._drive_task:
            return
        clients = list(self._drive_clients)
        self._drive_config = None
        # Keep the PDF and its pending edits; subsequent saves use a local dialog.
        if self.drive_document:
            self.drive_client = None
            self.dirty = True
        if not clients:
            return

        def work(cancel):
            from .drive_auth import DriveError
            failed = False
            for client in clients:
                try:
                    client.auth.disconnect(cancel)
                except DriveError:
                    failed = True
            if failed:
                raise DriveError("Local sign-in tokens were cleared, but Google revocation could not be confirmed. "
                                 "Remove Gpdf's access in your Google Account permissions if needed.")

        self._start_drive_task(work, lambda _: self._update_actions(), "Disconnecting Google Drive…")

    def _close_drive(self):
        self._drive_timer.stop()
        for client in list(self._drive_clients):
            self._dispose_drive_client(client)
