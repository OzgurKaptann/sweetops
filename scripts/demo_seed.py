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


# Reorder thresholds for stores this script creates rows for (seed.py sets its
# own for store 1). The heavy-use ending targets below sit under these, so the
# demo dashboard always has genuine CRITICAL rows to show.
_REORDER_LEVEL = {"g": Decimal('300'), "ml": Decimal('150'), "piece": Decimal('15')}


def ensure_stock_rows_for_all_stores(db: Session):
    """
    Give EVERY store its own explicit stock row for every active ingredient.

    Stock is physical and per-branch: a store with no rows has no stock, and
    nothing in the system will quietly lend it another branch's. So a demo with
    two branches needs two sets of rows, created deliberately.

    This is synthetic data, and cloning quantities across stores is fine HERE
    precisely because it is fake. The production migration does the opposite —
    it refuses to duplicate real stock into a second store, because that would
    fabricate inventory that does not exist on any shelf.
    """
    stores = db.query(Store).all()
    ingredients = db.query(Ingredient).filter(Ingredient.is_active == True).all()

    existing = {
        (s.store_id, s.ingredient_id) for s in db.query(IngredientStock).all()
    }
    created = 0
    for store in stores:
        for ing in ingredients:
            if (store.id, ing.id) in existing:
                continue
            db.add(IngredientStock(
                store_id=store.id,
                ingredient_id=ing.id,
                on_hand_quantity=Decimal('0'),
                reserved_quantity=Decimal('0'),
                unit=ing.unit,
                reorder_level=_REORDER_LEVEL.get(ing.unit, Decimal('300')),
            ))
            created += 1
    db.commit()
    logger.info(
        f"Stock rows: {len(stores)} store(s) × {len(ingredients)} ingredient(s); "
        f"{created} row(s) created."
    )


# Where each (store, ingredient) should END UP after 14 days of demo orders.
# Heavy-use ingredients land below their reorder level so the owner dashboard has
# real CRITICAL rows to show; everything else lands comfortably above it.
_ENDING_TARGET = {
    True:  {"g": Decimal('150'), "ml": Decimal('80'),  "piece": Decimal('8')},   # heavy use
    False: {"g": Decimal('1500'), "ml": Decimal('750'), "piece": Decimal('60')},
}


def _ending_target(ing: Ingredient) -> Decimal:
    heavy = ing.name in HEAVY_USE
    return _ENDING_TARGET[heavy].get(ing.unit, _ENDING_TARGET[heavy]["g"])


def reset_stock_to_zero(db: Session):
    """
    Zero every store's stock so the demo can rebuild it from an honest ledger.

    The opening balance is NOT written here, because it cannot be known yet: it
    is derived, in ``_apply_demo_stock_deductions``, from what the demo orders
    actually consume (opening = consumed + ending target). Writing a fixed
    opening up front is what used to leave the demo database permanently
    unreconciled — on-hand was set directly, with no ledger row to justify it,
    and heavy-use ingredients then consumed more than they ever had, clamping
    on-hand at zero while the ledger sailed off into negative numbers.
    """
    ensure_stock_rows_for_all_stores(db)
    for s in db.query(IngredientStock).all():
        s.on_hand_quantity = Decimal('0')
        s.reserved_quantity = Decimal('0')
    db.commit()
    logger.info("Stock levels zeroed; opening balances will be written from the ledger.")


def generate_demo_orders(db: Session):
    """
    Generate 14 days of demo orders for EVERY store.

    Each store's orders draw down only that store's stock: the deduction map is
    keyed by (store_id, ingredient_id), and every inventory line and ledger row
    is stamped with the order's own store. A demo database therefore exhibits
    the property the whole branch is about — two branches, two independent sets
    of shelves — rather than quietly reproducing the old global behaviour.
    """
    stores = db.query(Store).all()
    if not stores:
        logger.error("No store found. Run seed.py first.")
        return

    product = db.query(Product).first()
    if not product:
        logger.error("No product found. Run seed.py first.")
        return

    all_ingredients = db.query(Ingredient).filter(Ingredient.is_active == True).all()
    ing_by_name = {i.name: i for i in all_ingredients}

    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=14)

    total_orders = 0
    # (store_id, ingredient_id) → quantity. The store in the key is what keeps
    # one branch's consumption out of another branch's summary.
    total_stock_deductions: dict[tuple[int, int], float] = {}

    for store in stores:
        tables = db.query(Table).filter(Table.store_id == store.id).all()
        if not tables:
            logger.warning("Store %s has no tables — skipping.", store.id)
            continue
        total_orders += _generate_orders_for_store(
            db, store, tables, product, all_ingredients, ing_by_name,
            start_date, total_stock_deductions,
        )

    db.commit()
    _apply_demo_stock_deductions(db, total_stock_deductions, total_orders)

    logger.info(f"Generated {total_orders} orders across 14 days, {len(stores)} store(s).")
    _log_stock_status(db)


def _generate_orders_for_store(
    db: Session,
    store,
    tables,
    product,
    all_ingredients,
    ing_by_name,
    start_date,
    total_stock_deductions: dict[tuple[int, int], float],
) -> int:
    total_orders = 0

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
                        store_id=order.store_id,
                        order_id=order.id,
                        order_item_id=item.id,
                        ingredient_id=ing.id,
                        reserved_quantity=Decimal(str(consumed_qty)),
                        consumed_quantity=Decimal(str(consumed_qty)),
                        unit=ing.unit,
                    ))

                # Track stock consumed — per STORE and ingredient.
                key = (order.store_id, ing.id)
                total_stock_deductions[key] = total_stock_deductions.get(key, 0) + consumed_qty

            total_orders += 1

    return total_orders


def _apply_demo_stock_deductions(
    db: Session,
    total_stock_deductions: dict[tuple[int, int], float],
    total_orders: int,
):
    """
    Write each store's opening balance and demo consumption as a real ledger.

    The opening balance is DERIVED, not assumed:

        opening = what the demo orders consumed + where we want the shelf to end

    which is what makes the demo database reconcile. Every (store, ingredient)
    then gets the PURCHASE_RECEIPT → RESERVATION_CREATED → CONSUMPTION sequence
    the real lifecycle would have produced, so reserved nets back to zero and the
    ledger's on-hand deltas sum EXACTLY to that store's summary — no clamping, no
    negative ledger, no permanent phantom drift for the reconciler to report.

    Each store's opening balance is its own. Nothing is shared or borrowed.
    """
    ingredients = {i.id: i for i in db.query(Ingredient).all()}

    for stock in db.query(IngredientStock).all():
        ing = ingredients.get(stock.ingredient_id)
        if ing is None:
            continue

        consumed = Decimal(str(
            total_stock_deductions.get((stock.store_id, stock.ingredient_id), 0)
        ))
        ending = _ending_target(ing)
        opening = consumed + ending

        # 1. Opening balance — the goods this branch started the fortnight with.
        db.add(IngredientStockMovement(
            store_id=stock.store_id,
            ingredient_id=stock.ingredient_id,
            movement_type="PURCHASE_RECEIPT",
            quantity=opening,
            quantity_delta_on_hand=opening,
            quantity_delta_reserved=Decimal('0'),
            unit=stock.unit,
            reason="Demo seed: opening balance",
            legacy_backfill=True,      # synthetic: no real actor to attribute it to
        ))

        # 2. The fortnight's trade: reserved, then physically consumed.
        if consumed > 0:
            db.add(IngredientStockMovement(
                store_id=stock.store_id,
                ingredient_id=stock.ingredient_id,
                movement_type="RESERVATION_CREATED",
                quantity=consumed,
                quantity_delta_on_hand=Decimal('0'),
                quantity_delta_reserved=consumed,
                unit=stock.unit,
                reason=f"Demo seed: reserved across {total_orders} orders",
            ))
            db.add(IngredientStockMovement(
                store_id=stock.store_id,
                ingredient_id=stock.ingredient_id,
                movement_type="CONSUMPTION",
                quantity=consumed,
                quantity_delta_on_hand=-consumed,
                quantity_delta_reserved=-consumed,
                unit=stock.unit,
                reason=f"Demo seed: {consumed:.0f} {stock.unit} consumed across "
                       f"{total_orders} orders",
            ))

        # 3. The summary the ledger above adds up to, by construction.
        stock.on_hand_quantity = ending
        stock.reserved_quantity = Decimal('0')

    db.commit()


def _log_stock_status(db: Session):
    # NOTE: reset_stock_to_initial() writes on-hand directly, without a ledger
    # movement, so a demo database is intentionally NOT ledger-reconciled — the
    # opening balance was overwritten. scripts/reconcile_inventory.py will report
    # that drift, per store, correctly. This seeder is a dev/demo tool only.
    logger.info("--- Stock status after demo seed (per store) ---")
    rows = (
        db.query(IngredientStock, Ingredient, Store)
        .join(Ingredient, Ingredient.id == IngredientStock.ingredient_id)
        .join(Store, Store.id == IngredientStock.store_id)
        .order_by(IngredientStock.store_id, Ingredient.name)
        .all()
    )
    for s, ing, store in rows:
        level = (
            "🔴 CRITICAL"
            if float(s.available_quantity) <= float(s.reorder_level or 0)
            else "✅ OK"
        )
        logger.info(
            f"  [store {store.id} {store.name}] {level} {ing.name}: "
            f"on-hand {float(s.on_hand_quantity):.0f} "
            f"/ available {float(s.available_quantity):.0f} {s.unit}"
        )


def main():
    logger.info("=" * 50)
    logger.info("SweetOps Demo Seed v2 — Turkish Waffle Catalog")
    logger.info("=" * 50)
    try:
        db = SessionLocal()
        clear_transactional_data(db)
        reset_stock_to_zero(db)
        generate_demo_orders(db)
        logger.info("Demo seed complete! Dashboard should now show full data.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
