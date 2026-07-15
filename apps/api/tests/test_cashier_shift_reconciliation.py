"""
Cashier shift reconciliation — the closed snapshot must equal a fresh ledger
re-derivation of its own window.

The failure this guards against is not a crash: it is a shift that reports a tidy
"Denk" while its snapshot has been tampered with in the database. The shift
trigger makes that unrepresentable through the app, so these tests corrupt the
snapshot with direct SQL (ownership-gated) and insist the reconciler CATCHES it —
and, just as importantly, that it never WRITES.
"""
import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.models.cashier_shift import CashierShift
from tests.conftest import _shift_maintenance, make_authed_client

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "reconcile_payments.py"


@pytest.fixture(scope="module")
def reconciler():
    assert _SCRIPT.exists(), f"missing {_SCRIPT}"
    spec = importlib.util.spec_from_file_location("reconcile_payments", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _key() -> str:
    return uuid.uuid4().hex


def _closed_shift(db, make_store, make_table, make_staff, make_order):
    """A store with one cashier who opened, collected cash+card, and closed."""
    store = make_store()
    table = make_table(store.id)
    cashier = make_staff("CASHIER", store_id=store.id)
    cclient = make_authed_client(db, cashier)

    opened = cclient.post(
        "/cashier/shifts/open",
        json={"opening_cash_amount": "100.00"},
        headers={"Idempotency-Key": _key()},
    )
    shift_id = opened.json()["id"]

    o1 = make_order(store.id, table.id, Decimal("60.00"))
    o2 = make_order(store.id, table.id, Decimal("40.00"))
    cclient.post(f"/cashier/orders/{o1.id}/payments",
                 json={"payment_method": "CASH"}, headers={"Idempotency-Key": _key()})
    cclient.post(f"/cashier/orders/{o2.id}/payments",
                 json={"payment_method": "CARD"}, headers={"Idempotency-Key": _key()})

    closed = cclient.post(
        f"/cashier/shifts/{shift_id}/close",
        json={"counted_closing_cash_amount": "160.00"},
        headers={"Idempotency-Key": _key()},
    )
    assert closed.status_code == 200, closed.text
    return store, shift_id


def test_valid_closed_shift_reconciles(db, reconciler, make_store, make_table, make_staff, make_order):
    store, _sid = _closed_shift(db, make_store, make_table, make_staff, make_order)
    assert reconciler.reconcile_shifts(store.id) == []


def test_reconciliation_detects_corrupted_expected_cash(
    db, reconciler, make_store, make_table, make_staff, make_order
):
    store, shift_id = _closed_shift(db, make_store, make_table, make_staff, make_order)
    # Tamper with the frozen snapshot directly (ownership-gated trigger bypass).
    with _shift_maintenance(db):
        db.execute(
            text("UPDATE cashier_shifts SET expected_closing_cash_amount = 999 WHERE id=:id"),
            {"id": shift_id},
        )
    db.commit()

    mism = reconciler.reconcile_shifts(store.id)
    assert len(mism) == 1
    assert "expected_closing_cash" in mism[0]["fields"]


def test_reconciliation_detects_corrupted_discrepancy(
    db, reconciler, make_store, make_table, make_staff, make_order
):
    store, shift_id = _closed_shift(db, make_store, make_table, make_staff, make_order)
    with _shift_maintenance(db):
        db.execute(
            text("UPDATE cashier_shifts SET cash_discrepancy_amount = -50 WHERE id=:id"),
            {"id": shift_id},
        )
    db.commit()

    mism = reconciler.reconcile_shifts(store.id)
    assert len(mism) == 1
    assert "cash_discrepancy" in mism[0]["fields"]


def test_reconciliation_does_not_mutate(
    db, reconciler, make_store, make_table, make_staff, make_order
):
    store, shift_id = _closed_shift(db, make_store, make_table, make_staff, make_order)
    before = db.get(CashierShift, shift_id)
    db.refresh(before)
    snap = {
        "expected": before.expected_closing_cash_amount,
        "discrepancy": before.cash_discrepancy_amount,
        "cash_pay": before.cash_payments_amount,
    }
    reconciler.reconcile_shifts(store.id)
    after = db.get(CashierShift, shift_id)
    db.refresh(after)
    assert after.expected_closing_cash_amount == snap["expected"]
    assert after.cash_discrepancy_amount == snap["discrepancy"]
    assert after.cash_payments_amount == snap["cash_pay"]
