"""Qt/Drive lifecycle tests without a PDF process or external network access."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from gpdf.drive_auth import DriveError
from gpdf.drive_ui import DriveWindowMixin


class WindowHarness(DriveWindowMixin, QWidget):
    def __init__(self):
        super().__init__()
        self.busy = False
        self.worker_dead = False
        self.revision = 5
        self.session = 2
        self.dirty = False
        self.action_updates = 0
        self.renders = 0
        self.leave_requests = []
        self.leave_cancellations = []
        self.drive_status = QLabel(self)
        self._init_drive()

    def _update_actions(self):
        self.action_updates += 1

    def request_render(self):
        self.renders += 1

    def _leave_document(self, after, on_cancel=None):
        self.leave_requests.append(after)
        self.leave_cancellations.append(on_cancel)


class DriveUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = WindowHarness()
        self.release = threading.Event()
        self.warning_patch = patch("gpdf.drive_ui.QMessageBox.warning")
        self.warning = self.warning_patch.start()
        self.info_patch = patch("gpdf.drive_ui.QMessageBox.information")
        self.info = self.info_patch.start()

    def tearDown(self):
        self.release.set()
        task = self.window._drive_task
        if task:
            task[1].set()
            task[0].join(3)
            self.assertFalse(task[0].is_alive())
        self.window._drive_timer.stop()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.warning_patch.stop()
        self.info_patch.stop()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            self.app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                self.fail("Drive task did not complete")

    def test_background_task_keeps_qt_responsive_and_serializes_requests(self):
        worker_threads = []
        callback_threads = []
        heartbeats = []
        timer = QTimer(self.window)
        timer.setInterval(5)
        timer.timeout.connect(lambda: heartbeats.append(True))
        timer.start()

        def work(cancel):
            worker_threads.append(threading.get_ident())
            self.release.wait(3)
            return "complete"

        done = Mock(side_effect=lambda _: callback_threads.append(threading.get_ident()))
        self.assertTrue(self.window._start_drive_task(work, done, "Downloading"))
        self.assertFalse(self.window._start_drive_task(lambda _: None, done, "Second"))
        self.wait_until(lambda: len(heartbeats) >= 3)
        self.assertFalse(self.window.busy)
        self.assertNotEqual(worker_threads[0], threading.get_ident())
        done.assert_not_called()
        self.release.set()
        self.wait_until(lambda: self.window._drive_task is None)
        done.assert_called_once_with("complete")
        self.assertEqual(callback_threads, [threading.get_ident()])
        timer.stop()

    def test_open_uses_app_registration_without_user_configuration_dialog(self):
        config = object()
        with patch("gpdf.drive_auth.OAuthConfig.for_app", return_value=config) as load, \
                patch("gpdf.drive_auth.DriveAuth") as auth_type, \
                patch("gpdf.drive.DriveClient") as client_type, \
                patch("PyQt6.QtWidgets.QFileDialog.getOpenFileName") as choose_file:
            client = client_type.return_value
            client.auth.authorize.return_value = ["chosen-file"]
            client.download.return_value = object()
            self.window.open_drive()
            self.wait_until(lambda: self.window._drive_task is None)
        load.assert_called_once_with()
        auth_type.assert_called_once_with(config)
        client.auth.authorize.assert_called_once()
        self.assertEqual(client.download.call_args.args[0], "chosen-file")
        choose_file.assert_not_called()
        self.info.assert_not_called()
        self.warning.assert_not_called()
        self.assertEqual(len(self.window.leave_requests), 1)

    def test_missing_app_registration_does_not_prompt_for_user_json_or_start_network(self):
        with patch("gpdf.drive_auth.OAuthConfig.for_app", side_effect=DriveError("Not configured")), \
                patch("gpdf.drive_auth.DriveAuth") as auth_type, \
                patch("PyQt6.QtWidgets.QFileDialog.getOpenFileName") as choose_file:
            self.window.open_drive()
        choose_file.assert_not_called()
        auth_type.assert_not_called()
        self.assertIsNone(self.window._drive_task)
        self.warning.assert_called_once()

    def test_unexpected_errors_are_sanitized_and_failure_cleanup_runs(self):
        failed = Mock()
        done = Mock()

        def work(cancel):
            raise ValueError("Bearer secret-token https://private-document.example")

        self.window._start_drive_task(work, done, "Download", failed)
        self.wait_until(lambda: self.window._drive_task is None)
        done.assert_not_called()
        failed.assert_called_once_with()
        message = self.warning.call_args.args[2]
        self.assertNotIn("secret-token", message)
        self.assertNotIn("private-document", message)
        self.assertIn("edits are still available", message)

    def test_cancel_signals_worker_without_blocking_gui(self):
        observed = threading.Event()

        def work(cancel):
            if cancel.wait(3):
                observed.set()
            raise DriveError("Google Drive operation cancelled.")

        done = Mock()
        self.window._start_drive_task(work, done, "Downloading")
        self.window.cancel_drive()
        self.assertTrue(self.window._drive_task[1].is_set())
        self.wait_until(lambda: self.window._drive_task is None)
        self.assertTrue(observed.is_set())
        done.assert_not_called()
        self.assertIn("cancelled", self.warning.call_args.args[2])

    def test_completion_waits_for_pdf_operation_before_touching_document(self):
        self.window.busy = True
        done = Mock()
        self.window._start_drive_task(lambda _: "ready", done, "Download")
        self.wait_until(lambda: not self.window._drive_task[0].is_alive())
        self.window._poll_drive()
        done.assert_not_called()
        self.window.busy = False
        self.window._poll_drive()
        done.assert_called_once_with("ready")

    def _start_upload(self, after=None, on_failure=None):
        client = Mock()
        document = object()
        self.window.drive_client = client
        self.window.drive_document = document

        def upload(document, cancel):
            self.release.wait(3)
            return {"id": "new-copy"}

        client.upload.side_effect = upload
        self.window._upload_drive(after, on_failure)
        self.assertTrue(self.window.dirty)
        return client, document

    def test_successful_upload_clears_only_matching_document_revision(self):
        self._start_upload()
        self.release.set()
        self.wait_until(lambda: self.window._drive_task is None)
        self.assertFalse(self.window.dirty)
        self.info.assert_called_once()

    def test_new_edits_during_upload_stay_dirty_and_are_checked_before_leaving(self):
        after = Mock()
        self._start_upload(after)
        self.window.revision += 1
        self.release.set()
        self.wait_until(lambda: self.window._drive_task is None)
        self.assertTrue(self.window.dirty)
        self.assertEqual(self.window.leave_requests, [after])
        after.assert_not_called()

    def test_stale_upload_cannot_clear_new_session_dirty_state(self):
        self._start_upload()
        self.window.session += 1
        self.window.dirty = True
        self.release.set()
        self.wait_until(lambda: self.window._drive_task is None)
        self.assertTrue(self.window.dirty)

    def test_failed_upload_retains_edits(self):
        client = Mock()
        client.upload.side_effect = DriveError("The upload failed. Local edits were retained.")
        self.window.drive_client = client
        self.window.drive_document = object()
        self.window._upload_drive()
        self.wait_until(lambda: self.window._drive_task is None)
        self.assertTrue(self.window.dirty)
        self.info.assert_not_called()
        self.warning.assert_called_once()

    def test_disconnect_keeps_working_document_and_clears_config(self):
        client = Mock()
        document = object()
        self.window.drive_client = self.window._drive_owner = client
        self.window.drive_document = document
        self.window._drive_config = object()
        self.window._drive_clients = [client]
        self.window.disconnect_drive()
        self.wait_until(lambda: self.window._drive_task is None)
        self.assertIsNone(self.window.drive_client)
        self.assertIsNone(self.window._drive_config)
        self.assertIs(self.window.drive_document, document)
        self.assertIs(self.window._drive_owner, client)
        self.assertTrue(self.window.dirty)
        client.auth.disconnect.assert_called_once()
        client.cleanup.assert_not_called()

    def test_cleanup_failure_keeps_client_for_retry_without_exposing_details(self):
        client = Mock()
        client.close.side_effect = PermissionError("secret-token C:/private/confidential.pdf")
        self.window._drive_clients = [client]

        self.window._dispose_drive_client(client)

        self.assertEqual(self.window._drive_clients, [client])
        client.auth.clear.assert_called_once_with()
        client.close.assert_called_once_with()
        message = self.warning.call_args.args[2]
        self.assertIn("retry on exit", message)
        self.assertNotIn("secret-token", message)
        self.assertNotIn("confidential.pdf", message)
        self.assertNotIn("PermissionError", message)

        client.close.side_effect = None
        self.window._close_drive()

        self.assertEqual(self.window._drive_clients, [])
        self.assertEqual(client.auth.clear.call_count, 2)
        self.assertEqual(client.close.call_count, 2)
        self.warning.assert_called_once()

    def test_upload_continuation_preserves_cancel_cleanup_for_pending_download(self):
        pending_client = Mock()
        self.window._drive_clients = [pending_client]
        after = Mock()
        cleanup = Mock(side_effect=lambda: self.window._dispose_drive_client(pending_client))
        self._start_upload(after, on_failure=cleanup)
        self.window.revision += 1
        self.release.set()
        self.wait_until(lambda: self.window._drive_task is None)

        self.assertTrue(self.window.dirty)
        self.assertEqual(self.window.leave_requests, [after])
        self.assertEqual(self.window.leave_cancellations, [cleanup])
        cleanup.assert_not_called()
        after.assert_not_called()

        # The user cancels leaving after making another edit during upload.
        self.window.leave_cancellations[0]()

        cleanup.assert_called_once_with()
        pending_client.auth.clear.assert_called_once_with()
        pending_client.close.assert_called_once_with()
        self.assertEqual(self.window._drive_clients, [])
        self.assertTrue(self.window.dirty)
        after.assert_not_called()


if __name__ == "__main__":
    unittest.main()
