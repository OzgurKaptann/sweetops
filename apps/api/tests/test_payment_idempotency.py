"""Idempotency: replay safety and payload-mismatch protection."""
import uuid
from decimal import Decimal

from tests.conftest import make_authed_client


def _key() -> str:
    return uuid.uuid4().hex


def test_same_key_same_payload_returns_same_settlement(cashier_env, make_order, db):
    from app.models.payment_allocation import PaymentAllocation
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("100.00"))
    key = _key()
    body = {"payment_method": "CASH"}

    r1 = env.client.post(f"/cashier/orders/{order.id}/payments", json=body,
                         headers={"Idempotency-Key": key})
    r2 = env.client.post(f"/cashier/orders/{order.id}/payments", json=body,
                         headers={"Idempotency-Key": key})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["settlement_id"] == r2.json()["settlement_id"]
    assert r2.json()["idempotent_replay"] is True

    # No duplicate allocations; paid amount incremented once.
    alloc_count = db.query(PaymentAllocation).filter(
        PaymentAllocation.order_id == order.id
    ).count()
    assert alloc_count == 1
    detail = env.client.get(f"/cashier/orders/{order.id}").json()
    assert detail["paid_amount"] == "100.00"


def test_same_key_different_payload_conflict(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("100.00"))
    key = _key()
    env.client.post(f"/cashier/orders/{order.id}/payments",
                    json={"payment_method": "CASH", "amount": "40.00"},
                    headers={"Idempotency-Key": key})
    res = env.client.post(f"/cashier/orders/{order.id}/payments",
                          json={"payment_method": "CARD", "amount": "50.00"},
                          headers={"Idempotency-Key": key})
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "idempotency_mismatch"


def test_refund_same_key_same_payload_returns_same_refund(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("100.00"))
    res = mgr.post(f"/cashier/orders/{order.id}/payments",
                   json={"payment_method": "CASH"},
                   headers={"Idempotency-Key": _key()})
    alloc_id = res.json()["allocations"][0]["id"]

    key = _key()
    body = {"amount": "30.00", "reason": "müşteri iadesi"}
    r1 = mgr.post(f"/cashier/allocations/{alloc_id}/refunds", json=body,
                  headers={"Idempotency-Key": key})
    r2 = mgr.post(f"/cashier/allocations/{alloc_id}/refunds", json=body,
                  headers={"Idempotency-Key": key})
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert r1.json()["refund_id"] == r2.json()["refund_id"]
    assert r2.json()["idempotent_replay"] is True
    detail = mgr.get(f"/cashier/orders/{order.id}").json()
    assert detail["refunded_amount"] == "30.00"


def test_refund_same_key_different_payload_conflict(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("100.00"))
    res = mgr.post(f"/cashier/orders/{order.id}/payments",
                   json={"payment_method": "CASH"},
                   headers={"Idempotency-Key": _key()})
    alloc_id = res.json()["allocations"][0]["id"]

    key = _key()
    mgr.post(f"/cashier/allocations/{alloc_id}/refunds",
             json={"amount": "30.00", "reason": "a"},
             headers={"Idempotency-Key": key})
    res2 = mgr.post(f"/cashier/allocations/{alloc_id}/refunds",
                    json={"amount": "40.00", "reason": "a"},
                    headers={"Idempotency-Key": key})
    assert res2.status_code == 409
    assert res2.json()["detail"]["error"] == "idempotency_mismatch"


def test_raw_key_never_stored_or_returned(cashier_env, make_order, db):
    from app.models.payment_settlement import PaymentSettlement
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("100.00"))
    key = "super-secret-key-12345"
    res = env.client.post(f"/cashier/orders/{order.id}/payments",
                          json={"payment_method": "CASH"},
                          headers={"Idempotency-Key": key})
    assert key not in res.text
    sid = res.json()["settlement_id"]
    row = db.get(PaymentSettlement, sid)
    assert row.idempotency_key_hash != key
    assert len(row.idempotency_key_hash) == 64  # sha256 hex
    assert row.request_hash and len(row.request_hash) == 64
