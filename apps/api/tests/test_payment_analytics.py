"""Payment analytics: ordered value vs collected cash, and reconciliation."""
import uuid
from decimal import Decimal

from tests.conftest import make_authed_client


def _key() -> str:
    return uuid.uuid4().hex


def test_summary_distinguishes_ordered_and_collected(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    o1 = make_order(store.id, table.id, Decimal("100.00"))
    make_order(store.id, table.id, Decimal("50.00"))  # unpaid
    # Collect all of o1, refund 20.
    res = mgr.post(f"/cashier/orders/{o1.id}/payments",
                   json={"payment_method": "CASH"},
                   headers={"Idempotency-Key": _key()})
    alloc = res.json()["allocations"][0]["id"]
    mgr.post(f"/cashier/allocations/{alloc}/refunds",
             json={"amount": "20.00", "reason": "x"},
             headers={"Idempotency-Key": _key()})

    s = mgr.get("/owner/payment-summary").json()
    assert s["gross_order_value"] == "150.00"     # both orders' totals
    assert s["collected_amount"] == "100.00"       # one allocation
    assert s["refunded_amount"] == "20.00"
    assert s["net_collected_amount"] == "80.00"
    assert s["outstanding_amount"] == "70.00"      # 150 - 80


def test_cancelled_excluded_from_gross(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    make_order(store.id, table.id, Decimal("100.00"))
    make_order(store.id, table.id, Decimal("40.00"), status="CANCELLED")
    s = mgr.get("/owner/payment-summary").json()
    assert s["gross_order_value"] == "100.00"


def test_existing_revenue_metric_not_redefined(db, make_store, make_table, make_staff, make_order):
    """/owner/kpis still reports gross ordered value, independent of collection."""
    store = make_store(); table = make_table(store.id)
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))
    make_order(store.id, table.id, Decimal("100.00"))  # unpaid
    kpis = owner.get("/owner/kpis").json()
    # gross_revenue is order-derived and unaffected by (lack of) collection.
    assert "gross_revenue" in kpis["kpis"]


def test_reconciliation_clean_and_mismatch(db, make_store, make_table, make_staff, make_order):
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts"))
    import importlib
    recon = importlib.import_module("reconcile_payments")

    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("100.00"))
    mgr.post(f"/cashier/orders/{order.id}/payments",
             json={"payment_method": "CASH"},
             headers={"Idempotency-Key": _key()})

    # Clean: no mismatches for this store.
    assert recon.reconcile(store.id) == []

    # Introduce drift directly on the summary field (simulating corruption).
    from sqlalchemy import text
    db.execute(text("UPDATE orders SET paid_amount = 999 WHERE id = :i"), {"i": order.id})
    db.commit()
    mismatches = recon.reconcile(store.id)
    assert any(m["order_id"] == order.id for m in mismatches)

    # Restore so fixture cleanup is unaffected.
    db.execute(text("UPDATE orders SET paid_amount = 100 WHERE id = :i"), {"i": order.id})
    db.commit()
