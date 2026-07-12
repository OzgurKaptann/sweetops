"""
Kitchen WebSocket authorization, Cross-Site WebSocket Hijacking (CSWSH) defence,
and store partitioning.

The handshake validates the browser Origin FIRST (an additional boundary, not a
replacement for auth), then authenticates from the session cookie. Untrusted /
missing / malformed origins are rejected with 4403; unauthenticated / wrong role
/ revoked sessions with 4401 / 4403. The store is always derived from the
session; initial state and broadcasts are limited to that store.

The Starlette test client does NOT send an Origin header by default, so every
connection that must succeed passes an explicit trusted Origin — production
policy (reject missing Origin) is never weakened to make the client work.
"""
import asyncio
import re
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.main import app
from app.services import auth_service
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_ingredient,
)

SESSION_COOKIE = settings.SESSION_COOKIE_NAME

# Two distinct trusted staff origins (owner-web + kitchen-web dev origins).
TRUSTED_OWNER_ORIGIN = settings.staff_origins[0]    # http://localhost:3001
TRUSTED_KITCHEN_ORIGIN = settings.staff_origins[-1]  # http://localhost:3002

# WS close codes (mirror app/routers/ws.py).
WS_UNAUTHENTICATED = 4401
WS_FORBIDDEN = 4403


def _connect(client: TestClient, *, origin: str | None = TRUSTED_KITCHEN_ORIGIN):
    """
    Open the kitchen WS. `origin=None` sends NO Origin header (non-browser
    client); any string is sent verbatim as the Origin header.
    """
    headers = {} if origin is None else {"Origin": origin}
    return client.websocket_connect("/ws/kitchen", headers=headers)


def _order_in_store(db, store_id: int):
    # Stock is store-scoped: the order's own store is the one that must hold it.
    ing, _ = make_ingredient(db, on_hand=Decimal("200.00"), store_id=store_id)
    payload = {
        "store_id": store_id,
        "items": [{"product_id": 1, "quantity": 1,
                   "ingredients": [{"ingredient_id": ing.id, "quantity": 1}]}],
    }
    plain = TestClient(app)
    r = plain.post("/public/orders/", json=payload, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 200
    return ing, r.json()["order_id"]


def _ws_client_for(db, user):
    _session, raw_token, _csrf = auth_service.create_session(db, user)
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, raw_token)
    return c, raw_token


# ── Origin: trusted origins succeed ───────────────────────────────────────────

def test_valid_session_trusted_kitchen_origin_succeeds(db, make_staff):
    """(1) Valid session + trusted kitchen origin succeeds."""
    user = make_staff("KITCHEN", store_id=DEFAULT_STORE_ID)
    c, _ = _ws_client_for(db, user)
    with _connect(c, origin=TRUSTED_KITCHEN_ORIGIN) as ws:
        msg = ws.receive_json()
    assert msg["event"] == "initial_state"


def test_valid_session_trusted_owner_origin_succeeds(db, make_staff):
    """(2) Valid session + trusted owner origin succeeds for a kitchen:read role."""
    user = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
    c, _ = _ws_client_for(db, user)
    with _connect(c, origin=TRUSTED_OWNER_ORIGIN) as ws:
        msg = ws.receive_json()
    assert msg["event"] == "initial_state"


# ── Origin: untrusted / malformed origins rejected (even with a valid session) ─

@pytest.mark.parametrize(
    "bad_origin",
    [
        "https://evil.example.com",                 # (3) clearly malicious
        "http://localhost.attacker.test:3001",      # (4) lookalike host suffix
        "null",                                     # (5) opaque origin
        "http://",                                  # (6) malformed (no host)
        "file:///etc/passwd",                       # unexpected scheme
        "http://user:pass@localhost:3001",          # embedded credentials
        "*",                                        # wildcard
    ],
)
def test_valid_session_untrusted_origin_rejected(db, make_staff, bad_origin):
    """(3)-(6) A valid session cannot rescue an untrusted / malformed origin."""
    user = make_staff("KITCHEN", store_id=DEFAULT_STORE_ID)
    c, _ = _ws_client_for(db, user)
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(c, origin=bad_origin):
            pass
    assert exc.value.code == WS_FORBIDDEN


def test_lookalike_suffix_not_trusted_by_substring(db, make_staff):
    """(4) Explicit: a trusted host is never trusted by substring/suffix match."""
    user = make_staff("KITCHEN", store_id=DEFAULT_STORE_ID)
    c, _ = _ws_client_for(db, user)
    # TRUSTED_KITCHEN_ORIGIN with an attacker-controlled suffix appended.
    lookalike = TRUSTED_KITCHEN_ORIGIN + ".attacker.test"
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(c, origin=lookalike):
            pass
    assert exc.value.code == WS_FORBIDDEN


# ── Origin: missing-origin production policy ───────────────────────────────────

def test_missing_origin_rejected_by_default(db, make_staff):
    """(7) Missing Origin is rejected under the default (production) policy."""
    assert settings.ALLOW_MISSING_WEBSOCKET_ORIGIN is False
    user = make_staff("KITCHEN", store_id=DEFAULT_STORE_ID)
    c, _ = _ws_client_for(db, user)
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(c, origin=None):
            pass
    assert exc.value.code == WS_FORBIDDEN


def test_missing_origin_allowed_only_with_explicit_config(db, make_staff):
    """(8) Missing Origin succeeds ONLY when the explicit exception is enabled."""
    user = make_staff("KITCHEN", store_id=DEFAULT_STORE_ID)
    c, _ = _ws_client_for(db, user)
    original = settings.ALLOW_MISSING_WEBSOCKET_ORIGIN
    settings.ALLOW_MISSING_WEBSOCKET_ORIGIN = True
    try:
        with _connect(c, origin=None) as ws:
            msg = ws.receive_json()
        assert msg["event"] == "initial_state"
    finally:
        settings.ALLOW_MISSING_WEBSOCKET_ORIGIN = original


# ── Session / role rejected even from a trusted origin ─────────────────────────

def test_unauthenticated_ws_rejected(db, make_staff):
    """(9) Invalid/absent session from a trusted origin is rejected (4401)."""
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, "not-a-real-token")
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(c, origin=TRUSTED_KITCHEN_ORIGIN):
            pass
    assert exc.value.code == WS_UNAUTHENTICATED


def test_revoked_session_cannot_reconnect(db, make_staff):
    """(10) A revoked session from a trusted origin is rejected (4401)."""
    user = make_staff("KITCHEN", store_id=DEFAULT_STORE_ID)
    session, raw_token, _csrf = auth_service.create_session(db, user)
    c = TestClient(app)
    c.cookies.set(SESSION_COOKIE, raw_token)

    with _connect(c, origin=TRUSTED_KITCHEN_ORIGIN) as ws:
        ws.receive_json()

    auth_service.revoke_session(db, session, reason="test")
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(c, origin=TRUSTED_KITCHEN_ORIGIN):
            pass
    assert exc.value.code == WS_UNAUTHENTICATED


def test_unauthorized_role_ws_rejected(db, make_staff):
    """(11) CASHIER (no kitchen:read) from a trusted origin is rejected (4403)."""
    user = make_staff("CASHIER", store_id=DEFAULT_STORE_ID)
    c, _ = _ws_client_for(db, user)
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(c, origin=TRUSTED_KITCHEN_ORIGIN):
            pass
    assert exc.value.code == WS_FORBIDDEN


# ── Store context comes from the session, never the client ─────────────────────

def test_ws_store_comes_from_session(db, make_staff, make_store):
    """(12) Store is derived from the session, not any client-supplied value."""
    store_b = make_store()
    user = make_staff("KITCHEN", store_id=store_b.id)
    c, _ = _ws_client_for(db, user)
    with _connect(c, origin=TRUSTED_KITCHEN_ORIGIN) as ws:
        msg = ws.receive_json()
    assert msg["event"] == "initial_state"
    assert msg["data"]["store_id"] == store_b.id


def test_ws_ignores_query_param_store(db, make_staff, make_store):
    """(12) A spoofed ?store_id query param is ignored; session store wins."""
    store_b = make_store()
    user = make_staff("KITCHEN", store_id=store_b.id)
    c, _ = _ws_client_for(db, user)
    with c.websocket_connect(
        f"/ws/kitchen?store_id={DEFAULT_STORE_ID}&token=abc",
        headers={"Origin": TRUSTED_KITCHEN_ORIGIN},
    ) as ws:
        msg = ws.receive_json()
    assert msg["data"]["store_id"] == store_b.id


def test_ws_initial_state_excludes_other_store_orders(db, make_staff, make_store):
    store_b = make_store()
    ing_a, oid_a = _order_in_store(db, DEFAULT_STORE_ID)
    ing_b, oid_b = _order_in_store(db, store_b.id)

    user_a = make_staff("KITCHEN", store_id=DEFAULT_STORE_ID)
    c, _ = _ws_client_for(db, user_a)
    with _connect(c, origin=TRUSTED_KITCHEN_ORIGIN) as ws:
        msg = ws.receive_json()

    ids = {o["id"] for o in msg["data"]["orders"]}
    assert oid_a in ids
    assert oid_b not in ids

    cleanup_ingredient(db, ing_a.id)
    cleanup_ingredient(db, ing_b.id)


# ── (13) Frontend WS URL carries no store or credential query parameter ────────

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FRONTEND_WS_FILES = [
    _REPO_ROOT / "apps" / "kitchen-web" / "src" / "app" / "page.tsx",
    _REPO_ROOT / "apps" / "owner-web" / "src" / "app" / "kitchen" / "page.tsx",
    _REPO_ROOT / "apps" / "owner-web" / "src" / "app" / "page.tsx",
]
_FORBIDDEN_WS_URL_TOKENS = ["store_id", "token", "csrf", "user_id", "role", "actor_id", "?"]


def test_frontend_ws_url_has_no_credential_or_store_query():
    """(13) Every frontend WS URL is exactly .../ws/kitchen — no query params."""
    ws_url_re = re.compile(r"wss?://[^\s\"'`]+")
    checked = 0
    for path in _FRONTEND_WS_FILES:
        assert path.exists(), f"expected frontend file missing: {path}"
        text = path.read_text(encoding="utf-8")
        for url in ws_url_re.findall(text):
            checked += 1
            assert url.endswith("/ws/kitchen"), f"unexpected WS URL {url} in {path}"
            for tok in _FORBIDDEN_WS_URL_TOKENS:
                assert tok not in url, f"WS URL {url} in {path} leaks '{tok}'"
    assert checked >= len(_FRONTEND_WS_FILES)


# ── (14) Store-partitioned broadcast: store A never reaches store B ────────────

class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, message: str) -> None:
        self.sent.append(message)


def test_store_a_broadcast_invisible_to_store_b():
    """(14) A broadcast for store A is delivered only to store-A sockets."""
    from app.services.websocket_manager import KitchenWebSocketManager

    mgr = KitchenWebSocketManager()
    sock_a, sock_b = _FakeWS(), _FakeWS()

    async def run():
        await mgr.connect(sock_a, 1)
        await mgr.connect(sock_b, 2)
        await mgr.broadcast_kitchen_event(1, "order_created", {"order_id": 7})

    asyncio.run(run())

    assert any("order_created" in m for m in sock_a.sent)
    assert sock_b.sent == []


# ── (15) Credentialed HTTP CORS never uses a wildcard origin ───────────────────

def test_cors_credentialed_no_wildcard():
    """(15) The credentialed CORS allow-list contains no wildcard origin."""
    origins = settings.all_cors_origins
    assert origins, "expected an explicit credentialed CORS allow-list"
    assert "*" not in origins
    for o in origins:
        assert o != "*"
        assert o.startswith("http://") or o.startswith("https://")

    # The live middleware must be credentialed AND non-wildcard.
    cors = next(
        (m for m in app.user_middleware if "CORSMiddleware" in str(m.cls)),
        None,
    )
    assert cors is not None
    assert cors.kwargs.get("allow_credentials") is True
    assert "*" not in cors.kwargs.get("allow_origins", [])
