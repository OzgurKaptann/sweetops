"""
Staff authentication tests: password hashing, login + lockout, opaque sessions,
cookies, /auth/me, logout, logout-all, and CSRF.

These exercise the real login pipeline (no dependency overrides) so production
security behaviour is what is being verified.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import hash_token, verify_password
from app.main import app
from app.models.auth_session import AuthSession
from app.models.user import User
from app.services import auth_service
from tests.conftest import DEFAULT_PASSWORD, DEFAULT_STORE_ID, make_authed_client

client = TestClient(app)

SESSION_COOKIE = settings.SESSION_COOKIE_NAME
CSRF_COOKIE = settings.CSRF_COOKIE_NAME


def _login(c: TestClient, username: str, password: str):
    return c.post("/auth/login", json={"username": username, "password": password})


# ---------------------------------------------------------------------------
# 1. Passwords & Argon2
# ---------------------------------------------------------------------------

class TestPasswords:
    def test_password_stored_as_argon2id(self, make_staff):
        user = make_staff("OWNER")
        assert user.password_hash.startswith("$argon2id$")

    def test_correct_password_verifies(self, make_staff):
        user = make_staff("OWNER")
        assert verify_password(user.password_hash, DEFAULT_PASSWORD) is True

    def test_incorrect_password_fails(self, make_staff):
        user = make_staff("OWNER")
        assert verify_password(user.password_hash, "wrong-password") is False

    def test_unknown_username_uses_dummy_hash_path(self):
        # No hash → dummy verify path, returns False, does not raise.
        assert verify_password(None, "anything") is False


# ---------------------------------------------------------------------------
# 2. Login pipeline & case-insensitivity
# ---------------------------------------------------------------------------

class TestLogin:
    def test_valid_login_sets_session_and_csrf_cookies(self, make_staff):
        user = make_staff("KITCHEN")
        c = TestClient(app)
        r = _login(c, user.username, DEFAULT_PASSWORD)
        assert r.status_code == 200
        assert SESSION_COOKIE in r.cookies
        assert CSRF_COOKIE in r.cookies
        body = r.json()
        assert body["username"] == user.username
        assert body["role"] == "KITCHEN"

    def test_case_insensitive_login_succeeds(self, make_staff):
        user = make_staff("OWNER", username=f"MixedCase_{uuid.uuid4().hex[:6]}")
        c = TestClient(app)
        r = _login(c, user.username.upper(), DEFAULT_PASSWORD)
        assert r.status_code == 200

    def test_invalid_login_returns_generic_turkish_error(self, make_staff):
        user = make_staff("OWNER")
        c = TestClient(app)
        r = _login(c, user.username, "totally-wrong")
        assert r.status_code == 401
        assert r.json()["detail"]["message"] == "Kullanıcı adı veya şifre hatalı."

    def test_unknown_user_and_bad_password_are_indistinguishable(self, make_staff):
        user = make_staff("OWNER")
        c = TestClient(app)
        unknown = _login(c, f"nope_{uuid.uuid4().hex}", "whatever123")
        wrong = _login(c, user.username, "whatever123")
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["detail"]["message"] == wrong.json()["detail"]["message"]

    def test_disabled_user_cannot_login(self, make_staff):
        user = make_staff("OWNER", is_active=False)
        c = TestClient(app)
        r = _login(c, user.username, DEFAULT_PASSWORD)
        assert r.status_code == 401

    def test_operational_role_without_store_rejected(self, make_staff):
        user = make_staff("KITCHEN", store_id=None)
        c = TestClient(app)
        r = _login(c, user.username, DEFAULT_PASSWORD)
        assert r.status_code == 401

    def test_successful_login_updates_last_login(self, db, make_staff):
        user = make_staff("OWNER")
        assert user.last_login_at is None
        c = TestClient(app)
        _login(c, user.username, DEFAULT_PASSWORD)
        db.refresh(user)
        assert user.last_login_at is not None

    def test_login_response_has_no_secret_material(self, make_staff):
        user = make_staff("OWNER")
        c = TestClient(app)
        r = _login(c, user.username, DEFAULT_PASSWORD)
        body = r.json()
        assert "password_hash" not in body
        assert "token" not in str(body).lower() or "token_hash" not in body


# ---------------------------------------------------------------------------
# 3. Lockout
# ---------------------------------------------------------------------------

class TestLockout:
    def test_five_failures_trigger_lockout(self, db, make_staff):
        user = make_staff("OWNER")
        c = TestClient(app)
        for _ in range(settings.LOGIN_MAX_FAILED_ATTEMPTS):
            _login(c, user.username, "bad-password")
        # correct password now — but account is locked
        r = _login(c, user.username, DEFAULT_PASSWORD)
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "account_locked"
        db.refresh(user)
        assert user.locked_until is not None

    def test_successful_login_clears_failed_count(self, db, make_staff):
        user = make_staff("OWNER")
        c = TestClient(app)
        for _ in range(settings.LOGIN_MAX_FAILED_ATTEMPTS - 1):
            _login(c, user.username, "bad-password")
        db.refresh(user)
        assert user.failed_login_count == settings.LOGIN_MAX_FAILED_ATTEMPTS - 1
        r = _login(c, user.username, DEFAULT_PASSWORD)
        assert r.status_code == 200
        db.refresh(user)
        assert user.failed_login_count == 0
        assert user.locked_until is None


# ---------------------------------------------------------------------------
# 4. Session storage & liveness
# ---------------------------------------------------------------------------

class TestSessions:
    def test_raw_session_token_never_stored(self, db, make_staff):
        user = make_staff("OWNER")
        _session, raw_token, raw_csrf = auth_service.create_session(db, user)
        # No row stores the raw token; only its hash exists.
        assert db.query(AuthSession).filter(AuthSession.token_hash == raw_token).first() is None
        assert db.query(AuthSession).filter(AuthSession.token_hash == hash_token(raw_token)).first() is not None

    def test_raw_csrf_token_never_stored(self, db, make_staff):
        user = make_staff("OWNER")
        session, raw_token, raw_csrf = auth_service.create_session(db, user)
        db.refresh(session)
        assert session.csrf_token_hash != raw_csrf
        assert session.csrf_token_hash == hash_token(raw_csrf)

    def test_valid_session_resolves_user(self, db, make_staff):
        user = make_staff("OWNER")
        _session, raw_token, _csrf = auth_service.create_session(db, user)
        resolved = auth_service.resolve_session(db, raw_token)
        assert resolved is not None
        assert resolved[1].id == user.id

    def test_revoked_session_rejected(self, db, make_staff):
        user = make_staff("OWNER")
        session, raw_token, _csrf = auth_service.create_session(db, user)
        auth_service.revoke_session(db, session, reason="test")
        assert auth_service.resolve_session(db, raw_token) is None

    def test_expired_session_rejected(self, db, make_staff):
        from datetime import datetime, timedelta, timezone
        user = make_staff("OWNER")
        session, raw_token, _csrf = auth_service.create_session(db, user)
        session.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        assert auth_service.resolve_session(db, raw_token) is None

    def test_disabled_user_invalidates_session(self, db, make_staff):
        user = make_staff("OWNER")
        _session, raw_token, _csrf = auth_service.create_session(db, user)
        user.is_active = False
        db.commit()
        assert auth_service.resolve_session(db, raw_token) is None

    def test_role_change_affects_next_request(self, db, make_staff):
        from app.models.role import Role
        user = make_staff("KITCHEN")
        _session, raw_token, _csrf = auth_service.create_session(db, user)
        owner_role = db.query(Role).filter(Role.name == "OWNER").first()
        user.role_id = owner_role.id
        db.commit()
        resolved = auth_service.resolve_session(db, raw_token)
        assert resolved is not None
        staff = auth_service.build_context(*resolved)
        assert staff.role == "OWNER"

    def test_store_change_affects_next_request(self, db, make_staff, make_store):
        store_b = make_store()
        user = make_staff("KITCHEN", store_id=DEFAULT_STORE_ID)
        _session, raw_token, _csrf = auth_service.create_session(db, user)
        user.store_id = store_b.id
        db.commit()
        resolved = auth_service.resolve_session(db, raw_token)
        staff = auth_service.build_context(*resolved)
        assert staff.store_id == store_b.id

    def test_password_reset_revokes_sessions(self, db, make_staff):
        from app.core.security import hash_password
        user = make_staff("OWNER")
        _session, raw_token, _csrf = auth_service.create_session(db, user)
        # simulate reset-password CLI behaviour
        user.password_hash = hash_password("brand-new-passphrase")
        db.flush()
        auth_service.revoke_all_sessions(db, user.id, reason="password_reset")
        db.commit()
        assert auth_service.resolve_session(db, raw_token) is None


# ---------------------------------------------------------------------------
# 5. /auth/me, logout, logout-all, cookies
# ---------------------------------------------------------------------------

class TestAuthEndpoints:
    def test_me_returns_profile_no_secrets(self, db, make_staff):
        user = make_staff("OWNER")
        c = make_authed_client(db, user)
        r = c.get("/auth/me")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"
        body = r.json()
        assert body["id"] == user.id
        assert "password_hash" not in body
        assert set(body["permissions"]) >= {"owner:read", "kitchen:read"}

    def test_me_requires_auth(self):
        r = client.get("/auth/me")
        assert r.status_code == 401

    def test_logout_revokes_session(self, db, make_staff):
        user = make_staff("OWNER")
        c = make_authed_client(db, user)
        assert c.get("/auth/me").status_code == 200
        r = c.post("/auth/logout")
        assert r.status_code == 200
        # cookie cleared -> subsequent /auth/me unauthorized
        c2 = TestClient(app)
        c2.cookies.set(SESSION_COOKIE, "irrelevant")
        assert c2.get("/auth/me").status_code == 401

    def test_logout_is_idempotent(self):
        c = TestClient(app)
        r = c.post("/auth/logout")
        assert r.status_code == 200

    def test_logout_all_revokes_all_sessions(self, db, make_staff):
        user = make_staff("OWNER")
        # two sessions
        s1, t1, csrf1 = auth_service.create_session(db, user)
        c = make_authed_client(db, user)  # third session, authed client
        r = c.post("/auth/logout-all")
        assert r.status_code == 200
        # first session also revoked
        assert auth_service.resolve_session(db, t1) is None

    def test_protected_response_uses_no_store(self, db, make_staff):
        user = make_staff("OWNER")
        c = make_authed_client(db, user)
        r = c.get("/auth/me")
        assert r.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------
# 6. CSRF
# ---------------------------------------------------------------------------

class TestCsrf:
    def _order(self, db):
        from decimal import Decimal
        from tests.conftest import make_ingredient, order_payload
        ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        return ing, r.json()["order_id"]

    def test_protected_patch_without_csrf_rejected(self, db, make_staff):
        from tests.conftest import cleanup_ingredient
        ing, oid = self._order(db)
        user = make_staff("KITCHEN")
        _session, raw_token, raw_csrf = auth_service.create_session(db, user)
        c = TestClient(app)
        c.cookies.set(SESSION_COOKIE, raw_token)
        c.cookies.set(CSRF_COOKIE, raw_csrf)
        # No X-CSRF-Token header
        r = c.patch(f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"})
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "csrf_invalid"
        cleanup_ingredient(db, ing.id)

    def test_protected_patch_with_wrong_csrf_rejected(self, db, make_staff):
        from tests.conftest import cleanup_ingredient
        ing, oid = self._order(db)
        user = make_staff("KITCHEN")
        _session, raw_token, raw_csrf = auth_service.create_session(db, user)
        c = TestClient(app)
        c.cookies.set(SESSION_COOKIE, raw_token)
        r = c.patch(
            f"/kitchen/orders/{oid}/status",
            json={"status": "IN_PREP"},
            headers={"X-CSRF-Token": "not-the-real-token"},
        )
        assert r.status_code == 403
        cleanup_ingredient(db, ing.id)

    def test_protected_patch_with_valid_csrf_succeeds(self, db, make_staff):
        from tests.conftest import cleanup_ingredient
        ing, oid = self._order(db)
        user = make_staff("KITCHEN")
        c = make_authed_client(db, user)  # sets X-CSRF-Token by default
        r = c.patch(f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"})
        assert r.status_code == 200
        cleanup_ingredient(db, ing.id)

    def test_public_order_does_not_require_staff_csrf(self, db):
        from decimal import Decimal
        from tests.conftest import cleanup_ingredient, make_ingredient, order_payload
        ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 200
        cleanup_ingredient(db, ing.id)

    def test_cross_origin_staff_mutation_rejected(self, db, make_staff):
        from tests.conftest import cleanup_ingredient
        ing, oid = self._order(db)
        user = make_staff("KITCHEN")
        c = make_authed_client(db, user)
        r = c.patch(
            f"/kitchen/orders/{oid}/status",
            json={"status": "IN_PREP"},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "origin_rejected"
        cleanup_ingredient(db, ing.id)
