"""
Reconciliation and analytics for physical stock counts.

Two independent claims are proved here.

RECONCILIATION — a count's correction is an ORDINARY on-hand delta.
    It is not special-cased, not excluded, and not exempt: after a valid count the
    summary still equals the sum of the ledger, exactly as it must after a waste or
    a purchase receipt. What reconciliation adds on top is the one thing a per-store
    total cannot see — a count whose movement is missing or wrong. That leaves the
    ledger and the summary in perfect agreement WITH EACH OTHER (both still hold the
    pre-count figure) while the count sheet says something else entirely, and each
    record looks internally consistent on its own.

    Zero-delta counts must never be reported as drift. The shelf agreed with the
    system; that is the healthy case, and it is the whole point of recording it.

ANALYTICS — a counted discrepancy is NOT waste, NOT a purchase, NOT consumption.
    This is the reason STOCK_COUNT_ADJUSTMENT is its own movement type rather than
    a MANUAL_ADJUSTMENT or a WASTE. If a count that found 350 g missing were booked
    as WASTE, the owner's waste report would accuse the branch of throwing away
    chocolate nobody threw away; as a PURCHASE_RECEIPT, the purchasing report would
    invent a supplier delivery; as CONSUMPTION, the velocity that drives every
    reorder decision would be inflated by stock that was never sold.

    A count does, however, change the SHELF — so stock summary and stockout risk
    must reflect it immediately. Being excluded from the flow metrics and being
    reflected in the stock level are not in tension: one is about what HAPPENED, the
    other about what IS.
"""
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.models.ingredient_stock import (
    MOVEMENT_CONSUMPTION,
    MOVEMENT_PURCHASE_RECEIPT,
    MOVEMENT_STOCK_COUNT_ADJUSTMENT,
    MOVEMENT_WASTE,
    IngredientStock,
    IngredientStockMovement,
)
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "reconcile_inventory.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reconcile(*args: str) -> subprocess.CompletedProcess:
    """Run the real reconciliation script as a subprocess, exactly as ops would."""
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _count(client, ing_id: int, counted: str, reason: str = "Haftalik sayim"):
    return client.post(
        "/inventory/stock-counts",
        json={
            "ingredient_id": ing_id,
            "counted_quantity": counted,
            "reason": reason,
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )


def _opening(db, store_id: int, ing, qty: Decimal) -> None:
    """
    The opening receipt that makes a store's summary row add up to its ledger.

    The fixtures create a stock row directly, with no movement behind it, so without
    this the ledger sum would be short by the opening balance and EVERY row would
    report a (spurious) mismatch. Same helper, and same reason, as
    test_inventory_transfer_reconciliation.py.
    """
    db.add(IngredientStockMovement(
        store_id=store_id,
        ingredient_id=ing.id,
        movement_type=MOVEMENT_PURCHASE_RECEIPT,
        quantity=qty,
        quantity_delta_on_hand=qty,
        quantity_delta_reserved=Decimal("0"),
        unit=ing.unit,
        reason="acilis bakiyesi",
        legacy_backfill=True,
    ))
    db.commit()


@pytest.fixture()
def env(db, make_staff):
    """
    A manager and a 10 kg shelf, with NO opening ledger row.

    The analytics tests want the ledger to contain exactly what the count put there
    and nothing else, so they can assert that a specific movement type is ABSENT.
    An opening PURCHASE_RECEIPT would defeat that, so it is added only by
    ``recon_env`` below, where the ledger arithmetic is the thing under test.
    """
    class Env:
        pass

    e = Env()
    e.manager = make_staff("MANAGER", store_id=DEFAULT_STORE_ID)
    e.client = make_authed_client(db, e.manager)
    e.ingredient, e.stock = make_ingredient(
        db,
        on_hand=Decimal("10.000"),
        standard_quantity=Decimal("2.000"),
        unit="kg",
        store_id=DEFAULT_STORE_ID,
    )
    e.ingredient_id = e.ingredient.id
    yield e
    cleanup_ingredient(db, e.ingredient_id)


@pytest.fixture()
def recon_env(db, env):
    """The same shelf, with its opening balance booked into the ledger — so the
    summary and the ledger start out in agreement and any drift the tests see is
    drift the COUNT caused."""
    _opening(db, DEFAULT_STORE_ID, env.ingredient, Decimal("10.000"))
    return env


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

class TestReconciliation:
    def test_reconciliation_passes_after_a_valid_count(self, db, recon_env):
        """
        The count's correction is an ordinary ledger delta, so the summary still
        equals the sum of the ledger. Nothing about a count is exempt from the
        arithmetic.
        """
        assert _count(recon_env.client, recon_env.ingredient_id, "9.250").status_code == 200

        proc = _reconcile("--ingredient", str(recon_env.ingredient_id))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "OK" in proc.stdout
        assert "every count has exactly the ledger movement" in proc.stdout

    def test_zero_delta_count_is_not_drift(self, db, recon_env):
        """The shelf was checked and found correct. That is the healthy case and it
        must not be reported as a mismatch — it is evidence, not an error."""
        res = _count(recon_env.client, recon_env.ingredient_id, "10.000")
        assert res.status_code == 200
        assert res.json()["movement_id"] is None

        proc = _reconcile("--ingredient", str(recon_env.ingredient_id))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "BROKEN STOCK COUNTS" not in proc.stdout

    def test_reconciliation_detects_a_non_zero_count_whose_movement_was_deleted(
        self, db, recon_env
    ):
        """
        The failure a per-store total CANNOT see. Delete the movement and the ledger
        and the summary still agree with each other — both simply hold the pre-count
        figure — while the count sheet says the shelf was corrected. Only comparing
        the count against its movement finds it.

        The database refuses to create this (the deferred trigger checks it at
        COMMIT), so it is forged here through the ownership-gated DDL escape hatch —
        which is exactly the class of corruption reconciliation exists to catch: a
        manual SQL edit, a bad restore, a future migration bug.
        """
        res = _count(recon_env.client, recon_env.ingredient_id, "9.250")
        assert res.status_code == 200
        count_id = res.json()["stock_count_id"]

        # Forge the corruption: the count survives, its movement does not.
        db.execute(text(
            "ALTER TABLE ingredient_stock_movements "
            "DISABLE TRIGGER trg_ingredient_stock_movements_immutable"
        ))
        db.execute(
            text("DELETE FROM ingredient_stock_movements WHERE stock_count_id = :i"),
            {"i": count_id},
        )
        db.execute(text(
            "ALTER TABLE ingredient_stock_movements "
            "ENABLE TRIGGER trg_ingredient_stock_movements_immutable"
        ))
        db.commit()

        proc = _reconcile("--ingredient", str(recon_env.ingredient_id))
        assert proc.returncode == 1, proc.stdout
        assert "BROKEN STOCK COUNTS" in proc.stdout
        # The report names the count, so an operator can go and look at it.
        assert f"stock_count {count_id}" in proc.stdout
        assert "corrected on paper only" in proc.stdout

    def test_reconciliation_reports_the_stock_count_id_in_json(self, db, recon_env):
        res = _count(recon_env.client, recon_env.ingredient_id, "9.250")
        count_id = res.json()["stock_count_id"]

        db.execute(text(
            "ALTER TABLE ingredient_stock_movements "
            "DISABLE TRIGGER trg_ingredient_stock_movements_immutable"
        ))
        db.execute(
            text("DELETE FROM ingredient_stock_movements WHERE stock_count_id = :i"),
            {"i": count_id},
        )
        db.execute(text(
            "ALTER TABLE ingredient_stock_movements "
            "ENABLE TRIGGER trg_ingredient_stock_movements_immutable"
        ))
        db.commit()

        import json as _json

        proc = _reconcile("--ingredient", str(recon_env.ingredient_id), "--json")
        assert proc.returncode == 1
        report = _json.loads(proc.stdout)
        assert report["broken_stock_count_count"] == 1
        broken = report["broken_stock_counts"][0]
        assert broken["stock_count_id"] == count_id
        assert broken["delta_quantity"] == "-0.750"
        assert broken["total_movements"] == 0

    def test_reconciliation_never_mutates(self, db, recon_env):
        """A reconciliation that 'fixed' drift by overwriting the summary would
        destroy the evidence needed to find the bug that caused it."""
        assert _count(recon_env.client, recon_env.ingredient_id, "9.250").status_code == 200

        before = _reconcile("--ingredient", str(recon_env.ingredient_id), "--json").stdout
        after = _reconcile("--ingredient", str(recon_env.ingredient_id), "--json").stdout
        assert before == after

        db.expire_all()
        stock = (
            db.query(IngredientStock)
            .filter(
                IngredientStock.store_id == DEFAULT_STORE_ID,
                IngredientStock.ingredient_id == recon_env.ingredient_id,
            )
            .first()
        )
        assert Decimal(stock.on_hand_quantity) == Decimal("9.250")


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

class TestAnalyticsSeparation:
    """
    The count's correction must not be mistaken for any other kind of stock event.
    Each of these would be a WRONG NUMBER on a report an owner makes decisions from,
    not a crash.
    """

    def _movement_types(self, db, ing_id: int) -> list[str]:
        db.expire_all()
        return [
            m.movement_type
            for m in db.query(IngredientStockMovement)
            .filter(IngredientStockMovement.ingredient_id == ing_id)
            .all()
        ]

    def test_count_is_not_recorded_as_waste(self, db, env):
        """A count that found 750 g missing did not throw 750 g away. Booking it as
        WASTE would accuse the branch of binning chocolate nobody binned."""
        assert _count(env.client, env.ingredient_id, "9.250").status_code == 200

        types = self._movement_types(db, env.ingredient_id)
        assert MOVEMENT_STOCK_COUNT_ADJUSTMENT in types
        assert MOVEMENT_WASTE not in types

        # And the waste-filtered ledger read does not see it. Scoped to THIS
        # ingredient: the store's ledger carries other tests' movements too, and the
        # claim under test is about this count, not about the store being pristine.
        waste = env.client.get(
            "/inventory/movements",
            params={
                "movement_type": MOVEMENT_WASTE,
                "ingredient_id": env.ingredient_id,
            },
        ).json()
        assert waste["total"] == 0

    def test_count_is_not_recorded_as_a_purchase_receipt(self, db, env):
        """A count that found 1.5 kg MORE on the shelf did not buy 1.5 kg. Booking it
        as a PURCHASE_RECEIPT would invent a supplier delivery that never happened."""
        assert _count(env.client, env.ingredient_id, "11.500").status_code == 200

        types = self._movement_types(db, env.ingredient_id)
        assert MOVEMENT_STOCK_COUNT_ADJUSTMENT in types
        assert MOVEMENT_PURCHASE_RECEIPT not in types

        receipts = env.client.get(
            "/inventory/movements",
            params={
                "movement_type": MOVEMENT_PURCHASE_RECEIPT,
                "ingredient_id": env.ingredient_id,
            },
        ).json()
        assert receipts["total"] == 0

    def test_count_is_not_recorded_as_consumption(self, db, env):
        """Nobody ate it. Booking a count as CONSUMPTION would inflate the velocity
        that every reorder decision is computed from."""
        assert _count(env.client, env.ingredient_id, "9.250").status_code == 200

        types = self._movement_types(db, env.ingredient_id)
        assert MOVEMENT_CONSUMPTION not in types

    def test_count_is_excluded_from_consumption_velocity(self, db, env):
        """
        The decision engine measures velocity from CONSUMPTION movements only. A
        count is physical reality catching up with the books, not a waffle being
        sold, so it must contribute NOTHING to consumption velocity.
        """
        from app.services import decision_engine

        # A big downward count. If it counted as consumption, velocity would spike.
        assert _count(env.client, env.ingredient_id, "1.000").status_code == 200

        db.expire_all()
        consumed = (
            db.query(IngredientStockMovement)
            .filter(
                IngredientStockMovement.ingredient_id == env.ingredient_id,
                IngredientStockMovement.movement_type == MOVEMENT_CONSUMPTION,
            )
            .count()
        )
        assert consumed == 0

        # The engine's own stock-risk query measures velocity from CONSUMPTION rows
        # only, so the count contributes nothing to it. The ingredient may well be
        # flagged AT RISK — its shelf really is nearly empty — but at zero velocity:
        # the risk comes from the level, never from mistaking a count for a sale.
        signals = decision_engine._stock_risk_signals(db, store_id=DEFAULT_STORE_ID)
        mine = [
            s for s in signals
            if s.get("data", {}).get("ingredient_id") == env.ingredient_id
        ]
        for signal in mine:
            assert signal["data"]["velocity_per_hour"] == 0

    def test_count_is_not_a_manual_adjustment(self, db, env):
        """The distinction the whole feature rests on: a count and an adjustment are
        different events and analytics must be able to tell them apart."""
        from app.models.ingredient_stock import MOVEMENT_MANUAL_ADJUSTMENT

        assert _count(env.client, env.ingredient_id, "9.250").status_code == 200
        assert MOVEMENT_MANUAL_ADJUSTMENT not in self._movement_types(
            db, env.ingredient_id
        )

    def test_stock_summary_and_stockout_risk_reflect_the_post_count_shelf(
        self, db, env
    ):
        """
        Excluded from the FLOW metrics, but absolutely reflected in the LEVEL. A
        count that found the freezer nearly empty must make the branch look nearly
        out of stock — that is the entire operational point of counting.
        """
        before = env.client.get("/inventory/stock").json()
        row_before = next(
            r for r in before["items"] if r["ingredient_id"] == env.ingredient_id
        )
        assert Decimal(row_before["on_hand_quantity"]) == Decimal("10.000")

        # reorder_level is 5.00 (conftest default). Count down to 1 kg.
        assert _count(env.client, env.ingredient_id, "1.000").status_code == 200

        after = env.client.get("/inventory/stock").json()
        row = next(r for r in after["items"] if r["ingredient_id"] == env.ingredient_id)
        assert Decimal(row["on_hand_quantity"]) == Decimal("1.000")
        assert Decimal(row["available_quantity"]) == Decimal("1.000")
        # ...and it is now below its reorder level, i.e. genuinely at risk.
        assert Decimal(row["available_quantity"]) < Decimal(row["reorder_level"])
