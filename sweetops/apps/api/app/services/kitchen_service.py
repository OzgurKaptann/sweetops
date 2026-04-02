from sqlalchemy.orm import Session
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.models.product import Product
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from app.models.order_status_event import OrderStatusEvent
from fastapi import HTTPException
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

def get_kitchen_orders(db: Session, store_id: int = 1):
    orders = db.query(Order).filter(
        Order.store_id == store_id,
        Order.status.in_(["NEW", "IN_PREP"])
    ).order_by(Order.created_at.asc()).all()
    
    result = []
    for order in orders:
        items_list = []
        for item in order.items:
            product = db.query(Product).filter(Product.id == item.product_id).first()
            
            ing_list = []
            for ing_rel in item.ingredients:
                ingredient = db.query(Ingredient).filter(Ingredient.id == ing_rel.ingredient_id).first()
                ing_list.append({
                    "id": ing_rel.id,
                    "ingredient_id": ing_rel.ingredient_id,
                    "ingredient_name": ingredient.name if ingredient else "Bilinmiyor",
                    "quantity": ing_rel.quantity
                })
                
            items_list.append({
                "id": item.id,
                "product_id": item.product_id,
                "product_name": product.name if product else "Bilinmiyor",
                "quantity": item.quantity,
                "ingredients": ing_list
            })
            
        result.append({
            "id": order.id,
            "store_id": order.store_id,
            "table_id": order.table_id,
            "status": order.status,
            "created_at": order.created_at,
            "items": items_list
        })
        
    return result

def _deduct_stock_for_order(db: Session, order: Order):
    """Deduct ingredient stock when order moves to IN_PREP."""
    # Idempotency guard: check if already deducted
    existing = db.query(IngredientStockMovement).filter(
        IngredientStockMovement.reference_type == "order",
        IngredientStockMovement.reference_id == order.id,
        IngredientStockMovement.movement_type == "ORDER_DEDUCTION",
    ).first()
    if existing:
        logger.info(f"Stock already deducted for order {order.id}, skipping.")
        return

    for item in order.items:
        for oi_ing in item.ingredients:
            if not oi_ing.consumed_quantity or not oi_ing.consumed_unit:
                continue

            consumed = float(oi_ing.consumed_quantity)

            # Create movement record
            movement = IngredientStockMovement(
                ingredient_id=oi_ing.ingredient_id,
                movement_type="ORDER_DEDUCTION",
                quantity_delta=-consumed,
                unit=oi_ing.consumed_unit,
                reference_type="order",
                reference_id=order.id,
            )
            db.add(movement)

            # Update cached stock
            stock = db.query(IngredientStock).filter(
                IngredientStock.ingredient_id == oi_ing.ingredient_id
            ).first()
            if stock:
                stock.stock_quantity = float(stock.stock_quantity) - consumed
                stock.updated_at = datetime.now(timezone.utc)

    logger.info(f"Stock deducted for order {order.id}")


def _return_stock_for_order(db: Session, order: Order):
    """Return ingredient stock when order is cancelled after IN_PREP."""
    # Only return if there was a deduction
    existing = db.query(IngredientStockMovement).filter(
        IngredientStockMovement.reference_type == "order",
        IngredientStockMovement.reference_id == order.id,
        IngredientStockMovement.movement_type == "ORDER_DEDUCTION",
    ).first()
    if not existing:
        return

    # Check if already returned
    already_returned = db.query(IngredientStockMovement).filter(
        IngredientStockMovement.reference_type == "order",
        IngredientStockMovement.reference_id == order.id,
        IngredientStockMovement.movement_type == "CANCELLATION_RETURN",
    ).first()
    if already_returned:
        return

    for item in order.items:
        for oi_ing in item.ingredients:
            if not oi_ing.consumed_quantity or not oi_ing.consumed_unit:
                continue

            consumed = float(oi_ing.consumed_quantity)

            movement = IngredientStockMovement(
                ingredient_id=oi_ing.ingredient_id,
                movement_type="CANCELLATION_RETURN",
                quantity_delta=consumed,  # positive = return
                unit=oi_ing.consumed_unit,
                reference_type="order",
                reference_id=order.id,
            )
            db.add(movement)

            stock = db.query(IngredientStock).filter(
                IngredientStock.ingredient_id == oi_ing.ingredient_id
            ).first()
            if stock:
                stock.stock_quantity = float(stock.stock_quantity) + consumed
                stock.updated_at = datetime.now(timezone.utc)

    logger.info(f"Stock returned for cancelled order {order.id}")


def update_order_status(db: Session, order_id: int, new_status: str, background_tasks):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    old_status = order.status

    # Validate status transitions
    valid_transitions = {
        "NEW": ["IN_PREP", "CANCELLED"],
        "IN_PREP": ["READY", "CANCELLED"],
        "READY": ["DELIVERED"],
    }
    allowed = valid_transitions.get(old_status, [])
    if new_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition from {old_status} to {new_status}"
        )

    order.status = new_status
    
    event = OrderStatusEvent(
        order_id=order.id,
        status_from=old_status,
        status_to=new_status
    )
    db.add(event)

    # Stock deduction on IN_PREP
    if new_status == "IN_PREP":
        _deduct_stock_for_order(db, order)

    # Stock return on cancellation after prep
    if new_status == "CANCELLED" and old_status == "IN_PREP":
        _return_stock_for_order(db, order)

    db.commit()
    db.refresh(order)

    # Broadcast status update to Kitchen WebSocket
    from app.services.websocket_manager import kitchen_ws_manager
    
    background_tasks.add_task(
        kitchen_ws_manager.broadcast_kitchen_event,
        event="order_status_updated",
        data={
            "order_id": order.id,
            "store_id": order.store_id,
            "status": new_status,
            "updated_at": event.created_at.isoformat()
        }
    )

    return order
