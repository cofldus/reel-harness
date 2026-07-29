from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import urllib.parse
from dataclasses import dataclass

# Shared by every OAuth-based publisher adapter (YouTube, TikTok, ...) --
# PKCE/state generation and the loopback callback server have no
# provider-specific logic, so they live here once instead of being
# duplicated per adapter module. See docs/PUBLISHING.md.


@dataclass(frozen=True)
class PKCEChallenge:
    verifier: str
    challenge: str
    method: str = "S256"


def generate_pkce() -> PKCEChallenge:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PKCEChallenge(verifier=verifier, challenge=challenge)


def generate_state() -> str:
    return secrets.token_urlsafe(24)


class OAuthCallbackError(Exception):
    pass


class LoopbackCallbackServer:
    """Binds to 127.0.0.1 on an OS-assigned ephemeral port, accepts exactly
    one redirect from the authorization server, validates `state`, and
    shuts down. The server is bound to the loopback interface only -- the
    OS never routes a connection from outside this machine to it, so
    "arbitrary remote access" is structurally impossible, not just
    filtered."""

    def __init__(self, expected_state: str, timeout_seconds: float = 300.0, port: int = 0) -> None:
        """`port=0` (the default, used by YouTube) binds an OS-assigned
        ephemeral port -- Google's installed-app flow accepts any
        http://127.0.0.1:PORT as an implicit redirect, no fixed
        registration needed. A provider that instead requires an exact
        pre-registered redirect_uri (TikTok) must pass that URI's actual
        port here so the listener matches what was registered -- an
        ephemeral port would never receive the callback."""
        self._expected_state = expected_state
        self._result: dict[str, str] = {}
        self._error: str | None = None
        self._httpd = http.server.HTTPServer(("127.0.0.1", port), self._make_handler())
        self._httpd.timeout = timeout_seconds

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _make_handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:  # silence default stderr logging
                pass

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
                parsed = urllib.parse.urlsplit(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                state = params.get("state", [None])[0]
                code = params.get("code", [None])[0]
                error = params.get("error", [None])[0]
                if error:
                    outer._error = error
                elif state != outer._expected_state:
                    outer._error = "state_mismatch"
                elif not code:
                    outer._error = "missing_code"
                else:
                    outer._result["code"] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"You may close this window and return to the terminal.")

        return Handler

    def wait_for_code(self) -> str:
        """Blocks for exactly one request (or until the configured timeout
        with no request received). Returns the authorization code; raises
        OAuthCallbackError on an error= param, a state mismatch, a missing
        code, or a timeout. The code itself is never logged by this class
        -- only returned to the caller."""
        self._httpd.handle_request()
        self._httpd.server_close()
        if self._error:
            raise OAuthCallbackError(self._error)
        if "code" not in self._result:
            raise OAuthCallbackError("timeout waiting for the oauth callback")
        return self._result["code"]
