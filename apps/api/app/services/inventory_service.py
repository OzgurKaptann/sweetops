"""
Inventory Service — the ONE place where physical stock moves.

Guarantees
----------
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
   ingredient_id order, so multi-ingredient orders cannot deadlock each other.
5. Decimal end-to-end. Never binary floating point.
6. Manual mutations require an authenticated actor, an idempotency key, and (for
   waste and adjustments) a reason. Only hashes of the key/payload are stored.
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
    MOVEMENT_WASTE,
    IngredientStock,
    IngredientStockMovement,
    OrderInventoryLine,
)
from app.models.order import Order
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
    db: Session, ingredient_ids: Iterable[int]
) -> dict[int, IngredientStock]:
    """
    Lock the stock rows for these ingredients FOR UPDATE, in ascending
    ingredient_id order.

    The ordering is the whole point: two concurrent orders that both need
    chocolate (id 3) and banana (id 7) always take id 3 first, so one waits on
    the other instead of the two deadlocking head-to-head.

    populate_existing() overwrites any stale identity-map copy with the freshly
    locked row — without it a caller could validate availability against a value
    read before a competing transaction committed.
    """
    ids = sorted({int(i) for i in ingredient_ids})
    if not ids:
        return {}

    rows = db.execute(
        select(IngredientStock)
        .where(IngredientStock.ingredient_id.in_(ids))
        .order_by(IngredientStock.ingredient_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().all()

    return {row.ingredient_id: row for row in rows}


def _lock_order_lines(db: Session, order_id: int) -> list[OrderInventoryLine]:
    """Lock an order's inventory lines FOR UPDATE, ordered by ingredient_id."""
    return list(
        db.execute(
            select(OrderInventoryLine)
            .where(OrderInventoryLine.order_id == order_id)
            .order_by(OrderInventoryLine.ingredient_id, OrderInventoryLine.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalars().all()
    )


# ── Ledger ───────────────────────────────────────────────────────────────────

def _movement(
    db: Session,
    *,
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
) -> IngredientStockMovement:
    row = IngredientStockMovement(
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
    Reject the order unless every ingredient has enough AVAILABLE stock.

    Availability — not on-hand — is the gate. Batter already promised to the
    order two tables over is not batter this order may have.
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
    """
    reserved_by_ingredient: dict[int, Decimal] = {}

    for order_item_id, ingredient_id, quantity in line_requirements:
        qty = q3(quantity)
        if qty <= ZERO:
            continue
        ing = ingredients_by_id[ingredient_id]

        line = OrderInventoryLine(
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
    """
    lines = _lock_order_lines(db, order.id)
    pending = [(ln, outstanding_reservation(ln)) for ln in lines]
    pending = [(ln, qty) for ln, qty in pending if qty > ZERO]
    if not pending:
        return {}

    stock_rows = lock_stock_rows(db, [ln.ingredient_id for ln, _ in pending])

    consumed_by_ingredient: dict[int, Decimal] = {}
    for line, qty in pending:
        stock = stock_rows.get(line.ingredient_id)
        if stock is None:
            # A stock row cannot vanish while an order reserves against it (FK +
            # lock). Refuse rather than silently cook untracked stock.
            raise _conflict(
                messages.INVENTORY_INGREDIENT_NOT_FOUND, error="stock_row_missing"
            )

        line.consumed_quantity = q3(line.consumed_quantity) + qty
        stock.on_hand_quantity = q3(stock.on_hand_quantity) - qty
        stock.reserved_quantity = q3(stock.reserved_quantity) - qty

        _movement(
            db,
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
            "order_id": order.id,
            "consumed": {
                str(k): str(v) for k, v in sorted(consumed_by_ingredient.items())
            },
        },
    )
    logger.info(
        "inventory_consumed order=%s ingredients=%s",
        order.id, sorted(consumed_by_ingredient),
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
    """
    lines = _lock_order_lines(db, order.id)
    pending = [(ln, outstanding_reservation(ln)) for ln in lines]
    pending = [(ln, qty) for ln, qty in pending if qty > ZERO]
    if not pending:
        return {}

    stock_rows = lock_stock_rows(db, [ln.ingredient_id for ln, _ in pending])

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
            "order_id": order.id,
            "released": {
                str(k): str(v) for k, v in sorted(released_by_ingredient.items())
            },
        },
    )
    logger.info(
        "inventory_reservation_released order=%s ingredients=%s",
        order.id, sorted(released_by_ingredient),
    )
    return released_by_ingredient


# ═════════════════════════════════════════════════════════════════════════════
# Manual operations (staff-driven, idempotent, audited)
# ═════════════════════════════════════════════════════════════════════════════

def _load_stock_for_update(db: Session, ingredient_id: int) -> tuple[Ingredient, IngredientStock]:
    ingredient = db.get(Ingredient, ingredient_id)
    if ingredient is None or not ingredient.is_active:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "ingredient_not_found",
                "message": messages.INVENTORY_INGREDIENT_NOT_FOUND,
            },
        )
    stock_rows = lock_stock_rows(db, [ingredient_id])
    stock = stock_rows.get(ingredient_id)
    if stock is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "ingredient_not_found",
                "message": messages.INVENTORY_INGREDIENT_NOT_FOUND,
            },
        )
    return ingredient, stock


@dataclass(frozen=True)
class MovementResult:
    """A manual stock command's outcome, and whether it was an idempotent replay."""

    movement: IngredientStockMovement
    replayed: bool


def _find_movement_by_key(db: Session, key_hash: str) -> Optional[IngredientStockMovement]:
    return (
        db.query(IngredientStockMovement)
        .filter(IngredientStockMovement.idempotency_key_hash == key_hash)
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
    Shared body of every manual stock command: lock, re-check idempotency under
    the lock, validate the resulting physical state, write the ledger row, move
    the summary, audit, commit.
    """
    try:
        ingredient, stock = _load_stock_for_update(db, ingredient_id)

        # Definitive idempotency re-check now that we hold the row lock: an
        # identical concurrent command may have committed while we waited.
        existing = _find_movement_by_key(db, key_hash)
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
            "inventory_manual_movement type=%s ingredient=%s actor=%s delta=%s",
            movement_type, ingredient_id, actor_user_id, q3(delta_on_hand),
        )
        return MovementResult(movement=movement, replayed=False)

    except IntegrityError:
        # A concurrent command with the same key committed between our re-check
        # and the insert — the partial unique index caught it.
        db.rollback()
        existing = _find_movement_by_key(db, key_hash)
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
    ingredient_id: int,
    quantity,
    actor_user_id: int,
    reason: str | None = None,
    idempotency_key: str | None = None,
    ip_address: str | None = None,
) -> MovementResult:
    """Goods arrived from a supplier — physical stock goes up."""
    key = _require_key(idempotency_key)
    qty = _require_positive(quantity)
    note = (reason or "").strip() or None

    key_hash = _sha256(key)
    request_hash = _sha256(_canonical({
        "cmd": "purchase_receipt",
        "ingredient_id": ingredient_id,
        "quantity": str(qty),
        "reason": note or "",
    }))

    existing = _find_movement_by_key(db, key_hash)
    if existing is not None:
        return _resolve_replay(db, existing, request_hash)

    return _apply_manual_movement(
        db,
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
    ingredient_id: int,
    delta,
    reason: str,
    actor_user_id: int,
    idempotency_key: str | None = None,
    ip_address: str | None = None,
) -> MovementResult:
    """
    Correct on-hand stock to match a real physical count. ``delta`` may be
    positive or negative but never zero, and always needs a reason — an
    unexplained stock correction is indistinguishable from theft.
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

    existing = _find_movement_by_key(db, key_hash)
    if existing is not None:
        return _resolve_replay(db, existing, request_hash)

    return _apply_manual_movement(
        db,
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
    ingredient_id: int,
    quantity,
    reason: str,
    actor_user_id: int,
    idempotency_key: str | None = None,
    ip_address: str | None = None,
) -> MovementResult:
    """
    Stock physically thrown away (burnt, dropped, spoiled). On-hand falls and the
    loss stays visible in the ledger as WASTE — never quietly folded into
    consumption, because waste is a cost the owner must be able to see.
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

    existing = _find_movement_by_key(db, key_hash)
    if existing is not None:
        return _resolve_replay(db, existing, request_hash)

    return _apply_manual_movement(
        db,
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
