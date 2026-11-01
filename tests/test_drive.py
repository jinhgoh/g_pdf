"""Drive transfer regressions use synthetic bytes and mocked HTTP only."""

import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from gpdf.drive import API_ROOT, UPLOAD_ROOT, DriveClient, _MultipartUpload, _restrict_windows_folder
from gpdf.drive_auth import DriveError


PDF = b"%PDF-1.7\nsynthetic test content\n%%EOF\n"


def metadata(**updates):
    result = {"id": "safe_file-123", "name": "Test.pdf", "mimeType": "application/pdf",
              "size": str(len(PDF)), "version": "11", "md5Checksum": hashlib.md5(PDF).hexdigest(),
              "trashed": False, "capabilities": {"canDownload": True, "canCopy": True}}
    result.update(updates)
    return result


class Response:
    def __init__(self, value=None, *, body=None, status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self.body = body if body is not None else json.dumps(value).encode()
        self.chunks = chunks
        self.closed = False

    def iter_content(self, amount):
        if self.chunks is not None:
            yield from self.chunks
        else:
            for start in range(0, len(self.body), amount):
                yield self.body[start:start + amount]

    def close(self):
        self.closed = True


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.private_temp = patch("gpdf.drive._private_temp", side_effect=lambda:
                                  tempfile.TemporaryDirectory(dir=self.temp.name))
        self.private_temp.start()
        self.auth = Mock()
        self.auth.access_token.return_value = "fake-token"
        self.client = DriveClient(self.auth)
        self.client.session.request = Mock()
        self.cancel = threading.Event()

    def tearDown(self):
        self.client.close()
        self.private_temp.stop()
        self.temp.cleanup()

    def download(self, before=None, after=None):
        before = before or metadata()
        self.client.session.request.side_effect = [Response(before), Response(body=PDF),
                                                    Response(after or before)]
        return self.client.download("safe_file-123", self.cancel)

    def test_download_checks_integrity_and_uses_opaque_filename(self):
        document = self.download(metadata(name="../../secrets\\outside.pdf"))
        self.assertEqual(Path(document.path).name, "working.pdf")
        self.assertTrue(Path(document.path).is_relative_to(self.temp.name))
        self.assertEqual(Path(document.path).read_bytes(), PDF)
        self.assertEqual(document.version, "11")
        self.assertFalse(self.client.session.trust_env)
        for call in self.client.session.request.call_args_list:
            self.assertTrue(call.args[1].startswith(API_ROOT + "/safe_file-123"))
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertTrue(call.kwargs["stream"])
            self.assertEqual(call.kwargs["timeout"], (5, 30))
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer fake-token")
        self.client.cleanup(document)
        self.assertFalse(Path(document.path).exists())

    def test_invalid_file_ids_never_reach_network(self):
        for file_id in ("../oops", "x?alt=media", "https://evil.test/x", "a/b", "a%2fb", "a\r\n", "", "x" * 257, None):
            with self.subTest(file_id=file_id), self.assertRaises(DriveError):
                self.client.download(file_id, self.cancel)
        self.client.session.request.assert_not_called()

    def test_missing_permissions_wrong_mime_trash_and_size_rejected(self):
        for updates in ({"capabilities": {"canDownload": False}}, {"mimeType": "text/html"},
                        {"trashed": True}, {"size": "536870913"}, {"size": "bad"},
                        {"version": "x"}, {"md5Checksum": "bad"}, {"id": "different"}):
            with self.subTest(updates=updates):
                self.client.session.request.reset_mock(side_effect=True)
                self.client.session.request.return_value = Response(metadata(**updates))
                with self.assertRaises(DriveError):
                    self.client.download("safe_file-123", self.cancel)
                self.assertEqual(self.client.session.request.call_count, 1)

    def test_changed_remote_version_discards_inconsistent_download(self):
        with self.assertRaisesRegex(DriveError, "changed during download"):
            self.download(after=metadata(version="12"))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.assertEqual(self.client._documents, {})

    def test_checksum_mismatch_discards_download(self):
        with self.assertRaisesRegex(DriveError, "changed during download"):
            self.download(metadata(md5Checksum="0" * 32))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_non_pdf_content_rejected_and_cleaned(self):
        body = b"<html>not a PDF</html>"
        info = metadata(size=str(len(body)), md5Checksum=hashlib.md5(body).hexdigest())
        self.client.session.request.side_effect = [Response(info), Response(body=body)]
        with self.assertRaisesRegex(DriveError, "not a PDF"):
            self.client.download("safe_file-123", self.cancel)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_stream_size_limit_applies_without_content_length(self):
        self.client.session.request.side_effect = [Response(metadata()), Response(body=PDF + b"overflow")]
        with self.assertRaisesRegex(DriveError, "expected size"):
            self.client.download("safe_file-123", self.cancel)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_declared_overlarge_download_refused(self):
        response = Response(body=PDF, headers={"Content-Length": "536870913"})
        self.client.session.request.side_effect = [Response(metadata()), response]
        with self.assertRaisesRegex(DriveError, "allowed size"):
            self.client.download("safe_file-123", self.cancel)
        self.assertTrue(response.closed)

    def test_cancellation_mid_download_cleans_partial_file(self):
        def chunks():
            yield PDF[:10]
            self.cancel.set()
            yield PDF[10:]
        self.client.session.request.side_effect = [Response(metadata()), Response(chunks=chunks())]
        with self.assertRaisesRegex(DriveError, "cancelled"):
            self.client.download("safe_file-123", self.cancel)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_redirect_is_not_followed_and_response_is_closed(self):
        response = Response(body=b"secret-url", status=302, headers={"Location": "https://evil.test"})
        self.client.session.request.return_value = response
        with self.assertRaises(DriveError) as error:
            self.client.download("safe_file-123", self.cancel)
        self.assertNotIn("secret", str(error.exception))
        self.assertTrue(response.closed)
        self.assertEqual(self.client.session.request.call_count, 1)

    def test_transport_errors_never_reveal_tokens_or_response(self):
        self.client.session.request.side_effect = requests.ConnectionError("secret-token https://evil.test")
        with self.assertRaises(DriveError) as error:
            self.client.download("safe_file-123", self.cancel)
        self.assertNotIn("secret-token", str(error.exception))
        self.assertNotIn("evil.test", str(error.exception))

    def test_upload_creates_new_copy_and_does_not_copy_sharing(self):
        document = self.download()
        calls = []
        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "GET":
                return Response(metadata(version="99"))  # Source changes cannot be overwritten.
            body = kwargs["data"]
            chunks = []
            while chunk := body.read(7):
                chunks.append(chunk)
            raw = b"".join(chunks)
            self.assertEqual(len(raw), int(kwargs["headers"]["Content-Length"]))
            self.assertIn(PDF, raw)
            self.assertNotIn(b'"parents"', raw)
            self.assertNotIn(b'"permissions"', raw)
            return Response({"id": "new_id", "name": "ignored"})
        self.client.session.request.side_effect = request
        result = self.client.upload(document, self.cancel)
        self.assertEqual(result["id"], "new_id")
        self.assertIn("(edited ", result["name"])
        self.assertEqual([call[0] for call in calls], ["GET", "POST"])
        self.assertEqual(calls[1][1], UPLOAD_ROOT)
        self.assertNotIn(document.file_id, calls[1][1])
        self.assertEqual(document.file_id, "safe_file-123")
        self.assertEqual(document.version, "11")

    def test_upload_ambiguity_does_not_retry_or_delete_local_edits(self):
        document = self.download()
        self.client.session.request.side_effect = [Response(metadata()), requests.Timeout("secret")]
        self.client.session.request.reset_mock()
        with self.assertRaisesRegex(DriveError, "not confirmed"):
            self.client.upload(document, self.cancel)
        self.assertEqual(self.client.session.request.call_count, 2)
        self.assertEqual(Path(document.path).read_bytes(), PDF)
        self.assertIn(document, self.client._documents)

    def test_requests_prepares_stream_without_reading_pdf_or_chunked_encoding(self):
        source = io.BytesIO(PDF)
        body = _MultipartUpload(source, len(PDF), "Edited.pdf", self.cancel, float("inf"))
        request = requests.Request("POST", UPLOAD_ROOT, data=body,
                                   headers={"Content-Type": f"multipart/related; boundary={body.boundary}",
                                            "Content-Length": str(len(body))}).prepare()
        self.assertIs(request.body, body)
        self.assertEqual(source.tell(), 0)
        self.assertEqual(request.headers["Content-Length"], str(len(body)))
        self.assertNotIn("Transfer-Encoding", request.headers)

    def test_changed_local_path_is_not_uploaded(self):
        document = self.download()
        document.path = str(Path(self.temp.name) / "different-private-file.pdf")
        self.client.session.request.reset_mock()
        with self.assertRaisesRegex(DriveError, "path is invalid"):
            self.client.upload(document, self.cancel)
        self.client.session.request.assert_not_called()

    def test_cleanup_failure_keeps_reference_for_retry(self):
        document = self.download()
        folder = self.client._documents[document]
        with patch.object(folder, "cleanup", side_effect=PermissionError):
            with self.assertRaisesRegex(DriveError, "could not be removed"):
                self.client.cleanup(document)
        self.assertIn(document, self.client._documents)
        self.assertTrue(Path(document.path).exists())
        self.client.cleanup(document)
        self.assertNotIn(document, self.client._documents)

    def test_copy_restriction_prevents_upload(self):
        document = self.download(metadata(capabilities={"canDownload": True, "canCopy": False}))
        self.client.session.request.side_effect = [Response(metadata())]
        self.client.session.request.reset_mock()
        with self.assertRaisesRegex(DriveError, "does not allow copies"):
            self.client.upload(document, self.cancel)
        self.assertEqual(self.client.session.request.call_count, 1)

    def test_metadata_payload_bound(self):
        self.client.session.request.return_value = Response(body=b" " * (65 * 1024))
        with self.assertRaisesRegex(DriveError, "large response"):
            self.client.download("safe_file-123", self.cancel)

    def test_cancelled_before_request_has_no_network_effect(self):
        self.cancel.set()
        with self.assertRaisesRegex(DriveError, "cancelled"):
            self.client.download("safe_file-123", self.cancel)
        self.client.session.request.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows ACL regression")
    def test_windows_private_acl_can_be_applied_before_writing(self):
        import ctypes
        from ctypes import wintypes

        directory = Path(self.temp.name) / "private"
        directory.mkdir()
        _restrict_windows_folder(str(directory))
        (directory / "test.pdf").write_bytes(PDF)
        self.assertEqual((directory / "test.pdf").read_bytes(), PDF)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                          ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
        advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
        kernel.LocalFree.argtypes = [wintypes.HLOCAL]
        for path, inherited in ((directory, False), (directory / "test.pdf", True)):
            size = wintypes.DWORD()
            advapi.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(size))
            descriptor = ctypes.create_string_buffer(size.value)
            self.assertTrue(advapi.GetFileSecurityW(str(path), 4, descriptor, size,
                                                   ctypes.byref(size)))
            text = wintypes.LPWSTR()
            self.assertTrue(advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, 1, 4, ctypes.byref(text), None))
            try:
                dacl = text.value
                self.assertEqual(dacl.count("(A;"), 2)
                # Windows may materialize a new file's inherited ACL as explicit
                # entries; both representations must grant only these principals.
                flags = "(?:ID)?" if inherited else "OICI"
                self.assertRegex(dacl, rf"\(A;{flags};FA;;;SY\)")
                self.assertRegex(dacl, rf"\(A;{flags};FA;;;S-1-(?:\d+-)+\d+\)")
                if not inherited:
                    self.assertTrue(dacl.startswith("D:P"), dacl)
            finally:
                kernel.LocalFree(ctypes.cast(text, wintypes.HLOCAL))


if __name__ == "__main__":
    unittest.main()
