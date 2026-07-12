"""
Reconciliation of transfers.

A transfer's two legs are ordinary ledger deltas, so the existing per-store
on-hand check already accounts for them: the outbound leg lowers the source's
total, the inbound leg raises the destination's, and each branch reconciles on its
own. The first test here proves exactly that — a valid transfer must not make the
reconciler complain.

What the per-store totals CANNOT see is a HALF transfer, and that is the whole
reason a fourth check exists. Stock that left Kadıköy and arrived nowhere leaves
Kadıköy's ledger and summary in perfect agreement with each other: both are simply
2 kg short of physical reality, and no per-store total is wrong. Only comparing the
transfer row against its legs finds it.

The database refuses to create such a thing (a deferred constraint trigger checks
the pairing at COMMIT — see test_inventory_transfer.py). So the corruption below is
manufactured with the trigger disabled, which is what a manual SQL edit, a restore
from an inconsistent backup, or a future migration bug would look like. Those are
the things a reconciler is FOR: it must not assume the constraint that is supposed
to protect it was actually in force.
"""
import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from app.models.inventory_transfer import InventoryTransfer
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
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


def _key() -> str:
    return uuid.uuid4().hex


@pytest.fixture()
def transfer_client(db, make_store, make_staff):
    dest = make_store("Beşiktaş")
    owner = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
    return make_authed_client(db, owner), DEFAULT_STORE_ID, dest.id


def _transfer(client, dest_id: int, ing_id: int, qty: str):
    return client.post(
        "/inventory/transfers",
        json={
            "destination_store_id": dest_id,
            "ingredient_id": ing_id,
            "quantity": qty,
            "reason": "şube takviyesi",
        },
        headers={"Idempotency-Key": _key()},
    )


def _opening(db, store_id: int, ing, qty: Decimal) -> None:
    """
    The opening receipt that makes a store's summary row add up to its ledger.

    The fixtures create a stock row directly, with no movement behind it, so
    without this the ledger sum would be short by the opening balance and EVERY
    row would report a (spurious) mismatch. Same helper, and same reason, as
    test_store_scoped_reconciliation.py.
    """
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


def _rows_for(reconciler, store_id: int, ing_id: int) -> dict | None:
    rows = reconciler.reconcile(store_id=store_id, ingredient_id=ing_id)
    return rows[0] if rows else None


def _stock(db, store_id: int, ing_id: int) -> IngredientStock | None:
    db.expire_all()
    return (
        db.query(IngredientStock)
        .filter(
            IngredientStock.store_id == store_id,
            IngredientStock.ingredient_id == ing_id,
        )
        .first()
    )


class TestReconciliationAfterAValidTransfer:

    def test_both_stores_reconcile_cleanly(self, db, reconciler, transfer_client):
        """
        The baseline. Each branch's summary must still equal the sum of its own
        ledger deltas — which now includes a transfer leg.
        """
        client, src, dst = transfer_client
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        stock_for(db, ing, dst, on_hand=Decimal("30.000"))
        _opening(db, src, ing, Decimal("100.000"))
        _opening(db, dst, ing, Decimal("30.000"))
        try:
            assert _transfer(client, dst, ing.id, "20.000").status_code == 200

            source = _rows_for(reconciler, src, ing.id)
            destination = _rows_for(reconciler, dst, ing.id)

            assert source["mismatch"] is False, source
            assert destination["mismatch"] is False, destination

            # The store-filtered report shows the correct POST-transfer stock,
            # per branch, without the two ever being pooled.
            assert Decimal(source["stored_on_hand_quantity"]) == Decimal("80.000")
            assert Decimal(source["computed_on_hand_from_ledger"]) == Decimal("80.000")
            assert Decimal(destination["stored_on_hand_quantity"]) == Decimal("50.000")
            assert Decimal(destination["computed_on_hand_from_ledger"]) == Decimal("50.000")

            # ...and the transfer pairing check is clean.
            assert reconciler.reconcile_transfers(ingredient_id=ing.id) == []
        finally:
            cleanup_ingredient(db, ing.id)

    def test_a_healthy_transfer_is_not_reported_as_broken(
        self, db, reconciler, transfer_client
    ):
        client, _src, dst = transfer_client
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            assert _transfer(client, dst, ing.id, "20.000").status_code == 200
            assert _transfer(client, dst, ing.id, "5.000").status_code == 200

            assert reconciler.reconcile_transfers(ingredient_id=ing.id) == []
            # Both stores, filtered either way.
            assert reconciler.reconcile_transfers(
                store_id=DEFAULT_STORE_ID, ingredient_id=ing.id
            ) == []
            assert reconciler.reconcile_transfers(store_id=dst, ingredient_id=ing.id) == []
        finally:
            cleanup_ingredient(db, ing.id)


class TestReconciliationDetectsAOneSidedTransfer:
    """
    Corruption is manufactured here by disabling the append-only trigger — the same
    ownership-gated escape hatch the test teardown uses, and NOT something reachable
    from the application or an injection path. It stands in for the ways a broken
    pair could really arrive: a DBA's manual UPDATE, a restore from an inconsistent
    backup, a future migration bug.
    """

    def test_a_missing_inbound_leg_is_reported(self, db, reconciler, transfer_client):
        """
        The worst case: stock left the source and arrived NOWHERE.

        Note what the per-store on-hand check says about this, and why it is not
        enough on its own — the destination's summary is corrected to match its
        (now leg-less) ledger, so BOTH stores reconcile perfectly against their own
        ledgers, and only the pairing check can see that 20 g of chocolate has
        ceased to exist.
        """
        client, src, dst = transfer_client
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        _opening(db, src, ing, Decimal("100.000"))
        try:
            r = _transfer(client, dst, ing.id, "20.000")
            assert r.status_code == 200
            transfer_id = r.json()["transfer_id"]

            # Delete the inbound leg, and take the destination's summary down with
            # it so that the store-level check is perfectly happy.
            db.execute(
                text("ALTER TABLE ingredient_stock_movements DISABLE TRIGGER "
                     "trg_ingredient_stock_movements_immutable")
            )
            try:
                db.execute(
                    text(
                        "DELETE FROM ingredient_stock_movements "
                        "WHERE transfer_id = :t AND movement_type = 'TRANSFER_IN'"
                    ),
                    {"t": transfer_id},
                )
            finally:
                db.execute(
                    text("ALTER TABLE ingredient_stock_movements ENABLE TRIGGER "
                         "trg_ingredient_stock_movements_immutable")
                )
            db.execute(
                text(
                    "UPDATE ingredient_stock SET on_hand_quantity = 0 "
                    "WHERE store_id = :s AND ingredient_id = :i"
                ),
                {"s": dst, "i": ing.id},
            )
            db.commit()

            # Every store-level total still agrees with its own ledger...
            assert _rows_for(reconciler, src, ing.id)["mismatch"] is False
            assert _rows_for(reconciler, dst, ing.id)["mismatch"] is False

            # ...and yet 20 g of chocolate has vanished. Only the pairing check sees it.
            broken = reconciler.reconcile_transfers(ingredient_id=ing.id)
            assert len(broken) == 1, broken
            issue = broken[0]
            assert issue["transfer_id"] == transfer_id
            assert issue["transfer_out_movements"] == 1
            assert issue["transfer_in_movements"] == 0
            assert "TRANSFER_IN" in issue["issue"]

            # It is reported to BOTH branches: the one that shipped, and the one
            # that never got its crate.
            assert reconciler.reconcile_transfers(store_id=src, ingredient_id=ing.id)
            assert reconciler.reconcile_transfers(store_id=dst, ingredient_id=ing.id)
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)

    def test_a_missing_outbound_leg_is_reported(self, db, reconciler, transfer_client):
        """The mirror image: the destination gained stock the source never gave up."""
        client, _src, dst = transfer_client
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = _transfer(client, dst, ing.id, "20.000")
            assert r.status_code == 200
            transfer_id = r.json()["transfer_id"]

            db.execute(
                text("ALTER TABLE ingredient_stock_movements DISABLE TRIGGER "
                     "trg_ingredient_stock_movements_immutable")
            )
            try:
                db.execute(
                    text(
                        "DELETE FROM ingredient_stock_movements "
                        "WHERE transfer_id = :t AND movement_type = 'TRANSFER_OUT'"
                    ),
                    {"t": transfer_id},
                )
            finally:
                db.execute(
                    text("ALTER TABLE ingredient_stock_movements ENABLE TRIGGER "
                         "trg_ingredient_stock_movements_immutable")
                )
            db.commit()

            broken = reconciler.reconcile_transfers(ingredient_id=ing.id)
            assert len(broken) == 1, broken
            assert broken[0]["transfer_out_movements"] == 0
            assert broken[0]["transfer_in_movements"] == 1
            assert "TRANSFER_OUT" in broken[0]["issue"]
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)

    def test_the_script_exits_nonzero_when_a_transfer_is_broken(
        self, db, reconciler, transfer_client, monkeypatch, capsys
    ):
        """
        A broken pair must FAIL the run, not merely be mentioned. A reconciler that
        prints a problem and exits 0 will be wired into a cron job and ignored
        forever.
        """
        client, _src, dst = transfer_client
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = _transfer(client, dst, ing.id, "20.000")
            transfer_id = r.json()["transfer_id"]

            db.execute(
                text("ALTER TABLE ingredient_stock_movements DISABLE TRIGGER "
                     "trg_ingredient_stock_movements_immutable")
            )
            try:
                db.execute(
                    text(
                        "DELETE FROM ingredient_stock_movements "
                        "WHERE transfer_id = :t AND movement_type = 'TRANSFER_IN'"
                    ),
                    {"t": transfer_id},
                )
            finally:
                db.execute(
                    text("ALTER TABLE ingredient_stock_movements ENABLE TRIGGER "
                         "trg_ingredient_stock_movements_immutable")
                )
            db.execute(
                text(
                    "UPDATE ingredient_stock SET on_hand_quantity = 0 "
                    "WHERE store_id = :s AND ingredient_id = :i"
                ),
                {"s": dst, "i": ing.id},
            )
            db.commit()

            monkeypatch.setattr(
                "sys.argv",
                ["reconcile_inventory.py", "--ingredient", str(ing.id)],
            )
            exit_code = reconciler.main()
            out = capsys.readouterr().out

            assert exit_code == 1
            assert "BROKEN TRANSFERS" in out
            assert str(transfer_id) in out
            # No secret ever reaches the report.
            assert "idempotency_key_hash" not in out
            assert "request_hash" not in out
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)


class TestReconcilerNeverWrites:

    def test_reconciling_a_broken_transfer_does_not_repair_it(
        self, db, reconciler, transfer_client
    ):
        """
        A reconciler that "fixes" what it finds destroys the only evidence of
        whatever wrote stock outside the inventory service. It reports; it never
        writes.
        """
        client, _src, dst = transfer_client
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = _transfer(client, dst, ing.id, "20.000")
            transfer_id = r.json()["transfer_id"]

            db.execute(
                text("ALTER TABLE ingredient_stock_movements DISABLE TRIGGER "
                     "trg_ingredient_stock_movements_immutable")
            )
            try:
                db.execute(
                    text(
                        "DELETE FROM ingredient_stock_movements "
                        "WHERE transfer_id = :t AND movement_type = 'TRANSFER_IN'"
                    ),
                    {"t": transfer_id},
                )
            finally:
                db.execute(
                    text("ALTER TABLE ingredient_stock_movements ENABLE TRIGGER "
                         "trg_ingredient_stock_movements_immutable")
                )
            db.commit()

            before = _stock(db, dst, ing.id).on_hand_quantity

            assert reconciler.reconcile_transfers(ingredient_id=ing.id)
            assert reconciler.reconcile(ingredient_id=ing.id)

            # Still broken, still exactly as broken as it was.
            assert _stock(db, dst, ing.id).on_hand_quantity == before
            assert db.get(InventoryTransfer, transfer_id) is not None
            assert (
                db.query(IngredientStockMovement)
                .filter(IngredientStockMovement.transfer_id == transfer_id)
                .count()
                == 1
            )
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)
