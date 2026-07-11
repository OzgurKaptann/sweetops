"""
Inventory reconciliation (scripts/reconcile_inventory.py).

Reconciliation exists to catch the thing every other test assumes cannot happen:
something wrote stock outside the inventory service. So these tests deliberately
corrupt the database behind the service's back and demand that the reconciler
notices.

It must also NEVER write. A reconciler that "repairs" drift by overwriting the
summary destroys the very evidence needed to find the bug that caused it.
"""
import importlib.util
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from tests.conftest import (
    _inventory_maintenance,
    cleanup_ingredient,
    make_ingredient,
    order_payload,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "reconcile_inventory.py"


def _load_reconciler():
    spec = importlib.util.spec_from_file_location("reconcile_inventory", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def reconciler():
    assert _SCRIPT.exists(), f"missing {_SCRIPT}"
    return _load_reconciler()


def _row_for(rows: list[dict], ingredient_id: int) -> dict:
    return next(r for r in rows if r["ingredient_id"] == ingredient_id)


# ---------------------------------------------------------------------------
# Clean books
# ---------------------------------------------------------------------------

class TestCleanLedgerReconciles:

    def test_untouched_ingredient_reconciles(self, db, reconciler):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            # An ingredient created straight into the summary has no ledger
            # history, so it legitimately shows a drift of its whole balance.
            # Give it the opening receipt the real world would have.
            db.add(IngredientStockMovement(
                ingredient_id=ing.id,
                movement_type="PURCHASE_RECEIPT",
                quantity=Decimal("100.000"),
                quantity_delta_on_hand=Decimal("100.000"),
                quantity_delta_reserved=Decimal("0"),
                unit="g",
                reason="acilis bakiyesi",
                legacy_backfill=True,
            ))
            db.commit()

            row = _row_for(reconciler.reconcile(ing.id), ing.id)
            assert row["mismatch"] is False, row
            assert Decimal(row["stored_on_hand_quantity"]) == Decimal("100.000")
            assert Decimal(row["computed_on_hand_from_ledger"]) == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_full_order_lifecycle_reconciles(self, db, client, kitchen_client, reconciler):
        """
        Reserve → consume through the real API, then reconcile. The summary, the
        ledger and the order lines must all tell the same story.
        """
        ing, _ = make_ingredient(
            db, on_hand=Decimal("100.000"), standard_quantity=Decimal("10.00")
        )
        try:
            db.add(IngredientStockMovement(
                ingredient_id=ing.id,
                movement_type="PURCHASE_RECEIPT",
                quantity=Decimal("100.000"),
                quantity_delta_on_hand=Decimal("100.000"),
                quantity_delta_reserved=Decimal("0"),
                unit="g",
                reason="acilis bakiyesi",
                legacy_backfill=True,
            ))
            db.commit()

            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            oid = client.post(
                "/public/orders/", json=payload, headers=headers
            ).json()["order_id"]

            # While merely reserved, the books still reconcile.
            assert _row_for(reconciler.reconcile(ing.id), ing.id)["mismatch"] is False

            kitchen_client.patch(
                f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"}
            )

            row = _row_for(reconciler.reconcile(ing.id), ing.id)
            assert row["mismatch"] is False, row
            assert Decimal(row["stored_on_hand_quantity"]) == Decimal("90.000")
            assert Decimal(row["computed_on_hand_from_ledger"]) == Decimal("90.000")
            assert Decimal(row["stored_reserved_quantity"]) == Decimal("0")
            assert Decimal(row["computed_reserved_from_order_lines"]) == Decimal("0")
        finally:
            cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# Detecting drift
# ---------------------------------------------------------------------------

class TestDriftDetection:

    def test_summary_drift_is_detected(self, db, reconciler):
        """Someone edited the on-hand summary directly. The ledger disagrees."""
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            db.add(IngredientStockMovement(
                ingredient_id=ing.id,
                movement_type="PURCHASE_RECEIPT",
                quantity=Decimal("100.000"),
                quantity_delta_on_hand=Decimal("100.000"),
                quantity_delta_reserved=Decimal("0"),
                unit="g", reason="acilis", legacy_backfill=True,
            ))
            db.commit()
            assert _row_for(reconciler.reconcile(ing.id), ing.id)["mismatch"] is False

            # Behind the service's back: bump on-hand with no movement.
            db.execute(
                text("UPDATE ingredient_stock SET on_hand_quantity = 137 "
                     "WHERE ingredient_id = :i"),
                {"i": ing.id},
            )
            db.commit()

            row = _row_for(reconciler.reconcile(ing.id), ing.id)
            assert row["mismatch"] is True
            assert row["on_hand_mismatch"] is True
            assert Decimal(row["stored_on_hand_quantity"]) == Decimal("137.000")
            assert Decimal(row["computed_on_hand_from_ledger"]) == Decimal("100.000")
            assert Decimal(row["on_hand_mismatch_amount"]) == Decimal("37.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_reserved_drift_against_order_lines_is_detected(self, db, client, reconciler):
        """The reserved summary no longer matches the outstanding order lines."""
        ing, _ = make_ingredient(
            db, on_hand=Decimal("100.000"), standard_quantity=Decimal("10.00")
        )
        try:
            db.add(IngredientStockMovement(
                ingredient_id=ing.id,
                movement_type="PURCHASE_RECEIPT",
                quantity=Decimal("100.000"),
                quantity_delta_on_hand=Decimal("100.000"),
                quantity_delta_reserved=Decimal("0"),
                unit="g", reason="acilis", legacy_backfill=True,
            ))
            db.commit()

            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            client.post("/public/orders/", json=payload, headers=headers)
            assert _row_for(reconciler.reconcile(ing.id), ing.id)["mismatch"] is False

            # Corrupt only the reserved summary; the order line still says 10.
            db.execute(
                text("UPDATE ingredient_stock SET reserved_quantity = 3 "
                     "WHERE ingredient_id = :i"),
                {"i": ing.id},
            )
            db.commit()

            row = _row_for(reconciler.reconcile(ing.id), ing.id)
            assert row["mismatch"] is True
            assert row["reserved_mismatch"] is True
            assert Decimal(row["stored_reserved_quantity"]) == Decimal("3.000")
            assert Decimal(row["computed_reserved_from_order_lines"]) == Decimal("10.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_missing_ledger_movement_is_detected(self, db, reconciler):
        """A movement was removed from history — the summary now overstates."""
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            db.add(IngredientStockMovement(
                ingredient_id=ing.id,
                movement_type="PURCHASE_RECEIPT",
                quantity=Decimal("100.000"),
                quantity_delta_on_hand=Decimal("100.000"),
                quantity_delta_reserved=Decimal("0"),
                unit="g", reason="acilis", legacy_backfill=True,
            ))
            db.commit()
            assert _row_for(reconciler.reconcile(ing.id), ing.id)["mismatch"] is False

            # Deleting requires the ownership-gated escape hatch, because the
            # ledger refuses DELETE — which is itself the point.
            with _inventory_maintenance(db):
                db.query(IngredientStockMovement).filter_by(
                    ingredient_id=ing.id
                ).delete(synchronize_session=False)
            db.commit()

            row = _row_for(reconciler.reconcile(ing.id), ing.id)
            assert row["mismatch"] is True
            assert Decimal(row["computed_on_hand_from_ledger"]) == Decimal("0")
            assert Decimal(row["on_hand_mismatch_amount"]) == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------

class TestReconcilerCli:

    def test_cli_returns_nonzero_on_mismatch_and_zero_when_clean(self, db):
        import os

        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            db.add(IngredientStockMovement(
                ingredient_id=ing.id,
                movement_type="PURCHASE_RECEIPT",
                quantity=Decimal("100.000"),
                quantity_delta_on_hand=Decimal("100.000"),
                quantity_delta_reserved=Decimal("0"),
                unit="g", reason="acilis", legacy_backfill=True,
            ))
            db.commit()

            clean = subprocess.run(
                [sys.executable, str(_SCRIPT), "--ingredient", str(ing.id), "--json"],
                capture_output=True, text=True, cwd=str(_REPO_ROOT), env=os.environ.copy(),
            )
            assert clean.returncode == 0, clean.stdout + clean.stderr
            assert '"mismatch_count": 0' in clean.stdout

            # Introduce drift.
            db.execute(
                text("UPDATE ingredient_stock SET on_hand_quantity = 55 "
                     "WHERE ingredient_id = :i"),
                {"i": ing.id},
            )
            db.commit()

            dirty = subprocess.run(
                [sys.executable, str(_SCRIPT), "--ingredient", str(ing.id), "--json"],
                capture_output=True, text=True, cwd=str(_REPO_ROOT), env=os.environ.copy(),
            )
            assert dirty.returncode == 1, dirty.stdout + dirty.stderr
            assert '"mismatch_count": 1' in dirty.stdout
        finally:
            cleanup_ingredient(db, ing.id)

    def test_reconciler_never_mutates_stock(self, db, reconciler):
        """Running the reconciler over drifted books must change nothing."""
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            db.execute(
                text("UPDATE ingredient_stock SET on_hand_quantity = 42 "
                     "WHERE ingredient_id = :i"),
                {"i": ing.id},
            )
            db.commit()

            movements_before = db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id
            ).count()

            rows = reconciler.reconcile(ing.id)
            assert _row_for(rows, ing.id)["mismatch"] is True

            db.expire_all()
            s = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
            assert s.on_hand_quantity == Decimal("42.000"), (
                "Reconciliation must report drift, never silently repair it"
            )
            assert db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id
            ).count() == movements_before, "Reconciliation must not append movements"
        finally:
            cleanup_ingredient(db, ing.id)
