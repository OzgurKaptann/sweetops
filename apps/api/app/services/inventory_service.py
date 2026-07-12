"""
Inventory Service — the ONE place where physical stock moves.

Guarantees
----------
0. Every stock read and every stock write is scoped to exactly ONE store.
   ``store_id`` is a required argument of every function here, and it is never
   taken from a client: an order's store comes from its QR-derived
   ``order.store_id``, and a manual command's store comes from the authenticated
   staff session. Store A can neither see nor move Store B's stock, and the
   composite foreign keys in app/models/ingredient_stock.py mean the database
   refuses a cross-store row even if this module were wrong.
1. Reservation ≠ consumption. Placing an order reserves ingredients; only the
   kitchen physically starting preparation consumes them. On-hand stock tracks
   the real shop, not the order book.
2. Every mutation writes an append-only ledger row whose movement type states
   exactly what happened and how it moved on-hand and reserved. The database
   refuses UPDATE/DELETE on that ledger and refuses rows whose deltas do not
   match their type.
3. Exactly-once: consumption and release are both driven by the same
   ``outstanding = reserved - consumed - released`` expression on
   order_inventory_lines, under a row lock, and the database enforces
   ``consumed + released <= reserved``. Replays are therefore no-ops, not
   double mutations.
4. Deterministic locking: stock rows are always locked FOR UPDATE in ascending
   ingredient_id order within a store, so multi-ingredient orders cannot
   deadlock each other.
5. Decimal end-to-end. Never binary floating point.
6. Manual mutations require an authenticated actor, an idempotency key, and (for
   waste and adjustments) a reason. Only hashes of the key/payload are stored,
   and the key's uniqueness is per store.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional, Sequence

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.core import messages
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import (
    MOVEMENT_CONSUMPTION,
    MOVEMENT_MANUAL_ADJUSTMENT,
    MOVEMENT_PURCHASE_RECEIPT,
    MOVEMENT_RESERVATION_CREATED,
    MOVEMENT_RESERVATION_RELEASED,
    MOVEMENT_TRANSFER_IN,
    MOVEMENT_TRANSFER_OUT,
    MOVEMENT_WASTE,
    IngredientStock,
    IngredientStockMovement,
    OrderInventoryLine,
)
from app.models.inventory_transfer import TRANSFER_COMPLETED, InventoryTransfer
from app.models.order import Order
from app.models.store import Store
from app.services.audit_service import audit

logger = logging.getLogger(__name__)

THREE_PLACES = Decimal("0.001")
ZERO = Decimal("0")

# Audit actions (append-only forensic trail).
AUDIT_RESERVED = "INVENTORY_RESERVED"
AUDIT_RESERVATION_RELEASED = "INVENTORY_RESERVATION_RELEASED"
AUDIT_CONSUMED = "INVENTORY_CONSUMED"
AUDIT_WASTE_RECORDED = "INVENTORY_WASTE_RECORDED"
AUDIT_ADJUSTED = "INVENTORY_ADJUSTED"
AUDIT_RECEIVED = "INVENTORY_RECEIVED"
AUDIT_TRANSFERRED = "INVENTORY_TRANSFERRED"


# ── Quantity maths ───────────────────────────────────────────────────────────

def q3(value) -> Decimal:
    """Quantise any numeric to the 3 decimal places inventory is stored at."""
    return Decimal(str(value if value is not None else "0")).quantize(
        THREE_PLACES, rounding=ROUND_HALF_UP
    )


def available(stock: IngredientStock) -> Decimal:
    """
    Stock that a NEW order may still claim.

        available = on_hand - reserved

    This is the number order acceptance must test against — never on_hand alone,
    which would let the shop promise the same 200 g of pistachio to two tables.
    """
    return q3(stock.on_hand_quantity) - q3(stock.reserved_quantity)


def outstanding_reservation(line: OrderInventoryLine) -> Decimal:
    """The part of this line's reservation that is still neither consumed nor released."""
    return (
        q3(line.reserved_quantity)
        - q3(line.consumed_quantity)
        - q3(line.released_quantity)
    )


# ── Hashing (idempotency) ────────────────────────────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _require_key(idempotency_key: Optional[str]) -> str:
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=400,
            detail={
                "error": "idempotency_required",
                "message": messages.INVENTORY_IDEMPOTENCY_REQUIRED,
            },
        )
    return idempotency_key.strip()


def _conflict(message: str, error: str = "conflict") -> HTTPException:
    return HTTPException(status_code=409, detail={"error": error, "message": message})


# ── Deterministic row locking ────────────────────────────────────────────────

def lock_stock_rows(
    db: Session, store_id: int, ingredient_ids: Iterable[int]
) -> dict[int, IngredientStock]:
    """
    Lock THIS STORE's stock rows for these ingredients FOR UPDATE, in ascending
    ingredient_id order.

    The store filter is not an optimisation — it is the isolation boundary. It
    is the reason a Kadıköy order waits only on other Kadıköy orders, and the
    reason it can never lock, read, or spend a gram of Beşiktaş's chocolate.

    The ordering is the other half: two concurrent orders in the SAME store that
    both need chocolate (id 3) and banana (id 7) always take id 3 first, so one
    waits on the other instead of the two deadlocking head-to-head.

    populate_existing() overwrites any stale identity-map copy with the freshly
    locked row — without it a caller could validate availability against a value
    read before a competing transaction committed.
    """
    ids = sorted({int(i) for i in ingredient_ids})
    if not ids:
        return {}

    rows = db.execute(
        select(IngredientStock)
        .where(
            IngredientStock.store_id == store_id,
            IngredientStock.ingredient_id.in_(ids),
        )
        .order_by(IngredientStock.ingredient_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().all()

    return {row.ingredient_id: row for row in rows}


def _lock_order_lines(
    db: Session, store_id: int, order_id: int
) -> list[OrderInventoryLine]:
    """
    Lock an order's inventory lines FOR UPDATE, ordered by ingredient_id.

    Filtered by store as well as order. An order belongs to exactly one store,
    so this is belt-and-braces — but it means that even if a caller ever passed
    a mismatched (store, order) pair, it would settle nothing rather than
    settling another store's reservation.
    """
    return list(
        db.execute(
            select(OrderInventoryLine)
            .where(
                OrderInventoryLine.store_id == store_id,
                OrderInventoryLine.order_id == order_id,
            )
            .order_by(OrderInventoryLine.ingredient_id, OrderInventoryLine.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalars().all()
    )


# ── Ledger ───────────────────────────────────────────────────────────────────

def _movement(
    db: Session,
    *,
    store_id: int,
    ingredient_id: int,
    movement_type: str,
    quantity: Decimal,
    delta_on_hand: Decimal,
    delta_reserved: Decimal,
    unit: str,
    order_id: int | None = None,
    order_item_id: int | None = None,
    order_inventory_line_id: int | None = None,
    reason: str | None = None,
    actor_user_id: int | None = None,
    idempotency_key_hash: str | None = None,
    request_hash: str | None = None,
    transfer_id: int | None = None,
) -> IngredientStockMovement:
    row = IngredientStockMovement(
        store_id=store_id,
        ingredient_id=ingredient_id,
        movement_type=movement_type,
        quantity=q3(quantity),
        quantity_delta_on_hand=q3(delta_on_hand),
        quantity_delta_reserved=q3(delta_reserved),
        unit=unit,
        order_id=order_id,
        order_item_id=order_item_id,
        order_inventory_line_id=order_inventory_line_id,
        reason=reason,
        actor_user_id=actor_user_id,
        idempotency_key_hash=idempotency_key_hash,
        request_hash=request_hash,
        transfer_id=transfer_id,
    )
    db.add(row)
    return row


# ═════════════════════════════════════════════════════════════════════════════
# Order lifecycle
# ═════════════════════════════════════════════════════════════════════════════

class InsufficientStock(Exception):
    """Raised with the names of ingredients whose AVAILABLE quantity is short."""

    def __init__(self, ingredient_names: list[str]):
        self.ingredient_names = ingredient_names
        super().__init__(", ".join(ingredient_names))


def check_availability(
    stock_rows: dict[int, IngredientStock],
    required: dict[int, Decimal],
    ingredients_by_id: dict[int, Ingredient],
) -> None:
    """
    Reject the order unless every ingredient has enough AVAILABLE stock IN THIS
    STORE.

    ``stock_rows`` must come from ``lock_stock_rows(db, store_id, …)``, so it
    contains only this store's rows. Availability — not on-hand — is the gate:
    batter already promised to the order two tables over is not batter this
    order may have.

    A missing row means this branch does not stock the ingredient at all, and
    that is a shortage for this branch. It is never satisfied from another
    store's shelf.
    """
    short: list[str] = []
    for ing_id, needed in required.items():
        stock = stock_rows.get(ing_id)
        if stock is None or available(stock) < q3(needed):
            short.append(ingredients_by_id[ing_id].name)
    if short:
        raise InsufficientStock(short)


def reserve_for_order(
    db: Session,
    order: Order,
    line_requirements: Sequence[tuple[int, int, Decimal]],
    stock_rows: dict[int, IngredientStock],
    ingredients_by_id: dict[int, Ingredient],
    *,
    ip_address: str | None = None,
) -> None:
    """
    Create the order's inventory reservation.

    ``line_requirements`` is a sequence of (order_item_id, ingredient_id,
    quantity) already aggregated to one entry per (order_item, ingredient) —
    that is the deterministic grain of order_inventory_lines.

    Reserves only: reserved_quantity rises, on_hand_quantity is untouched. The
    caller must already hold the stock row locks (see lock_stock_rows) and must
    have validated availability.

    The store is ``order.store_id`` — derived server-side from the QR token the
    customer scanned, never from anything the client sent. It is stamped on the
    inventory line and on every ledger row, so a Store A order can only ever
    reserve Store A stock.
    """
    store_id = order.store_id
    reserved_by_ingredient: dict[int, Decimal] = {}

    for order_item_id, ingredient_id, quantity in line_requirements:
        qty = q3(quantity)
        if qty <= ZERO:
            continue
        ing = ingredients_by_id[ingredient_id]

        line = OrderInventoryLine(
            store_id=store_id,
            order_id=order.id,
            order_item_id=order_item_id,
            ingredient_id=ingredient_id,
            reserved_quantity=qty,
            unit=ing.unit,
        )
        db.add(line)
        db.flush()  # line.id, for the ledger's lineage column

        _movement(
            db,
            store_id=store_id,
            ingredient_id=ingredient_id,
            movement_type=MOVEMENT_RESERVATION_CREATED,
            quantity=qty,
            delta_on_hand=ZERO,
            delta_reserved=qty,
            unit=ing.unit,
            order_id=order.id,
            order_item_id=order_item_id,
            order_inventory_line_id=line.id,
        )

        reserved_by_ingredient[ingredient_id] = (
            reserved_by_ingredient.get(ingredient_id, ZERO) + qty
        )

    for ingredient_id, qty in reserved_by_ingredient.items():
        stock = stock_rows[ingredient_id]
        stock.reserved_quantity = q3(stock.reserved_quantity) + qty

    if reserved_by_ingredient:
        audit(
            db,
            entity_type="inventory",
            entity_id=order.id,
            action=AUDIT_RESERVED,
            # A customer places this order — there is no staff actor to name.
            actor_type="CUSTOMER",
            ip_address=ip_address,
            payload_after={
                "store_id": store_id,
                "order_id": order.id,
                "reserved": {
                    str(k): str(v) for k, v in sorted(reserved_by_ingredient.items())
                },
            },
        )


def consume_order(
    db: Session,
    order: Order,
    *,
    actor_type: str = "STAFF",
    actor_user_id: int | None = None,
    ip_address: str | None = None,
) -> dict[int, Decimal]:
    """
    Turn this order's outstanding reservation into physical consumption.

    Called when the kitchen actually starts cooking. Reserved falls, on-hand
    falls — the batter is really gone now.

    Exactly-once by construction: it consumes ``outstanding = reserved -
    consumed - released``, which is zero on any replay, so a second call is a
    no-op rather than a second deduction. Returns {ingredient_id: consumed}.

    Store scope comes from ``order.store_id``, so the kitchen that starts an
    order can only ever draw down the stock of the branch that order was placed
    in — even though the kitchen staff's own session store is what authorised
    the transition.
    """
    store_id = order.store_id
    lines = _lock_order_lines(db, store_id, order.id)
    pending = [(ln, outstanding_reservation(ln)) for ln in lines]
    pending = [(ln, qty) for ln, qty in pending if qty > ZERO]
    if not pending:
        return {}

    stock_rows = lock_stock_rows(db, store_id, [ln.ingredient_id for ln, _ in pending])

    consumed_by_ingredient: dict[int, Decimal] = {}
    for line, qty in pending:
        stock = stock_rows.get(line.ingredient_id)
        if stock is None:
            # A stock row cannot vanish while an order reserves against it in
            # this store (composite FK + lock). Refuse rather than silently cook
            # untracked stock — and never fall back to another store's row.
            raise _conflict(
                messages.INVENTORY_INGREDIENT_NOT_FOUND, error="stock_row_missing"
            )

        line.consumed_quantity = q3(line.consumed_quantity) + qty
        stock.on_hand_quantity = q3(stock.on_hand_quantity) - qty
        stock.reserved_quantity = q3(stock.reserved_quantity) - qty

        _movement(
            db,
            store_id=store_id,
            ingredient_id=line.ingredient_id,
            movement_type=MOVEMENT_CONSUMPTION,
            quantity=qty,
            delta_on_hand=-qty,
            delta_reserved=-qty,
            unit=line.unit,
            order_id=order.id,
            order_item_id=line.order_item_id,
            order_inventory_line_id=line.id,
            actor_user_id=actor_user_id,
        )

        consumed_by_ingredient[line.ingredient_id] = (
            consumed_by_ingredient.get(line.ingredient_id, ZERO) + qty
        )

    audit(
        db,
        entity_type="inventory",
        entity_id=order.id,
        action=AUDIT_CONSUMED,
        actor_type=actor_type,
        actor_id=str(actor_user_id) if actor_user_id is not None else None,
        ip_address=ip_address,
        payload_after={
            "store_id": store_id,
            "order_id": order.id,
            "consumed": {
                str(k): str(v) for k, v in sorted(consumed_by_ingredient.items())
            },
        },
    )
    logger.info(
        "inventory_consumed store=%s order=%s ingredients=%s",
        store_id, order.id, sorted(consumed_by_ingredient),
    )
    return consumed_by_ingredient


def release_order_reservation(
    db: Session,
    order: Order,
    *,
    actor_type: str = "STAFF",
    actor_user_id: int | None = None,
    ip_address: str | None = None,
) -> dict[int, Decimal]:
    """
    Release whatever of this order's reservation was never consumed.

    Called on cancellation. Reserved falls; on-hand is NOT touched — releasing a
    promise returns nothing physical to the shelf.

    Anything already consumed stays consumed: the kitchen really did pour that
    batter, and a cancellation cannot un-pour it. Returning usable stock is a
    deliberate, separate, actor-attributed RETURNED movement — never an implicit
    side effect of pressing cancel. Returns {ingredient_id: released}.

    Store scope comes from ``order.store_id``: cancelling a Store A order gives
    Store A its promised stock back, and touches nothing in Store B.
    """
    store_id = order.store_id
    lines = _lock_order_lines(db, store_id, order.id)
    pending = [(ln, outstanding_reservation(ln)) for ln in lines]
    pending = [(ln, qty) for ln, qty in pending if qty > ZERO]
    if not pending:
        return {}

    stock_rows = lock_stock_rows(db, store_id, [ln.ingredient_id for ln, _ in pending])

    released_by_ingredient: dict[int, Decimal] = {}
    for line, qty in pending:
        stock = stock_rows.get(line.ingredient_id)
        if stock is None:
            raise _conflict(
                messages.INVENTORY_INGREDIENT_NOT_FOUND, error="stock_row_missing"
            )

        line.released_quantity = q3(line.released_quantity) + qty
        stock.reserved_quantity = q3(stock.reserved_quantity) - qty

        _movement(
            db,
            store_id=store_id,
            ingredient_id=line.ingredient_id,
            movement_type=MOVEMENT_RESERVATION_RELEASED,
            quantity=qty,
            delta_on_hand=ZERO,
            delta_reserved=-qty,
            unit=line.unit,
            order_id=order.id,
            order_item_id=line.order_item_id,
            order_inventory_line_id=line.id,
            actor_user_id=actor_user_id,
        )

        released_by_ingredient[line.ingredient_id] = (
            released_by_ingredient.get(line.ingredient_id, ZERO) + qty
        )

    audit(
        db,
        entity_type="inventory",
        entity_id=order.id,
        action=AUDIT_RESERVATION_RELEASED,
        actor_type=actor_type,
        actor_id=str(actor_user_id) if actor_user_id is not None else None,
        ip_address=ip_address,
        payload_after={
            "store_id": store_id,
            "order_id": order.id,
            "released": {
                str(k): str(v) for k, v in sorted(released_by_ingredient.items())
            },
        },
    )
    logger.info(
        "inventory_reservation_released store=%s order=%s ingredients=%s",
        store_id, order.id, sorted(released_by_ingredient),
    )
    return released_by_ingredient


# ═════════════════════════════════════════════════════════════════════════════
# Manual operations (staff-driven, idempotent, audited)
# ═════════════════════════════════════════════════════════════════════════════

def _load_stock_for_update(
    db: Session, store_id: int, ingredient_id: int
) -> tuple[Ingredient, IngredientStock]:
    """
    Load the catalog ingredient and lock THIS STORE's stock row for it.

    The ingredient is catalog (global); the stock row is physical (store-scoped).
    A store that has never stocked an ingredient has no row, and that is a 404
    for that store — it is emphatically NOT a reason to reach for another
    store's row. Stock is initialised for a store explicitly, through a purchase
    receipt, an adjustment, or seed data.
    """
    ingredient = db.get(Ingredient, ingredient_id)
    if ingredient is None or not ingredient.is_active:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "ingredient_not_found",
                "message": messages.INVENTORY_INGREDIENT_NOT_FOUND,
            },
        )
    stock_rows = lock_stock_rows(db, store_id, [ingredient_id])
    stock = stock_rows.get(ingredient_id)
    if stock is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "stock_not_configured",
                "message": messages.INVENTORY_STOCK_NOT_CONFIGURED,
            },
        )
    return ingredient, stock


@dataclass(frozen=True)
class MovementResult:
    """A manual stock command's outcome, and whether it was an idempotent replay."""

    movement: IngredientStockMovement
    replayed: bool


def _find_movement_by_key(
    db: Session, store_id: int, key_hash: str
) -> Optional[IngredientStockMovement]:
    """
    Look up a previous manual movement by idempotency key WITHIN THIS STORE.

    The store filter is load-bearing. Two branch managers working from the same
    printed run-book will legitimately send the same Idempotency-Key; that is a
    coincidence, not a replay. Without the store in the lookup, Beşiktaş's 5 kg
    purchase receipt would return Kadıköy's receipt and quietly record no stock.
    """
    return (
        db.query(IngredientStockMovement)
        .filter(
            IngredientStockMovement.store_id == store_id,
            IngredientStockMovement.idempotency_key_hash == key_hash,
        )
        .first()
    )


def _resolve_replay(
    db: Session, existing: IngredientStockMovement, request_hash: str
) -> MovementResult:
    """
    Same key + same payload replays the original movement. Same key + a DIFFERENT
    payload is a client bug or an attack, and is refused with a 409 — replaying
    it under the original's result would silently discard the new intent.
    """
    if existing.request_hash != request_hash:
        raise _conflict(
            messages.INVENTORY_IDEMPOTENCY_MISMATCH, error="idempotency_mismatch"
        )
    return MovementResult(movement=existing, replayed=True)


def _require_reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if not text:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "reason_required",
                "message": messages.INVENTORY_REASON_REQUIRED,
            },
        )
    return text


def _require_positive(quantity) -> Decimal:
    qty = q3(quantity)
    if qty <= ZERO:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_quantity",
                "message": messages.INVENTORY_QUANTITY_INVALID,
            },
        )
    return qty


def _apply_manual_movement(
    db: Session,
    *,
    store_id: int,
    ingredient_id: int,
    movement_type: str,
    quantity: Decimal,
    delta_on_hand: Decimal,
    reason: str | None,
    actor_user_id: int,
    audit_action: str,
    key_hash: str,
    request_hash: str,
    ip_address: str | None,
) -> MovementResult:
    """
    Shared body of every manual stock command: lock this store's row, re-check
    idempotency under the lock, validate the resulting physical state, write the
    ledger row, move the summary, audit, commit.

    ``store_id`` is the authenticated staff member's store. It is never read
    from the request body or the query string, so a Store A manager cannot write
    off Store B's chocolate by naming Store B in a payload.
    """
    try:
        ingredient, stock = _load_stock_for_update(db, store_id, ingredient_id)

        # Definitive idempotency re-check now that we hold the row lock: an
        # identical concurrent command may have committed while we waited.
        existing = _find_movement_by_key(db, store_id, key_hash)
        if existing is not None:
            db.rollback()
            return _resolve_replay(db, existing, request_hash)

        new_on_hand = q3(stock.on_hand_quantity) + q3(delta_on_hand)
        if new_on_hand < ZERO or new_on_hand < q3(stock.reserved_quantity):
            # Refuse to write off stock that accepted orders are already
            # counting on — that would either go negative or silently break a
            # promise the shop has made to a waiting customer.
            raise _conflict(
                messages.INVENTORY_INSUFFICIENT_ON_HAND, error="insufficient_on_hand"
            )

        movement = _movement(
            db,
            store_id=store_id,
            ingredient_id=ingredient_id,
            movement_type=movement_type,
            quantity=q3(quantity),
            delta_on_hand=q3(delta_on_hand),
            delta_reserved=ZERO,
            unit=ingredient.unit,
            reason=reason,
            actor_user_id=actor_user_id,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
        )
        stock.on_hand_quantity = new_on_hand
        if movement_type == MOVEMENT_PURCHASE_RECEIPT:
            stock.last_restocked = func.now()

        audit(
            db,
            entity_type="inventory",
            entity_id=ingredient_id,
            action=audit_action,
            actor_type="STAFF",
            actor_id=str(actor_user_id),
            ip_address=ip_address,
            payload_after={
                "store_id": store_id,
                "ingredient_id": ingredient_id,
                "movement_type": movement_type,
                "quantity": str(q3(quantity)),
                "delta_on_hand": str(q3(delta_on_hand)),
                "on_hand_after": str(new_on_hand),
                "reason": reason,
            },
        )

        db.commit()
        db.refresh(movement)
        logger.info(
            "inventory_manual_movement store=%s type=%s ingredient=%s actor=%s delta=%s",
            store_id, movement_type, ingredient_id, actor_user_id, q3(delta_on_hand),
        )
        return MovementResult(movement=movement, replayed=False)

    except IntegrityError:
        # A concurrent command with the same key committed between our re-check
        # and the insert — the store-scoped partial unique index caught it.
        db.rollback()
        existing = _find_movement_by_key(db, store_id, key_hash)
        if existing is not None:
            return _resolve_replay(db, existing, request_hash)
        raise
    except HTTPException:
        db.rollback()
        raise


# ── Manual commands ──────────────────────────────────────────────────────────

def record_purchase_receipt(
    db: Session,
    *,
    store_id: int,
    ingredient_id: int,
    quantity,
    actor_user_id: int,
    reason: str | None = None,
    idempotency_key: str | None = None,
    ip_address: str | None = None,
) -> MovementResult:
    """
    Goods arrived from a supplier at THIS store — that store's physical stock
    goes up, and no other store's changes by a gram.

    This is also how a newly opened branch gets its first stock: a store never
    inherits another store's inventory.
    """
    key = _require_key(idempotency_key)
    qty = _require_positive(quantity)
    note = (reason or "").strip() or None

    key_hash = _sha256(key)
    # The payload hash covers the request BODY only. The store is not in the
    # body — it comes from the session — and it is already part of the
    # idempotency lookup, so a match here is always a match within one store.
    request_hash = _sha256(_canonical({
        "cmd": "purchase_receipt",
        "ingredient_id": ingredient_id,
        "quantity": str(qty),
        "reason": note or "",
    }))

    existing = _find_movement_by_key(db, store_id, key_hash)
    if existing is not None:
        return _resolve_replay(db, existing, request_hash)

    return _apply_manual_movement(
        db,
        store_id=store_id,
        ingredient_id=ingredient_id,
        movement_type=MOVEMENT_PURCHASE_RECEIPT,
        quantity=qty,
        delta_on_hand=qty,
        reason=note,
        actor_user_id=actor_user_id,
        audit_action=AUDIT_RECEIVED,
        key_hash=key_hash,
        request_hash=request_hash,
        ip_address=ip_address,
    )


def record_manual_adjustment(
    db: Session,
    *,
    store_id: int,
    ingredient_id: int,
    delta,
    reason: str,
    actor_user_id: int,
    idempotency_key: str | None = None,
    ip_address: str | None = None,
) -> MovementResult:
    """
    Correct THIS store's on-hand stock to match a real physical count. ``delta``
    may be positive or negative but never zero, and always needs a reason — an
    unexplained stock correction is indistinguishable from theft.

    A physical count is taken in one branch, of one branch's shelves. Applying
    it anywhere else would be meaningless, so the store is fixed by the session.
    """
    key = _require_key(idempotency_key)
    note = _require_reason(reason)

    signed = q3(delta)
    if signed == ZERO:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_quantity",
                "message": messages.INVENTORY_QUANTITY_INVALID,
            },
        )
    magnitude = abs(signed)

    key_hash = _sha256(key)
    request_hash = _sha256(_canonical({
        "cmd": "manual_adjustment",
        "ingredient_id": ingredient_id,
        "delta": str(signed),
        "reason": note,
    }))

    existing = _find_movement_by_key(db, store_id, key_hash)
    if existing is not None:
        return _resolve_replay(db, existing, request_hash)

    return _apply_manual_movement(
        db,
        store_id=store_id,
        ingredient_id=ingredient_id,
        movement_type=MOVEMENT_MANUAL_ADJUSTMENT,
        quantity=magnitude,
        delta_on_hand=signed,
        reason=note,
        actor_user_id=actor_user_id,
        audit_action=AUDIT_ADJUSTED,
        key_hash=key_hash,
        request_hash=request_hash,
        ip_address=ip_address,
    )


def record_waste(
    db: Session,
    *,
    store_id: int,
    ingredient_id: int,
    quantity,
    reason: str,
    actor_user_id: int,
    idempotency_key: str | None = None,
    ip_address: str | None = None,
) -> MovementResult:
    """
    Stock physically thrown away at THIS store (burnt, dropped, spoiled).
    On-hand falls and the loss stays visible in the ledger as WASTE — never
    quietly folded into consumption, because waste is a cost the owner must be
    able to see, and must be able to attribute to the branch that incurred it.
    """
    key = _require_key(idempotency_key)
    qty = _require_positive(quantity)
    note = _require_reason(reason)

    key_hash = _sha256(key)
    request_hash = _sha256(_canonical({
        "cmd": "waste",
        "ingredient_id": ingredient_id,
        "quantity": str(qty),
        "reason": note,
    }))

    existing = _find_movement_by_key(db, store_id, key_hash)
    if existing is not None:
        return _resolve_replay(db, existing, request_hash)

    return _apply_manual_movement(
        db,
        store_id=store_id,
        ingredient_id=ingredient_id,
        movement_type=MOVEMENT_WASTE,
        quantity=qty,
        delta_on_hand=-qty,
        reason=note,
        actor_user_id=actor_user_id,
        audit_action=AUDIT_WASTE_RECORDED,
        key_hash=key_hash,
        request_hash=request_hash,
        ip_address=ip_address,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Store-to-store transfer
# ═════════════════════════════════════════════════════════════════════════════
#
# A transfer is ONE business event with TWO ledger movements, not two unrelated
# manual adjustments. See app/models/inventory_transfer.py for why that
# distinction is the whole point, and docs/INVENTORY_TRANSFER_WORKFLOW.md for the
# operational consequences.


@dataclass(frozen=True)
class TransferResult:
    """A transfer's outcome, its two ledger legs, and whether it was a replay."""

    transfer: InventoryTransfer
    source_movement: IngredientStockMovement
    destination_movement: IngredientStockMovement
    replayed: bool


def _find_transfer_by_key(
    db: Session, source_store_id: int, key_hash: str
) -> Optional[InventoryTransfer]:
    """
    Look up a previous transfer by idempotency key WITHIN THIS SOURCE STORE.

    Scoped to the source store for the same reason the movement ledger's lookup
    is scoped to the store: two branch managers working from the same printed
    run-book will legitimately send the same Idempotency-Key, and that collision
    is a coincidence, not a replay. Without the store in the lookup, Beşiktaş's
    transfer would return Kadıköy's result and quietly ship nothing.
    """
    return (
        db.query(InventoryTransfer)
        .filter(
            InventoryTransfer.source_store_id == source_store_id,
            InventoryTransfer.idempotency_key_hash == key_hash,
        )
        .first()
    )


def _transfer_legs(
    db: Session, transfer: InventoryTransfer
) -> tuple[IngredientStockMovement, IngredientStockMovement]:
    """The OUT and IN movements of a transfer. Both always exist — the deferred
    pairing trigger refuses to commit a transfer that has anything else."""
    rows = (
        db.query(IngredientStockMovement)
        .filter(IngredientStockMovement.transfer_id == transfer.id)
        .all()
    )
    by_type = {m.movement_type: m for m in rows}
    return by_type[MOVEMENT_TRANSFER_OUT], by_type[MOVEMENT_TRANSFER_IN]


def _replay_transfer(
    db: Session, existing: InventoryTransfer, request_hash: str
) -> TransferResult:
    """
    Same key + same payload replays the original transfer, moving no further
    stock. Same key + a DIFFERENT payload is a client bug or an attack: replaying
    the original's result would silently discard the new intent — a manager who
    meant to ship 5 kg would be told the 2 kg they shipped an hour ago succeeded.
    """
    if existing.request_hash != request_hash:
        raise _conflict(
            messages.INVENTORY_IDEMPOTENCY_MISMATCH, error="idempotency_mismatch"
        )
    out_leg, in_leg = _transfer_legs(db, existing)
    return TransferResult(
        transfer=existing,
        source_movement=out_leg,
        destination_movement=in_leg,
        replayed=True,
    )


def _lock_transfer_stock(
    db: Session, ingredient_id: int, store_ids: Sequence[int]
) -> dict[int, IngredientStock]:
    """
    Lock BOTH stores' stock rows for one ingredient, FOR UPDATE, in ascending
    store_id order.

    The ordering is what prevents deadlock, and it is deliberately by store_id —
    NOT by "source first". Two managers shipping chocolate to each other at the
    same moment (Kadıköy → Beşiktaş and Beşiktaş → Kadıköy) would otherwise each
    hold the lock the other needs, and the pair would deadlock head-to-head.
    Ordering by store_id means both transactions reach for the lower-numbered
    store first, so one simply waits for the other. Together with the existing
    single-store rule (ascending ingredient_id), every stock lock in the system
    is now taken in ascending (store_id, ingredient_id) order.

    Returns {store_id: stock_row}. A store with no row for this ingredient is
    absent from the result rather than raising, so the caller can distinguish
    "source cannot ship what it does not stock" (an error) from "destination has
    never held this" (fine — the row is materialised at zero first).
    """
    rows = db.execute(
        select(IngredientStock)
        .where(
            IngredientStock.ingredient_id == ingredient_id,
            IngredientStock.store_id.in_(sorted(set(store_ids))),
        )
        .order_by(IngredientStock.store_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().all()
    return {row.store_id: row for row in rows}


def _ensure_destination_stock_row(
    db: Session, *, store_id: int, ingredient: Ingredient
) -> None:
    """
    Materialise the destination branch's stock row at ZERO if it has never held
    this ingredient.

    Policy, stated plainly: a transfer to a store that does not yet stock the
    ingredient CREATES the row rather than 404ing. This does not contradict
    "a new branch never inherits another branch's stock" — nothing is inherited
    and nothing is fabricated. The row starts at zero, and the only thing that
    puts stock in it is the TRANSFER_IN movement, which is exactly matched by a
    TRANSFER_OUT somewhere else. Chain-wide totals are unchanged to the gram.

    The alternative — refusing until someone books a purchase receipt — would
    force a manager stocking a newly opened branch from the warehouse branch to
    invent a supplier delivery that never happened, which is precisely the kind
    of lie about physical stock this whole module exists to prevent.

    ON CONFLICT DO NOTHING, so two concurrent first-ever transfers into the same
    branch cannot race to create duplicate rows (uq_stock_store_ingredient would
    reject the loser anyway; this makes it a no-op instead of an error).
    """
    db.execute(
        pg_insert(IngredientStock.__table__)
        .values(
            store_id=store_id,
            ingredient_id=ingredient.id,
            on_hand_quantity=ZERO,
            reserved_quantity=ZERO,
            unit=ingredient.unit,
        )
        .on_conflict_do_nothing(index_elements=["store_id", "ingredient_id"])
    )


def transfer_stock(
    db: Session,
    *,
    source_store_id: int,
    destination_store_id: int,
    ingredient_id: int,
    quantity,
    reason: str,
    note: str | None = None,
    actor_user_id: int,
    idempotency_key: str | None = None,
    ip_address: str | None = None,
) -> TransferResult:
    """
    Move stock from the caller's store to another store, atomically.

        source.on_hand      -= quantity      (TRANSFER_OUT, in the source store)
        destination.on_hand += quantity      (TRANSFER_IN,  in the destination store)

    Neither store's ``reserved`` changes: a transfer moves physical stock, it does
    not move anybody's promise to a customer.

    ``source_store_id`` is the authenticated staff member's store, passed by the
    router from the session. It is NEVER read from the request body — a Store A
    manager cannot ship Store B's chocolate by naming Store B as the source, and
    the database would refuse the row even if this function were wrong
    (fk_transfer_actor_source_store binds the initiator to the source store).

    What may NOT be transferred: stock that is already reserved for accepted
    orders. The gate is AVAILABLE (on_hand - reserved), not on-hand, because the
    batter promised to the table waiting in the corner is not batter this branch
    still has to give away. Refuses with 409 rather than silently breaking that
    promise.

    All-or-nothing: both legs, both summary updates, the transfer row and the
    audit record are one database transaction. There is no window in which the
    source has lost stock the destination has not gained — and if there somehow
    were, the deferred pairing trigger would refuse the COMMIT.
    """
    key = _require_key(idempotency_key)
    qty = _require_positive(quantity)
    why = _require_reason(reason)
    memo = (note or "").strip() or None

    key_hash = _sha256(key)
    # The payload hash covers the request BODY only. The source store is not in
    # the body — it comes from the session — and it is already part of the
    # idempotency lookup, so a match here is always a match within one source
    # store.
    request_hash = _sha256(_canonical({
        "cmd": "transfer",
        "destination_store_id": destination_store_id,
        "ingredient_id": ingredient_id,
        "quantity": str(qty),
        "reason": why,
        "note": memo or "",
    }))

    if destination_store_id == source_store_id:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "same_store_transfer",
                "message": messages.INVENTORY_TRANSFER_SAME_STORE,
            },
        )

    existing = _find_transfer_by_key(db, source_store_id, key_hash)
    if existing is not None:
        return _replay_transfer(db, existing, request_hash)

    try:
        destination = db.get(Store, destination_store_id)
        if destination is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "destination_store_not_found",
                    "message": messages.INVENTORY_TRANSFER_DESTINATION_NOT_FOUND,
                },
            )

        ingredient = db.get(Ingredient, ingredient_id)
        if ingredient is None or not ingredient.is_active:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "ingredient_not_found",
                    "message": messages.INVENTORY_INGREDIENT_NOT_FOUND,
                },
            )

        # The destination may never have held this ingredient. Give it a zeroed
        # row BEFORE the locks are taken, so the row exists to be locked and the
        # transfer's composite FK to it can be satisfied.
        _ensure_destination_stock_row(
            db, store_id=destination_store_id, ingredient=ingredient
        )

        stock = _lock_transfer_stock(
            db, ingredient_id, (source_store_id, destination_store_id)
        )
        source_stock = stock.get(source_store_id)
        destination_stock = stock.get(destination_store_id)
        if source_stock is None:
            # This branch has never stocked the ingredient, so it has nothing to
            # ship. It is emphatically NOT satisfied from a third store's shelf.
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "stock_not_configured",
                    "message": messages.INVENTORY_STOCK_NOT_CONFIGURED,
                },
            )

        # Definitive idempotency re-check now that we hold the row locks: an
        # identical concurrent transfer may have committed while we waited.
        existing = _find_transfer_by_key(db, source_store_id, key_hash)
        if existing is not None:
            db.rollback()
            return _replay_transfer(db, existing, request_hash)

        if available(source_stock) < qty:
            raise _conflict(
                messages.INVENTORY_TRANSFER_INSUFFICIENT_AVAILABLE,
                error="insufficient_available",
            )

        transfer = InventoryTransfer(
            source_store_id=source_store_id,
            destination_store_id=destination_store_id,
            ingredient_id=ingredient_id,
            quantity=qty,
            unit=ingredient.unit,
            status=TRANSFER_COMPLETED,
            reason=why,
            note=memo,
            initiated_by_user_id=actor_user_id,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
        )
        db.add(transfer)
        db.flush()  # transfer.id, which both legs must carry

        out_leg = _movement(
            db,
            store_id=source_store_id,
            ingredient_id=ingredient_id,
            movement_type=MOVEMENT_TRANSFER_OUT,
            quantity=qty,
            delta_on_hand=-qty,
            delta_reserved=ZERO,
            unit=ingredient.unit,
            reason=why,
            actor_user_id=actor_user_id,
            transfer_id=transfer.id,
        )
        in_leg = _movement(
            db,
            store_id=destination_store_id,
            ingredient_id=ingredient_id,
            movement_type=MOVEMENT_TRANSFER_IN,
            quantity=qty,
            delta_on_hand=qty,
            delta_reserved=ZERO,
            unit=ingredient.unit,
            reason=why,
            # No actor: the initiator belongs to the SOURCE store, and staff only
            # move stock in their own store. Accountability is the transfer row's
            # initiated_by_user_id. See ck_movement_transfer_in_no_actor.
            actor_user_id=None,
            transfer_id=transfer.id,
        )

        source_stock.on_hand_quantity = q3(source_stock.on_hand_quantity) - qty
        destination_stock.on_hand_quantity = (
            q3(destination_stock.on_hand_quantity) + qty
        )
        # reserved_quantity is deliberately untouched on BOTH sides. Stock moved;
        # nobody's promise did.

        audit(
            db,
            entity_type="inventory_transfer",
            entity_id=transfer.id,
            action=AUDIT_TRANSFERRED,
            actor_type="STAFF",
            actor_id=str(actor_user_id),
            ip_address=ip_address,
            # No session token, no CSRF token, no idempotency key and no request
            # hash: an audit trail that leaks a replayable credential is a
            # liability, not a control.
            payload_after={
                "transfer_id": transfer.id,
                "source_store_id": source_store_id,
                "destination_store_id": destination_store_id,
                "ingredient_id": ingredient_id,
                "quantity": str(qty),
                "unit": ingredient.unit,
                "actor_user_id": actor_user_id,
                "reason": why,
                "status": TRANSFER_COMPLETED,
            },
        )

        # The deferred pairing trigger fires HERE. If either leg were missing or
        # mismatched, this COMMIT raises and no stock has moved on either side.
        db.commit()
        db.refresh(transfer)
        db.refresh(out_leg)
        db.refresh(in_leg)
        logger.info(
            "inventory_transferred transfer=%s src=%s dst=%s ingredient=%s qty=%s actor=%s",
            transfer.id, source_store_id, destination_store_id,
            ingredient_id, qty, actor_user_id,
        )
        return TransferResult(
            transfer=transfer,
            source_movement=out_leg,
            destination_movement=in_leg,
            replayed=False,
        )

    except IntegrityError:
        # A concurrent transfer with the same key committed between our re-check
        # and the insert — uq_transfer_source_idem caught it. Nothing of ours was
        # written; return the winner's result rather than double-shipping.
        db.rollback()
        existing = _find_transfer_by_key(db, source_store_id, key_hash)
        if existing is not None:
            return _replay_transfer(db, existing, request_hash)
        raise
    except HTTPException:
        db.rollback()
        raise
