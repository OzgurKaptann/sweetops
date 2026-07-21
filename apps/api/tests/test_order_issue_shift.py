"""
Order issue refunds and the cashier shift snapshot.

A refund created by resolving an issue is an ordinary refund row in the ledger, so:
  * a refund taken inside an OPEN shift window is reflected by the existing close
    attribution rule (refunds of the cashier's own collected money in the window),
  * a CLOSED shift's frozen snapshot is unaffected by a refund taken after the close.

The issue workflow adds no new shift logic; it just lands money in the ledger. The
window here is derived from the DB clock (not the host clock the close endpoint's
``closed_at`` uses) so the attribution assertion is deterministic rather than racing
the sub-millisecond gap between a payment and an immediate close.
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.conftest import make_authed_client
from app.models.cashier_shift import CashierShift
from app.services import cashier_shift_service


def _key() -> str:
    return uuid.uuid4().hex


@pytest.fixture()
def env(db, make_store, make_table, make_staff, make_order):
    class Env:
        pass
    e = Env()
    e.db = db
    e.store = make_store()
    e.table = make_table(e.store.id)
    e.cashier = make_staff("CASHIER", store_id=e.store.id)
    e.manager = make_staff("MANAGER", store_id=e.store.id)
    e.cashier_client = make_authed_client(db, e.cashier)
    e.manager_client = make_authed_client(db, e.manager)
    e.order = make_order(e.store.id, e.table.id, Decimal("100.00"))
    return e


def _open_shift(client, opening="0.00"):
    r = client.post("/cashier/shifts/open", json={"opening_cash_amount": opening},
                    headers={"Idempotency-Key": _key()})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _pay_cash(client, order_id):
    r = client.post(f"/cashier/orders/{order_id}/payments", json={"payment_method": "CASH"},
                    headers={"Idempotency-Key": _key()})
    assert r.status_code == 200, r.text


def _full_refund_via_issue(client, order_id):
    issue = client.post(f"/orders/{order_id}/issues",
                        json={"issue_type": "CUSTOMER_CANCELLED", "reason": "iade"},
                        headers={"Idempotency-Key": _key()})
    assert issue.status_code == 200, issue.text
    issue_id = issue.json()["id"]
    res = client.post(f"/order-issues/{issue_id}/resolve",
                      json={"resolution_type": "FULL_REFUND", "reason": "tam iade"},
                      headers={"Idempotency-Key": _key()})
    assert res.status_code == 200, res.text


def test_refund_inside_open_shift_appears_in_close_totals(env):
    shift_id = _open_shift(env.cashier_client, opening="0.00")
    opened_at = env.db.get(CashierShift, shift_id).opened_at
    _pay_cash(env.cashier_client, env.order.id)              # cashier collects 100 cash
    _full_refund_via_issue(env.manager_client, env.order.id)  # manager refunds it inside window

    # Re-derive the window totals with the EXACT rule the close uses, over a window
    # bounded by the DB clock so the assertion is deterministic. This is precisely
    # cashier_shift_service.close_shift's snapshot computation. clock_timestamp()
    # (not now(), which is the test session's transaction-start time) gives a real
    # "after the refund" upper bound.
    env.db.rollback()  # fresh snapshot so the just-committed ledger rows are visible
    closed_at = env.db.execute(text("SELECT clock_timestamp()")).scalar()
    totals = cashier_shift_service.compute_shift_totals(
        env.db, store_id=env.store.id, cashier_user_id=env.cashier.id,
        opened_at=opened_at, closed_at=closed_at,
    )
    assert totals["cash_payments_amount"] == Decimal("100.00")
    # Refund of THIS cashier's cash money, in the window → reflected in the totals.
    assert totals["cash_refunds_amount"] == Decimal("100.00")
    assert totals["total_refunds_amount"] == Decimal("100.00")
    assert totals["net_collected_amount"] == Decimal("0.00")


def test_closed_shift_snapshot_frozen_after_issue_refund(env):
    shift_id = _open_shift(env.cashier_client, opening="0.00")
    _pay_cash(env.cashier_client, env.order.id)
    snap_before = env.cashier_client.post(
        f"/cashier/shifts/{shift_id}/close",
        json={"counted_closing_cash_amount": "100.00"},
        headers={"Idempotency-Key": _key()},
    ).json()
    assert snap_before["cash_refunds_amount"] == "0.00"  # no refund yet at close

    # A refund taken AFTER the close cannot retroactively change the frozen snapshot.
    _full_refund_via_issue(env.manager_client, env.order.id)

    env.db.expire_all()
    row = env.db.get(CashierShift, shift_id)
    # Every snapshot field is exactly what it was at close — the trigger froze it.
    assert str(row.cash_refunds_amount) == snap_before["cash_refunds_amount"]
    assert str(row.total_refunds_amount) == snap_before["total_refunds_amount"]
    assert str(row.cash_payments_amount) == snap_before["cash_payments_amount"]
    assert str(row.net_collected_amount) == snap_before["net_collected_amount"]
    # The order itself really was refunded — the freeze is the snapshot, not the ledger.
    detail = env.manager_client.get(f"/cashier/orders/{env.order.id}").json()
    assert detail["refunded_amount"] == "100.00"
