"""
Strict financial-mutation request schemas.

Financial write requests (settlement creation, single-order payment, refund
creation) set Pydantic ``extra="forbid"``. An unknown field — most importantly a
client-supplied ``currency`` — is REJECTED with a 422 validation error instead of
being silently dropped. Silently discarding a financial instruction would let a
client believe an override was honoured; forbidding extras makes the contract
explicit while the server currency (TRY) stays authoritative because there is no
accepted channel to supply one.

These tests prove:
  1. a settlement request carrying ``currency`` is rejected,
  2. a refund request carrying ``currency`` is rejected,
  3. an arbitrary unknown financial field is rejected,
  4. the equivalent valid requests (no extra fields) still succeed — the
     strictness is backward compatible with the documented contract.
"""
import uuid
from decimal import Decimal

from tests.conftest import make_authed_client


def _key() -> str:
    return uuid.uuid4().hex


# ── 1. Settlement request with `currency` is rejected ───────────────────────────

def test_settlement_request_with_currency_rejected(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("40.00"))
    res = env.client.post(
        "/cashier/settlements",
        json={"table_id": env.table.id, "order_ids": [order.id],
              "payment_method": "CASH", "currency": "USD"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 422, res.text


def test_order_payment_request_with_currency_rejected(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("40.00"))
    res = env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH", "currency": "USD"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 422, res.text


# ── 2. Refund request with `currency` is rejected ───────────────────────────────

def test_refund_request_with_currency_rejected(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("40.00"))
    pay = mgr.post(f"/cashier/orders/{order.id}/payments",
                   json={"payment_method": "CASH"},
                   headers={"Idempotency-Key": _key()})
    alloc = pay.json()["allocations"][0]["id"]

    res = mgr.post(
        f"/cashier/allocations/{alloc}/refunds",
        json={"amount": "10.00", "reason": "iade", "currency": "USD"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 422, res.text


# ── 3. Arbitrary unknown financial field is rejected ────────────────────────────

def test_unknown_financial_field_rejected(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("40.00"))
    res = env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH", "gross_amount": "999.00"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 422, res.text


# ── 4. Valid requests remain backward compatible ────────────────────────────────

def test_valid_order_payment_still_succeeds(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("40.00"))
    res = env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text
    assert res.json()["currency"] == "TRY"


def test_valid_settlement_and_refund_still_succeed(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("40.00"))

    settle = mgr.post(
        "/cashier/settlements",
        json={"table_id": table.id, "order_ids": [order.id],
              "payment_method": "CARD", "note": "ok", "terminal_reference": "T1"},
        headers={"Idempotency-Key": _key()},
    )
    assert settle.status_code == 200, settle.text
    alloc = settle.json()["allocations"][0]["id"]

    refund = mgr.post(
        f"/cashier/allocations/{alloc}/refunds",
        json={"amount": "5.00", "reason": "kismi iade"},
        headers={"Idempotency-Key": _key()},
    )
    assert refund.status_code == 200, refund.text
