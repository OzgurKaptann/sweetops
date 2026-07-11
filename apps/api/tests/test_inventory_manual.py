"""
Manual inventory operations: purchase receipt, adjustment, waste.

These are the only ways physical stock moves without a customer order, so they
are the ones most in need of accountability: an authenticated actor, a reason,
an idempotency key, and an append-only ledger row. An unexplained stock
correction is indistinguishable from theft.

Also covers the authorization matrix — a CASHIER handles money, not stock; a
cook may look at stock but not rewrite it.
"""
import uuid
from decimal import Decimal

import pytest

from app.models.audit_log import AuditLog
from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from tests.conftest import cleanup_ingredient, make_authed_client, make_ingredient


def _key() -> dict:
    return {"Idempotency-Key": uuid.uuid4().hex}


def _stock(db, ing_id: int) -> IngredientStock:
    db.expire_all()
    return db.query(IngredientStock).filter_by(ingredient_id=ing_id).first()


@pytest.fixture()
def owner(db, make_staff):
    return make_authed_client(db, make_staff("OWNER", store_id=1))


# ---------------------------------------------------------------------------
# Purchase receipt
# ---------------------------------------------------------------------------

class TestPurchaseReceipt:

    def test_receipt_increases_on_hand(self, db, owner):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = owner.post(
                "/inventory/purchase-receipts",
                json={"ingredient_id": ing.id, "quantity": "50.000",
                      "reason": "haftalik teslimat"},
                headers=_key(),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert Decimal(body["on_hand_quantity"]) == Decimal("150.000")
            assert Decimal(body["available_quantity"]) == Decimal("150.000")
            assert body["movement_type"] == "PURCHASE_RECEIPT"
            assert body["idempotent_replay"] is False

            s = _stock(db, ing.id)
            assert s.on_hand_quantity == Decimal("150.000")

            mv = db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type="PURCHASE_RECEIPT"
            ).one()
            assert mv.quantity_delta_on_hand == Decimal("50.000")
            assert mv.quantity_delta_reserved == Decimal("0")
            assert mv.actor_user_id is not None
        finally:
            cleanup_ingredient(db, ing.id)

    def test_receipt_writes_audit_record(self, db, owner):
        ing, _ = make_ingredient(db, on_hand=Decimal("10.000"))
        try:
            owner.post(
                "/inventory/purchase-receipts",
                json={"ingredient_id": ing.id, "quantity": "5.000"},
                headers=_key(),
            )
            log = db.query(AuditLog).filter_by(
                entity_type="inventory", entity_id=ing.id, action="INVENTORY_RECEIVED"
            ).first()
            assert log is not None
            assert log.actor_type == "STAFF"
            # The raw idempotency key must never reach the audit trail.
            assert "idempotency_key" not in (log.payload_after or {})
        finally:
            cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# Manual adjustment
# ---------------------------------------------------------------------------

class TestManualAdjustment:

    def test_positive_adjustment_increases_on_hand(self, db, owner):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = owner.post(
                "/inventory/manual-adjustments",
                json={"ingredient_id": ing.id, "delta": "10.000",
                      "reason": "sayim fazlasi"},
                headers=_key(),
            )
            assert r.status_code == 200, r.text
            assert Decimal(r.json()["on_hand_quantity"]) == Decimal("110.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_negative_adjustment_decreases_on_hand(self, db, owner):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = owner.post(
                "/inventory/manual-adjustments",
                json={"ingredient_id": ing.id, "delta": "-10.000",
                      "reason": "sayim eksigi"},
                headers=_key(),
            )
            assert r.status_code == 200, r.text
            assert Decimal(r.json()["on_hand_quantity"]) == Decimal("90.000")

            mv = db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type="MANUAL_ADJUSTMENT"
            ).one()
            assert mv.quantity == Decimal("10.000"), "quantity is always the magnitude"
            assert mv.quantity_delta_on_hand == Decimal("-10.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_negative_adjustment_cannot_break_a_reservation(self, db, owner, client):
        """
        A write-off must not push physical stock below what open orders are
        already counting on. The customer at table 4 is waiting for that batter.
        """
        from tests.conftest import order_payload

        ing, _ = make_ingredient(
            db, on_hand=Decimal("100.000"), standard_quantity=Decimal("90.00")
        )
        try:
            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            assert client.post("/public/orders/", json=payload, headers=headers).status_code == 200
            assert _stock(db, ing.id).reserved_quantity == Decimal("90.000")

            # Only 10 g is unreserved; writing off 20 g would break the promise.
            r = owner.post(
                "/inventory/manual-adjustments",
                json={"ingredient_id": ing.id, "delta": "-20.000",
                      "reason": "dokuldu"},
                headers=_key(),
            )
            assert r.status_code == 409
            assert r.json()["detail"]["error"] == "insufficient_on_hand"

            s = _stock(db, ing.id)
            assert s.on_hand_quantity == Decimal("100.000"), "refused, so unchanged"

            # Writing off exactly the unreserved 10 g IS allowed.
            r2 = owner.post(
                "/inventory/manual-adjustments",
                json={"ingredient_id": ing.id, "delta": "-10.000",
                      "reason": "dokuldu"},
                headers=_key(),
            )
            assert r2.status_code == 200, r2.text
            assert Decimal(r2.json()["on_hand_quantity"]) == Decimal("90.000")
            assert Decimal(r2.json()["available_quantity"]) == Decimal("0")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_adjustment_requires_a_reason(self, db, owner):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = owner.post(
                "/inventory/manual-adjustments",
                json={"ingredient_id": ing.id, "delta": "10.000", "reason": ""},
                headers=_key(),
            )
            assert r.status_code == 422
        finally:
            cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# Waste
# ---------------------------------------------------------------------------

class TestWaste:

    def test_waste_decreases_on_hand_and_records_reason(self, db, owner):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = owner.post(
                "/inventory/waste",
                json={"ingredient_id": ing.id, "quantity": "5.000",
                      "reason": "yanmis hamur"},
                headers=_key(),
            )
            assert r.status_code == 200, r.text
            assert Decimal(r.json()["on_hand_quantity"]) == Decimal("95.000")

            mv = db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type="WASTE"
            ).one()
            assert mv.reason == "yanmis hamur"
            assert mv.quantity_delta_on_hand == Decimal("-5.000")
            assert mv.quantity_delta_reserved == Decimal("0")
            assert mv.actor_user_id is not None

            # Waste stays visible AS waste — never folded into consumption, or
            # the owner could never see what the shop is throwing away.
            assert db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type="CONSUMPTION"
            ).count() == 0
        finally:
            cleanup_ingredient(db, ing.id)

    def test_waste_requires_a_reason(self, db, owner):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = owner.post(
                "/inventory/waste",
                json={"ingredient_id": ing.id, "quantity": "5.000", "reason": ""},
                headers=_key(),
            )
            assert r.status_code == 422
        finally:
            cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestManualIdempotency:

    def test_missing_key_is_rejected(self, db, owner):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = owner.post(
                "/inventory/purchase-receipts",
                json={"ingredient_id": ing.id, "quantity": "5.000"},
            )
            assert r.status_code == 400
            assert r.json()["detail"]["error"] == "idempotency_required"
            assert _stock(db, ing.id).on_hand_quantity == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_same_key_same_payload_replays_once(self, db, owner):
        """A retried receipt must not deliver the goods twice."""
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            headers = _key()
            body = {"ingredient_id": ing.id, "quantity": "25.000",
                    "reason": "teslimat"}

            r1 = owner.post("/inventory/purchase-receipts", json=body, headers=headers)
            r2 = owner.post("/inventory/purchase-receipts", json=body, headers=headers)

            assert r1.status_code == r2.status_code == 200
            assert r1.json()["movement_id"] == r2.json()["movement_id"]
            assert r1.json()["idempotent_replay"] is False
            assert r2.json()["idempotent_replay"] is True

            assert _stock(db, ing.id).on_hand_quantity == Decimal("125.000")
            assert db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type="PURCHASE_RECEIPT"
            ).count() == 1, "replay must not append a second ledger row"
        finally:
            cleanup_ingredient(db, ing.id)

    def test_same_key_different_payload_returns_409(self, db, owner):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            headers = _key()
            r1 = owner.post(
                "/inventory/purchase-receipts",
                json={"ingredient_id": ing.id, "quantity": "25.000"},
                headers=headers,
            )
            assert r1.status_code == 200

            r2 = owner.post(
                "/inventory/purchase-receipts",
                json={"ingredient_id": ing.id, "quantity": "999.000"},
                headers=headers,
            )
            assert r2.status_code == 409
            assert r2.json()["detail"]["error"] == "idempotency_mismatch"

            # The second, different intent was refused — not silently applied.
            assert _stock(db, ing.id).on_hand_quantity == Decimal("125.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_raw_key_is_never_stored(self, db, owner):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            raw = uuid.uuid4().hex
            owner.post(
                "/inventory/purchase-receipts",
                json={"ingredient_id": ing.id, "quantity": "5.000"},
                headers={"Idempotency-Key": raw},
            )
            mv = db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type="PURCHASE_RECEIPT"
            ).one()
            assert mv.idempotency_key_hash is not None
            assert mv.idempotency_key_hash != raw, "only the SHA-256 hash is stored"
            assert len(mv.idempotency_key_hash) == 64
            assert mv.request_hash is not None
        finally:
            cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

class TestInventoryPermissions:

    def test_unauthenticated_is_rejected(self, db, client):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            assert client.get("/inventory/stock").status_code == 401
            r = client.post(
                "/inventory/waste",
                json={"ingredient_id": ing.id, "quantity": "1", "reason": "x"},
                headers=_key(),
            )
            assert r.status_code == 401
            assert _stock(db, ing.id).on_hand_quantity == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_cashier_cannot_read_or_adjust_inventory(self, db, make_staff):
        """A cashier handles money, not stock. No inventory permission at all."""
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        cashier = make_authed_client(db, make_staff("CASHIER", store_id=1))
        try:
            assert cashier.get("/inventory/stock").status_code == 403

            r = cashier.post(
                "/inventory/manual-adjustments",
                json={"ingredient_id": ing.id, "delta": "-50", "reason": "x"},
                headers=_key(),
            )
            assert r.status_code == 403
            assert _stock(db, ing.id).on_hand_quantity == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_kitchen_can_read_but_not_adjust(self, db, make_staff):
        """A cook may see what is left, but may not rewrite the count."""
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        kitchen = make_authed_client(db, make_staff("KITCHEN", store_id=1))
        try:
            assert kitchen.get("/inventory/stock").status_code == 200

            for path, body in (
                ("/inventory/waste",
                 {"ingredient_id": ing.id, "quantity": "5", "reason": "yandi"}),
                ("/inventory/manual-adjustments",
                 {"ingredient_id": ing.id, "delta": "5", "reason": "sayim"}),
                ("/inventory/purchase-receipts",
                 {"ingredient_id": ing.id, "quantity": "5"}),
            ):
                r = kitchen.post(path, json=body, headers=_key())
                assert r.status_code == 403, f"KITCHEN must not be able to POST {path}"

            assert _stock(db, ing.id).on_hand_quantity == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_csrf_token_required_for_mutations(self, db, make_staff):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        owner_client = make_authed_client(db, make_staff("OWNER", store_id=1))
        try:
            headers = _key()
            headers["X-CSRF-Token"] = "forged-token"
            r = owner_client.post(
                "/inventory/waste",
                json={"ingredient_id": ing.id, "quantity": "5", "reason": "yandi"},
                headers=headers,
            )
            assert r.status_code == 403
            assert r.json()["detail"]["error"] == "csrf_invalid"
            assert _stock(db, ing.id).on_hand_quantity == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_untrusted_origin_is_rejected(self, db, make_staff):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        owner_client = make_authed_client(db, make_staff("OWNER", store_id=1))
        try:
            headers = _key()
            headers["Origin"] = "https://evil.example.com"
            r = owner_client.post(
                "/inventory/waste",
                json={"ingredient_id": ing.id, "quantity": "5", "reason": "yandi"},
                headers=headers,
            )
            assert r.status_code == 403
            assert r.json()["detail"]["error"] == "origin_rejected"
            assert _stock(db, ing.id).on_hand_quantity == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

class TestInventoryReads:

    def test_stock_read_exposes_all_three_quantities(self, db, owner, client):
        from tests.conftest import order_payload

        ing, _ = make_ingredient(
            db, on_hand=Decimal("100.000"), standard_quantity=Decimal("10.00")
        )
        try:
            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            client.post("/public/orders/", json=payload, headers=headers)

            r = owner.get("/inventory/stock")
            assert r.status_code == 200
            row = next(i for i in r.json()["items"] if i["ingredient_id"] == ing.id)
            assert Decimal(row["on_hand_quantity"]) == Decimal("100.000")
            assert Decimal(row["reserved_quantity"]) == Decimal("10.000")
            assert Decimal(row["available_quantity"]) == Decimal("90.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_movements_read_returns_the_ledger(self, db, owner):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            owner.post(
                "/inventory/waste",
                json={"ingredient_id": ing.id, "quantity": "5.000", "reason": "yandi"},
                headers=_key(),
            )
            r = owner.get(f"/inventory/movements?ingredient_id={ing.id}")
            assert r.status_code == 200
            types = [m["movement_type"] for m in r.json()["items"]]
            assert "WASTE" in types
        finally:
            cleanup_ingredient(db, ing.id)
