"""
Public (customer-facing) menu.

Nothing served here is global. ``products`` is what the branch has published
(``store_products`` — see docs/CUSTOMER_MENU_SCOPING.md); ``stock_status`` is
physical stock on that branch's shelves. Both need to know which branch they
are talking about, so every route here does.

The ingredient CATALOG (names, units, recipe quantities, prices) is still
chain-wide — every branch builds the same waffle from the same definitions —
but which of those a guest may pick is filtered by that branch's own stock.

There are two ways to know the branch, and only two:

  QR-GATED   POST /resolve, POST /upsell, POST /validate (with qr_token)
             The scanned token resolves server-side to a store. This is what the
             customer app uses, and it is fully store-scoped.

  UNGATED    GET /, GET /upsell, POST /validate (without qr_token)
             No token, no session, no store. These fall back to
             ``resolve_ungated_menu_store_id`` — which returns the single
             operational store, or refuses with a Turkish 409 when there is more
             than one. They cannot do better: with two branches open, "is
             pistachio in stock?" has two different true answers and no way to
             tell which one was asked. Refusing beats guessing.

See docs/STORE_SCOPED_INVENTORY.md § "Remaining limitation".
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.core import messages
from app.core.db import get_db
from app.schemas.qr import QrResolveRequest
from app.services.menu_service import get_menu
from app.services.conversion_engine import compute_upsell, validate_ingredient_selection
from app.services.inventory_guard import resolve_ungated_menu_store_id
from app.services.operational_context_service import compute_operational_context, OperationalContext
from app.services.qr_token_service import resolve_token, QrTableUnavailable

router = APIRouter(prefix="/public/menu", tags=["Public Menu"])


def _store_from_token(db: Session, qr_token: str) -> int:
    """Resolve a scanned QR token to its store, or raise the standard errors."""
    try:
        ctx = resolve_token(db, qr_token, touch=False)
    except QrTableUnavailable:
        raise HTTPException(status_code=409, detail=messages.QR_UNAVAILABLE)
    if ctx is None:
        raise HTTPException(status_code=404, detail=messages.QR_INVALID)
    return ctx.store_id


@router.get("/")
def read_menu(
    db: Session = Depends(get_db),
):
    """
    Public menu with conversion signals (ungated).

    This route carries NO QR token — a bearer token must never appear in a URL
    (see `POST /public/menu/resolve`) — and therefore no store context. Its
    ``products`` and ``stock_status`` fields both belong to one branch, so it
    resolves the single operational store and fails closed with a Turkish 409
    once a second branch is staffed.
    The customer app uses the QR-gated `POST /public/menu/resolve` variant, which
    is properly store-scoped; this ungated read remains for internal callers of
    a single-branch installation.

    Each ingredient includes additive fields:
      stock_status             — "in_stock" | "low_stock" | "out_of_stock" (this store)
      popular_badge            — true if top-20% by usage in last 7 days
      profitable_badge         — true if high-margin (or high-price proxy)
      recommended_with         — list of ingredient IDs that frequently appear together
      out_of_stock_alternative — nearest same-category in-stock ingredient (if OOS/low)
      ranking_score            — deterministic sort key (promoted > popular > margin > availability)

    Ingredients within each category are ordered by ranking_score DESC.
    """
    return get_menu(db, resolve_ungated_menu_store_id(db))


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
    menu. There is deliberately no numeric `store` parameter to manipulate — the
    store is DERIVED from the token, which is exactly what makes it trustworthy.

    `products` is this branch's published menu — not the chain's `products`
    table. A product nobody published here is absent, which is what keeps a
    seed leftover or an interrupted test run's row off a guest's phone. An
    unprovisioned branch returns an EMPTY product list and the customer app
    shows a Turkish empty state; it never falls back to "everything".

    `stock_status` is likewise this table's own branch: the same ingredient can
    read in_stock here and out_of_stock at the branch across town, and the
    customer is told the truth about the kitchen that will actually cook their
    waffle.

    Each ingredient includes additive fields:
      stock_status             — "in_stock" | "low_stock" | "out_of_stock" (this store)
      popular_badge            — true if top-20% by usage in last 7 days
      profitable_badge         — true if high-margin (or high-price proxy)
      recommended_with         — list of ingredient IDs that frequently appear together
      out_of_stock_alternative — nearest same-category in-stock ingredient (if OOS/low)
      ranking_score            — deterministic sort key (promoted > popular > margin > availability)

    Ingredients within each category are ordered by ranking_score DESC.
    """
    return get_menu(db, _store_from_token(db, body.qr_token))


@router.post("/upsell")
def upsell_suggestions_for_qr(
    body: dict,
    db: Session = Depends(get_db),
):
    """
    QR-gated upsell — the store-scoped variant the customer app uses.

    Request body: { "qr_token": "...", "ingredient_ids": [1, 3] }

    Same ranking as the ungated GET below, but the in_stock filter runs against
    the scanning table's own branch. Suggesting an ingredient this branch has run
    out of would be worse than suggesting nothing: the customer adds it and the
    order is then rejected at checkout.

    The token travels in the BODY, never the query string — see `/resolve`.
    """
    qr_token = body.get("qr_token")
    if not isinstance(qr_token, str) or not qr_token:
        raise HTTPException(status_code=404, detail=messages.QR_INVALID)

    ingredient_ids = body.get("ingredient_ids", [])
    if not isinstance(ingredient_ids, list):
        ingredient_ids = []

    store_id = _store_from_token(db, qr_token)
    try:
        ctx = compute_operational_context(db)
    except Exception:
        ctx = OperationalContext()
    return compute_upsell(
        db, store_id, ingredient_ids, max_suggestions=ctx.max_upsell_suggestions
    )


@router.get("/upsell")
def upsell_suggestions(
    ingredient_ids: List[int] = Query(default=[], alias="ingredient_ids"),
    db: Session = Depends(get_db),
):
    """
    Ungated upsell. Carries no token, so it has no store context: it resolves the
    single operational store and fails closed with a Turkish 409 once a second
    branch is staffed. Multi-branch clients must use `POST /public/menu/upsell`.

    Suggestions are ranked by:
      combo_frequency × (1 + price_rank)

    Filters applied:
      - not already selected
      - is_active
      - in_stock (in the resolved store)

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
    store_id = resolve_ungated_menu_store_id(db)
    try:
        ctx = compute_operational_context(db)
    except Exception:
        ctx = OperationalContext()
    return compute_upsell(
        db, store_id, ingredient_ids, max_suggestions=ctx.max_upsell_suggestions
    )


@router.post("/validate")
def validate_selection(
    body: dict,
    db: Session = Depends(get_db),
):
    """
    Validate a customer's ingredient selection before ordering, against the
    stock of the branch they are ordering from.

    Request body: { "ingredient_ids": [1, 2, 3], "qr_token": "..." }

    ``qr_token`` is optional but strongly preferred: with it the check runs
    against the scanning table's store. Without it there is no store context, so
    the single operational store is resolved and a multi-branch installation
    fails closed with a Turkish 409.

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

    qr_token = body.get("qr_token")
    store_id = (
        _store_from_token(db, qr_token)
        if isinstance(qr_token, str) and qr_token
        else resolve_ungated_menu_store_id(db)
    )
    return validate_ingredient_selection(db, store_id, ingredient_ids)
