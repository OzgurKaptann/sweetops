"""
Threshold alerts vs. the ledger — the separation, proved rather than promised.

The claim this feature rests on is a NEGATIVE one: configuring an alert threshold
changes nothing about the stock, the ledger, or any report built on them. A negative
claim is exactly the kind that quietly stops being true, because nothing fails when it
does — the numbers merely start drifting, and every screen still renders.

So it is tested directly. After a threshold update:

  * reconciliation still passes (summary == ledger == order lines);
  * the movement ledger has not gained a single row, of any type;
  * waste, purchase-receipt, consumption and transfer totals are unchanged to the gram;
  * the reconciler reports threshold anomalies as WARNINGS and does not fail on them —
    because a badly-set warning level does not make the shop's books wrong, and a red
    reconciliation must always mean the stock is wrong.

The last one is a policy test, not an arithmetic one, and it is the one most likely to
be "fixed" by a future contributor who thinks a warning should fail the build. It is
here to tell them why it should not.
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.db import engine
from app.models.ingredient_stock import (
    MOVEMENT_CONSUMPTION,
    MOVEMENT_PURCHASE_RECEIPT,
    MOVEMENT_TRANSFER_IN,
    MOVEMENT_TRANSFER_OUT,
    MOVEMENT_WASTE,
    IngredientStock,
    IngredientStockMovement,
)
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
    order_payload,
)

import importlib.util
from pathlib import Path

# The reconciler is a script, not a package module.
_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "reconcile_inventory.py"
_spec = importlib.util.spec_from_file_location("reconcile_inventory", _SCRIPT)
reconcile_inventory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reconcile_inventory)


def _stock(db, store_id: int, ing_id: int) -> IngredientStock:
    db.expire_all()
    return (
        db.query(IngredientStock)
        .filter(
            IngredientStock.store_id == store_id,
            IngredientStock.ingredient_id == ing_id,
        )
        .first()
    )


def _movement_totals(db, ing_id: int) -> dict[str, Decimal]:
    """Every movement type's total on-hand delta, for this ingredient."""
    db.expire_all()
    rows = (
        db.query(IngredientStockMovement)
        .filter(IngredientStockMovement.ingredient_id == ing_id)
        .all()
    )
    totals: dict[str, Decimal] = {}
    for m in rows:
        totals[m.movement_type] = (
            totals.get(m.movement_type, Decimal("0")) + Decimal(m.quantity_delta_on_hand)
        )
    return totals


def _patch(client, ing_id: int, **kw):
    body = {
        "critical_quantity": kw.pop("critical", None),
        "minimum_quantity": kw.pop("minimum", None),
        "target_quantity": kw.pop("target", None),
        "reason": kw.pop("reason", "Kis sezonu"),
    }
    return client.patch(
        f"/inventory/stock/{ing_id}/thresholds",
        json=body,
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )


def _opening(db, store_id: int, ing, qty: Decimal) -> None:
    """
    The opening receipt that makes a store's summary row add up to its ledger.

    The fixtures create a stock row directly, with no movement behind it, so without
    this the ledger sum would be short by the opening balance and EVERY row would report
    a (spurious) mismatch. Same helper, and same reason, as
    test_inventory_stock_count_reconciliation.py and
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
    A manager and a 100 kg shelf, with NO opening ledger row.

    The analytics tests want the ledger to contain exactly what the test put there and
    nothing else, so they can assert that a movement type is ABSENT. An opening
    PURCHASE_RECEIPT would defeat that, so it is added only by ``recon_env``, where the
    ledger arithmetic is the thing under test.

    ``ingredient_id`` is held as a PLAIN INT as well as an ORM object: the anomaly test
    closes the session before running DDL (an idle-in-transaction connection makes ALTER
    TABLE wait forever), which detaches every ORM instance — so reading
    ``ingredient.id`` in teardown would raise DetachedInstanceError and leave the rows
    behind.
    """
    class Env:
        pass

    e = Env()
    e.manager = make_staff("MANAGER", store_id=DEFAULT_STORE_ID)
    e.client = make_authed_client(db, e.manager)
    e.ingredient, e.stock = make_ingredient(
        db,
        on_hand=Decimal("100.000"),
        standard_quantity=Decimal("2.000"),
        unit="kg",
        store_id=DEFAULT_STORE_ID,
    )
    e.ingredient_id = e.ingredient.id
    yield e
    cleanup_ingredient(db, e.ingredient_id)


@pytest.fixture()
def recon_env(db, env):
    """The same shelf, with its opening balance booked into the ledger — so the summary
    and the ledger start out in agreement and any drift the tests see is drift a
    THRESHOLD caused. (Which is the point: there should never be any.)"""
    _opening(db, DEFAULT_STORE_ID, env.ingredient, Decimal("100.000"))
    return env


# ═══════════════════════════════════════════════════════════════════════════
# Reconciliation
# ═══════════════════════════════════════════════════════════════════════════

class TestReconciliation:
    def test_reconciliation_still_passes_after_a_threshold_update(self, db, recon_env):
        """
        The summary still matches its ledger and its order lines. It could hardly do
        otherwise — no threshold column appears on either side of that comparison — and
        that is precisely the property worth pinning down.
        """
        assert _patch(recon_env.client, recon_env.ingredient.id, critical="10", minimum="25", target="80").status_code == 200

        rows = reconcile_inventory.reconcile(ingredient_id=recon_env.ingredient.id)
        assert rows, "the ingredient should be reconciled"
        assert all(not r["mismatch"] for r in rows), rows

    def test_reconciliation_is_unaffected_by_thresholds_alongside_real_movements(
        self, db, recon_env, client
    ):
        """A threshold change interleaved with real stock movements must not perturb the
        arithmetic of any of them."""
        payload, headers = order_payload(
            recon_env.ingredient.id, store_id=DEFAULT_STORE_ID, idem_key=uuid.uuid4().hex
        )
        assert client.post("/public/orders/", json=payload, headers=headers).status_code in (200, 201)

        assert _patch(recon_env.client, recon_env.ingredient.id, critical="10", minimum="25").status_code == 200

        assert recon_env.client.post(
            "/inventory/waste",
            json={"ingredient_id": recon_env.ingredient.id, "quantity": "5.000", "reason": "Yanan hamur"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).status_code == 200

        assert _patch(recon_env.client, recon_env.ingredient.id, critical="12", minimum="30").status_code == 200

        rows = reconcile_inventory.reconcile(ingredient_id=recon_env.ingredient.id)
        assert all(not r["mismatch"] for r in rows), rows

    def test_a_threshold_anomaly_is_a_warning_and_never_a_stock_mismatch(self, db, recon_env):
        """
        THE policy test.

        An incoherent threshold is reported — it means somebody bypassed the CHECK
        constraints, which is worth knowing — but it does NOT make the reconciliation
        fail, because the shop's books are not wrong. A mis-set warning level makes the
        alert screen useless; it does not move a gram of stock.

        If this is ever "fixed" so that a threshold warning fails the reconciliation,
        the next person to see a red build will learn that a red reconciliation does not
        necessarily mean the stock is wrong — and that is the one thing it must always
        mean.

        The anomaly has to be written with the CHECK constraint dropped, because the
        database will not otherwise let it exist at all — which is itself the point, and
        is asserted first. The session's connection is fully released before the DDL: an
        idle-in-transaction connection holds a lock that ALTER TABLE would wait behind
        forever.
        """
        ing_id = recon_env.ingredient_id

        # First: the database really does refuse it. The reconciler's check exists to
        # catch what got in some OTHER way (a manual SQL edit, a bad restore), not to
        # catch something the schema permits.
        stock = _stock(db, DEFAULT_STORE_ID, ing_id)
        stock.critical_quantity = Decimal("90")
        stock.minimum_quantity = Decimal("10")
        with pytest.raises(Exception):
            db.commit()
        db.rollback()

        # Now go around it, the way a bad restore would.
        db.close()
        engine.dispose()

        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE ingredient_stock "
                "DROP CONSTRAINT ck_stock_threshold_critical_le_minimum"
            ))
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE ingredient_stock "
                        "SET critical_quantity = 90, minimum_quantity = 10 "
                        "WHERE store_id = :s AND ingredient_id = :i"
                    ),
                    {"s": DEFAULT_STORE_ID, "i": ing_id},
                )

            warnings = reconcile_inventory.audit_thresholds(ingredient_id=ing_id)
            assert len(warnings) == 1
            assert any("critical" in issue for issue in warnings[0]["issues"])

            # ...and the STOCK reconciliation is still perfectly clean.
            rows = reconcile_inventory.reconcile(ingredient_id=ing_id)
            assert all(not r["mismatch"] for r in rows), (
                "an incoherent threshold must never be reported as a stock mismatch"
            )
        finally:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE ingredient_stock SET critical_quantity = NULL, "
                        "minimum_quantity = NULL WHERE store_id = :s AND ingredient_id = :i"
                    ),
                    {"s": DEFAULT_STORE_ID, "i": ing_id},
                )
                conn.execute(text(
                    "ALTER TABLE ingredient_stock ADD CONSTRAINT "
                    "ck_stock_threshold_critical_le_minimum CHECK ("
                    "critical_quantity IS NULL OR minimum_quantity IS NULL "
                    "OR critical_quantity <= minimum_quantity)"
                ))

    def test_a_coherent_threshold_raises_no_warning(self, db, recon_env):
        """NULL is not an anomaly. Nagging about unconfigured thresholds would train
        people to ignore this report, which is how a real warning gets missed."""
        assert _patch(recon_env.client, recon_env.ingredient.id, critical="10", minimum="25", target="80").status_code == 200
        assert reconcile_inventory.audit_thresholds(ingredient_id=recon_env.ingredient.id) == []

        # And an entirely unconfigured row is likewise silent.
        assert _patch(recon_env.client, recon_env.ingredient.id, reason="Eşikler kaldırıldı").status_code == 200
        assert reconcile_inventory.audit_thresholds(ingredient_id=recon_env.ingredient.id) == []


# ═══════════════════════════════════════════════════════════════════════════
# Analytics isolation
# ═══════════════════════════════════════════════════════════════════════════

class TestAnalyticsIsolation:
    def test_a_threshold_update_adds_no_ledger_row_of_any_type(self, db, env):
        """
        Not "no WASTE row" — NO ROW. A threshold change is not a movement of any type,
        so it cannot appear in any movement-based report, present or future. That is the
        structural guarantee behind every exclusion below, and it is why none of them
        needed a special case anywhere in the analytics code.
        """
        before = _movement_totals(db, env.ingredient.id)

        for critical, minimum in (("5", "20"), ("8", "25"), (None, None)):
            assert _patch(
                env.client, env.ingredient.id, critical=critical, minimum=minimum
            ).status_code == 200

        assert _movement_totals(db, env.ingredient.id) == before

    def test_thresholds_are_excluded_from_waste_purchases_consumption_and_transfers(
        self, db, env, client, make_store
    ):
        """
        Every analytics axis at once: waste, purchase receipts, consumption and transfer
        legs are byte-for-byte unchanged across a threshold edit.
        """
        # Some real movement of each kind, so the totals are non-trivial.
        assert env.client.post(
            "/inventory/purchase-receipts",
            json={"ingredient_id": env.ingredient.id, "quantity": "20.000", "reason": "Teslimat"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).status_code == 200
        assert env.client.post(
            "/inventory/waste",
            json={"ingredient_id": env.ingredient.id, "quantity": "3.000", "reason": "Yanan hamur"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).status_code == 200

        dest = make_store("Beşiktaş")
        assert env.client.post(
            "/inventory/transfers",
            json={
                "destination_store_id": dest.id,
                "ingredient_id": env.ingredient.id,
                "quantity": "4.000",
                "reason": "Takviye",
            },
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).status_code == 200

        payload, headers = order_payload(
            env.ingredient.id, store_id=DEFAULT_STORE_ID, idem_key=uuid.uuid4().hex
        )
        assert client.post("/public/orders/", json=payload, headers=headers).status_code in (200, 201)

        before = _movement_totals(db, env.ingredient.id)
        assert before.get(MOVEMENT_WASTE) == Decimal("-3.000")
        assert before.get(MOVEMENT_PURCHASE_RECEIPT) == Decimal("20.000")
        assert before.get(MOVEMENT_TRANSFER_OUT) == Decimal("-4.000")
        assert before.get(MOVEMENT_TRANSFER_IN) == Decimal("4.000")

        # Now churn the thresholds hard.
        for critical, minimum, target in (
            ("5", "20", "100"), ("1", "2", "3"), (None, None, None), ("0", "0", "0"),
        ):
            assert _patch(
                env.client, env.ingredient.id,
                critical=critical, minimum=minimum, target=target,
            ).status_code == 200

        after = _movement_totals(db, env.ingredient.id)
        assert after == before, (
            "a threshold change must not perturb waste, purchase, consumption or "
            "transfer metrics by a single gram"
        )
        # Consumption velocity is derived from CONSUMPTION rows; none was invented.
        assert after.get(MOVEMENT_CONSUMPTION) == before.get(MOVEMENT_CONSUMPTION)

    def test_stockout_risk_reflects_the_threshold_status(self, db, env):
        """
        The alert view is the one place a threshold IS allowed to change what a manager
        sees. 100 kg on the shelf with a minimum of 25 is healthy; raise the minimum
        above the stock and the same shelf is LOW — no stock moved, and the risk
        assessment changed, which is the entire point of the feature.
        """
        assert _patch(env.client, env.ingredient.id, critical="10", minimum="25").status_code == 200
        items = env.client.get("/inventory/threshold-alerts").json()["items"]
        mine = next(i for i in items if i["ingredient_id"] == env.ingredient.id)
        assert mine["status"] == "HEALTHY"

        on_hand_before = Decimal(_stock(db, DEFAULT_STORE_ID, env.ingredient.id).on_hand_quantity)

        assert _patch(env.client, env.ingredient.id, critical="10", minimum="150").status_code == 200
        items = env.client.get("/inventory/threshold-alerts").json()["items"]
        mine = next(i for i in items if i["ingredient_id"] == env.ingredient.id)
        assert mine["status"] == "LOW"

        # ...and the shelf never moved.
        assert Decimal(
            _stock(db, DEFAULT_STORE_ID, env.ingredient.id).on_hand_quantity
        ) == on_hand_before
