from sqlalchemy.orm import Session

from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock
from app.models.product import Product
from app.services.conversion_engine import enrich_menu


def get_menu(db: Session) -> dict:
    products = db.query(Product).all()
    active_ingredients = (
        db.query(Ingredient).filter(Ingredient.is_active == True).all()
    )

    stocks: dict[int, IngredientStock] = {
        row.ingredient_id: row
        for row in db.query(IngredientStock).filter(
            IngredientStock.ingredient_id.in_([i.id for i in active_ingredients])
        ).all()
    }

    # Enriched + ranked ingredient dicts (sorted by ranking_score DESC)
    enriched = enrich_menu(db, active_ingredients, stocks)

    # Group by category preserving the ranked order within each category
    category_order = ["Meyveler", "Kuruyemiş / Süslemeler", "Çikolatalar / Soslar"]

    categories_map: dict[str, list[dict]] = {cat: [] for cat in category_order}
    for ing_dict in enriched:
        cat = ing_dict["category"]
        if cat in categories_map:
            categories_map[cat].append(ing_dict)
        # ingredients in unknown categories are still in the flat list but omitted from grouped view

    categorized = [
        {"name": cat, "ingredients": categories_map[cat]}
        for cat in category_order
        if categories_map.get(cat)
    ]

    return {
        "products":    products,
        "ingredients": enriched,   # enriched flat list (replaces raw ORM list — same shape + new fields)
        "categories":  categorized,
    }
