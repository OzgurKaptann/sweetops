"""
Inventory threshold alerts — the API, the status logic, and the line between a
threshold and the stock it describes.

The property under test, in one sentence: a threshold is an OPINION about the shelf,
and no opinion may ever move the shelf.

The failure modes being guarded against are not crashes. They are a system that looks
like it is working:

  * a threshold update that quietly changes a stock quantity, or writes a ledger row,
    so that configuring an alert silently corrupts the books;
  * an alert status computed against ON-HAND, so a shelf whose every gram is already
    promised to accepted orders reads as healthy and the branch cheerfully accepts an
    order it cannot cook;
  * an inverted ladder (critical above minimum) accepted, so the ingredient goes
    CRITICAL before it ever goes LOW and the early warning never fires at all;
  * an unconfigured threshold reported as HEALTHY, i.e. reassurance the system has no
    basis for;
  * a retried form re-logging the decision and re-stamping the timestamp an owner reads
    to ask who moved a warning level;
  * one branch's Idempotency-Key returning another branch's result;
  * a raw idempotency key stored anywhere at all.

Several of these tests go around the service layer entirely and assert that the
DATABASE refuses, because a service-level check that review can delete is not an
invariant.
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models.audit_log import AuditLog
from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from app.models.inventory_threshold import InventoryThresholdUpdate
from app.services import inventory_service
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
    order_payload,
    stock_for,
)

_DB_REJECTS = (IntegrityError, DBAPIError)

AUDIT_ACTION = "INVENTORY_THRESHOLDS_UPDATED"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _updates(db, ing_id: int) -> list[InventoryThresholdUpdate]:
    db.expire_all()
    return (
        db.query(InventoryThresholdUpdate)
        .filter(InventoryThresholdUpdate.ingredient_id == ing_id)
        .order_by(InventoryThresholdUpdate.id)
        .all()
    )


def _movements(db, ing_id: int) -> list[IngredientStockMovement]:
    db.expire_all()
    return (
        db.query(IngredientStockMovement)
        .filter(IngredientStockMovement.ingredient_id == ing_id)
        .all()
    )


def _audits(db, ing_id: int) -> list[AuditLog]:
    db.expire_all()
    return [
        a
        for a in db.query(AuditLog).filter(AuditLog.action == AUDIT_ACTION).all()
        if (a.payload_after or {}).get("ingredient_id") == ing_id
    ]


def _patch(client, ing_id: int, **kw):
    """PATCH thresholds with a fresh idempotency key unless one is given."""
    body = {
        "critical_quantity": kw.pop("critical", None),
        "minimum_quantity": kw.pop("minimum", None),
        "target_quantity": kw.pop("target", None),
        "reason": kw.pop("reason", "Kis sezonu"),
    }
    body.update(kw.pop("extra", {}))
    for drop in kw.pop("omit", []):
        body.pop(drop, None)

    headers = {}
    key = kw.pop("key", "__fresh__")
    if key is not None:
        headers["Idempotency-Key"] = uuid.uuid4().hex if key == "__fresh__" else key

    return client.patch(
        f"/inventory/stock/{ing_id}/thresholds", json=body, headers=headers
    )


def _alerts(client, **params):
    return client.get("/inventory/threshold-alerts", params=params or None)


def _alert_for(payload: dict, ing_id: int) -> dict | None:
    for item in payload["items"]:
        if item["ingredient_id"] == ing_id:
            return item
    return None


@pytest.fixture()
def env(db, make_staff):
    """A manager, an authenticated client, and 10 kg of an ingredient on the shelf."""

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
    yield e
    cleanup_ingredient(db, e.ingredient.id)


def _set(db, stock: IngredientStock, **thresholds) -> None:
    """Set thresholds directly, for status tests that are not about the endpoint."""
    for key, value in thresholds.items():
        setattr(stock, key, None if value is None else Decimal(value))
    db.commit()
    db.refresh(stock)


# ═══════════════════════════════════════════════════════════════════════════
# A threshold is not stock
# ═══════════════════════════════════════════════════════════════════════════

class TestThresholdsDoNotMoveStock:
    def test_update_does_not_change_stock_quantities(self, db, env):
        """The whole feature rests on this. An opinion about the shelf is not the shelf."""
        before = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        on_hand, reserved, avail = (
            Decimal(before.on_hand_quantity),
            Decimal(before.reserved_quantity),
            Decimal(before.available_quantity),
        )

        res = _patch(env.client, env.ingredient.id, critical="2", minimum="5", target="20")
        assert res.status_code == 200, res.text

        after = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        assert Decimal(after.on_hand_quantity) == on_hand
        assert Decimal(after.reserved_quantity) == reserved
        assert Decimal(after.available_quantity) == avail

        # ...and the response says so too, so a client can see nothing moved.
        body = res.json()
        assert Decimal(body["on_hand_quantity"]) == on_hand
        assert Decimal(body["reserved_quantity"]) == reserved
        assert Decimal(body["available_quantity"]) == avail

    def test_update_writes_no_stock_movement(self, db, env):
        """
        Not one ledger row. This is what keeps waste, consumption velocity, purchase
        receipts and transfer metrics free of threshold noise: a threshold change is
        not a movement of any type, so it cannot appear in any movement report.
        """
        before = len(_movements(db, env.ingredient.id))
        res = _patch(env.client, env.ingredient.id, critical="2", minimum="5")
        assert res.status_code == 200

        assert len(_movements(db, env.ingredient.id)) == before

    def test_update_does_not_touch_last_restocked(self, db, env):
        res = _patch(env.client, env.ingredient.id, target="50")
        assert res.status_code == 200
        assert _stock(db, DEFAULT_STORE_ID, env.ingredient.id).last_restocked is None


# ═══════════════════════════════════════════════════════════════════════════
# Status logic
# ═══════════════════════════════════════════════════════════════════════════

class TestStatusLogic:
    def test_healthy(self, db, env):
        _set(db, env.stock, critical_quantity="2", minimum_quantity="5")
        item = _alert_for(_alerts(env.client).json(), env.ingredient.id)
        assert item["status"] == "HEALTHY"
        assert item["status_label"] == "Stok yeterli"

    def test_low(self, db, env):
        # 10 available, minimum 12 → low but not yet critical.
        _set(db, env.stock, critical_quantity="2", minimum_quantity="12")
        item = _alert_for(_alerts(env.client).json(), env.ingredient.id)
        assert item["status"] == "LOW"
        assert item["status_label"] == "Düşük stok"

    def test_critical(self, db, env):
        _set(db, env.stock, critical_quantity="12", minimum_quantity="20")
        item = _alert_for(_alerts(env.client).json(), env.ingredient.id)
        assert item["status"] == "CRITICAL"
        assert item["status_label"] == "Kritik stok"

    def test_critical_takes_priority_over_low(self, db, env):
        """At or below BOTH levels, the stronger one is reported."""
        _set(db, env.stock, critical_quantity="10", minimum_quantity="10")
        item = _alert_for(_alerts(env.client).json(), env.ingredient.id)
        assert item["status"] == "CRITICAL"

    def test_at_the_threshold_is_at_or_below(self, db, env):
        """available == critical is CRITICAL. The boundary is inclusive, as documented."""
        _set(db, env.stock, critical_quantity="10")
        assert _alert_for(_alerts(env.client).json(), env.ingredient.id)["status"] == "CRITICAL"

        _set(db, env.stock, critical_quantity="9.999")
        assert _alert_for(_alerts(env.client).json(), env.ingredient.id)["status"] == "HEALTHY"

    def test_out_of_stock(self, db, make_staff):
        manager = make_staff("MANAGER", store_id=DEFAULT_STORE_ID)
        client = make_authed_client(db, manager)
        ing, stock = make_ingredient(
            db, on_hand=Decimal("0.000"), unit="kg", store_id=DEFAULT_STORE_ID
        )
        try:
            _set(db, stock, critical_quantity="2", minimum_quantity="5")
            item = _alert_for(_alerts(client).json(), ing.id)
            assert item["status"] == "OUT_OF_STOCK"
            assert item["status_label"] == "Stokta yok"
        finally:
            cleanup_ingredient(db, ing.id)

    def test_out_of_stock_beats_not_configured(self, db, make_staff):
        """
        An empty shelf is empty whether or not anybody configured a threshold for it. A
        manager does not have to have set a level to be told there is none left.
        """
        manager = make_staff("MANAGER", store_id=DEFAULT_STORE_ID)
        client = make_authed_client(db, manager)
        ing, _stock_row = make_ingredient(
            db, on_hand=Decimal("0.000"), unit="kg", store_id=DEFAULT_STORE_ID
        )
        try:
            item = _alert_for(_alerts(client).json(), ing.id)
            assert item["status"] == "OUT_OF_STOCK"
        finally:
            cleanup_ingredient(db, ing.id)

    def test_not_configured(self, db, env):
        """
        No alert threshold set. Reported as NOT_CONFIGURED and NOT as HEALTHY: an
        unconfigured threshold is missing information, and rendering it as an all-clear
        would be the system inventing reassurance it has no basis for.
        """
        item = _alert_for(_alerts(env.client).json(), env.ingredient.id)
        assert item["status"] == "NOT_CONFIGURED"
        assert item["status_label"] == "Eşik tanımlı değil"

    def test_target_alone_is_not_an_alert_threshold(self, db, env):
        """
        A target answers "how much should I buy?", not "am I in trouble?". A row with
        only a target configured has had nothing said about when to warn — so it is
        NOT_CONFIGURED, even though its recommended top-up is perfectly computable.
        """
        _set(db, env.stock, target_quantity="50")
        item = _alert_for(_alerts(env.client).json(), env.ingredient.id)
        assert item["status"] == "NOT_CONFIGURED"
        # ...and the recommendation is still there, because it is a different question.
        assert Decimal(item["recommended_restock_quantity"]) == Decimal("40.000")

    def test_critical_only_is_a_valid_configuration(self, db, env):
        _set(db, env.stock, critical_quantity="12")
        assert _alert_for(_alerts(env.client).json(), env.ingredient.id)["status"] == "CRITICAL"

        _set(db, env.stock, critical_quantity="2")
        assert _alert_for(_alerts(env.client).json(), env.ingredient.id)["status"] == "HEALTHY"

    def test_status_uses_available_not_on_hand(self, db, env, client):
        """
        THE central decision of this feature.

        The shelf physically holds 10 kg and the minimum is 5. On-hand alone says
        healthy. But an accepted order has already promised some of it, and stock that
        is promised is not stock this branch can use for new demand — so the status is
        judged on AVAILABLE, and it is LOW.

        Getting this wrong means a branch reads "stok yeterli" off a shelf whose every
        gram is already spoken for, and accepts an order it cannot cook.
        """
        # Reserve enough to push available below the minimum without touching on-hand.
        for _ in range(3):
            payload, headers = order_payload(
                env.ingredient.id, store_id=DEFAULT_STORE_ID, idem_key=uuid.uuid4().hex
            )
            assert client.post("/public/orders/", json=payload, headers=headers).status_code in (200, 201)

        stock = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        assert Decimal(stock.on_hand_quantity) == Decimal("10.000")
        reserved = Decimal(stock.reserved_quantity)
        assert reserved > 0
        available = Decimal(stock.available_quantity)

        # A minimum that available is BELOW but on-hand is comfortably above.
        minimum = available + Decimal("1.000")
        assert minimum < Decimal("10.000"), "the test needs on-hand to look healthy"
        _set(db, stock, minimum_quantity=str(minimum))

        item = _alert_for(_alerts(env.client).json(), env.ingredient.id)
        assert item["status"] == "LOW", (
            "reserved stock must be able to push an ingredient into LOW even while the "
            "shelf looks full"
        )
        assert Decimal(item["on_hand_quantity"]) == Decimal("10.000")
        assert Decimal(item["available_quantity"]) == available

    def test_reserved_stock_can_push_an_item_to_out_of_stock(self, db, env, client):
        """
        available == 0 while the shelf is NOT empty: every gram is promised. Reported as
        OUT_OF_STOCK, because there is nothing left to promise anybody new.
        """
        stock = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        # Reserve the lot, without moving on-hand.
        stock.reserved_quantity = Decimal("10.000")
        db.commit()

        item = _alert_for(_alerts(env.client).json(), env.ingredient.id)
        assert Decimal(item["on_hand_quantity"]) == Decimal("10.000")
        assert Decimal(item["available_quantity"]) == Decimal("0.000")
        assert item["status"] == "OUT_OF_STOCK"

    def test_below_reserved_has_the_highest_priority(self, db, env):
        """
        on_hand < reserved: the branch has promised stock it does not physically hold.

        ck_stock_reserved_le_on_hand makes this unrepresentable, so it is built here as
        a detached in-memory row rather than written — the point is that IF the state
        ever arose, the classifier shouts about it rather than filing it under "stokta
        yok" beside a merely empty shelf. Those are different problems: one is solved by
        buying stock, the other by dealing with orders that cannot be fulfilled.
        """
        row = IngredientStock(
            store_id=DEFAULT_STORE_ID,
            ingredient_id=env.ingredient.id,
            on_hand_quantity=Decimal("1.000"),
            reserved_quantity=Decimal("5.000"),
            unit="kg",
            critical_quantity=Decimal("2.000"),
            minimum_quantity=Decimal("5.000"),
        )
        # available is GENERATED in the database; this row was never persisted, so it is
        # supplied here as the value the identity defines.
        row.available_quantity = Decimal("-4.000")

        assert inventory_service.threshold_status(row) == "BELOW_RESERVED"

    def test_the_database_refuses_a_below_reserved_row(self, db, env):
        """The status above is defensive. THIS is why it should never fire."""
        stock = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        stock.reserved_quantity = Decimal("50.000")  # > on_hand
        with pytest.raises(_DB_REJECTS):
            db.commit()
        db.rollback()


# ═══════════════════════════════════════════════════════════════════════════
# Recommended restock
# ═══════════════════════════════════════════════════════════════════════════

class TestRecommendedRestock:
    def test_uses_target_minus_available(self, db, env):
        _set(db, env.stock, minimum_quantity="5", target_quantity="25")
        item = _alert_for(_alerts(env.client).json(), env.ingredient.id)
        # 25 target − 10 available
        assert Decimal(item["recommended_restock_quantity"]) == Decimal("15.000")

    def test_is_measured_against_available_not_on_hand(self, db, env, client):
        """
        Stock already promised to accepted orders will not be on the shelf to satisfy
        tomorrow's demand. Counting it as if it were is how a branch under-orders.
        """
        payload, headers = order_payload(
            env.ingredient.id, store_id=DEFAULT_STORE_ID, idem_key=uuid.uuid4().hex
        )
        assert client.post("/public/orders/", json=payload, headers=headers).status_code in (200, 201)

        stock = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        available = Decimal(stock.available_quantity)
        assert available < Decimal("10.000")
        _set(db, stock, target_quantity="25")

        item = _alert_for(_alerts(env.client).json(), env.ingredient.id)
        assert Decimal(item["recommended_restock_quantity"]) == Decimal("25.000") - available

    def test_is_null_when_no_target_is_configured(self, db, env):
        _set(db, env.stock, critical_quantity="2", minimum_quantity="5")
        item = _alert_for(_alerts(env.client).json(), env.ingredient.id)
        assert item["recommended_restock_quantity"] is None

    def test_is_null_when_the_branch_is_already_at_target(self, db, env):
        """
        Nothing to recommend. Null rather than 0: a zero would render as a number in a
        column of numbers and invite someone to order zero of something.
        """
        _set(db, env.stock, minimum_quantity="5", target_quantity="10")
        item = _alert_for(_alerts(env.client).json(), env.ingredient.id)
        assert item["recommended_restock_quantity"] is None


# ═══════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════

class TestSummary:
    def test_summary_counts_and_totals(self, db, env):
        _set(db, env.stock, critical_quantity="12", minimum_quantity="20", target_quantity="30")
        body = _alerts(env.client).json()

        assert body["summary"]["critical"] >= 1
        # 30 target − 10 available
        assert Decimal(body["summary"]["total_recommended_restock"]) >= Decimal("20.000")

    def test_filtering_does_not_change_the_summary(self, db, env):
        """
        The cards must describe the BRANCH, not the filter. A manager who filters to
        "kritik" must still be able to see that four other ingredients are low —
        otherwise the cards agree with the filter and hide the very thing they exist to
        surface.
        """
        _set(db, env.stock, critical_quantity="12")
        unfiltered = _alerts(env.client).json()["summary"]
        filtered = _alerts(env.client, status="CRITICAL").json()

        assert filtered["summary"] == unfiltered
        assert all(i["status"] == "CRITICAL" for i in filtered["items"])
        assert any(i["ingredient_id"] == env.ingredient.id for i in filtered["items"])


# ═══════════════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════════════

class TestValidation:
    def test_negative_thresholds_are_rejected(self, db, env):
        for field in ("critical", "minimum", "target"):
            res = _patch(env.client, env.ingredient.id, **{field: "-1"})
            assert res.status_code == 422, field
            assert res.json()["detail"]["error"] == "threshold_negative"
            assert "negatif" in res.json()["detail"]["message"]

    def test_critical_above_minimum_is_rejected(self, db, env):
        res = _patch(env.client, env.ingredient.id, critical="9", minimum="5")
        assert res.status_code == 422
        assert res.json()["detail"]["error"] == "threshold_critical_above_minimum"

    def test_minimum_above_target_is_rejected(self, db, env):
        res = _patch(env.client, env.ingredient.id, minimum="30", target="20")
        assert res.status_code == 422
        assert res.json()["detail"]["error"] == "threshold_minimum_above_target"

    def test_critical_above_target_is_rejected_without_a_minimum(self, db, env):
        """Nothing else relates critical to target when minimum is not configured."""
        res = _patch(env.client, env.ingredient.id, critical="30", target="20")
        assert res.status_code == 422
        assert res.json()["detail"]["error"] == "threshold_critical_above_target"

    def test_partial_configurations_are_accepted(self, db, env):
        assert _patch(env.client, env.ingredient.id, critical="2").status_code == 200
        assert _patch(env.client, env.ingredient.id, minimum="5", target="20").status_code == 200
        assert _patch(env.client, env.ingredient.id, critical="2", target="20").status_code == 200

    def test_a_threshold_can_be_cleared(self, db, env):
        """Clearing is a real decision, and it is logged like any other."""
        assert _patch(env.client, env.ingredient.id, critical="2", minimum="5").status_code == 200
        assert _stock(db, DEFAULT_STORE_ID, env.ingredient.id).critical_quantity is not None

        res = _patch(env.client, env.ingredient.id, reason="Eşikler kaldırıldı")
        assert res.status_code == 200

        stock = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        assert stock.critical_quantity is None
        assert stock.minimum_quantity is None
        assert stock.target_quantity is None
        assert len(_updates(db, env.ingredient.id)) == 2  # the set AND the clear

    def test_zero_is_a_real_threshold_and_is_not_a_clear(self, db, env):
        """
        "Warn me only when it is actually gone" is a deliberate choice, and it must not
        be stored as "nobody has decided anything".
        """
        res = _patch(env.client, env.ingredient.id, critical="0")
        assert res.status_code == 200
        assert Decimal(res.json()["critical_quantity"]) == Decimal("0.000")

        stock = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        assert stock.critical_quantity is not None
        assert Decimal(stock.critical_quantity) == Decimal("0")

    def test_reason_is_required(self, db, env):
        res = _patch(env.client, env.ingredient.id, critical="2", reason="   ")
        assert res.status_code == 422

        res = _patch(env.client, env.ingredient.id, critical="2", omit=["reason"])
        assert res.status_code == 422

    def test_unknown_fields_are_rejected(self, db, env):
        """
        REJECTED, not ignored. Silently dropping a smuggled store_id would leave a
        client believing it had configured another branch's alerts and cheerfully told
        so.
        """
        for field, value in (
            ("store_id", 999),
            ("actor_user_id", 999),
            ("status", "HEALTHY"),
            ("on_hand_quantity", "5"),
            ("idempotency_key_hash", "x"),
        ):
            res = _patch(
                env.client, env.ingredient.id, critical="2", extra={field: value}
            )
            assert res.status_code == 422, f"{field} was not rejected"

    def test_the_store_comes_from_the_session_not_the_body(self, db, env, make_store, make_staff):
        """
        There is no store_id field to send (the schema forbids it), so the only thing
        left to prove is that the update landed in the CALLER's branch and nowhere else.
        """
        other = make_store()
        other_stock = stock_for(db, env.ingredient, other.id, on_hand=Decimal("10.000"))

        res = _patch(env.client, env.ingredient.id, critical="2", minimum="5")
        assert res.status_code == 200
        assert res.json()["store_id"] == DEFAULT_STORE_ID

        db.refresh(other_stock)
        assert other_stock.critical_quantity is None, (
            "another branch's thresholds must not move"
        )

    def test_an_ingredient_this_branch_does_not_stock_is_404(self, db, env, make_store, make_staff):
        """
        Configuring a threshold does not CREATE stock. A shelf that exists only because
        somebody said they would like to be warned about it is a lie about what the
        branch carries.
        """
        other = make_store()
        other_manager = make_staff("MANAGER", store_id=other.id)
        other_client = make_authed_client(db, other_manager)

        res = _patch(other_client, env.ingredient.id, critical="2")
        assert res.status_code == 404
        assert res.json()["detail"]["error"] == "stock_not_configured"

        # ...and no stock row was conjured up for it.
        assert _stock(db, other.id, env.ingredient.id) is None


# ═══════════════════════════════════════════════════════════════════════════
# Permissions, CSRF, origin
# ═══════════════════════════════════════════════════════════════════════════

class TestPermissions:
    def test_owner_can_read_alerts_and_update(self, db, env, make_staff):
        owner = make_authed_client(db, make_staff("OWNER", store_id=DEFAULT_STORE_ID))
        assert _alerts(owner).status_code == 200
        assert _patch(owner, env.ingredient.id, critical="2").status_code == 200

    def test_manager_can_read_alerts_and_update(self, db, env):
        assert _alerts(env.client).status_code == 200
        assert _patch(env.client, env.ingredient.id, critical="2").status_code == 200

    def test_kitchen_may_read_but_not_update(self, db, env, make_staff):
        """KITCHEN holds inventory:read and not inventory:adjust — it sees the shortage
        it has to cook around, and cannot rewrite the levels."""
        kitchen = make_authed_client(db, make_staff("KITCHEN", store_id=DEFAULT_STORE_ID))
        assert _alerts(kitchen).status_code == 200
        assert _patch(kitchen, env.ingredient.id, critical="2").status_code == 403

    def test_cashier_cannot_read_or_update(self, db, env, make_staff):
        cashier = make_authed_client(db, make_staff("CASHIER", store_id=DEFAULT_STORE_ID))
        assert _alerts(cashier).status_code == 403
        assert _patch(cashier, env.ingredient.id, critical="2").status_code == 403

    def test_unauthenticated_is_refused(self, client, env):
        assert _alerts(client).status_code == 401
        assert _patch(client, env.ingredient.id, critical="2").status_code == 401

    def test_missing_csrf_is_rejected(self, db, env):
        no_csrf = make_authed_client(db, env.manager)
        no_csrf.headers.pop("X-CSRF-Token", None)

        res = no_csrf.patch(
            f"/inventory/stock/{env.ingredient.id}/thresholds",
            json={"critical_quantity": "2", "reason": "x"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert res.status_code == 403
        # ...and nothing was written.
        assert _stock(db, DEFAULT_STORE_ID, env.ingredient.id).critical_quantity is None

    def test_missing_idempotency_key_is_rejected(self, db, env):
        res = _patch(env.client, env.ingredient.id, critical="2", key=None)
        assert res.status_code == 400
        assert res.json()["detail"]["error"] == "idempotency_required"
        assert _stock(db, DEFAULT_STORE_ID, env.ingredient.id).critical_quantity is None

    def test_a_storeless_session_is_refused(self, db, env, make_staff):
        """
        There is no chain-wide alert screen, and no chain-wide threshold to set.

        The rejection is a 401, not the router's 403 ``no_store_assigned``: an
        OPERATIONAL role with no store assignment cannot resolve a session at all
        (auth_service refuses it), so the request never reaches the permission check.
        That is the stronger of the two answers and it is the one asserted here — the
        403 in routers/inventory.py remains the backstop for any future non-operational
        role that could hold inventory:read.
        """
        nomad = make_authed_client(db, make_staff("MANAGER", store_id=None))
        assert _alerts(nomad).status_code == 401
        assert _patch(nomad, env.ingredient.id, critical="2").status_code == 401
        # The database holds this line independently: fk_stock_threshold_actor_store
        # (store_id, threshold_updated_by_user_id) → users has no row to match for a
        # user whose store_id is NULL.
        assert _stock(db, DEFAULT_STORE_ID, env.ingredient.id).critical_quantity is None


# ═══════════════════════════════════════════════════════════════════════════
# Idempotency
# ═══════════════════════════════════════════════════════════════════════════

class TestIdempotency:
    def test_same_key_same_payload_replays(self, db, env):
        key = uuid.uuid4().hex
        first = _patch(env.client, env.ingredient.id, critical="2", minimum="5", key=key)
        assert first.status_code == 200
        assert first.json()["idempotent_replay"] is False

        second = _patch(env.client, env.ingredient.id, critical="2", minimum="5", key=key)
        assert second.status_code == 200
        assert second.json()["idempotent_replay"] is True

        # ONE decision on the record, not two.
        assert len(_updates(db, env.ingredient.id)) == 1

    def test_replay_does_not_re_stamp_the_timestamp(self, db, env):
        """
        `threshold_updated_at` means "when the levels last actually CHANGED", not "when
        somebody last pressed the button". It is worthless to an owner asking who moved
        a warning level if a retry moves it.
        """
        key = uuid.uuid4().hex
        first = _patch(env.client, env.ingredient.id, critical="2", key=key)
        stamped = _stock(db, DEFAULT_STORE_ID, env.ingredient.id).threshold_updated_at

        second = _patch(env.client, env.ingredient.id, critical="2", key=key)
        assert second.json()["idempotent_replay"] is True

        assert _stock(db, DEFAULT_STORE_ID, env.ingredient.id).threshold_updated_at == stamped
        assert second.json()["threshold_updated_at"] == first.json()["threshold_updated_at"]

    def test_same_key_different_payload_is_409(self, db, env):
        """
        Replaying the original under the new intent would tell a manager who has just
        lowered the critical level to 2 kg that their change succeeded, while the branch
        quietly keeps warning at 5.
        """
        key = uuid.uuid4().hex
        assert _patch(env.client, env.ingredient.id, critical="5", key=key).status_code == 200

        res = _patch(env.client, env.ingredient.id, critical="2", key=key)
        assert res.status_code == 409
        assert res.json()["detail"]["error"] == "idempotency_mismatch"

        # The original still stands, untouched.
        assert Decimal(_stock(db, DEFAULT_STORE_ID, env.ingredient.id).critical_quantity) == Decimal("5")
        assert len(_updates(db, env.ingredient.id)) == 1

    def test_clearing_and_setting_zero_are_different_payloads(self, db, env):
        """
        A retry of "clear the critical level" must never be mistaken for "set it to
        zero". They are opposite instructions to whoever reads the row next.
        """
        key = uuid.uuid4().hex
        assert _patch(env.client, env.ingredient.id, critical=None, key=key).status_code == 200

        res = _patch(env.client, env.ingredient.id, critical="0", key=key)
        assert res.status_code == 409

    def test_the_same_key_may_be_reused_by_another_store(self, db, env, make_store, make_staff):
        """
        Two branch managers working from the same printed run-book will legitimately send
        the same Idempotency-Key. That is a coincidence, not a replay: Beşiktaş's update
        must never return Kadıköy's result and quietly configure nothing.
        """
        other = make_store()
        other_stock = stock_for(db, env.ingredient, other.id, on_hand=Decimal("10.000"))
        other_client = make_authed_client(db, make_staff("MANAGER", store_id=other.id))

        key = uuid.uuid4().hex
        mine = _patch(env.client, env.ingredient.id, critical="2", key=key)
        theirs = _patch(other_client, env.ingredient.id, critical="7", minimum="9", key=key)

        assert mine.status_code == 200
        assert theirs.status_code == 200, theirs.text
        assert theirs.json()["idempotent_replay"] is False, (
            "the other branch's key collision must not be treated as a replay"
        )
        assert theirs.json()["store_id"] == other.id

        db.refresh(other_stock)
        assert Decimal(other_stock.critical_quantity) == Decimal("7")
        assert Decimal(_stock(db, DEFAULT_STORE_ID, env.ingredient.id).critical_quantity) == Decimal("2")

    def test_the_raw_key_is_never_stored(self, db, env):
        """Only SHA-256 digests. A stored key is a replay token sitting in a table."""
        key = uuid.uuid4().hex
        assert _patch(env.client, env.ingredient.id, critical="2", key=key).status_code == 200

        update = _updates(db, env.ingredient.id)[0]
        assert update.idempotency_key_hash != key
        assert len(update.idempotency_key_hash) == 64

        # ...and it is nowhere else in the row, or in the audit payload, either.
        hits = db.execute(
            text(
                "SELECT COUNT(*) FROM inventory_threshold_updates "
                "WHERE idempotency_key_hash = :k OR request_hash = :k OR reason = :k"
            ),
            {"k": key},
        ).scalar()
        assert hits == 0

    def test_the_receipt_never_echoes_the_key_or_the_hash(self, db, env):
        key = uuid.uuid4().hex
        body = _patch(env.client, env.ingredient.id, critical="2", key=key).json()
        serialized = str(body)
        assert key not in serialized
        assert "idempotency_key_hash" not in body
        assert "request_hash" not in body


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════

class TestAudit:
    def test_audit_event_is_written_once_with_old_and_new_values(self, db, env):
        assert _patch(env.client, env.ingredient.id, critical="2", minimum="5").status_code == 200
        res = _patch(
            env.client, env.ingredient.id,
            critical="3", minimum="8", target="20", reason="Kis talebi artti",
        )
        assert res.status_code == 200

        events = _audits(db, env.ingredient.id)
        assert len(events) == 2

        latest = events[-1]
        payload = latest.payload_after
        assert payload["store_id"] == DEFAULT_STORE_ID
        assert payload["ingredient_id"] == env.ingredient.id
        assert payload["old_critical_quantity"] == "2.000"
        assert payload["old_minimum_quantity"] == "5.000"
        assert payload["old_target_quantity"] is None
        assert payload["new_critical_quantity"] == "3.000"
        assert payload["new_minimum_quantity"] == "8.000"
        assert payload["new_target_quantity"] == "20.000"
        assert payload["actor_user_id"] == env.manager.id
        assert payload["reason"] == "Kis talebi artti"

    def test_replay_does_not_duplicate_the_audit_event(self, db, env):
        key = uuid.uuid4().hex
        assert _patch(env.client, env.ingredient.id, critical="2", key=key).status_code == 200
        assert _patch(env.client, env.ingredient.id, critical="2", key=key).status_code == 200

        assert len(_audits(db, env.ingredient.id)) == 1

    def test_the_audit_payload_leaks_no_credential(self, db, env):
        key = uuid.uuid4().hex
        assert _patch(env.client, env.ingredient.id, critical="2", key=key).status_code == 200

        payload = _audits(db, env.ingredient.id)[0].payload_after
        serialized = str(payload)
        assert key not in serialized
        for forbidden in ("idempotency", "request_hash", "csrf", "session", "token"):
            assert forbidden not in serialized.lower()

    def test_an_unconfigured_threshold_is_audited_as_null_not_zero(self, db, env):
        """None means NOT CONFIGURED. Writing "0" there would record a decision the
        manager did not make."""
        assert _patch(env.client, env.ingredient.id, critical="2").status_code == 200

        payload = _audits(db, env.ingredient.id)[0].payload_after
        assert payload["old_critical_quantity"] is None
        assert payload["new_minimum_quantity"] is None
        assert payload["new_target_quantity"] is None


# ═══════════════════════════════════════════════════════════════════════════
# The database, not merely the service
# ═══════════════════════════════════════════════════════════════════════════

class TestDatabaseRefuses:
    """
    A service-level check that review can delete is not an invariant. Every rule the
    endpoint enforces is also a CHECK constraint, and these tests go around the service
    entirely to prove it.
    """

    def test_negative_threshold_is_refused(self, db, env):
        stock = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        stock.critical_quantity = Decimal("-1.000")
        with pytest.raises(_DB_REJECTS):
            db.commit()
        db.rollback()

    def test_critical_above_minimum_is_refused(self, db, env):
        stock = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        stock.critical_quantity = Decimal("9.000")
        stock.minimum_quantity = Decimal("5.000")
        with pytest.raises(_DB_REJECTS):
            db.commit()
        db.rollback()

    def test_minimum_above_target_is_refused(self, db, env):
        stock = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        stock.minimum_quantity = Decimal("30.000")
        stock.target_quantity = Decimal("20.000")
        with pytest.raises(_DB_REJECTS):
            db.commit()
        db.rollback()

    def test_critical_above_target_is_refused_without_a_minimum(self, db, env):
        stock = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        stock.critical_quantity = Decimal("30.000")
        stock.target_quantity = Decimal("20.000")
        with pytest.raises(_DB_REJECTS):
            db.commit()
        db.rollback()

    def test_partial_thresholds_are_allowed(self, db, env):
        stock = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        stock.critical_quantity = Decimal("2.000")
        db.commit()  # critical alone: fine
        assert Decimal(_stock(db, DEFAULT_STORE_ID, env.ingredient.id).critical_quantity) == 2

    def test_the_threshold_log_is_append_only(self, db, env):
        assert _patch(env.client, env.ingredient.id, critical="2").status_code == 200
        update = _updates(db, env.ingredient.id)[0]

        update.reason = "rewritten after the fact"
        with pytest.raises(_DB_REJECTS):
            db.commit()
        db.rollback()

    def test_the_threshold_actor_must_belong_to_the_store(self, db, env, make_store, make_staff):
        """
        fk_stock_threshold_actor_store. A Kadıköy manager stamped on Beşiktaş's threshold
        row is unrepresentable, not merely forbidden.
        """
        other = make_store()
        outsider = make_staff("MANAGER", store_id=other.id)

        stock = _stock(db, DEFAULT_STORE_ID, env.ingredient.id)
        stock.threshold_updated_by_user_id = outsider.id
        with pytest.raises(_DB_REJECTS):
            db.commit()
        db.rollback()
