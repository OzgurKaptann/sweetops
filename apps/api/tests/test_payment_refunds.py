"""Refund rules: partial/full, overrun, reason, actor, summary, append-only."""
import uuid
from decimal import Decimal

import pytest

from tests.conftest import make_authed_client


def _key() -> str:
    return uuid.uuid4().hex


@pytest.fixture()
def paid_order(db, make_store, make_table, make_staff, make_order):
    """A store with a fully-paid order and a MANAGER client. Returns a namespace."""
    class Env:
        pass
    env = Env()
    env.store = make_store()
    env.table = make_table(env.store.id)
    env.manager = make_staff("MANAGER", store_id=env.store.id)
    env.client = make_authed_client(db, env.manager)
    env.order = make_order(env.store.id, env.table.id, Decimal("100.00"))
    res = env.client.post(f"/cashier/orders/{env.order.id}/payments",
                          json={"payment_method": "CASH"},
                          headers={"Idempotency-Key": _key()})
    env.allocation_id = res.json()["allocations"][0]["id"]
    return env


def _refund(env, amount, reason="iade"):
    return env.client.post(
        f"/cashier/allocations/{env.allocation_id}/refunds",
        json={"amount": amount, "reason": reason},
        headers={"Idempotency-Key": _key()},
    )


def test_partial_refund_succeeds(paid_order):
    res = _refund(paid_order, "30.00")
    assert res.status_code == 200, res.text
    detail = paid_order.client.get(f"/cashier/orders/{paid_order.order.id}").json()
    assert detail["refunded_amount"] == "30.00"
    assert detail["refund_status"] == "PARTIALLY_REFUNDED"
    assert detail["payment_status"] == "PARTIALLY_PAID"  # net paid dropped to 70


def test_full_refund_succeeds(paid_order):
    res = _refund(paid_order, "100.00")
    assert res.status_code == 200, res.text
    detail = paid_order.client.get(f"/cashier/orders/{paid_order.order.id}").json()
    assert detail["refunded_amount"] == "100.00"
    assert detail["refund_status"] == "REFUNDED"
    assert detail["payment_status"] == "UNPAID"  # net paid back to 0


def test_refund_above_refundable_rejected(paid_order):
    assert _refund(paid_order, "150.00").status_code == 409


def test_refund_incremental_cannot_exceed_total(paid_order):
    assert _refund(paid_order, "60.00").status_code == 200
    # 60 already refunded, only 40 refundable.
    res = _refund(paid_order, "50.00")
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "refund_over_balance"


def test_refund_requires_reason(paid_order):
    res = paid_order.client.post(
        f"/cashier/allocations/{paid_order.allocation_id}/refunds",
        json={"amount": "10.00", "reason": ""},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 422


def test_refund_actor_from_session(paid_order, db):
    from app.models.payment_refund import PaymentRefund
    res = _refund(paid_order, "10.00")
    refund_id = res.json()["refund_id"]
    row = db.get(PaymentRefund, refund_id)
    assert row.refunded_by_user_id == paid_order.manager.id
    assert res.json()["refunded_by_display"] == paid_order.manager.username


def test_refund_does_not_change_preparation_status(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("100.00"), status="READY")
    res = mgr.post(f"/cashier/orders/{order.id}/payments",
                   json={"payment_method": "CASH"},
                   headers={"Idempotency-Key": _key()})
    alloc = res.json()["allocations"][0]["id"]
    mgr.post(f"/cashier/allocations/{alloc}/refunds",
             json={"amount": "50.00", "reason": "x"},
             headers={"Idempotency-Key": _key()})
    detail = mgr.get(f"/cashier/orders/{order.id}").json()
    assert detail["preparation_status"] == "READY"


def test_refund_history_append_only(paid_order, db):
    from app.models.payment_refund import PaymentRefund
    _refund(paid_order, "20.00")
    _refund(paid_order, "30.00")
    rows = db.query(PaymentRefund).filter(
        PaymentRefund.order_id == paid_order.order.id
    ).all()
    assert len(rows) == 2
    assert sorted(str(r.amount) for r in rows) == ["20.00", "30.00"]
