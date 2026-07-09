"""
Central authorization dependencies.

Authentication and permission checks live here — routers never re-implement
them. All failures return consistent, structured Turkish errors:
  - 401 (unauthorized)  → not authenticated / expired / revoked session.
  - 403 (forbidden)     → authenticated but insufficient permission, or a failed
                          CSRF / origin check on a state-changing request.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.exceptions import HTTPException
from sqlalchemy.orm import Session

from app.core import messages
from app.core.config import settings
from app.core.db import get_db
from app.core.security import constant_time_equals, hash_token
from app.services.auth_service import (
    CurrentStaff,
    build_context,
    resolve_session,
)

# Methods that mutate state and therefore require CSRF + origin validation.
_STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"error": "unauthorized", "message": messages.AUTH_SESSION_EXPIRED},
    )


def _forbidden(message: str = messages.AUTH_FORBIDDEN, error: str = "forbidden") -> HTTPException:
    return HTTPException(status_code=403, detail={"error": error, "message": message})


def get_current_staff(
    request: Request,
    db: Session = Depends(get_db),
) -> CurrentStaff:
    """Resolve the authenticated staff member from the session cookie (or 401)."""
    raw_token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    resolved = resolve_session(db, raw_token)
    if resolved is None:
        raise _unauthorized()
    session, user, role = resolved
    return build_context(session, user, role)


def enforce_origin(request: Request) -> None:
    """
    Reject a request whose browser-supplied Origin/Referer is present but not
    among the trusted staff origins. Absent Origin (non-browser clients) is
    allowed — CSRF token validation is the second, independent line of defence.
    """
    origin = request.headers.get("origin")
    trusted = settings.staff_origins
    if origin is not None:
        if origin not in trusted:
            raise _forbidden(messages.AUTH_ORIGIN_REJECTED, error="origin_rejected")
        return

    referer = request.headers.get("referer")
    if referer is not None:
        if not any(referer.startswith(o) for o in trusted):
            raise _forbidden(messages.AUTH_ORIGIN_REJECTED, error="origin_rejected")


# ── Origin canonicalization (shared by HTTP and WebSocket checks) ─────────────
# A browser Origin is exactly ``scheme://host[:port]`` — no path, query, fragment
# or userinfo. Only http/https are meaningful for a staff web origin.
_ORIGIN_SCHEMES = {"http", "https"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonical_origin(value: str | None) -> tuple[str, str, int] | None:
    """
    Normalize an origin to ``(scheme, host, port)`` for exact comparison, or
    return ``None`` if it is missing, malformed, opaque (``null``), a wildcard,
    carries embedded credentials, uses an unexpected scheme, or contains any
    path/query/fragment. Comparison is therefore structural, never substring —
    ``https://kitchen.example.com`` never matches
    ``https://kitchen.example.com.attacker.test``.
    """
    if not value:
        return None
    raw = value.strip()
    if raw in ("", "null", "*"):
        return None

    try:
        parts = urlsplit(raw)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in _ORIGIN_SCHEMES:
        return None
    # An origin has no path/query/fragment and no userinfo (embedded creds).
    if parts.path or parts.query or parts.fragment:
        return None
    if parts.username is not None or parts.password is not None:
        return None

    host = parts.hostname
    if not host:
        return None

    try:
        port = parts.port
    except ValueError:
        return None  # malformed / out-of-range port
    if port is None:
        port = _DEFAULT_PORTS[scheme]

    return (scheme, host.lower(), port)


def is_trusted_origin(origin: str | None, trusted: list[str]) -> bool:
    """True iff ``origin`` canonically equals one of the configured origins."""
    canon = canonical_origin(origin)
    if canon is None:
        return False
    return any(canonical_origin(t) == canon for t in trusted)


def websocket_origin_allowed(origin: str | None) -> bool:
    """
    Decide whether a WebSocket handshake Origin may proceed.

    - A present Origin must canonically match a trusted staff origin.
    - A missing Origin (non-browser client) is allowed ONLY when
      ``ALLOW_MISSING_WEBSOCKET_ORIGIN`` is explicitly enabled (default False),
      so production rejects it. Never inferred from the hostname.
    """
    if origin is None:
        return settings.ALLOW_MISSING_WEBSOCKET_ORIGIN
    return is_trusted_origin(origin, settings.staff_origins)


def safe_origin_label(origin: str | None) -> str:
    """
    A log-safe representation of a handshake origin. Returns the normalized
    ``scheme://host:port`` for a parseable value, or a fixed placeholder — never
    an attacker-controlled raw string (avoids log injection), and never any
    cookie/token/URL material.
    """
    if origin is None:
        return "<missing>"
    canon = canonical_origin(origin)
    if canon is None:
        return "<invalid>"
    scheme, host, port = canon
    return f"{scheme}://{host}:{port}"


def enforce_csrf(request: Request, staff: CurrentStaff) -> None:
    """Constant-time double-submit CSRF check for state-changing requests."""
    header = request.headers.get("X-CSRF-Token")
    if not header:
        raise _forbidden(messages.AUTH_CSRF_INVALID, error="csrf_invalid")
    if not constant_time_equals(hash_token(header), staff.csrf_token_hash):
        raise _forbidden(messages.AUTH_CSRF_INVALID, error="csrf_invalid")


def require_permission(permission: str):
    """
    Build a dependency enforcing authentication + a named permission.

    For state-changing methods it also enforces trusted-origin and CSRF checks,
    so a single dependency fully protects a mutating endpoint.
    """

    def _dependency(
        request: Request,
        db: Session = Depends(get_db),
    ) -> CurrentStaff:
        staff = get_current_staff(request, db)

        if request.method in _STATE_CHANGING:
            enforce_origin(request)
            enforce_csrf(request, staff)

        if not staff.has_permission(permission):
            raise _forbidden()

        return staff

    return _dependency
