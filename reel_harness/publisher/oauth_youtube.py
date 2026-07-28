from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import urllib.parse
from dataclasses import dataclass
from typing import Any

from reel_harness.core.errors import ProviderAuthError, TransientProviderError

# Per Google's installed-app OAuth guide (checked 2026-07-28 -- see
# docs/PUBLISHING.md).
AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CHANNELS_ENDPOINT = "https://www.googleapis.com/youtube/v3/channels"

# The minimal scope pair for this adapter's actual operations (upload +
# read-only channel identity check) -- not the broader `youtube` or
# `youtubepartner` scopes videos.insert also accepts. See docs/PUBLISHING.md.
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)


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


def build_authorization_url(client_id: str, redirect_uri: str, state: str, pkce: PKCEChallenge) -> str:
    """access_type=offline + prompt=consent guarantee a refresh_token is
    issued even on a re-authorization (Google otherwise only issues one on
    the very first consent) -- see docs/PUBLISHING.md."""
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "state": state,
        "code_challenge": pkce.challenge, "code_challenge_method": pkce.method,
        "access_type": "offline", "prompt": "consent",
    }
    return f"{AUTHORIZATION_ENDPOINT}?{urllib.parse.urlencode(params)}"


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str


@dataclass
class ChannelIdentity:
    channel_id: str
    title: str


class YouTubeOAuthClient:
    """Talks only to Google's OAuth token/channel-identity endpoints --
    never the upload endpoint itself (that's providers.youtube_publisher).
    Isolated so it can be contract-tested with httpx.MockTransport like
    every other adapter in this project. The client secret lives only in
    the request body sent to Google's own token endpoint; it never appears
    in an exception message, a log line, or a return value."""

    def __init__(
        self, client_id: str, client_secret: str, transport: Any = None,
        connect_timeout: float = 10.0, read_timeout: float = 30.0,
    ) -> None:
        import httpx

        self._client_id = client_id
        self._client_secret = client_secret
        self._httpx = httpx
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=30.0, pool=30.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> TokenResponse:
        return self._post_token({
            "client_id": self._client_id, "client_secret": self._client_secret,
            "code": code, "code_verifier": code_verifier, "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })

    def refresh(self, refresh_token: str) -> TokenResponse:
        return self._post_token({
            "client_id": self._client_id, "client_secret": self._client_secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        })

    def fetch_channel_identity(self, access_token: str) -> ChannelIdentity:
        try:
            response = self._client.get(
                CHANNELS_ENDPOINT, params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except self._httpx.TimeoutException as exc:
            raise TransientProviderError(f"channel identity request timed out ({type(exc).__name__})") from exc
        except self._httpx.HTTPError as exc:
            raise TransientProviderError(f"channel identity transport error ({type(exc).__name__})") from exc

        if response.status_code in (401, 403):
            raise ProviderAuthError(f"channel identity request rejected (HTTP {response.status_code})")
        if response.status_code != 200:
            raise TransientProviderError(f"channel identity endpoint returned HTTP {response.status_code}")
        try:
            payload = response.json()
            items = payload["items"]
            if not items:
                raise TransientProviderError("channel identity response had no channels for this account")
            first = items[0]
            return ChannelIdentity(channel_id=first["id"], title=first["snippet"]["title"])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise TransientProviderError("channel identity response missing required fields") from exc

    def _post_token(self, data: dict) -> TokenResponse:
        try:
            response = self._client.post(TOKEN_ENDPOINT, data=data)
        except self._httpx.TimeoutException as exc:
            raise TransientProviderError(f"oauth token request timed out ({type(exc).__name__})") from exc
        except self._httpx.HTTPError as exc:
            raise TransientProviderError(f"oauth token transport error ({type(exc).__name__})") from exc

        if response.status_code in (400, 401, 403):
            raise ProviderAuthError(f"oauth token request rejected (HTTP {response.status_code})")
        if response.status_code >= 500:
            raise TransientProviderError(f"oauth token endpoint returned HTTP {response.status_code}")
        if response.status_code != 200:
            raise TransientProviderError(f"oauth token endpoint returned unexpected HTTP {response.status_code}")
        try:
            payload = response.json()
            return TokenResponse(
                access_token=payload["access_token"], refresh_token=payload.get("refresh_token"),
                expires_in=int(payload.get("expires_in", 3600)), scope=payload.get("scope", ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TransientProviderError("oauth token response missing required fields") from exc


class OAuthCallbackError(Exception):
    pass


class LoopbackCallbackServer:
    """Binds to 127.0.0.1 on an OS-assigned ephemeral port, accepts exactly
    one redirect from the authorization server, validates `state`, and
    shuts down. The server is bound to the loopback interface only -- the
    OS never routes a connection from outside this machine to it, so
    "arbitrary remote access" is structurally impossible, not just
    filtered."""

    def __init__(self, expected_state: str, timeout_seconds: float = 300.0) -> None:
        self._expected_state = expected_state
        self._result: dict[str, str] = {}
        self._error: str | None = None
        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), self._make_handler())
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
