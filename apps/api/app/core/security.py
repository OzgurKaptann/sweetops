"""
Security primitives for staff authentication.

Responsibilities:
  - Argon2id password hashing / verification (via argon2-cffi).
  - Opaque session + CSRF token generation (secrets.token_urlsafe).
  - Deterministic SHA-256 hashing of tokens for storage/lookup.
  - Constant-time comparison for CSRF double-submit validation.

Invariants:
  - Raw session/CSRF tokens are NEVER persisted or logged — only SHA-256 hashes.
  - Unknown-username logins still run a dummy Argon2 verify to equalise timing.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

from app.core.config import settings

# Argon2id with library-recommended defaults. type=ID is the default for
# argon2-cffi's PasswordHasher.
_ph = PasswordHasher()

# A precomputed hash used to burn CPU on unknown usernames so an attacker cannot
# distinguish "no such user" from "wrong password" by timing. The plaintext is
# irrelevant and never used to authenticate anything.
_DUMMY_HASH = _ph.hash("sweetops-dummy-password-for-timing-equalisation")

# Raw token entropy (bytes) → token_urlsafe(32) ≈ 43 url-safe chars.
_TOKEN_BYTES = 32


# ── Password policy ──────────────────────────────────────────────────────────

def validate_password(password: str) -> None:
    """
    Enforce the minimum password policy. Raises ValueError with an English
    (operator-facing, CLI-only) message on violation.

    Long passphrases are allowed; only an absurd upper bound is enforced.
    """
    if password is None or password.strip() == "":
        raise ValueError("Password must not be empty or whitespace-only.")
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"Password must be at least {settings.PASSWORD_MIN_LENGTH} characters."
        )
    if len(password) > settings.PASSWORD_MAX_LENGTH:
        raise ValueError(
            f"Password must be at most {settings.PASSWORD_MAX_LENGTH} characters."
        )


# ── Password hashing ─────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Return an Argon2id hash string for the given plaintext password."""
    return _ph.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """
    Verify a plaintext password against a stored Argon2id hash.

    When password_hash is None/empty (e.g. an unknown username or a user with no
    credential), a dummy verify is still performed to equalise timing, and the
    function returns False.
    """
    if not password_hash:
        # Equalise timing for unknown users / credential-less accounts.
        try:
            _ph.verify(_DUMMY_HASH, password)
        except Exception:
            pass
        return False

    try:
        _ph.verify(password_hash, password)
        return True
    except (VerifyMismatchError, InvalidHash, Exception):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash uses outdated Argon2 parameters."""
    try:
        return _ph.check_needs_rehash(password_hash)
    except Exception:
        return False


# ── Token generation & hashing ───────────────────────────────────────────────

def generate_token() -> str:
    """Cryptographically strong, URL-safe opaque token."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(raw_token: str) -> str:
    """Deterministic SHA-256 hex digest used to store/look up a token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    """Constant-time string comparison (guards CSRF double-submit checks)."""
    return hmac.compare_digest(a, b)
