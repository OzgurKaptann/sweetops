import os
import sys
import random
from datetime import datetime, timedelta
import logging

# Ensure we can import app modules from apps/api
current_dir = os.path.dirname(os.path.abspath(__file__))
api_dir = os.path.join(current_dir, '..', 'apps', 'api')
sys.path.insert(0, api_dir)
sys.path.insert(0, '/app') # For docker execution

from sqlalchemy.orm import Session
from app.core.db import SessionLocal, engine
from app.models.product import Product
from app.models.ingredient import Ingredient
from app.models.store import Store
from app.models.table import Table
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.models.order_status_event import OrderStatusEvent
from decimal import Decimal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def clear_existing_data(db: Session):
    logger.info("Clearing existing TRANSACTONAL data (Orders, Events)...")
    db.query(OrderStatusEvent).delete()
    db.query(OrderItemIngredient).delete()
    db.query(OrderItem).delete()
    db.query(Order).delete()
    db.commit()
    logger.info("Cleared.")

def generate_demo_data(db: Session):
    store = db.query(Store).first()
    if not store:
        logger.error("No store found. Please run regular seed.py first to create base entities.")
        return

    tables = db.query(Table).filter(Table.store_id == store.id).all()
    if not tables:
        logger.error("No tables found. Please run regular seed.py to create base entities.")
        return

    products = db.query(Product).all()
    ingredients = db.query(Ingredient).all()
    
    # Specific ingredient targets for Analytics (Nutella & Strawberries are heavy)
    nutella = next((i for i in ingredients if "Nutella" in i.name), None)
    batter = next((i for i in ingredients if "Waffle" in i.name), None)
    strawberry = next((i for i in ingredients if "Straw" in i.name), None)
    pistachio = next((i for i in ingredients if "Pistachio" in i.name), None)

    # 14 days ago to today
    today = datetime.now()
    start_date = today - timedelta(days=14)
    
    total_orders_created = 0

    for day_offset in range(15): # 0 to 14
        current_date = start_date + timedelta(days=day_offset)
        
        # Weekend bump: More orders on Fri, Sat, Sun
        is_weekend = current_date.weekday() >= 4 # Friday = 4
        base_orders = random.randint(3, 7) if not is_weekend else random.randint(8, 15)
        
        # Also create a trend: Last 3 days have slightly more Nutella demand
        is_recent = day_offset >= 11

        for _ in range(base_orders):
            # Orders usually happen between 12:00 and 22:00
            hour = random.randint(12, 21)
            minute = random.randint(0, 59)
            order_time = current_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            # Select a random valid table
            random_table = random.choice(tables)

            # Create Order
            order = Order(
                store_id=store.id,
                table_id=random_table.id,
                status="DELIVERED",
                total_amount=0, # Will calculate
            )
            # Override created_at to simulate history
            order.created_at = order_time
            db.add(order)
            db.flush()

            # Status Flow Events (To make prep-time analytics possible later)
            event_new = OrderStatusEvent(order_id=order.id, status_to="NEW")
            event_new.created_at = order_time
            
            event_prep = OrderStatusEvent(order_id=order.id, status_from="NEW", status_to="IN_PREP")
            event_prep.created_at = order_time + timedelta(minutes=random.randint(1, 4))
            
            event_ready = OrderStatusEvent(order_id=order.id, status_from="IN_PREP", status_to="READY")
            event_ready.created_at = event_prep.created_at + timedelta(minutes=random.randint(3, 10))

            event_delivered = OrderStatusEvent(order_id=order.id, status_from="READY", status_to="DELIVERED")
            event_delivered.created_at = event_ready.created_at + timedelta(minutes=random.randint(1, 5))

            db.add_all([event_new, event_prep, event_ready, event_delivered])

            # Add Items
            num_items = random.randint(1, 3)
            order_total = Decimal('0.00')
            
            for _ in range(num_items):
                p = random.choice(products)
                qty = random.randint(1, 2)
                item = OrderItem(
                    order_id=order.id,
                    product_id=p.id,
                    quantity=qty,
                    price=p.base_price
                )
                db.add(item)
                db.flush()
                
                item_total = p.base_price * qty
                
                # Ingredients Strategy
                # Always add batter if applicable
                added_ingredients = []
                if batter:
                    added_ingredients.append(batter)
                
                # Nutella Heavy in recent days
                if nutella and (is_recent or random.random() > 0.4):
                    added_ingredients.append(nutella)
                
                # Mix others
                for ing in [strawberry, pistachio]:
                    if ing and random.random() > 0.5:
                        added_ingredients.append(ing)

                for ing in added_ingredients:
                    ing_qty = random.randint(1, 2)
                    oi_ing = OrderItemIngredient(
                        order_item_id=item.id,
                        ingredient_id=ing.id,
                        quantity=ing_qty,
                        price_modifier=ing.price
                    )
                    db.add(oi_ing)
                    item_total += (ing.price * ing_qty)
                
                order_total += item_total

            order.total_amount = order_total
            total_orders_created += 1

    db.commit()
    logger.info(f"Successfully generated {total_orders_created} historical DELIVERED orders across 14 days.")

def main():
    logger.info("Starting DEMO SEED process...")
    try:
        db = SessionLocal()
        clear_existing_data(db)
        generate_demo_data(db)
        logger.info("Demo Seed Process DB Phase Completed.")
        logger.info("NOW: Please run `docker-compose run dbt run` to generate Analytics from this data!")
    finally:
        db.close()

if __name__ == "__main__":
    main()
