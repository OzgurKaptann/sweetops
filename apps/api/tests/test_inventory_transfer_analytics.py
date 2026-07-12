"""
Transfers must not lie to the owner's reports.

The whole reason a transfer is its own movement type — rather than a signed
MANUAL_ADJUSTMENT, or a WASTE plus a PURCHASE_RECEIPT — is that analytics read the
ledger by TYPE, and every wrong type tells the owner a specific falsehood:

    booked as WASTE             "Kadıköy binned 2 kg of chocolate."  It shipped it.
    booked as PURCHASE_RECEIPT  "Beşiktaş bought 2 kg from a supplier."  Nobody did.
                                Purchasing spend is now overstated by 2 kg.
    booked as CONSUMPTION       the burn rate rises, so the reorder engine sees a
                                branch racing through chocolate and orders more of
                                it — for a branch that simply put it on a van.

The one thing a transfer legitimately DOES change is availability, and therefore
stockout risk: a branch that ships away its last chocolate really is about to run
out, and a branch that receives it really is not. That is the final test here.
"""
import uuid
from decimal import Decimal

import pytest

from app.models.ingredient_stock import (
    MOVEMENT_CONSUMPTION,
    MOVEMENT_PURCHASE_RECEIPT,
    MOVEMENT_TRANSFER_IN,
    MOVEMENT_TRANSFER_OUT,
    MOVEMENT_WASTE,
    NON_CONSUMPTION_OUTFLOW_TYPES,
    IngredientStock,
    IngredientStockMovement,
)
from app.services.decision_engine import _slow_moving_signals, _stock_risk_signals
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
    stock_for,
)


def _key() -> str:
    return uuid.uuid4().hex


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


def _signal_for(signals: list[dict], ing_id: int) -> dict | None:
    return next((s for s in signals if s["data"]["ingredient_id"] == ing_id), None)


def _risk(db, store_id: int) -> list[dict]:
    """Stockout-risk signals for a store, read FRESH.

    expire_all() is load-bearing: the transfer commits in the API request's own
    session, so this session's identity map still holds the pre-transfer stock row.
    Without it the engine would score the risk against stock that has already
    left the building — which is precisely the staleness bug these signals exist
    to catch, so the test must not commit it itself.
    """
    db.expire_all()
    return _stock_risk_signals(db, store_id=store_id)


def _slow(db, store_id: int) -> list[dict]:
    db.expire_all()
    return _slow_moving_signals(db, store_id=store_id)


def _ledger(db, store_id: int, ing_id: int, movement_type: str) -> list:
    db.expire_all()
    return (
        db.query(IngredientStockMovement)
        .filter(
            IngredientStockMovement.store_id == store_id,
            IngredientStockMovement.ingredient_id == ing_id,
            IngredientStockMovement.movement_type == movement_type,
        )
        .all()
    )


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


class TestTransferIsNotWaste:

    def test_transfer_out_is_excluded_from_waste(self, db, transfer_client):
        """
        The outbound leg physically removes stock from the branch, exactly as waste
        does — same sign, same magnitude. The ONLY thing separating "we shipped it
        to Beşiktaş" from "we burnt it" is the movement type, and an owner deciding
        whether a branch manager is careless depends entirely on that distinction.
        """
        client, src, dst = transfer_client
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            assert _transfer(client, dst, ing.id, "40.000").status_code == 200

            # The source shed 40 g...
            assert _stock(db, src, ing.id).on_hand_quantity == Decimal("60.000")
            # ...and not one gram of it is waste.
            assert _ledger(db, src, ing.id, MOVEMENT_WASTE) == []
            assert len(_ledger(db, src, ing.id, MOVEMENT_TRANSFER_OUT)) == 1

            # The waste view of the ledger shows nothing for this ingredient.
            r = client.get(
                "/inventory/movements",
                params={"ingredient_id": ing.id, "movement_type": MOVEMENT_WASTE},
            )
            assert r.status_code == 200
            assert r.json()["items"] == []
        finally:
            cleanup_ingredient(db, ing.id)

    def test_waste_and_transfer_out_are_separable_in_the_same_store(
        self, db, transfer_client
    ):
        """Both remove stock from the same shelf on the same day. A report must be
        able to say which was which, and how much of each."""
        client, src, dst = transfer_client
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            assert _transfer(client, dst, ing.id, "40.000").status_code == 200
            burnt = client.post(
                "/inventory/waste",
                json={"ingredient_id": ing.id, "quantity": "10.000", "reason": "yandı"},
                headers={"Idempotency-Key": _key()},
            )
            assert burnt.status_code == 200, burnt.text

            waste_rows = _ledger(db, src, ing.id, MOVEMENT_WASTE)
            transfer_rows = _ledger(db, src, ing.id, MOVEMENT_TRANSFER_OUT)

            assert len(waste_rows) == 1
            assert waste_rows[0].quantity == Decimal("10.000")
            assert len(transfer_rows) == 1
            assert transfer_rows[0].quantity == Decimal("40.000")

            # 100 - 40 shipped - 10 burnt.
            assert _stock(db, src, ing.id).on_hand_quantity == Decimal("50.000")

            # Both are non-consumption outflow, but they are NOT the same thing —
            # the grouping exists so a query cannot silently merge them.
            assert MOVEMENT_TRANSFER_OUT in NON_CONSUMPTION_OUTFLOW_TYPES
            assert MOVEMENT_WASTE in NON_CONSUMPTION_OUTFLOW_TYPES
            assert MOVEMENT_TRANSFER_OUT != MOVEMENT_WASTE
        finally:
            cleanup_ingredient(db, ing.id)


class TestTransferIsNotAPurchase:

    def test_transfer_in_is_excluded_from_purchase_receipts(self, db, transfer_client):
        """
        The inbound leg adds stock to a branch exactly as a supplier delivery does.
        If it were booked as PURCHASE_RECEIPT, the chain's purchasing figures would
        count 2 kg of chocolate it never bought — every internal shipment inflating
        spend that was only ever incurred once, at the branch that actually bought it.
        """
        client, _src, dst = transfer_client
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        stock_for(db, ing, dst, on_hand=Decimal("5.000"))
        try:
            assert _transfer(client, dst, ing.id, "40.000").status_code == 200

            # The destination really did gain 40 g...
            assert _stock(db, dst, ing.id).on_hand_quantity == Decimal("45.000")
            # ...and none of it is recorded as bought from anyone.
            assert _ledger(db, dst, ing.id, MOVEMENT_PURCHASE_RECEIPT) == []
            assert len(_ledger(db, dst, ing.id, MOVEMENT_TRANSFER_IN)) == 1

            # last_restocked is a PURCHASE_RECEIPT concept and must not be touched
            # by an internal shipment: the branch has not been resupplied by a
            # supplier, and a reorder report keying off it would be misled.
            assert _stock(db, dst, ing.id).last_restocked is None
        finally:
            cleanup_ingredient(db, ing.id)


class TestTransferIsNotConsumption:

    def test_transfers_do_not_inflate_consumption_velocity(self, db, transfer_client):
        """
        Velocity is the rate a branch physically BURNS stock, and it is what the
        reorder engine reasons from. A branch that ships 40 g to another branch has
        not consumed 40 g — if it counted, the engine would see a branch racing
        through chocolate and reorder for it, while the branch that actually got the
        chocolate looks idle.
        """
        client, src, dst = transfer_client
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            assert _transfer(client, dst, ing.id, "40.000").status_code == 200

            # No consumption movement exists in either store.
            assert _ledger(db, src, ing.id, MOVEMENT_CONSUMPTION) == []
            assert _ledger(db, dst, ing.id, MOVEMENT_CONSUMPTION) == []

            # ...so the source is still, correctly, a SLOW-MOVING ingredient: it has
            # not been cooked with. Shipping it away is not "movement" in the sense
            # this signal means, and pretending otherwise would hide dead stock.
            slow = _slow(db, src)
            assert _signal_for(slow, ing.id) is not None, (
                "a transferred-away ingredient was counted as 'moving' — a transfer "
                "is not consumption"
            )

            # The receiving branch has not consumed it either.
            slow_dst = _slow(db, dst)
            assert _signal_for(slow_dst, ing.id) is not None
        finally:
            cleanup_ingredient(db, ing.id)


class TestStockoutRiskFollowsTheStock:

    def test_shipping_stock_away_raises_the_source_s_stockout_risk(
        self, db, transfer_client
    ):
        """
        The one thing a transfer legitimately DOES change.

        Stockout risk runs on AVAILABLE, and a transfer really does lower the
        source's available stock — a branch that has shipped away its last chocolate
        genuinely is about to run out, and the owner needs to see that. Meanwhile the
        branch that received it genuinely is not.
        """
        client, src, dst = transfer_client
        # Plenty on hand: comfortably above the reorder level, no risk signal.
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            before = _signal_for(_risk(db, src), ing.id)
            assert before is None, "the source was already at risk before the transfer"

            # Ship almost all of it away.
            assert _transfer(client, dst, ing.id, "97.000").status_code == 200

            after = _signal_for(_risk(db, src), ing.id)
            assert after is not None, (
                "the source shipped away 97 of 100 g and its stockout risk did not "
                "move — risk must follow AVAILABLE stock"
            )
            assert _stock(db, src, ing.id).available_quantity == Decimal("3.000")

            # And the destination, which now holds the stock, is not at risk.
            assert _stock(db, dst, ing.id).available_quantity == Decimal("97.000")
            assert _signal_for(_risk(db, dst), ing.id) is None
        finally:
            cleanup_ingredient(db, ing.id)

    def test_receiving_a_transfer_clears_the_destination_s_stockout_risk(
        self, db, transfer_client
    ):
        """The rescue case, which is the entire operational point of the feature: a
        branch about to run out of chocolate is topped up from a branch that has
        plenty, and its risk signal clears."""
        client, _src, dst = transfer_client
        ing, _ = make_ingredient(db, on_hand=Decimal("500.000"))
        stock_for(db, ing, dst, on_hand=Decimal("1.000"))     # nearly out
        try:
            assert _signal_for(_risk(db, dst), ing.id) is not None

            assert _transfer(client, dst, ing.id, "200.000").status_code == 200

            assert _stock(db, dst, ing.id).available_quantity == Decimal("201.000")
            assert _signal_for(_risk(db, dst), ing.id) is None
        finally:
            cleanup_ingredient(db, ing.id)
