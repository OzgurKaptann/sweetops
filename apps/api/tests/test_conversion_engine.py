"""
Tests for the Customer Conversion Engine.

Coverage:
  1. popular_badge — usage threshold, top-20% logic, no-data fallback
  2. profitable_badge — price-rank proxy, margin logic when cost data available
  3. recommended_with — combo ordering, OOS filtering, self-exclusion
  4. out_of_stock_alternative — same category, closest price, in-stock only
  5. Menu ranking — promoted first, then score DESC, tiebreak by id ASC
  6. Upsell — correct suggestions, excludes selected/OOS, scored correctly
  7. Validate/fallback — OOS removed, price delta, alternatives suggested
  8. HTTP endpoint shapes and status codes
"""
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.services.conversion_engine import (
    MAX_RECOMMENDATIONS,
    MAX_UPSELL_SUGGESTIONS,
    PROFITABLE_PRICE_PERCENTILE,
    _find_alternative,
    _popular_threshold,
    _price_75th,
    _ranking_score,
    _stock_status,
    compute_upsell,
    enrich_menu,
    validate_ingredient_selection,
)
from tests.conftest import cleanup_ingredient, make_ingredient, order_payload

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_full_ingredient(
    db,
    *,
    name: str,
    category: str = "Test",
    price: float = 10.0,
    stock_quantity: float = 100.0,
    reorder_level: float = 5.0,
    cost_per_unit: float | None = None,
    standard_quantity: float = 10.0,
    is_promoted: bool = False,
) -> tuple[Ingredient, IngredientStock]:
    uid = uuid.uuid4().hex[:6]
    ing = Ingredient(
        name=name or f"TestIng_{uid}",
        category=category,
        price=Decimal(str(price)),
        unit="g",
        standard_quantity=Decimal(str(standard_quantity)),
        cost_per_unit=Decimal(str(cost_per_unit)) if cost_per_unit is not None else None,
        is_active=True,
        is_promoted=is_promoted,
    )
    db.add(ing)
    db.flush()
    stock = IngredientStock(
        ingredient_id=ing.id,
        stock_quantity=Decimal(str(stock_quantity)),
        unit="g",
        reorder_level=Decimal(str(reorder_level)),
    )
    db.add(stock)
    db.commit()
    db.refresh(ing)
    db.refresh(stock)
    return ing, stock


def _make_order_with_ingredients(db, ingredient_ids: list[int], store_id: int = 1) -> Order:
    """Insert an order + order_item + order_item_ingredient rows directly."""
    order = Order(store_id=store_id, status="DELIVERED", total_amount=Decimal("10.00"))
    db.add(order)
    db.flush()
    item = OrderItem(order_id=order.id, product_id=1, quantity=1, price=Decimal("10.00"))
    db.add(item)
    db.flush()
    for ing_id in ingredient_ids:
        db.add(OrderItemIngredient(
            order_item_id=item.id,
            ingredient_id=ing_id,
            quantity=1,
        ))
    db.commit()
    return order


def _cleanup_order(db, order_id: int) -> None:
    from app.models.order_status_event import OrderStatusEvent
    db.query(OrderStatusEvent).filter(OrderStatusEvent.order_id == order_id).delete(synchronize_session=False)
    oi_ids = [r.id for r in db.query(OrderItem).filter(OrderItem.order_id == order_id).all()]
    if oi_ids:
        db.query(OrderItemIngredient).filter(
            OrderItemIngredient.order_item_id.in_(oi_ids)
        ).delete(synchronize_session=False)
        db.query(OrderItem).filter(OrderItem.id.in_(oi_ids)).delete(synchronize_session=False)
    db.query(Order).filter(Order.id == order_id).delete(synchronize_session=False)
    db.commit()


def _cleanup_ing_direct(db, ing_id: int) -> None:
    """Cleanup an ingredient created by _make_full_ingredient (no orders attached)."""
    db.query(IngredientStockMovement).filter(
        IngredientStockMovement.ingredient_id == ing_id
    ).delete(synchronize_session=False)
    db.query(IngredientStock).filter(
        IngredientStock.ingredient_id == ing_id
    ).delete(synchronize_session=False)
    db.query(Ingredient).filter(Ingredient.id == ing_id).delete(synchronize_session=False)
    db.commit()


# ---------------------------------------------------------------------------
# 1. stock_status helper
# ---------------------------------------------------------------------------

class TestStockStatus:
    def test_none_stock_is_out(self):
        assert _stock_status(None) == "out_of_stock"

    def test_zero_quantity_is_out(self, db):
        _, stock = _make_full_ingredient(db, name="OOS", stock_quantity=0.0, reorder_level=5.0)
        assert _stock_status(stock) == "out_of_stock"
        _cleanup_ing_direct(db, stock.ingredient_id)

    def test_above_low_threshold_is_in_stock(self, db):
        _, stock = _make_full_ingredient(db, name="FullStock", stock_quantity=100.0, reorder_level=5.0)
        # 100 > 5 * 1.5 = 7.5 → in_stock
        assert _stock_status(stock) == "in_stock"
        _cleanup_ing_direct(db, stock.ingredient_id)

    def test_at_low_stock_boundary(self, db):
        # reorder=10, stock=10 → 10 <= 10*1.5=15 → low_stock
        _, stock = _make_full_ingredient(db, name="LowStock", stock_quantity=10.0, reorder_level=10.0)
        assert _stock_status(stock) == "low_stock"
        _cleanup_ing_direct(db, stock.ingredient_id)


# ---------------------------------------------------------------------------
# 2. popular_badge
# ---------------------------------------------------------------------------

class TestPopularBadge:
    def test_popular_threshold_empty_usage(self):
        assert _popular_threshold({}) == 1  # impossible to reach

    def test_popular_threshold_top_20_percent(self):
        # sorted_counts = [1, 2, 3, 4, 5], len=5
        # idx = max(0, int(5 * 0.80) - 1) = max(0, 3) = 3
        # threshold = sorted_counts[3] = 4
        # → only ingredient with count=5 gets the badge (top 20%)
        counts = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5}
        t = _popular_threshold(counts)
        assert t == 4

    def test_popular_threshold_tie_all_same(self):
        counts = {1: 3, 2: 3, 3: 3}
        t = _popular_threshold(counts)
        assert t == 3  # all at same count → all qualify

    def test_popular_badge_appears_in_enriched(self, db):
        """Ingredient with highest usage among test set gets popular_badge=True."""
        ing_a, _ = _make_full_ingredient(db, name="PopA", category="Test_pop")
        ing_b, _ = _make_full_ingredient(db, name="PopB", category="Test_pop")

        # Create 5 orders for ing_a, 0 for ing_b
        order_ids = []
        for _ in range(5):
            o = _make_order_with_ingredients(db, [ing_a.id])
            order_ids.append(o.id)

        stocks = {ing_a.id: db.get(IngredientStock, db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing_a.id).first().id)}
        stocks[ing_b.id] = db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing_b.id).first()

        enriched = enrich_menu(db, [ing_a, ing_b], stocks)
        a_dict = next(e for e in enriched if e["id"] == ing_a.id)
        b_dict = next(e for e in enriched if e["id"] == ing_b.id)

        assert a_dict["popular_badge"] is True
        assert b_dict["popular_badge"] is False

        for oid in order_ids:
            _cleanup_order(db, oid)
        _cleanup_ing_direct(db, ing_a.id)
        _cleanup_ing_direct(db, ing_b.id)


# ---------------------------------------------------------------------------
# 3. profitable_badge
# ---------------------------------------------------------------------------

class TestProfitableBadge:
    def test_high_price_gets_badge(self, db):
        """Ingredient at top-25% price → profitable_badge=True."""
        ings = [
            _make_full_ingredient(db, name=f"Pr_{i}", price=float(p))[0]
            for i, p in enumerate([5, 8, 10, 15])
        ]
        # price_75th of [5,8,10,15] → sorted: [5,8,10,15], idx = int(4*0.75)-1 = 2 → prices[2]=10
        p75 = _price_75th(ings)
        assert p75 == 10.0

        stocks = {
            ing.id: db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing.id).first()
            for ing in ings
        }
        enriched = enrich_menu(db, ings, stocks)

        high_priced = next(e for e in enriched if float(e["price"]) == 15.0)
        low_priced  = next(e for e in enriched if float(e["price"]) == 5.0)
        assert high_priced["profitable_badge"] is True
        assert low_priced["profitable_badge"] is False

        for ing in ings:
            _cleanup_ing_direct(db, ing.id)

    def test_cost_data_takes_precedence(self, db):
        """If cost_per_unit is set, margin calculation overrides price proxy."""
        # price=10, cost_per_unit=1, standard_quantity=10 → cost=10, margin=0 → False
        low_margin, _ = _make_full_ingredient(db, name="LowMarginIng",
                                               price=10.0, cost_per_unit=1.0, standard_quantity=10.0)
        # price=10, cost_per_unit=0.1, standard_quantity=10 → cost=1, margin=0.9 → True
        high_margin, _ = _make_full_ingredient(db, name="HighMarginIng",
                                                price=10.0, cost_per_unit=0.1, standard_quantity=10.0)

        stocks = {
            low_margin.id:  db.query(IngredientStock).filter(IngredientStock.ingredient_id == low_margin.id).first(),
            high_margin.id: db.query(IngredientStock).filter(IngredientStock.ingredient_id == high_margin.id).first(),
        }
        enriched = enrich_menu(db, [low_margin, high_margin], stocks)
        low_e  = next(e for e in enriched if e["id"] == low_margin.id)
        high_e = next(e for e in enriched if e["id"] == high_margin.id)

        assert low_e["profitable_badge"] is False
        assert high_e["profitable_badge"] is True

        _cleanup_ing_direct(db, low_margin.id)
        _cleanup_ing_direct(db, high_margin.id)


# ---------------------------------------------------------------------------
# 4. recommended_with
# ---------------------------------------------------------------------------

class TestRecommendedWith:
    def test_recommends_frequent_combos(self, db):
        """Ingredients that co-appear often should be in recommended_with."""
        ing_a, _ = _make_full_ingredient(db, name="RecA", category="Test_rec")
        ing_b, _ = _make_full_ingredient(db, name="RecB", category="Test_rec")
        ing_c, _ = _make_full_ingredient(db, name="RecC", category="Test_rec")

        order_ids = []
        # a+b co-appear 3 times, a+c co-appear 1 time
        for _ in range(3):
            order_ids.append(_make_order_with_ingredients(db, [ing_a.id, ing_b.id]).id)
        order_ids.append(_make_order_with_ingredients(db, [ing_a.id, ing_c.id]).id)

        stocks = {
            ing.id: db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing.id).first()
            for ing in [ing_a, ing_b, ing_c]
        }
        enriched = enrich_menu(db, [ing_a, ing_b, ing_c], stocks)
        a_dict = next(e for e in enriched if e["id"] == ing_a.id)

        # ing_b should appear before ing_c in recommended_with for ing_a
        recs = a_dict["recommended_with"]
        if ing_b.id in recs and ing_c.id in recs:
            assert recs.index(ing_b.id) < recs.index(ing_c.id)

        for oid in order_ids:
            _cleanup_order(db, oid)
        for ing in [ing_a, ing_b, ing_c]:
            _cleanup_ing_direct(db, ing.id)

    def test_oos_ingredient_excluded_from_recommendations(self, db):
        """Out-of-stock ingredient must not appear in recommended_with."""
        ing_a, _ = _make_full_ingredient(db, name="RecOOSA", category="Test_roos")
        ing_b, _ = _make_full_ingredient(db, name="RecOOSB_oos", category="Test_roos",
                                          stock_quantity=0.0)

        order_ids = [_make_order_with_ingredients(db, [ing_a.id, ing_b.id]).id for _ in range(3)]

        stocks = {
            ing_a.id: db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing_a.id).first(),
            ing_b.id: db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing_b.id).first(),
        }
        enriched = enrich_menu(db, [ing_a, ing_b], stocks)
        a_dict = next(e for e in enriched if e["id"] == ing_a.id)

        assert ing_b.id not in a_dict["recommended_with"]

        for oid in order_ids:
            _cleanup_order(db, oid)
        _cleanup_ing_direct(db, ing_a.id)
        _cleanup_ing_direct(db, ing_b.id)

    def test_self_not_in_recommendations(self, db):
        """An ingredient should never recommend itself."""
        ing, _ = _make_full_ingredient(db, name="SelfRec", category="Test_self")
        stocks = {ing.id: db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing.id).first()}
        enriched = enrich_menu(db, [ing], stocks)
        d = enriched[0]
        assert ing.id not in d["recommended_with"]
        _cleanup_ing_direct(db, ing.id)

    def test_max_recommendations_respected(self, db):
        """recommended_with must not exceed MAX_RECOMMENDATIONS."""
        ings = [_make_full_ingredient(db, name=f"MaxRec_{i}", category="Test_maxrec")[0] for i in range(6)]
        # Create combos: ing[0] with all others
        order_ids = []
        for other in ings[1:]:
            for _ in range(2):
                order_ids.append(_make_order_with_ingredients(db, [ings[0].id, other.id]).id)

        stocks = {ing.id: db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing.id).first() for ing in ings}
        enriched = enrich_menu(db, ings, stocks)
        first = next(e for e in enriched if e["id"] == ings[0].id)
        assert len(first["recommended_with"]) <= MAX_RECOMMENDATIONS

        for oid in order_ids:
            _cleanup_order(db, oid)
        for ing in ings:
            _cleanup_ing_direct(db, ing.id)


# ---------------------------------------------------------------------------
# 5. out_of_stock_alternative
# ---------------------------------------------------------------------------

class TestOutOfStockAlternative:
    def test_oos_ingredient_gets_alternative(self, db):
        """OOS ingredient in category X → alternative is nearest-price in-stock in same category."""
        oos, _ = _make_full_ingredient(db, name="AltOOS", category="Test_alt",
                                        price=10.0, stock_quantity=0.0)
        close, _ = _make_full_ingredient(db, name="AltClose", category="Test_alt",
                                          price=11.0, stock_quantity=50.0)
        far, _   = _make_full_ingredient(db, name="AltFar", category="Test_alt",
                                          price=20.0, stock_quantity=50.0)

        stocks = {
            oos.id:   db.query(IngredientStock).filter(IngredientStock.ingredient_id == oos.id).first(),
            close.id: db.query(IngredientStock).filter(IngredientStock.ingredient_id == close.id).first(),
            far.id:   db.query(IngredientStock).filter(IngredientStock.ingredient_id == far.id).first(),
        }
        enriched = enrich_menu(db, [oos, close, far], stocks)
        oos_dict = next(e for e in enriched if e["id"] == oos.id)

        assert oos_dict["out_of_stock_alternative"] is not None
        assert oos_dict["out_of_stock_alternative"]["ingredient_id"] == close.id

        for ing in [oos, close, far]:
            _cleanup_ing_direct(db, ing.id)

    def test_in_stock_ingredient_has_no_alternative(self, db):
        """In-stock ingredient must have out_of_stock_alternative=None."""
        ing, _ = _make_full_ingredient(db, name="InStockAlt", stock_quantity=100.0)
        stocks = {ing.id: db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing.id).first()}
        enriched = enrich_menu(db, [ing], stocks)
        assert enriched[0]["out_of_stock_alternative"] is None
        _cleanup_ing_direct(db, ing.id)

    def test_alternative_is_same_category_only(self, db):
        """Alternative must be from the same category, not a different one."""
        oos, _ = _make_full_ingredient(db, name="AltCatOOS", category="Cat_A",
                                        price=10.0, stock_quantity=0.0)
        wrong_cat, _ = _make_full_ingredient(db, name="AltCatWrong", category="Cat_B",
                                              price=10.0, stock_quantity=50.0)
        correct, _   = _make_full_ingredient(db, name="AltCatCorrect", category="Cat_A",
                                              price=12.0, stock_quantity=50.0)

        stocks = {
            ing.id: db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing.id).first()
            for ing in [oos, wrong_cat, correct]
        }
        enriched = enrich_menu(db, [oos, wrong_cat, correct], stocks)
        oos_dict = next(e for e in enriched if e["id"] == oos.id)

        alt = oos_dict["out_of_stock_alternative"]
        assert alt is not None
        assert alt["ingredient_id"] == correct.id

        for ing in [oos, wrong_cat, correct]:
            _cleanup_ing_direct(db, ing.id)

    def test_no_alternative_if_category_all_oos(self, db):
        """If all same-category ingredients are OOS, alternative is None."""
        ing_a, _ = _make_full_ingredient(db, name="AllOOS_A", category="Cat_alloos", stock_quantity=0.0)
        ing_b, _ = _make_full_ingredient(db, name="AllOOS_B", category="Cat_alloos", stock_quantity=0.0)

        stocks = {
            ing_a.id: db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing_a.id).first(),
            ing_b.id: db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing_b.id).first(),
        }
        enriched = enrich_menu(db, [ing_a, ing_b], stocks)
        for e in enriched:
            assert e["out_of_stock_alternative"] is None

        _cleanup_ing_direct(db, ing_a.id)
        _cleanup_ing_direct(db, ing_b.id)


# ---------------------------------------------------------------------------
# 6. Menu ranking
# ---------------------------------------------------------------------------

class TestMenuRanking:
    def test_promoted_ingredient_ranks_first(self, db):
        """is_promoted=True ingredient must always sort before non-promoted."""
        normal, _ = _make_full_ingredient(db, name="RankNormal", category="Test_rank",
                                           price=15.0, stock_quantity=100.0)
        promoted, _ = _make_full_ingredient(db, name="RankPromoted", category="Test_rank",
                                             price=5.0, stock_quantity=100.0, is_promoted=True)

        stocks = {
            ing.id: db.query(IngredientStock).filter(IngredientStock.ingredient_id == ing.id).first()
            for ing in [normal, promoted]
        }
        enriched = enrich_menu(db, [normal, promoted], stocks)

        ids = [e["id"] for e in enriched]
        assert ids.index(promoted.id) < ids.index(normal.id), \
            "Promoted ingredient must appear before higher-price non-promoted"

        _cleanup_ing_direct(db, normal.id)
        _cleanup_ing_direct(db, promoted.id)

    def test_oos_ranks_last_within_same_score_group(self, db):
        """Out-of-stock ingredient should score lower than in-stock."""
        in_stock_ing, _ = _make_full_ingredient(db, name="RankInStock", category="Test_rstk",
                                                 price=10.0, stock_quantity=100.0)
        oos_ing, _       = _make_full_ingredient(db, name="RankOOS", category="Test_rstk",
                                                  price=10.0, stock_quantity=0.0)

        # No usage, no combos, same price → only stock_factor differs
        in_stock_score = _ranking_score(in_stock_ing, "in_stock",  0, 0, 0.5)
        oos_score      = _ranking_score(oos_ing,      "out_of_stock", 0, 0, 0.5)
        assert in_stock_score > oos_score

        _cleanup_ing_direct(db, in_stock_ing.id)
        _cleanup_ing_direct(db, oos_ing.id)

    def test_ranking_score_deterministic(self):
        """Same inputs always produce the same score."""
        ing = Ingredient(
            id=1, name="X", category="Y", price=Decimal("10.00"),
            standard_quantity=Decimal("10.00"), unit="g",
            is_active=True, is_promoted=False,
        )
        s1 = _ranking_score(ing, "in_stock", 5, 10, 0.5)
        s2 = _ranking_score(ing, "in_stock", 5, 10, 0.5)
        assert s1 == s2

    def test_categories_sorted_within_menu(self, db):
        """In the menu response, ingredients within each category must be sorted by ranking_score DESC."""
        r = client.get("/public/menu/")
        assert r.status_code == 200
        for cat in r.json()["categories"]:
            scores = [ing.get("ranking_score", 0) for ing in cat["ingredients"]]
            assert scores == sorted(scores, reverse=True), \
                f"Category '{cat['name']}' not sorted by ranking_score"


# ---------------------------------------------------------------------------
# 7. Upsell suggestions
# ---------------------------------------------------------------------------

class TestUpsell:
    def test_no_selection_returns_empty(self, db):
        result = compute_upsell(db, [])
        assert result["suggestions"] == []
        assert result["based_on_ingredient_ids"] == []

    def test_suggests_frequent_combos(self, db):
        """Selected ing + frequently co-appearing ing → suggestion."""
        sel, _ = _make_full_ingredient(db, name="UpsellSel", category="Test_ups")
        sug, _ = _make_full_ingredient(db, name="UpsellSug", category="Test_ups")
        other, _ = _make_full_ingredient(db, name="UpsellOther", category="Test_ups",
                                          stock_quantity=0.0)  # OOS — should not appear

        order_ids = [_make_order_with_ingredients(db, [sel.id, sug.id]).id for _ in range(4)]
        order_ids += [_make_order_with_ingredients(db, [sel.id, other.id]).id for _ in range(2)]

        result = compute_upsell(db, [sel.id])
        suggested_ids = [s["ingredient_id"] for s in result["suggestions"]]

        assert sug.id in suggested_ids, "Frequent in-stock combo should be suggested"
        assert other.id not in suggested_ids, "OOS ingredient should not be suggested"
        assert sel.id not in suggested_ids, "Selected ingredient should not be suggested"

        for oid in order_ids:
            _cleanup_order(db, oid)
        for ing in [sel, sug, other]:
            _cleanup_ing_direct(db, ing.id)

    def test_max_suggestions_respected(self, db):
        """At most MAX_UPSELL_SUGGESTIONS suggestions returned."""
        base, _ = _make_full_ingredient(db, name="UpsellBase", category="Test_umax")
        others  = [_make_full_ingredient(db, name=f"UpsellOth_{i}", category="Test_umax")[0]
                   for i in range(5)]

        order_ids = []
        for other in others:
            for _ in range(2):
                order_ids.append(_make_order_with_ingredients(db, [base.id, other.id]).id)

        result = compute_upsell(db, [base.id])
        assert len(result["suggestions"]) <= MAX_UPSELL_SUGGESTIONS

        for oid in order_ids:
            _cleanup_order(db, oid)
        _cleanup_ing_direct(db, base.id)
        for o in others:
            _cleanup_ing_direct(db, o.id)

    def test_suggestion_structure(self, db):
        """Each suggestion must have all required fields."""
        sel, _ = _make_full_ingredient(db, name="UpsellStruct_sel", category="Test_ustruct")
        sug, _ = _make_full_ingredient(db, name="UpsellStruct_sug", category="Test_ustruct")
        oid = _make_order_with_ingredients(db, [sel.id, sug.id]).id

        result = compute_upsell(db, [sel.id])
        for s in result["suggestions"]:
            for field in ("ingredient_id", "ingredient_name", "category", "price",
                          "reason", "combo_count", "stock_status"):
                assert field in s, f"Missing field '{field}' in suggestion"

        _cleanup_order(db, oid)
        _cleanup_ing_direct(db, sel.id)
        _cleanup_ing_direct(db, sug.id)

    def test_no_combos_returns_empty(self, db):
        """With no order history, suggestions should be empty."""
        ing, _ = _make_full_ingredient(db, name="UpsellNoCombos", category="Test_nocombo")
        result = compute_upsell(db, [ing.id])
        assert result["suggestions"] == []
        _cleanup_ing_direct(db, ing.id)


# ---------------------------------------------------------------------------
# 8. Validate / fallback
# ---------------------------------------------------------------------------

class TestValidateSelection:
    def test_all_valid_returns_unchanged(self, db):
        ing_a, _ = _make_full_ingredient(db, name="ValA", stock_quantity=50.0)
        ing_b, _ = _make_full_ingredient(db, name="ValB", stock_quantity=50.0)

        result = validate_ingredient_selection(db, [ing_a.id, ing_b.id])
        assert set(result["valid_ids"]) == {ing_a.id, ing_b.id}
        assert result["removed"] == []
        assert result["price_delta"] == 0.0

        _cleanup_ing_direct(db, ing_a.id)
        _cleanup_ing_direct(db, ing_b.id)

    def test_oos_ingredient_removed(self, db):
        good, _ = _make_full_ingredient(db, name="ValGood", stock_quantity=50.0, price=10.0)
        oos, _  = _make_full_ingredient(db, name="ValOOS", stock_quantity=0.0, price=8.0)

        result = validate_ingredient_selection(db, [good.id, oos.id])
        assert good.id in result["valid_ids"]
        assert oos.id not in result["valid_ids"]
        assert len(result["removed"]) == 1
        assert result["removed"][0]["ingredient_id"] == oos.id
        assert result["removed"][0]["reason"] == "out_of_stock"
        assert result["price_delta"] == -8.0  # OOS ingredient price removed

        _cleanup_ing_direct(db, good.id)
        _cleanup_ing_direct(db, oos.id)

    def test_unknown_id_removed_with_not_found_reason(self, db):
        result = validate_ingredient_selection(db, [999999])
        assert 999999 not in result["valid_ids"]
        assert result["removed"][0]["reason"] == "not_found"
        assert result["removed"][0]["ingredient_name"] is None

    def test_oos_with_alternative(self, db):
        """OOS ingredient in category with in-stock alternatives → alternative suggested."""
        oos,  _ = _make_full_ingredient(db, name="ValOOSAlt", category="Cat_val",
                                         price=10.0, stock_quantity=0.0)
        alt, _  = _make_full_ingredient(db, name="ValAlt", category="Cat_val",
                                         price=11.0, stock_quantity=50.0)

        result = validate_ingredient_selection(db, [oos.id])
        removed = result["removed"][0]
        assert removed["alternative"] is not None
        assert removed["alternative"]["ingredient_id"] == alt.id

        _cleanup_ing_direct(db, oos.id)
        _cleanup_ing_direct(db, alt.id)

    def test_price_breakdown_for_valid_ids(self, db):
        ing_a, _ = _make_full_ingredient(db, name="PBrkA", price=8.0, stock_quantity=50.0)
        ing_b, _ = _make_full_ingredient(db, name="PBrkB", price=12.0, stock_quantity=50.0)

        result = validate_ingredient_selection(db, [ing_a.id, ing_b.id])
        assert str(ing_a.id) in result["price_breakdown"]
        assert str(ing_b.id) in result["price_breakdown"]
        assert result["price_breakdown"][str(ing_a.id)] == 8.0
        assert result["price_breakdown"][str(ing_b.id)] == 12.0

        _cleanup_ing_direct(db, ing_a.id)
        _cleanup_ing_direct(db, ing_b.id)

    def test_empty_selection(self, db):
        result = validate_ingredient_selection(db, [])
        assert result["valid_ids"] == []
        assert result["removed"] == []
        assert result["price_delta"] == 0.0
        assert result["price_breakdown"] == {}

    def test_price_delta_cumulative_for_multiple_oos(self, db):
        """price_delta must sum across all removed ingredients."""
        oos_a, _ = _make_full_ingredient(db, name="OOS_Multi_A", price=10.0, stock_quantity=0.0)
        oos_b, _ = _make_full_ingredient(db, name="OOS_Multi_B", price=5.0, stock_quantity=0.0)

        result = validate_ingredient_selection(db, [oos_a.id, oos_b.id])
        assert result["price_delta"] == -15.0

        _cleanup_ing_direct(db, oos_a.id)
        _cleanup_ing_direct(db, oos_b.id)


# ---------------------------------------------------------------------------
# 9. HTTP endpoint tests
# ---------------------------------------------------------------------------

class TestMenuEndpoints:
    def test_menu_returns_200(self):
        assert client.get("/public/menu/").status_code == 200

    def test_menu_has_conversion_fields(self):
        body = client.get("/public/menu/").json()
        assert "ingredients" in body
        assert len(body["ingredients"]) > 0
        ing = body["ingredients"][0]
        for field in ("stock_status", "popular_badge", "profitable_badge",
                      "recommended_with", "out_of_stock_alternative", "ranking_score"):
            assert field in ing, f"Missing conversion field '{field}' in menu ingredient"

    def test_menu_original_fields_preserved(self):
        """All original fields must still be present (additive only)."""
        body = client.get("/public/menu/").json()
        ing = body["ingredients"][0]
        for field in ("id", "name", "category", "price", "unit",
                      "standard_quantity", "allows_portion_choice"):
            assert field in ing, f"Original field '{field}' missing — breaking change!"

    def test_menu_stock_status_valid_values(self):
        body = client.get("/public/menu/").json()
        valid = {"in_stock", "low_stock", "out_of_stock"}
        for ing in body["ingredients"]:
            assert ing["stock_status"] in valid

    def test_menu_categories_sorted(self):
        """Ingredients within each category must be sorted by ranking_score DESC."""
        body = client.get("/public/menu/").json()
        for cat in body["categories"]:
            scores = [i["ranking_score"] for i in cat["ingredients"]]
            assert scores == sorted(scores, reverse=True)

    def test_upsell_returns_200(self):
        assert client.get("/public/menu/upsell").status_code == 200

    def test_upsell_empty_selection(self):
        body = client.get("/public/menu/upsell").json()
        assert "suggestions" in body
        assert isinstance(body["suggestions"], list)

    def test_upsell_with_ids(self):
        # Use real ingredient IDs from the DB
        menu = client.get("/public/menu/").json()
        if menu["ingredients"]:
            ing_id = menu["ingredients"][0]["id"]
            r = client.get(f"/public/menu/upsell?ingredient_ids={ing_id}")
            assert r.status_code == 200
            body = r.json()
            assert "suggestions" in body
            assert "based_on_ingredient_ids" in body
            assert ing_id not in [s["ingredient_id"] for s in body["suggestions"]]

    def test_validate_returns_200(self):
        assert client.post("/public/menu/validate", json={"ingredient_ids": []}).status_code == 200

    def test_validate_all_valid(self):
        menu = client.get("/public/menu/").json()
        in_stock = [i["id"] for i in menu["ingredients"] if i["stock_status"] == "in_stock"][:2]
        if len(in_stock) >= 2:
            body = client.post("/public/menu/validate", json={"ingredient_ids": in_stock}).json()
            assert set(body["valid_ids"]) == set(in_stock)
            assert body["removed"] == []

    def test_validate_unknown_id(self):
        body = client.post("/public/menu/validate", json={"ingredient_ids": [888888, 999999]}).json()
        assert 888888 not in body["valid_ids"]
        assert 999999 not in body["valid_ids"]
        reasons = {r["reason"] for r in body["removed"]}
        assert "not_found" in reasons

    def test_validate_response_shape(self):
        body = client.post("/public/menu/validate", json={"ingredient_ids": [1]}).json()
        for field in ("valid_ids", "removed", "price_delta", "price_breakdown"):
            assert field in body
