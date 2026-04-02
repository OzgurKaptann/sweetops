from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import List

from app.core.db import get_db
from app.services.menu_service import get_menu
from app.services.conversion_engine import compute_upsell, validate_ingredient_selection

router = APIRouter(prefix="/public/menu", tags=["Public Menu"])


@router.get("/")
def read_menu(db: Session = Depends(get_db)):
    """
    Public menu with conversion signals.

    Each ingredient includes additive fields:
      stock_status             — "in_stock" | "low_stock" | "out_of_stock"
      popular_badge            — true if top-20% by usage in last 7 days
      profitable_badge         — true if high-margin (or high-price proxy)
      recommended_with         — list of ingredient IDs that frequently appear together
      out_of_stock_alternative — nearest same-category in-stock ingredient (if OOS/low)
      ranking_score            — deterministic sort key (promoted > popular > margin > availability)

    Ingredients within each category are ordered by ranking_score DESC.
    """
    return get_menu(db)


@router.get("/upsell")
def upsell_suggestions(
    ingredient_ids: List[int] = Query(default=[], alias="ingredient_ids"),
    db: Session = Depends(get_db),
):
    """
    Given the customer's currently selected ingredient IDs, return up to 3
    additional ingredients worth adding to the order.

    Suggestions are ranked by:
      combo_frequency × (1 + price_rank)

    Filters applied:
      - not already selected
      - is_active
      - in_stock

    Usage: GET /public/menu/upsell?ingredient_ids=1&ingredient_ids=3

    Example response:
    {
      "suggestions": [
        {
          "ingredient_id": 7,
          "ingredient_name": "Lotus Biscoff",
          "category": "Kuruyemiş / Süslemeler",
          "price": "12.00",
          "reason": "popular_combo",
          "combo_count": 4,
          "stock_status": "in_stock"
        }
      ],
      "based_on_ingredient_ids": [1, 3]
    }
    """
    return compute_upsell(db, ingredient_ids)


@router.post("/validate")
def validate_selection(
    body: dict,
    db: Session = Depends(get_db),
):
    """
    Validate a customer's ingredient selection before ordering.

    Request body: { "ingredient_ids": [1, 2, 3] }

    Removes:
      - unknown IDs (reason: "not_found")
      - out-of-stock IDs (reason: "out_of_stock") + suggests nearest alternative

    Returns:
      valid_ids       — IDs safe to use in an order
      removed         — list of removed items with reason and optional alternative
      price_delta     — float (negative = cost removed by OOS items)
      price_breakdown — {str(ingredient_id): price} for valid items

    Example response:
    {
      "valid_ids": [1, 3],
      "removed": [
        {
          "ingredient_id": 2,
          "ingredient_name": "Çilek",
          "reason": "out_of_stock",
          "alternative": {
            "ingredient_id": 1,
            "ingredient_name": "Muz",
            "category": "Meyveler",
            "price": "8.00"
          }
        }
      ],
      "price_delta": -10.0,
      "price_breakdown": {"1": 8.0, "3": 10.0}
    }
    """
    ingredient_ids = body.get("ingredient_ids", [])
    if not isinstance(ingredient_ids, list):
        ingredient_ids = []
    return validate_ingredient_selection(db, ingredient_ids)
