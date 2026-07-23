"""
Order Service — production-grade order creation.

Key guarantees:
  0. Store-scoped menu validation: an order may only contain products the
     resolved store actually publishes (store_products), that are active in the
     catalog, in a quantity inside the bounds the schema enforces. Nothing the
     client sends can widen any of the three.
  1. Idempotent: same idempotency_key returns existing order, never duplicates
     and never double-reserves — including under a concurrent retry, where the
     unique key constraint makes the loser return the winner's order.
  2. Transactional stock validation: SELECT ... FOR UPDATE row locks (taken in
     ascending ingredient_id order) prevent concurrent over-reservation. If any
     ingredient is short, the whole transaction rolls back with a 422.
  3. Order creation RESERVES stock; it does not consume it. Physical on-hand
     stock only falls when the kitchen actually starts cooking. A customer is
     still rejected immediately when stock is unavailable — availability is
     tested against (on_hand - reserved), so the shop can never promise the same
     ingredient twice.
  4. Every mutation is audit-logged inside the same transaction.
"""
import logging
from datetime import timezone
from decimal import Decimal
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import messages
from app.core.config import settings
from app.services.qr_token_service import resolve_token, QrTableUnavailable
from app.models.audit_log import AuditLog  # noqa — ensure model registered
from app.models.ingredient import Ingredient
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.models.order_status_event import OrderStatusEvent
from app.models.product import Product
from app.models.store_product import StoreProduct
from app.schemas.order import OrderCreateRequest, OrderCreatedResponse
from app.services import inventory_service
from app.services.audit_service import audit
from app.services.inventory_service import InsufficientStock

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical quantity math
# ---------------------------------------------------------------------------

def calculate_consumed_quantity(
    standard_quantity: Decimal,
    selected_quantity: int,
    item_quantity: int,
) -> Decimal:
    """
    Ingredient quantity required by a single order-item line.

    required = standard_quantity (per portion)
             × selected_quantity (portions per product)
             × item_quantity     (number of products ordered)

    This is the ONE canonical formula. It is reused for availability validation,
    the persisted per-line requirement, the reservation, the ledger movement
    and — via order_inventory_lines — the eventual consumption and release, so
    those figures can never drift apart.

    Note on naming: this quantity is what the order WILL consume if it is
    cooked. Under the reservation lifecycle it is reserved at creation and only
    becomes physical consumption at start-of-preparation. The function name (and
    the OrderItemIngredient.consumed_quantity column it feeds) predate the
    lifecycle and are kept to avoid a gratuitous rename of a stable contract;
    the authoritative lifecycle state lives in order_inventory_lines.
    """
    return standard_quantity * selected_quantity * item_quantity


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_order(
    db: Session,
    order_data: OrderCreateRequest,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = None,
    ip_address: str | None = None,
) -> OrderCreatedResponse:
    """
    Create an order.

    Menu context:
        Every ordered product must be published by the store the QR token
        resolved to, active in the catalog, and available today. A product id
        that fails any of those is refused with a Turkish 422 before any stock
        is locked — the client's rendered menu is never taken as evidence.
        See ``_resolve_menu_products``.

    Idempotency:
        If idempotency_key already exists in the orders table, returns the
        existing order immediately without touching stock. A concurrent retry
        that races past that check is caught by the unique constraint on
        orders.idempotency_key and resolved to the same order — so a retry can
        never produce a second reservation.

    Stock:
        Availability (on_hand - reserved) for every required ingredient is
        checked inside a single transaction under SELECT … FOR UPDATE row locks
        taken in ascending ingredient_id order. Any shortfall triggers a 422
        listing the unavailable ingredients. On success the order RESERVES those
        quantities atomically in the same transaction — physical on-hand stock
        is untouched until the kitchen starts preparation.
    """
    # ── 1. Idempotency check ─────────────────────────────────────────────
    if idempotency_key:
        existing = db.query(Order).filter(
            Order.idempotency_key == idempotency_key
        ).first()
        if existing:
            logger.info("idempotency_hit order_id=%s key=%s", existing.id, idempotency_key)
            return _build_response(existing)

    # ── 1b. Derive trusted store/table context from the QR token ─────────
    # The QR token is the ONLY trusted source of store/table. It is resolved
    # inside this transaction with a row lock (`for_update`) so a concurrent
    # revoke/rotate serializes behind us — an order can never be created on a
    # token that was revoked before this transaction validated it. Any
    # client-supplied store_id/table_id are ignored whenever a token is present.
    store_id, table_id = _derive_order_context(db, order_data)

    # ── 1c. Validate the products against THAT store's menu ──────────────
    # Runs before any stock is touched, and against the store derived above —
    # never against a store_id the client sent. A product the branch does not
    # publish cannot be ordered from it even if the id is real and the guest
    # read it off another branch's menu.
    products_by_id = _resolve_menu_products(db, order_data, store_id)

    # ── 2. Resolve products & ingredients (outside lock — read-only) ─────
    ingredient_ids: list[int] = []
    for item in order_data.items:
        for ing in item.ingredients:
            ingredient_ids.append(ing.ingredient_id)

    if not ingredient_ids:
        raise HTTPException(status_code=422, detail=messages.ORDER_NO_INGREDIENTS)

    # Fetch ingredient metadata once
    ingredients_by_id: dict[int, Ingredient] = {
        ing.id: ing
        for ing in db.query(Ingredient).filter(
            Ingredient.id.in_(ingredient_ids),
            Ingredient.is_active == True,  # noqa: E712
        ).all()
    }

    # Validate all requested ingredients exist and are active
    missing = [iid for iid in ingredient_ids if iid not in ingredients_by_id]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown_ingredients", "ids": missing},
        )

    # ── 3. Compute required stock per ingredient ─────────────────────────
    # Map ingredient_id → total consumed_quantity needed for this order
    required: dict[int, Decimal] = {}
    for item in order_data.items:
        for ing_req in item.ingredients:
            ing = ingredients_by_id[ing_req.ingredient_id]
            consumed = calculate_consumed_quantity(
                ing.standard_quantity or Decimal("1"),
                ing_req.quantity,
                item.quantity,
            )
            required[ing.id] = required.get(ing.id, Decimal("0")) + consumed

    # ── 4. Availability validation — SELECT … FOR UPDATE (row-level lock) ─
    # Locks THIS STORE's ingredient_stock rows, in ascending ingredient_id order,
    # so concurrent orders queue deterministically instead of deadlocking. The
    # gate is AVAILABLE (on_hand - reserved), never on_hand: stock already
    # promised to another table's accepted order is not stock this order may
    # claim.
    #
    # `store_id` is the one derived from the QR token above — never from the
    # request body. That single argument is what stops a Kadıköy QR order from
    # reserving, or even reading, Beşiktaş's chocolate: the two branches hold
    # separate stock rows and separate locks, and the same ingredient can be
    # plentiful in one and sold out in the other.
    stock_rows = inventory_service.lock_stock_rows(db, store_id, required.keys())
    try:
        inventory_service.check_availability(stock_rows, required, ingredients_by_id)
    except InsufficientStock as short:
        # Nothing has been written yet — reject without mutating any stock.
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail={"error": "out_of_stock", "items": short.ingredient_names},
        )

    try:
        # ── 5. Build order inside transaction ────────────────────────────
        new_order = Order(
            store_id=store_id,
            table_id=table_id,
            status="NEW",
            total_amount=Decimal("0.00"),
            idempotency_key=idempotency_key,
        )
        db.add(new_order)
        db.flush()  # get new_order.id without committing

        # Status event
        db.add(OrderStatusEvent(
            order_id=new_order.id,
            status_from=None,
            status_to="NEW",
            actor_type="CUSTOMER",
        ))

        # ── 6. Order items + price calculation ───────────────────────────
        total_amount = Decimal("0.00")
        # (order_item_id, ingredient_id) → quantity. This is the deterministic
        # grain of order_inventory_lines; aggregating here means an item that
        # names the same ingredient twice still yields exactly one line.
        line_requirements: dict[tuple[int, int], Decimal] = {}

        for item_data in order_data.items:
            # Validated in step 1c against this store's published menu — the
            # price charged is therefore always the price of a product this
            # branch actually offers.
            product = products_by_id[item_data.product_id]

            base_price = product.base_price
            item_total = base_price * item_data.quantity

            new_item = OrderItem(
                order_id=new_order.id,
                product_id=item_data.product_id,
                quantity=item_data.quantity,
                price=base_price,
            )
            db.add(new_item)
            db.flush()

            for ing_data in item_data.ingredients:
                ing = ingredients_by_id[ing_data.ingredient_id]
                required_qty = calculate_consumed_quantity(
                    ing.standard_quantity or Decimal("1"),
                    ing_data.quantity,
                    item_data.quantity,
                )

                db.add(OrderItemIngredient(
                    order_item_id=new_item.id,
                    ingredient_id=ing_data.ingredient_id,
                    quantity=ing_data.quantity,
                    price_modifier=ing.price,
                    consumed_quantity=required_qty,
                    consumed_unit=ing.unit,
                ))
                key = (new_item.id, ing_data.ingredient_id)
                line_requirements[key] = line_requirements.get(key, Decimal("0")) + required_qty

                item_total += ing.price * ing_data.quantity * item_data.quantity

            total_amount += item_total

        new_order.total_amount = total_amount
        db.flush()

        # ── 7. Reserve stock (same transaction, locks already held) ───────
        # Reserved rises; on-hand does NOT. Nothing has been cooked yet.
        inventory_service.reserve_for_order(
            db,
            new_order,
            [(oi_id, ing_id, qty) for (oi_id, ing_id), qty in line_requirements.items()],
            stock_rows,
            ingredients_by_id,
            ip_address=ip_address,
        )

        # ── 8. Audit log ─────────────────────────────────────────────────
        try:
            audit(
                db,
                entity_type="order",
                entity_id=new_order.id,
                action="created",
                actor_type="CUSTOMER",
                ip_address=ip_address,
                payload_after={
                    "store_id": new_order.store_id,
                    "table_id": new_order.table_id,
                    "total_amount": new_order.total_amount,
                    "ingredient_ids": list(required.keys()),
                },
            )
        except Exception as exc:  # pragma: no cover
            logger.error("audit_call_failed order_created err=%s", exc)

        db.commit()
    except IntegrityError:
        # A concurrent request with the SAME idempotency key committed while we
        # were building this order. The unique constraint on
        # orders.idempotency_key rejected the duplicate — resolve to the winner
        # rather than surfacing a 500, so a retry storm still reserves once.
        db.rollback()
        if idempotency_key:
            winner = db.query(Order).filter(
                Order.idempotency_key == idempotency_key
            ).first()
            if winner is not None:
                logger.info(
                    "idempotency_race_resolved order_id=%s", winner.id
                )
                return _build_response(winner)
        raise
    except HTTPException:
        db.rollback()
        raise

    db.refresh(new_order)

    logger.info("order_created id=%s store=%s total=%s",
                new_order.id, new_order.store_id, new_order.total_amount)

    # ── 9. Broadcast (after commit — data is durable) ────────────────────
    from app.services.websocket_manager import kitchen_ws_manager
    # Slot count = distinct ingredient rows (one prep step each)
    ingredient_slot_count = sum(
        len(item.ingredients)
        for item in order_data.items
    )
    created_utc = new_order.created_at
    if created_utc.tzinfo is None:
        created_utc = created_utc.replace(tzinfo=timezone.utc)
    background_tasks.add_task(
        kitchen_ws_manager.broadcast_kitchen_event,
        store_id=new_order.store_id,
        event="order_created",
        data={
            "order_id": new_order.id,
            "store_id": new_order.store_id,
            "table_id": new_order.table_id,
            "status": new_order.status,
            "ingredient_slot_count": ingredient_slot_count,
            "priority_score": round(ingredient_slot_count * 0.3, 2),  # age≈0 at creation
            "sla_severity": "ok",
            "created_at": created_utc.isoformat(),
        },
    )

    return _build_response(new_order)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_menu_products(
    db: Session, order_data: OrderCreateRequest, store_id: int
) -> dict[int, Product]:
    """
    Resolve every ordered product against the MENU OF THE RESOLVED STORE.

    A product id in the request body is a claim, and this is where the claim is
    checked. It survives only if all three hold:

      * the product exists;
      * it is active in the catalog (not retired chain-wide);
      * this branch has published it and has not switched it off today —
        i.e. there is a ``store_products`` row for (store_id, product_id) with
        is_available.

    The store is the one derived from the QR token in ``_derive_order_context``,
    so the check cannot be moved by anything the client sends. That is what
    makes it a boundary rather than a formality: the frontend renders a menu,
    but the menu the frontend rendered is not evidence of anything here.

    Everything that fails collapses into ONE Turkish 422 with the machine
    code ``product_unavailable``. A guest can only ever do one thing about it —
    pick something that IS on the menu in front of them — and a per-reason
    response would let a probe map which product ids exist in other branches.
    """
    requested = {item.product_id for item in order_data.items}

    offered: dict[int, Product] = {
        product.id: product
        for product in (
            db.query(Product)
            .join(StoreProduct, StoreProduct.product_id == Product.id)
            .filter(
                Product.id.in_(requested),
                Product.is_active == True,          # noqa: E712
                StoreProduct.store_id == store_id,
                StoreProduct.is_available == True,  # noqa: E712
            )
            .all()
        )
    }

    rejected = sorted(requested - offered.keys())
    if rejected:
        logger.info(
            "order_rejected_product_not_on_menu store_id=%s product_ids=%s",
            store_id,
            rejected,
        )
        raise HTTPException(
            status_code=422,
            detail={
                "error": "product_unavailable",
                "message": messages.ORDER_PRODUCT_UNAVAILABLE,
                "ids": rejected,
            },
        )

    return offered


def _derive_order_context(
    db: Session, order_data: OrderCreateRequest
) -> tuple[int, int | None]:
    """
    Determine the trusted (store_id, table_id) for an order.

    Priority:
      1. If a qr_token is present, resolve it (row-locked) and derive both ids
         from it — client-supplied store_id/table_id are ignored entirely.
      2. Otherwise, only if settings.ALLOW_LEGACY_ORDER_CONTEXT is enabled
         (non-production transition mode) fall back to the client store_id.
      3. Otherwise reject: production never trusts client-supplied context.
    """
    if order_data.qr_token:
        try:
            ctx = resolve_token(db, order_data.qr_token, touch=True, for_update=True)
        except QrTableUnavailable:
            raise HTTPException(status_code=409, detail=messages.QR_UNAVAILABLE)
        if ctx is None:
            raise HTTPException(status_code=404, detail=messages.QR_INVALID)
        return ctx.store_id, ctx.table_id

    if settings.ALLOW_LEGACY_ORDER_CONTEXT and order_data.store_id is not None:
        return order_data.store_id, order_data.table_id

    raise HTTPException(status_code=400, detail=messages.QR_REQUIRED)


def _build_response(order: Order) -> OrderCreatedResponse:
    item_count = sum(item.quantity for item in order.items)
    return OrderCreatedResponse(
        order_id=order.id,
        store_id=order.store_id,
        table_id=order.table_id,
        status=order.status,
        total_amount=order.total_amount,
        item_count=item_count,
        created_at=order.created_at,
    )
