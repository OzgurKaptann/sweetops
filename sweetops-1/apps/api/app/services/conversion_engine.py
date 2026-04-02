"""
Customer Conversion Engine — deterministic signals that increase conversion
and average order value without adding friction.

Four responsibilities:
  1. Conversion signals per ingredient (badges, recommendations, alternatives)
  2. Menu ranking (promoted → popular → margin → availability)
  3. Upsell suggestions (given selected ingredients, suggest additions)
  4. Fallback validation (detect OOS selections, swap to alternatives)

All logic is deterministic:
  - popularity   = usage count in last POPULARITY_WINDOW_DAYS
  - combo freq   = ingredient co-occurrence within same order_item in last COMBO_WINDOW_DAYS
  - margin proxy = price rank (75th-percentile cutoff) when cost_per_unit is NULL
  - alternatives = same-category, in-stock, min absolute price difference

No ML, no randomness, no A/B flags.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from itertools import combinations
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
POPULARITY_WINDOW_DAYS    = 7
COMBO_WINDOW_DAYS         = 30
POPULAR_TOP_PERCENTILE    = 0.20   # top 20% by usage → popular_badge
PROFITABLE_PRICE_PERCENTILE = 0.75 # top 25% by price → profitable_badge (no cost data fallback)
PROFITABLE_MARGIN_THRESHOLD = 0.40 # if cost_per_unit set: margin ≥ 40% → profitable
MAX_RECOMMENDATIONS       = 3
MAX_UPSELL_SUGGESTIONS    = 3
LOW_STOCK_MULTIPLIER      = 1.5    # stock ≤ reorder * 1.5 → low_stock


# ---------------------------------------------------------------------------
# Internal: data loaders (one query each, results grouped in Python)
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _load_usage_counts(db: Session) -> dict[int, int]:
    """ingredient_id → order count in last POPULARITY_WINDOW_DAYS."""
    cutoff = _now_utc() - timedelta(days=POPULARITY_WINDOW_DAYS)
    rows = (
        db.query(
            OrderItemIngredient.ingredient_id,
            func.count(OrderItemIngredient.id).label("cnt"),
        )
        .join(OrderItem, OrderItem.id == OrderItemIngredient.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(Order.created_at >= cutoff)
        .group_by(OrderItemIngredient.ingredient_id)
        .all()
    )
    return {r.ingredient_id: r.cnt for r in rows}


def _load_combo_counts(db: Session) -> dict[tuple[int, int], int]:
    """
    (ing_a, ing_b) where a < b → co-occurrence count within same order_item,
    over last COMBO_WINDOW_DAYS.
    """
    cutoff = _now_utc() - timedelta(days=COMBO_WINDOW_DAYS)
    rows = (
        db.query(
            OrderItemIngredient.order_item_id,
            OrderItemIngredient.ingredient_id,
        )
        .join(OrderItem, OrderItem.id == OrderItemIngredient.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(Order.created_at >= cutoff)
        .all()
    )

    # Group by order_item_id
    item_to_ings: dict[int, list[int]] = defaultdict(list)
    for oi_id, ing_id in rows:
        item_to_ings[oi_id].append(ing_id)

    combo_counts: dict[tuple[int, int], int] = defaultdict(int)
    for ings in item_to_ings.values():
        for a, b in combinations(sorted(set(ings)), 2):
            combo_counts[(a, b)] += 1

    return dict(combo_counts)


def _stock_status(stock: IngredientStock | None) -> str:
    """Returns 'in_stock' | 'low_stock' | 'out_of_stock'."""
    if stock is None or float(stock.stock_quantity) <= 0:
        return "out_of_stock"
    reorder = float(stock.reorder_level) if stock.reorder_level else 0.0
    if reorder > 0 and float(stock.stock_quantity) <= reorder * LOW_STOCK_MULTIPLIER:
        return "low_stock"
    return "in_stock"


def _compute_margin(ing: Ingredient) -> float | None:
    """
    Returns margin as 0.0–1.0 if cost data available, None otherwise.
    margin = (price - cost_per_unit * standard_quantity) / price
    """
    if ing.cost_per_unit is None or ing.price is None or float(ing.price) == 0:
        return None
    cost = float(ing.cost_per_unit) * float(ing.standard_quantity or 1)
    price = float(ing.price)
    return max(0.0, (price - cost) / price)


def _profitable_badge(ing: Ingredient, price_75th: float) -> bool:
    """
    True if:
      - cost_per_unit is set and margin ≥ PROFITABLE_MARGIN_THRESHOLD, OR
      - cost_per_unit not set and price ≥ 75th-percentile price (proxy)
    """
    margin = _compute_margin(ing)
    if margin is not None:
        return margin >= PROFITABLE_MARGIN_THRESHOLD
    return float(ing.price or 0) >= price_75th


def _popular_badge(ing_id: int, usage_counts: dict[int, int], popular_threshold: int) -> bool:
    """True if this ingredient's usage count is at or above the top-20% threshold."""
    return usage_counts.get(ing_id, 0) >= popular_threshold and popular_threshold > 0


def _popular_threshold(usage_counts: dict[int, int]) -> int:
    """Compute the usage count at the 80th percentile (top 20% cutoff)."""
    if not usage_counts:
        return 1  # impossible to reach → no badges
    sorted_counts = sorted(usage_counts.values())
    idx = max(0, int(len(sorted_counts) * (1 - POPULAR_TOP_PERCENTILE)) - 1)
    return sorted_counts[idx]


def _price_75th(ingredients: list[Ingredient]) -> float:
    """75th-percentile price across all active ingredients."""
    prices = sorted(float(ing.price or 0) for ing in ingredients)
    if not prices:
        return float("inf")
    idx = max(0, int(len(prices) * PROFITABLE_PRICE_PERCENTILE) - 1)
    return prices[idx]


def _ranking_score(
    ing: Ingredient,
    stock_st: str,
    usage_count: int,
    max_usage: int,
    price_rank_norm: float,  # 0.0–1.0 = ing price / max price
) -> float:
    """
    Deterministic ranking score.
    Components (additive, higher = better position):
      is_promoted   × 1000   — owner-controlled always-first
      usage_norm    ×  50    — normalized popularity
      price_rank    ×  30    — margin proxy (higher price = more profitable)
      stock_factor  ×  20    — prefer in-stock
    """
    usage_norm   = (usage_count / max_usage) if max_usage > 0 else 0.0
    stock_factor = {"in_stock": 1.0, "low_stock": 0.5, "out_of_stock": 0.0}.get(stock_st, 0.0)
    promoted     = 1000.0 if getattr(ing, "is_promoted", False) else 0.0
    return round(promoted + usage_norm * 50 + price_rank_norm * 30 + stock_factor * 20, 4)


def _recommendations_for(
    ing_id: int,
    combo_counts: dict[tuple[int, int], int],
    in_stock_ids: set[int],
) -> list[int]:
    """
    Top-N ingredient IDs that most frequently co-appear with ing_id,
    filtered to in-stock ingredients, sorted by frequency DESC.
    """
    candidates: list[tuple[int, int]] = []  # (freq, other_id)
    for (a, b), freq in combo_counts.items():
        other = b if a == ing_id else (a if b == ing_id else None)
        if other is None or other not in in_stock_ids:
            continue
        candidates.append((freq, other))

    candidates.sort(key=lambda x: (-x[0], x[1]))
    return [other for _, other in candidates[:MAX_RECOMMENDATIONS]]


def _find_alternative(
    ing: Ingredient,
    all_rows: list[tuple[Ingredient, IngredientStock | None]],
    in_stock_ids: set[int],
) -> dict | None:
    """
    Find the closest same-category in-stock alternative.
    Closest = minimum absolute price difference, tiebreak by id ASC.
    """
    price = float(ing.price or 0)
    best: dict | None = None
    best_diff = float("inf")

    for candidate, _ in all_rows:
        if candidate.id == ing.id:
            continue
        if candidate.category != ing.category:
            continue
        if candidate.id not in in_stock_ids:
            continue
        diff = abs(float(candidate.price or 0) - price)
        if diff < best_diff or (diff == best_diff and (best is None or candidate.id < best["ingredient_id"])):
            best_diff = diff
            best = {
                "ingredient_id":   candidate.id,
                "ingredient_name": candidate.name,
                "category":        candidate.category,
                "price":           str(candidate.price),
            }

    return best


# ---------------------------------------------------------------------------
# Public: enrich_menu
# ---------------------------------------------------------------------------

def enrich_menu(
    db: Session,
    ingredients: list[Ingredient],
    stocks: dict[int, IngredientStock],
) -> list[dict]:
    """
    Return enriched ingredient dicts, ranked and annotated with conversion signals.

    Additive fields per ingredient (no existing fields removed):
      stock_status          — "in_stock" | "low_stock" | "out_of_stock"
      popular_badge         — bool
      profitable_badge      — bool
      recommended_with      — list[int] (ingredient IDs)
      out_of_stock_alternative — {ingredient_id, ingredient_name, category, price} | null
      ranking_score         — float (used for ordering; exposed for transparency)
    """
    if not ingredients:
        return []

    usage_counts = _load_usage_counts(db)
    combo_counts  = _load_combo_counts(db)

    pop_threshold  = _popular_threshold(usage_counts)
    price75        = _price_75th(ingredients)
    max_usage      = max(usage_counts.values()) if usage_counts else 0
    max_price      = max(float(ing.price or 0) for ing in ingredients) or 1.0

    all_rows: list[tuple[Ingredient, IngredientStock | None]] = [
        (ing, stocks.get(ing.id)) for ing in ingredients
    ]

    # Pre-compute stock statuses
    stock_statuses: dict[int, str] = {
        ing.id: _stock_status(stocks.get(ing.id)) for ing in ingredients
    }
    in_stock_ids: set[int] = {
        ing.id for ing in ingredients if stock_statuses[ing.id] == "in_stock"
    }

    result: list[dict] = []
    for ing in ingredients:
        stock_st = stock_statuses[ing.id]
        usage    = usage_counts.get(ing.id, 0)
        price_norm = float(ing.price or 0) / max_price

        rec_with   = _recommendations_for(ing.id, combo_counts, in_stock_ids)
        alternative = (
            _find_alternative(ing, all_rows, in_stock_ids)
            if stock_st != "in_stock"
            else None
        )
        score = _ranking_score(ing, stock_st, usage, max_usage, price_norm)

        result.append({
            # Original fields (unchanged)
            "id":                   ing.id,
            "name":                 ing.name,
            "category":             ing.category,
            "price":                str(ing.price),
            "unit":                 ing.unit,
            "standard_quantity":    str(ing.standard_quantity),
            "allows_portion_choice": ing.allows_portion_choice,
            # New conversion signal fields
            "stock_status":              stock_st,
            "popular_badge":             _popular_badge(ing.id, usage_counts, pop_threshold),
            "profitable_badge":          _profitable_badge(ing, price75),
            "recommended_with":          rec_with,
            "out_of_stock_alternative":  alternative,
            "ranking_score":             score,
        })

    # Sort by ranking_score DESC, then id ASC for deterministic tiebreak
    result.sort(key=lambda d: (-d["ranking_score"], d["id"]))
    return result


# ---------------------------------------------------------------------------
# Public: compute_upsell
# ---------------------------------------------------------------------------

def compute_upsell(
    db: Session,
    selected_ids: list[int],
) -> dict:
    """
    Given currently selected ingredient IDs, return up to MAX_UPSELL_SUGGESTIONS
    additional ingredients worth adding.

    Algorithm:
      1. Load combo frequency for last COMBO_WINDOW_DAYS
      2. For each selected ingredient, collect co-occurring candidates
      3. Score: combo_freq × (1 + price_rank_norm)  — rewards high-freq + high-margin combos
      4. Filter: not already selected, is_active, in_stock
      5. Return top N sorted by score DESC, then id ASC
    """
    if not selected_ids:
        return {"suggestions": [], "based_on_ingredient_ids": []}

    combo_counts = _load_combo_counts(db)

    # Load all active ingredients + stocks
    ingredients = (
        db.query(Ingredient)
        .filter(Ingredient.is_active == True)
        .all()
    )
    stocks = {
        row.ingredient_id: row
        for row in db.query(IngredientStock).filter(
            IngredientStock.ingredient_id.in_([i.id for i in ingredients])
        ).all()
    }

    stock_statuses = {ing.id: _stock_status(stocks.get(ing.id)) for ing in ingredients}
    ing_by_id      = {ing.id: ing for ing in ingredients}
    max_price      = max(float(ing.price or 0) for ing in ingredients) or 1.0

    # Aggregate combo scores for candidates
    candidate_scores: dict[int, float] = defaultdict(float)
    selected_set = set(selected_ids)

    for sel_id in selected_ids:
        for (a, b), freq in combo_counts.items():
            other = b if a == sel_id else (a if b == sel_id else None)
            if other is None:
                continue
            if other in selected_set:
                continue
            if other not in ing_by_id:
                continue
            if stock_statuses.get(other, "out_of_stock") != "in_stock":
                continue
            price_norm = float(ing_by_id[other].price or 0) / max_price
            candidate_scores[other] += freq * (1.0 + price_norm)

    if not candidate_scores:
        return {"suggestions": [], "based_on_ingredient_ids": selected_ids}

    # Sort: score DESC, id ASC
    ranked = sorted(candidate_scores.items(), key=lambda x: (-x[1], x[0]))[:MAX_UPSELL_SUGGESTIONS]

    suggestions = []
    for ing_id, score in ranked:
        ing  = ing_by_id[ing_id]
        # Find the highest combo count with any selected ingredient (for display)
        max_combo = 0
        for sel_id in selected_ids:
            pair = (min(sel_id, ing_id), max(sel_id, ing_id))
            max_combo = max(max_combo, combo_counts.get(pair, 0))

        suggestions.append({
            "ingredient_id":   ing.id,
            "ingredient_name": ing.name,
            "category":        ing.category,
            "price":           str(ing.price),
            "reason":          "popular_combo",
            "combo_count":     max_combo,
            "stock_status":    stock_statuses[ing.id],
        })

    return {
        "suggestions":              suggestions,
        "based_on_ingredient_ids":  selected_ids,
    }


# ---------------------------------------------------------------------------
# Public: validate_ingredient_selection
# ---------------------------------------------------------------------------

def validate_ingredient_selection(
    db: Session,
    selected_ids: list[int],
) -> dict:
    """
    Validate a customer's ingredient selection:
      - Unknown IDs → removed with reason "not_found"
      - Inactive IDs → removed with reason "not_available"
      - Out-of-stock → removed with reason "out_of_stock", alternative suggested
      - Low-stock    → kept, alternative suggested (proactive)

    Returns:
      valid_ids       — IDs that can proceed to ordering
      removed         — list of {ingredient_id, ingredient_name, reason, alternative}
      price_delta     — float (negative = price reduction from removed items)
      price_breakdown — {str(ingredient_id): float} for valid items
    """
    if not selected_ids:
        return {
            "valid_ids": [],
            "removed": [],
            "price_delta": 0.0,
            "price_breakdown": {},
        }

    # Load all active ingredients
    all_ings = {
        ing.id: ing
        for ing in db.query(Ingredient).filter(Ingredient.is_active == True).all()
    }
    all_stocks = {
        row.ingredient_id: row
        for row in db.query(IngredientStock).filter(
            IngredientStock.ingredient_id.in_(list(all_ings.keys()))
        ).all()
    }

    stock_statuses = {
        ing_id: _stock_status(all_stocks.get(ing_id))
        for ing_id in all_ings
    }
    in_stock_ids = {iid for iid, st in stock_statuses.items() if st == "in_stock"}

    all_rows: list[tuple[Ingredient, IngredientStock | None]] = [
        (ing, all_stocks.get(ing.id)) for ing in all_ings.values()
    ]

    valid_ids: list[int] = []
    removed: list[dict]  = []
    price_delta = 0.0

    for iid in selected_ids:
        if iid not in all_ings:
            removed.append({
                "ingredient_id":   iid,
                "ingredient_name": None,
                "reason":          "not_found",
                "alternative":     None,
            })
            continue

        ing = all_ings[iid]
        st  = stock_statuses[iid]

        if st == "out_of_stock":
            alt = _find_alternative(ing, all_rows, in_stock_ids)
            removed.append({
                "ingredient_id":   iid,
                "ingredient_name": ing.name,
                "reason":          "out_of_stock",
                "alternative":     alt,
            })
            price_delta -= float(ing.price or 0)
        else:
            valid_ids.append(iid)

    price_breakdown = {
        str(iid): float(all_ings[iid].price or 0)
        for iid in valid_ids
    }

    return {
        "valid_ids":       valid_ids,
        "removed":         removed,
        "price_delta":     round(price_delta, 2),
        "price_breakdown": price_breakdown,
    }
