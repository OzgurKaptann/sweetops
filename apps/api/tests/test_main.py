"""
Smoke tests — fast sanity checks for every public endpoint.

These do not verify business logic (that's in test_rollback/state_machine/etc).
They verify: routes are reachable, responses have the expected shape,
and required fields are present.
"""
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import cleanup_ingredient, make_ingredient, order_payload

client = TestClient(app)


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "service" in body


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

def test_public_menu_shape():
    r = client.get("/public/menu/")
    assert r.status_code == 200
    body = r.json()
    assert "products" in body
    assert "ingredients" in body
    assert "categories" in body
    assert isinstance(body["products"], list)
    assert isinstance(body["ingredients"], list)
    assert isinstance(body["categories"], list)


def test_public_menu_categories_have_ingredients():
    r = client.get("/public/menu/")
    assert r.status_code == 200
    for cat in r.json()["categories"]:
        assert "name" in cat
        assert "ingredients" in cat
        assert isinstance(cat["ingredients"], list)


# ---------------------------------------------------------------------------
# Order creation
# ---------------------------------------------------------------------------

def test_order_creation_requires_ingredients(db):
    """New service rejects orders with no ingredient selections."""
    ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))

    payload = {
        "store_id": 1,
        "table_id": 1,
        "items": [{"product_id": 1, "quantity": 1, "ingredients": []}],
    }
    r = client.post("/public/orders/", json=payload,
                    headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 422

    cleanup_ingredient(db, ing.id)


def test_order_creation_success(db):
    ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))

    payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
    r = client.post("/public/orders/", json=payload, headers=headers)

    assert r.status_code == 200
    body = r.json()
    assert "order_id" in body
    assert body["status"] == "NEW"
    assert body["store_id"] == 1
    assert "total_amount" in body

    cleanup_ingredient(db, ing.id)


def test_order_creation_missing_items_field():
    """Pydantic must reject payloads missing the `items` field."""
    r = client.post("/public/orders/", json={"store_id": 1, "table_id": 1})
    assert r.status_code == 422


def test_order_creation_invalid_status_value():
    """Pydantic must reject unknown status strings."""
    r = client.patch("/kitchen/orders/99999/status", json={"status": "FLYING"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Kitchen
# ---------------------------------------------------------------------------

def test_kitchen_orders_returns_dashboard():
    r = client.get("/kitchen/orders/?store_id=1")
    assert r.status_code == 200
    body = r.json()
    assert "orders" in body
    assert "kitchen_load" in body
    assert "batching_suggestions" in body
    assert isinstance(body["orders"], list)
    assert isinstance(body["batching_suggestions"], list)
    load = body["kitchen_load"]
    assert load["load_level"] in ("low", "medium", "high")


def test_kitchen_orders_shape(db):
    ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
    payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
    client.post("/public/orders/", json=payload, headers=headers)

    r = client.get("/kitchen/orders/?store_id=1")
    assert r.status_code == 200
    orders = r.json()["orders"]
    assert len(orders) >= 1

    order = orders[0]
    for field in ("id", "store_id", "status", "created_at", "items",
                  "should_be_started", "urgency_reason", "action_hint",
                  "sla_severity", "priority_score", "computed_age_minutes"):
        assert field in order, f"Missing field: {field}"

    cleanup_ingredient(db, ing.id)


def test_kitchen_nonexistent_order_returns_404():
    r = client.patch("/kitchen/orders/999999999/status", json={"status": "IN_PREP"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Owner analytics
# ---------------------------------------------------------------------------

def test_owner_kpis_shape():
    r = client.get("/owner/kpis")
    assert r.status_code == 200
    body = r.json()
    assert "kpis" in body
    kpis = body["kpis"]
    for field in ("total_orders", "gross_revenue", "average_order_value",
                  "active_orders_count", "delivered_orders_count"):
        assert field in kpis, f"KPI missing: {field}"


def test_owner_daily_sales_shape():
    r = client.get("/owner/daily-sales")
    assert r.status_code == 200
    body = r.json()
    assert "points" in body
    assert isinstance(body["points"], list)


def test_owner_hourly_demand_shape():
    r = client.get("/owner/hourly-demand")
    assert r.status_code == 200
    body = r.json()
    assert "points" in body


def test_owner_top_ingredients_shape():
    r = client.get("/owner/top-ingredients")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert isinstance(body["items"], list)


def test_owner_stock_status_shape():
    r = client.get("/owner/stock-status")
    assert r.status_code == 200
    body = r.json()
    assert "total" in body
    assert "items" in body


def test_owner_forecast_shape():
    r = client.get("/owner/ingredient-forecast")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert isinstance(body["items"], list)


def test_owner_insights_critical_alerts():
    r = client.get("/owner/insights/critical-alerts")
    assert r.status_code == 200
    body = r.json()
    assert "alerts" in body


def test_owner_insights_prep_time():
    r = client.get("/owner/insights/prep-time")
    assert r.status_code == 200


def test_owner_insights_trending():
    r = client.get("/owner/insights/trending-ingredients")
    assert r.status_code == 200


def test_owner_insights_combos():
    r = client.get("/owner/insights/popular-combos")
    assert r.status_code == 200


def test_owner_insights_value_summary():
    r = client.get("/owner/insights/value-summary")
    assert r.status_code == 200
    body = r.json()
    assert "weekly_revenue" in body
