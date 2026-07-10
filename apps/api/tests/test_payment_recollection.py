"""
Fully-refunded vs never-paid order behaviour.

After a full refund an order has net_paid == 0, exactly like a never-paid order,
but it is NOT the same thing operationally. The API keeps them distinguishable
(refund_status) and refuses to silently recollect a refunded order through the
generic one-click "settle whole table" flow — recollection must be an explicit,
per-order action.

  * never-paid:      payment_status=UNPAID, refund_status=NONE
  * fully-refunded:  payment_status=UNPAID, refund_status=REFUNDED
"""
import uuid
from decimal import Decimal

from tests.conftest import make_authed_client


def _key() -> str:
    return uuid.uuid4().hex


def _fully_refunded_order(db, store, table, mgr, make_order, total="100.00"):
    order = make_order(store.id, table.id, Decimal(total), status="READY")
    pay = mgr.post(f"/cashier/orders/{order.id}/payments",
                   json={"payment_method": "CASH"},
                   headers={"Idempotency-Key": _key()})
    alloc = pay.json()["allocations"][0]["id"]
    mgr.post(f"/cashier/allocations/{alloc}/refunds",
             json={"amount": total, "reason": "tam iade"},
             headers={"Idempotency-Key": _key()})
    return order


def test_fully_refunded_is_distinct_from_never_paid(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))

    never_paid = make_order(store.id, table.id, Decimal("100.00"), status="READY")
    refunded = _fully_refunded_order(db, store, table, mgr, make_order)

    d_never = mgr.get(f"/cashier/orders/{never_paid.id}").json()
    d_refunded = mgr.get(f"/cashier/orders/{refunded.id}").json()

    # Both are net-zero / UNPAID …
    assert d_never["net_paid"] == "0.00"
    assert d_refunded["net_paid"] == "0.00"
    assert d_never["payment_status"] == "UNPAID"
    assert d_refunded["payment_status"] == "UNPAID"

    # … but the refund status makes them clearly different in bill & detail.
    assert d_never["refund_status"] == "NONE"
    assert d_refunded["refund_status"] == "REFUNDED"
    # Preparation status is untouched by the money lifecycle.
    assert d_refunded["preparation_status"] == "READY"


def test_bill_exposes_refund_status(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    refunded = _fully_refunded_order(db, store, table, mgr, make_order)

    bill = mgr.get(f"/cashier/tables/{table.id}/bill").json()
    line = next(o for o in bill["orders"] if o["order_id"] == refunded.id)
    assert line["refund_status"] == "REFUNDED"
    assert line["payment_status"] == "UNPAID"


def test_one_click_settle_refuses_refunded_order(db, make_store, make_table, make_staff, make_order):
    """
    Regression: the generic whole-table settle must NOT silently recollect a
    previously-refunded order. It returns a specific 409 telling the operator to
    use the explicit per-order flow.
    """
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    refunded = _fully_refunded_order(db, store, table, mgr, make_order)

    res = mgr.post(
        "/cashier/settlements",
        json={"table_id": table.id, "order_ids": [refunded.id], "payment_method": "CASH"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "refunded_recollect"


def test_explicit_per_order_recollection_allowed(db, make_store, make_table, make_staff, make_order):
    """The explicit, confirmed per-order endpoint DOES allow recollection."""
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    refunded = _fully_refunded_order(db, store, table, mgr, make_order)

    res = mgr.post(
        f"/cashier/orders/{refunded.id}/payments",
        json={"payment_method": "CASH"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text
    detail = mgr.get(f"/cashier/orders/{refunded.id}").json()
    assert detail["payment_status"] == "PAID"
    # Recollection is append-only: the earlier refund is never erased, so the
    # order still visibly carries refund history (the new payment now exceeds the
    # refunded total, so the derived status is PARTIALLY_REFUNDED, not NONE).
    assert detail["refund_status"] != "NONE"


def test_one_click_settle_still_works_for_normal_orders(db, make_store, make_table, make_staff, make_order):
    """A never-refunded order is unaffected by the recollection guard."""
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    o1 = make_order(store.id, table.id, Decimal("30.00"), status="READY")
    o2 = make_order(store.id, table.id, Decimal("70.00"), status="READY")
    res = mgr.post(
        "/cashier/settlements",
        json={"table_id": table.id, "order_ids": [o1.id, o2.id], "payment_method": "CASH"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text
    assert res.json()["gross_amount"] == "100.00"
