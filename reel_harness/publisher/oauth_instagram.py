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
    "ShortLivedTokenResponse",
    "LongLivedTokenResponse",
    "AccountIdentity",
    "InstagramOAuthClient",
    "OAuthCallbackError",
    "LoopbackCallbackServer",
]

# Per Meta's Instagram Login for Business (Business Login for Instagram)
# guide (checked 2026-07-29 -- see docs/PUBLISHING.md). Endpoints are read
# from Settings by every call site below -- there is no hardcoded
# module-level endpoint the way oauth_youtube.py has one, mirroring
# oauth_tiktok.py's pattern, since a contract-test fake server needs to
# substitute its own.

# This adapter only ever requests the minimum pair for its own operations
# (publish + basic account read for eligibility/account-info checks) --
# never comment/message-management scopes, since this adapter doesn't use them.
SCOPES = ("instagram_business_basic", "instagram_business_content_publish")


def build_authorization_url(
    app_id: str, redirect_uri: str, state: str, pkce: PKCEChallenge, auth_url: str,
) -> str:
    """PKCE is not documented as supported or required by Meta's guide
    (unlike YouTube's/TikTok's installed-app flows) -- sent anyway since
    it's cheap and harmless if the authorization server ignores it,
    consistent with this project's other two OAuth flows."""
    params = {
        "client_id": app_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": ",".join(SCOPES), "state": state,
        "code_challenge": pkce.challenge, "code_challenge_method": pkce.method,
    }
    return f"{auth_url}?{urllib.parse.urlencode(params)}"


@dataclass
class ShortLivedTokenResponse:
    access_token: str
    user_id: str | None = None


@dataclass
class LongLivedTokenResponse:
    access_token: str
    expires_in: int  # seconds -- ~60 days per docs


@dataclass
class AccountIdentity:
    account_id: str
    username: str | None


class InstagramOAuthClient:
    """Talks only to Meta's OAuth/token endpoints -- never the Content
    Publishing API itself (that's providers.instagram_publisher, a later
    commit). Isolated so it can be contract-tested with
    httpx.MockTransport like every other adapter in this project. The app
    secret lives only in the request sent to Meta's own token endpoints;
    it never appears in an exception message, a log line, or a return
    value.

    Instagram's token model differs from YouTube's/TikTok's in a real
    way: there is no separate refresh_token grant. A long-lived access
    token is refreshed by presenting *itself* to the refresh endpoint
    (`grant_type=ig_refresh_token`), which returns a new long-lived token
    with a renewed ~60-day expiry -- see refresh_long_lived_token.
    publisher.credentials.OAuthCredential.refresh_token stays None for
    every Instagram credential; the access_token field IS the
    refreshable artifact."""

    def __init__(
        self, app_id: str, app_secret: str, token_url: str, graph_url: str, transport: Any = None,
        connect_timeout: float = 10.0, read_timeout: float = 30.0,
    ) -> None:
        import httpx

        self._app_id = app_id
        self._app_secret = app_secret
        self._token_url = token_url
        self._graph_url = graph_url.rstrip("/")
        self._httpx = httpx
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=30.0, pool=30.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> ShortLivedTokenResponse:
        """Step 1: authorization code -> short-lived access token."""
        try:
            response = self._client.post(self._token_url, data={
                "client_id": self._app_id, "client_secret": self._app_secret,
                "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                "code": code, "code_verifier": code_verifier,
            })
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
            return ShortLivedTokenResponse(
                access_token=payload["access_token"],
                user_id=str(payload["user_id"]) if payload.get("user_id") is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TransientProviderError("oauth token response missing required fields") from exc

    def exchange_long_lived_token(self, short_lived_access_token: str) -> LongLivedTokenResponse:
        """Step 2: short-lived token -> long-lived (~60 day) token."""
        return self._get_token(
            f"{self._graph_url}/access_token",
            {
                "grant_type": "ig_exchange_token", "client_secret": self._app_secret,
                "access_token": short_lived_access_token,
            },
        )

    def refresh_long_lived_token(self, access_token: str) -> LongLivedTokenResponse:
        """Renews an existing long-lived token for another ~60 days.
        Presents the token itself, not a separate refresh_token -- see
        this class's docstring. Only valid once the token is at least 24
        hours old and not yet expired, per Meta's docs; a token outside
        that window must be re-authorized via publisher-auth instead."""
        return self._get_token(
            f"{self._graph_url}/refresh_access_token",
            {"grant_type": "ig_refresh_token", "access_token": access_token},
        )

    def _get_token(self, url: str, params: dict) -> LongLivedTokenResponse:
        try:
            response = self._client.get(url, params=params)
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
            return LongLivedTokenResponse(
                access_token=payload["access_token"], expires_in=int(payload.get("expires_in", 5184000)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TransientProviderError("oauth token response missing required fields") from exc

    def fetch_account_identity(self, access_token: str) -> AccountIdentity:
        try:
            response = self._client.get(
                f"{self._graph_url}/me", params={"fields": "user_id,username", "access_token": access_token},
            )
        except self._httpx.TimeoutException as exc:
            raise TransientProviderError(f"account identity request timed out ({type(exc).__name__})") from exc
        except self._httpx.HTTPError as exc:
            raise TransientProviderError(f"account identity transport error ({type(exc).__name__})") from exc

        if response.status_code in (401, 403):
            raise ProviderAuthError(f"account identity request rejected (HTTP {response.status_code})")
        if response.status_code != 200:
            raise TransientProviderError(f"account identity endpoint returned HTTP {response.status_code}")
        try:
            payload = response.json()
            account_id = payload.get("user_id") or payload.get("id")
            if not account_id:
                raise TransientProviderError("account identity response had no user id")
            return AccountIdentity(account_id=str(account_id), username=payload.get("username"))
        except (KeyError, TypeError, ValueError) as exc:
            raise TransientProviderError("account identity response missing required fields") from exc
