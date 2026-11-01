"""OAuth tests use synthetic credentials, mocked Google HTTP and real loopback."""

import base64
from contextlib import chdir
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from gpdf.drive_auth import (
    AUTH_URL, DRIVE_SCOPE, MAX_RESPONSE_BYTES, REVOKE_URL, TOKEN_URL,
    DriveAuth, DriveError, OAuthConfig,
)


class Response:
    def __init__(self, payload=None, status=200, content=None):
        self.status_code = status
        self.content = content if content is not None else json.dumps(payload).encode()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, size):
        for offset in range(0, len(self.content), size):
            yield self.content[offset:offset + size]


def token_payload(**changes):
    payload = {"access_token": "test-access", "refresh_token": "test-refresh",
               "scope": DRIVE_SCOPE, "token_type": "Bearer", "expires_in": 3600}
    payload.update(changes)
    return payload


class DriveAuthTests(unittest.TestCase):
    def setUp(self):
        self.config = OAuthConfig("123-test.apps.googleusercontent.com", "test-secret")
        self.auth = DriveAuth(self.config)
        self.cancel = threading.Event()
        self.threads = []
        self.callbacks = []
        self.thread_errors = []
        self.auth_url = None
        self.post = Mock(return_value=Response(token_payload()))
        self.auth._session.post = self.post

    def tearDown(self):
        self.auth.clear()
        self.auth._session.close()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        if self.thread_errors:
            self.fail(str(self.thread_errors))

    def browser(self, url, new=1):
        self.auth_url = url
        params = parse_qs(urlsplit(url).query)
        redirect = urlsplit(params["redirect_uri"][0])
        good = {"state": params["state"][0], "code": "test-code",
                "scope": DRIVE_SCOPE, "picked_file_ids": "test-file_1"}

        def callback():
            try:
                for path, overrides, host in self.callbacks + [("/", {}, None)]:
                    query = dict(good)
                    query.update(overrides)
                    target = path + "?" + urlencode(query, doseq=True)
                    connection = http.client.HTTPConnection(redirect.hostname, redirect.port, timeout=3)
                    headers = {"Host": host} if host else {}
                    connection.request("GET", target, headers=headers)
                    response = connection.getresponse()
                    body = response.read()
                    self.callback_results.append((response.status, body, dict(response.headers)))
                    connection.close()
            except Exception as error:
                self.thread_errors.append(type(error).__name__)
        self.callback_results = []
        thread = threading.Thread(target=callback, daemon=True)
        self.threads.append(thread)
        thread.start()
        return True

    def test_pkce_picker_limited_scope_and_memory_only_token(self):
        with patch("gpdf.drive_auth.webbrowser.open", self.browser), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(self.auth.authorize(self.cancel), ["test-file_1"])
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(self.auth.access_token(self.cancel), "test-access")
        params = parse_qs(urlsplit(self.auth_url).query)
        self.assertTrue(self.auth_url.startswith(AUTH_URL + "?"))
        self.assertEqual(params["scope"], [DRIVE_SCOPE])
        self.assertEqual(params["trigger_onepick"], ["true"])
        self.assertEqual(params["mimetypes"], ["application/pdf"])
        self.assertEqual(params["allow_multiple"], ["false"])
        self.assertEqual(params["include_granted_scopes"], ["false"])
        self.assertEqual(params["prompt"], ["consent"])
        args, kwargs = self.post.call_args
        self.assertEqual(args[0], TOKEN_URL)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertFalse(self.auth._session.trust_env)
        verifier = kwargs["data"]["code_verifier"]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(params["code_challenge"], [challenge])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertGreaterEqual(len(verifier), 43)
        self.assertGreaterEqual(len(params["state"][0]), 43)
        self.assertNotIn("test-secret", self.auth_url)
        self.assertNotIn("test-access", self.auth_url)
        for thread in self.threads:
            thread.join(3)
        self.assertEqual(self.callback_results[-1][2]["Cache-Control"], "no-store")
        self.assertNotIn(b"test-code", self.callback_results[-1][1])

    def test_callback_rejects_state_host_path_and_duplicate_params(self):
        self.callbacks = [
            ("/", {"state": "forged"}, None),
            ("/", {"state": "nonascii-\u2603"}, None),
            ("/", {}, "evil.example"),
            ("/unexpected", {}, None),
            ("/", {"code": ["test-code", "duplicate"]}, None),
        ]
        with patch("gpdf.drive_auth.webbrowser.open", self.browser):
            self.auth.authorize(self.cancel)
        for thread in self.threads:
            thread.join(3)
        self.assertEqual([result[0] for result in self.callback_results], [400] * 5 + [200])
        self.post.assert_called_once()

    def test_callback_scope_expansion_and_multiple_file_ids_fail_closed(self):
        for override in ({"scope": DRIVE_SCOPE + " https://www.googleapis.com/auth/drive"},
                         {"picked_file_ids": "first,second"}, {"picked_file_ids": "../../escape"}):
            with self.subTest(override=override):
                # Only send the invalid terminal response, without the helper's good response.
                def browser(url, new=1):
                    params = parse_qs(urlsplit(url).query)
                    redirect = urlsplit(params["redirect_uri"][0])
                    query = {"state": params["state"][0], "code": "test-code",
                             "scope": DRIVE_SCOPE, "picked_file_ids": "test-file"}
                    query.update(override)
                    def send():
                        connection = http.client.HTTPConnection(redirect.hostname, redirect.port, timeout=3)
                        connection.request("GET", "/?" + urlencode(query))
                        connection.getresponse().read()
                        connection.close()
                    thread = threading.Thread(target=send, daemon=True)
                    self.threads.append(thread)
                    thread.start()
                    return True
                with patch("gpdf.drive_auth.webbrowser.open", browser), self.assertRaises(DriveError):
                    self.auth.authorize(self.cancel)
                self.assertIsNone(self.auth._access_token)
        self.post.assert_not_called()

    def test_config_accepts_only_desktop_and_ignores_remote_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.json"
            installed = {"client_id": self.config.client_id, "client_secret": "test-secret",
                         "token_uri": "http://attacker.invalid/token", "auth_uri": "http://attacker.invalid"}
            path.write_text(json.dumps({"installed": installed}), encoding="utf-8")
            config = OAuthConfig.from_file(path)
            self.assertEqual(config, self.config)
            self.assertNotIn("test-secret", repr(config))
            path.write_text(json.dumps({"web": installed}), encoding="utf-8")
            with self.assertRaises(DriveError):
                OAuthConfig.from_file(path)
            path.write_bytes(b"x" * (MAX_RESPONSE_BYTES + 1))
            with self.assertRaises(DriveError):
                OAuthConfig.from_file(path)

    def test_app_config_loads_only_the_bundled_desktop_client(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            unrelated = root / "unrelated"
            package.mkdir()
            unrelated.mkdir()
            payload = {"installed": {"client_id": self.config.client_id,
                                     "client_secret": "test-secret"}}
            (package / "google_oauth_client.json").write_text(json.dumps(payload), encoding="utf-8")
            payload["installed"]["client_id"] = "attacker.apps.googleusercontent.com"
            (unrelated / "google_oauth_client.json").write_text(json.dumps(payload), encoding="utf-8")
            with patch("gpdf.drive_auth.__file__", str(package / "drive_auth.py")), chdir(unrelated):
                self.assertEqual(OAuthConfig.for_app(), self.config)

    def test_missing_app_config_does_not_fall_back_to_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            package.mkdir()
            (root / "google_oauth_client.json").write_text(json.dumps({"installed": {
                "client_id": self.config.client_id, "client_secret": "test-secret",
            }}), encoding="utf-8")
            with patch("gpdf.drive_auth.__file__", str(package / "drive_auth.py")), chdir(root):
                with self.assertRaisesRegex(DriveError, "not configured in this build") as error:
                    OAuthConfig.for_app()
                self.assertNotIn(str(root), str(error.exception))

    def test_invalid_bundled_config_reports_a_build_configuration_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "google_oauth_client.json").write_text("not-json-private", encoding="utf-8")
            with patch("gpdf.drive_auth.__file__", str(root / "drive_auth.py")):
                with self.assertRaisesRegex(DriveError, "not configured in this build") as error:
                    OAuthConfig.for_app()
                self.assertNotIn("not-json-private", str(error.exception))

    def test_expanded_missing_or_malformed_tokens_are_rejected(self):
        for changes in ({"scope": DRIVE_SCOPE + " other"}, {"scope": None},
                        {"token_type": None}, {"access_token": "bad\r\nheader"},
                        {"expires_in": True}, {"expires_in": float("nan")},
                        {"refresh_token": []}):
            with self.subTest(changes=changes), self.assertRaises(DriveError):
                self.auth._install_token(token_payload(**changes))
            self.assertIsNone(self.auth._access_token)
        payload = token_payload()
        del payload["scope"]
        with self.assertRaises(DriveError):
            self.auth._install_token(payload)

    def test_deeply_nested_config_and_token_json_fail_with_safe_messages(self):
        nested = b"[" * 2000 + b'"private-value"' + b"]" * 2000
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.json"
            path.write_bytes(nested)
            with self.assertRaisesRegex(DriveError, "valid Google Desktop") as error:
                OAuthConfig.from_file(path)
            self.assertNotIn("private-value", str(error.exception))
        self.post.return_value = Response(content=nested)
        with self.assertRaisesRegex(DriveError, "invalid authorization response") as error:
            self.auth._post(TOKEN_URL, {}, self.cancel)
        self.assertNotIn("private-value", str(error.exception))

    def test_refresh_uses_prior_narrow_scope_if_omitted(self):
        self.auth._install_token(token_payload())
        self.auth._expires_at = 0
        payload = token_payload(access_token="new-access")
        del payload["scope"]
        del payload["refresh_token"]
        self.post.return_value = Response(payload)
        self.assertEqual(self.auth.access_token(self.cancel), "new-access")
        self.assertEqual(self.auth._refresh_token, "test-refresh")
        self.assertEqual(self.post.call_args.kwargs["data"]["grant_type"], "refresh_token")

    def test_refresh_scope_expansion_clears_old_credentials(self):
        self.auth._install_token(token_payload())
        self.auth._expires_at = 0
        self.post.return_value = Response(token_payload(scope="https://www.googleapis.com/auth/drive"))
        with self.assertRaises(DriveError):
            self.auth.access_token(self.cancel)
        self.assertIsNone(self.auth._refresh_token)
        self.assertIsNone(self.auth._access_token)

    def test_network_errors_and_redirects_never_expose_secrets(self):
        self.post.side_effect = requests.RequestException("secret-token https://attacker.invalid")
        with self.assertRaises(DriveError) as error:
            self.auth._post(TOKEN_URL, {}, self.cancel)
        self.assertNotIn("secret-token", str(error.exception))
        self.post.side_effect = None
        response = Response({"error": "secret-code"}, status=302)
        self.post.return_value = response
        with self.assertRaises(DriveError) as error:
            self.auth._post(TOKEN_URL, {}, self.cancel)
        self.assertNotIn("secret-code", str(error.exception))
        self.assertTrue(response.closed)

    def test_token_response_size_and_destination_are_bounded(self):
        self.post.return_value = Response(content=b"x" * (MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(DriveError):
            self.auth._post(TOKEN_URL, {}, self.cancel)
        self.post.reset_mock()
        with self.assertRaises(DriveError):
            self.auth._post("https://attacker.invalid/", {}, self.cancel)
        self.post.assert_not_called()

    def test_cancellation_and_browser_failure_make_no_http_request(self):
        self.cancel.set()
        with self.assertRaises(DriveError):
            self.auth.authorize(self.cancel)
        self.cancel.clear()
        with patch("gpdf.drive_auth.webbrowser.open", return_value=False), self.assertRaises(DriveError):
            self.auth.authorize(self.cancel)
        with patch("gpdf.drive_auth.webbrowser.open", return_value=True), patch("gpdf.drive_auth.AUTH_TIMEOUT", 0.01):
            with self.assertRaisesRegex(DriveError, "timed out"):
                self.auth.authorize(self.cancel)
        self.post.assert_not_called()

    def test_disconnect_revokes_with_body_and_clears_even_on_failure(self):
        self.auth._install_token(token_payload())
        self.post.return_value = Response(content=b"")
        self.auth.disconnect(self.cancel)
        self.assertIsNone(self.auth._access_token)
        self.assertIsNone(self.auth._refresh_token)
        self.assertEqual(self.post.call_args.args[0], REVOKE_URL)
        self.assertEqual(self.post.call_args.kwargs["data"], {"token": "test-refresh"})
        self.auth._install_token(token_payload())
        self.post.side_effect = requests.RequestException("private")
        with self.assertRaisesRegex(DriveError, "Signed out locally"):
            self.auth.disconnect(self.cancel)
        self.assertIsNone(self.auth._access_token)
        self.assertIsNone(self.auth._refresh_token)


if __name__ == "__main__":
    unittest.main()
