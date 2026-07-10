"""
Role-based access control matrix tests for OWNER, MANAGER, KITCHEN, CASHIER.

401 = unauthenticated; 403 = authenticated but insufficient permission.
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
    order_payload,
)

client = TestClient(app)


def _client_for(db, make_staff, role):
    return make_authed_client(db, make_staff(role, store_id=DEFAULT_STORE_ID))


def _make_order(db):
    ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
    payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
    r = client.post("/public/orders/", json=payload, headers=headers)
    return ing, r.json()["order_id"]


# ---------------------------------------------------------------------------
# Owner endpoint access
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role,expected", [
    ("OWNER", 200),
    ("MANAGER", 200),
    ("KITCHEN", 403),
    ("CASHIER", 403),
])
def test_owner_kpis_access_by_role(db, make_staff, role, expected):
    c = _client_for(db, make_staff, role)
    assert c.get("/owner/kpis").status_code == expected


@pytest.mark.parametrize("role,expected", [
    ("OWNER", 200),
    ("MANAGER", 200),
    ("KITCHEN", 403),
    ("CASHIER", 403),
])
def test_owner_decisions_access_by_role(db, make_staff, role, expected):
    c = _client_for(db, make_staff, role)
    assert c.get("/owner/decisions/").status_code == expected


# ---------------------------------------------------------------------------
# Kitchen endpoint access
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role,expected", [
    ("OWNER", 200),
    ("MANAGER", 200),
    ("KITCHEN", 200),
    ("CASHIER", 403),
])
def test_kitchen_dashboard_access_by_role(db, make_staff, role, expected):
    c = _client_for(db, make_staff, role)
    assert c.get("/kitchen/orders/").status_code == expected


def test_kitchen_can_update_order_status(db, make_staff):
    ing, oid = _make_order(db)
    c = _client_for(db, make_staff, "KITCHEN")
    r = c.patch(f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"})
    assert r.status_code == 200
    cleanup_ingredient(db, ing.id)


def test_cashier_cannot_update_kitchen_status(db, make_staff):
    ing, oid = _make_order(db)
    c = _client_for(db, make_staff, "CASHIER")
    r = c.patch(f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"})
    assert r.status_code == 403
    cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# 401 vs 403
# ---------------------------------------------------------------------------

def test_unauthenticated_request_is_401():
    assert client.get("/owner/kpis").status_code == 401
    assert client.get("/kitchen/orders/").status_code == 401


def test_authenticated_insufficient_role_is_403(db, make_staff):
    c = _client_for(db, make_staff, "KITCHEN")
    assert c.get("/owner/kpis").status_code == 403


def test_cashier_permissions_are_payments_only(db, make_staff):
    """CASHIER may read bills and collect payments — but never refund, and no
    owner/kitchen write access."""
    from app.core.permissions import (
        permissions_for_role,
        PERM_PAYMENTS_READ,
        PERM_PAYMENTS_COLLECT,
        PERM_PAYMENTS_REFUND,
        PERM_OWNER_READ,
        PERM_KITCHEN_ORDERS_WRITE,
    )
    perms = set(permissions_for_role("CASHIER"))
    assert perms == {PERM_PAYMENTS_READ, PERM_PAYMENTS_COLLECT}
    assert PERM_PAYMENTS_REFUND not in perms
    assert PERM_OWNER_READ not in perms
    assert PERM_KITCHEN_ORDERS_WRITE not in perms
