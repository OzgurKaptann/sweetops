"""
Audit actor integrity: the actor recorded for a staff mutation is always the
authenticated user. Client-supplied identity (X-Actor-Id header, request-body
actor_id) can never override it.
"""
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.audit_log import AuditLog
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
    order_payload,
)

client = TestClient(app)


def _make_order(db):
    ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
    payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
    r = client.post("/public/orders/", json=payload, headers=headers)
    return ing, r.json()["order_id"]


def _latest_status_audit(db, order_id):
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == "order",
            AuditLog.entity_id == order_id,
            AuditLog.action == "status_changed",
        )
        .order_by(AuditLog.id.desc())
        .first()
    )


def test_kitchen_mutation_audit_actor_is_authenticated_user(db, make_staff):
    ing, oid = _make_order(db)
    user = make_staff("KITCHEN", store_id=DEFAULT_STORE_ID)
    c = make_authed_client(db, user)

    r = c.patch(f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"})
    assert r.status_code == 200

    row = _latest_status_audit(db, oid)
    assert row is not None
    assert row.actor_id == str(user.id)
    assert row.actor_type == "STAFF"

    cleanup_ingredient(db, ing.id)


def test_x_actor_id_header_cannot_override_audit_actor(db, make_staff):
    ing, oid = _make_order(db)
    user = make_staff("KITCHEN", store_id=DEFAULT_STORE_ID)
    c = make_authed_client(db, user)

    r = c.patch(
        f"/kitchen/orders/{oid}/status",
        json={"status": "IN_PREP"},
        headers={"X-Actor-Id": "999999"},
    )
    assert r.status_code == 200

    row = _latest_status_audit(db, oid)
    assert row.actor_id == str(user.id)
    assert row.actor_id != "999999"

    cleanup_ingredient(db, ing.id)


def test_owner_decision_body_actor_cannot_override(db, make_staff, make_store):
    # Fresh store so the SLA decision starts clean (store 1 carries history).
    # Its stock must be its own: the order below reserves from THIS store.
    store = make_store()
    ing, _ = make_ingredient(db, on_hand=Decimal("100.00"), store_id=store.id)
    payload = {
        "store_id": store.id,
        "items": [{"product_id": 1, "quantity": 1,
                   "ingredients": [{"ingredient_id": ing.id, "quantity": 1}]}],
    }
    r = client.post("/public/orders/", json=payload, headers={"Idempotency-Key": uuid.uuid4().hex})
    oid = r.json()["order_id"]
    from datetime import datetime, timedelta, timezone
    from app.models.order import Order
    db.query(Order).filter(Order.id == oid).update(
        {"created_at": datetime.now(timezone.utc) - timedelta(minutes=12)},
        synchronize_session=False,
    )
    db.commit()

    user = make_staff("OWNER", store_id=store.id)
    c = make_authed_client(db, user)
    c.get("/owner/decisions/")  # materialise

    resp = c.patch(
        "/owner/decisions/sla_risk_current",
        json={"action": "acknowledge", "actor_id": "spoofed-owner"},
    )
    assert resp.status_code == 200
    assert resp.json()["actor_id"] == str(user.id)
    assert resp.json()["actor_id"] != "spoofed-owner"

    cleanup_ingredient(db, ing.id)
