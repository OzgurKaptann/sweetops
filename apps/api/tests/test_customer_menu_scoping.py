"""
The customer-facing menu is scoped to the branch the guest is sitting in, and
ordering is checked against that same branch's menu.

What these tests are actually defending
---------------------------------------
Before migration a9e4c7b25d13 the public menu was ``db.query(Product).all()``:
every row of a chain-wide table, with no store filter and no activation flag,
served to whoever scanned a QR code. The live database at the time held eight
``TestWaffle_<hex>`` rows at ₺100.00 left by interrupted test runs
(RUNTIME_PRODUCT_GAP_REVIEW F-02 / F-23), one rendered list away from a guest's
phone.

Nothing below matches on a product NAME. That would be a filter over a symptom
and the next debris row would be named something else. What is tested is the
relationship: a product reaches a guest only through a ``store_products`` row
that says this branch publishes it, and an order is refused unless the same
relationship holds for the store the QR token resolved to.
"""
import uuid
from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app
from app.models.store_product import StoreProduct
from tests.conftest import (
    cleanup_ingredient,
    cleanup_product,
    cleanup_store_table,
    make_ingredient,
    make_product,
    make_store_table_token,
    offer_product,
    purge_menu_for_store,
    withdraw_product,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def menu_for(qr_token: str) -> dict:
    r = client.post("/public/menu/resolve", json={"qr_token": qr_token})
    assert r.status_code == 200, r.text
    return r.json()


def product_ids(menu: dict) -> set[int]:
    return {p["id"] for p in menu["products"]}


def order_payload(qr_token: str, product_id: int, ingredient_id: int, *,
                  quantity: int = 1, ingredient_quantity: int = 1) -> tuple[dict, dict]:
    return (
        {
            "qr_token": qr_token,
            "items": [{
                "product_id": product_id,
                "quantity": quantity,
                "ingredients": [
                    {"ingredient_id": ingredient_id, "quantity": ingredient_quantity}
                ],
            }],
        },
        {"Idempotency-Key": uuid.uuid4().hex},
    )


# ---------------------------------------------------------------------------
# Menu scoping
# ---------------------------------------------------------------------------

def test_menu_returns_only_the_products_this_branch_published(db):
    """The QR-resolved menu is a publication list, not a catalog dump."""
    store, table, _record, raw = make_store_table_token(db)
    # This branch is provisioned by the fixture with the default product only;
    # start from a clean slate so the assertion is about what we publish here.
    purge_menu_for_store(db, store.id)

    published = make_product(db, offered_at_store_id=None)
    unpublished = make_product(db, offered_at_store_id=None)
    offer_product(db, store.id, published.id)

    try:
        ids = product_ids(menu_for(raw))
        assert published.id in ids
        assert unpublished.id not in ids
        # And nothing else leaked in from the chain-wide table either.
        assert ids == {published.id}
    finally:
        cleanup_product(db, published.id)
        cleanup_product(db, unpublished.id)
        cleanup_store_table(db, store.id, table.id)


def test_a_product_no_branch_published_reaches_no_customer(db):
    """
    The test-debris shape, without matching on a name.

    A row lands in ``products`` (an import, a seed, a killed test run) and
    nobody ever decides to sell it. It must be absent from every branch's menu
    — not filtered out of one.
    """
    store_a, table_a, _ra, raw_a = make_store_table_token(db)
    store_b, table_b, _rb, raw_b = make_store_table_token(db)
    debris = make_product(db, base_price=Decimal("100.00"), offered_at_store_id=None)

    try:
        assert debris.id not in product_ids(menu_for(raw_a))
        assert debris.id not in product_ids(menu_for(raw_b))
    finally:
        cleanup_product(db, debris.id)
        cleanup_store_table(db, store_a.id, table_a.id)
        cleanup_store_table(db, store_b.id, table_b.id)


def test_inactive_product_is_excluded_even_though_the_branch_published_it(db):
    """products.is_active retires an item chain-wide; publication cannot override it."""
    store, table, _record, raw = make_store_table_token(db)
    purge_menu_for_store(db, store.id)
    retired = make_product(db, is_active=False, offered_at_store_id=None)
    live = make_product(db, offered_at_store_id=None)
    offer_product(db, store.id, retired.id)
    offer_product(db, store.id, live.id)

    try:
        ids = product_ids(menu_for(raw))
        assert live.id in ids
        assert retired.id not in ids
    finally:
        cleanup_product(db, retired.id)
        cleanup_product(db, live.id)
        cleanup_store_table(db, store.id, table.id)


def test_offering_switched_off_for_today_is_excluded(db):
    """is_available=False takes it off the board without forgetting it is sold."""
    store, table, _record, raw = make_store_table_token(db)
    purge_menu_for_store(db, store.id)
    product = make_product(db, offered_at_store_id=None)
    offer_product(db, store.id, product.id, is_available=False)

    try:
        assert product.id not in product_ids(menu_for(raw))
        # The publication decision survives — it is switched off, not deleted.
        offering = db.query(StoreProduct).filter(
            StoreProduct.store_id == store.id,
            StoreProduct.product_id == product.id,
        ).one()
        assert offering.is_available is False

        # Switching it back on puts it straight back on the menu.
        offer_product(db, store.id, product.id, is_available=True)
        assert product.id in product_ids(menu_for(raw))
    finally:
        cleanup_product(db, product.id)
        cleanup_store_table(db, store.id, table.id)


def test_two_branches_keep_independent_menus(db):
    """
    One catalog, two branches, two different menus.

    This is the property a single global product list cannot express at all: a
    seasonal item in Kadıköy that Moda does not sell.
    """
    store_a, table_a, _ra, raw_a = make_store_table_token(db)
    store_b, table_b, _rb, raw_b = make_store_table_token(db)
    purge_menu_for_store(db, store_a.id)
    purge_menu_for_store(db, store_b.id)

    shared = make_product(db, offered_at_store_id=None)
    only_a = make_product(db, offered_at_store_id=None)
    only_b = make_product(db, offered_at_store_id=None)
    offer_product(db, store_a.id, shared.id)
    offer_product(db, store_a.id, only_a.id)
    offer_product(db, store_b.id, shared.id)
    offer_product(db, store_b.id, only_b.id)

    try:
        ids_a = product_ids(menu_for(raw_a))
        ids_b = product_ids(menu_for(raw_b))
        assert ids_a == {shared.id, only_a.id}
        assert ids_b == {shared.id, only_b.id}
        # Each response says whose menu it is, so a client cannot mix them up.
        assert menu_for(raw_a)["store_id"] == store_a.id
        assert menu_for(raw_b)["store_id"] == store_b.id
    finally:
        for pid in (shared.id, only_a.id, only_b.id):
            cleanup_product(db, pid)
        cleanup_store_table(db, store_a.id, table_a.id)
        cleanup_store_table(db, store_b.id, table_b.id)


def test_a_branch_with_no_menu_returns_an_empty_list_not_the_catalog(db):
    """
    Fail closed. An unprovisioned branch sells nothing; it does not fall back
    to "everything in the products table", which is precisely the old bug.
    """
    store, table, _record, raw = make_store_table_token(db)
    purge_menu_for_store(db, store.id)
    existing_elsewhere = make_product(db, offered_at_store_id=None)

    try:
        menu = menu_for(raw)
        assert menu["products"] == []
        assert existing_elsewhere.id not in product_ids(menu)
        # The rest of the menu still works — this is an empty menu, not an error.
        assert "categories" in menu and "ingredients" in menu
    finally:
        cleanup_product(db, existing_elsewhere.id)
        cleanup_store_table(db, store.id, table.id)


# ---------------------------------------------------------------------------
# Order creation is validated against the SAME scope
# ---------------------------------------------------------------------------

def test_ordering_a_product_this_branch_does_not_publish_is_rejected(db):
    """
    The frontend is not evidence. A real, active product id that belongs to
    ANOTHER branch's menu is refused here.
    """
    store_a, table_a, _ra, raw_a = make_store_table_token(db)
    store_b, table_b, _rb, _raw_b = make_store_table_token(db)
    purge_menu_for_store(db, store_a.id)

    only_b = make_product(db, offered_at_store_id=None)
    offer_product(db, store_b.id, only_b.id)
    ing, _ = make_ingredient(db, on_hand=Decimal("500.00"), store_id=store_a.id)

    try:
        payload, headers = order_payload(raw_a, only_b.id, ing.id)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "product_unavailable"
        assert detail["ids"] == [only_b.id]
        # Turkish, and it never says which branch does sell it.
        assert "menüsünde" in detail["message"]
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_product(db, only_b.id)
        cleanup_store_table(db, store_a.id, table_a.id)
        cleanup_store_table(db, store_b.id, table_b.id)


def test_ordering_an_unpublished_product_is_rejected(db):
    """A catalog row nobody published cannot be ordered by guessing its id."""
    store, table, _record, raw = make_store_table_token(db)
    debris = make_product(db, offered_at_store_id=None)
    ing, _ = make_ingredient(db, on_hand=Decimal("500.00"), store_id=store.id)

    try:
        payload, headers = order_payload(raw, debris.id, ing.id)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "product_unavailable"
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_product(db, debris.id)
        cleanup_store_table(db, store.id, table.id)


def test_ordering_a_retired_product_is_rejected(db):
    """Even with a live offering row, an inactive product cannot be sold."""
    store, table, _record, raw = make_store_table_token(db)
    retired = make_product(db, is_active=False, offered_at_store_id=None)
    offer_product(db, store.id, retired.id)
    ing, _ = make_ingredient(db, on_hand=Decimal("500.00"), store_id=store.id)

    try:
        payload, headers = order_payload(raw, retired.id, ing.id)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "product_unavailable"
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_product(db, retired.id)
        cleanup_store_table(db, store.id, table.id)


def test_ordering_a_product_withdrawn_after_the_menu_loaded_is_rejected(db):
    """
    The race a customer actually hits: they loaded the menu, the branch took the
    item off, they tap order. The server refuses on the state at submit time.
    """
    store, table, _record, raw = make_store_table_token(db)
    product = make_product(db, offered_at_store_id=None)
    offer_product(db, store.id, product.id)
    ing, _ = make_ingredient(db, on_hand=Decimal("500.00"), store_id=store.id)

    try:
        assert product.id in product_ids(menu_for(raw))  # it was on the menu…
        withdraw_product(db, store.id, product.id)        # …and now it is not

        payload, headers = order_payload(raw, product.id, ing.id)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "product_unavailable"
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_product(db, product.id)
        cleanup_store_table(db, store.id, table.id)


# ---------------------------------------------------------------------------
# Quantity safety
# ---------------------------------------------------------------------------

def test_non_positive_quantities_are_rejected(db):
    """
    Zero orders nothing; a negative one used to multiply straight through the
    consumption formula into a NEGATIVE stock requirement — a "sale" that
    releases stock and reduces the bill.
    """
    store, table, _record, raw = make_store_table_token(db)
    product = make_product(db, offered_at_store_id=None)
    offer_product(db, store.id, product.id)
    ing, _ = make_ingredient(db, on_hand=Decimal("500.00"), store_id=store.id)

    try:
        for bad in (0, -1, -1000):
            payload, headers = order_payload(raw, product.id, ing.id, quantity=bad)
            r = client.post("/public/orders/", json=payload, headers=headers)
            assert r.status_code == 422, f"quantity={bad} was accepted: {r.text}"

        # …and the same bound applies to ingredient portions.
        payload, headers = order_payload(
            raw, product.id, ing.id, ingredient_quantity=0
        )
        assert client.post(
            "/public/orders/", json=payload, headers=headers
        ).status_code == 422
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_product(db, product.id)
        cleanup_store_table(db, store.id, table.id)


def test_absurd_quantities_are_rejected(db):
    """A table orders waffles, not pallets. The bound is refused, not clamped."""
    store, table, _record, raw = make_store_table_token(db)
    product = make_product(db, offered_at_store_id=None)
    offer_product(db, store.id, product.id)
    ing, _ = make_ingredient(db, on_hand=Decimal("500000.00"), store_id=store.id)

    try:
        for bad in (21, 10_000, 2_147_483_647):
            payload, headers = order_payload(raw, product.id, ing.id, quantity=bad)
            r = client.post("/public/orders/", json=payload, headers=headers)
            assert r.status_code == 422, f"quantity={bad} was accepted: {r.text}"
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_product(db, product.id)
        cleanup_store_table(db, store.id, table.id)


def test_an_empty_order_is_rejected(db):
    """No lines is not an order."""
    store, table, _record, raw = make_store_table_token(db)
    try:
        r = client.post(
            "/public/orders/",
            json={"qr_token": raw, "items": []},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert r.status_code == 422
    finally:
        cleanup_store_table(db, store.id, table.id)


# ---------------------------------------------------------------------------
# The happy path still works
# ---------------------------------------------------------------------------

def test_a_valid_customer_order_still_succeeds(db):
    """Published product, in-scope branch, sane quantity — nothing is in the way."""
    store, table, _record, raw = make_store_table_token(db)
    purge_menu_for_store(db, store.id)
    product = make_product(db, base_price=Decimal("90.00"), offered_at_store_id=None)
    offer_product(db, store.id, product.id)
    ing, _ = make_ingredient(
        db,
        on_hand=Decimal("500.00"),
        standard_quantity=Decimal("10.00"),
        price=Decimal("10.00"),
        store_id=store.id,
    )

    try:
        # The guest can see it on the menu…
        assert product.id in product_ids(menu_for(raw))

        # …and order two of them.
        payload, headers = order_payload(raw, product.id, ing.id, quantity=2)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["store_id"] == store.id
        assert body["table_id"] == table.id
        assert body["item_count"] == 2
        # (90 base + 10 modifier) × 2
        assert Decimal(str(body["total_amount"])) == Decimal("200.00")
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_product(db, product.id)
        cleanup_store_table(db, store.id, table.id)
