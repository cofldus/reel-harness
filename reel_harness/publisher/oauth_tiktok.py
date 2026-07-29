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
    "SCOPES",
    "PKCEChallenge",
    "generate_pkce",
    "generate_state",
    "build_authorization_url",
    "TokenResponse",
    "TikTokOAuthClient",
    "OAuthCallbackError",
    "LoopbackCallbackServer",
]

# Per the official TikTok Login Kit / Content Posting API docs (checked
# 2026-07-29 -- see docs/PUBLISHING.md). Unlike Google's fixed endpoints,
# these are read from Settings (config.tiktok_auth_url/tiktok_token_url) by
# every call site below -- there is no hardcoded module-level endpoint the
# way oauth_youtube.py has one, since a contract-test fake server needs to
# substitute its own.

# This adapter only ever requests video.publish (Direct Post) -- never
# video.upload (upload-only/inbox draft) or user.info.basic (unneeded
# beyond the open_id the token response already carries).
SCOPES = ("video.publish",)


def build_authorization_url(
    client_key: str, redirect_uri: str, state: str, pkce: PKCEChallenge, auth_url: str,
) -> str:
    """PKCE is required for this app type per TikTok's docs (desktop/CLI is
    a public client, same reasoning as YouTube's installed-app flow)."""
    params = {
        "client_key": client_key, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": ",".join(SCOPES), "state": state,
        "code_challenge": pkce.challenge, "code_challenge_method": pkce.method,
    }
    return f"{auth_url}?{urllib.parse.urlencode(params)}"


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int
    refresh_expires_in: int | None
    scope: str
    open_id: str


class TikTokOAuthClient:
    """Talks only to TikTok's OAuth token endpoint -- never the Content
    Posting API itself (that's providers.tiktok_publisher, a later commit).
    Isolated so it can be contract-tested with httpx.MockTransport like
    every other adapter in this project. The client secret lives only in
    the request body sent to TikTok's own token endpoint; it never appears
    in an exception message, a log line, or a return value.

    A refresh call MAY return a *different* refresh_token than the one
    that was sent -- the caller (providers.registry, a later commit) is
    responsible for always replacing the stored refresh_token with
    whatever the response carries, never assuming it's unchanged the way
    YouTube's refresh (which never rotates the refresh_token) allows."""

    def __init__(
        self, client_key: str, client_secret: str, token_url: str, transport: Any = None,
        connect_timeout: float = 10.0, read_timeout: float = 30.0,
    ) -> None:
        import httpx

        self._client_key = client_key
        self._client_secret = client_secret
        self._token_url = token_url
        self._httpx = httpx
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=30.0, pool=30.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> TokenResponse:
        return self._post_token({
            "client_key": self._client_key, "client_secret": self._client_secret,
            "code": code, "code_verifier": code_verifier, "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })

    def refresh(self, refresh_token: str) -> TokenResponse:
        return self._post_token({
            "client_key": self._client_key, "client_secret": self._client_secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        })

    def _post_token(self, data: dict) -> TokenResponse:
        try:
            response = self._client.post(
                self._token_url, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
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
            # TikTok's own error envelope can arrive with an HTTP 200 status
            # (an `error.code` field != "ok") -- never treated as success.
            error = payload.get("error")
            if isinstance(error, dict) and error.get("code") not in (None, "ok"):
                raise ProviderAuthError(f"oauth token request rejected ({error.get('code')})")
            return TokenResponse(
                access_token=payload["access_token"], refresh_token=payload.get("refresh_token"),
                expires_in=int(payload.get("expires_in", 86400)),
                refresh_expires_in=(
                    int(payload["refresh_expires_in"]) if payload.get("refresh_expires_in") is not None else None
                ),
                scope=payload.get("scope", ""), open_id=payload.get("open_id", ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TransientProviderError("oauth token response missing required fields") from exc
