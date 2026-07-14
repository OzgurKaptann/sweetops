"""
Physical stock count — the API, the stock effects, the count/movement pair.

The property under test, in one sentence: counting the shelf records WHAT WAS
COUNTED and WHAT THE SYSTEM BELIEVED, and the two can never disagree with the
correction that was applied.

The failure modes being guarded against are not crashes. They are plausible-looking
numbers:

  * a count row says the shelf was corrected while no stock actually moved (or
    moved by the wrong amount) — the ledger and the count sheet tell different
    stories, and both look internally consistent;
  * a count writes on-hand below what accepted orders are already promised, so a
    customer who has been told "yes" is quietly told "no";
  * a count silently changes reserved stock, un-promising a waffle nobody cancelled;
  * a counted discrepancy is booked as WASTE or a MANUAL_ADJUSTMENT, so shrinkage
    disappears into a report that means something else;
  * a retried count sheet is applied twice;
  * a shelf that was counted and found CORRECT leaves no trace, so "we checked it"
    is indistinguishable from "nobody looked".

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
from app.models.ingredient_stock import (
    MOVEMENT_MANUAL_ADJUSTMENT,
    MOVEMENT_STOCK_COUNT_ADJUSTMENT,
    IngredientStock,
    IngredientStockMovement,
)
from app.models.inventory_stock_count import InventoryStockCount
from app.services import inventory_service
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
    order_payload,
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


def _counts(db, ing_id: int) -> list[InventoryStockCount]:
    db.expire_all()
    return (
        db.query(InventoryStockCount)
        .filter(InventoryStockCount.ingredient_id == ing_id)
        .order_by(InventoryStockCount.id)
        .all()
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


def _post_count(client, ing_id: int, counted: str, **kw):
    """POST a count with a fresh idempotency key unless one is given."""
    body = {
        "ingredient_id": ing_id,
        "counted_quantity": counted,
        "reason": kw.pop("reason", "Haftalik sayim"),
    }
    if "note" in kw:
        body["note"] = kw.pop("note")
    body.update(kw.pop("extra", {}))
    key = kw.pop("key", None) or uuid.uuid4().hex
    return client.post(
        "/inventory/stock-counts", json=body, headers={"Idempotency-Key": key}
    )


@pytest.fixture()
def counted(db, make_staff):
    """
    A manager, an authenticated client, and 10 kg of an ingredient on the shelf.

    ``standard_quantity`` is 2 kg per selection, so one order reserves 2 of the 10
    and leaves headroom. The tests that count BELOW reserved derive their figure
    from the reserved quantity rather than hardcoding it, so they hold either way.
    """
    class Env:
        pass

    env = Env()
    env.manager = make_staff("MANAGER", store_id=DEFAULT_STORE_ID)
    env.client = make_authed_client(db, env.manager)
    env.ingredient, env.stock = make_ingredient(
        db,
        on_hand=Decimal("10.000"),
        standard_quantity=Decimal("2.000"),
        unit="kg",
        store_id=DEFAULT_STORE_ID,
    )
    yield env
    cleanup_ingredient(db, env.ingredient.id)


# ---------------------------------------------------------------------------
# Stock effects
# ---------------------------------------------------------------------------

class TestStockEffects:
    def test_count_below_system_lowers_on_hand(self, db, counted):
        """10.000 on the system, 9.250 on the shelf → on-hand falls to 9.250."""
        res = _post_count(counted.client, counted.ingredient.id, "9.250")
        assert res.status_code == 200, res.text
        body = res.json()

        assert Decimal(body["counted_quantity"]) == Decimal("9.250")
        assert Decimal(body["system_on_hand_quantity"]) == Decimal("10.000")
        assert Decimal(body["delta_quantity"]) == Decimal("-0.750")

        stock = _stock(db, DEFAULT_STORE_ID, counted.ingredient.id)
        assert Decimal(stock.on_hand_quantity) == Decimal("9.250")

    def test_count_above_system_raises_on_hand(self, db, counted):
        """The shelf may hold MORE than the system thought — a count corrects up too."""
        res = _post_count(counted.client, counted.ingredient.id, "11.500")
        assert res.status_code == 200, res.text
        assert Decimal(res.json()["delta_quantity"]) == Decimal("1.500")

        stock = _stock(db, DEFAULT_STORE_ID, counted.ingredient.id)
        assert Decimal(stock.on_hand_quantity) == Decimal("11.500")

    def test_reserved_quantity_never_changes(self, db, counted, client):
        """
        A count observes the shelf. It does NOT un-promise a waffle that an accepted
        order is already counting on — reserved is untouched, and available moves
        only because on-hand did.
        """
        payload, headers = order_payload(
            counted.ingredient.id, store_id=DEFAULT_STORE_ID, idem_key=uuid.uuid4().hex
        )
        order = client.post("/public/orders/", json=payload, headers=headers)
        assert order.status_code in (200, 201), order.text

        before = _stock(db, DEFAULT_STORE_ID, counted.ingredient.id)
        reserved_before = Decimal(before.reserved_quantity)
        assert reserved_before > 0  # the order really did reserve

        res = _post_count(counted.client, counted.ingredient.id, "8.000")
        assert res.status_code == 200, res.text

        after = _stock(db, DEFAULT_STORE_ID, counted.ingredient.id)
        assert Decimal(after.reserved_quantity) == reserved_before
        assert Decimal(after.on_hand_quantity) == Decimal("8.000")
        # available is a GENERATED column: on_hand - reserved. It moved only because
        # on-hand did.
        assert Decimal(after.available_quantity) == Decimal("8.000") - reserved_before

    def test_non_zero_delta_writes_exactly_one_stock_count_movement(self, db, counted):
        res = _post_count(counted.client, counted.ingredient.id, "9.250")
        assert res.status_code == 200

        rows = _movements(db, counted.ingredient.id, MOVEMENT_STOCK_COUNT_ADJUSTMENT)
        assert len(rows) == 1
        m = rows[0]
        assert Decimal(m.quantity) == Decimal("0.750")           # abs(delta)
        assert Decimal(m.quantity_delta_on_hand) == Decimal("-0.750")
        assert Decimal(m.quantity_delta_reserved) == Decimal("0")
        assert m.stock_count_id == res.json()["stock_count_id"]
        assert res.json()["movement_id"] == m.id

        # And emphatically NOT a manual adjustment.
        assert _movements(db, counted.ingredient.id, MOVEMENT_MANUAL_ADJUSTMENT) == []

    def test_zero_delta_records_the_count_and_writes_no_movement(self, db, counted):
        """
        THE DOCUMENTED POLICY. The shelf agreed with the system: nothing physical
        happened, so nothing goes in the physical ledger — but the count row stands,
        because "we counted it and it was right" is exactly the fact a count exists
        to record.
        """
        res = _post_count(counted.client, counted.ingredient.id, "10.000")
        assert res.status_code == 200, res.text
        body = res.json()

        assert Decimal(body["delta_quantity"]) == Decimal("0")
        assert body["movement_id"] is None          # no ledger row
        assert body["stock_count_id"] is not None   # but the count is recorded

        assert len(_counts(db, counted.ingredient.id)) == 1
        assert _movements(db, counted.ingredient.id, MOVEMENT_STOCK_COUNT_ADJUSTMENT) == []

        stock = _stock(db, DEFAULT_STORE_ID, counted.ingredient.id)
        assert Decimal(stock.on_hand_quantity) == Decimal("10.000")

    def test_counting_an_empty_shelf_is_allowed(self, db, counted):
        """Zero is a valid count — and the one a manager most needs to report."""
        res = _post_count(counted.client, counted.ingredient.id, "0")
        assert res.status_code == 200, res.text
        assert Decimal(res.json()["delta_quantity"]) == Decimal("-10.000")

        stock = _stock(db, DEFAULT_STORE_ID, counted.ingredient.id)
        assert Decimal(stock.on_hand_quantity) == Decimal("0")

    def test_system_snapshot_is_captured_not_taken_from_the_client(self, db, counted):
        """
        The count stores what the system believed AT THE MOMENT OF COUNTING, and the
        client never gets to state it — there is no field for it, and the delta is
        derived server-side from the locked row.
        """
        res = _post_count(counted.client, counted.ingredient.id, "9.000")
        assert res.status_code == 200

        count = _counts(db, counted.ingredient.id)[0]
        assert Decimal(count.system_on_hand_quantity) == Decimal("10.000")
        assert Decimal(count.system_reserved_quantity) == Decimal("0")
        assert Decimal(count.delta_quantity) == Decimal("-1.000")
        assert count.status == "APPLIED"


# ---------------------------------------------------------------------------
# The reserved-stock safety rule
# ---------------------------------------------------------------------------

class TestCountBelowReserved:
    def test_count_below_reserved_is_refused(self, db, counted, client):
        """
        Counting 0.5 kg while an accepted order is promised more is NOT a stock
        correction — it means the shop has sold stock it does not have. Refuse, and
        leave both the shelf and the promise exactly as they were.
        """
        payload, headers = order_payload(
            counted.ingredient.id, store_id=DEFAULT_STORE_ID, idem_key=uuid.uuid4().hex
        )
        assert client.post("/public/orders/", json=payload, headers=headers).status_code in (200, 201)

        before = _stock(db, DEFAULT_STORE_ID, counted.ingredient.id)
        reserved = Decimal(before.reserved_quantity)
        on_hand_before = Decimal(before.on_hand_quantity)
        assert reserved > 0

        # Count strictly below what is already promised.
        below = reserved - Decimal("0.001")
        res = _post_count(counted.client, counted.ingredient.id, str(below))
        assert res.status_code == 409, res.text
        assert res.json()["detail"]["error"] == "stock_count_below_reserved"
        assert "ayrılmış" in res.json()["detail"]["message"].lower()

        # Nothing moved, and no count row was left behind.
        after = _stock(db, DEFAULT_STORE_ID, counted.ingredient.id)
        assert Decimal(after.on_hand_quantity) == on_hand_before
        assert Decimal(after.reserved_quantity) == reserved
        assert _counts(db, counted.ingredient.id) == []

    def test_count_exactly_equal_to_reserved_is_allowed(self, db, counted, client):
        """The rule is counted >= reserved, not counted > reserved. Equality is fine:
        every gram on the shelf is promised, and none is missing."""
        payload, headers = order_payload(
            counted.ingredient.id, store_id=DEFAULT_STORE_ID, idem_key=uuid.uuid4().hex
        )
        assert client.post("/public/orders/", json=payload, headers=headers).status_code in (200, 201)

        reserved = Decimal(_stock(db, DEFAULT_STORE_ID, counted.ingredient.id).reserved_quantity)
        res = _post_count(counted.client, counted.ingredient.id, str(reserved))
        assert res.status_code == 200, res.text

        after = _stock(db, DEFAULT_STORE_ID, counted.ingredient.id)
        assert Decimal(after.on_hand_quantity) == reserved
        assert Decimal(after.available_quantity) == Decimal("0")

    def test_database_refuses_a_count_below_reserved_even_without_the_service(
        self, db, counted
    ):
        """
        The service check is not the guarantee. ck_stock_count_counted_ge_reserved is.
        """
        with pytest.raises(_DB_REJECTS):
            db.execute(
                text(
                    """
                    INSERT INTO inventory_stock_counts (
                        store_id, ingredient_id, counted_quantity,
                        system_on_hand_quantity, system_reserved_quantity, unit,
                        reason, status, counted_by_user_id,
                        idempotency_key_hash, request_hash
                    ) VALUES (
                        :store, :ing, 1.000, 10.000, 4.000, 'kg',
                        'forced', 'APPLIED', :actor, :k, :r
                    )
                    """
                ),
                {
                    "store": DEFAULT_STORE_ID,
                    "ing": counted.ingredient.id,
                    "actor": counted.manager.id,
                    "k": uuid.uuid4().hex,
                    "r": uuid.uuid4().hex,
                },
            )
            db.commit()
        db.rollback()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_same_key_same_payload_replays_without_moving_stock_again(self, db, counted):
        key = uuid.uuid4().hex
        first = _post_count(counted.client, counted.ingredient.id, "9.250", key=key)
        assert first.status_code == 200
        assert first.json()["idempotent_replay"] is False

        second = _post_count(counted.client, counted.ingredient.id, "9.250", key=key)
        assert second.status_code == 200
        assert second.json()["idempotent_replay"] is True
        assert second.json()["stock_count_id"] == first.json()["stock_count_id"]

        # The shelf moved ONCE — 10.000 → 9.250, not 8.500.
        stock = _stock(db, DEFAULT_STORE_ID, counted.ingredient.id)
        assert Decimal(stock.on_hand_quantity) == Decimal("9.250")
        assert len(_counts(db, counted.ingredient.id)) == 1
        assert len(_movements(db, counted.ingredient.id, MOVEMENT_STOCK_COUNT_ADJUSTMENT)) == 1

    def test_same_key_different_payload_is_a_conflict(self, db, counted):
        """
        Replaying the original under a NEW intent would tell a manager who re-counted
        and found 8 kg that their 9.25 kg count succeeded. Refuse.
        """
        key = uuid.uuid4().hex
        assert _post_count(counted.client, counted.ingredient.id, "9.250", key=key).status_code == 200

        res = _post_count(counted.client, counted.ingredient.id, "8.000", key=key)
        assert res.status_code == 409
        assert res.json()["detail"]["error"] == "idempotency_mismatch"

        stock = _stock(db, DEFAULT_STORE_ID, counted.ingredient.id)
        assert Decimal(stock.on_hand_quantity) == Decimal("9.250")

    def test_another_store_may_reuse_the_same_key_independently(
        self, db, counted, make_store, make_staff
    ):
        """
        Two managers working from the same printed count sheet WILL send the same
        Idempotency-Key. That is a coincidence, not a replay — Beşiktaş's count must
        never return Kadıköy's result.
        """
        from tests.conftest import stock_for

        other = make_store()
        stock_for(db, counted.ingredient, other.id, on_hand=Decimal("4.000"))
        other_manager = make_staff("MANAGER", store_id=other.id)
        other_client = make_authed_client(db, other_manager)

        key = uuid.uuid4().hex
        a = _post_count(counted.client, counted.ingredient.id, "9.250", key=key)
        b = _post_count(other_client, counted.ingredient.id, "3.000", key=key)

        assert a.status_code == 200 and b.status_code == 200, b.text
        assert b.json()["idempotent_replay"] is False
        assert a.json()["stock_count_id"] != b.json()["stock_count_id"]

        # Each branch's own shelf, and only its own.
        assert Decimal(_stock(db, DEFAULT_STORE_ID, counted.ingredient.id).on_hand_quantity) == Decimal("9.250")
        assert Decimal(_stock(db, other.id, counted.ingredient.id).on_hand_quantity) == Decimal("3.000")

    def test_raw_idempotency_key_is_never_stored_or_returned(self, db, counted):
        key = uuid.uuid4().hex
        res = _post_count(counted.client, counted.ingredient.id, "9.250", key=key)
        assert res.status_code == 200

        # Not in the response body...
        assert key not in res.text
        # ...and only a SHA-256 digest on the row.
        count = _counts(db, counted.ingredient.id)[0]
        assert count.idempotency_key_hash != key
        assert len(count.idempotency_key_hash) == 64
        assert inventory_service._sha256(key) == count.idempotency_key_hash

    def test_missing_idempotency_key_is_rejected(self, counted):
        res = counted.client.post(
            "/inventory/stock-counts",
            json={
                "ingredient_id": counted.ingredient.id,
                "counted_quantity": "9.000",
                "reason": "sayim",
            },
        )
        assert res.status_code == 400
        assert res.json()["detail"]["error"] == "idempotency_required"


# ---------------------------------------------------------------------------
# Request validation & store scoping
# ---------------------------------------------------------------------------

class TestRequestValidation:
    def test_unknown_fields_are_rejected(self, counted):
        """
        extra="forbid". A smuggled store_id/delta/movement_type is not silently
        ignored — the whole request is refused, because a client that believes it
        counted another branch's freezer must be told otherwise.
        """
        for smuggled in (
            {"store_id": 999},
            {"delta_quantity": "-5.000"},
            {"system_on_hand_quantity": "1.000"},
            {"actor_user_id": 1},
            {"movement_type": "WASTE"},
            {"idempotency_key_hash": "x" * 64},
            {"request_hash": "x" * 64},
            {"status": "DRAFT"},
        ):
            res = _post_count(
                counted.client, counted.ingredient.id, "9.000", extra=smuggled
            )
            assert res.status_code == 422, f"{smuggled} was accepted: {res.text}"

    def test_client_store_id_cannot_redirect_the_count(self, db, counted, make_store):
        """
        There is no store_id field at all — the count lands in the SESSION's store.
        The attempt is rejected outright rather than quietly applied to the caller's
        own branch, which would leave the client believing it had counted elsewhere.
        """
        other = make_store()
        res = _post_count(
            counted.client, counted.ingredient.id, "9.000", extra={"store_id": other.id}
        )
        assert res.status_code == 422
        assert _counts(db, counted.ingredient.id) == []

    def test_negative_count_is_rejected(self, counted):
        res = _post_count(counted.client, counted.ingredient.id, "-1.000")
        assert res.status_code == 422

    def test_reason_is_required(self, counted):
        res = _post_count(counted.client, counted.ingredient.id, "9.000", reason="")
        assert res.status_code == 422

    def test_count_for_unconfigured_stock_returns_stock_not_configured(
        self, db, counted, make_store, make_staff
    ):
        """A branch that has never stocked an ingredient has no shelf to count."""
        other = make_store()  # deliberately NO stock row for this ingredient
        other_client = make_authed_client(db, make_staff("MANAGER", store_id=other.id))

        res = _post_count(other_client, counted.ingredient.id, "5.000")
        assert res.status_code == 404
        assert res.json()["detail"]["error"] == "stock_not_configured"


class TestPermissions:
    def test_owner_can_apply_a_count(self, db, counted, make_staff):
        owner = make_authed_client(db, make_staff("OWNER", store_id=DEFAULT_STORE_ID))
        assert _post_count(owner, counted.ingredient.id, "9.000").status_code == 200

    def test_manager_can_apply_a_count(self, counted):
        assert _post_count(counted.client, counted.ingredient.id, "9.000").status_code == 200

    def test_cashier_cannot_apply_a_count(self, db, counted, make_staff):
        """Money and stock are separate authorities. CASHIER has neither inventory
        permission, so it cannot even read."""
        cashier = make_authed_client(db, make_staff("CASHIER", store_id=DEFAULT_STORE_ID))
        assert _post_count(cashier, counted.ingredient.id, "9.000").status_code == 403

    def test_kitchen_cannot_apply_a_count(self, db, counted, make_staff):
        """
        KITCHEN holds inventory:read and NOT inventory:adjust. A cook correcting the
        count is exactly the unaccountable adjustment this lifecycle exists to
        prevent — so it may look, and may not touch.
        """
        kitchen = make_authed_client(db, make_staff("KITCHEN", store_id=DEFAULT_STORE_ID))
        assert _post_count(kitchen, counted.ingredient.id, "9.000").status_code == 403
        # ...but the read is allowed.
        assert kitchen.get("/inventory/stock-counts").status_code == 200

    def test_missing_csrf_token_is_rejected(self, db, counted, make_staff):
        from app.core.config import settings
        from app.services import auth_service
        from fastapi.testclient import TestClient
        from app.main import app

        user = make_staff("MANAGER", store_id=DEFAULT_STORE_ID)
        _session, raw_token, _csrf = auth_service.create_session(db, user)
        naked = TestClient(app)
        naked.cookies.set(settings.SESSION_COOKIE_NAME, raw_token)
        # No X-CSRF-Token header at all.

        res = naked.post(
            "/inventory/stock-counts",
            json={
                "ingredient_id": counted.ingredient.id,
                "counted_quantity": "9.000",
                "reason": "sayim",
            },
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert res.status_code == 403
        assert _counts(db, counted.ingredient.id) == []

    def test_no_cross_store_count(self, db, counted, make_store, make_staff):
        """
        A manager of another branch cannot count THIS branch's shelf, and its own
        count never touches this branch's stock.
        """
        from tests.conftest import stock_for

        other = make_store()
        stock_for(db, counted.ingredient, other.id, on_hand=Decimal("4.000"))
        other_client = make_authed_client(db, make_staff("MANAGER", store_id=other.id))

        assert _post_count(other_client, counted.ingredient.id, "1.000").status_code == 200

        # This store's shelf is untouched.
        assert Decimal(
            _stock(db, DEFAULT_STORE_ID, counted.ingredient.id).on_hand_quantity
        ) == Decimal("10.000")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

class TestReads:
    def test_list_returns_this_stores_counts_only(self, db, counted, make_store, make_staff):
        from tests.conftest import stock_for

        other = make_store()
        stock_for(db, counted.ingredient, other.id, on_hand=Decimal("4.000"))
        other_client = make_authed_client(db, make_staff("MANAGER", store_id=other.id))

        assert _post_count(counted.client, counted.ingredient.id, "9.000").status_code == 200
        assert _post_count(other_client, counted.ingredient.id, "3.000").status_code == 200

        mine = counted.client.get("/inventory/stock-counts").json()
        assert mine["total"] == 1
        assert mine["items"][0]["store_id"] == DEFAULT_STORE_ID
        assert Decimal(mine["items"][0]["counted_quantity"]) == Decimal("9.000")

    def test_get_one_count(self, counted):
        created = _post_count(counted.client, counted.ingredient.id, "9.250")
        cid = created.json()["stock_count_id"]

        res = counted.client.get(f"/inventory/stock-counts/{cid}")
        assert res.status_code == 200
        assert res.json()["stock_count_id"] == cid
        assert Decimal(res.json()["delta_quantity"]) == Decimal("-0.750")

    def test_another_stores_count_is_a_404_not_a_403(
        self, db, counted, make_store, make_staff
    ):
        """A 403 would confirm the count exists. It is none of this branch's business."""
        from tests.conftest import stock_for

        other = make_store()
        stock_for(db, counted.ingredient, other.id, on_hand=Decimal("4.000"))
        other_client = make_authed_client(db, make_staff("MANAGER", store_id=other.id))

        theirs = _post_count(other_client, counted.ingredient.id, "3.000").json()
        res = counted.client.get(f"/inventory/stock-counts/{theirs['stock_count_id']}")
        assert res.status_code == 404

    def test_movement_appears_in_the_ledger_read(self, counted):
        _post_count(counted.client, counted.ingredient.id, "9.250")
        res = counted.client.get(
            "/inventory/movements",
            params={"movement_type": MOVEMENT_STOCK_COUNT_ADJUSTMENT},
        )
        assert res.status_code == 200
        assert res.json()["total"] == 1
        assert res.json()["items"][0]["movement_type"] == MOVEMENT_STOCK_COUNT_ADJUSTMENT


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

class TestAudit:
    def test_count_is_audited_without_leaking_credentials(self, db, counted):
        key = uuid.uuid4().hex
        res = _post_count(counted.client, counted.ingredient.id, "9.250", key=key)
        cid = res.json()["stock_count_id"]

        db.expire_all()
        # NEWEST first, deliberately. audit_log is append-only and outlives the rows
        # it describes: the migration round-trip tests drop and recreate
        # inventory_stock_counts, which resets its id sequence, so a fresh count can
        # legitimately reuse an id that an OLD audit row still names. The entry this
        # test means is the one it just wrote — the latest.
        entry = (
            db.query(AuditLog)
            .filter(
                AuditLog.action == inventory_service.AUDIT_STOCK_COUNTED,
                AuditLog.entity_type == "inventory_stock_count",
                AuditLog.entity_id == cid,
            )
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry is not None

        payload = entry.payload_after
        assert payload["stock_count_id"] == cid
        assert payload["store_id"] == DEFAULT_STORE_ID
        assert payload["ingredient_id"] == counted.ingredient.id
        assert Decimal(payload["counted_quantity"]) == Decimal("9.250")
        assert Decimal(payload["system_on_hand_quantity"]) == Decimal("10.000")
        assert Decimal(payload["system_reserved_quantity"]) == Decimal("0")
        assert Decimal(payload["delta_quantity"]) == Decimal("-0.750")
        assert payload["actor_user_id"] == counted.manager.id
        assert payload["reason"]

        # An audit trail that leaks a replayable credential is a liability.
        blob = str(payload)
        assert key not in blob
        assert inventory_service._sha256(key) not in blob
        assert "request_hash" not in payload
        assert "idempotency" not in blob.lower()
