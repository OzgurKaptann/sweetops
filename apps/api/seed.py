from sqlalchemy.orm import Session
from app.core.db import SessionLocal
from app.models.store import Store
from app.models.table import Table
from app.models.product import Product
from app.models.ingredient import Ingredient
from decimal import Decimal

def seed_db():
    db: Session = SessionLocal()
    
    if db.query(Store).count() > 0:
        db.close()
        print("Database already seeded.")
        return

    # Seed Store and Table
    store1 = Store(name="SweetOps Central Waffle", location="Downtown")
    db.add(store1)
    db.commit()
    db.refresh(store1)
    
    table1 = Table(store_id=store1.id, table_number="T1", qr_code="QR-1234")
    db.add(table1)
    db.commit()

    # Seed Products
    p1 = Product(name="Classic Belgian Waffle", category="Waffle", base_price=Decimal('5.50'))
    p2 = Product(name="Choco Explosion", category="Waffle", base_price=Decimal('7.00'))
    db.add_all([p1, p2])
    db.commit()
    
    # Seed Ingredients
    i1 = Ingredient(name="Nutella", category="Base", price=Decimal('1.50'))
    i2 = Ingredient(name="Strawberries", category="Fruit", price=Decimal('2.00'))
    i3 = Ingredient(name="Banana", category="Fruit", price=Decimal('1.00'))
    i4 = Ingredient(name="Oreo Crumbles", category="Topping", price=Decimal('1.20'))
    db.add_all([i1, i2, i3, i4])
    db.commit()

    print("Database seeded with sample products and ingredients.")
    db.close()

if __name__ == "__main__":
    seed_db()
