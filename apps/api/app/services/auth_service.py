"""
Staff authentication service — opaque server-side sessions.

Covers:
  - Credential verification with account-level lockout and timing equalisation.
  - Session creation (returns raw token + raw CSRF exactly once).
  - Session resolution with full liveness checks derived from CURRENT DB state
    (active flag, role, store) — never trusting values copied into the cookie.
  - Session revocation (single / all) with forensic revoked_reason.

Security logs use safe identifiers only. Passwords, raw tokens, token hashes,
and CSRF material are never logged.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import is_operational_role, permissions_for_role
from app.core.security import (
    generate_token,
    hash_password,
    hash_token,
    needs_rehash,
    verify_password,
)
from app.models.auth_session import AuthSession
from app.models.role import Role
from app.models.user import User

logger = logging.getLogger(__name__)


# ── Errors ───────────────────────────────────────────────────────────────────

class LoginError(Exception):
    """Base login failure. `message` is a Turkish, user-safe string."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidCredentials(LoginError):
    """Unknown user, wrong password, or disabled account — indistinguishable."""


class AccountLocked(LoginError):
    """Account temporarily locked due to failed attempts."""


# ── Resolved staff context ───────────────────────────────────────────────────

@dataclass(frozen=True)
class CurrentStaff:
    user_id: int
    username: str
    role: str
    store_id: int | None
    permissions: tuple[str, ...]
    session_id: int
    csrf_token_hash: str

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _get_user_by_username(db: Session, username: str) -> User | None:
    """Case-insensitive username lookup."""
    return (
        db.query(User)
        .filter(func.lower(User.username) == username.strip().lower())
        .first()
    )


def _is_locked(user: User, now: datetime) -> bool:
    locked_until = _aware(user.locked_until)
    return locked_until is not None and locked_until > now


def _register_failed_attempt(db: Session, user: User, now: datetime) -> None:
    """
    Atomically increment the failed-login counter and lock the account once the
    configured threshold is reached.
    """
    db.query(User).filter(User.id == user.id).update(
        {User.failed_login_count: User.failed_login_count + 1},
        synchronize_session=False,
    )
    db.flush()
    db.refresh(user)

    if user.failed_login_count >= settings.LOGIN_MAX_FAILED_ATTEMPTS:
        user.locked_until = now + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
        logger.warning(
            "auth_account_locked user_id=%s attempts=%s",
            user.id,
            user.failed_login_count,
        )
    db.commit()


def _clear_lock_and_mark_login(db: Session, user: User, now: datetime) -> None:
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    db.commit()


# ── Public: authentication ───────────────────────────────────────────────────

def authenticate(db: Session, username: str, password: str) -> User:
    """
    Verify credentials and return the authenticated User.

    Raises AccountLocked when the account is currently locked, or
    InvalidCredentials for every other failure (unknown user, wrong password,
    disabled account, operational role without a store) so callers cannot
    distinguish the underlying condition.
    """
    now = _now()
    user = _get_user_by_username(db, username)

    if user is None:
        # Equalise timing against a real verify for unknown usernames.
        verify_password(None, password)
        logger.info("auth_login_failed reason=unknown_user")
        raise InvalidCredentials("Kullanıcı adı veya şifre hatalı.")

    if _is_locked(user, now):
        logger.info("auth_login_blocked reason=locked user_id=%s", user.id)
        raise AccountLocked(
            "Hesabın geçici olarak kilitlendi. Lütfen daha sonra tekrar dene."
        )

    if not verify_password(user.password_hash, password):
        _register_failed_attempt(db, user, now)
        logger.info("auth_login_failed reason=bad_password user_id=%s", user.id)
        raise InvalidCredentials("Kullanıcı adı veya şifre hatalı.")

    # Correct password from here on — but the account must still be usable.
    if not user.is_active:
        logger.info("auth_login_failed reason=disabled user_id=%s", user.id)
        raise InvalidCredentials("Kullanıcı adı veya şifre hatalı.")

    role = db.get(Role, user.role_id) if user.role_id else None
    if role is None:
        logger.warning("auth_login_failed reason=no_role user_id=%s", user.id)
        raise InvalidCredentials("Kullanıcı adı veya şifre hatalı.")

    if is_operational_role(role.name) and user.store_id is None:
        logger.warning("auth_login_failed reason=no_store user_id=%s role=%s", user.id, role.name)
        raise InvalidCredentials("Kullanıcı adı veya şifre hatalı.")

    # Opportunistic rehash if Argon2 parameters are outdated.
    if user.password_hash and needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    _clear_lock_and_mark_login(db, user, now)
    logger.info("auth_login_success user_id=%s role=%s", user.id, role.name)
    return user


# ── Public: session lifecycle ────────────────────────────────────────────────

def create_session(
    db: Session,
    user: User,
    user_agent: str | None = None,
) -> tuple[AuthSession, str, str]:
    """
    Create a new opaque session for `user`.

    Returns (session, raw_session_token, raw_csrf_token). The raw tokens exist
    only here and in the response cookies — only their SHA-256 hashes are
    persisted.
    """
    now = _now()
    raw_token = generate_token()
    raw_csrf = generate_token()

    session = AuthSession(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        csrf_token_hash=hash_token(raw_csrf),
        expires_at=now + timedelta(hours=settings.SESSION_ABSOLUTE_LIFETIME_HOURS),
        last_seen_at=now,
        user_agent_hash=hash_token(user_agent) if user_agent else None,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    logger.info("auth_session_created session_id=%s user_id=%s", session.id, user.id)
    return session, raw_token, raw_csrf


def resolve_session(db: Session, raw_token: str) -> tuple[AuthSession, User, Role] | None:
    """
    Validate a raw session token against current DB state.

    Returns (session, user, role) when the session is fully valid, else None.
    All checks (active, role, store) read live rows — never cookie-copied state.
    Idle-timed-out sessions are revoked as a side effect.
    """
    if not raw_token:
        return None

    now = _now()
    token_hash = hash_token(raw_token)
    session = (
        db.query(AuthSession)
        .filter(AuthSession.token_hash == token_hash)
        .first()
    )
    if session is None:
        return None
    if session.revoked_at is not None:
        return None
    if _aware(session.expires_at) <= now:
        return None

    # Idle timeout.
    idle_deadline = _aware(session.last_seen_at) + timedelta(
        minutes=settings.SESSION_IDLE_TIMEOUT_MINUTES
    )
    if idle_deadline <= now:
        _revoke(db, session, "idle_timeout", commit=True)
        return None

    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        return None

    role = db.get(Role, user.role_id) if user.role_id else None
    if role is None:
        return None

    if is_operational_role(role.name) and user.store_id is None:
        return None

    # Throttled last_seen update — avoids a write on every request.
    last_seen = _aware(session.last_seen_at)
    if (now - last_seen).total_seconds() >= settings.SESSION_LAST_SEEN_THROTTLE_SECONDS:
        session.last_seen_at = now
        db.commit()

    return session, user, role


def build_context(session: AuthSession, user: User, role: Role) -> CurrentStaff:
    return CurrentStaff(
        user_id=user.id,
        username=user.username,
        role=role.name,
        store_id=user.store_id,
        permissions=tuple(permissions_for_role(role.name)),
        session_id=session.id,
        csrf_token_hash=session.csrf_token_hash,
    )


# ── Public: revocation ───────────────────────────────────────────────────────

def _revoke(db: Session, session: AuthSession, reason: str, *, commit: bool) -> None:
    if session.revoked_at is None:
        session.revoked_at = _now()
        session.revoked_reason = reason
    if commit:
        db.commit()


def revoke_session(db: Session, session: AuthSession, reason: str = "logout") -> None:
    _revoke(db, session, reason, commit=True)
    logger.info("auth_session_revoked session_id=%s reason=%s", session.id, reason)


def revoke_all_sessions(db: Session, user_id: int, reason: str = "logout_all") -> int:
    """Revoke every currently-active session for a user. Returns count revoked."""
    now = _now()
    count = (
        db.query(AuthSession)
        .filter(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
        )
        .update(
            {AuthSession.revoked_at: now, AuthSession.revoked_reason: reason},
            synchronize_session=False,
        )
    )
    db.commit()
    logger.info("auth_sessions_revoked_all user_id=%s count=%s reason=%s", user_id, count, reason)
    return count
