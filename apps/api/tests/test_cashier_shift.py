"""
Cashier shift closing — API behaviour, permissions, idempotency, snapshot maths
and audit.

A shift is a reconciliation over the existing payment ledger. These tests prove:
opening/closing work and are store-scoped and idempotent; the close snapshot is
computed from the ledger for the shift window and then frozen; and every command
is audited exactly once.
"""
import uuid
from decimal import Decimal

from sqlalchemy import text

from app.models.audit_log import AuditLog
from app.models.cashier_shift import CashierShift
from tests.conftest import make_authed_client


def _key() -> str:
    return uuid.uuid4().hex


def _open(client, opening="200.00", key=None, note=None):
    body = {"opening_cash_amount": opening}
    if note is not None:
        body["open_note"] = note
    return client.post(
        "/cashier/shifts/open", json=body, headers={"Idempotency-Key": key or _key()}
    )


def _close(client, shift_id, counted="200.00", key=None, note=None):
    body = {"counted_closing_cash_amount": counted}
    if note is not None:
        body["close_note"] = note
    return client.post(
        f"/cashier/shifts/{shift_id}/close",
        json=body,
        headers={"Idempotency-Key": key or _key()},
    )


def _collect(client, order_id, method="CASH", amount=None):
    body = {"payment_method": method}
    if amount is not None:
        body["amount"] = amount
    return client.post(
        f"/cashier/orders/{order_id}/payments",
        json=body,
        headers={"Idempotency-Key": _key()},
    )


# ── Open ──────────────────────────────────────────────────────────────────────

def test_cashier_can_open_own_shift(cashier_env):
    res = _open(cashier_env.client, "150.00")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "OPEN"
    assert body["opening_cash_amount"] == "150.00"
    assert body["cashier_user_id"] == cashier_env.cashier.id
    assert body["store_id"] == cashier_env.store.id
    assert body["closed_at"] is None
    assert body["counted_closing_cash_amount"] is None
    assert body["idempotent_replay"] is False


def test_open_negative_opening_cash_rejected(cashier_env):
    res = _open(cashier_env.client, "-1.00")
    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "opening_cash_invalid"


def test_open_zero_opening_cash_allowed(cashier_env):
    res = _open(cashier_env.client, "0.00")
    assert res.status_code == 200, res.text
    assert res.json()["opening_cash_amount"] == "0.00"


def test_open_unknown_field_rejected(cashier_env):
    res = cashier_env.client.post(
        "/cashier/shifts/open",
        json={"opening_cash_amount": "10.00", "store_id": 999},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 422  # extra="forbid"


def test_open_missing_idempotency_key_rejected(cashier_env):
    res = cashier_env.client.post(
        "/cashier/shifts/open", json={"opening_cash_amount": "10.00"}
    )
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "idempotency_required"


def test_open_missing_csrf_rejected(db, cashier_env):
    # A fresh client with the session cookie but no X-CSRF-Token header.
    from app.core.config import settings
    from app.main import app
    from fastapi.testclient import TestClient
    from app.services import auth_service

    _s, raw_token, _csrf = auth_service.create_session(db, cashier_env.cashier)
    bare = TestClient(app)
    bare.cookies.set(settings.SESSION_COOKIE_NAME, raw_token)
    res = bare.post(
        "/cashier/shifts/open",
        json={"opening_cash_amount": "10.00"},
        headers={"Idempotency-Key": _key(), "Origin": settings.staff_origins[0]},
    )
    assert res.status_code == 403
    assert res.json()["detail"]["error"] == "csrf_invalid"


def test_duplicate_open_returns_existing_shift(cashier_env):
    first = _open(cashier_env.client, "100.00")
    assert first.status_code == 200
    # A DIFFERENT key, but the cashier already has an open shift → return it,
    # never a second open shift.
    second = _open(cashier_env.client, "999.00")
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["opening_cash_amount"] == "100.00"  # not the 999


def test_open_same_key_same_payload_replays(cashier_env):
    k = _key()
    first = _open(cashier_env.client, "100.00", key=k)
    second = _open(cashier_env.client, "100.00", key=k)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["idempotent_replay"] is True


def test_open_same_key_different_payload_conflicts(cashier_env):
    k = _key()
    _open(cashier_env.client, "100.00", key=k)
    res = _open(cashier_env.client, "250.00", key=k)
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "idempotency_mismatch"


def test_open_raw_idempotency_key_never_stored(db, cashier_env):
    k = _key()
    res = _open(cashier_env.client, "100.00", key=k)
    shift_id = res.json()["id"]
    row = db.get(CashierShift, shift_id)
    db.refresh(row)
    assert row.opened_idempotency_key_hash != k
    assert len(row.opened_idempotency_key_hash) == 64  # sha-256 hex


def _opened_audit_count(db) -> int:
    # Count globally rather than by entity_id: the migration round-trip tests drop
    # and recreate cashier_shifts (resetting its id sequence) while the append-only
    # audit log keeps older shift audits, so a shift id can be reused. Measuring the
    # DELTA caused by our own two calls is robust to that; an absolute-by-id count is
    # not. Nothing else writes between the measurements (tests run serially).
    db.expire_all()
    return db.query(AuditLog).filter(
        AuditLog.entity_type == "cashier_shift",
        AuditLog.action == "CASHIER_SHIFT_OPENED",
    ).count()


def test_open_audit_written_once(db, cashier_env):
    before = _opened_audit_count(db)
    k = _key()
    _open(cashier_env.client, "100.00", key=k)
    after_create = _opened_audit_count(db)
    assert after_create == before + 1  # the open wrote exactly one OPENED event
    # Replay must NOT write a second OPENED event.
    _open(cashier_env.client, "100.00", key=k)
    assert _opened_audit_count(db) == after_create


def test_owner_can_open_own_shift(db, make_store, make_staff):
    store = make_store()
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))
    res = _open(owner, "300.00")
    assert res.status_code == 200, res.text


# ── Current ───────────────────────────────────────────────────────────────────

def test_current_returns_open_shift(cashier_env):
    opened = _open(cashier_env.client, "120.00")
    res = cashier_env.client.get("/cashier/shifts/current")
    assert res.status_code == 200
    assert res.json()["current_shift"]["id"] == opened.json()["id"]


def test_current_null_when_none(cashier_env):
    res = cashier_env.client.get("/cashier/shifts/current")
    assert res.status_code == 200
    assert res.json()["current_shift"] is None


def test_current_null_after_close(cashier_env):
    opened = _open(cashier_env.client, "120.00")
    _close(cashier_env.client, opened.json()["id"], "120.00")
    res = cashier_env.client.get("/cashier/shifts/current")
    assert res.json()["current_shift"] is None


# ── Close: snapshot maths ─────────────────────────────────────────────────────

def test_close_computes_full_snapshot(db, make_store, make_table, make_staff, make_order):
    store = make_store()
    table = make_table(store.id)
    cashier = make_staff("CASHIER", store_id=store.id)
    cclient = make_authed_client(db, cashier)
    manager = make_authed_client(db, make_staff("MANAGER", store_id=store.id))

    opened = _open(cclient, "100.00")
    shift_id = opened.json()["id"]

    # Cashier collects: cash 60, card 40, then another cash 20 → cash 80, card 40.
    o1 = make_order(store.id, table.id, Decimal("60.00"))
    o2 = make_order(store.id, table.id, Decimal("40.00"))
    o3 = make_order(store.id, table.id, Decimal("20.00"))
    assert _collect(cclient, o1.id, "CASH").status_code == 200
    assert _collect(cclient, o2.id, "CARD").status_code == 200
    assert _collect(cclient, o3.id, "CASH").status_code == 200

    # Manager refunds 10 of the cash sale o1 (money the CASHIER collected).
    alloc = db.execute(
        text(
            "SELECT a.id FROM payment_allocations a JOIN payment_settlements s "
            "ON s.id=a.settlement_id WHERE a.order_id=:oid"
        ),
        {"oid": o1.id},
    ).scalar()
    ref = manager.post(
        f"/cashier/allocations/{alloc}/refunds",
        json={"amount": "10.00", "reason": "eksik ürün"},
        headers={"Idempotency-Key": _key()},
    )
    assert ref.status_code == 200, ref.text

    res = _close(cclient, shift_id, counted="175.00")
    assert res.status_code == 200, res.text
    b = res.json()
    assert b["status"] == "CLOSED"
    assert b["cash_payments_amount"] == "80.00"
    assert b["card_payments_amount"] == "40.00"
    assert b["cash_refunds_amount"] == "10.00"
    assert b["card_refunds_amount"] == "0.00"
    assert b["gross_payments_amount"] == "120.00"
    assert b["total_refunds_amount"] == "10.00"
    assert b["net_collected_amount"] == "110.00"
    # expected = opening 100 + cash 80 - cash refund 10 = 170
    assert b["expected_closing_cash_amount"] == "170.00"
    # discrepancy = counted 175 - expected 170 = +5 (fazla)
    assert b["cash_discrepancy_amount"] == "5.00"


def test_close_discrepancy_negative_when_short(db, make_store, make_table, make_staff, make_order):
    store = make_store()
    table = make_table(store.id)
    cclient = make_authed_client(db, make_staff("CASHIER", store_id=store.id))
    opened = _open(cclient, "100.00")
    o1 = make_order(store.id, table.id, Decimal("50.00"))
    _collect(cclient, o1.id, "CASH")
    # expected = 100 + 50 = 150, counted 140 → -10 (eksik)
    res = _close(cclient, opened.json()["id"], counted="140.00")
    assert res.json()["cash_discrepancy_amount"] == "-10.00"


def test_close_snapshot_frozen_after_new_payment(db, make_store, make_table, make_staff, make_order):
    store = make_store()
    table = make_table(store.id)
    cclient = make_authed_client(db, make_staff("CASHIER", store_id=store.id))
    opened = _open(cclient, "0.00")
    o1 = make_order(store.id, table.id, Decimal("30.00"))
    _collect(cclient, o1.id, "CASH")
    closed = _close(cclient, opened.json()["id"], counted="30.00")
    snap = closed.json()

    # A payment collected AFTER the close must not change the snapshot.
    o2 = make_order(store.id, table.id, Decimal("99.00"))
    _collect(cclient, o2.id, "CASH")
    again = cclient.get(f"/cashier/shifts/{opened.json()['id']}")
    assert again.json()["cash_payments_amount"] == snap["cash_payments_amount"] == "30.00"
    assert again.json()["gross_payments_amount"] == "30.00"


def test_close_counted_negative_rejected(cashier_env):
    opened = _open(cashier_env.client, "10.00")
    res = _close(cashier_env.client, opened.json()["id"], counted="-5.00")
    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "counted_cash_invalid"


def test_close_unknown_field_rejected(cashier_env):
    opened = _open(cashier_env.client, "10.00")
    res = cashier_env.client.post(
        f"/cashier/shifts/{opened.json()['id']}/close",
        json={"counted_closing_cash_amount": "10.00", "bogus": 1},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 422


# ── Close: idempotency + immutability ─────────────────────────────────────────

def test_close_same_key_same_payload_replays(cashier_env):
    opened = _open(cashier_env.client, "10.00")
    sid = opened.json()["id"]
    k = _key()
    first = _close(cashier_env.client, sid, counted="10.00", key=k)
    second = _close(cashier_env.client, sid, counted="10.00", key=k)
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert second.json()["closed_at"] == first.json()["closed_at"]


def test_close_same_key_different_payload_conflicts(cashier_env):
    opened = _open(cashier_env.client, "10.00")
    sid = opened.json()["id"]
    k = _key()
    _close(cashier_env.client, sid, counted="10.00", key=k)
    res = _close(cashier_env.client, sid, counted="99.00", key=k)
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "idempotency_mismatch"


def test_closed_shift_cannot_be_closed_again(cashier_env):
    opened = _open(cashier_env.client, "10.00")
    sid = opened.json()["id"]
    _close(cashier_env.client, sid, counted="10.00")
    res = _close(cashier_env.client, sid, counted="10.00")  # different key
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "already_closed"


def _closed_audit_count(db) -> int:
    # Delta-based for the same reason as _opened_audit_count above.
    db.expire_all()
    return db.query(AuditLog).filter(
        AuditLog.entity_type == "cashier_shift",
        AuditLog.action == "CASHIER_SHIFT_CLOSED",
    ).count()


def test_close_audit_written_once(db, cashier_env):
    opened = _open(cashier_env.client, "10.00")
    sid = opened.json()["id"]
    before = _closed_audit_count(db)
    k = _key()
    _close(cashier_env.client, sid, counted="10.00", key=k)
    after_close = _closed_audit_count(db)
    assert after_close == before + 1  # the close wrote exactly one CLOSED event
    _close(cashier_env.client, sid, counted="10.00", key=k)  # replay
    assert _closed_audit_count(db) == after_close


# ── Close: permissions + store scope ──────────────────────────────────────────

def test_cashier_cannot_close_another_cashier_shift(db, make_store, make_staff):
    store = make_store()
    c1 = make_staff("CASHIER", store_id=store.id)
    c2 = make_staff("CASHIER", store_id=store.id)
    client1 = make_authed_client(db, c1)
    client2 = make_authed_client(db, c2)
    opened = _open(client1, "10.00")
    sid = opened.json()["id"]
    res = _close(client2, sid, counted="10.00")
    assert res.status_code == 403


def test_manager_can_close_own_store_shift(db, make_store, make_staff):
    store = make_store()
    cashier = make_staff("CASHIER", store_id=store.id)
    cclient = make_authed_client(db, cashier)
    manager = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    opened = _open(cclient, "10.00")
    res = _close(manager, opened.json()["id"], counted="10.00")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "CLOSED"


def test_cross_store_close_rejected(db, make_store, make_staff):
    a = make_store()
    b = make_store()
    ca = make_authed_client(db, make_staff("CASHIER", store_id=a.id))
    cb_owner = make_authed_client(db, make_staff("OWNER", store_id=b.id))
    opened = _open(ca, "10.00")
    # Store-B owner cannot even see store-A's shift → 404, not 403.
    res = _close(cb_owner, opened.json()["id"], counted="10.00")
    assert res.status_code == 404


# ── Reads: scope ──────────────────────────────────────────────────────────────

def test_shift_list_store_scoped(db, make_store, make_staff):
    a = make_store()
    b = make_store()
    ca = make_authed_client(db, make_staff("CASHIER", store_id=a.id))
    cb = make_authed_client(db, make_staff("CASHIER", store_id=b.id))
    _open(ca, "10.00")
    _open(cb, "20.00")
    owner_a = make_authed_client(db, make_staff("OWNER", store_id=a.id))
    shifts = owner_a.get("/cashier/shifts").json()["shifts"]
    assert all(s["store_id"] == a.id for s in shifts)
    assert len(shifts) >= 1


def test_cashier_lists_only_own_shifts(db, make_store, make_staff):
    store = make_store()
    c1 = make_staff("CASHIER", store_id=store.id)
    c2 = make_staff("CASHIER", store_id=store.id)
    client1 = make_authed_client(db, c1)
    client2 = make_authed_client(db, c2)
    _open(client1, "10.00")
    _open(client2, "20.00")
    mine = client1.get("/cashier/shifts").json()["shifts"]
    assert all(s["cashier_user_id"] == c1.id for s in mine)


def test_owner_sees_all_store_shifts(db, make_store, make_staff):
    store = make_store()
    c1 = make_staff("CASHIER", store_id=store.id)
    c2 = make_staff("CASHIER", store_id=store.id)
    _open(make_authed_client(db, c1), "10.00")
    _open(make_authed_client(db, c2), "20.00")
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))
    ids = {s["cashier_user_id"] for s in owner.get("/cashier/shifts").json()["shifts"]}
    assert {c1.id, c2.id} <= ids


def test_cashier_cannot_read_another_cashier_shift(db, make_store, make_staff):
    store = make_store()
    c1 = make_staff("CASHIER", store_id=store.id)
    c2 = make_staff("CASHIER", store_id=store.id)
    opened = _open(make_authed_client(db, c1), "10.00")
    res = make_authed_client(db, c2).get(f"/cashier/shifts/{opened.json()['id']}")
    assert res.status_code == 404


def test_shift_detail_cross_store_404(db, make_store, make_staff):
    a = make_store()
    b = make_store()
    opened = _open(make_authed_client(db, make_staff("CASHIER", store_id=a.id)), "10.00")
    res = make_authed_client(db, make_staff("OWNER", store_id=b.id)).get(
        f"/cashier/shifts/{opened.json()['id']}"
    )
    assert res.status_code == 404
