from sqlalchemy.orm import Session

from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock
from app.models.product import Product
from app.models.store_product import StoreProduct
from app.services.conversion_engine import enrich_menu, load_store_stocks
from app.services.operational_context_service import compute_operational_context


def list_menu_products(db: Session, store_id: int) -> list[Product]:
    """
    The products THIS BRANCH offers to guests, in menu order.

    Membership is a join, not a filter on a name or a category: a product
    reaches this list only through a ``store_products`` row that says this
    branch publishes it. A row that was never published — a seed left over, an
    import, the ``TestWaffle_<hex>`` a killed test run wrote — has no such row
    and is therefore not merely hidden, it is unreachable from every customer
    surface.

    Two switches can take a published product back off the menu:
      products.is_active == False        retired chain-wide
      store_products.is_available == False   this branch, today only

    Deterministic order: sort_order, then name, then id — a menu that reshuffles
    between two loads makes a guest lose the item they were about to tap.
    """
    return (
        db.query(Product)
        .join(StoreProduct, StoreProduct.product_id == Product.id)
        .filter(
            StoreProduct.store_id == store_id,
            StoreProduct.is_available == True,   # noqa: E712
            Product.is_active == True,           # noqa: E712
        )
        .order_by(StoreProduct.sort_order, Product.name, Product.id)
        .all()
    )


def serialize_product(product: Product) -> dict:
    """Wire shape of one menu product. Prices are strings — see enrich_menu."""
    return {
        "id":         product.id,
        "name":       product.name,
        "category":   product.category,
        "base_price": str(product.base_price),
    }


def get_menu(db: Session, store_id: int) -> dict:
    """
    Return the enriched menu for ONE STORE, with conversion signals.

    Nothing in this response is global. ``products`` is what this branch has
    published (see ``list_menu_products``); ``stock_status`` is what this
    branch physically holds. ``store_id`` comes from the QR token the customer
    scanned, so a table in Kadıköy is told what Kadıköy actually sells and can
    actually make.

    A branch that has published nothing gets an EMPTY product list, and the
    customer app shows a Turkish empty state. That is the intended failure
    mode: showing a guest the chain's whole ``products`` table — which is what
    this function used to do — is how test debris ends up on a phone.

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

    products = list_menu_products(db, store_id)
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
        # stock_status figures describe, or whose menu it is looking at.
        "store_id":    store_id,
        "products":    [serialize_product(p) for p in products],
        "ingredients": enriched,
        "categories":  categorized,
        # Operational context exposed so the customer UI can adapt behaviour
        "operational_context": {
            "mode":                  ctx.mode,
            "max_upsell_suggestions": ctx.max_upsell_suggestions,
        },
    }
