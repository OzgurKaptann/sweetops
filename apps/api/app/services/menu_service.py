from sqlalchemy.orm import Session

from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock
from app.models.product import Product
from app.services.conversion_engine import enrich_menu, load_store_stocks
from app.services.operational_context_service import compute_operational_context


def get_menu(db: Session, store_id: int) -> dict:
    """
    Return the enriched menu for ONE STORE, with conversion signals.

    The catalog half of the menu — products, ingredients, prices, recipes — is
    global: every branch sells the same waffle. The ``stock_status`` half is
    physical, and therefore belongs to the branch the customer is sitting in.
    ``store_id`` comes from the QR token they scanned, so a table in Kadıköy is
    told what Kadıköy can actually make.

    An ingredient this branch does not stock has no row and reads as
    out_of_stock. There is no fallback to another branch's shelves.

    Operational context (from today's metrics) is applied to the ranking:
      - boost_combos mode: popular combo ingredients get a higher ranking_score
      - high_kitchen_load / sla_critical: no combo boost (reduce complexity)
      - normal: standard ranking, no adjustment

    The context block is included in the response so the customer UI can adapt
    (e.g. display a "Kitchen is busy" notice and reduce visible combos).
    """
    # Compute today's operational context — safe default if metrics unavailable
    try:
        ctx = compute_operational_context(db)
    except Exception:
        from app.services.operational_context_service import OperationalContext
        ctx = OperationalContext()

    products = db.query(Product).all()
    active_ingredients = (
        db.query(Ingredient).filter(Ingredient.is_active == True).all()
    )

    stocks: dict[int, IngredientStock] = load_store_stocks(
        db, store_id, [i.id for i in active_ingredients]
    )

    # Enriched + ranked ingredient dicts with operational-context-aware ranking
    enriched = enrich_menu(db, active_ingredients, stocks, combo_boost=ctx.combo_boost)

    # Group by category preserving the ranked order within each category
    category_order = ["Meyveler", "Kuruyemiş / Süslemeler", "Çikolatalar / Soslar"]

    categories_map: dict[str, list[dict]] = {cat: [] for cat in category_order}
    for ing_dict in enriched:
        cat = ing_dict["category"]
        if cat in categories_map:
            categories_map[cat].append(ing_dict)

    categorized = [
        {"name": cat, "ingredients": categories_map[cat]}
        for cat in category_order
        if categories_map.get(cat)
    ]

    return {
        # Echoed so a client can never be confused about whose shelves the
        # stock_status figures describe.
        "store_id":    store_id,
        "products":    products,
        "ingredients": enriched,
        "categories":  categorized,
        # Operational context exposed so the customer UI can adapt behaviour
        "operational_context": {
            "mode":                  ctx.mode,
            "max_upsell_suggestions": ctx.max_upsell_suggestions,
        },
    }
