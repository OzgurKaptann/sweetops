"""
An owner/manager can provision a branch's menu and tables — and only their own.

What these tests are actually defending
---------------------------------------
Migration ``a9e4c7b25d13`` made the customer menu fail closed: a product reaches a
guest only through a ``store_products`` publication row, and nothing was
backfilled (docs/CUSTOMER_MENU_SCOPING.md). Correct, and it left the shop with no
supported way to write those rows — RUNTIME_PRODUCT_GAP_REVIEW **F-13**, "there is
no way to onboard a shop". This module proves the new surface closes that gap
without reopening the hole the migration closed.

Two properties dominate:

  * **The branch is the session's branch.** Every assertion about isolation is
    about a request that CANNOT NAME another store — there is no store_id field
    anywhere in these payloads, and the schemas reject one outright.
  * **The setup surface and the guest's phone agree.** Publishing, withdrawing,
    switching off for the day and retiring chain-wide are each checked THROUGH
    the public menu, not merely against a row in a table. A publication API that
    writes rows the customer menu does not honour would pass a row-level test and
    fail a shop.
"""
import uuid
from decimal import Decimal

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.models.store_product import StoreProduct
from app.models.table import Table
from app.models.table_qr_token import QR_TOKEN_STATUS_ACTIVE, TableQrToken
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_product,
    cleanup_store_table,
    make_authed_client,
    make_product,
    make_store_table_token,
    offer_product,
    purge_menu_for_store,
)

public = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def menu_product_ids(qr_token: str) -> set[int]:
    """What a guest holding this QR code can actually see, right now."""
    r = public.post("/public/menu/resolve", json={"qr_token": qr_token})
    assert r.status_code == 200, r.text
    return {p["id"] for p in r.json()["products"]}


def setup_status(client: TestClient) -> dict:
    r = client.get("/owner/setup/status")
    assert r.status_code == 200, r.text
    return r.json()


def check(status: dict, key: str) -> dict:
    return next(c for c in status["checks"] if c["key"] == key)


def menu_rows(client: TestClient) -> dict[int, dict]:
    r = client.get("/owner/menu/products")
    assert r.status_code == 200, r.text
    return {item["product_id"]: item for item in r.json()["items"]}


def unique_name() -> str:
    return f"SetupProduct_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Authorization & store scope
# ---------------------------------------------------------------------------

def test_unauthenticated_setup_requests_are_401():
    """No session, no menu — for reads as well as writes."""
    assert public.get("/owner/setup/status").status_code == 401
    assert public.get("/owner/menu/products").status_code == 401
    assert public.get("/owner/tables").status_code == 401
    assert public.post("/owner/menu/products/1/publish").status_code == 401


def test_kitchen_and_cashier_cannot_reach_the_setup_surface(db, make_staff):
    """
    A cook cannot rewrite the menu and a cashier cannot rotate a QR sticker.

    Both roles are authenticated and both are legitimate members of staff; the
    refusal is about the named permission (``setup:read`` / ``setup:manage``),
    which neither holds, not about who they are.
    """
    for role in ("KITCHEN", "CASHIER"):
        c = make_authed_client(db, make_staff(role, store_id=DEFAULT_STORE_ID))
        assert c.get("/owner/setup/status").status_code == 403, role
        assert c.get("/owner/menu/products").status_code == 403, role
        assert c.get("/owner/tables").status_code == 403, role


def test_manager_may_use_the_setup_surface(db, make_staff):
    """MANAGER matches OWNER here: running a branch includes running its menu."""
    c = make_authed_client(db, make_staff("MANAGER", store_id=DEFAULT_STORE_ID))
    assert c.get("/owner/setup/status").status_code == 200
    assert c.get("/owner/menu/products").status_code == 200
    assert c.get("/owner/tables").status_code == 200


def test_a_storeless_owner_reaches_no_setup_data_at_all(db, make_staff):
    """
    Fail closed, in Turkish, with no data — and it fails EARLIER than this module.

    An operational role with no branch cannot hold a session at all
    (``auth_service.resolve_session`` refuses it), so the refusal here is a 401 and
    not the router's own 403. That is the stronger of the two outcomes and it is
    what this test pins: a menu belongs to a shop, and a storeless caller must not
    be answered with an empty list — which would read as "your menu is empty" and
    invite a publish that has nowhere to land — nor with somebody else's.

    The router's own ``no_store_assigned`` guard is the second line, exercised
    directly below.
    """
    c = make_authed_client(db, make_staff("OWNER", store_id=None))

    for method, path in (
        ("get", "/owner/setup/status"),
        ("get", "/owner/menu/products"),
        ("get", "/owner/tables"),
    ):
        r = getattr(c, method)(path)
        assert r.status_code == 401, f"{path}: {r.text}"
        detail = r.json()["detail"]
        # Turkish, and it leaks nothing about branches, products or tables.
        assert "Oturum" in detail["message"]
        assert "ürün" not in detail["message"]

    r = c.post("/owner/tables", json={"table_number": "1"})
    assert r.status_code == 401


def test_the_routers_own_no_store_guard_answers_in_turkish(db):
    """
    The second line of defence, tested directly.

    Session resolution already refuses a storeless operational account, so this
    guard is unreachable through a login today — exactly as the equivalent guard in
    routers/inventory.py is. It is kept, and tested, because the thing it protects
    against is a FUTURE role that is allowed to be storeless: the day one exists,
    the failure must be a Turkish refusal rather than a query with
    ``store_id = None`` silently returning another shop's rows.
    """
    from fastapi import HTTPException

    from app.routers.owner_setup import _store_id
    from app.services.auth_service import CurrentStaff

    storeless = CurrentStaff(
        user_id=1,
        username="storeless",
        role="OWNER",
        store_id=None,
        permissions=("setup:read", "setup:manage"),
        session_id=1,
        csrf_token_hash="x",
    )

    try:
        _store_id(storeless)
        raise AssertionError("a storeless staff context was accepted")
    except HTTPException as exc:
        assert exc.status_code == 403
        assert exc.detail["error"] == "no_store_assigned"
        assert "şube" in exc.detail["message"]
        # No internals and no English identifier in what a manager reads.
        assert "store" not in exc.detail["message"].lower()


def test_mutations_require_csrf_and_a_trusted_origin(db, make_staff):
    """
    The repo's mutation contract, applied here too.

    Publishing decides what a guest can order, so it is a state change and gets
    the same double-submit CSRF check and trusted-Origin check as every other
    state change — enforced by ``require_permission``, not re-implemented here.
    """
    user = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
    c = make_authed_client(db, user)
    product = make_product(db, offered_at_store_id=None)

    try:
        # No CSRF header at all.
        bare = TestClient(app)
        bare.cookies.set(
            settings.SESSION_COOKIE_NAME,
            c.cookies.get(settings.SESSION_COOKIE_NAME),
        )
        r = bare.post(f"/owner/menu/products/{product.id}/publish")
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "csrf_invalid"

        # A CSRF token that is not this session's.
        r = c.post(
            f"/owner/menu/products/{product.id}/publish",
            headers={"X-CSRF-Token": "not-the-session-token"},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "csrf_invalid"

        # An untrusted browser Origin, with a valid CSRF token.
        r = c.post(
            f"/owner/menu/products/{product.id}/publish",
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "origin_rejected"

        # Nothing was published by any of those attempts.
        assert menu_rows(c)[product.id]["published"] is False
    finally:
        cleanup_product(db, product.id)


# ---------------------------------------------------------------------------
# Reading the branch's publication state
# ---------------------------------------------------------------------------

def test_menu_list_shows_publication_state_for_this_branch_only(
    db, make_store, manager_client_factory
):
    """
    One catalog, two branches, two different answers about the same product.

    The catalog rows are identical for both callers — they are the chain's. The
    ``published`` / ``is_available`` / ``sort_order`` columns are the caller's
    branch's decision and nobody else's, so a product the other branch sells is
    indistinguishable here from one nobody sells.
    """
    other = make_store()
    purge_menu_for_store(db, other.id)

    a = manager_client_factory(DEFAULT_STORE_ID)
    b = manager_client_factory(other.id)

    only_a = make_product(db, offered_at_store_id=None)
    offer_product(db, DEFAULT_STORE_ID, only_a.id)

    try:
        rows_a = menu_rows(a)
        rows_b = menu_rows(b)

        # Both see the catalog row.
        assert only_a.id in rows_a and only_a.id in rows_b
        # Only the publishing branch sees a publication.
        assert rows_a[only_a.id]["published"] is True
        assert rows_a[only_a.id]["on_customer_menu"] is True
        assert rows_b[only_a.id]["published"] is False
        assert rows_b[only_a.id]["is_available"] is None
        assert rows_b[only_a.id]["sort_order"] is None
        assert rows_b[only_a.id]["on_customer_menu"] is False
    finally:
        cleanup_product(db, only_a.id)


def test_published_items_sort_before_unpublished_catalog_rows(db, owner_client):
    """
    The list is deterministic and puts the branch's own menu first.

    A screen whose rows move between two loads is a screen where a manager
    unpublishes the wrong item.
    """
    product = make_product(db, offered_at_store_id=None)
    try:
        owner_client.post(f"/owner/menu/products/{product.id}/publish")
        items = owner_client.get("/owner/menu/products").json()["items"]
        published_flags = [i["published"] for i in items]
        # Every published row precedes every unpublished one.
        assert published_flags == sorted(published_flags, reverse=True)
    finally:
        cleanup_product(db, product.id)


# ---------------------------------------------------------------------------
# Product creation
# ---------------------------------------------------------------------------

def test_created_product_does_not_leak_onto_any_menu(db, owner_client, make_store):
    """
    The central rule. Creating a catalog product publishes NOTHING.

    Automatically publishing new catalog rows everywhere is the exact shape that
    put eight ``TestWaffle`` rows one render away from a customer's phone. A
    product created here must be invisible to a guest in this branch and in every
    other branch.
    """
    store, table, _rec, raw = make_store_table_token(db)
    other = make_store()
    purge_menu_for_store(db, store.id)
    purge_menu_for_store(db, other.id)

    r = owner_client.post(
        "/owner/menu/products",
        json={"name": unique_name(), "category": "Waffle", "base_price": "120.00"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    product_id = body["product_id"]

    try:
        assert body["published"] is False
        assert body["on_customer_menu"] is False
        assert body["is_available"] is None

        # Not on the creating branch's guest menu…
        assert product_id not in menu_product_ids(raw)
        # …and not anywhere else either.
        assert (
            db.query(StoreProduct)
            .filter(StoreProduct.product_id == product_id)
            .count()
            == 0
        )
    finally:
        cleanup_product(db, product_id)
        cleanup_store_table(db, store.id, table.id)


def test_creation_can_publish_to_the_current_store_when_asked_explicitly(db):
    """
    The explicit opt-in, and it reaches only the CALLER'S branch.

    "Add my menu" is the honest first-run flow, so the flag exists — but it is a
    decision the manager took (a checkbox on the form), for one named branch.
    """
    store, table, _rec, raw = make_store_table_token(db)
    other_store, other_table, _r2, other_raw = make_store_table_token(db)
    purge_menu_for_store(db, store.id)
    purge_menu_for_store(db, other_store.id)

    client = _manager_for(db, store.id)
    product_id = None

    try:
        r = client.post(
            "/owner/menu/products",
            json={
                "name": unique_name(),
                "base_price": "95.00",
                "publish_to_current_store": True,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        product_id = body["product_id"]

        assert body["published"] is True
        assert body["is_available"] is True
        assert body["on_customer_menu"] is True
        assert body["store_id"] == store.id

        # The guest in THIS branch sees it…
        assert product_id in menu_product_ids(raw)
        # …and the guest in the other branch does not.
        assert product_id not in menu_product_ids(other_raw)
    finally:
        _cleanup_manager(db, store.id)
        if product_id is not None:
            cleanup_product(db, product_id)
        cleanup_store_table(db, store.id, table.id)
        cleanup_store_table(db, other_store.id, other_table.id)


def test_duplicate_product_name_is_refused(db, owner_client):
    """
    What makes a double-submitted create form safe without an idempotency key.

    The second POST finds the first product and is refused, rather than quietly
    minting a twin the manager then has to tell apart on a menu screen.
    """
    name = unique_name()
    r1 = owner_client.post(
        "/owner/menu/products", json={"name": name, "base_price": "50.00"}
    )
    assert r1.status_code == 200, r1.text
    product_id = r1.json()["product_id"]

    try:
        r2 = owner_client.post(
            "/owner/menu/products", json={"name": name, "base_price": "60.00"}
        )
        assert r2.status_code == 409
        detail = r2.json()["detail"]
        assert detail["error"] == "product_name_taken"
        assert "zaten var" in detail["message"]

        # Case-insensitively too — "Waffle" and "waffle" are one item on a board.
        r3 = owner_client.post(
            "/owner/menu/products", json={"name": name.upper(), "base_price": "60.00"}
        )
        assert r3.status_code == 409
    finally:
        cleanup_product(db, product_id)


def test_creation_rejects_an_empty_name_and_a_non_positive_price(db, owner_client):
    """A menu item a guest can order for nothing is a form submitted empty."""
    assert owner_client.post(
        "/owner/menu/products", json={"name": "   ", "base_price": "10.00"}
    ).status_code == 422
    assert owner_client.post(
        "/owner/menu/products", json={"name": unique_name(), "base_price": "0"}
    ).status_code == 422
    assert owner_client.post(
        "/owner/menu/products", json={"name": unique_name(), "base_price": "-5.00"}
    ).status_code == 422


def test_a_smuggled_store_id_is_rejected_not_ignored(db, owner_client, make_store):
    """
    ``extra="forbid"``: the request is refused, not quietly redirected.

    Silently ignoring the field would leave a client believing it had published
    onto another branch's menu and cheerfully told so.
    """
    other = make_store()
    r = owner_client.post(
        "/owner/menu/products",
        json={
            "name": unique_name(),
            "base_price": "10.00",
            "store_id": other.id,
        },
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Publish / unpublish, checked through the customer menu
# ---------------------------------------------------------------------------

def test_publishing_puts_the_product_on_this_branchs_customer_menu(db):
    """The whole point: a published product is what a guest can see and order."""
    store, table, _rec, raw = make_store_table_token(db)
    purge_menu_for_store(db, store.id)
    product = make_product(db, offered_at_store_id=None)
    client = _manager_for(db, store.id)

    try:
        assert product.id not in menu_product_ids(raw)

        r = client.post(f"/owner/menu/products/{product.id}/publish")
        assert r.status_code == 200, r.text
        assert r.json()["published"] is True
        assert r.json()["changed"] is True
        assert r.json()["on_customer_menu"] is True

        assert product.id in menu_product_ids(raw)
    finally:
        _cleanup_manager(db, store.id)
        cleanup_product(db, product.id)
        cleanup_store_table(db, store.id, table.id)


def test_publishing_twice_is_a_no_op_not_an_error(db):
    """A double-click must not become a second failure on screen."""
    store, table, _rec, raw = make_store_table_token(db)
    purge_menu_for_store(db, store.id)
    product = make_product(db, offered_at_store_id=None)
    client = _manager_for(db, store.id)

    try:
        first = client.post(f"/owner/menu/products/{product.id}/publish")
        second = client.post(f"/owner/menu/products/{product.id}/publish")
        assert first.status_code == second.status_code == 200
        assert first.json()["changed"] is True
        assert second.json()["changed"] is False
        # Still exactly one publication row.
        assert (
            db.query(StoreProduct)
            .filter(
                StoreProduct.store_id == store.id,
                StoreProduct.product_id == product.id,
            )
            .count()
            == 1
        )
    finally:
        _cleanup_manager(db, store.id)
        cleanup_product(db, product.id)
        cleanup_store_table(db, store.id, table.id)


def test_unpublishing_removes_the_product_from_the_customer_menu(db):
    """
    Withdrawal is immediate and total from the guest's point of view.

    The row is deleted, so the menu join has nothing to join to — the item is not
    filtered out of the guest's list, it is unreachable from it.
    """
    store, table, _rec, raw = make_store_table_token(db)
    purge_menu_for_store(db, store.id)
    product = make_product(db, offered_at_store_id=None)
    client = _manager_for(db, store.id)

    try:
        client.post(f"/owner/menu/products/{product.id}/publish")
        assert product.id in menu_product_ids(raw)

        r = client.post(f"/owner/menu/products/{product.id}/unpublish")
        assert r.status_code == 200, r.text
        assert r.json()["published"] is False
        assert r.json()["on_customer_menu"] is False

        assert product.id not in menu_product_ids(raw)
        assert (
            db.query(StoreProduct)
            .filter(
                StoreProduct.store_id == store.id,
                StoreProduct.product_id == product.id,
            )
            .count()
            == 0
        )

        # Idempotent: withdrawing again is a no-op, not a 404 to explain.
        again = client.post(f"/owner/menu/products/{product.id}/unpublish")
        assert again.status_code == 200
        assert again.json()["changed"] is False
    finally:
        _cleanup_manager(db, store.id)
        cleanup_product(db, product.id)
        cleanup_store_table(db, store.id, table.id)


def test_unavailable_product_disappears_but_keeps_its_publication(db):
    """
    "Sold out today" is not "we stopped selling this".

    The guest stops seeing it; the branch keeps its publication decision and its
    menu order, so tomorrow morning is one toggle rather than a re-publish.
    """
    store, table, _rec, raw = make_store_table_token(db)
    purge_menu_for_store(db, store.id)
    product = make_product(db, offered_at_store_id=None)
    client = _manager_for(db, store.id)

    try:
        client.post(f"/owner/menu/products/{product.id}/publish")
        client.patch(
            f"/owner/menu/products/{product.id}/sort-order", json={"sort_order": 7}
        )

        r = client.patch(
            f"/owner/menu/products/{product.id}/availability",
            json={"is_available": False},
        )
        assert r.status_code == 200, r.text
        assert r.json()["published"] is True
        assert r.json()["is_available"] is False
        assert r.json()["on_customer_menu"] is False
        assert product.id not in menu_product_ids(raw)

        # The publication decision and the menu position both survived.
        offering = (
            db.query(StoreProduct)
            .filter(
                StoreProduct.store_id == store.id,
                StoreProduct.product_id == product.id,
            )
            .one()
        )
        assert offering.is_available is False
        assert offering.sort_order == 7

        # Back on the board.
        back = client.patch(
            f"/owner/menu/products/{product.id}/availability",
            json={"is_available": True},
        )
        assert back.json()["on_customer_menu"] is True
        assert product.id in menu_product_ids(raw)
    finally:
        _cleanup_manager(db, store.id)
        cleanup_product(db, product.id)
        cleanup_store_table(db, store.id, table.id)


def test_deactivating_a_product_hides_it_even_where_it_is_published(db):
    """
    ``products.is_active`` retires an item CHAIN-WIDE and publication cannot
    override it.

    This is the case a row-level test would miss: the ``store_products`` row is
    still there, still available, and the guest still must not see the item.
    """
    store, table, _rec, raw = make_store_table_token(db)
    purge_menu_for_store(db, store.id)
    product = make_product(db, offered_at_store_id=None)
    client = _manager_for(db, store.id)

    try:
        client.post(f"/owner/menu/products/{product.id}/publish")
        assert product.id in menu_product_ids(raw)

        r = client.patch(
            f"/owner/menu/products/{product.id}", json={"is_active": False}
        )
        assert r.status_code == 200, r.text
        assert r.json()["published"] is True        # the row is still there…
        assert r.json()["is_available"] is True     # …and still "available"…
        assert r.json()["on_customer_menu"] is False  # …and the guest cannot see it.

        assert product.id not in menu_product_ids(raw)
        assert (
            db.query(StoreProduct)
            .filter(
                StoreProduct.store_id == store.id,
                StoreProduct.product_id == product.id,
            )
            .count()
            == 1
        )
    finally:
        _cleanup_manager(db, store.id)
        cleanup_product(db, product.id)
        cleanup_store_table(db, store.id, table.id)


def test_availability_and_sort_order_need_a_publication_first(db, owner_client):
    """
    A product this branch has not published has no availability to change.

    Inferring a publication from "mark it unavailable" would put an item on the
    menu by way of a button that says it is not.
    """
    product = make_product(db, offered_at_store_id=None)
    try:
        r = owner_client.patch(
            f"/owner/menu/products/{product.id}/availability",
            json={"is_available": True},
        )
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["error"] == "not_published"
        assert "yayında değil" in detail["message"]

        r = owner_client.patch(
            f"/owner/menu/products/{product.id}/sort-order", json={"sort_order": 1}
        )
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "not_published"
    finally:
        cleanup_product(db, product.id)


def test_sort_order_drives_the_customer_menu_order(db):
    """Menu order is the branch's, and the guest's list obeys it."""
    store, table, _rec, raw = make_store_table_token(db)
    purge_menu_for_store(db, store.id)
    first = make_product(db, name=f"AAA_{uuid.uuid4().hex[:6]}", offered_at_store_id=None)
    second = make_product(db, name=f"BBB_{uuid.uuid4().hex[:6]}", offered_at_store_id=None)
    client = _manager_for(db, store.id)

    try:
        client.post(f"/owner/menu/products/{first.id}/publish")
        client.post(f"/owner/menu/products/{second.id}/publish")

        client.patch(
            f"/owner/menu/products/{first.id}/sort-order", json={"sort_order": 10}
        )
        client.patch(
            f"/owner/menu/products/{second.id}/sort-order", json={"sort_order": 1}
        )

        r = public.post("/public/menu/resolve", json={"qr_token": raw})
        ordered = [p["id"] for p in r.json()["products"]]
        assert ordered.index(second.id) < ordered.index(first.id)
    finally:
        _cleanup_manager(db, store.id)
        cleanup_product(db, first.id)
        cleanup_product(db, second.id)
        cleanup_store_table(db, store.id, table.id)


def test_a_negative_sort_order_is_refused(db, owner_client):
    """A menu position is a place on a board, not a signed offset."""
    product = make_product(db, offered_at_store_id=None)
    try:
        owner_client.post(f"/owner/menu/products/{product.id}/publish")
        r = owner_client.patch(
            f"/owner/menu/products/{product.id}/sort-order", json={"sort_order": -1}
        )
        assert r.status_code == 422
    finally:
        cleanup_product(db, product.id)


def test_unknown_product_is_a_404_with_a_safe_message(db, owner_client):
    r = owner_client.post("/owner/menu/products/99999999/publish")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["error"] == "product_not_found"
    assert detail["message"] == "Böyle bir ürün bulunamadı."


# ---------------------------------------------------------------------------
# Cross-store isolation of the mutations themselves
# ---------------------------------------------------------------------------

def test_a_manager_cannot_publish_onto_another_branchs_menu(db, make_store):
    """
    The isolation that matters, and note HOW it is enforced.

    There is no request this manager can send that names the other branch: the
    store comes from the session and the schema rejects a smuggled ``store_id``.
    So the publish below lands on the CALLER'S menu, and the other branch's menu
    is untouched — the attack is not blocked by a check, it is unexpressible.
    """
    store_a, table_a, _ra, raw_a = make_store_table_token(db)
    store_b, table_b, _rb, raw_b = make_store_table_token(db)
    purge_menu_for_store(db, store_a.id)
    purge_menu_for_store(db, store_b.id)

    product = make_product(db, offered_at_store_id=None)
    client_a = _manager_for(db, store_a.id)

    try:
        client_a.post(f"/owner/menu/products/{product.id}/publish")

        assert product.id in menu_product_ids(raw_a)
        assert product.id not in menu_product_ids(raw_b)
        assert (
            db.query(StoreProduct)
            .filter(
                StoreProduct.product_id == product.id,
                StoreProduct.store_id == store_b.id,
            )
            .count()
            == 0
        )
    finally:
        _cleanup_manager(db, store_a.id)
        cleanup_product(db, product.id)
        cleanup_store_table(db, store_a.id, table_a.id)
        cleanup_store_table(db, store_b.id, table_b.id)


def test_a_manager_cannot_withdraw_another_branchs_publication(db):
    """
    Store A withdrawing "the product" cannot take it off Store B's menu.

    Same mechanism: the delete is filtered by the session's store id, so the other
    branch's row is not in scope at all.
    """
    store_a, table_a, _ra, raw_a = make_store_table_token(db)
    store_b, table_b, _rb, raw_b = make_store_table_token(db)
    purge_menu_for_store(db, store_a.id)
    purge_menu_for_store(db, store_b.id)

    shared = make_product(db, offered_at_store_id=None)
    offer_product(db, store_a.id, shared.id)
    offer_product(db, store_b.id, shared.id)
    client_a = _manager_for(db, store_a.id)

    try:
        client_a.post(f"/owner/menu/products/{shared.id}/unpublish")
        assert shared.id not in menu_product_ids(raw_a)
        # Store B still sells it.
        assert shared.id in menu_product_ids(raw_b)
    finally:
        _cleanup_manager(db, store_a.id)
        cleanup_product(db, shared.id)
        cleanup_store_table(db, store_a.id, table_a.id)
        cleanup_store_table(db, store_b.id, table_b.id)


# ---------------------------------------------------------------------------
# Tables & QR
# ---------------------------------------------------------------------------

def test_table_list_is_scoped_to_the_callers_branch(db, make_store, make_table):
    """A manager sees their own tables and cannot enumerate anyone else's."""
    store_a, table_a, _ra, _raw_a = make_store_table_token(db)
    other = make_store()
    other_table = make_table(other.id, "OtherBranchTable")
    client_a = _manager_for(db, store_a.id)

    try:
        body = client_a.get("/owner/tables").json()
        ids = {i["table_id"] for i in body["items"]}
        assert table_a.id in ids
        assert other_table.id not in ids
        assert body["store_id"] == store_a.id
        assert all(i["store_id"] == store_a.id for i in body["items"])
    finally:
        _cleanup_manager(db, store_a.id)
        cleanup_store_table(db, store_a.id, table_a.id)


def test_table_list_never_carries_a_raw_qr_token(db):
    """
    The security property this surface is built around.

    A raw token is stored only as a SHA-256 hash, so no listing endpoint can
    return one — not even by accident. What is here is the non-secret prefix,
    which identifies the printed sticker and cannot be scanned.
    """
    store, table, record, raw = make_store_table_token(db)
    client = _manager_for(db, store.id)

    try:
        body = client.get("/owner/tables").json()
        row = next(i for i in body["items"] if i["table_id"] == table.id)

        assert row["has_active_qr"] is True
        assert row["token_prefix"] == record.token_prefix
        assert "qr_url" not in row
        # The raw token appears nowhere in the serialized response.
        assert raw not in client.get("/owner/tables").text
    finally:
        _cleanup_manager(db, store.id)
        cleanup_store_table(db, store.id, table.id)


def test_creating_a_table_issues_a_scannable_qr_once(db, make_store):
    """
    A new table comes with a working sticker, and the link is shown exactly once.

    The URL must be the shape the customer app actually parses — the raw token in
    the URL *fragment* — or the printed sticker resolves to nothing.
    """
    store = make_store()
    client = _manager_for(db, store.id)

    try:
        r = client.post("/owner/tables", json={"table_number": "12"})
        assert r.status_code == 200, r.text
        body = r.json()

        assert body["table"]["table_number"] == "12"
        assert body["table"]["store_id"] == store.id
        assert body["table"]["has_active_qr"] is True

        qr = body["qr"]
        assert qr["previous_token_revoked"] is False
        assert qr["qr_url"].startswith(settings.CUSTOMER_WEB_BASE_URL.rstrip("/"))
        assert "/#qr=" in qr["qr_url"]
        # …and never as a query parameter, which would leak into access logs.
        assert "?qr=" not in qr["qr_url"]
        assert "yalnızca bir kez" in qr["notice"]

        # The link genuinely works: the public resolver accepts it.
        raw = qr["qr_url"].split("#qr=", 1)[1]
        resolved = public.post("/public/qr-context/resolve", json={"qr_token": raw})
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["store"]["id"] == store.id
        assert resolved.json()["table"]["id"] == body["table"]["table_id"]
    finally:
        _cleanup_manager(db, store.id)


def test_duplicate_table_number_in_one_branch_is_refused(db, make_store):
    """Two "Masa 3" stickers in one shop is an ambiguity a guest cannot resolve."""
    store = make_store()
    client = _manager_for(db, store.id)
    try:
        assert client.post("/owner/tables", json={"table_number": "3"}).status_code == 200
        r = client.post("/owner/tables", json={"table_number": "3"})
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "table_number_taken"
        # But another branch may of course have its own "Masa 3".
    finally:
        _cleanup_manager(db, store.id)


def test_renaming_a_table_keeps_its_qr_working(db):
    """
    A typo fix must not invalidate a printed sticker.

    Rotation is the operation that replaces a sticker, and it is deliberately not
    a side effect of a rename.
    """
    store, table, _rec, raw = make_store_table_token(db)
    client = _manager_for(db, store.id)

    try:
        r = client.patch(
            f"/owner/tables/{table.id}", json={"table_number": "Teras 1"}
        )
        assert r.status_code == 200, r.text
        row = next(i for i in r.json()["items"] if i["table_id"] == table.id)
        assert row["table_number"] == "Teras 1"
        assert row["display_name"] == "Masa Teras 1"

        # The old sticker still resolves.
        resolved = public.post("/public/qr-context/resolve", json={"qr_token": raw})
        assert resolved.status_code == 200
    finally:
        _cleanup_manager(db, store.id)
        cleanup_store_table(db, store.id, table.id)


def test_another_branchs_table_is_a_404_not_a_403(db, make_store, make_table):
    """A 403 would confirm the table exists somewhere. It must not."""
    store_a, table_a, _ra, _raw = make_store_table_token(db)
    other = make_store()
    other_table = make_table(other.id, "Gizli")
    client_a = _manager_for(db, store_a.id)

    try:
        for call in (
            lambda: client_a.patch(
                f"/owner/tables/{other_table.id}", json={"table_number": "X"}
            ),
            lambda: client_a.post(f"/owner/tables/{other_table.id}/rotate-qr"),
            lambda: client_a.post(f"/owner/tables/{other_table.id}/qr-token"),
        ):
            r = call()
            assert r.status_code == 404, r.text
            assert r.json()["detail"]["error"] == "table_not_found"

        # And nothing happened to it.
        db.expire_all()
        assert db.get(Table, other_table.id).table_number == "Gizli"
        assert (
            db.query(TableQrToken)
            .filter(TableQrToken.table_id == other_table.id)
            .count()
            == 0
        )
    finally:
        _cleanup_manager(db, store_a.id)
        cleanup_store_table(db, store_a.id, table_a.id)


def test_issuing_a_second_qr_for_a_table_is_refused(db):
    """One physical table, one trusted sticker. The manager is sent to rotate."""
    store, table, _rec, _raw = make_store_table_token(db)
    client = _manager_for(db, store.id)
    try:
        r = client.post(f"/owner/tables/{table.id}/qr-token")
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["error"] == "qr_token_already_active"
        assert "yenileyin" in detail["message"]
    finally:
        _cleanup_manager(db, store.id)
        cleanup_store_table(db, store.id, table.id)


def test_rotating_a_qr_invalidates_the_old_sticker(db):
    """
    Rotation is the destructive one, and it is destructive on purpose.

    The old printed code stops resolving immediately — which is exactly right for
    a photographed sticker and exactly wrong by accident, hence the warning the
    response carries.
    """
    store, table, _rec, old_raw = make_store_table_token(db)
    client = _manager_for(db, store.id)

    try:
        assert public.post(
            "/public/qr-context/resolve", json={"qr_token": old_raw}
        ).status_code == 200

        r = client.post(f"/owner/tables/{table.id}/rotate-qr")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["previous_token_revoked"] is True
        new_raw = body["qr_url"].split("#qr=", 1)[1]
        assert new_raw != old_raw

        # Old sticker: dead. New sticker: alive, same table.
        assert public.post(
            "/public/qr-context/resolve", json={"qr_token": old_raw}
        ).status_code == 404
        resolved = public.post(
            "/public/qr-context/resolve", json={"qr_token": new_raw}
        )
        assert resolved.status_code == 200
        assert resolved.json()["table"]["id"] == table.id

        # Exactly one ACTIVE token survives, and the history row is kept.
        assert (
            db.query(TableQrToken)
            .filter(
                TableQrToken.table_id == table.id,
                TableQrToken.status == QR_TOKEN_STATUS_ACTIVE,
            )
            .count()
            == 1
        )
        assert (
            db.query(TableQrToken).filter(TableQrToken.table_id == table.id).count()
            == 2
        )
    finally:
        _cleanup_manager(db, store.id)
        cleanup_store_table(db, store.id, table.id)


# ---------------------------------------------------------------------------
# Setup readiness
# ---------------------------------------------------------------------------

def test_setup_status_explains_an_empty_menu_and_then_clears(db, make_store):
    """
    The screen that answers "why is my customer menu empty?".

    A brand-new branch fails every check. Each fix flips exactly the check it
    fixed, so the owner is never told to do something they have already done.
    """
    store = make_store()
    purge_menu_for_store(db, store.id)
    client = _manager_for(db, store.id)
    product = make_product(db, offered_at_store_id=None)

    try:
        s = setup_status(client)
        assert s["store_id"] == store.id
        assert s["tables_total"] == 0
        assert s["published_products"] == 0
        assert s["menu_products"] == 0
        assert s["ready_for_customer_orders"] is False
        assert check(s, "has_table")["done"] is False
        assert check(s, "has_published_product")["done"] is False
        # The catalog count is what makes the confusion resolvable: there ARE
        # products in the system, they are simply not on this branch's menu.
        assert s["catalog_active_products"] >= 1

        # 1) A table, with its sticker.
        client.post("/owner/tables", json={"table_number": "1"})
        s = setup_status(client)
        assert check(s, "has_table")["done"] is True
        assert check(s, "has_table_qr")["done"] is True
        assert check(s, "has_published_product")["done"] is False
        assert s["ready_for_customer_orders"] is False

        # 2) A published product.
        client.post(f"/owner/menu/products/{product.id}/publish")
        s = setup_status(client)
        assert check(s, "has_published_product")["done"] is True
        assert check(s, "menu_ready")["done"] is True
        assert s["published_products"] == 1
        assert s["menu_products"] == 1
        assert s["ready_for_customer_orders"] is True

        # 3) Switching it off for the day breaks readiness without un-publishing.
        client.patch(
            f"/owner/menu/products/{product.id}/availability",
            json={"is_available": False},
        )
        s = setup_status(client)
        assert check(s, "has_published_product")["done"] is True
        assert check(s, "menu_ready")["done"] is False
        assert s["published_products"] == 1
        assert s["available_products"] == 0
        assert s["menu_products"] == 0
        assert s["ready_for_customer_orders"] is False
        # And it says which of the two situations this is.
        assert "kapalı" in check(s, "menu_ready")["detail"]
    finally:
        _cleanup_manager(db, store.id)
        cleanup_product(db, product.id)


def test_setup_status_counts_tables_without_a_qr(db, make_store, make_table):
    """
    A table nobody can scan is not a table anybody can order from.

    The check reports the shortfall rather than a bare "no", because "2/3 masa"
    and "0 masa" need different actions.
    """
    store = make_store()
    make_table(store.id, "Bare")
    client = _manager_for(db, store.id)

    try:
        s = setup_status(client)
        assert s["tables_total"] == 1
        assert s["tables_with_active_qr"] == 0
        assert check(s, "has_table")["done"] is True
        assert check(s, "has_table_qr")["done"] is False
        assert s["ready_for_customer_orders"] is False
    finally:
        _cleanup_manager(db, store.id)


def test_setup_status_is_never_served_from_a_cache(db, owner_client):
    """A cached checklist would tell an owner their menu is still empty after
    they fixed it."""
    r = owner_client.get("/owner/setup/status")
    assert r.headers.get("cache-control") == "no-store"
    assert owner_client.get("/owner/menu/products").headers.get("cache-control") == "no-store"
    assert owner_client.get("/owner/tables").headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------
# Local staff helpers
# ---------------------------------------------------------------------------
#
# ``manager_client_factory`` is a fixture and cannot be called from the module-level
# helpers above, so these two do the same job for a store created inside a test.
# They track nothing globally: each test cleans up the users it made, in the same
# ``finally`` block that removes its store.

_MANAGER_USERS: dict[int, list[int]] = {}


def _manager_for(db, store_id: int) -> TestClient:
    """An authenticated MANAGER session bound to `store_id`."""
    from app.core.security import hash_password
    from app.models.role import Role
    from app.models.user import User

    role = db.query(Role).filter(Role.name == "MANAGER").first()
    if role is None:
        role = Role(name="MANAGER")
        db.add(role)
        db.commit()
        db.refresh(role)

    user = User(
        username=f"setup_{uuid.uuid4().hex[:10]}",
        password_hash=hash_password("testpassw0rd"),
        role_id=role.id,
        store_id=store_id,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    _MANAGER_USERS.setdefault(store_id, []).append(user.id)
    return make_authed_client(db, user)


def _cleanup_manager(db, store_id: int) -> None:
    """Remove the sessions and users `_manager_for` created for this store."""
    from app.models.auth_session import AuthSession
    from app.models.user import User

    user_ids = _MANAGER_USERS.pop(store_id, [])
    if not user_ids:
        return
    db.query(AuthSession).filter(
        AuthSession.user_id.in_(user_ids)
    ).delete(synchronize_session=False)
    db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    db.commit()
