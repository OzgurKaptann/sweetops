"""Cancellation interaction with payment state."""
import uuid
from decimal import Decimal

from tests.conftest import make_authed_client
from app.services.kitchen_service import update_order_status


def _key() -> str:
    return uuid.uuid4().hex


class _DummyBg:
    def add_task(self, *a, **k):
        pass


def test_unpaid_order_can_be_cancelled(db, make_store, make_table, make_order):
    store = make_store(); table = make_table(store.id)
    order = make_order(store.id, table.id, Decimal("50.00"), status="NEW")
    updated = update_order_status(
        db, order.id, "CANCELLED", _DummyBg(), store_id=store.id,
        actor_type="STAFF", actor_id="1",
    )
    assert updated.status == "CANCELLED"


def test_paid_order_cancellation_blocked(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("50.00"), status="NEW")
    mgr.post(f"/cashier/orders/{order.id}/payments",
             json={"payment_method": "CASH"},
             headers={"Idempotency-Key": _key()})

    import pytest
    from fastapi import HTTPException
    db.expire_all()
    with pytest.raises(HTTPException) as exc:
        update_order_status(db, order.id, "CANCELLED", _DummyBg(),
                            store_id=store.id, actor_type="STAFF", actor_id="1")
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "payment_outstanding"


def test_partially_paid_order_cancellation_blocked(db, make_store, make_table, make_staff, make_order):
    import pytest
    from fastapi import HTTPException
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("50.00"), status="NEW")
    mgr.post(f"/cashier/orders/{order.id}/payments",
             json={"payment_method": "CASH", "amount": "20.00"},
             headers={"Idempotency-Key": _key()})
    db.expire_all()
    with pytest.raises(HTTPException) as exc:
        update_order_status(db, order.id, "CANCELLED", _DummyBg(),
                            store_id=store.id, actor_type="STAFF", actor_id="1")
    assert exc.value.status_code == 409


def test_fully_refunded_order_can_be_cancelled(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("50.00"), status="NEW")
    res = mgr.post(f"/cashier/orders/{order.id}/payments",
                   json={"payment_method": "CASH"},
                   headers={"Idempotency-Key": _key()})
    alloc = res.json()["allocations"][0]["id"]
    mgr.post(f"/cashier/allocations/{alloc}/refunds",
             json={"amount": "50.00", "reason": "iade"},
             headers={"Idempotency-Key": _key()})
    db.expire_all()
    updated = update_order_status(db, order.id, "CANCELLED", _DummyBg(),
                                  store_id=store.id, actor_type="STAFF", actor_id="1")
    assert updated.status == "CANCELLED"


def test_cancel_creates_no_payment_record(db, make_store, make_table, make_order):
    from app.models.payment_settlement import PaymentSettlement
    from app.models.payment_refund import PaymentRefund
    store = make_store(); table = make_table(store.id)
    order = make_order(store.id, table.id, Decimal("50.00"), status="NEW")
    update_order_status(db, order.id, "CANCELLED", _DummyBg(),
                        store_id=store.id, actor_type="STAFF", actor_id="1")
    assert db.query(PaymentSettlement).filter(PaymentSettlement.table_id == table.id).count() == 0
    assert db.query(PaymentRefund).filter(PaymentRefund.order_id == order.id).count() == 0
