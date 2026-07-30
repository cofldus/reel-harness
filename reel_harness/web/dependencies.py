from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from fastapi import Cookie, Header, HTTPException, Request

CSRF_COOKIE_NAME = "rh_csrf"
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_FORM_FIELD = "csrf_token"

_templates = None


def get_templates():
    """Memoized Jinja2Templates, same lazy-singleton pattern api.app.get_context
    already uses. Resolved via Path(__file__).parent so this works identically
    from a source checkout and from an installed wheel, regardless of the
    process's current working directory."""
    global _templates
    if _templates is None:
        from fastapi.templating import Jinja2Templates

        _templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    return _templates


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def ensure_csrf_cookie(request: Request) -> str:
    """Returns the current request's CSRF token, generating one if the
    cookie is absent. The caller (a page route) is responsible for setting
    the cookie on the response when this returns a freshly-generated value
    (see web.router's set_csrf_cookie_if_needed helper)."""
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    return existing if existing else new_csrf_token()


async def require_csrf(
    request: Request,
    rh_csrf: str | None = Cookie(default=None),
    x_csrf_token: str | None = Header(default=None, alias=CSRF_HEADER_NAME),
) -> None:
    """Double-submit CSRF check for every mutating /jobs/... route. The
    cookie proves "this browser visited our page first"; the header/form
    field proves "this specific request was issued by our own page's JS/
    markup, not a cross-site form post" (a cross-site page can make the
    browser send cookies automatically, but cannot read/echo our cookie's
    value into a custom header or hidden field). Never merged with the
    existing /v1/* require_api_key dependency -- see docs/OPERATIONS.md's
    web UI security section for why these are two separate trust
    boundaries."""
    submitted: str | None = x_csrf_token
    if submitted is None:
        form = await request.form()
        field = form.get(CSRF_FORM_FIELD)
        submitted = field if isinstance(field, str) else None
    if not rh_csrf or not submitted or not hmac.compare_digest(rh_csrf, submitted):
        raise HTTPException(status_code=403, detail="missing or invalid CSRF token")
