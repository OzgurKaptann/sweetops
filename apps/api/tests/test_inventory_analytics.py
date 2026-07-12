"""
Analytics must not confuse a promise with a fact.

The two failure modes this guards against:

  * counting RESERVED stock as CONSUMED — inflates the burn rate, so the shop
    reorders too early and the "ingredient consumption" report is fiction;
  * judging stockout risk on ON-HAND alone — hides a stockout that has already
    effectively happened, because the remaining stock is entirely promised to
    open orders.

Stockout risk therefore runs on AVAILABLE; consumption reporting runs on
CONSUMPTION movements; waste stays separately visible as WASTE.
"""
import uuid
from decimal import Decimal

import pytest

from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from app.services.decision_engine import _slow_moving_signals, _stock_risk_signals
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
    order_payload,
)


def _stock(db, ing_id: int) -> IngredientStock:
    db.expire_all()
    return db.query(IngredientStock).filter_by(ingredient_id=ing_id).first()


def _signal_for(signals: list[dict], ing_id: int) -> dict | None:
    return next((s for s in signals if s["data"]["ingredient_id"] == ing_id), None)


class TestAvailabilityDefinition:

    def test_available_equals_on_hand_minus_reserved(self, db, client):
        ing, _ = make_ingredient(
            db, on_hand=Decimal("100.000"), standard_quantity=Decimal("10.00")
        )
        try:
            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            client.post("/public/orders/", json=payload, headers=headers)

            s = _stock(db, ing.id)
            assert s.available_quantity == s.on_hand_quantity - s.reserved_quantity
            assert s.available_quantity == Decimal("90.000")
        finally:
            cleanup_ingredient(db, ing.id)


class TestStockRiskUsesAvailable:

    def test_reserved_stock_triggers_stockout_risk(self, db, client):
        """
        100 g physically on the shelf, but 100 g is promised to open orders.
        The shop can sell nothing — that IS a stockout, and the signal must fire
        even though on-hand is still comfortably above the reorder level.
        """
        ing, _ = make_ingredient(
            db, on_hand=Decimal("100.000"), standard_quantity=Decimal("100.00")
        )
        try:
            # Before any order there is no risk at all.
            assert _signal_for(_stock_risk_signals(db, DEFAULT_STORE_ID), ing.id) is None

            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            r = client.post("/public/orders/", json=payload, headers=headers)
            assert r.status_code == 200

            s = _stock(db, ing.id)
            assert s.on_hand_quantity == Decimal("100.000"), "still physically there"
            assert s.available_quantity == Decimal("0")

            signal = _signal_for(_stock_risk_signals(db, DEFAULT_STORE_ID), ing.id)
            assert signal is not None, (
                "Stock that is entirely reserved must raise a stockout risk — "
                "judging on on-hand alone would hide it"
            )
            assert signal["severity"] == "high"
            assert signal["data"]["available_quantity"] == 0.0
            assert signal["data"]["on_hand_quantity"] == 100.0
            assert signal["data"]["reserved_quantity"] == 100.0
            # current_stock is the availability figure the decision runs on.
            assert signal["data"]["current_stock"] == 0.0
        finally:
            cleanup_ingredient(db, ing.id)


class TestConsumptionAnalyticsUseConsumptionMovements:

    def test_reservation_is_not_counted_as_consumption(self, db, client, kitchen_client):
        """
        Velocity is burn rate. A merely-reserved order has burned nothing, so it
        must contribute zero velocity until the kitchen actually starts it.
        """
        ing, _ = make_ingredient(
            db, on_hand=Decimal("60.000"), standard_quantity=Decimal("10.00"),
        )
        try:
            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            oid = client.post(
                "/public/orders/", json=payload, headers=headers
            ).json()["order_id"]

            # Reserved only → no CONSUMPTION movement exists yet.
            assert db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type="CONSUMPTION"
            ).count() == 0

            signal = _signal_for(_stock_risk_signals(db, DEFAULT_STORE_ID), ing.id)
            if signal is not None:
                assert signal["data"]["velocity_per_hour"] == 0.0, (
                    "A reservation is not consumption — it must not create burn rate"
                )

            # Now actually cook it.
            kitchen_client.patch(
                f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"}
            )
            consumed = db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type="CONSUMPTION"
            ).all()
            assert len(consumed) == 1
            assert consumed[0].quantity == Decimal("10.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_slow_moving_ignores_reservations(self, db, client):
        """
        An ingredient sitting under an un-cooked reservation has still not moved.
        It must remain flagged as slow-moving.
        """
        ing, _ = make_ingredient(
            db, on_hand=Decimal("1000.000"), standard_quantity=Decimal("10.00")
        )
        try:
            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            client.post("/public/orders/", json=payload, headers=headers)

            signal = _signal_for(_slow_moving_signals(db, DEFAULT_STORE_ID), ing.id)
            assert signal is not None, (
                "A reservation that was never cooked does not make stock 'moving'"
            )
        finally:
            cleanup_ingredient(db, ing.id)


class TestWasteIsSeparatelyVisible:

    def test_waste_is_not_reported_as_consumption(self, db, make_staff):
        """
        Burnt batter is a cost, not a sale. It must never be folded into
        consumption, or the owner can never see what the shop is throwing away.
        """
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        owner = make_authed_client(db, make_staff("OWNER", store_id=1))
        try:
            r = owner.post(
                "/inventory/waste",
                json={"ingredient_id": ing.id, "quantity": "20.000",
                      "reason": "yanmis hamur"},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            )
            assert r.status_code == 200, r.text

            waste = db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type="WASTE"
            ).all()
            assert len(waste) == 1
            assert waste[0].quantity == Decimal("20.000")
            assert waste[0].reason == "yanmis hamur"

            assert db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type="CONSUMPTION"
            ).count() == 0, "Waste must never masquerade as consumption"

            # It did reduce physical stock, though.
            assert _stock(db, ing.id).on_hand_quantity == Decimal("80.000")
        finally:
            cleanup_ingredient(db, ing.id)


class TestOwnerStockStatusEndpoint:

    def test_stock_status_reports_all_three_quantities(self, db, client, make_staff):
        ing, _ = make_ingredient(
            db, on_hand=Decimal("100.000"), standard_quantity=Decimal("10.00")
        )
        owner = make_authed_client(db, make_staff("OWNER", store_id=1))
        try:
            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            client.post("/public/orders/", json=payload, headers=headers)

            r = owner.get("/owner/stock-status")
            assert r.status_code == 200, r.text
            row = next(
                i for i in r.json()["items"] if i["ingredient_id"] == ing.id
            )
            assert row["on_hand_quantity"] == 100.0
            assert row["reserved_quantity"] == 10.0
            assert row["available_quantity"] == 90.0
        finally:
            cleanup_ingredient(db, ing.id)

    def test_fully_reserved_stock_reads_as_critical_but_physically_present(
        self, db, client, make_staff
    ):
        """
        The owner must be able to tell "we are out of pistachio" apart from "we
        still have pistachio, but every gram is promised". Those need opposite
        responses — reorder vs. nothing.
        """
        ing, _ = make_ingredient(
            db, on_hand=Decimal("100.000"), standard_quantity=Decimal("100.00")
        )
        owner = make_authed_client(db, make_staff("OWNER", store_id=1))
        try:
            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            client.post("/public/orders/", json=payload, headers=headers)

            r = owner.get("/owner/stock-status")
            row = next(i for i in r.json()["items"] if i["ingredient_id"] == ing.id)

            assert row["severity"] == "critical"
            assert row["available_quantity"] == 0.0
            assert row["on_hand_quantity"] == 100.0
            # ...and the message says WHY, rather than claiming the shelf is bare.
            assert row["message"] == "Kalan stok bekleyen siparişler için ayrıldı"
        finally:
            cleanup_ingredient(db, ing.id)
