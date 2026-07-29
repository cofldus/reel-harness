from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any

from reel_harness.core.errors import ProviderAuthError, TransientProviderError
from reel_harness.publisher.oauth_common import (
    LoopbackCallbackServer,
    OAuthCallbackError,
    PKCEChallenge,
    generate_pkce,
    generate_state,
)

__all__ = [
    "AUTHORIZATION_ENDPOINT",
    "TOKEN_ENDPOINT",
    "CHANNELS_ENDPOINT",
    "SCOPES",
    "PKCEChallenge",
    "generate_pkce",
    "generate_state",
    "build_authorization_url",
    "TokenResponse",
    "ChannelIdentity",
    "YouTubeOAuthClient",
    "OAuthCallbackError",
    "LoopbackCallbackServer",
]

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

# PKCEChallenge/generate_pkce/generate_state/OAuthCallbackError/
# LoopbackCallbackServer have no YouTube-specific logic and live in
# publisher.oauth_common (shared with publisher.oauth_tiktok); re-exported
# here so existing call sites (`from .oauth_youtube import generate_pkce`,
# etc.) keep working unchanged.


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
