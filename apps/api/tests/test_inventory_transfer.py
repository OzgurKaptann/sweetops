"""
Store-to-store inventory transfer — the API, the stock effects, the ledger pair.

The property under test, in one sentence: moving 2 kg of chocolate from Kadıköy to
Beşiktaş is ONE event, and the database must not be able to represent half of it.

Everything here is built on the same shape — one catalog ingredient, two stores,
two independent physical quantities — because that is the shape a transfer moves
stock between. The failure modes being guarded against are not crashes. They are
plausible-looking numbers:

  * the source's stock falls and the destination's never rises (2 kg evaporates),
  * the outbound half is booked as WASTE (the branch is accused of throwing away
    chocolate it actually shipped),
  * the inbound half is booked as a PURCHASE_RECEIPT (a supplier delivery that
    never happened, inflating what the chain thinks it bought),
  * stock already promised to an accepted order is put on a van, and a customer
    who has been told "yes" is quietly told "no",
  * a retried request ships the chocolate twice.

None of those raise an exception on their own, which is why several of these tests
go around the service layer entirely and assert that the DATABASE refuses.
"""
import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models.audit_log import AuditLog
from app.models.ingredient_stock import (
    MOVEMENT_MANUAL_ADJUSTMENT,
    MOVEMENT_PURCHASE_RECEIPT,
    MOVEMENT_TRANSFER_IN,
    MOVEMENT_TRANSFER_OUT,
    IngredientStock,
    IngredientStockMovement,
)
from app.models.inventory_transfer import InventoryTransfer
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
    order_payload,
    stock_for,
)

# Both are integrity failures raised by PostgreSQL; which one surfaces depends on
# whether SQLAlchemy could pre-empt it.
_DB_REJECTS = (IntegrityError, DBAPIError)


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


def _movements(db, ing_id: int, movement_type: str) -> list[IngredientStockMovement]:
    db.expire_all()
    return (
        db.query(IngredientStockMovement)
        .filter(
            IngredientStockMovement.ingredient_id == ing_id,
            IngredientStockMovement.movement_type == movement_type,
        )
        .all()
    )


def _transfer_body(dest_store_id: int, ing_id: int, qty: str = "20.000", **over) -> dict:
    body = {
        "destination_store_id": dest_store_id,
        "ingredient_id": ing_id,
        "quantity": qty,
        "reason": "Beşiktaş şubesine takviye",
    }
    body.update(over)
    return body


def _key() -> str:
    return uuid.uuid4().hex


def _audit_for(db, transfer_id: int, actor_user_id: int) -> AuditLog:
    """The INVENTORY_TRANSFERRED record for this transfer, written by this actor."""
    rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == "inventory_transfer",
            AuditLog.entity_id == transfer_id,
            AuditLog.action == "INVENTORY_TRANSFERRED",
            AuditLog.actor_id == str(actor_user_id),
        )
        .all()
    )
    assert len(rows) == 1, f"expected one audit record, got {len(rows)}"
    return rows[0]


@pytest.fixture()
def two_stores(db, make_store, make_staff):
    """
    A source store (the pre-existing default) and a freshly created destination,
    plus an OWNER authenticated in the source store.

    The source is DEFAULT_STORE_ID because that is where the rest of the suite's
    fixtures put stock; the destination is new, so it starts with no stock at all
    — which is exactly the "receiving branch has never held this ingredient" case
    the transfer policy has to answer.
    """
    dest = make_store("Beşiktaş")
    owner = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
    client = make_authed_client(db, owner)
    return client, owner, DEFAULT_STORE_ID, dest.id


# ═══════════════════════════════════════════════════════════════════════════
# Stock effects — the two halves of one event
# ═══════════════════════════════════════════════════════════════════════════

class TestStockEffects:

    def test_transfer_moves_on_hand_and_leaves_reserved_alone(self, db, two_stores):
        """
        The core arithmetic, on both shelves at once.

        Source loses 20 g of on-hand, destination gains exactly 20 g, and NEITHER
        store's reserved moves — a transfer moves physical stock, not anybody's
        promise to a customer.
        """
        client, _owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        stock_for(db, ing, dst, on_hand=Decimal("30.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "20.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 200, r.text
            body = r.json()

            source = _stock(db, src, ing.id)
            destination = _stock(db, dst, ing.id)

            # Source: on-hand and available both fall; reserved is untouched.
            assert source.on_hand_quantity == Decimal("80.000")
            assert source.reserved_quantity == Decimal("0.000")
            assert source.available_quantity == Decimal("80.000")

            # Destination: on-hand and available both rise; reserved untouched.
            assert destination.on_hand_quantity == Decimal("50.000")
            assert destination.reserved_quantity == Decimal("0.000")
            assert destination.available_quantity == Decimal("50.000")

            # Nothing was created or destroyed: the chain still holds 130 g.
            assert (
                source.on_hand_quantity + destination.on_hand_quantity
                == Decimal("130.000")
            )

            assert body["status"] == "COMPLETED"
            assert body["source_store_id"] == src
            assert body["destination_store_id"] == dst
            assert Decimal(body["quantity"]) == Decimal("20.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_reserved_source_stock_is_not_transferable(self, db, client, two_stores):
        """
        The whole point of gating on AVAILABLE rather than on-hand.

        100 g on the shelf, 10 g already promised to an accepted order. 95 g looks
        transferable if you read on-hand, and it is not: shipping it would leave
        the customer who has already been told "yes" with 5 g short of a waffle.
        """
        transfer_client, _owner, src, dst = two_stores
        ing, _ = make_ingredient(
            db, on_hand=Decimal("100.000"), standard_quantity=Decimal("10.00")
        )
        stock_for(db, ing, dst, on_hand=Decimal("0.000"))
        try:
            # An accepted order reserves 10 g in the source store.
            payload, headers = order_payload(ing.id, idem_key=_key())
            assert client.post("/public/orders/", json=payload, headers=headers).status_code in (200, 201)

            source = _stock(db, src, ing.id)
            assert source.reserved_quantity == Decimal("10.000")
            assert source.available_quantity == Decimal("90.000")

            # 95 <= on_hand (100) but > available (90). Must be refused.
            r = transfer_client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "95.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["error"] == "insufficient_available"

            # Nothing moved on either side.
            assert _stock(db, src, ing.id).on_hand_quantity == Decimal("100.000")
            assert _stock(db, dst, ing.id).on_hand_quantity == Decimal("0.000")

            # ...and exactly the available amount still IS transferable.
            ok = transfer_client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "90.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert ok.status_code == 200, ok.text
            source = _stock(db, src, ing.id)
            assert source.on_hand_quantity == Decimal("10.000")
            assert source.reserved_quantity == Decimal("10.000")   # promise intact
            assert source.available_quantity == Decimal("0.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_insufficient_available_stock_is_refused(self, db, two_stores):
        client, _owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("5.000"))
        stock_for(db, ing, dst, on_hand=Decimal("0.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "10.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 409
            assert _stock(db, src, ing.id).on_hand_quantity == Decimal("5.000")
            assert _stock(db, dst, ing.id).on_hand_quantity == Decimal("0.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_destination_stock_row_is_created_when_the_branch_has_never_held_it(
        self, db, two_stores
    ):
        """
        POLICY, and it is a deliberate choice: transferring into a branch that has
        never stocked the ingredient CREATES its stock row at zero and then posts
        the inbound leg into it. It does not 404.

        Nothing is fabricated — the row starts empty and is filled only by a
        TRANSFER_IN that is exactly matched by a TRANSFER_OUT elsewhere, so the
        chain's total is unchanged to the gram. The alternative (refuse until
        someone books a purchase receipt) would force a manager stocking a newly
        opened branch from the warehouse branch to invent a supplier delivery that
        never happened — precisely the lie about physical stock this module exists
        to prevent. Documented in docs/INVENTORY_TRANSFER_WORKFLOW.md.
        """
        client, _owner, _src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("50.000"))
        try:
            assert _stock(db, dst, ing.id) is None   # the branch has never held it

            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "12.500"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 200, r.text

            created = _stock(db, dst, ing.id)
            assert created is not None
            assert created.on_hand_quantity == Decimal("12.500")
            assert created.reserved_quantity == Decimal("0.000")
            assert created.unit == ing.unit
        finally:
            cleanup_ingredient(db, ing.id)


# ═══════════════════════════════════════════════════════════════════════════
# The movement pair — one event, two linked legs, and nothing else
# ═══════════════════════════════════════════════════════════════════════════

class TestMovementPair:

    def test_exactly_one_out_and_one_in_sharing_a_transfer_identity(self, db, two_stores):
        client, owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "20.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 200, r.text
            transfer_id = r.json()["transfer_id"]

            out_legs = _movements(db, ing.id, MOVEMENT_TRANSFER_OUT)
            in_legs = _movements(db, ing.id, MOVEMENT_TRANSFER_IN)
            assert len(out_legs) == 1
            assert len(in_legs) == 1
            out, inn = out_legs[0], in_legs[0]

            # They are the two halves of ONE event.
            assert out.transfer_id == transfer_id
            assert inn.transfer_id == transfer_id

            # The outbound leg is booked in the SOURCE store, signed negative,
            # and leaves reserved alone.
            assert out.store_id == src
            assert out.quantity_delta_on_hand == Decimal("-20.000")
            assert out.quantity_delta_reserved == Decimal("0.000")
            # ...and it names the person who authorised it.
            assert out.actor_user_id == owner.id

            # The inbound leg is booked in the DESTINATION store, signed positive.
            assert inn.store_id == dst
            assert inn.quantity_delta_on_hand == Decimal("20.000")
            assert inn.quantity_delta_reserved == Decimal("0.000")
            # It carries NO actor: the initiator works in the source store, and
            # staff only move stock in their own store (fk_movement_actor_store).
            # Accountability is the transfer row's initiated_by_user_id.
            assert inn.actor_user_id is None

            transfer = db.get(InventoryTransfer, transfer_id)
            assert transfer.initiated_by_user_id == owner.id

            # The response exposes both legs, and neither hash nor raw key.
            body = r.json()
            assert body["source_movement_id"] == out.id
            assert body["destination_movement_id"] == inn.id
            assert "idempotency_key_hash" not in body
            assert "request_hash" not in body
        finally:
            cleanup_ingredient(db, ing.id)

    @pytest.mark.parametrize(
        "forbidden_type",
        ["WASTE", "PURCHASE_RECEIPT", "MANUAL_ADJUSTMENT", "CONSUMPTION"],
    )
    def test_transfer_creates_no_other_movement_type(self, db, two_stores, forbidden_type):
        """
        A transfer must be unmistakable for anything else in the ledger.

        This is the test that protects the owner's reports: if a transfer left a
        WASTE row behind, the waste report would accuse a branch of binning
        chocolate it shipped; a PURCHASE_RECEIPT row would invent a supplier
        delivery; a CONSUMPTION row would inflate the burn rate that reorder
        decisions are made from.
        """
        client, _owner, _src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "20.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 200, r.text
            assert _movements(db, ing.id, forbidden_type) == []
        finally:
            cleanup_ingredient(db, ing.id)


# ═══════════════════════════════════════════════════════════════════════════
# Validation of the destination
# ═══════════════════════════════════════════════════════════════════════════

class TestDestinationValidation:

    def test_destination_store_must_exist(self, db, two_stores):
        client, _owner, _src, _dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(999_999, ing.id, "10.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 404
            assert r.json()["detail"]["error"] == "destination_store_not_found"
        finally:
            cleanup_ingredient(db, ing.id)

    def test_destination_cannot_be_the_source(self, db, two_stores):
        """Shipping stock to yourself is not a transfer; it is a no-op that would
        leave a cancelling pair of movements in the ledger for no reason."""
        client, _owner, src, _dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(src, ing.id, "10.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 422
            assert r.json()["detail"]["error"] == "same_store_transfer"
            assert _stock(db, src, ing.id).on_hand_quantity == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_quantity_must_be_positive(self, db, two_stores):
        client, _owner, _src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            for bad in ("0", "-5.000"):
                r = client.post(
                    "/inventory/transfers",
                    json=_transfer_body(dst, ing.id, bad),
                    headers={"Idempotency-Key": _key()},
                )
                assert r.status_code == 422, f"{bad} was accepted"
        finally:
            cleanup_ingredient(db, ing.id)

    def test_reason_is_required(self, db, two_stores):
        """An unexplained shipment of stock out of a branch is indistinguishable
        from stock walking out of the door."""
        client, _owner, _src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            body = _transfer_body(dst, ing.id)
            body["reason"] = ""
            r = client.post(
                "/inventory/transfers",
                json=body,
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 422
        finally:
            cleanup_ingredient(db, ing.id)

    def test_source_must_already_stock_the_ingredient(self, db, make_store, make_staff):
        """A branch cannot ship what it has never held — and is emphatically not
        allowed to satisfy the shipment from a third store's shelf."""
        source = make_store("Kadıköy")
        dest = make_store("Beşiktaş")
        owner = make_staff("OWNER", store_id=source.id)
        client = make_authed_client(db, owner)

        # The ingredient exists in the catalog, with stock ONLY in the default store.
        ing, _ = make_ingredient(db, on_hand=Decimal("500.000"), store_id=DEFAULT_STORE_ID)
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dest.id, ing.id, "10.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 404
            assert r.json()["detail"]["error"] == "stock_not_configured"
            # The other store's 500 g is untouched — it was never a candidate.
            assert _stock(db, DEFAULT_STORE_ID, ing.id).on_hand_quantity == Decimal("500.000")
        finally:
            cleanup_ingredient(db, ing.id)


# ═══════════════════════════════════════════════════════════════════════════
# Request strictness — the source store is not the client's to choose
# ═══════════════════════════════════════════════════════════════════════════

class TestRequestStrictness:

    @pytest.mark.parametrize(
        "smuggled",
        [
            {"source_store_id": 2},
            {"actor_user_id": 1},
            {"movement_type": "PURCHASE_RECEIPT"},
            {"quantity_delta_on_hand": "999.000"},
            {"idempotency_key_hash": "a" * 64},
            {"request_hash": "b" * 64},
            {"status": "PENDING"},
        ],
    )
    def test_unknown_fields_are_rejected_outright(self, db, two_stores, smuggled):
        """
        The schema FORBIDS unknown fields rather than ignoring them.

        Ignoring a smuggled ``source_store_id`` would be safe in the sense that the
        session's store is still used — but the client would be told 200 OK and
        would reasonably believe it had shipped the store it named. A 422 is the
        honest answer: that request cannot be expressed.
        """
        client, _owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "10.000", **smuggled),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 422, f"{smuggled} was accepted: {r.text}"
            # ...and no stock moved anywhere.
            assert _stock(db, src, ing.id).on_hand_quantity == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_source_store_always_comes_from_the_session(self, db, make_store, make_staff):
        """
        Two stores both hold the same ingredient. A manager authenticated in store
        A transfers; the stock that leaves is store A's, and store B's is untouched
        — there is no field in the request that could have said otherwise.
        """
        other = make_store("Üsküdar")
        dest = make_store("Beşiktaş")
        owner = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
        client = make_authed_client(db, owner)

        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"), store_id=DEFAULT_STORE_ID)
        stock_for(db, ing, other.id, on_hand=Decimal("777.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dest.id, ing.id, "10.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 200, r.text
            assert r.json()["source_store_id"] == DEFAULT_STORE_ID

            assert _stock(db, DEFAULT_STORE_ID, ing.id).on_hand_quantity == Decimal("90.000")
            # The uninvolved branch did not lose a gram.
            assert _stock(db, other.id, ing.id).on_hand_quantity == Decimal("777.000")
        finally:
            cleanup_ingredient(db, ing.id)


# ═══════════════════════════════════════════════════════════════════════════
# Permissions, CSRF, Origin, session
# ═══════════════════════════════════════════════════════════════════════════

class TestPermissions:

    @pytest.mark.parametrize("role", ["OWNER", "MANAGER"])
    def test_owner_and_manager_can_transfer_from_their_own_store(
        self, db, make_store, make_staff, role
    ):
        dest = make_store("Beşiktaş")
        user = make_staff(role, store_id=DEFAULT_STORE_ID)
        client = make_authed_client(db, user)
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dest.id, ing.id, "10.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 200, r.text
            assert r.json()["initiated_by_user_id"] == user.id
        finally:
            cleanup_ingredient(db, ing.id)

    @pytest.mark.parametrize("role", ["CASHIER", "KITCHEN"])
    def test_cashier_and_kitchen_cannot_transfer(self, db, make_store, make_staff, role):
        """
        Transfer requires ``inventory:adjust`` — the same physical-stock authority
        as waste and manual adjustment, because it permanently changes what is on a
        branch's shelves.

        CASHIER has no inventory permission at all (money and stock are separate
        authorities). KITCHEN can READ stock so it can flag a shortage, but cannot
        rewrite it — a cook shipping a crate to another branch is exactly the
        unaccountable stock movement this lifecycle exists to prevent. Neither role
        is granted inventory:adjust today, so neither can transfer; if that grant
        ever changes, this test changes with it deliberately rather than silently.
        """
        dest = make_store("Beşiktaş")
        user = make_staff(role, store_id=DEFAULT_STORE_ID)
        client = make_authed_client(db, user)
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dest.id, ing.id, "10.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 403, r.text
            assert _stock(db, DEFAULT_STORE_ID, ing.id).on_hand_quantity == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_unauthenticated_is_rejected(self, db, client, make_store):
        dest = make_store("Beşiktaş")
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dest.id, ing.id, "10.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 401
        finally:
            cleanup_ingredient(db, ing.id)

    def test_missing_csrf_token_is_rejected(self, db, two_stores):
        client, _owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            client.headers.pop("X-CSRF-Token", None)
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "10.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 403, r.text
            assert _stock(db, src, ing.id).on_hand_quantity == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_untrusted_origin_is_rejected(self, db, two_stores):
        client, _owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "10.000"),
                headers={
                    "Idempotency-Key": _key(),
                    "Origin": "https://evil.example.com",
                },
            )
            assert r.status_code == 403, r.text
            assert _stock(db, src, ing.id).on_hand_quantity == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_missing_idempotency_key_is_rejected(self, db, two_stores):
        client, _owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post("/inventory/transfers", json=_transfer_body(dst, ing.id, "10.000"))
            assert r.status_code == 400, r.text
            assert r.json()["detail"]["error"] == "idempotency_required"
            assert _stock(db, src, ing.id).on_hand_quantity == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_staff_with_no_store_cannot_transfer(self, db, make_store, make_staff):
        """
        There is no chain-wide inventory to ship FROM.

        The rejection is a 401, not the router's 403 ``no_store_assigned``: an
        OPERATIONAL role with no store assignment cannot resolve a session at all
        (auth_service refuses it), so the request never reaches the permission
        check. That is the stronger of the two answers, and it is the one asserted
        here — the 403 in routers/inventory.py remains the backstop for any future
        non-operational role that could hold inventory:adjust.
        """
        dest = make_store("Beşiktaş")
        user = make_staff("OWNER", store_id=None)
        client = make_authed_client(db, user)
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dest.id, ing.id, "10.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 401, r.text
            # The database also holds this line independently: the transfer's
            # composite FK (source_store_id, initiated_by_user_id) → users has no
            # row to match for a user whose store_id is NULL.
            assert _stock(db, DEFAULT_STORE_ID, ing.id).on_hand_quantity == Decimal("100.000")
        finally:
            cleanup_ingredient(db, ing.id)


# ═══════════════════════════════════════════════════════════════════════════
# Idempotency
# ═══════════════════════════════════════════════════════════════════════════

class TestIdempotency:

    def test_same_key_same_payload_replays_and_ships_nothing_more(self, db, two_stores):
        """A retried van manifest ships the chocolate ONCE."""
        client, _owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            key = _key()
            body = _transfer_body(dst, ing.id, "20.000")

            first = client.post(
                "/inventory/transfers", json=body, headers={"Idempotency-Key": key}
            )
            assert first.status_code == 200, first.text
            assert first.json()["idempotent_replay"] is False

            second = client.post(
                "/inventory/transfers", json=body, headers={"Idempotency-Key": key}
            )
            assert second.status_code == 200, second.text
            assert second.json()["idempotent_replay"] is True

            # Same event, same two legs — not a second shipment.
            assert second.json()["transfer_id"] == first.json()["transfer_id"]
            assert second.json()["source_movement_id"] == first.json()["source_movement_id"]
            assert second.json()["destination_movement_id"] == first.json()["destination_movement_id"]

            # Stock moved exactly once.
            assert _stock(db, src, ing.id).on_hand_quantity == Decimal("80.000")
            assert _stock(db, dst, ing.id).on_hand_quantity == Decimal("20.000")
            assert len(_movements(db, ing.id, MOVEMENT_TRANSFER_OUT)) == 1
            assert len(_movements(db, ing.id, MOVEMENT_TRANSFER_IN)) == 1
            assert db.query(InventoryTransfer).filter(
                InventoryTransfer.ingredient_id == ing.id
            ).count() == 1
        finally:
            cleanup_ingredient(db, ing.id)

    def test_same_key_different_payload_is_a_conflict(self, db, two_stores):
        """
        Replaying the original's result under a DIFFERENT payload would silently
        discard the new intent: a manager who meant to ship 50 g would be told the
        20 g they shipped an hour ago succeeded.
        """
        client, _owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            key = _key()
            first = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "20.000"),
                headers={"Idempotency-Key": key},
            )
            assert first.status_code == 200, first.text

            clash = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "50.000"),
                headers={"Idempotency-Key": key},
            )
            assert clash.status_code == 409, clash.text
            assert clash.json()["detail"]["error"] == "idempotency_mismatch"

            # The second request shipped nothing.
            assert _stock(db, src, ing.id).on_hand_quantity == Decimal("80.000")
            assert len(_movements(db, ing.id, MOVEMENT_TRANSFER_OUT)) == 1
        finally:
            cleanup_ingredient(db, ing.id)

    def test_another_source_store_may_reuse_the_same_key(self, db, make_store, make_staff):
        """
        Two branch managers working from the same printed run-book will legitimately
        send "Idempotency-Key: 1". That collision is a coincidence, not a replay —
        and if the key were global, the second branch's transfer would silently
        return the first branch's result and ship nothing at all.
        """
        source_b = make_store("Üsküdar")
        dest = make_store("Beşiktaş")

        owner_a = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
        owner_b = make_staff("OWNER", store_id=source_b.id)
        client_a = make_authed_client(db, owner_a)
        client_b = make_authed_client(db, owner_b)

        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"), store_id=DEFAULT_STORE_ID)
        stock_for(db, ing, source_b.id, on_hand=Decimal("60.000"))
        try:
            shared_key = _key()

            a = client_a.post(
                "/inventory/transfers",
                json=_transfer_body(dest.id, ing.id, "10.000"),
                headers={"Idempotency-Key": shared_key},
            )
            b = client_b.post(
                "/inventory/transfers",
                json=_transfer_body(dest.id, ing.id, "25.000"),
                headers={"Idempotency-Key": shared_key},
            )
            assert a.status_code == 200, a.text
            assert b.status_code == 200, b.text

            # Two DIFFERENT transfers, neither a replay of the other.
            assert a.json()["transfer_id"] != b.json()["transfer_id"]
            assert a.json()["idempotent_replay"] is False
            assert b.json()["idempotent_replay"] is False

            # Both branches really shipped.
            assert _stock(db, DEFAULT_STORE_ID, ing.id).on_hand_quantity == Decimal("90.000")
            assert _stock(db, source_b.id, ing.id).on_hand_quantity == Decimal("35.000")
            # ...and the destination got both crates.
            assert _stock(db, dest.id, ing.id).on_hand_quantity == Decimal("35.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_raw_idempotency_key_is_never_stored(self, db, two_stores):
        """Only SHA-256 digests are persisted. A stored raw key is a replay token
        sitting in the database waiting to be read."""
        client, _owner, _src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            key = _key()
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "20.000"),
                headers={"Idempotency-Key": key},
            )
            assert r.status_code == 200, r.text
            transfer = db.get(InventoryTransfer, r.json()["transfer_id"])

            assert transfer.idempotency_key_hash != key
            assert len(transfer.idempotency_key_hash) == 64
            assert len(transfer.request_hash) == 64

            # The raw key appears NOWHERE in the transfer table.
            hits = db.execute(
                text(
                    "SELECT count(*) FROM inventory_transfers "
                    "WHERE idempotency_key_hash = :k OR request_hash = :k "
                    "   OR reason = :k OR COALESCE(note, '') = :k"
                ),
                {"k": key},
            ).scalar()
            assert hits == 0
        finally:
            cleanup_ingredient(db, ing.id)


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════

class TestAudit:

    def test_transfer_writes_an_audit_record_naming_both_stores_and_the_actor(
        self, db, two_stores
    ):
        client, owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "20.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 200, r.text
            tid = r.json()["transfer_id"]

            # Scoped by actor as well as by transfer id. The audit table is never
            # purged between runs, and the transfer id sequence restarts whenever
            # the migration round-trip test drops and recreates the table — so
            # `entity_id` alone can collide with a previous run's row. This test's
            # staff user is freshly created and unique to it.
            entry = _audit_for(db, tid, owner.id)
            after = entry.payload_after
            assert after["transfer_id"] == tid
            assert after["source_store_id"] == src
            assert after["destination_store_id"] == dst
            assert after["ingredient_id"] == ing.id
            assert Decimal(str(after["quantity"])) == Decimal("20.000")
            assert after["actor_user_id"] == owner.id
            assert entry.actor_id == str(owner.id)
        finally:
            cleanup_ingredient(db, ing.id)

    def test_audit_never_records_a_credential(self, db, two_stores):
        """
        An audit trail that leaks a replayable credential is a liability, not a
        control. No session token, no CSRF token, no raw idempotency key, and no
        request hash may reach it.
        """
        client, owner, _src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            key = _key()
            csrf = client.headers.get("X-CSRF-Token")
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "20.000"),
                headers={"Idempotency-Key": key},
            )
            assert r.status_code == 200, r.text

            entry = _audit_for(db, r.json()["transfer_id"], owner.id)
            blob = json.dumps(entry.payload_after or {})

            assert key not in blob
            assert csrf and csrf not in blob
            for banned in (
                "idempotency_key", "idempotency_key_hash", "request_hash",
                "csrf", "session", "token",
            ):
                assert banned not in blob.lower(), f"{banned} leaked into the audit trail"
        finally:
            cleanup_ingredient(db, ing.id)


# ═══════════════════════════════════════════════════════════════════════════
# Reads
# ═══════════════════════════════════════════════════════════════════════════

class TestTransferReads:

    def test_both_stores_see_the_transfer_from_their_own_side(
        self, db, make_store, make_staff
    ):
        dest = make_store("Beşiktaş")
        sender = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
        receiver = make_staff("OWNER", store_id=dest.id)
        sender_client = make_authed_client(db, sender)
        receiver_client = make_authed_client(db, receiver)

        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = sender_client.post(
                "/inventory/transfers",
                json=_transfer_body(dest.id, ing.id, "20.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 200, r.text
            tid = r.json()["transfer_id"]

            # The sender sees it leaving...
            out = sender_client.get(f"/inventory/transfers/{tid}")
            assert out.status_code == 200
            assert out.json()["direction"] == "OUTBOUND"

            # ...and the receiver sees the very same event arriving. Without this,
            # a branch could not answer "where did this crate come from?".
            inn = receiver_client.get(f"/inventory/transfers/{tid}")
            assert inn.status_code == 200
            assert inn.json()["direction"] == "INBOUND"
            assert inn.json()["transfer_id"] == tid

            listed = sender_client.get("/inventory/transfers", params={"ingredient_id": ing.id})
            assert listed.status_code == 200
            assert [i["transfer_id"] for i in listed.json()["items"]] == [tid]
        finally:
            cleanup_ingredient(db, ing.id)

    def test_a_transfer_between_two_other_stores_is_invisible(
        self, db, make_store, make_staff
    ):
        """404, not 403: a 403 would confirm the transfer exists."""
        source_b = make_store("Üsküdar")
        dest = make_store("Beşiktaş")
        owner_b = make_staff("OWNER", store_id=source_b.id)
        outsider = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
        client_b = make_authed_client(db, owner_b)
        outsider_client = make_authed_client(db, outsider)

        ing, _ = make_ingredient(db, on_hand=Decimal("10.000"), store_id=DEFAULT_STORE_ID)
        stock_for(db, ing, source_b.id, on_hand=Decimal("60.000"))
        try:
            r = client_b.post(
                "/inventory/transfers",
                json=_transfer_body(dest.id, ing.id, "20.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 200, r.text
            tid = r.json()["transfer_id"]

            seen = outsider_client.get(f"/inventory/transfers/{tid}")
            assert seen.status_code == 404

            listed = outsider_client.get("/inventory/transfers", params={"ingredient_id": ing.id})
            assert listed.json()["items"] == []
        finally:
            cleanup_ingredient(db, ing.id)

    def test_transfer_legs_appear_in_the_movement_ledger_with_their_own_types(
        self, db, two_stores
    ):
        """Stock movement history must show a transfer AS a transfer — not as an
        anonymous signed number that a reader has to guess the meaning of."""
        client, _owner, _src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "20.000"),
                headers={"Idempotency-Key": _key()},
            )
            r = client.get(
                "/inventory/movements",
                params={"ingredient_id": ing.id, "movement_type": MOVEMENT_TRANSFER_OUT},
            )
            assert r.status_code == 200
            items = r.json()["items"]
            assert len(items) == 1
            assert items[0]["movement_type"] == MOVEMENT_TRANSFER_OUT
            assert Decimal(items[0]["quantity_delta_on_hand"]) == Decimal("-20.000")
        finally:
            cleanup_ingredient(db, ing.id)


# ═══════════════════════════════════════════════════════════════════════════
# The database refuses, even when the application does not ask it to
# ═══════════════════════════════════════════════════════════════════════════

class TestDatabaseRefuses:
    """
    Every test here goes AROUND the service layer and writes raw SQL, because the
    guarantee being claimed is that a corrupt transfer is unrepresentable — not
    merely that the current code happens not to write one.
    """

    def test_a_lone_transfer_out_cannot_be_committed(self, db, two_stores):
        """
        THE headline invariant. Stock that leaves a branch and arrives nowhere is
        the worst outcome this feature can produce, and no per-row constraint can
        see it — "this transfer has both halves" is a statement about a SET of
        rows. A deferred constraint trigger refuses it at COMMIT.
        """
        client, owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        stock_for(db, ing, dst, on_hand=Decimal("0.000"))
        try:
            with pytest.raises(_DB_REJECTS):
                db.execute(
                    text(
                        """
                        WITH t AS (
                            INSERT INTO inventory_transfers (
                                source_store_id, destination_store_id, ingredient_id,
                                quantity, unit, status, reason, initiated_by_user_id,
                                idempotency_key_hash, request_hash
                            ) VALUES (
                                :src, :dst, :ing, 5, 'g', 'COMPLETED', 'half a transfer',
                                :uid, :k, :h
                            ) RETURNING id
                        )
                        INSERT INTO ingredient_stock_movements (
                            store_id, ingredient_id, movement_type, quantity,
                            quantity_delta_on_hand, quantity_delta_reserved, unit,
                            reason, actor_user_id, transfer_id
                        )
                        SELECT :src, :ing, 'TRANSFER_OUT', 5, -5, 0, 'g',
                               'half a transfer', :uid, t.id
                        FROM t
                        """
                    ),
                    {
                        "src": src, "dst": dst, "ing": ing.id, "uid": owner.id,
                        "k": "c" * 64, "h": "d" * 64,
                    },
                )
                db.commit()   # the deferred pairing trigger fires HERE
            db.rollback()

            # Nothing survived: no transfer, no orphan leg.
            assert db.query(InventoryTransfer).filter(
                InventoryTransfer.ingredient_id == ing.id
            ).count() == 0
            assert _movements(db, ing.id, MOVEMENT_TRANSFER_OUT) == []
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)

    def test_out_leg_cannot_be_booked_in_the_wrong_store(self, db, two_stores):
        """
        fk_movement_transfer_source_leg. Booking the outbound half of a
        Kadıköy → Beşiktaş transfer against Beşiktaş's shelf is not a bug for code
        review to catch — it is a row PostgreSQL will not store.
        """
        client, owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "20.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 200, r.text
            tid = r.json()["transfer_id"]

            # A second TRANSFER_OUT, booked against the DESTINATION store.
            with pytest.raises(_DB_REJECTS):
                db.execute(
                    text(
                        """
                        INSERT INTO ingredient_stock_movements (
                            store_id, ingredient_id, movement_type, quantity,
                            quantity_delta_on_hand, quantity_delta_reserved, unit,
                            reason, transfer_id
                        ) VALUES (
                            :wrong_store, :ing, 'TRANSFER_OUT', 5, -5, 0, 'g',
                            'wrong side', :tid
                        )
                        """
                    ),
                    {"wrong_store": dst, "ing": ing.id, "tid": tid},
                )
                db.commit()
            db.rollback()
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)

    def test_transfer_leg_cannot_cite_a_different_ingredient(self, db, two_stores):
        """The legs must move the thing the transfer says moved."""
        client, owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        other, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "20.000"),
                headers={"Idempotency-Key": _key()},
            )
            assert r.status_code == 200, r.text
            tid = r.json()["transfer_id"]

            with pytest.raises(_DB_REJECTS):
                db.execute(
                    text(
                        """
                        INSERT INTO ingredient_stock_movements (
                            store_id, ingredient_id, movement_type, quantity,
                            quantity_delta_on_hand, quantity_delta_reserved, unit,
                            reason, transfer_id
                        ) VALUES (
                            :dst, :other, 'TRANSFER_IN', 5, 5, 0, 'g',
                            'wrong ingredient', :tid
                        )
                        """
                    ),
                    {"dst": dst, "other": other.id, "tid": tid},
                )
                db.commit()
            db.rollback()
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)
            cleanup_ingredient(db, other.id)

    @pytest.mark.parametrize(
        "movement_type,delta_on_hand,delta_reserved",
        [
            ("TRANSFER_OUT", "5", "0"),    # outbound must be NEGATIVE
            ("TRANSFER_IN", "-5", "0"),    # inbound must be POSITIVE
            ("TRANSFER_OUT", "-5", "-5"),  # reserved must not move
            ("TRANSFER_IN", "5", "5"),     # reserved must not move
        ],
    )
    def test_leg_delta_must_match_its_direction(
        self, db, two_stores, movement_type, delta_on_hand, delta_reserved
    ):
        """ck_movement_delta_matches_type, extended to the transfer types. A
        TRANSFER_OUT that ADDS stock is not a transfer, it is a fabrication."""
        _client, _owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            with pytest.raises(_DB_REJECTS):
                db.execute(
                    text(
                        """
                        INSERT INTO ingredient_stock_movements (
                            store_id, ingredient_id, movement_type, quantity,
                            quantity_delta_on_hand, quantity_delta_reserved, unit,
                            reason, transfer_id
                        ) VALUES (
                            :store, :ing, :mt, 5, :doh, :dres, 'g', 'bad delta', NULL
                        )
                        """
                    ),
                    {
                        "store": src, "ing": ing.id, "mt": movement_type,
                        "doh": delta_on_hand, "dres": delta_reserved,
                    },
                )
                db.commit()
            db.rollback()
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)

    def test_a_transfer_movement_must_carry_a_transfer_id(self, db, two_stores):
        """ck_movement_transfer_link: a TRANSFER_OUT with no transfer is an orphan
        half of an event that nothing can ever reconcile."""
        _client, owner, src, _dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            with pytest.raises(_DB_REJECTS):
                db.execute(
                    text(
                        """
                        INSERT INTO ingredient_stock_movements (
                            store_id, ingredient_id, movement_type, quantity,
                            quantity_delta_on_hand, quantity_delta_reserved, unit,
                            reason, actor_user_id, transfer_id
                        ) VALUES (
                            :src, :ing, 'TRANSFER_OUT', 5, -5, 0, 'g',
                            'no transfer', :uid, NULL
                        )
                        """
                    ),
                    {"src": src, "ing": ing.id, "uid": owner.id},
                )
                db.commit()
            db.rollback()
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)

    def test_a_non_transfer_movement_cannot_claim_a_transfer(self, db, two_stores):
        """The other half of ck_movement_transfer_link: a PURCHASE_RECEIPT must not
        be able to dress itself up as an arriving shipment."""
        client, owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            r = client.post(
                "/inventory/transfers",
                json=_transfer_body(dst, ing.id, "20.000"),
                headers={"Idempotency-Key": _key()},
            )
            tid = r.json()["transfer_id"]

            with pytest.raises(_DB_REJECTS):
                db.execute(
                    text(
                        """
                        INSERT INTO ingredient_stock_movements (
                            store_id, ingredient_id, movement_type, quantity,
                            quantity_delta_on_hand, quantity_delta_reserved, unit,
                            reason, actor_user_id, transfer_id
                        ) VALUES (
                            :src, :ing, 'PURCHASE_RECEIPT', 5, 5, 0, 'g',
                            'masquerade', :uid, :tid
                        )
                        """
                    ),
                    {"src": src, "ing": ing.id, "uid": owner.id, "tid": tid},
                )
                db.commit()
            db.rollback()
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)

    def test_initiator_must_belong_to_the_source_store(self, db, make_store, make_staff):
        """
        fk_transfer_actor_source_store. A Store B manager cannot be recorded as
        having shipped Store A's stock — not because a permission check refuses,
        but because the row does not exist that would say so.
        """
        source = make_store("Kadıköy")
        dest = make_store("Beşiktaş")
        outsider = make_staff("OWNER", store_id=dest.id)   # belongs to the WRONG store

        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"), store_id=source.id)
        stock_for(db, ing, dest.id, on_hand=Decimal("0.000"))
        try:
            with pytest.raises(_DB_REJECTS):
                db.execute(
                    text(
                        """
                        INSERT INTO inventory_transfers (
                            source_store_id, destination_store_id, ingredient_id,
                            quantity, unit, status, reason, initiated_by_user_id,
                            idempotency_key_hash, request_hash
                        ) VALUES (
                            :src, :dst, :ing, 5, 'g', 'COMPLETED', 'not my store',
                            :uid, :k, :h
                        )
                        """
                    ),
                    {
                        "src": source.id, "dst": dest.id, "ing": ing.id,
                        "uid": outsider.id, "k": "e" * 64, "h": "f" * 64,
                    },
                )
                db.commit()
            db.rollback()
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)

    def test_source_and_destination_must_differ_at_the_database_level(
        self, db, two_stores
    ):
        _client, owner, src, _dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            with pytest.raises(_DB_REJECTS):
                db.execute(
                    text(
                        """
                        INSERT INTO inventory_transfers (
                            source_store_id, destination_store_id, ingredient_id,
                            quantity, unit, status, reason, initiated_by_user_id,
                            idempotency_key_hash, request_hash
                        ) VALUES (
                            :src, :src, :ing, 5, 'g', 'COMPLETED', 'to myself',
                            :uid, :k, :h
                        )
                        """
                    ),
                    {"src": src, "ing": ing.id, "uid": owner.id, "k": "1" * 64, "h": "2" * 64},
                )
                db.commit()
            db.rollback()
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)

    def test_transfer_quantity_must_be_positive_at_the_database_level(
        self, db, two_stores
    ):
        _client, owner, src, dst = two_stores
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            for bad in ("0", "-5"):
                with pytest.raises(_DB_REJECTS):
                    db.execute(
                        text(
                            """
                            INSERT INTO inventory_transfers (
                                source_store_id, destination_store_id, ingredient_id,
                                quantity, unit, status, reason, initiated_by_user_id,
                                idempotency_key_hash, request_hash
                            ) VALUES (
                                :src, :dst, :ing, :q, 'g', 'COMPLETED', 'bad qty',
                                :uid, :k, :h
                            )
                            """
                        ),
                        {
                            "src": src, "dst": dst, "ing": ing.id, "q": bad,
                            "uid": owner.id, "k": uuid.uuid4().hex * 2, "h": "3" * 64,
                        },
                    )
                    db.commit()
                db.rollback()
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)

    def test_transfer_idempotency_uniqueness_is_source_store_scoped(
        self, db, make_store, make_staff
    ):
        """
        uq_transfer_source_idem. The SAME key hash is accepted from two different
        source stores (they are two independent commands), and refused twice from
        the same one.
        """
        source_b = make_store("Üsküdar")
        dest = make_store("Beşiktaş")
        owner_a = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
        owner_b = make_staff("OWNER", store_id=source_b.id)

        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"), store_id=DEFAULT_STORE_ID)
        stock_for(db, ing, source_b.id, on_hand=Decimal("100.000"))
        stock_for(db, ing, dest.id, on_hand=Decimal("0.000"))

        shared_hash = "9" * 64
        insert = text(
            """
            INSERT INTO inventory_transfers (
                source_store_id, destination_store_id, ingredient_id,
                quantity, unit, status, reason, initiated_by_user_id,
                idempotency_key_hash, request_hash
            ) VALUES (
                :src, :dst, :ing, 5, 'g', 'COMPLETED', 'scoped key',
                :uid, :k, :h
            )
            """
        )
        try:
            # The pairing trigger is deferred and would refuse these leg-less
            # transfers at COMMIT, so the uniqueness behaviour is observed inside
            # a transaction that is then rolled back. Uniqueness is checked
            # immediately on INSERT; pairing only at COMMIT.
            db.execute(insert, {
                "src": DEFAULT_STORE_ID, "dst": dest.id, "ing": ing.id,
                "uid": owner_a.id, "k": shared_hash, "h": "a" * 64,
            })
            # Same key hash, DIFFERENT source store — a coincidence, not a replay.
            db.execute(insert, {
                "src": source_b.id, "dst": dest.id, "ing": ing.id,
                "uid": owner_b.id, "k": shared_hash, "h": "a" * 64,
            })
            db.flush()   # both accepted

            # Same key hash, SAME source store — that IS a duplicate.
            with pytest.raises(_DB_REJECTS):
                db.execute(insert, {
                    "src": DEFAULT_STORE_ID, "dst": dest.id, "ing": ing.id,
                    "uid": owner_a.id, "k": shared_hash, "h": "a" * 64,
                })
                db.flush()
        finally:
            db.rollback()
            cleanup_ingredient(db, ing.id)
