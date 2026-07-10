"""Store isolation: a Store-A cashier can never touch Store-B payments."""
import uuid
from decimal import Decimal

from tests.conftest import make_authed_client


def _key() -> str:
    return uuid.uuid4().hex


def _collect(client, order_id, amount=None):
    body = {"payment_method": "CASH"}
    if amount is not None:
        body["amount"] = amount
    return client.post(
        f"/cashier/orders/{order_id}/payments",
        json=body,
        headers={"Idempotency-Key": _key()},
    )


def test_open_tables_only_own_store(db, make_store, make_table, make_staff, make_order):
    a = make_store(); b = make_store()
    ta = make_table(a.id); tb = make_table(b.id)
    make_order(a.id, ta.id, Decimal("10.00"))
    make_order(b.id, tb.id, Decimal("20.00"))

    ca = make_authed_client(db, make_staff("CASHIER", store_id=a.id))
    tables = ca.get("/cashier/tables/open").json()["tables"]
    table_ids = {t["table_id"] for t in tables}
    assert ta.id in table_ids
    assert tb.id not in table_ids


def test_cannot_read_other_store_order(db, make_store, make_table, make_staff, make_order):
    a = make_store(); b = make_store()
    tb = make_table(b.id)
    order_b = make_order(b.id, tb.id, Decimal("20.00"))
    ca = make_authed_client(db, make_staff("CASHIER", store_id=a.id))
    res = ca.get(f"/cashier/orders/{order_b.id}")
    assert res.status_code == 404


def test_cannot_collect_other_store_order(db, make_store, make_table, make_staff, make_order):
    a = make_store(); b = make_store()
    tb = make_table(b.id)
    order_b = make_order(b.id, tb.id, Decimal("20.00"))
    ca = make_authed_client(db, make_staff("CASHIER", store_id=a.id))
    assert _collect(ca, order_b.id).status_code == 404


def test_cannot_refund_other_store_payment(db, make_store, make_table, make_staff, make_order):
    a = make_store(); b = make_store()
    tb = make_table(b.id)
    order_b = make_order(b.id, tb.id, Decimal("20.00"))
    # Store B manager collects.
    mb = make_authed_client(db, make_staff("MANAGER", store_id=b.id))
    res = _collect(mb, order_b.id)
    alloc_id = res.json()["allocations"][0]["id"]
    # Store A manager tries to refund it.
    ma = make_authed_client(db, make_staff("MANAGER", store_id=a.id))
    refund = ma.post(
        f"/cashier/allocations/{alloc_id}/refunds",
        json={"amount": "5.00", "reason": "x"},
        headers={"Idempotency-Key": _key()},
    )
    assert refund.status_code == 404


def test_client_store_id_cannot_override_session(db, make_store, make_table, make_staff, make_order):
    """The settlement body has no store_id; a table from another store is rejected."""
    a = make_store(); b = make_store()
    ta = make_table(a.id); tb = make_table(b.id)
    order_b = make_order(b.id, tb.id, Decimal("20.00"))
    ca = make_authed_client(db, make_staff("CASHIER", store_id=a.id))
    res = ca.post(
        "/cashier/settlements",
        json={"table_id": tb.id, "order_ids": [order_b.id], "payment_method": "CASH"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 404


def test_order_must_belong_to_table(cashier_env, make_table, make_order):
    env = cashier_env
    other_table = make_table(env.store.id)
    order = make_order(env.store.id, other_table.id, Decimal("15.00"))
    res = env.client.post(
        "/cashier/settlements",
        json={"table_id": env.table.id, "order_ids": [order.id], "payment_method": "CASH"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "table_mismatch"


def test_payment_summary_store_scoped(db, make_store, make_table, make_staff, make_order):
    a = make_store(); b = make_store()
    ta = make_table(a.id); tb = make_table(b.id)
    oa = make_order(a.id, ta.id, Decimal("100.00"))
    make_order(b.id, tb.id, Decimal("999.00"))
    ca = make_authed_client(db, make_staff("MANAGER", store_id=a.id))
    _collect(ca, oa.id)
    summary = ca.get("/owner/payment-summary").json()
    assert summary["store_id"] == a.id
    assert summary["gross_order_value"] == "100.00"
    assert summary["collected_amount"] == "100.00"
