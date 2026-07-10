"""
Server-controlled currency.

SweetOps is single-currency (TRY). The currency is fixed by the server: it is
never read from a request body, the cashier UI cannot choose it, and a refund
always inherits the currency of its original settlement. Orders carry no
currency column, so the whole system is TRY by construction — there is no
exchange conversion anywhere.

These tests prove:
  1. a settlement created for a (TRY) order is always TRY — the client has no
     accepted channel to choose a currency (a `currency` field is now rejected
     outright, see test_payment_schema_strictness.py),
  2. a refund inherits its settlement's server currency,
  3. a full-table settlement over several orders is always the single server
     currency (there is no per-order currency to mix),
  4. the settlement receipt and the analytics summary report the persisted,
     server-derived currency.
"""
import uuid
from decimal import Decimal

from tests.conftest import make_authed_client


def _key() -> str:
    return uuid.uuid4().hex


def test_settlement_currency_is_server_try(cashier_env, make_order, db):
    from app.models.payment_settlement import PaymentSettlement
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("100.00"))
    # A valid payload carries no currency; the server fixes it to TRY. (A hostile
    # client that injects `currency` is now rejected — see
    # test_payment_schema_strictness.py.)
    res = env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text
    assert res.json()["currency"] == "TRY"
    row = db.get(PaymentSettlement, res.json()["settlement_id"])
    assert row.currency == "TRY"


def test_refund_inherits_server_currency(db, make_store, make_table, make_staff, make_order):
    from app.models.payment_refund import PaymentRefund
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("100.00"))
    pay = mgr.post(f"/cashier/orders/{order.id}/payments",
                   json={"payment_method": "CASH"},
                   headers={"Idempotency-Key": _key()})
    alloc = pay.json()["allocations"][0]["id"]

    res = mgr.post(
        f"/cashier/allocations/{alloc}/refunds",
        json={"amount": "30.00", "reason": "iade"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text
    assert res.json()["currency"] == "TRY"
    row = db.get(PaymentRefund, res.json()["refund_id"])
    assert row.currency == "TRY"


def test_full_table_settlement_is_single_server_currency(cashier_env, make_order):
    env = cashier_env
    o1 = make_order(env.store.id, env.table.id, Decimal("30.00"))
    o2 = make_order(env.store.id, env.table.id, Decimal("70.00"))
    res = env.client.post(
        "/cashier/settlements",
        json={"table_id": env.table.id, "order_ids": [o1.id, o2.id],
              "payment_method": "CARD"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text
    assert res.json()["currency"] == "TRY"


def test_receipt_and_analytics_use_server_currency(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("50.00"))
    pay = mgr.post(f"/cashier/orders/{order.id}/payments",
                   json={"payment_method": "CASH"},
                   headers={"Idempotency-Key": _key()})
    sid = pay.json()["settlement_id"]

    receipt = mgr.get(f"/cashier/settlements/{sid}").json()
    assert receipt["currency"] == "TRY"

    summary = mgr.get("/owner/payment-summary").json()
    assert summary["currency"] == "TRY"
