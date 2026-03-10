from fastapi import BackgroundTasks
from sqlalchemy.orm import Session
from app.schemas.order import OrderCreateRequest
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.models.order_status_event import OrderStatusEvent
from app.models.product import Product
from app.models.ingredient import Ingredient
from decimal import Decimal

def create_order(db: Session, order_data: OrderCreateRequest, background_tasks: BackgroundTasks):
    # Calculate totals
    total_amount = Decimal('0.00')
    
    # Create the base order
    new_order = Order(
        store_id=order_data.store_id,
        table_id=order_data.table_id,
        status="NEW",
        total_amount=0 # Will update after items
    )
    db.add(new_order)
    db.flush() # Secure the order ID
    
    # Log the status event for analytics
    status_event = OrderStatusEvent(
        order_id=new_order.id,
        status_to="NEW"
    )
    db.add(status_event)
    
    item_count = 0
    
    for item_data in order_data.items:
        # Get product price (In a real scenario, handle Not Found errors properly)
        product = db.query(Product).filter(Product.id == item_data.product_id).first()
        base_price = product.base_price if product else Decimal('0.00')
        
        new_item = OrderItem(
            order_id=new_order.id,
            product_id=item_data.product_id,
            quantity=item_data.quantity,
            price=base_price
        )
        db.add(new_item)
        db.flush()
        item_count += item_data.quantity
        
        item_total = base_price * item_data.quantity
        
        for ing_data in item_data.ingredients:
            ingredient = db.query(Ingredient).filter(Ingredient.id == ing_data.ingredient_id).first()
            price_mod = ingredient.price if ingredient else Decimal('0.00')
            
            new_ing = OrderItemIngredient(
                order_item_id=new_item.id,
                ingredient_id=ing_data.ingredient_id,
                quantity=ing_data.quantity,
                price_modifier=price_mod
            )
            db.add(new_ing)
            item_total += (price_mod * ing_data.quantity)
            
        total_amount += item_total
    
    # Update total
    new_order.total_amount = total_amount
    db.commit()
    db.refresh(new_order)

    # Broadcast order_created event to Kitchen WebSocket safely
    from app.services.websocket_manager import kitchen_ws_manager
    
    background_tasks.add_task(
        kitchen_ws_manager.broadcast_kitchen_event,
        event="order_created",
        data={
            "order_id": new_order.id,
            "store_id": new_order.store_id,
            "table_id": new_order.table_id,
            "status": new_order.status,
            "created_at": new_order.created_at.isoformat()
        }
    )

    from app.schemas.order import OrderCreatedResponse

    res = OrderCreatedResponse(
        order_id=new_order.id,
        store_id=new_order.store_id,
        table_id=new_order.table_id,
        status=new_order.status,
        total_amount=new_order.total_amount,
        item_count=item_count,
        created_at=new_order.created_at
    )
    return res, item_count
