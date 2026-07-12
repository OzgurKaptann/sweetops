"""
Two-store data isolation tests.

Store A staff must never read or mutate Store B data. Client-supplied store
values must never override the session store. Inventory is store-scoped, so
owner inventory views serve each store its own figures rather than failing
closed once a second branch opens.
"""
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
)

client = TestClient(app)


def _order_in_store(db, store_id: int, backdate_minutes: float | None = None):
    """
    Create an order (legacy path) in a specific store; optionally backdate it.

    The ingredient is stocked IN THAT STORE. Stock is store-scoped, so a store
    with no stock row of its own cannot fulfil an order — there is deliberately
    no fallback to another branch's shelves, and an order placed in a store that
    was never stocked is correctly rejected as out of stock.
    """
    ing, _ = make_ingredient(db, on_hand=Decimal("200.00"), store_id=store_id)
    payload = {
        "store_id": store_id,
        "items": [{
            "product_id": 1,
            "quantity": 1,
            "ingredients": [{"ingredient_id": ing.id, "quantity": 1}],
        }],
    }
    r = client.post("/public/orders/", json=payload, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 200, r.json()
    oid = r.json()["order_id"]
    if backdate_minutes is not None:
        from datetime import datetime, timedelta, timezone
        from app.models.order import Order
        target = datetime.now(timezone.utc) - timedelta(minutes=backdate_minutes)
        db.query(Order).filter(Order.id == oid).update({"created_at": target}, synchronize_session=False)
        db.commit()
    return ing, oid


# ---------------------------------------------------------------------------
# Kitchen order isolation
# ---------------------------------------------------------------------------

def test_kitchen_users_see_only_their_store_orders(db, make_staff, make_store):
    store_b = make_store()
    ing_a, oid_a = _order_in_store(db, DEFAULT_STORE_ID)
    ing_b, oid_b = _order_in_store(db, store_b.id)

    ca = make_authed_client(db, make_staff("KITCHEN", store_id=DEFAULT_STORE_ID))
    cb = make_authed_client(db, make_staff("KITCHEN", store_id=store_b.id))

    ids_a = {o["id"] for o in ca.get("/kitchen/orders/").json()["orders"]}
    ids_b = {o["id"] for o in cb.get("/kitchen/orders/").json()["orders"]}

    assert oid_a in ids_a and oid_a not in ids_b
    assert oid_b in ids_b and oid_b not in ids_a

    cleanup_ingredient(db, ing_a.id)
    cleanup_ingredient(db, ing_b.id)


def test_store_a_cannot_mutate_store_b_order(db, make_staff, make_store):
    store_b = make_store()
    ing_b, oid_b = _order_in_store(db, store_b.id)

    ca = make_authed_client(db, make_staff("KITCHEN", store_id=DEFAULT_STORE_ID))
    r = ca.patch(f"/kitchen/orders/{oid_b}/status", json={"status": "IN_PREP"})
    assert r.status_code == 404  # non-disclosing

    cleanup_ingredient(db, ing_b.id)


def test_client_supplied_store_id_cannot_override_session(db, make_staff, make_store):
    store_b = make_store()
    ing_a, oid_a = _order_in_store(db, DEFAULT_STORE_ID)
    ing_b, oid_b = _order_in_store(db, store_b.id)

    ca = make_authed_client(db, make_staff("KITCHEN", store_id=DEFAULT_STORE_ID))
    # attempt to force store B via query param — must be ignored
    ids = {o["id"] for o in ca.get("/kitchen/orders/?store_id=%d" % store_b.id).json()["orders"]}
    assert oid_a in ids
    assert oid_b not in ids

    cleanup_ingredient(db, ing_a.id)
    cleanup_ingredient(db, ing_b.id)


# ---------------------------------------------------------------------------
# Owner analytics isolation
# ---------------------------------------------------------------------------

def test_owner_kpis_count_only_authenticated_store(db, make_staff, make_store):
    # Two fresh stores → deterministic counts (no historical orders).
    store_a = make_store()
    store_b = make_store()
    ing_a, _ = _order_in_store(db, store_a.id)
    ing_b1, _ = _order_in_store(db, store_b.id)
    ing_b2, _ = _order_in_store(db, store_b.id)

    ca = make_authed_client(db, make_staff("OWNER", store_id=store_a.id))
    cb = make_authed_client(db, make_staff("OWNER", store_id=store_b.id))

    kpis_a = ca.get("/owner/kpis").json()["kpis"]
    kpis_b = cb.get("/owner/kpis").json()["kpis"]

    assert kpis_a["total_orders"] == 1
    assert kpis_b["total_orders"] == 2

    cleanup_ingredient(db, ing_a.id)
    cleanup_ingredient(db, ing_b1.id)
    cleanup_ingredient(db, ing_b2.id)


def test_daily_sales_scoped_to_store(db, make_staff, make_store):
    store_b = make_store()
    ing_b, _ = _order_in_store(db, store_b.id)

    cb = make_authed_client(db, make_staff("OWNER", store_id=store_b.id))
    points = cb.get("/owner/daily-sales").json()["points"]
    total = sum(p["total_orders"] for p in points)
    assert total >= 1  # only store B orders contributed

    # An owner in an empty brand-new store sees zero.
    store_c = make_store()
    cc = make_authed_client(db, make_staff("OWNER", store_id=store_c.id))
    points_c = cc.get("/owner/daily-sales").json()["points"]
    assert sum(p["total_orders"] for p in points_c) == 0

    cleanup_ingredient(db, ing_b.id)


def test_hourly_demand_scoped_to_store(db, make_staff, make_store):
    store_c = make_store()
    cc = make_authed_client(db, make_staff("OWNER", store_id=store_c.id))
    # brand-new store: no orders -> no hourly demand points
    assert cc.get("/owner/hourly-demand").json()["points"] == []


def test_top_ingredients_scoped_to_store(db, make_staff, make_store):
    store_c = make_store()
    cc = make_authed_client(db, make_staff("OWNER", store_id=store_c.id))
    assert cc.get("/owner/top-ingredients").json()["items"] == []


def test_metrics_layer_scoped_to_store(db, make_staff, make_store):
    store_c = make_store()
    cc = make_authed_client(db, make_staff("OWNER", store_id=store_c.id))
    body = cc.get("/owner/metrics/").json()
    # brand new store: no conversion data
    assert body["conversion"]["combo_usage_rate"]["quality"]["status"] in ("no_data", "low_sample")


# ---------------------------------------------------------------------------
# Owner decision isolation
# ---------------------------------------------------------------------------

def test_owner_decisions_stored_and_retrieved_by_store(db, make_staff, make_store):
    store_b = make_store()
    # SLA-breaching order only in store B
    ing_b, oid_b = _order_in_store(db, store_b.id, backdate_minutes=12)

    ca = make_authed_client(db, make_staff("OWNER", store_id=DEFAULT_STORE_ID))
    cb = make_authed_client(db, make_staff("OWNER", store_id=store_b.id))

    dec_b = cb.get("/owner/decisions/").json()["decisions"]
    dec_a = ca.get("/owner/decisions/").json()["decisions"]

    b_has_sla = any(d["type"] == "sla_risk" and oid_b in d["data"].get("critical_order_ids", []) for d in dec_b)
    assert b_has_sla, "Store B owner should see its own SLA risk"

    a_refs_b = any(oid_b in d["data"].get("critical_order_ids", []) for d in dec_a)
    assert not a_refs_b, "Store A owner must not see Store B's SLA order"

    cleanup_ingredient(db, ing_b.id)


def test_store_a_cannot_mutate_store_b_decision(db, make_staff, make_store):
    # Two fresh stores so neither carries historical decisions.
    store_a = make_store()
    store_b = make_store()
    ing_b, oid_b = _order_in_store(db, store_b.id, backdate_minutes=12)

    ca = make_authed_client(db, make_staff("OWNER", store_id=store_a.id))
    cb = make_authed_client(db, make_staff("OWNER", store_id=store_b.id))

    # Materialise store B's decision
    cb.get("/owner/decisions/")

    # Store A owner cannot acknowledge store B's decision -> 404 (does not exist in A)
    r = ca.patch("/owner/decisions/sla_risk_current", json={"action": "acknowledge"})
    assert r.status_code == 404

    # Store B owner can
    r2 = cb.patch("/owner/decisions/sla_risk_current", json={"action": "acknowledge"})
    assert r2.status_code == 200

    cleanup_ingredient(db, ing_b.id)


# ---------------------------------------------------------------------------
# Owner inventory views: store-scoped, and no longer fail-closed
# ---------------------------------------------------------------------------

def test_owner_inventory_views_work_with_multiple_stores(db, make_staff, make_store):
    """
    The inversion of the old global-inventory guard.

    These three endpoints used to return 409 "inventory_not_store_scoped" the
    moment a second branch was staffed — a real multi-branch owner simply could
    not see their stock. Physical stock now carries a store_id, so each owner
    gets their OWN branch's figures and a second store costs nobody anything.
    """
    store_b = make_store()
    ca = make_authed_client(db, make_staff("OWNER", store_id=DEFAULT_STORE_ID))
    make_staff("OWNER", store_id=store_b.id)  # a genuinely second operational store

    for path in ("/owner/stock-status",
                 "/owner/insights/critical-alerts",
                 "/owner/insights/value-summary"):
        r = ca.get(path)
        assert r.status_code == 200, f"{path} must not fail closed, got {r.status_code}"


def test_owner_stock_status_shows_only_own_store(db, make_staff, make_store):
    """Store A's owner sees Store A's quantity for an ingredient, not Store B's."""
    from tests.conftest import stock_for

    store_b = make_store()
    ing, _ = make_ingredient(db, on_hand=Decimal("111"), store_id=DEFAULT_STORE_ID)
    stock_for(db, ing, store_b.id, on_hand=Decimal("999"))

    ca = make_authed_client(db, make_staff("OWNER", store_id=DEFAULT_STORE_ID))
    cb = make_authed_client(db, make_staff("OWNER", store_id=store_b.id))

    def _on_hand(client) -> float | None:
        rows = client.get("/owner/stock-status").json()["items"]
        row = next((x for x in rows if x["ingredient_id"] == ing.id), None)
        return float(row["on_hand_quantity"]) if row else None

    # Same catalog ingredient, two branches, two independent physical truths.
    assert _on_hand(ca) == 111.0
    assert _on_hand(cb) == 999.0

    cleanup_ingredient(db, ing.id)
