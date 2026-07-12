"""
Store-scoped inventory reconciliation.

The bug this file exists to prevent is not a crash. It is a report that says
everything is fine.

Suppose Kadıköy is 40 g of chocolate SHORT and Beşiktaş is 40 g OVER. Those are
two real, serious, opposite faults — one looks like theft, the other like a
miscount, and they need opposite responses. Summed into a single chain-wide
figure they are exactly zero, and a global reconciler cheerfully prints "OK"
while both branches are broken.

So every total is computed per (store, ingredient), every mismatch names its
store, and a mismatch in ANY store fails the whole run. The tests below construct
precisely that cancelling pair and insist both halves are still reported.

The reconciler must also never write. A reconciler that "repairs" drift by
overwriting the summary destroys the only evidence of whatever wrote stock
outside the inventory service.
"""
import importlib.util
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_ingredient,
    stock_for,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "reconcile_inventory.py"


@pytest.fixture(scope="module")
def reconciler():
    assert _SCRIPT.exists(), f"missing {_SCRIPT}"
    spec = importlib.util.spec_from_file_location("reconcile_inventory", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True, text=True, cwd=str(_REPO_ROOT), env=os.environ.copy(),
    )


def _opening(db, store_id: int, ing, qty: Decimal) -> None:
    """The opening receipt that makes a store's summary row add up to its ledger."""
    db.add(IngredientStockMovement(
        store_id=store_id,
        ingredient_id=ing.id,
        movement_type="PURCHASE_RECEIPT",
        quantity=qty,
        quantity_delta_on_hand=qty,
        quantity_delta_reserved=Decimal("0"),
        unit=ing.unit,
        reason="acilis bakiyesi",
        legacy_backfill=True,
    ))
    db.commit()


def _set_on_hand(db, store_id: int, ing_id: int, qty: str) -> None:
    """Corrupt one store's summary behind the service's back."""
    db.execute(
        text("UPDATE ingredient_stock SET on_hand_quantity = :q "
             "WHERE store_id = :s AND ingredient_id = :i"),
        {"q": Decimal(qty), "s": store_id, "i": ing_id},
    )
    db.commit()


@pytest.fixture()
def two_store_books(db, make_store):
    """One ingredient, stocked and cleanly ledgered in two stores."""
    store_b = make_store()
    ing, _ = make_ingredient(db, on_hand=Decimal("100.000"), store_id=DEFAULT_STORE_ID)
    stock_for(db, ing, store_b.id, on_hand=Decimal("100.000"))
    _opening(db, DEFAULT_STORE_ID, ing, Decimal("100.000"))
    _opening(db, store_b.id, ing, Decimal("100.000"))
    yield DEFAULT_STORE_ID, store_b.id, ing
    cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------

class TestStoreScopedReconciliation:

    def test_reconciliation_groups_by_store(self, db, reconciler, two_store_books):
        store_a, store_b, ing = two_store_books
        rows = reconciler.reconcile(ingredient_id=ing.id)

        # One row per (store, ingredient) — not one row per ingredient.
        by_store = {r["store_id"]: r for r in rows}
        assert set(by_store) == {store_a, store_b}
        assert all(r["mismatch"] is False for r in rows)
        # Every row names its store, so a mismatch is always actionable by a
        # specific branch manager.
        assert all(r["store_id"] and r["store_name"] for r in rows)

    def test_store_filtered_reconciliation_works(self, db, reconciler, two_store_books):
        store_a, store_b, ing = two_store_books

        assert [r["store_id"] for r in
                reconciler.reconcile(store_id=store_b, ingredient_id=ing.id)] == [store_b]
        assert [r["store_id"] for r in
                reconciler.reconcile(store_id=store_a, ingredient_id=ing.id)] == [store_a]

    def test_store_a_mismatch_does_not_cancel_out_store_b_mismatch(
        self, db, reconciler, two_store_books
    ):
        """
        The headline. Store A short by 40, store B over by 40 — the drifts sum to
        zero, so a chain-wide total would report a clean bill of health while both
        branches are wrong. Both must be reported, each against its own store.
        """
        store_a, store_b, ing = two_store_books
        _set_on_hand(db, store_a, ing.id, "60.000")    # ledger says 100 → short 40
        _set_on_hand(db, store_b, ing.id, "140.000")   # ledger says 100 → over  40

        rows = {r["store_id"]: r for r in reconciler.reconcile(ingredient_id=ing.id)}

        assert rows[store_a]["mismatch"] is True
        assert rows[store_b]["mismatch"] is True
        assert Decimal(rows[store_a]["on_hand_mismatch_amount"]) == Decimal("-40.000")
        assert Decimal(rows[store_b]["on_hand_mismatch_amount"]) == Decimal("40.000")

        # The two drifts really do sum to zero. That is exactly why they must
        # never be summed — and why this reconciler never was tempted to.
        assert sum(
            Decimal(r["on_hand_mismatch_amount"]) for r in rows.values()
        ) == Decimal("0.000")

    def test_clean_store_is_not_tainted_by_a_dirty_one(
        self, db, reconciler, two_store_books
    ):
        store_a, store_b, ing = two_store_books
        _set_on_hand(db, store_b, ing.id, "10.000")

        rows = {r["store_id"]: r for r in reconciler.reconcile(ingredient_id=ing.id)}
        assert rows[store_a]["mismatch"] is False
        assert rows[store_b]["mismatch"] is True

    def test_reserved_drift_is_also_store_scoped(
        self, db, reconciler, two_store_books
    ):
        """Reserved is reconciled against that store's order lines, not everyone's."""
        store_a, store_b, ing = two_store_books
        db.execute(
            text("UPDATE ingredient_stock SET reserved_quantity = 15 "
                 "WHERE store_id = :s AND ingredient_id = :i"),
            {"s": store_b, "i": ing.id},
        )
        db.commit()

        rows = {r["store_id"]: r for r in reconciler.reconcile(ingredient_id=ing.id)}
        assert rows[store_b]["reserved_mismatch"] is True
        assert rows[store_a]["reserved_mismatch"] is False


class TestStoreScopedReconcilerCli:

    def test_cli_returns_nonzero_when_any_store_mismatches(self, db, two_store_books):
        """A mismatch in ONE branch must fail the whole run — never be averaged away."""
        store_a, store_b, ing = two_store_books

        clean = _cli("--ingredient", str(ing.id), "--json")
        assert clean.returncode == 0, clean.stdout + clean.stderr

        _set_on_hand(db, store_b, ing.id, "7.000")     # break store B only

        dirty = _cli("--ingredient", str(ing.id), "--json")
        assert dirty.returncode == 1, dirty.stdout + dirty.stderr
        assert f'"store_id": {store_b}' in dirty.stdout

        # ...and a run scoped to the healthy branch still passes, so an operator
        # can isolate which branch is actually broken instead of guessing.
        scoped_a = _cli("--store-id", str(store_a), "--ingredient", str(ing.id), "--json")
        assert scoped_a.returncode == 0, scoped_a.stdout + scoped_a.stderr

    def test_cli_never_mutates_any_stores_inventory(self, db, two_store_books):
        store_a, store_b, ing = two_store_books
        _set_on_hand(db, store_b, ing.id, "33.000")

        movements_before = db.query(IngredientStockMovement).filter_by(
            ingredient_id=ing.id
        ).count()

        proc = _cli("--json")
        assert proc.returncode == 1

        db.expire_all()
        rows = {
            s.store_id: s
            for s in db.query(IngredientStock).filter_by(ingredient_id=ing.id).all()
        }
        # The drift is REPORTED, not repaired. Repairing it would erase the only
        # evidence of whatever wrote stock outside the inventory service.
        assert rows[store_b].on_hand_quantity == Decimal("33.000")
        assert rows[store_a].on_hand_quantity == Decimal("100.000")
        assert db.query(IngredientStockMovement).filter_by(
            ingredient_id=ing.id
        ).count() == movements_before, "reconciliation must not append movements"
