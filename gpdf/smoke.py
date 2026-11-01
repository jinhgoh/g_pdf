"""Optional end-to-end smoke test for the source and packaged GUI.

The fixture is a tiny hand-written PDF. This module deliberately never imports
MuPDF: all document reading, editing, rendering and saving use the real worker.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication


COMMENT = "Packaged smoke test: editable comment"


def _fixture_bytes() -> bytes:
    """Build a valid, searchable one-page PDF with byte-accurate xref offsets."""
    stream = (
        b"0.15 0.39 0.79 rg 64 736 484 5 re f\n"
        b"BT /F1 28 Tf 0.12 0.21 0.36 rg 64 692 Td (Gpdf) Tj ET\n"
        b"BT /F1 15 Tf 0.35 0.42 0.53 rg 64 662 Td "
        b"(Packaged Windows smoke test) Tj ET\n"
        b"BT /F1 11 Tf 0.19 0.24 0.32 rg 64 618 Td "
        b"(This fixture was opened and rendered by the PDF worker.) Tj ET\n"
        b"BT /F1 11 Tf 64 598 Td "
        b"(An editable comment is added below, saved, and reopened.) Tj ET\n"
        b"BT /F1 10 Tf 0.40 0.47 0.57 rg 64 76 Td "
        b"(Local files. Native annotations. No uploads.) Tj ET\n"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Title (Gpdf smoke test) /Creator (Gpdf) >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, contents in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(contents)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 6 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


class _SmokeController(QObject):
    def __init__(self, window, report_path):
        super().__init__(window)
        self.window = window
        self.report_path = Path(report_path).expanduser().resolve()
        self.started = time.perf_counter()
        self.stage_started = self.started
        self.state = "starting"
        self.finished = False
        self.directory = None
        self.fixture = None
        self.report = {
            "ok": False,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "packaged": bool(getattr(sys, "frozen", False)),
            "python": sys.version.split()[0],
            "timings_ms": {},
            "checks": {},
            "errors": [],
        }
        self.timer = QTimer(self)
        self.timer.setInterval(25)
        self.timer.timeout.connect(self._tick)

    def begin(self):
        try:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            if QApplication.platformName().lower() == "offscreen" and sys.platform == "win32":
                font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
                self.report["offscreen_font_loaded"] = (
                    font_path.is_file() and QFontDatabase.addApplicationFont(str(font_path)) >= 0)
                # Widgets were created before this optional harness was loaded.
                # Refresh their fonts after registering the offscreen font face.
                QApplication.setFont(QFont("Segoe UI", 10))
                self.window.setStyleSheet(self.window.styleSheet())
            self.window._error = self._operation_error
            # The ordinary failure handler is modal. Automated tests must report
            # failures without leaving a hidden dialog waiting for a click.
            try:
                self.window.bridge.failed.disconnect(self.window._worker_failed)
            except (TypeError, RuntimeError):
                pass
            self.window.bridge.failed.connect(self._worker_error)
            self.directory = tempfile.TemporaryDirectory(prefix="gpdf-smoke-")
            self.fixture = Path(self.directory.name) / "smoke-fixture.pdf"
            self.fixture.write_bytes(_fixture_bytes())
            self._set_state("opening")
            self.window._open(str(self.fixture))
            self.timer.start()
        except Exception:
            self._fail(traceback.format_exc())

    def _set_state(self, state):
        self.state = state
        self.stage_started = time.perf_counter()

    def _record_stage(self):
        self.report["timings_ms"][self.state] = round(
            (time.perf_counter() - self.stage_started) * 1000, 1)

    def _ready(self):
        window = self.window
        return (
            not window.busy
            and not getattr(window, "worker_dead", False)
            and window.display_identity == (window.session, window.revision, window.page)
            and window.bridge.active is None
            and not window.bridge.pending
            and not window.zoom_timer.isActive()
            and not window.resize_timer.isActive()
        )

    def _tick(self):
        if self.finished:
            return
        try:
            if time.perf_counter() - self.started > 30:
                self._fail(f"Timed out after 30 seconds while {self.state}.")
                return
            if not self.window.bridge.process.is_alive():
                self._fail(f"PDF worker exited while {self.state}.")
                return
            if not self._ready():
                return
            if self.state == "opening":
                self._record_stage()
                if self.window.page_count != 1 or self.window.annotations:
                    self._fail("The initial fixture did not open as one unannotated page.")
                    return
                self.report["checks"]["opened_one_page"] = True
                self._set_state("annotating")
                self.window.font_size.setValue(18)
                self.window.add_annotation({"kind": "text", "rect": [64, 218, 548, 292]})
                editor = self.window.canvas.inline_editor
                if editor is None:
                    self._fail("The page-local text editor did not open.")
                    return
                editor.setPlainText(COMMENT)
                self.report["checks"]["inline_text_editor"] = editor.parent() is self.window.canvas.viewport()
                self.window.canvas.finish_inline_text()
            elif self.state == "annotating":
                self._record_stage()
                if not self._validate_comment("created"):
                    return
                self._set_state("saving")
                self.window.save()
            elif self.state == "saving":
                self._record_stage()
                if self.window.dirty:
                    self._fail("Save completed but the document still has unsaved changes.")
                    return
                if not self.fixture.is_file() or self.fixture.stat().st_size <= 0:
                    self._fail("The saved fixture is missing or empty.")
                    return
                self.report["checks"]["saved_bytes"] = self.fixture.stat().st_size
                self._set_state("reopening")
                self.window._open(str(self.fixture))
            elif self.state == "reopening":
                self._record_stage()
                if not self._validate_comment("reopened"):
                    return
                gui_loaded_pdf = "pymupdf" in sys.modules or "fitz" in sys.modules
                self.report["checks"]["pdf_engine_stays_out_of_gui"] = not gui_loaded_pdf
                if gui_loaded_pdf:
                    self._fail("MuPDF was imported into the GUI process.")
                    return
                self.window.set_tool("select")
                self.window.select_annotation(self.window.annotations[0]["xref"])
                self.window.statusBar().showMessage("Smoke test passed: opened, annotated, saved, and reopened.")
                self._set_state("capturing")
            elif self.state == "capturing":
                self._finish(True)
        except Exception:
            self._fail(traceback.format_exc())

    def _validate_comment(self, stage):
        annotations = self.window.annotations
        valid = (len(annotations) == 1
                 and annotations[0].get("kind") == "text"
                 and annotations[0].get("text") == COMMENT
                 and abs(float(annotations[0].get("fontsize", 0)) - 18) < 0.1)
        self.report["checks"][f"{stage}_comment"] = {
            "ok": valid, "count": len(annotations),
            "text": annotations[0].get("text") if annotations else None,
            "fontsize": annotations[0].get("fontsize") if annotations else None,
        }
        if not valid:
            self._fail(f"The {stage} native text annotation did not preserve its content and 18 pt font size.")
        return valid

    def _operation_error(self, response):
        self._fail(str(response.get("error", "A PDF operation failed.")))

    def _worker_error(self, message):
        self._fail(str(message))

    def _fail(self, message):
        if self.finished:
            return
        self.report["errors"].append(f"{self.state}: {message}")
        self._finish(False)

    def _finish(self, success):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        self.window.zoom_timer.stop()
        self.window.resize_timer.stop()
        self.report["last_stage"] = self.state
        self.report["elapsed_ms"] = round((time.perf_counter() - self.started) * 1000, 1)
        try:
            screenshot = self.report_path.with_suffix(".png")
            if not self.window.grab().save(str(screenshot), "PNG"):
                raise OSError("Qt could not save the smoke-test screenshot.")
            self.report["screenshot"] = str(screenshot)
        except Exception as exc:
            success = False
            self.report["errors"].append(f"Screenshot: {exc}")
        try:
            self.window.bridge.shutdown()
        except Exception as exc:
            success = False
            self.report["errors"].append(f"Worker shutdown: {exc}")
        try:
            if self.directory is not None:
                self.directory.cleanup()
        except Exception as exc:
            success = False
            self.report["errors"].append(f"Fixture cleanup: {exc}")
        self.report["ok"] = bool(success and not self.report["errors"])
        try:
            self.report_path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        except Exception as exc:
            self.report["ok"] = False
            self.report["errors"].append(f"Report write: {exc}")
            if sys.stderr is not None:
                print(json.dumps(self.report, indent=2), file=sys.stderr)
        self.window._closing = True
        self.window.close()
        QApplication.instance().exit(0 if self.report["ok"] else 1)


def start(window, report_path):
    """Run the real worker workflow, write JSON/PNG evidence, then exit the app."""
    controller = _SmokeController(window, report_path)
    window._smoke_controller = controller
    QTimer.singleShot(0, controller.begin)
    return controller
