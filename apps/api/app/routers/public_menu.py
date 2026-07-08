from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.core import messages
from app.core.db import get_db
from app.schemas.qr import QrResolveRequest
from app.services.menu_service import get_menu
from app.services.conversion_engine import compute_upsell, validate_ingredient_selection
from app.services.operational_context_service import compute_operational_context, OperationalContext
from app.services.qr_token_service import resolve_token, QrTableUnavailable

router = APIRouter(prefix="/public/menu", tags=["Public Menu"])


@router.get("/")
def read_menu(
    db: Session = Depends(get_db),
):
    """
    Public menu with conversion signals (ungated).

    This route carries NO QR token — a bearer token must never appear in a URL
    (see `POST /public/menu/resolve`). The catalog is a single shared waffle
    menu in the current data model, so this endpoint exposes only non-sensitive
    menu content and no store/table context. The customer app uses the QR-gated
    `POST /public/menu/resolve` variant; other internal callers may use this
    ungated read.

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


@router.post("/resolve")
def read_menu_for_qr(
    body: QrResolveRequest,
    db: Session = Depends(get_db),
):
    """
    QR-gated menu. The opaque token is sent in the REQUEST BODY — never in the
    URL — because it is a long-lived bearer token attached to a physical table
    and a query-string token can leak through browser history, proxy/CDN access
    logs, referrer headers, screenshots and observability pipelines. (See
    `POST /public/qr-context/resolve` for the same body-transport rule.)

    The token is re-validated here server-side; an invalid/revoked token returns
    a Turkish error and NO menu, so a tampered or missing token can never load a
    menu. There is deliberately no numeric `store` parameter to manipulate. The
    catalog itself is a single shared waffle menu in the current data model, so
    no per-store filtering applies; access, not content, is what the token gates.

    Each ingredient includes additive fields:
      stock_status             — "in_stock" | "low_stock" | "out_of_stock"
      popular_badge            — true if top-20% by usage in last 7 days
      profitable_badge         — true if high-margin (or high-price proxy)
      recommended_with         — list of ingredient IDs that frequently appear together
      out_of_stock_alternative — nearest same-category in-stock ingredient (if OOS/low)
      ranking_score            — deterministic sort key (promoted > popular > margin > availability)

    Ingredients within each category are ordered by ranking_score DESC.
    """
    try:
        ctx = resolve_token(db, body.qr_token, touch=False)
    except QrTableUnavailable:
        raise HTTPException(status_code=409, detail=messages.QR_UNAVAILABLE)
    if ctx is None:
        raise HTTPException(status_code=404, detail=messages.QR_INVALID)
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
    try:
        ctx = compute_operational_context(db)
    except Exception:
        ctx = OperationalContext()
    return compute_upsell(db, ingredient_ids, max_suggestions=ctx.max_upsell_suggestions)


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
