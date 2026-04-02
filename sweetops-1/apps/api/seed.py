from sqlalchemy.orm import Session
from app.core.db import SessionLocal
from app.models.store import Store
from app.models.table import Table
from app.models.product import Product
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock
from decimal import Decimal

def seed_db():
    db: Session = SessionLocal()
    
    if db.query(Store).count() > 0:
        db.close()
        print("Database already seeded.")
        return

    # --- Store ---
    store = Store(name="SweetOps Waffle", location="Kadıköy, İstanbul")
    db.add(store)
    db.commit()
    db.refresh(store)
    
    # --- Tables (6 tables) ---
    for i in range(1, 7):
        t = Table(store_id=store.id, table_number=str(i), qr_code=f"sweetops-masa-{i}")
        db.add(t)
    db.commit()

    # --- Product: Waffle base ---
    waffle = Product(name="Waffle", category="Waffle", base_price=Decimal('45.00'))
    db.add(waffle)
    db.commit()

    # --- Ingredients: Turkish waffle catalog ---
    ingredients_data = [
        # Meyveler
        {"name": "Muz",       "category": "Meyveler", "price": Decimal('8.00'),  "unit": "g",     "standard_quantity": Decimal('50.00'), "shelf_life_days": 5},
        {"name": "Çilek",     "category": "Meyveler", "price": Decimal('10.00'), "unit": "g",     "standard_quantity": Decimal('40.00'), "shelf_life_days": 4},

        # Kuruyemiş / Süslemeler
        {"name": "Fransız Bisküvisi Çilekli",       "category": "Kuruyemiş / Süslemeler", "price": Decimal('10.00'), "unit": "piece", "standard_quantity": Decimal('3.00')},
        {"name": "Fransız Bisküvisi Yaban Mersinli", "category": "Kuruyemiş / Süslemeler", "price": Decimal('10.00'), "unit": "piece", "standard_quantity": Decimal('3.00')},
        {"name": "Fransız Bisküvisi Limon",          "category": "Kuruyemiş / Süslemeler", "price": Decimal('10.00'), "unit": "piece", "standard_quantity": Decimal('3.00')},
        {"name": "Fransız Bisküvisi Vanilya",        "category": "Kuruyemiş / Süslemeler", "price": Decimal('10.00'), "unit": "piece", "standard_quantity": Decimal('3.00')},
        {"name": "Lotus Biscoff",                    "category": "Kuruyemiş / Süslemeler", "price": Decimal('12.00'), "unit": "piece", "standard_quantity": Decimal('2.00')},
        {"name": "Oreo",                             "category": "Kuruyemiş / Süslemeler", "price": Decimal('10.00'), "unit": "piece", "standard_quantity": Decimal('2.00')},
        {"name": "Çikolata Topları",                 "category": "Kuruyemiş / Süslemeler", "price": Decimal('8.00'),  "unit": "g",     "standard_quantity": Decimal('15.00')},
        {"name": "Karışık Çikolata Topları",         "category": "Kuruyemiş / Süslemeler", "price": Decimal('10.00'), "unit": "g",     "standard_quantity": Decimal('15.00')},
        {"name": "Fındık",                           "category": "Kuruyemiş / Süslemeler", "price": Decimal('12.00'), "unit": "g",     "standard_quantity": Decimal('15.00')},
        {"name": "Fıstık",                           "category": "Kuruyemiş / Süslemeler", "price": Decimal('15.00'), "unit": "g",     "standard_quantity": Decimal('12.00')},
        {"name": "Sprinkle",                         "category": "Kuruyemiş / Süslemeler", "price": Decimal('5.00'),  "unit": "g",     "standard_quantity": Decimal('8.00')},
        {"name": "Çakıl Taşı",                      "category": "Kuruyemiş / Süslemeler", "price": Decimal('8.00'),  "unit": "g",     "standard_quantity": Decimal('10.00')},
        {"name": "Bonibon",                          "category": "Kuruyemiş / Süslemeler", "price": Decimal('8.00'),  "unit": "g",     "standard_quantity": Decimal('12.00')},

        # Çikolatalar / Soslar
        {"name": "Kinder Bueno",          "category": "Çikolatalar / Soslar", "price": Decimal('15.00'), "unit": "piece", "standard_quantity": Decimal('1.00')},
        {"name": "Nutella",               "category": "Çikolatalar / Soslar", "price": Decimal('10.00'), "unit": "g",     "standard_quantity": Decimal('30.00'), "allows_portion_choice": True},
        {"name": "Bitter Çikolata",       "category": "Çikolatalar / Soslar", "price": Decimal('10.00'), "unit": "ml",    "standard_quantity": Decimal('25.00'), "allows_portion_choice": True},
        {"name": "Frambuazlı Çikolata",   "category": "Çikolatalar / Soslar", "price": Decimal('12.00'), "unit": "ml",    "standard_quantity": Decimal('25.00'), "allows_portion_choice": True},
        {"name": "Karamel",               "category": "Çikolatalar / Soslar", "price": Decimal('8.00'),  "unit": "ml",    "standard_quantity": Decimal('20.00'), "allows_portion_choice": True},
    ]

    for ing_data in ingredients_data:
        ing = Ingredient(
            name=ing_data["name"],
            category=ing_data["category"],
            price=ing_data["price"],
            unit=ing_data.get("unit", "g"),
            standard_quantity=ing_data.get("standard_quantity", Decimal('0.00')),
            shelf_life_days=ing_data.get("shelf_life_days"),
            allows_portion_choice=ing_data.get("allows_portion_choice", False),
            is_active=True,
        )
        db.add(ing)
    db.commit()

    # --- Initial stock for all ingredients ---
    all_ingredients = db.query(Ingredient).all()
    for ing in all_ingredients:
        # Generous initial stock for demo
        if ing.unit == "g":
            initial_qty = Decimal('2000.00')
        elif ing.unit == "ml":
            initial_qty = Decimal('1000.00')
        else:  # piece
            initial_qty = Decimal('100.00')

        stock = IngredientStock(
            ingredient_id=ing.id,
            stock_quantity=initial_qty,
            unit=ing.unit,
            reorder_level=initial_qty * Decimal('0.15')  # 15% of initial as reorder threshold
        )
        db.add(stock)
    db.commit()

    print(f"Database seeded: 1 store, 6 tables, 1 product, {len(ingredients_data)} ingredients with stock.")
    db.close()

if __name__ == "__main__":
    seed_db()
