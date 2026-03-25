from sqlalchemy.orm import Session
from app.models.product import Product
from app.models.ingredient import Ingredient

def get_menu(db: Session):
    products = db.query(Product).all()
    ingredients = db.query(Ingredient).filter(Ingredient.is_active == True).all()
    
    # Group ingredients by category for the frontend
    categories = {}
    for ing in ingredients:
        cat = ing.category
        if cat not in categories:
            categories[cat] = []
        categories[cat].append({
            "id": ing.id,
            "name": ing.name,
            "category": ing.category,
            "price": str(ing.price),
            "unit": ing.unit,
            "standard_quantity": str(ing.standard_quantity),
            "allows_portion_choice": ing.allows_portion_choice,
        })
    
    # Define display order for categories
    category_order = ["Meyveler", "Kuruyemiş / Süslemeler", "Çikolatalar / Soslar"]
    
    categorized = []
    for cat_name in category_order:
        if cat_name in categories:
            categorized.append({
                "name": cat_name,
                "ingredients": categories[cat_name]
            })
    
    return {
        "products": products,
        "ingredients": ingredients,  # flat list for backward compat
        "categories": categorized,   # new: grouped by category
    }
