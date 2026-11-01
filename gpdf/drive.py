"""Selected-file Drive transfers, called only from a background thread.

Saving always creates a new file in My Drive. Drive v3 does not expose a
documented atomic content-update precondition, so originals are never replaced.
No PDF parsing, account-wide enumeration, logging, or persistent tokens occur here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time

import requests

from .drive_auth import DriveAuth, DriveError


API_ROOT = "https://www.googleapis.com/drive/v3/files"
UPLOAD_ROOT = "https://www.googleapis.com/upload/drive/v3/files"
MAX_PDF_BYTES = 512 * 1024 * 1024
CHUNK_BYTES = 128 * 1024
MAX_METADATA_BYTES = 64 * 1024
TRANSFER_SECONDS = 30 * 60
FILE_FIELDS = "id,name,mimeType,size,version,md5Checksum,trashed,capabilities(canDownload,canCopy)"
FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


def _check_cancel(cancel: threading.Event, deadline: float | None = None) -> None:
    if cancel.is_set():
        raise DriveError("Google Drive operation cancelled.")
    if deadline is not None and time.monotonic() > deadline:
        raise DriveError("Google Drive transfer timed out. Your local edits are retained.")


def _private_temp() -> tempfile.TemporaryDirectory:
    """Create a private, nonsynced working directory before writing PDF bytes."""
    if os.name != "nt":
        return tempfile.TemporaryDirectory(prefix="gpdf-drive-")
    # Python < 3.13 does not enforce Windows ACLs for mkdir(mode=0o700).
    # Resolve LocalAppData through Windows, not TEMP (which may be shared/synced).
    import ctypes
    from ctypes import wintypes

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    base = ctypes.create_unicode_buffer(32768)
    shell.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE,
                                     wintypes.DWORD, wintypes.LPWSTR]
    if shell.SHGetFolderPathW(None, 0x1C, None, 0, base) != 0:
        raise DriveError("A private local working folder could not be created.")
    folder = tempfile.TemporaryDirectory(prefix="gpdf-drive-", dir=base.value)
    try:
        _restrict_windows_folder(folder.name)
        return folder
    except Exception:
        folder.cleanup()
        raise DriveError("A private local working folder could not be secured.") from None


def _restrict_windows_folder(path: str) -> None:
    """Grant inherited full access only to this Windows user and SYSTEM."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                         ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                            ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD)]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    token = wintypes.HANDLE()
    sid_text = wintypes.LPWSTR()
    descriptor = ctypes.c_void_p()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise OSError("Cannot read current user token")
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        data = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
            raise OSError("Cannot read current user SID")
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise OSError("Cannot encode current user SID")
        sddl = f"D:P(A;OICI;FA;;;{sid_text.value})(A;OICI;FA;;;SY)"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):
            raise OSError("Cannot create private ACL")
        if not advapi.SetFileSecurityW(path, 0x00000004 | 0x80000000, descriptor):
            raise OSError("Cannot apply private ACL")
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
        if token:
            kernel.CloseHandle(token)


@dataclass(eq=False)
class DriveDocument:
    """A selected remote PDF and its private local working copy."""

    file_id: str
    name: str
    path: str
    version: str
    can_copy: bool
    size: int


class _MultipartUpload:
    """File-like multipart body that never buffers an entire PDF in memory."""

    def __init__(self, file, size: int, name: str, cancel: threading.Event, deadline: float):
        self.file = file
        self.cancel = cancel
        self.deadline = deadline
        self.boundary = "gpdf-" + secrets.token_hex(24)
        metadata = json.dumps({"name": name, "mimeType": "application/pdf"},
                              ensure_ascii=True).encode("utf-8")
        self.prefix = (f"--{self.boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                       + metadata + f"\r\n--{self.boundary}\r\nContent-Type: application/pdf\r\n\r\n".encode())
        self.suffix = f"\r\n--{self.boundary}--\r\n".encode()
        self.size = size
        self.position = 0

    def __len__(self):
        return len(self.prefix) + self.size + len(self.suffix)

    def read(self, amount=CHUNK_BYTES):
        _check_cancel(self.cancel, self.deadline)
        amount = min(CHUNK_BYTES, amount) if amount and amount > 0 else CHUNK_BYTES
        chunks = []
        while amount and self.position < len(self):
            if self.position < len(self.prefix):
                chunk = self.prefix[self.position:self.position + amount]
            elif self.position < len(self.prefix) + self.size:
                chunk = self.file.read(min(amount, len(self.prefix) + self.size - self.position))
                if not chunk:
                    raise DriveError("The local PDF changed during upload. Save again before retrying.")
            else:
                offset = self.position - len(self.prefix) - self.size
                chunk = self.suffix[offset:offset + amount]
            chunks.append(chunk)
            self.position += len(chunk)
            amount -= len(chunk)
        return b"".join(chunks)


class DriveClient:
    """Stream only explicitly selected PDFs directly to/from Google's API."""

    def __init__(self, auth: DriveAuth):
        self.auth = auth
        self.session = requests.Session()
        self.session.trust_env = False  # No implicit proxies, .netrc, or CA overrides.
        self._documents: dict[DriveDocument, tempfile.TemporaryDirectory] = {}

    def _request(self, method, url, cancel, **kwargs):
        _check_cancel(cancel)
        headers = kwargs.pop("headers", {})
        headers.update({"Authorization": "Bearer " + self.auth.access_token(cancel),
                        "Accept-Encoding": "identity"})
        try:
            response = self.session.request(method, url, headers=headers, timeout=(5, 30),
                                            allow_redirects=False, stream=True, **kwargs)
        except requests.RequestException:
            raise DriveError("Could not contact Google Drive. Check your connection and sign in again if needed.") from None
        if response.status_code not in (200, 201):
            response.close()
            if response.status_code in (401, 403):
                raise DriveError("Google Drive denied access. Sign in again or check this file's permissions.")
            if response.status_code == 404:
                raise DriveError("The selected Google Drive file is no longer available.")
            if response.status_code == 429:
                raise DriveError("Google Drive is busy. Try again later.")
            raise DriveError("Google Drive could not complete the request.")
        return response

    def _json(self, response, cancel):
        try:
            content = bytearray()
            deadline = time.monotonic() + 60
            for chunk in response.iter_content(CHUNK_BYTES):
                _check_cancel(cancel, deadline)
                content.extend(chunk)
                if len(content) > MAX_METADATA_BYTES:
                    raise DriveError("Google Drive returned an unexpectedly large response.")
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, requests.RequestException):
            raise DriveError("Google Drive returned an invalid response.") from None
        finally:
            response.close()

    def _metadata(self, file_id, cancel):
        response = self._request("GET", f"{API_ROOT}/{file_id}", cancel,
                                 params={"fields": FILE_FIELDS, "supportsAllDrives": "true"})
        metadata = self._json(response, cancel)
        if metadata.get("id") != file_id or metadata.get("mimeType") != "application/pdf" or metadata.get("trashed"):
            raise DriveError("Select an available PDF file in Google Drive.")
        capabilities = metadata.get("capabilities")
        if not isinstance(capabilities, dict) or capabilities.get("canDownload") is not True:
            raise DriveError("The file owner does not allow this PDF to be downloaded.")
        try:
            size = int(metadata["size"])
            version = str(metadata["version"])
            checksum = metadata["md5Checksum"]
            if not 5 <= size <= MAX_PDF_BYTES:
                raise DriveError("Google Drive PDFs must be between 5 bytes and 512 MiB.")
            if not version.isdecimal() or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", checksum):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise DriveError("Google Drive did not provide the PDF's size and integrity information.") from None
        return metadata

    def download(self, file_id: str, cancel: threading.Event) -> DriveDocument:
        """Download one selected PDF with bounded memory and integrity checks."""
        if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
            raise DriveError("Google Drive returned an invalid file selection.")
        before = self._metadata(file_id, cancel)
        folder = _private_temp()
        path = Path(folder.name) / "working.pdf"
        response = None
        try:
            response = self._request("GET", f"{API_ROOT}/{file_id}", cancel,
                                     params={"alt": "media", "supportsAllDrives": "true"})
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_PDF_BYTES):
                raise DriveError("The Google Drive download exceeds the allowed size.")
            digest = hashlib.md5(usedforsecurity=False)  # Drive's blob integrity checksum.
            total = 0
            deadline = time.monotonic() + TRANSFER_SECONDS
            with path.open("xb") as destination:
                for chunk in response.iter_content(CHUNK_BYTES):
                    _check_cancel(cancel, deadline)
                    total += len(chunk)
                    if total > MAX_PDF_BYTES or total > int(before["size"]):
                        raise DriveError("The Google Drive download exceeds the expected size.")
                    destination.write(chunk)
                    digest.update(chunk)
            response.close()
            response = None
            with path.open("rb") as source:
                if source.read(5) != b"%PDF-":
                    raise DriveError("The selected file is not a PDF document.")
            after = self._metadata(file_id, cancel)
            if (str(after["version"]) != str(before["version"])
                    or after["md5Checksum"].lower() != before["md5Checksum"].lower()
                    or total != int(before["size"])
                    or digest.hexdigest() != before["md5Checksum"].lower()):
                raise DriveError("This PDF changed during download. Select it again to get a consistent copy.")
            name = before.get("name")
            # Display only; never use remote text as a filesystem path or HTML.
            name = "".join(c for c in name if c.isprintable())[:240] if isinstance(name, str) else "Drive PDF.pdf"
            document = DriveDocument(file_id, name or "Drive PDF.pdf", str(path), str(before["version"]),
                                     before["capabilities"].get("canCopy") is True, total)
            self._documents[document] = folder
            return document
        except (requests.RequestException, OSError):
            raise DriveError("The PDF could not be downloaded securely. Check your connection and local disk space.") from None
        finally:
            if response is not None:
                response.close()
            if not any(saved is folder for saved in self._documents.values()):
                folder.cleanup()

    def upload(self, document: DriveDocument, cancel: threading.Event) -> dict:
        """Create an edited copy without copying the original's sharing settings."""
        if document not in self._documents:
            raise DriveError("This Google Drive working copy is no longer available.")
        expected_path = Path(self._documents[document].name) / "working.pdf"
        if Path(document.path) != expected_path:
            raise DriveError("The Google Drive working copy path is invalid.")
        metadata = self._metadata(document.file_id, cancel)
        if not document.can_copy or metadata["capabilities"].get("canCopy") is not True:
            raise DriveError("The file owner does not allow copies of this PDF.")
        name = document.name[:-4] if document.name.lower().endswith(".pdf") else document.name
        name = f"{name[:180]} (edited {datetime.now(timezone.utc):%Y%m%d-%H%M%S}).pdf"
        try:
            with open(document.path, "rb") as source:
                size = os.fstat(source.fileno()).st_size
                if not 5 <= size <= MAX_PDF_BYTES or source.read(5) != b"%PDF-":
                    raise DriveError("The local PDF is invalid or larger than 512 MiB.")
                source.seek(0)
                body = _MultipartUpload(source, size, name, cancel,
                                        time.monotonic() + TRANSFER_SECONDS)
                # This single POST is never automatically retried. A lost response
                # may mean a copy exists; the UI must retain local edits either way.
                try:
                    response = self._request("POST", UPLOAD_ROOT, cancel,
                                             params={"uploadType": "multipart", "fields": "id,name"},
                                             headers={"Content-Type": f"multipart/related; boundary={body.boundary}",
                                                      "Content-Length": str(len(body))}, data=body)
                    result = self._json(response, cancel)
                    if not FILE_ID.fullmatch(str(result.get("id", ""))):
                        raise DriveError("Invalid upload result")
                    return {"id": result["id"], "name": name}
                except DriveError:
                    raise DriveError("The upload was not confirmed. Your local edits are retained. "
                                     "Check My Drive for an edited copy before retrying to avoid duplicates.") from None
        except OSError:
            raise DriveError("The local working PDF could not be read. Your edits have not been uploaded.") from None

    def cleanup(self, document: DriveDocument) -> None:
        """Remove a working copy after the PDF worker has released its file."""
        folder = self._documents.get(document)
        if folder is not None:
            try:
                folder.cleanup()
            except OSError:
                raise DriveError("A private working PDF could not be removed. Close Gpdf and remove it from LocalAppData.") from None
            del self._documents[document]

    def close(self) -> None:
        """Remove working copies and close HTTP resources after all jobs finish."""
        failure = None
        try:
            for document in list(self._documents):
                try:
                    self.cleanup(document)
                except DriveError as error:
                    failure = error
        finally:
            self.session.close()
            self.auth.close()
        if failure is not None:
            raise failure
