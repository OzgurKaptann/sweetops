"""
Store resolution for the UNGATED public menu — the last storeless inventory read.

History
-------
This module used to fail-close *every* inventory surface (staff stock, staff
movements, owner analytics, decision signals) whenever more than one operational
store existed, because ``ingredient_stock`` was global and there was no honest
way to decide whose stock a request was asking about.

Physical stock is now store-scoped, so all of those surfaces have a real answer:

    staff inventory  → the authenticated session's store
    owner analytics  → the authenticated session's store
    kitchen          → the order's store
    customer order   → the store the scanned QR token resolves to

None of them fail closed any more, and multi-store operation is no longer an
error condition. What remains here is the one surface that genuinely has no
store context to derive anything from:

    GET  /public/menu/          (ungated)
    GET  /public/menu/upsell    (ungated)
    POST /public/menu/validate  (ungated — no qr_token supplied)

These carry no QR token and no session, yet they report ``stock_status``, which
is physical and therefore belongs to exactly one branch. With two branches open
there is no non-arbitrary answer, and inventing one would mean telling a Kadıköy
customer that Beşiktaş has pistachio. So they fail closed.

This is a limitation of those endpoints, not of the inventory model. The QR-gated
paths the customer app actually uses — ``POST /public/menu/resolve`` and
``POST /public/menu/upsell`` — resolve a real store from the token and are fully
scoped. See docs/STORE_SCOPED_INVENTORY.md § "Remaining limitation".
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import messages
from app.models.ingredient_stock import IngredientStock
from app.models.store import Store
from app.models.user import User


def operational_store_ids(db: Session) -> list[int]:
    """
    Stores that are actually being operated — i.e. have at least one staff user
    assigned.

    Staffing, not mere existence, is the test. A Store row can be created ahead
    of a branch opening (or by a test fixture) without anybody working there and
    without a gram of stock on its shelves; counting those would fail-close the
    public menu of a shop that really does only run one branch.
    """
    return sorted(
        row[0]
        for row in db.query(func.distinct(User.store_id))
        .filter(User.store_id.isnot(None))
        .all()
    )


def operational_store_count(db: Session) -> int:
    return len(operational_store_ids(db))


def is_single_operational_store(db: Session) -> bool:
    return operational_store_count(db) <= 1


def stocked_store_ids(db: Session) -> list[int]:
    """
    Stores that actually hold physical stock.

    This — not staffing, and not the mere existence of a Store row — is the right
    signal for the ungated menu, because it is a direct answer to the only
    question that endpoint is asking: "whose shelves am I reporting?" A store
    with no stock rows cannot be that answer, whether it is a branch opening next
    month or a row a test fixture created a second ago.
    """
    return sorted(
        row[0]
        for row in db.query(func.distinct(IngredientStock.store_id)).all()
        if row[0] is not None
    )


def resolve_ungated_menu_store_id(db: Session) -> int:
    """
    The store whose stock an ungated public-menu read should report, or a
    structured Turkish 409 when that cannot be answered without guessing.

    Exactly one store holds stock → that store. This is the single-branch shop,
                                    which is the only shape these endpoints were
                                    ever able to serve honestly.
    No store holds stock yet      → the single Store row, if there is exactly one.
                                    A freshly seeded shop still has a menu; every
                                    ingredient simply reads out_of_stock, which is
                                    true.
    Anything else                 → 409. With two branches stocked, "is pistachio
                                    in stock?" has two different true answers and
                                    nothing in the request says which was asked.
                                    Refusing beats guessing: picking "store 1"
                                    would quietly show one branch's shelves to
                                    another branch's customers.
    """
    store_ids = stocked_store_ids(db)

    if len(store_ids) == 1:
        return store_ids[0]

    if not store_ids:
        all_stores = [row[0] for row in db.query(Store.id).all()]
        if len(all_stores) == 1:
            return all_stores[0]

    raise HTTPException(
        status_code=409,
        detail={
            "error": "inventory_store_context_required",
            "message": messages.INVENTORY_MULTISTORE_BLOCKED,
        },
    )
