from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.models.product import Product
from app.models.ingredient import Ingredient
from app.models.order_status_event import OrderStatusEvent
from fastapi import HTTPException

def get_kitchen_orders(db: Session, store_id: int = 1):
    # Eager load the required nested relationships
    orders = db.query(Order).filter(
        Order.store_id == store_id,
        Order.status.in_(["NEW", "IN_PREP"])
    ).order_by(Order.created_at.asc()).all()
    
    # Shape the response explicitly for the KDS
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
                    "ingredient_name": ingredient.name if ingredient else "Unknown",
                    "quantity": ing_rel.quantity
                })
                
            items_list.append({
                "id": item.id,
                "product_id": item.product_id,
                "product_name": product.name if product else "Unknown",
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

def update_order_status(db: Session, order_id: int, new_status: str, background_tasks):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
        
    old_status = order.status
    order.status = new_status
    
    event = OrderStatusEvent(
        order_id=order.id,
        status_from=old_status,
        status_to=new_status
    )
    db.add(event)
    db.commit()
    db.refresh(order)

    # Broadcast status update event to Kitchen WebSocket safely
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
