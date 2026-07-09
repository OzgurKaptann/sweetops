"""
Cookie helpers for the staff session + CSRF double-submit tokens.

Kept in one place so security attributes (HttpOnly, Secure, SameSite, Path,
Domain, Max-Age) are applied identically everywhere and can never drift.
"""
from __future__ import annotations

from fastapi import Response

from app.core.config import settings


def _max_age_seconds() -> int:
    return settings.SESSION_ABSOLUTE_LIFETIME_HOURS * 3600


def set_auth_cookies(response: Response, raw_session_token: str, raw_csrf_token: str) -> None:
    """
    Set the HttpOnly session cookie and the (JS-readable) CSRF cookie.

    The session cookie is HttpOnly so JavaScript can never read the bearer
    token. The CSRF cookie is deliberately NOT HttpOnly: the SPA reads it and
    echoes it back in the X-CSRF-Token header (double-submit).
    """
    common = dict(
        max_age=_max_age_seconds(),
        path=settings.SESSION_COOKIE_PATH,
        domain=settings.cookie_domain,
        secure=settings.cookie_secure,
        samesite=settings.SESSION_COOKIE_SAMESITE,
    )

    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=raw_session_token,
        httponly=True,
        **common,
    )
    response.set_cookie(
        key=settings.CSRF_COOKIE_NAME,
        value=raw_csrf_token,
        httponly=False,
        **common,
    )


def clear_auth_cookies(response: Response) -> None:
    """Expire both cookies. Attributes must match those used when setting."""
    for name in (settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME):
        response.delete_cookie(
            key=name,
            path=settings.SESSION_COOKIE_PATH,
            domain=settings.cookie_domain,
            secure=settings.cookie_secure,
            samesite=settings.SESSION_COOKIE_SAMESITE,
        )
