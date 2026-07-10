"""Permission matrix for payment endpoints (OWNER/MANAGER/CASHIER/KITCHEN)."""
import uuid
from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import make_authed_client


def _key() -> str:
    return uuid.uuid4().hex


def _collect(client, order_id):
    return client.post(
        f"/cashier/orders/{order_id}/payments",
        json={"payment_method": "CASH"},
        headers={"Idempotency-Key": _key()},
    )


def test_owner_can_read_collect_refund(db, make_store, make_table, make_staff, make_order):
    store = make_store()
    table = make_table(store.id)
    owner = make_staff("OWNER", store_id=store.id)
    client = make_authed_client(db, owner)
    order = make_order(store.id, table.id, Decimal("100.00"))

    assert client.get("/cashier/tables/open").status_code == 200
    res = _collect(client, order.id)
    assert res.status_code == 200
    alloc_id = res.json()["allocations"][0]["id"]
    refund = client.post(
        f"/cashier/allocations/{alloc_id}/refunds",
        json={"amount": "10.00", "reason": "test"},
        headers={"Idempotency-Key": _key()},
    )
    assert refund.status_code == 200, refund.text


def test_manager_can_read_collect_refund(db, make_store, make_table, make_staff, make_order):
    store = make_store()
    table = make_table(store.id)
    mgr = make_staff("MANAGER", store_id=store.id)
    client = make_authed_client(db, mgr)
    order = make_order(store.id, table.id, Decimal("50.00"))

    assert client.get("/cashier/tables/open").status_code == 200
    res = _collect(client, order.id)
    assert res.status_code == 200
    alloc_id = res.json()["allocations"][0]["id"]
    refund = client.post(
        f"/cashier/allocations/{alloc_id}/refunds",
        json={"amount": "5.00", "reason": "iade"},
        headers={"Idempotency-Key": _key()},
    )
    assert refund.status_code == 200, refund.text


def test_cashier_can_read_and_collect(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("30.00"))
    assert env.client.get("/cashier/tables/open").status_code == 200
    assert _collect(env.client, order.id).status_code == 200


def test_cashier_cannot_refund(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("30.00"))
    res = _collect(env.client, order.id)
    alloc_id = res.json()["allocations"][0]["id"]
    refund = env.client.post(
        f"/cashier/allocations/{alloc_id}/refunds",
        json={"amount": "5.00", "reason": "x"},
        headers={"Idempotency-Key": _key()},
    )
    assert refund.status_code == 403


def test_kitchen_cannot_read_or_collect(db, make_store, make_table, make_staff, make_order):
    store = make_store()
    table = make_table(store.id)
    kitchen = make_staff("KITCHEN", store_id=store.id)
    client = make_authed_client(db, kitchen)
    order = make_order(store.id, table.id, Decimal("30.00"))

    assert client.get("/cashier/tables/open").status_code == 403
    assert _collect(client, order.id).status_code == 403


def test_unauthenticated_returns_401():
    client = TestClient(app)
    assert client.get("/cashier/tables/open").status_code == 401


def test_missing_csrf_forbidden(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("30.00"))
    # Strip the CSRF header the fixture preset.
    res = env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH"},
        headers={"Idempotency-Key": _key(), "X-CSRF-Token": ""},
    )
    assert res.status_code == 403
