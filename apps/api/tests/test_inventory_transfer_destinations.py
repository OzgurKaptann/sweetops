"""
GET /inventory/transfer-destinations — the destination picker for the owner-web
transfer form.

This endpoint exists for exactly one reason: a manager filling in "Şube transferi"
has to be able to NAME the branch the van is going to, and no other route in the
API will tell them that a sibling branch exists. It was added with the owner
inventory management UI (docs/OWNER_INVENTORY_MANAGEMENT_UI.md).

The whole risk of adding it is scope creep — a "list the stores" endpoint is one
careless field away from being a chain-wide store-management API that leaks one
branch's operational data to another. So the tests below pin the *limits* as hard
as they pin the behaviour:

  * it is READ-ONLY and behind ``inventory:read`` plus a store-assigned session,
  * it returns id/name/location and NOTHING else — no stock, no staff, no takings,
  * the caller's own store is never offered as a destination,
  * a cashier (no inventory permission at all) cannot use it to enumerate branches.

The same-store exclusion here is a usability courtesy, not the security control:
``transfer_stock`` still rejects a same-store transfer server-side. That is
asserted in test_inventory_transfer.py and re-asserted at the end of this file, so
that deleting the filter in this list can never silently become exploitable.
"""
from decimal import Decimal

import pytest

from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
    stock_for,
)


@pytest.fixture()
def branches(db, make_store, make_staff):
    """The caller's store (DEFAULT) plus two sibling branches, and an owner session."""
    besiktas = make_store("Beşiktaş")
    uskudar = make_store("Üsküdar")
    owner = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
    client = make_authed_client(db, owner)
    return {"client": client, "besiktas": besiktas, "uskudar": uskudar}


@pytest.fixture()
def chocolate(db):
    """One catalog ingredient with real stock in the caller's own store."""
    ing, _stock = make_ingredient(
        db, on_hand=Decimal("50.000"), name="Çikolata", store_id=DEFAULT_STORE_ID
    )
    yield ing
    cleanup_ingredient(db, ing.id)


class TestDestinationList:
    def test_lists_the_other_branches(self, branches):
        res = branches["client"].get("/inventory/transfer-destinations")
        assert res.status_code == 200

        body = res.json()
        ids = {item["store_id"] for item in body["items"]}
        assert branches["besiktas"].id in ids
        assert branches["uskudar"].id in ids
        assert body["total"] == len(body["items"])

    def test_never_offers_the_callers_own_store_as_a_destination(self, branches):
        """A transfer to yourself is not a transfer. Do not put it in the picker."""
        res = branches["client"].get("/inventory/transfer-destinations")
        ids = {item["store_id"] for item in res.json()["items"]}
        assert DEFAULT_STORE_ID not in ids

    def test_carries_a_display_name_the_manager_can_actually_read(self, branches):
        res = branches["client"].get("/inventory/transfer-destinations")
        names = {item["name"] for item in res.json()["items"]}
        assert "Beşiktaş" in names

    def test_exposes_no_operational_data_about_the_other_branch(
        self, db, branches, chocolate
    ):
        """
        The picker must not become a window into a sibling branch. Give the other
        store real stock, then assert none of it — nor any other field — comes back.
        """
        stock_for(
            db, chocolate, branches["besiktas"].id, on_hand=Decimal("99.000")
        )

        res = branches["client"].get("/inventory/transfer-destinations")
        for item in res.json()["items"]:
            assert set(item.keys()) == {"store_id", "name", "location"}


class TestAuthorization:
    def test_anonymous_is_refused(self, client):
        res = client.get("/inventory/transfer-destinations")
        assert res.status_code == 401

    def test_cashier_cannot_enumerate_branches(self, db, make_staff):
        """
        CASHIER holds no inventory permission at all — money and stock are separate
        authorities — so it cannot reach the destination picker either.
        """
        cashier = make_staff("CASHIER", store_id=DEFAULT_STORE_ID)
        res = make_authed_client(db, cashier).get("/inventory/transfer-destinations")
        assert res.status_code == 403

    @pytest.mark.parametrize("role", ["OWNER", "MANAGER"])
    def test_owner_and_manager_may_read_it(self, db, make_staff, role):
        staff = make_staff(role, store_id=DEFAULT_STORE_ID)
        res = make_authed_client(db, staff).get("/inventory/transfer-destinations")
        assert res.status_code == 200

    def test_staff_with_no_store_is_refused(self, db, make_staff):
        """
        There is no chain-wide inventory view, so there is no chain-wide picker.

        The refusal lands EARLIER than the router: ``resolve_session`` returns None
        for any operational role with no store (auth_service), so the session never
        resolves and the request 401s before ``_store_id`` is ever consulted. That
        is a stronger guarantee than the router's own ``no_store_assigned`` 403 —
        which remains as a backstop for a role that is not in OPERATIONAL_ROLES —
        and it is the same guarantee every other inventory route already has.

        What matters for THIS endpoint: a storeless session cannot enumerate the
        chain's branches. It is asserted at whichever layer actually says no.
        """
        floating = make_staff("MANAGER", store_id=None)
        res = make_authed_client(db, floating).get("/inventory/transfer-destinations")
        assert res.status_code == 401


class TestTheListIsNotTheSecurityControl:
    def test_server_still_rejects_a_same_store_transfer(self, branches, chocolate):
        """
        Filtering the caller's own store out of the picker is a courtesy. If a client
        ignores the list and posts its own store id anyway, the SERVICE must still
        refuse — otherwise the UI would be the only thing standing between a manager
        and a pair of cancelling ledger movements.
        """
        res = branches["client"].post(
            "/inventory/transfers",
            json={
                "destination_store_id": DEFAULT_STORE_ID,  # the caller's OWN store
                "ingredient_id": chocolate.id,
                "quantity": "1.000",
                "reason": "Kendi şubesine transfer denemesi",
            },
            headers={"Idempotency-Key": "same-store-guard-test"},
        )
        assert res.status_code == 422
        assert res.json()["detail"]["error"] == "same_store_transfer"
