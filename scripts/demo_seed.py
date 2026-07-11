"""
SweetOps Demo Seed v2 — Turkish Waffle Catalog
Generates 14 days of realistic waffle order history with:
- Weekend demand spikes
- Nutella/Çilek trending up in recent days
- Consumption snapshots on all order_item_ingredients
- Stock deductions via movement ledger
- Full status flow with realistic prep times (3-8 min)
"""
import os
import sys
import random
from datetime import datetime, timedelta, timezone
import logging
from decimal import Decimal

# Path setup for imports
current_dir = os.path.dirname(os.path.abspath(__file__))
api_dir = os.path.join(current_dir, '..', 'apps', 'api')
sys.path.insert(0, api_dir)
sys.path.insert(0, '/app')

from sqlalchemy import text
from sqlalchemy.orm import Session
from app.core.db import SessionLocal
from app.models.product import Product
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import (
    IngredientStock,
    IngredientStockMovement,
    OrderInventoryLine,
)
from app.models.store import Store
from app.models.table import Table
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.models.order_status_event import OrderStatusEvent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Popular combos for realistic ordering ---
POPULAR_COMBOS = [
    ["Nutella", "Muz"],
    ["Nutella", "Çilek", "Fındık"],
    ["Kinder Bueno", "Muz", "Çikolata Topları"],
    ["Lotus Biscoff", "Karamel"],
    ["Bitter Çikolata", "Fıstık", "Çilek"],
    ["Nutella", "Oreo", "Muz"],
    ["Frambuazlı Çikolata", "Çilek"],
    ["Karamel", "Muz", "Bonibon"],
    ["Nutella", "Lotus Biscoff", "Fındık"],
    ["Kinder Bueno", "Sprinkle", "Çikolata Topları"],
]

# Ingredients that should trend UP in recent days (last 3)
TRENDING_UP = ["Nutella", "Çilek", "Lotus Biscoff"]
# Ingredients that should be used heavily to create low stock
HEAVY_USE = ["Nutella", "Çilek", "Muz"]


def clear_transactional_data(db: Session):
    """
    Wipe demo transactional data.

    The inventory ledger is append-only in production: a trigger refuses UPDATE
    and DELETE, with no runtime bypass. Wiping it therefore needs the same
    ownership-gated escape hatch the test teardown uses — ALTER TABLE ... DISABLE
    TRIGGER requires table ownership and is not reachable from ordinary
    application DML, so this is a dev-tool affordance, not a production hole.
    """
    logger.info("Clearing existing transactional data...")
    db.query(OrderStatusEvent).delete()
    db.execute(text("ALTER TABLE ingredient_stock_movements DISABLE TRIGGER "
                    "trg_ingredient_stock_movements_immutable"))
    try:
        db.query(IngredientStockMovement).delete()
        db.query(OrderInventoryLine).delete()
        db.query(OrderItemIngredient).delete()
        db.query(OrderItem).delete()
        db.query(Order).delete()
    finally:
        db.execute(text("ALTER TABLE ingredient_stock_movements ENABLE TRIGGER "
                        "trg_ingredient_stock_movements_immutable"))
    db.commit()
    logger.info("Cleared.")


def reset_stock_to_initial(db: Session):
    """Reset stock to values that will show warnings after 14 days of orders."""
    stocks = db.query(IngredientStock).all()
    for s in stocks:
        ing = db.query(Ingredient).filter(Ingredient.id == s.ingredient_id).first()
        if not ing:
            continue
        # Heavy-use ingredients get less starting stock → will show warnings
        if ing.name in HEAVY_USE:
            if ing.unit == "g":
                s.on_hand_quantity = Decimal('800')  # Will drop to ~200 after 14 days
            elif ing.unit == "ml":
                s.on_hand_quantity = Decimal('400')
            else:
                s.on_hand_quantity = Decimal('30')
        else:
            if ing.unit == "g":
                s.on_hand_quantity = Decimal('2000')
            elif ing.unit == "ml":
                s.on_hand_quantity = Decimal('1000')
            else:
                s.on_hand_quantity = Decimal('100')
        s.reserved_quantity = Decimal('0')
    db.commit()
    logger.info("Stock levels reset.")


def generate_demo_orders(db: Session):
    store = db.query(Store).first()
    if not store:
        logger.error("No store found. Run seed.py first.")
        return

    tables = db.query(Table).filter(Table.store_id == store.id).all()
    product = db.query(Product).first()
    if not product:
        logger.error("No product found. Run seed.py first.")
        return

    all_ingredients = db.query(Ingredient).filter(Ingredient.is_active == True).all()
    ing_by_name = {i.name: i for i in all_ingredients}

    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=14)

    total_orders = 0
    total_stock_deductions = {}

    for day_offset in range(15):
        current_date = start_date + timedelta(days=day_offset)
        weekday = current_date.weekday()

        # Realistic daily volume
        is_weekend = weekday >= 4  # Fri-Sun
        is_recent = day_offset >= 11  # Last 3 days

        if is_weekend:
            num_orders = random.randint(12, 20)
        else:
            num_orders = random.randint(5, 10)

        # Recent days have slightly more demand
        if is_recent:
            num_orders = int(num_orders * 1.3)

        for _ in range(num_orders):
            # Order time: 11:00 — 22:00, peak at 14-16 and 19-21
            hour_weights = {11: 1, 12: 3, 13: 4, 14: 6, 15: 6, 16: 5,
                           17: 3, 18: 4, 19: 7, 20: 8, 21: 5}
            hours = list(hour_weights.keys())
            weights = list(hour_weights.values())
            hour = random.choices(hours, weights=weights, k=1)[0]
            minute = random.randint(0, 59)
            order_time = current_date.replace(
                hour=hour, minute=minute, second=random.randint(0, 59), microsecond=0
            )

            table = random.choice(tables)

            # Pick ingredients: either a popular combo or random selection
            if random.random() < 0.65:
                # Use a popular combo
                combo = random.choice(POPULAR_COMBOS)
                selected_names = list(combo)
                # Sometimes add 1-2 extra
                if random.random() < 0.3:
                    extras = random.sample(
                        [i.name for i in all_ingredients if i.name not in selected_names],
                        k=min(2, len(all_ingredients) - len(selected_names))
                    )
                    selected_names.extend(extras)
            else:
                # Random pick 2-5 ingredients
                count = random.randint(2, 5)
                selected_names = [i.name for i in random.sample(all_ingredients, k=min(count, len(all_ingredients)))]

            # Boost trending ingredients in recent days
            if is_recent:
                for trend_name in TRENDING_UP:
                    if trend_name not in selected_names and random.random() < 0.5:
                        selected_names.append(trend_name)

            # Resolve to actual ingredient objects
            selected_ings = [ing_by_name[n] for n in selected_names if n in ing_by_name]
            if not selected_ings:
                continue

            # Calculate total
            base_price = product.base_price
            ing_total = sum(i.price for i in selected_ings)
            order_total = base_price + ing_total

            # Create order
            order = Order(
                store_id=store.id,
                table_id=table.id,
                status="DELIVERED",
                total_amount=order_total,
            )
            order.created_at = order_time
            db.add(order)
            db.flush()

            # Status events with realistic timing
            prep_delay = timedelta(minutes=random.randint(1, 3))
            prep_time = timedelta(minutes=random.randint(3, 8))  # actual cooking
            serve_delay = timedelta(minutes=random.randint(1, 3))

            events = [
                OrderStatusEvent(order_id=order.id, status_to="NEW"),
                OrderStatusEvent(order_id=order.id, status_from="NEW", status_to="IN_PREP"),
                OrderStatusEvent(order_id=order.id, status_from="IN_PREP", status_to="READY"),
                OrderStatusEvent(order_id=order.id, status_from="READY", status_to="DELIVERED"),
            ]
            events[0].created_at = order_time
            events[1].created_at = order_time + prep_delay
            events[2].created_at = events[1].created_at + prep_time
            events[3].created_at = events[2].created_at + serve_delay
            db.add_all(events)

            # Order item
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=1,
                price=base_price,
            )
            db.add(item)
            db.flush()

            # Order item ingredients with consumption snapshot + the inventory
            # line. These demo orders are all DELIVERED, so they were reserved
            # and then physically consumed: reserved == consumed, nothing
            # outstanding.
            for ing in selected_ings:
                consumed_qty = float(ing.standard_quantity) if ing.standard_quantity else 0
                oi_ing = OrderItemIngredient(
                    order_item_id=item.id,
                    ingredient_id=ing.id,
                    quantity=1,
                    price_modifier=ing.price,
                    consumed_quantity=consumed_qty if consumed_qty > 0 else None,
                    consumed_unit=ing.unit if consumed_qty > 0 else None,
                )
                db.add(oi_ing)

                if consumed_qty > 0:
                    db.add(OrderInventoryLine(
                        order_id=order.id,
                        order_item_id=item.id,
                        ingredient_id=ing.id,
                        reserved_quantity=Decimal(str(consumed_qty)),
                        consumed_quantity=Decimal(str(consumed_qty)),
                        unit=ing.unit,
                    ))

                # Track stock consumed
                key = ing.id
                total_stock_deductions[key] = total_stock_deductions.get(key, 0) + consumed_qty

            total_orders += 1

    db.commit()

    # Apply stock deductions. Each ingredient gets the RESERVATION_CREATED /
    # CONSUMPTION pair the real lifecycle would have produced, so reserved nets
    # back to zero and the ledger's on-hand deltas still sum to the summary.
    for ing_id, total_consumed in total_stock_deductions.items():
        if total_consumed <= 0:
            continue
        stock = db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing_id).first()
        if not stock:
            continue

        qty = Decimal(str(total_consumed))
        stock.on_hand_quantity = max(
            Decimal('0'), Decimal(str(stock.on_hand_quantity)) - qty
        )

        db.add(IngredientStockMovement(
            ingredient_id=ing_id,
            movement_type="RESERVATION_CREATED",
            quantity=qty,
            quantity_delta_on_hand=Decimal('0'),
            quantity_delta_reserved=qty,
            unit=stock.unit,
            reason=f"Demo seed: reserved across {total_orders} orders",
        ))
        db.add(IngredientStockMovement(
            ingredient_id=ing_id,
            movement_type="CONSUMPTION",
            quantity=qty,
            quantity_delta_on_hand=-qty,
            quantity_delta_reserved=-qty,
            unit=stock.unit,
            reason=f"Demo seed: {total_consumed:.0f} {stock.unit} consumed across {total_orders} orders",
        ))

    db.commit()
    logger.info(f"Generated {total_orders} orders across 14 days.")

    # NOTE: reset_stock_levels() above writes on-hand directly, without a ledger
    # movement, so a demo database is intentionally NOT ledger-reconciled — the
    # opening balance was overwritten. scripts/reconcile_inventory.py will report
    # that drift, correctly. This seeder is a dev/demo tool only.
    logger.info("--- Stock status after demo seed ---")
    stocks = db.query(IngredientStock).join(Ingredient).all()
    for s in stocks:
        ing = db.query(Ingredient).filter(Ingredient.id == s.ingredient_id).first()
        level = "🔴 CRITICAL" if float(s.available_quantity) <= float(s.reorder_level or 0) else "✅ OK"
        logger.info(
            f"  {level} {ing.name}: on-hand {float(s.on_hand_quantity):.0f} "
            f"/ available {float(s.available_quantity):.0f} {s.unit}"
        )


def main():
    logger.info("=" * 50)
    logger.info("SweetOps Demo Seed v2 — Turkish Waffle Catalog")
    logger.info("=" * 50)
    try:
        db = SessionLocal()
        clear_transactional_data(db)
        reset_stock_to_initial(db)
        generate_demo_orders(db)
        logger.info("Demo seed complete! Dashboard should now show full data.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
