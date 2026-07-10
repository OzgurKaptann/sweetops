"""Cashier search, open-table list, and table bill."""
import uuid
from decimal import Decimal


def _key() -> str:
    return uuid.uuid4().hex


def test_open_table_list_accurate_remaining(cashier_env, make_order):
    env = cashier_env
    make_order(env.store.id, env.table.id, Decimal("40.00"))
    make_order(env.store.id, env.table.id, Decimal("60.00"))
    tables = env.client.get("/cashier/tables/open").json()["tables"]
    row = next(t for t in tables if t["table_id"] == env.table.id)
    assert row["open_order_count"] == 2
    assert row["gross_amount"] == "100.00"
    assert row["remaining_amount"] == "100.00"


def test_order_search_by_code(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("40.00"))
    code = f"SIP-{order.id:06d}"
    res = env.client.get(f"/cashier/orders/search?q={code}")
    assert res.status_code == 200
    assert res.json()["order_id"] == order.id
    # Plain numeric id also works.
    res2 = env.client.get(f"/cashier/orders/search?q={order.id}")
    assert res2.status_code == 200 and res2.json()["order_id"] == order.id


def test_table_bill_lists_open_orders(cashier_env, make_order):
    env = cashier_env
    o1 = make_order(env.store.id, env.table.id, Decimal("40.00"))
    o2 = make_order(env.store.id, env.table.id, Decimal("60.00"))
    bill = env.client.get(f"/cashier/tables/{env.table.id}/bill").json()
    ids = {o["order_id"] for o in bill["orders"]}
    assert {o1.id, o2.id} <= ids
    assert bill["remaining_amount"] == "100.00"


def test_paid_orders_not_shown_as_outstanding(cashier_env, make_order):
    env = cashier_env
    paid = make_order(env.store.id, env.table.id, Decimal("40.00"))
    make_order(env.store.id, env.table.id, Decimal("60.00"))
    env.client.post(f"/cashier/orders/{paid.id}/payments",
                    json={"payment_method": "CASH"},
                    headers={"Idempotency-Key": _key()})
    tables = env.client.get("/cashier/tables/open").json()["tables"]
    row = next(t for t in tables if t["table_id"] == env.table.id)
    assert row["open_order_count"] == 1
    assert row["remaining_amount"] == "60.00"


def test_cancelled_orders_excluded_from_balance(cashier_env, make_order):
    env = cashier_env
    make_order(env.store.id, env.table.id, Decimal("40.00"))
    make_order(env.store.id, env.table.id, Decimal("99.00"), status="CANCELLED")
    bill = env.client.get(f"/cashier/tables/{env.table.id}/bill").json()
    # Cancelled order is not listed and not in the payable balance.
    assert bill["remaining_amount"] == "40.00"
    assert all(o["preparation_status"] != "CANCELLED" for o in bill["orders"])
