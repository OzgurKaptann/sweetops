"""
Tests for scripts/seed_demo_data.py — the deterministic demo seed.

The demo seed is a dev/demo affordance, but it must be SAFE: idempotent, strictly
demo-scoped, and never destructive to non-demo data. These tests run the real
script against the test database (the same PostgreSQL every other test uses), then
assert the properties the branch promises:

  * it runs, and a second run duplicates nothing,
  * store 1 and any other non-demo store/user are untouched,
  * every major read surface (owner dashboard, kitchen timing, inventory
    thresholds, order issues, cashier shifts) becomes meaningful,
  * all four reconcilers still pass for the demo store,
  * and it fails with a clear message when the schema is not migrated.

Teardown removes the demo stores' rows in FK-safe order (disabling the append-only
immutability triggers exactly as conftest's own teardown does), so the demo data
this module creates does not leak into other tests.
"""
import importlib.util
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.cashier_shift import SHIFT_CLOSED, SHIFT_OPEN, CashierShift
from app.models.ingredient_stock import IngredientStock
from app.models.order_issue import ISSUE_STATUS_OPEN, ISSUE_STATUS_RESOLVED, OrderIssue
from app.models.store import Store
from app.services import inventory_service
from app.services.kitchen_timing_service import get_active_order_timing, get_timing_summary
from app.services.operational_dashboard_service import fetch_operational_dashboard

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed_demo_data.py"
PRIMARY_STORE_NAME = "SweetOps Demo - Kadıköy"
SECONDARY_STORE_NAME = "SweetOps Demo - Moda"


# ── Load the seed module (for direct unit tests of its helpers) ───────────────
def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_demo_data", SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the module's dataclasses (which use string
    # annotations via `from __future__ import annotations`) can resolve them.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── FK-safe teardown of the demo stores (dev-scoped, trigger-gated) ───────────
_TRIGGERS = [
    ("payment_refunds", "trg_payment_refunds_immutable"),
    ("payment_allocations", "trg_payment_allocations_immutable"),
    ("payment_settlements", "trg_payment_settlements_immutable"),
    ("ingredient_stock_movements", "trg_ingredient_stock_movements_immutable"),
    ("inventory_stock_counts", "trg_inventory_stock_counts_immutable"),
    ("inventory_threshold_updates", "trg_inventory_threshold_updates_immutable"),
    ("order_issues", "trg_order_issues_guard"),
    ("cashier_shifts", "trg_cashier_shifts_guard"),
]


@contextmanager
def _triggers_off(db: Session):
    for tbl, trig in _TRIGGERS:
        db.execute(text(f"ALTER TABLE {tbl} DISABLE TRIGGER {trig}"))
    try:
        yield
    finally:
        for tbl, trig in _TRIGGERS:
            db.execute(text(f"ALTER TABLE {tbl} ENABLE TRIGGER {trig}"))


def _wipe_demo_stores(db: Session) -> None:
    sids = [
        r.id for r in db.query(Store.id)
        .filter(Store.name.in_([PRIMARY_STORE_NAME, SECONDARY_STORE_NAME]))
        .all()
    ]
    if not sids:
        return
    sp = tuple(sids)
    oids = tuple(
        r.id for r in db.execute(
            text("SELECT id FROM orders WHERE store_id IN :s"), {"s": sp}
        ).fetchall()
    )
    tids = tuple(
        r.id for r in db.execute(
            text("SELECT id FROM inventory_transfers "
                 "WHERE source_store_id IN :s OR destination_store_id IN :s"),
            {"s": sp},
        ).fetchall()
    )
    with _triggers_off(db):
        db.execute(text("UPDATE payment_refunds SET order_issue_id=NULL WHERE store_id IN :s"), {"s": sp})
        db.execute(text("DELETE FROM order_issues WHERE store_id IN :s"), {"s": sp})
        db.execute(text("DELETE FROM payment_refunds WHERE store_id IN :s"), {"s": sp})
        db.execute(text("DELETE FROM payment_allocations WHERE settlement_id IN "
                        "(SELECT id FROM payment_settlements WHERE store_id IN :s)"), {"s": sp})
        db.execute(text("DELETE FROM payment_settlements WHERE store_id IN :s"), {"s": sp})
        db.execute(text("DELETE FROM cashier_shifts WHERE store_id IN :s"), {"s": sp})
        db.execute(text("DELETE FROM ingredient_stock_movements WHERE store_id IN :s"), {"s": sp})
        db.execute(text("DELETE FROM inventory_stock_counts WHERE store_id IN :s"), {"s": sp})
        db.execute(text("DELETE FROM inventory_threshold_updates WHERE store_id IN :s"), {"s": sp})
        if tids:
            db.execute(text("DELETE FROM inventory_transfers WHERE id IN :t"), {"t": tids})
    db.execute(text("DELETE FROM order_inventory_lines WHERE store_id IN :s"), {"s": sp})
    if oids:
        db.execute(text("DELETE FROM order_item_ingredients WHERE order_item_id IN "
                        "(SELECT id FROM order_items WHERE order_id IN :o)"), {"o": oids})
        db.execute(text("DELETE FROM order_items WHERE order_id IN :o"), {"o": oids})
        db.execute(text("DELETE FROM order_status_events WHERE order_id IN :o"), {"o": oids})
        db.execute(text("DELETE FROM orders WHERE id IN :o"), {"o": oids})
    db.execute(text("UPDATE ingredient_stock SET threshold_updated_by_user_id=NULL, "
                    "threshold_updated_at=NULL WHERE store_id IN :s"), {"s": sp})
    db.execute(text("DELETE FROM ingredient_stock WHERE store_id IN :s"), {"s": sp})
    db.execute(text("DELETE FROM auth_sessions WHERE user_id IN "
                    "(SELECT id FROM users WHERE store_id IN :s)"), {"s": sp})
    db.execute(text("DELETE FROM users WHERE store_id IN :s"), {"s": sp})
    db.execute(text("DELETE FROM tables WHERE store_id IN :s"), {"s": sp})
    db.execute(text("DELETE FROM stores WHERE id IN :s"), {"s": sp})
    db.commit()


def _run_seed() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SEED_SCRIPT)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )


def _demo_store_id(db: Session) -> int:
    return db.query(Store.id).filter(Store.name == PRIMARY_STORE_NAME).scalar()


def _counts(db: Session, sids: tuple[int, ...]) -> dict[str, int]:
    def c(sql: str) -> int:
        return db.execute(text(sql), {"s": sids}).scalar()
    return {
        "stores": db.execute(text("SELECT count(*) FROM stores WHERE id IN :s"), {"s": sids}).scalar(),
        "users": c("SELECT count(*) FROM users WHERE store_id IN :s"),
        "tables": c("SELECT count(*) FROM tables WHERE store_id IN :s"),
        "orders": c("SELECT count(*) FROM orders WHERE store_id IN :s"),
        "settlements": c("SELECT count(*) FROM payment_settlements WHERE store_id IN :s"),
        "refunds": c("SELECT count(*) FROM payment_refunds WHERE store_id IN :s"),
        "shifts": c("SELECT count(*) FROM cashier_shifts WHERE store_id IN :s"),
        "issues": c("SELECT count(*) FROM order_issues WHERE store_id IN :s"),
        "movements": c("SELECT count(*) FROM ingredient_stock_movements WHERE store_id IN :s"),
        "stock_counts": c("SELECT count(*) FROM inventory_stock_counts WHERE store_id IN :s"),
    }


# ── Module fixture: seed twice, expose baseline, tear down demo data ──────────
@pytest.fixture(scope="module")
def seeded():
    db = SessionLocal()
    try:
        _wipe_demo_stores(db)  # start from a clean demo namespace
        # Baseline snapshot of a non-demo store (store 1) to prove it is untouched.
        base_store = db.query(Store).order_by(Store.id).first()
        baseline = {
            "store_id": base_store.id,
            "store_name": base_store.name,
            "users": db.execute(text("SELECT count(*) FROM users WHERE store_id = :s"),
                                {"s": base_store.id}).scalar(),
            "orders": db.execute(text("SELECT count(*) FROM orders WHERE store_id = :s"),
                                 {"s": base_store.id}).scalar(),
        }

        first = _run_seed()
        assert first.returncode == 0, f"first seed run failed:\n{first.stdout}\n{first.stderr}"
        sids = tuple(
            r.id for r in db.query(Store.id)
            .filter(Store.name.in_([PRIMARY_STORE_NAME, SECONDARY_STORE_NAME])).all()
        )
        after_first = _counts(db, sids)

        second = _run_seed()
        assert second.returncode == 0, f"second seed run failed:\n{second.stdout}\n{second.stderr}"
        after_second = _counts(db, sids)

        yield {
            "baseline": baseline,
            "sids": sids,
            "after_first": after_first,
            "after_second": after_second,
            "db": db,
        }
    finally:
        _wipe_demo_stores(db)
        db.close()


# ── Safety / idempotency ──────────────────────────────────────────────────────
def test_seed_runs_and_creates_two_demo_stores(seeded):
    assert len(seeded["sids"]) == 2


def test_second_run_duplicates_nothing(seeded):
    first, second = seeded["after_first"], seeded["after_second"]
    assert first == second, f"seed is not idempotent: {first} != {second}"


@pytest.mark.parametrize("kind,minimum", [
    ("stores", 2), ("users", 6), ("tables", 5), ("orders", 15),
    ("settlements", 1), ("refunds", 1), ("shifts", 3), ("issues", 4),
])
def test_expected_demo_rows_present(seeded, kind, minimum):
    assert seeded["after_second"][kind] >= minimum


def test_non_demo_store_preserved(seeded):
    db, base = seeded["db"], seeded["baseline"]
    store = db.get(Store, base["store_id"])
    assert store is not None and store.name == base["store_name"]
    users_now = db.execute(text("SELECT count(*) FROM users WHERE store_id = :s"),
                           {"s": base["store_id"]}).scalar()
    orders_now = db.execute(text("SELECT count(*) FROM orders WHERE store_id = :s"),
                            {"s": base["store_id"]}).scalar()
    assert users_now == base["users"], "seed changed a non-demo store's user count"
    assert orders_now == base["orders"], "seed changed a non-demo store's order count"


# ── Meaningful read surfaces ──────────────────────────────────────────────────
def test_owner_dashboard_is_meaningful(seeded):
    db = seeded["db"]
    d = fetch_operational_dashboard(db, _demo_store_id(db))
    assert d.orders.active_count > 0 and d.orders.completed_today > 0
    assert d.payments.gross_collected_today > 0
    assert d.payments.refunds_today > 0
    assert d.issues.open_count >= 1 and d.issues.resolved_today >= 1
    assert d.shifts.open_shift_count >= 1
    assert d.shifts.shifts_with_discrepancy_today >= 1
    assert d.inventory.out_of_stock_count >= 1
    assert d.inventory.critical_count >= 1
    assert d.inventory.low_count >= 1
    # The attention list surfaces every operational condition we seeded.
    codes = {a.code for a in d.attention}
    assert {"OUT_OF_STOCK", "CRITICAL_STOCK", "DELAYED_KITCHEN",
            "OPEN_ISSUES", "SHIFT_DISCREPANCY", "OPEN_SHIFTS"} <= codes


def test_kitchen_timing_is_meaningful(seeded):
    db = seeded["db"]
    sid = _demo_store_id(db)
    active = get_active_order_timing(db, sid)
    assert active["summary"]["active_orders"] > 0
    assert active["summary"]["delayed_orders"] > 0
    summary = get_timing_summary(db, sid)
    assert summary["completed_orders_today"] > 0
    assert summary["average_prep_seconds_today"] is not None
    assert summary["average_time_to_ready_seconds_today"] is not None


def test_inventory_threshold_states(seeded):
    db = seeded["db"]
    sid = _demo_store_id(db)
    states = {
        inventory_service.threshold_status(s)
        for s in db.query(IngredientStock).filter(IngredientStock.store_id == sid).all()
    }
    assert "OUT_OF_STOCK" in states
    assert "CRITICAL" in states
    assert "LOW" in states
    assert "HEALTHY" in states
    assert "NOT_CONFIGURED" in states


def test_order_issues_open_and_resolved(seeded):
    db = seeded["db"]
    sid = _demo_store_id(db)
    statuses = [
        r.status for r in db.query(OrderIssue.status)
        .filter(OrderIssue.store_id == sid).all()
    ]
    assert ISSUE_STATUS_OPEN in statuses
    assert ISSUE_STATUS_RESOLVED in statuses


def test_cashier_shifts_open_and_closed(seeded):
    db = seeded["db"]
    sid = _demo_store_id(db)
    shifts = db.query(CashierShift).filter(CashierShift.store_id == sid).all()
    statuses = [s.status for s in shifts]
    assert statuses.count(SHIFT_OPEN) >= 1
    assert statuses.count(SHIFT_CLOSED) >= 2
    # At least one closed shift carries a non-zero cash discrepancy.
    assert any(
        s.status == SHIFT_CLOSED and s.cash_discrepancy_amount not in (None, 0)
        for s in shifts
    )


# ── Reconcilers still pass with demo data present ─────────────────────────────
@pytest.mark.parametrize("script", [
    "reconcile_payments.py", "reconcile_inventory.py",
    "reconcile_order_issues.py", "reconcile_kitchen_timing.py",
])
def test_reconcilers_pass_for_demo_store(seeded, script):
    sid = _demo_store_id(seeded["db"])
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), "--store", str(sid)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{script} reported a mismatch:\n{result.stdout}"


# ── Fails clearly when the schema is not migrated ─────────────────────────────
def test_assert_migrated_raises_clear_error():
    """_assert_migrated must abort with an actionable message on an unmigrated DB."""
    from sqlalchemy.exc import ProgrammingError

    seed = _load_seed_module()

    class _FakeDB:
        def execute(self, *_a, **_k):
            raise ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))

        def rollback(self):
            pass

    with pytest.raises(SystemExit) as exc:
        seed._assert_migrated(_FakeDB())
    assert "not migrated" in str(exc.value)
    assert "alembic upgrade head" in str(exc.value)
