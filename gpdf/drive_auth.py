"""Session-only Google desktop OAuth with PKCE and the hosted PDF picker.

Call these blocking methods on the Drive worker, never on the Qt thread. Tokens
are kept only in memory. Neither callback requests nor HTTP errors are logged.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

import requests


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
AUTH_TIMEOUT = 180.0
MAX_RESPONSE_BYTES = 65536
FILE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


class DriveError(Exception):
    """An intentionally sanitized message suitable for displaying to a user."""


def _cancelled(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise DriveError("Google Drive operation cancelled.")


def _valid_secret(value: object, maximum: int = 8192) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and all(33 <= ord(char) <= 126 for char in value))


@dataclass(frozen=True)
class OAuthConfig:
    """A Google Desktop app client, without configurable network endpoints."""

    client_id: str
    client_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.client_id, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,200}\.apps\.googleusercontent\.com", self.client_id) is None
                or not _valid_secret(self.client_secret, 512)):
            raise DriveError("Choose a valid Google Desktop app OAuth client JSON file.")

    @classmethod
    def from_file(cls, path: str | Path) -> OAuthConfig:
        """Read Google's downloaded Desktop client JSON, with a size limit."""
        try:
            with Path(path).open("rb") as source:
                data = source.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError
            payload = json.loads(data)
            installed = payload["installed"]
            return cls(installed["client_id"], installed["client_secret"])
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise DriveError("Choose a valid Google Desktop app OAuth client JSON file.") from None

    @classmethod
    def for_app(cls) -> OAuthConfig:
        """Load the app publisher's client from the fixed package resource."""
        # Never discover credentials beside an opened PDF, in the process's
        # working directory, or through an environment variable.
        path = Path(__file__).with_name("google_oauth_client.json")
        try:
            return cls.from_file(path)
        except DriveError:
            raise DriveError(
                "Google sign-in is not configured in this build. The app developer needs to "
                "register Gpdf with Google and include its Desktop OAuth client."
            ) from None


class _LoopbackServer(HTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        # Prevent another local process binding this port on Windows.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(0.5)
        return connection, address

    def handle_error(self, request, client_address) -> None:
        # BaseServer's traceback could otherwise disclose a callback URL.
        pass


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "Gpdf"
    sys_version = ""

    def log_message(self, format, *args) -> None:
        pass

    def handle(self) -> None:
        # An absolute limit also stops a local client slowly dripping headers.
        def expire():
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(2.0, expire)
        timer.daemon = True
        timer.start()
        try:
            super().handle()
        finally:
            timer.cancel()

    def _reply(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        server = self.server
        host = f"127.0.0.1:{server.server_port}"
        if self.headers.get_all("Host") != [host] or len(self.path) > 8192:
            self._reply(400, "Invalid authorization response.")
            return
        try:
            parts = urlsplit(self.path)
            if parts.scheme or parts.netloc or parts.path != "/" or parts.fragment:
                raise ValueError
            params = parse_qs(parts.query, keep_blank_values=True, max_num_fields=20)
            if any(len(values) != 1 for values in params.values()):
                raise ValueError
            state = params.get("state", [""])[0]
            if not hmac.compare_digest(state.encode("utf-8"), server.oauth_state.encode("ascii")):
                raise ValueError
        except (ValueError, UnicodeError):
            self._reply(400, "Invalid authorization response.")
            return
        if server.oauth_result is not None:
            self._reply(409, "This authorization response was already received.")
            return
        if "error" in params:
            server.oauth_result = DriveError("Google Drive authorization was declined or cancelled.")
        else:
            code = params.get("code", [""])[0]
            picked_id = params.get("picked_file_ids", [""])[0]
            scope = params.get("scope", [""])[0]
            if (not _valid_secret(code) or FILE_ID_PATTERN.fullmatch(picked_id) is None
                    or set(scope.split()) != {DRIVE_SCOPE}):
                server.oauth_result = DriveError("Google did not return a PDF selection with the required limited permission.")
            else:
                server.oauth_result = (code, [picked_id])
        self._reply(200, "You can close this tab and return to Gpdf.")


class DriveAuth:
    """Own one session's Google credentials; no token is written to disk."""

    def __init__(self, config: OAuthConfig):
        self.config = config
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        self._session = requests.Session()
        # Do not silently send tokens through environment proxy settings, load
        # .netrc authentication, or accept environment CA bundle overrides.
        self._session.trust_env = False

    def clear(self) -> None:
        """Forget local credentials without contacting Google."""
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        self._session.cookies.clear()

    def close(self) -> None:
        """Forget credentials and release this session's network resources."""
        self.clear()
        self._session.close()

    def _post(self, endpoint: str, data: dict, cancel: threading.Event) -> dict:
        _cancelled(cancel)
        if endpoint not in (TOKEN_URL, REVOKE_URL):
            raise DriveError("Unsupported Google authorization endpoint.")
        try:
            with self._session.post(endpoint, data=data, timeout=(5, 15),
                                    allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise DriveError("Google authorization failed. Reconnect to Google Drive and try again.")
                received = bytearray()
                deadline = time.monotonic() + 30
                for chunk in response.iter_content(4096):
                    _cancelled(cancel)
                    if time.monotonic() > deadline or len(received) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise DriveError("Google returned an invalid authorization response.")
                    received.extend(chunk)
                _cancelled(cancel)
                if endpoint == REVOKE_URL:
                    return {}
                payload = json.loads(received)
                if not isinstance(payload, dict):
                    raise ValueError
                return payload
        except (requests.RequestException, OSError):
            raise DriveError("Could not securely connect to Google. Check your connection and try again.") from None
        except (ValueError, TypeError, RecursionError):
            raise DriveError("Google returned an invalid authorization response.") from None

    def _install_token(self, payload: dict, *, refreshing: bool = False) -> None:
        # A refresh response may omit scope, meaning the original grant applies.
        scope = payload.get("scope", DRIVE_SCOPE if refreshing else "")
        token = payload.get("access_token")
        refresh = payload.get("refresh_token", self._refresh_token if refreshing else None)
        expiry = payload.get("expires_in")
        token_type = payload.get("token_type")
        if (not isinstance(scope, str) or set(scope.split()) != {DRIVE_SCOPE}
                or not isinstance(token_type, str) or token_type.lower() != "bearer"
                or not _valid_secret(token)
                or (refresh is not None and not _valid_secret(refresh))
                or isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                or not 0 < expiry <= 86400):
            self.clear()
            raise DriveError("Google returned credentials outside the required limited permission. Reconnect to Drive.")
        self._access_token = token
        self._refresh_token = refresh
        self._expires_at = time.monotonic() + expiry - min(60, expiry / 10)

    def authorize(self, cancel: threading.Event) -> list[str]:
        """Open Google's system-browser PDF picker and validate its response."""
        _cancelled(cancel)
        self.clear()
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        try:
            server = _LoopbackServer(("127.0.0.1", 0), _CallbackHandler)
        except OSError:
            raise DriveError("Could not start the local Google sign-in callback.") from None
        with server:
            server.timeout = 0.2
            server.oauth_state = secrets.token_urlsafe(32)
            server.oauth_result = None
            redirect_uri = f"http://127.0.0.1:{server.server_port}/"
            params = {
                "client_id": self.config.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": DRIVE_SCOPE,
                "state": server.oauth_state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "include_granted_scopes": "false",
                "prompt": "consent",
                "trigger_onepick": "true",
                "mimetypes": "application/pdf",
                "allow_multiple": "false",
            }
            try:
                opened = webbrowser.open(AUTH_URL + "?" + urlencode(params), new=1)
            except (webbrowser.Error, OSError):
                opened = False
            if not opened:
                raise DriveError("Could not open your browser for Google sign-in.")
            deadline = time.monotonic() + AUTH_TIMEOUT
            while server.oauth_result is None:
                _cancelled(cancel)
                if time.monotonic() >= deadline:
                    raise DriveError("Google sign-in timed out. Try opening Google Drive again.")
                server.handle_request()
            _cancelled(cancel)
            if isinstance(server.oauth_result, DriveError):
                raise server.oauth_result
            code, selected = server.oauth_result
        payload = self._post(TOKEN_URL, {
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }, cancel)
        _cancelled(cancel)
        self._install_token(payload)
        return selected

    def access_token(self, cancel: threading.Event) -> str:
        """Return a narrow token, refreshing only the in-memory grant if needed."""
        _cancelled(cancel)
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token
        if not self._refresh_token:
            self.clear()
            raise DriveError("Connect to Google Drive to access this file.")
        try:
            payload = self._post(TOKEN_URL, {
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            }, cancel)
            _cancelled(cancel)
            self._install_token(payload, refreshing=True)
        except DriveError:
            self.clear()
            raise
        return self._access_token

    def disconnect(self, cancel: threading.Event) -> None:
        """Clear local tokens and attempt to revoke the Google grant."""
        token = self._refresh_token or self._access_token
        self.clear()
        if token:
            try:
                self._post(REVOKE_URL, {"token": token}, cancel)
            except DriveError:
                raise DriveError("Signed out locally. Google access could not be revoked; remove Gpdf in your Google Account connections if needed.") from None
