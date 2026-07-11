"""
Audit resilience tests — prove business operations succeed even when
the audit subsystem fails.

Principle:
  audit() catches all exceptions internally and logs them.
  It must never propagate an error to the caller.

Scenarios:
  1. audit() raises during order creation → order still commits, stock deducted.
  2. audit() raises during status transition → transition still commits.
  3. audit() raises during cancellation stock return → stock still returned.
  4. Verify the error is logged (caplog), not silently swallowed.
  5. Verify AuditLog rows ARE written when audit() works normally.
"""
import logging
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

from tests.conftest import cleanup_ingredient, make_ingredient, order_payload


def _post_order(client, ingredient_id: int) -> tuple[int, dict]:
    payload, headers = order_payload(ingredient_id, idem_key=uuid.uuid4().hex)
    r = client.post("/public/orders/", json=payload, headers=headers)
    return r.status_code, r.json()


def _patch_status(kitchen_client, order_id: int, status: str):
    return kitchen_client.patch(
        f"/kitchen/orders/{order_id}/status",
        json={"status": status},
    )


class TestAuditFailureDoesNotBlockOrderCreation:

    def test_order_succeeds_when_audit_raises(self, db, client, kitchen_client):
        """
        If audit() raises an unhandled exception, order creation must still
        return 200 and the order must be persisted.
        """
        ing, _ = make_ingredient(db, on_hand=Decimal("50.00"))

        with patch("app.services.order_service.audit", side_effect=RuntimeError("DB audit unavailable")):
            status, body = _post_order(client, ing.id)

        assert status == 200, f"Order must succeed despite audit failure. Got {status}: {body}"
        assert "order_id" in body
        assert body["status"] == "NEW"

        cleanup_ingredient(db, ing.id)

    def test_reservation_persists_when_audit_raises(self, db, client, kitchen_client):
        """
        The inventory reservation must commit atomically even when audit() fails.
        """
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("50.00")
        std_qty = Decimal("10.00")
        ing, _ = make_ingredient(
            db,
            on_hand=initial,
            standard_quantity=std_qty,
        )

        with patch("app.services.order_service.audit", side_effect=RuntimeError("audit down")):
            status, _ = _post_order(client, ing.id)

        assert status == 200

        db.expire_all()
        stock_after = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert stock_after.reserved_quantity == std_qty, (
            f"Stock must be reserved even when audit fails. "
            f"Expected reserved {std_qty}, got {stock_after.reserved_quantity}"
        )
        assert stock_after.on_hand_quantity == initial, (
            "Order creation reserves; it must not consume physical stock"
        )

        cleanup_ingredient(db, ing.id)

    def test_audit_failure_is_logged_not_silently_swallowed(self, db, client, kitchen_client, caplog):
        """
        When audit() catches an exception, it must emit an ERROR log.
        Silent swallowing is a debugging trap.
        """
        ing, _ = make_ingredient(db, on_hand=Decimal("50.00"))

        # Patch the _internal_ flush that audit() calls so the error
        # reaches audit()'s except block and gets logged.
        with caplog.at_level(logging.ERROR, logger="app.services.audit_service"):
            with patch(
                "app.services.audit_service.AuditLog",
                side_effect=Exception("simulated audit table error"),
            ):
                status, _ = _post_order(client, ing.id)

        assert status == 200, "Order must succeed"
        assert any("audit_write_failed" in r.message for r in caplog.records), (
            "audit_write_failed must be logged at ERROR level when audit() fails. "
            f"Logged messages: {[r.message for r in caplog.records]}"
        )

        cleanup_ingredient(db, ing.id)


class TestAuditFailureDoesNotBlockStatusTransition:

    def test_status_transition_succeeds_when_audit_raises(self, db, client, kitchen_client):
        """
        Status transitions must succeed even if audit() fails.
        """
        ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        _, body = _post_order(client, ing.id)[0], _post_order(client, ing.id)[1]

        # Create a fresh order properly
        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        order_id = r.json()["order_id"]

        with patch("app.services.kitchen_service.audit", side_effect=RuntimeError("audit down")):
            r2 = _patch_status(kitchen_client, order_id, "IN_PREP")

        assert r2.status_code == 200, (
            f"Status transition must succeed despite audit failure. Got {r2.status_code}: {r2.json()}"
        )
        assert r2.json()["new_status"] == "IN_PREP"

        cleanup_ingredient(db, ing.id)

    def test_cancellation_release_succeeds_when_audit_raises(self, db, client, kitchen_client):
        """
        The reservation must be released on cancellation even if the audit()
        call inside the kitchen transition raises.
        """
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("50.00")
        ing, _ = make_ingredient(
            db,
            on_hand=initial,
            standard_quantity=Decimal("10.00"),
        )

        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        order_id = r.json()["order_id"]

        with patch("app.services.kitchen_service.audit", side_effect=RuntimeError("audit down")):
            r2 = _patch_status(kitchen_client, order_id, "CANCELLED")

        assert r2.status_code == 200, (
            f"Cancellation must succeed despite audit failure. Got {r2.status_code}: {r2.json()}"
        )

        db.expire_all()
        stock_after = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert stock_after.reserved_quantity == Decimal("0"), (
            f"Reservation must be released even when audit fails. "
            f"Got reserved {stock_after.reserved_quantity}"
        )
        assert stock_after.on_hand_quantity == initial, (
            "Cancelling an un-started order must not move physical stock"
        )
        assert stock_after.available_quantity == initial

        cleanup_ingredient(db, ing.id)


class TestAuditWritesWhenHealthy:

    def test_order_creation_writes_audit_record(self, db, client, kitchen_client):
        """
        When audit is healthy, order creation must produce an AuditLog record
        with action='created' and entity_type='order'.
        """
        from app.models.audit_log import AuditLog

        ing, _ = make_ingredient(db, on_hand=Decimal("50.00"))

        idem = uuid.uuid4().hex
        payload, headers = order_payload(ing.id, idem_key=idem)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 200
        order_id = r.json()["order_id"]

        log = (
            db.query(AuditLog)
            .filter_by(entity_type="order", entity_id=order_id, action="created")
            .first()
        )
        assert log is not None, "AuditLog record must exist for order creation"
        assert log.actor_type == "CUSTOMER"
        assert log.payload_after is not None
        # The raw idempotency key is deliberately NOT logged — an audit trail is
        # not a place to keep a replay credential.
        assert "idempotency_key" not in log.payload_after
        assert log.payload_after.get("store_id") is not None

        cleanup_ingredient(db, ing.id)

    def test_status_change_writes_audit_record(self, db, client, kitchen_client):
        """
        Status transitions must produce AuditLog records with correct before/after.
        """
        from app.models.audit_log import AuditLog

        ing, _ = make_ingredient(db, on_hand=Decimal("50.00"))

        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        order_id = r.json()["order_id"]

        _patch_status(kitchen_client, order_id, "IN_PREP")

        log = (
            db.query(AuditLog)
            .filter_by(entity_type="order", entity_id=order_id, action="status_changed")
            .first()
        )
        assert log is not None, "AuditLog record must exist for status_changed"
        assert log.payload_before == {"status": "NEW", "order_id": order_id}
        assert log.payload_after == {"status": "IN_PREP"}
        assert log.actor_type == "STAFF"

        cleanup_ingredient(db, ing.id)

    def test_cancellation_writes_reservation_released_audit(self, db, client, kitchen_client):
        """
        Cancelling an un-started order produces an INVENTORY_RESERVATION_RELEASED
        audit record attributed to the staff member who cancelled it.
        """
        from app.models.audit_log import AuditLog

        ing, _ = make_ingredient(db, on_hand=Decimal("50.00"))

        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        order_id = r.json()["order_id"]

        _patch_status(kitchen_client, order_id, "CANCELLED")

        log = (
            db.query(AuditLog)
            .filter_by(
                entity_type="inventory",
                entity_id=order_id,
                action="INVENTORY_RESERVATION_RELEASED",
            )
            .first()
        )
        assert log is not None, (
            "INVENTORY_RESERVATION_RELEASED AuditLog must exist after cancellation"
        )
        assert log.actor_type == "STAFF"
        assert log.actor_id is not None, "A staff cancellation must name its actor"

        cleanup_ingredient(db, ing.id)

    def test_start_prep_writes_consumed_audit(self, db, client, kitchen_client):
        """Starting preparation produces an INVENTORY_CONSUMED audit record."""
        from app.models.audit_log import AuditLog

        ing, _ = make_ingredient(db, on_hand=Decimal("50.00"))

        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        order_id = r.json()["order_id"]

        _patch_status(kitchen_client, order_id, "IN_PREP")

        log = (
            db.query(AuditLog)
            .filter_by(
                entity_type="inventory",
                entity_id=order_id,
                action="INVENTORY_CONSUMED",
            )
            .first()
        )
        assert log is not None, "INVENTORY_CONSUMED AuditLog must exist after start-prep"
        assert log.actor_type == "STAFF"

        cleanup_ingredient(db, ing.id)

    def test_idempotent_retry_does_not_write_duplicate_audit(self, db, client, kitchen_client):
        """
        Submitting the same order twice (same key) must write exactly one
        audit record — not two.
        """
        from app.models.audit_log import AuditLog

        ing, _ = make_ingredient(db, on_hand=Decimal("50.00"))

        idem = uuid.uuid4().hex
        payload, headers = order_payload(ing.id, idem_key=idem)

        r1 = client.post("/public/orders/", json=payload, headers=headers)
        r2 = client.post("/public/orders/", json=payload, headers=headers)
        assert r1.json()["order_id"] == r2.json()["order_id"]

        order_id = r1.json()["order_id"]
        audit_count = (
            db.query(AuditLog)
            .filter_by(entity_type="order", entity_id=order_id, action="created")
            .count()
        )
        assert audit_count == 1, (
            f"Idempotent retry must not write duplicate audit records. Got {audit_count}"
        )

        cleanup_ingredient(db, ing.id)
