"""
Cashier shift — database-level guarantees: the CHECK constraints, the
one-open-shift index, the immutability trigger, and migration reversibility.

These bypass the service and talk to the table directly, because the point is
that the DATABASE refuses a bad shift even if the application is wrong.
"""
import importlib.util
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.cashier_shift import CashierShift
from tests.conftest import _shift_maintenance

API_DIR = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = (
    API_DIR / "alembic" / "versions" / "d5c7b3a91e40_cashier_shift_closing.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_shift_migration", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _open_row(db, store_id, cashier_id, **over):
    row = CashierShift(
        store_id=store_id,
        cashier_user_id=cashier_id,
        status="OPEN",
        opening_cash_amount=Decimal("100.00"),
        opened_idempotency_key_hash="a" * 64,
        opened_request_hash="b" * 64,
    )
    for k, v in over.items():
        setattr(row, k, v)
    db.add(row)
    db.flush()
    return row


# ── CHECK constraints ─────────────────────────────────────────────────────────

def test_negative_opening_cash_rejected(db, make_store, make_staff):
    store = make_store()
    cashier = make_staff("CASHIER", store_id=store.id)
    with pytest.raises(IntegrityError):
        _open_row(db, store.id, cashier.id, opening_cash_amount=Decimal("-1.00"))
    db.rollback()


def test_status_domain_enforced(db, make_store, make_staff):
    store = make_store()
    cashier = make_staff("CASHIER", store_id=store.id)
    with pytest.raises(IntegrityError):
        _open_row(db, store.id, cashier.id, status="PAUSED")
    db.rollback()


def test_open_shift_cannot_carry_close_fields(db, make_store, make_staff):
    """The status⟺snapshot consistency CHECK: an OPEN row with a counted amount."""
    store = make_store()
    cashier = make_staff("CASHIER", store_id=store.id)
    with pytest.raises(IntegrityError):
        _open_row(
            db, store.id, cashier.id,
            counted_closing_cash_amount=Decimal("10.00"),  # illegal while OPEN
        )
    db.rollback()


def test_closed_shift_must_have_snapshot(db, make_store, make_staff):
    """A CLOSED row missing its snapshot columns is unrepresentable."""
    store = make_store()
    cashier = make_staff("CASHIER", store_id=store.id)
    with pytest.raises(IntegrityError):
        _open_row(db, store.id, cashier.id, status="CLOSED")  # no closed_at etc.
    db.rollback()


def test_cashier_must_belong_to_store(db, make_store, make_staff):
    store = make_store()
    other = make_store()
    cashier_other = make_staff("CASHIER", store_id=other.id)
    # A cashier of `other` cannot open a shift attributed to `store`.
    with pytest.raises(IntegrityError):
        _open_row(db, store.id, cashier_other.id)
    db.rollback()


def test_one_open_shift_per_store_cashier(db, make_store, make_staff):
    store = make_store()
    cashier = make_staff("CASHIER", store_id=store.id)
    _open_row(db, store.id, cashier.id, opened_idempotency_key_hash="c" * 64)
    db.commit()
    with pytest.raises(IntegrityError):
        _open_row(db, store.id, cashier.id, opened_idempotency_key_hash="d" * 64)
    db.rollback()
    with _shift_maintenance(db):
        db.query(CashierShift).filter(CashierShift.store_id == store.id).delete()
    db.commit()


def test_open_idempotency_uniqueness_store_scoped(db, make_store, make_staff):
    store = make_store()
    c1 = make_staff("CASHIER", store_id=store.id)
    _open_row(db, store.id, c1.id, opened_idempotency_key_hash="e" * 64)
    db.commit()
    # Same store + same opening key on a second (would-be) shift → rejected.
    c2 = make_staff("CASHIER", store_id=store.id)
    with pytest.raises(IntegrityError):
        _open_row(db, store.id, c2.id, opened_idempotency_key_hash="e" * 64)
    db.rollback()
    with _shift_maintenance(db):
        db.query(CashierShift).filter(CashierShift.store_id == store.id).delete()
    db.commit()


# ── Immutability trigger ──────────────────────────────────────────────────────

def _close_via_sql(db, shift_id):
    db.execute(
        text(
            "UPDATE cashier_shifts SET status='CLOSED', closed_at=now(), "
            "counted_closing_cash_amount=0, cash_payments_amount=0, cash_refunds_amount=0, "
            "expected_closing_cash_amount=0, cash_discrepancy_amount=0, card_payments_amount=0, "
            "card_refunds_amount=0, gross_payments_amount=0, total_refunds_amount=0, "
            "net_collected_amount=0, closed_idempotency_key_hash=:k, closed_request_hash=:r "
            "WHERE id=:id"
        ),
        {"k": "f" * 64, "r": "0" * 64, "id": shift_id},
    )


def test_closed_shift_is_immutable(db, make_store, make_staff):
    store = make_store()
    cashier = make_staff("CASHIER", store_id=store.id)
    row = _open_row(db, store.id, cashier.id, opened_idempotency_key_hash="1" * 64)
    db.commit()
    _close_via_sql(db, row.id)
    db.commit()
    # Any UPDATE to a CLOSED shift is refused by the trigger.
    with pytest.raises(IntegrityError):
        db.execute(
            text("UPDATE cashier_shifts SET close_note='changed' WHERE id=:id"),
            {"id": row.id},
        )
        db.flush()
    db.rollback()
    with _shift_maintenance(db):
        db.query(CashierShift).filter(CashierShift.id == row.id).delete()
    db.commit()


def test_closed_shift_cannot_be_reopened(db, make_store, make_staff):
    store = make_store()
    cashier = make_staff("CASHIER", store_id=store.id)
    row = _open_row(db, store.id, cashier.id, opened_idempotency_key_hash="2" * 64)
    db.commit()
    _close_via_sql(db, row.id)
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text("UPDATE cashier_shifts SET status='OPEN' WHERE id=:id"),
            {"id": row.id},
        )
        db.flush()
    db.rollback()
    with _shift_maintenance(db):
        db.query(CashierShift).filter(CashierShift.id == row.id).delete()
    db.commit()


def test_open_snapshot_is_immutable(db, make_store, make_staff):
    """While OPEN, the opening cash figure cannot be edited."""
    store = make_store()
    cashier = make_staff("CASHIER", store_id=store.id)
    row = _open_row(db, store.id, cashier.id, opened_idempotency_key_hash="3" * 64)
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text("UPDATE cashier_shifts SET opening_cash_amount=999 WHERE id=:id"),
            {"id": row.id},
        )
        db.flush()
    db.rollback()
    with _shift_maintenance(db):
        db.query(CashierShift).filter(CashierShift.id == row.id).delete()
    db.commit()


def test_shift_delete_refused_without_ownership_bypass(db, make_store, make_staff):
    store = make_store()
    cashier = make_staff("CASHIER", store_id=store.id)
    row = _open_row(db, store.id, cashier.id, opened_idempotency_key_hash="4" * 64)
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(text("DELETE FROM cashier_shifts WHERE id=:id"), {"id": row.id})
        db.flush()
    db.rollback()
    with _shift_maintenance(db):
        db.query(CashierShift).filter(CashierShift.id == row.id).delete()
    db.commit()


# ── Migration ─────────────────────────────────────────────────────────────────

def test_alembic_single_head():
    out = subprocess.run(
        ["python", "-m", "alembic", "heads"],
        cwd=API_DIR, capture_output=True, text=True,
    )
    heads = [l for l in out.stdout.splitlines() if l.strip() and "head" in l]
    assert len(heads) == 1, out.stdout


def test_downgrade_refuses_while_shifts_exist(db, make_store, make_staff):
    """
    A downgrade that would destroy a real reconciliation must abort. We prove the
    guard clause fires while a shift exists — without actually downgrading the live
    test database (that would drop the table the rest of the suite needs).
    """
    migration = _load_migration()
    store = make_store()
    cashier = make_staff("CASHIER", store_id=store.id)
    row = _open_row(db, store.id, cashier.id, opened_idempotency_key_hash="5" * 64)
    db.commit()

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(db.connection())
    with Operations.context(ctx):
        with pytest.raises(migration.CashierShiftsExist):
            migration.downgrade()
    db.rollback()

    with _shift_maintenance(db):
        db.query(CashierShift).filter(CashierShift.id == row.id).delete()
    db.commit()
