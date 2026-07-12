"""
Owner Decision Engine — deterministic, explainable signals + action lifecycle.

Six distinct signal evaluators run per request (see get_owner_decisions):

  Realtime evaluators (moment-in-time):
    stock_risk      — velocity-based stockout prediction + revenue loss estimate
    slow_moving     — ingredients with stock but zero deductions in 24h
    demand_spike    — last-1h order rate vs 23h rolling baseline
    sla_risk        — kitchen orders breaching SLA thresholds
    revenue_anomaly — hourly revenue vs same-period baseline

  Metric-driven evaluator (pattern-level, batches four measurement-layer checks
  under "metric_" ids — see _metric_driven_signals):
    metric_driven   — combo health, upsell visibility, owner engagement,
                      kitchen performance

The response field ``signals_evaluated`` reports how many of these distinct
evaluators the engine actually executed for the authenticated store and request
(see get_owner_decisions for the exact contract).

Persistence layer:
  Every signal is upserted into owner_decisions on GET /owner/decisions/.
  Completed/dismissed decisions are suppressed for COOLDOWN_HOURS; after that
  window they reset to pending so the owner sees recurring issues again.

Prioritization:
  decision_score = base_score + urgency_bonus + blocking_bonus
  Ordering: decision_score DESC, then decision_id ASC (deterministic tiebreak).

Lifecycle transitions (via apply_decision_action):
  pending → acknowledged → completed
  pending | acknowledged → dismissed
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ingredient import Ingredient
from app.models.ingredient_stock import (
    MOVEMENT_CONSUMPTION,
    IngredientStock,
    IngredientStockMovement,
)
from app.models.order import Order
from app.models.owner_decision import OwnerDecision

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STOCK_RISK_HIGH_HOURS   = 6    # stockout < 6h  → high severity
STOCK_RISK_MEDIUM_HOURS = 12   # stockout < 12h → medium severity
DEMAND_SPIKE_HIGH   = 3.0
DEMAND_SPIKE_MEDIUM = 2.0
DEMAND_SPIKE_LOW    = 1.5
SLA_WARNING_MINUTES  = 7
SLA_CRITICAL_MINUTES = 10
REVENUE_DROP_HIGH    = 0.35    # < 35% of baseline → high
REVENUE_DROP_MEDIUM  = 0.60    # < 60% of baseline → medium
REVENUE_SPIKE_THRESHOLD = 2.5

COOLDOWN_HOURS = 2             # completed/dismissed decisions suppressed for 2h

# Scoring constants
_BASE_SCORE  = {"high": 100, "medium": 50, "low": 20}
_BLOCK_BONUS = 25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _severity_order(s: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(s, 3)


def _is_blocking(signal_type: str, severity: str, data: dict) -> bool:
    """
    A decision is blocking when the owner must act before the next order
    cycle or revenue is immediately at risk.
    """
    if signal_type == "stock_risk" and severity == "high":
        return True
    if signal_type == "demand_spike" and severity in ("high", "medium"):
        return True
    if signal_type == "sla_risk" and severity == "high":
        return True
    if signal_type == "revenue_anomaly" and severity == "high" and data.get("direction") == "drop":
        return True
    return False


def _urgency_bonus(signal_type: str, data: dict) -> float:
    """
    Type-specific urgency bonus added on top of the severity base score.
    All formulas are capped to prevent one signal from dominating unfairly.
    """
    if signal_type == "stock_risk":
        h = data.get("hours_to_stockout")
        if h is None:
            return 0.0
        if h == 0:
            return 30.0
        if h < STOCK_RISK_HIGH_HOURS:
            return (STOCK_RISK_HIGH_HOURS - h) * 5.0
        if h < STOCK_RISK_MEDIUM_HOURS:
            return (STOCK_RISK_MEDIUM_HOURS - h) * 2.0
        return 0.0

    if signal_type == "demand_spike":
        ratio = data.get("spike_ratio", 0.0)
        return min((ratio - DEMAND_SPIKE_LOW) * 10.0, 25.0)

    if signal_type == "sla_risk":
        critical = data.get("critical_count", 0)
        warning  = data.get("warning_count", 0)
        return min(critical * 5.0 + warning * 2.0, 30.0)

    if signal_type == "revenue_anomaly":
        direction = data.get("direction", "")
        ratio = data.get("ratio", 1.0)
        if direction == "drop":
            return 15.0 if ratio < REVENUE_DROP_HIGH else 5.0
        return 0.0

    return 0.0  # slow_moving


def _decision_score(severity: str, signal_type: str, data: dict, blocking: bool) -> float:
    base    = _BASE_SCORE.get(severity, 20)
    urgency = _urgency_bonus(signal_type, data)
    bonus   = _BLOCK_BONUS if blocking else 0
    return round(base + urgency + bonus, 2)


def _why_now(signal_type: str, severity: str, data: dict) -> str:
    """
    Concrete, time-anchored reason this decision is surfaced right now.

    Owner-facing prose, so Turkish. The signal_type / severity keys it switches
    on are internal identifiers and stay English.
    """
    if signal_type == "stock_risk":
        h = data.get("hours_to_stockout")
        if h == 0:
            return f"{data['ingredient_name']} stokta yok. Bu malzemeyi içeren her yeni sipariş karşılanamıyor."
        if h is not None:
            return f"Saatte {data['velocity_per_hour']:.2f} {data.get('unit','birim')} tüketim hızıyla stok {h:.1f} saat içinde bitiyor."
        return f"{data['ingredient_name']} sipariş seviyesinin altında ve son 24 saatte hiç talep görmedi."

    if signal_type == "demand_spike":
        ratio = data.get("spike_ratio", 0.0)
        return f"Son 60 dakikadaki sipariş hızı, 23 saatlik ortalamanın {ratio:.1f} katı."

    if signal_type == "slow_moving":
        return f"{data['ingredient_name']} stoğunda {data['current_stock']} birim duruyor ama son 24 saatte hiç kullanılmadı."

    if signal_type == "sla_risk":
        worst = data.get("worst_age_minutes", 0)
        critical = data.get("critical_count", 0)
        if critical:
            return f"{critical} sipariş {SLA_CRITICAL_MINUTES} dakikadan uzun süredir bekliyor. En uzun bekleyen: {worst:.1f} dk."
        return f"Siparişler {SLA_CRITICAL_MINUTES} dakikalık hazırlık süresi sınırına yaklaşıyor. En uzun bekleyen: {worst:.1f} dk."

    if signal_type == "revenue_anomaly":
        direction = data.get("direction", "")
        ratio = data.get("ratio", 1.0)
        baseline = data.get("avg_hourly_baseline", 0.0)
        last = data.get("last_1h_revenue", 0.0)
        if direction == "drop":
            pct = round((1 - ratio) * 100)
            return f"Son bir saatteki ciro ₺{last:.0f}; saatlik ortalama olan ₺{baseline:.0f} tutarının %{pct} altında."
        pct = round((ratio - 1) * 100)
        return f"Son bir saatteki ciro ₺{last:.0f}; saatlik ortalama olan ₺{baseline:.0f} tutarının %{pct} üstünde."

    return "Bu uyarı otomatik olarak oluşturuldu."


def _expected_impact(signal_type: str, severity: str, data: dict, blocking: bool) -> str:
    """What happens if the owner takes the recommended action. Owner-facing Turkish."""
    if signal_type == "stock_risk":
        risk = data.get("revenue_at_risk", 0.0)
        if risk > 0:
            return f"Mal kabul yaparsanız, karşılanamayan siparişlerden doğacak ~₺{risk:.0f} ciro kaybını önlersiniz."
        return "Mal kabul yaparsanız bu malzemeyi içeren siparişler aksamaz."

    if signal_type == "demand_spike":
        ratio = data.get("spike_ratio", 0.0)
        if severity == "high":
            return f"Normalin {ratio:.1f} katı yoğunlukta mutfak kapasitesini artırmak, süre aşımlarını ve müşteri kaybını önler."
        return f"Bu {ratio:.1f} kat yoğunlukta malzemeleri önceden hazırlamak, sipariş başına hazırlık süresini kısaltır."

    if signal_type == "slow_moving":
        capital = data.get("tied_capital", 0.0)
        return f"Kampanya yapmak veya sipariş miktarını azaltmak, stokta bağlı ~₺{capital:.0f} sermayeyi serbest bırakır."

    if signal_type == "sla_risk":
        breach_count = data.get("critical_count", 0) + data.get("warning_count", 0)
        return f"Şimdi müdahale ederseniz {breach_count} siparişte süre aşımını ve müşteri şikâyetini önlersiniz."

    if signal_type == "revenue_anomaly":
        direction = data.get("direction", "")
        if direction == "drop":
            if severity == "high":
                return "Nedenini bulup gidermek, şu anda kaybedilen ciroyu geri kazandırabilir."
            return "Erken müdahale, geçici bir düşüşün kalıcı bir soruna dönüşmesini engeller."
        return "Yoğunluk sürerken mutfak kapasitesini korumak, hazırlık sürelerini bozmadan ciroyu tam yakalamanızı sağlar."

    return "Önerilen adımı uygulamak operasyonel riski azaltır."


# ---------------------------------------------------------------------------
# Signal functions
# (Each returns a list of raw signal dicts — no DB interaction here.)
# ---------------------------------------------------------------------------

def _stock_risk_signals(db: Session, store_id: int) -> list[dict]:
    """
    Stockout risk for ONE store.

    Both halves of the calculation must come from the same branch, and this is
    where getting it wrong would be most invisible: divide Kadıköy's available
    quantity by Beşiktaş's burn rate and you get a plausible-looking number of
    hours that is simply about nothing. So the stock rows are filtered to the
    store, and so are the CONSUMPTION movements the velocity is measured from.
    """
    now = _now_utc()
    window_start = now - timedelta(hours=24)

    rows = (
        db.query(Ingredient, IngredientStock)
        .join(
            IngredientStock,
            (IngredientStock.ingredient_id == Ingredient.id)
            & (IngredientStock.store_id == store_id),
        )
        .filter(Ingredient.is_active == True)
        .all()
    )

    # Velocity is the rate at which THIS STORE physically consumes stock —
    # CONSUMPTION movements only. Reservations are not consumption: an order
    # that is sitting in the queue (or is about to be cancelled) has not burned
    # any batter, and counting it here would inflate the burn rate and cry
    # stockout too early.
    movements = (
        db.query(
            IngredientStockMovement.ingredient_id,
            func.sum(IngredientStockMovement.quantity).label("total_consumed"),
        )
        .filter(
            IngredientStockMovement.store_id == store_id,
            IngredientStockMovement.movement_type == MOVEMENT_CONSUMPTION,
            IngredientStockMovement.created_at >= window_start,
        )
        .group_by(IngredientStockMovement.ingredient_id)
        .all()
    )
    velocity_map: dict[int, float] = {
        m.ingredient_id: float(m.total_consumed) / 24.0 for m in movements
    }

    signals: list[dict] = []
    for ing, stock in rows:
        # Stockout risk is about what we can still SELL, so it runs off
        # available (on_hand - reserved), not on_hand. Stock already promised to
        # accepted orders cannot be sold again, and treating it as if it could
        # would hide a stockout that has effectively already happened.
        current_qty = float(stock.available_quantity)
        on_hand_qty = float(stock.on_hand_quantity)
        reserved_qty = float(stock.reserved_quantity)
        reorder     = float(stock.reorder_level) if stock.reorder_level else 0.0

        if current_qty > reorder:
            continue

        velocity = velocity_map.get(ing.id, 0.0)
        price    = float(ing.price) if ing.price else 0.0

        if current_qty <= 0:
            severity         = "high"
            hours_to_stockout: float | None = 0.0
            description      = f"{ing.name} stokta yok. Bu malzemeyi içeren siparişler karşılanamıyor."
        elif velocity > 0:
            hours_to_stockout = current_qty / velocity
            if hours_to_stockout < STOCK_RISK_HIGH_HOURS:
                severity = "high"
            elif hours_to_stockout < STOCK_RISK_MEDIUM_HOURS:
                severity = "medium"
            else:
                severity = "low"
            description = (
                f"{ing.name} mevcut tüketim hızıyla ({velocity:.1f} {ing.unit}/saat) "
                f"{hours_to_stockout:.1f} saat içinde tükenecek."
            )
        else:
            severity          = "low"
            hours_to_stockout = None
            description       = (
                f"{ing.name} sipariş seviyesinde ({current_qty} {ing.unit}) "
                f"ve son 24 saatte hiç talep görmedi."
            )

        if hours_to_stockout is not None and velocity > 0:
            hours_until_empty = hours_to_stockout if current_qty > 0 else 0.0
            revenue_at_risk   = round(velocity * hours_until_empty * price, 2)
            impact = f"Son 24 saatlik talebe göre ~₺{revenue_at_risk:.0f} ciro risk altında."
        else:
            revenue_at_risk = 0.0
            impact = "Son dönemde talep yok. Fire riskini izleyin veya sipariş miktarını azaltın."

        signal_data = {
            "ingredient_id":     ing.id,
            "ingredient_name":   ing.name,
            "unit":              ing.unit,
            # current_stock is the AVAILABLE quantity — what the shop can still
            # sell. on_hand/reserved are surfaced alongside it so the owner can
            # see whether a shortage is physical or merely promised away.
            "current_stock":     current_qty,
            "available_quantity": current_qty,
            "on_hand_quantity":  on_hand_qty,
            "reserved_quantity": reserved_qty,
            "reorder_level":     reorder,
            "velocity_per_hour": round(velocity, 3),
            "hours_to_stockout": round(hours_to_stockout, 1) if hours_to_stockout is not None else None,
            "revenue_at_risk":   revenue_at_risk,
        }
        blocking = _is_blocking("stock_risk", severity, signal_data)
        score    = _decision_score(severity, "stock_risk", signal_data, blocking)

        signals.append({
            "id":                    f"stock_risk_{ing.id}",
            "type":                  "stock_risk",
            "severity":              severity,
            "decision_score":        score,
            "blocking_vs_non_blocking": blocking,
            "title":                 f"Stok tükenme riski: {ing.name}",
            "description":           description,
            "impact":                impact,
            "recommended_action":    (
                f"{ing.name} için hemen sipariş verin."
                if severity == "high"
                else f"{ing.name} için yakın zamanda sipariş planlayın."
            ),
            "why_now":       _why_now("stock_risk", severity, signal_data),
            "expected_impact": _expected_impact("stock_risk", severity, signal_data, blocking),
            "data": signal_data,
        })

    return signals


def _demand_spike_signals(db: Session, store_id: int) -> list[dict]:
    now           = _now_utc()
    one_hour_ago  = now - timedelta(hours=1)
    window_start  = now - timedelta(hours=24)

    last_1h: int = (
        db.query(func.count(Order.id))
        .filter(Order.store_id == store_id, Order.created_at >= one_hour_ago)
        .scalar()
        or 0
    )
    prev_23h: int = (
        db.query(func.count(Order.id))
        .filter(
            Order.store_id == store_id,
            Order.created_at >= window_start,
            Order.created_at < one_hour_ago,
        )
        .scalar()
        or 0
    )

    avg_baseline = prev_23h / 23.0 if prev_23h > 0 else 0.0
    if last_1h == 0 or avg_baseline == 0:
        return []

    ratio = last_1h / avg_baseline
    if ratio < DEMAND_SPIKE_LOW:
        return []

    if ratio >= DEMAND_SPIKE_HIGH:
        severity = "high"
    elif ratio >= DEMAND_SPIKE_MEDIUM:
        severity = "medium"
    else:
        severity = "low"

    signal_data = {
        "last_1h_orders":      last_1h,
        "avg_hourly_baseline": round(avg_baseline, 2),
        "spike_ratio":         round(ratio, 2),
    }
    blocking = _is_blocking("demand_spike", severity, signal_data)
    score    = _decision_score(severity, "demand_spike", signal_data, blocking)

    return [{
        "id":                    "demand_spike_current",
        "type":                  "demand_spike",
        "severity":              severity,
        "decision_score":        score,
        "blocking_vs_non_blocking": blocking,
        "title":                 "Ani talep artışı",
        "description":           (
            f"Son bir saatte {last_1h} sipariş geldi; önceki 23 saatin ortalaması "
            f"saatte {avg_baseline:.1f} siparişti. Bu, normalin {ratio:.1f} katı."
        ),
        "impact": (
            f"Mutfak normalin {ratio:.1f} katı yükle çalışıyor. "
            f"Bu yoğunlukta hazırlık süresi aşımı riski belirgin şekilde artıyor."
        ),
        "recommended_action": (
            "Mutfak kapasitesini hemen artırın ve personeli uyarın."
            if severity == "high"
            else "Mutfak sırasını yakından izleyin, sık kullanılan malzemeleri önceden hazırlayın."
        ),
        "why_now":        _why_now("demand_spike", severity, signal_data),
        "expected_impact": _expected_impact("demand_spike", severity, signal_data, blocking),
        "data": signal_data,
    }]


def _slow_moving_signals(db: Session, store_id: int) -> list[dict]:
    """
    Capital sitting idle on ONE store's shelves.

    Store scope matters in both directions here. An ingredient that Beşiktaş
    sells briskly can still be dead stock in Kadıköy, and the Kadıköy manager is
    the one who has to run the promotion — so "has this moved?" is asked of this
    store's movements, against this store's on-hand.
    """
    now          = _now_utc()
    window_start = now - timedelta(hours=24)

    # "Moving" means physically consumed in THIS store's kitchen. An ingredient
    # that only sat in reservations for orders that never got cooked has not
    # moved, and one that moved in another branch has not moved here.
    active_ids: set[int] = {
        row.ingredient_id
        for row in db.query(IngredientStockMovement.ingredient_id)
        .filter(
            IngredientStockMovement.store_id == store_id,
            IngredientStockMovement.movement_type == MOVEMENT_CONSUMPTION,
            IngredientStockMovement.created_at >= window_start,
        )
        .distinct()
        .all()
    }

    rows = (
        db.query(Ingredient, IngredientStock)
        .join(
            IngredientStock,
            (IngredientStock.ingredient_id == Ingredient.id)
            & (IngredientStock.store_id == store_id),
        )
        .filter(Ingredient.is_active == True)
        .all()
    )

    signals: list[dict] = []
    for ing, stock in rows:
        # Slow-moving is about capital sitting on the shelf, so this one is
        # correctly about PHYSICAL stock (on-hand), not availability.
        current_qty = float(stock.on_hand_quantity)
        reorder     = float(stock.reorder_level) if stock.reorder_level else 0.0

        if current_qty <= 0:
            continue
        if ing.id in active_ids:
            continue
        if current_qty <= reorder:
            continue

        price        = float(ing.price) if ing.price else 0.0
        tied_capital = round(current_qty * price, 2)

        signal_data = {
            "ingredient_id":   ing.id,
            "ingredient_name": ing.name,
            "current_stock":   current_qty,
            "reorder_level":   reorder,
            "tied_capital":    tied_capital,
            "hours_since_last_use": 24,
        }
        blocking = _is_blocking("slow_moving", "medium", signal_data)
        score    = _decision_score("medium", "slow_moving", signal_data, blocking)

        signals.append({
            "id":                    f"slow_moving_{ing.id}",
            "type":                  "slow_moving",
            "severity":              "medium",
            "decision_score":        score,
            "blocking_vs_non_blocking": blocking,
            "title":                 f"Yavaş hareket eden stok: {ing.name}",
            "description":           (
                f"{ing.name} stoğunda {current_qty} {ing.unit} var "
                f"ama son 24 saatte hiçbir siparişte kullanılmadı."
            ),
            "impact":             f"Stokta ~₺{tied_capital:.0f} sermaye bağlı. Bozulabilir bir malzemede fire riski var.",
            "recommended_action": f"{ing.name} için kampanya yapın veya bir sonraki sipariş miktarını azaltın.",
            "why_now":        _why_now("slow_moving", "medium", signal_data),
            "expected_impact": _expected_impact("slow_moving", "medium", signal_data, blocking),
            "data": signal_data,
        })

    return signals


def _sla_risk_signals(db: Session, store_id: int) -> list[dict]:
    now = _now_utc()

    active_orders = (
        db.query(Order)
        .filter(Order.store_id == store_id, Order.status.in_(["NEW", "IN_PREP"]))
        .all()
    )
    if not active_orders:
        return []

    critical_orders: list[tuple[int, float]] = []
    warning_orders:  list[tuple[int, float]] = []

    for order in active_orders:
        created = order.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (now - created).total_seconds() / 60.0
        if age >= SLA_CRITICAL_MINUTES:
            critical_orders.append((order.id, round(age, 1)))
        elif age >= SLA_WARNING_MINUTES:
            warning_orders.append((order.id, round(age, 1)))

    if not critical_orders and not warning_orders:
        return []

    if critical_orders:
        severity      = "high"
        breach_count  = len(critical_orders)
        worst_age     = max(age for _, age in critical_orders)
        description   = (
            f"{breach_count} sipariş {SLA_CRITICAL_MINUTES} dakikalık hazırlık süresini aştı. "
            f"En uzun bekleyen: {worst_age:.1f} dk."
        )
        recommended_action = (
            "Ek personel çağırın veya müşterileri gecikme konusunda hemen bilgilendirin. "
            "Öncelikli siparişler: " + ", ".join(f"#{oid}" for oid, _ in critical_orders[:5]) + "."
        )
    else:
        severity      = "medium"
        breach_count  = len(warning_orders)
        worst_age     = max(age for _, age in warning_orders)
        description   = (
            f"{breach_count} sipariş {SLA_CRITICAL_MINUTES} dakikalık hazırlık süresi sınırına yaklaşıyor. "
            f"En uzun bekleyen: {worst_age:.1f} dk."
        )
        recommended_action = "Süre aşımını önlemek için bekleyen siparişleri şimdi önceliklendirin."

    signal_data = {
        "critical_order_ids": [oid for oid, _ in critical_orders],
        "warning_order_ids":  [oid for oid, _ in warning_orders],
        "critical_count":     len(critical_orders),
        "warning_count":      len(warning_orders),
        "worst_age_minutes":  worst_age,
    }
    blocking = _is_blocking("sla_risk", severity, signal_data)
    score    = _decision_score(severity, "sla_risk", signal_data, blocking)

    return [{
        "id":                    "sla_risk_current",
        "type":                  "sla_risk",
        "severity":              severity,
        "decision_score":        score,
        "blocking_vs_non_blocking": blocking,
        "title":                 "Mutfak hazırlık süresi riski",
        "description":           description,
        "impact": (
            f"{breach_count} siparişte müşteri memnuniyetsizliği riski var. "
            f"Tekrarlayan süre aşımları müşteri kaybına yol açıyor."
        ),
        "recommended_action":  recommended_action,
        "why_now":        _why_now("sla_risk", severity, signal_data),
        "expected_impact": _expected_impact("sla_risk", severity, signal_data, blocking),
        "data": signal_data,
    }]


def _revenue_anomaly_signals(db: Session, store_id: int) -> list[dict]:
    now           = _now_utc()
    one_hour_ago  = now - timedelta(hours=1)
    window_start  = now - timedelta(hours=24)

    last_1h_revenue: float = float(
        db.query(func.coalesce(func.sum(Order.total_amount), 0))
        .filter(Order.store_id == store_id, Order.created_at >= one_hour_ago)
        .scalar() or 0
    )
    prev_23h_revenue: float = float(
        db.query(func.coalesce(func.sum(Order.total_amount), 0))
        .filter(
            Order.store_id == store_id,
            Order.created_at >= window_start,
            Order.created_at < one_hour_ago,
        )
        .scalar() or 0
    )

    avg_baseline = prev_23h_revenue / 23.0 if prev_23h_revenue > 0 else 0.0
    if avg_baseline < 1.0:
        return []

    ratio = last_1h_revenue / avg_baseline
    if REVENUE_DROP_MEDIUM <= ratio <= REVENUE_SPIKE_THRESHOLD:
        return []

    if ratio < REVENUE_DROP_HIGH:
        severity  = "high"
        direction = "drop"
        pct       = round((1 - ratio) * 100)
        description = (
            f"Son bir saatteki ciro ₺{last_1h_revenue:.0f}; saatlik ortalama olan "
            f"₺{avg_baseline:.0f} tutarının %{pct} altında."
        )
        impact             = "Ciddi bir düşüş var. Mutfakta aksama, menüde bir sorun veya talep kaybı olabilir."
        recommended_action = (
            "Hemen inceleyin: menüde tükenen malzeme, mutfakta aksama "
            "veya dış bir etken olup olmadığını kontrol edin."
        )
    elif ratio < REVENUE_DROP_MEDIUM:
        severity  = "medium"
        direction = "drop"
        pct       = round((1 - ratio) * 100)
        description = (
            f"Son bir saatteki ciro ₺{last_1h_revenue:.0f}; saatlik ortalama olan "
            f"₺{avg_baseline:.0f} tutarının %{pct} altında."
        )
        impact             = "Ortalamanın altında bir performans. Sakin bir saat olabilir ya da bir sorunun ilk işareti."
        recommended_action = "Sipariş akışını izleyin. Mutfağın normal çalıştığını kontrol edin."
    else:
        severity  = "low"
        direction = "spike"
        pct       = round((ratio - 1) * 100)
        description = (
            f"Son bir saatteki ciro ₺{last_1h_revenue:.0f}; saatlik ortalama olan "
            f"₺{avg_baseline:.0f} tutarının %{pct} üstünde."
        )
        impact             = "Olumlu bir ciro artışı. Mutfağın bu tempoyu sürdürebildiğinden emin olun."
        recommended_action = "Mutfak kapasitesini kontrol edin. Popüler malzemeleri önceden hazırlayın."

    signal_data = {
        "last_1h_revenue":     round(last_1h_revenue, 2),
        "avg_hourly_baseline": round(avg_baseline, 2),
        "ratio":               round(ratio, 3),
        "direction":           direction,
    }
    blocking = _is_blocking("revenue_anomaly", severity, signal_data)
    score    = _decision_score(severity, "revenue_anomaly", signal_data, blocking)

    return [{
        "id":                    "revenue_anomaly_current",
        "type":                  "revenue_anomaly",
        "severity":              severity,
        "decision_score":        score,
        "blocking_vs_non_blocking": blocking,
        # `direction` is an internal enum ("drop"/"spike") — map it to a label
        # here rather than interpolating the raw value into owner-facing copy.
        "title":                 ("Ciro düşüşü" if direction == "drop" else "Ciro artışı"),
        "description":           description,
        "impact":                impact,
        "recommended_action":    recommended_action,
        "why_now":        _why_now("revenue_anomaly", severity, signal_data),
        "expected_impact": _expected_impact("revenue_anomaly", severity, signal_data, blocking),
        "data": signal_data,
    }]


# ---------------------------------------------------------------------------
# Upsert logic
# ---------------------------------------------------------------------------

def _upsert_decision(db: Session, signal: dict, now: datetime, store_id: int) -> OwnerDecision | None:
    """
    Upsert one signal into owner_decisions for this store:
      - INSERT if new
      - UPDATE mutable fields if pending/acknowledged
      - Skip if completed/dismissed within the cooldown window
      - Reset to pending if completed/dismissed and cooldown has expired
    Returns the row to include in the response, or None if suppressed.
    """
    decision_id = signal["id"]
    # Composite primary key (store_id, decision_id).
    row: OwnerDecision | None = db.get(OwnerDecision, (store_id, decision_id))

    if row is None:
        # First time this signal fires → create as pending
        row = OwnerDecision(
            store_id=store_id,
            decision_id=decision_id,
            status="pending",
        )
        db.add(row)
        _apply_signal_fields(row, signal)
        db.flush()
        return row

    # Row exists — check if it's in a terminal state within cooldown
    if row.status in ("completed", "dismissed"):
        cooldown_cutoff = now - timedelta(hours=COOLDOWN_HOURS)
        if row.updated_at and row.updated_at.replace(tzinfo=timezone.utc) > cooldown_cutoff:
            # Still within cooldown — suppress this signal
            return None
        # Cooldown expired — reset to pending
        row.status                  = "pending"
        row.acknowledged_at         = None
        row.completed_at            = None
        row.actor_id                = None
        row.resolution_note         = None
        row.resolution_quality      = None
        row.estimated_revenue_saved = None

    # Update mutable signal fields regardless of current status
    _apply_signal_fields(row, signal)
    db.flush()
    return row


def _apply_signal_fields(row: OwnerDecision, signal: dict) -> None:
    """Copy all signal-computed fields onto the ORM row."""
    row.type                   = signal["type"]
    row.severity               = signal["severity"]
    row.decision_score         = signal["decision_score"]
    row.blocking_vs_non_blocking = signal["blocking_vs_non_blocking"]
    row.title                  = signal["title"]
    row.description            = signal["description"]
    row.impact                 = signal["impact"]
    row.recommended_action     = signal["recommended_action"]
    row.why_now                = signal["why_now"]
    row.expected_impact        = signal["expected_impact"]
    row.data                   = signal["data"]


def _row_to_dict(row: OwnerDecision) -> dict:
    return {
        "id":                    row.decision_id,
        "store_id":              row.store_id,
        "type":                  row.type,
        "severity":              row.severity,
        "decision_score":        row.decision_score,
        "blocking_vs_non_blocking": row.blocking_vs_non_blocking,
        "title":                 row.title,
        "description":           row.description,
        "impact":                row.impact,
        "recommended_action":    row.recommended_action,
        "why_now":               row.why_now,
        "expected_impact":       row.expected_impact,
        "data":                  row.data or {},
        "status":                row.status,
        "acknowledged_at":       row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        "completed_at":          row.completed_at.isoformat() if row.completed_at else None,
        "actor_id":              row.actor_id,
        "resolution_note":       row.resolution_note,
        "resolution_quality":       row.resolution_quality,
        "estimated_revenue_saved":  row.estimated_revenue_saved,
        "created_at":            row.created_at.isoformat() if row.created_at else None,
        "updated_at":            row.updated_at.isoformat() if row.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Metric-driven signals
# (Generated from the measurement layer — pattern-level, not moment-in-time.)
#
# Decision IDs use "metric_" prefix — guaranteed non-overlapping with the
# realtime signals above (stock_risk_N, sla_risk_current, etc.).
#
# Score cap: 80  — these surface BELOW urgent realtime signals (base 100+)
#                  but ABOVE informational low-severity signals.
# ---------------------------------------------------------------------------

_METRIC_SCORE_CAP = 80.0

# Thresholds mirror operational_context_service constants (no shared import
# to avoid circular dependency — both files own their own constant copy).
_COMBO_RATE_THRESHOLD   = 0.30   # combo_usage_rate < 30%
_UPSELL_RATE_THRESHOLD  = 0.15   # upsell_acceptance_rate < 15%
_SLA_BREACH_THRESHOLD   = 0.20   # sla_breach_rate > 20%
_COMPLETION_RATE_LOW    = 0.30   # completion_rate < 30%
_DECISIONS_SEEN_MIN     = 3      # need ≥ 3 seen for engagement signal to fire


def _metric_driven_signals(db: Session, store_id: int) -> list[dict]:
    """
    Generate decisions driven by today's measurement layer output, scoped to
    this store.

    Four signal types (all "metric_" prefixed IDs):
      metric_combo_health       — combo_usage_rate below threshold (pattern, not a moment)
      metric_upsell_visibility  — upsell_acceptance_rate below threshold
      metric_owner_engagement   — completion_rate below threshold with enough seen
      metric_kitchen_performance — sla_breach_rate above threshold (sustained, not live)

    Only fires on DataQuality.status == "valid".  Metrics with low_sample, no_data,
    or unreliable quality are silently skipped — no false signals.
    """
    # Late import to avoid circular dependency (metrics_service → decision_engine)
    try:
        from app.services.metrics_service import fetch_daily_metrics
        metrics = fetch_daily_metrics(db, store_id=store_id)
    except Exception as exc:
        logger.error("metric_driven_signals: could not fetch metrics: %s", exc)
        return []

    signals: list[dict] = []
    conv    = metrics.conversion
    kitchen = metrics.kitchen
    dec     = metrics.decisions

    def _valid(quality_status: str) -> bool:
        return quality_status == "valid"

    # ── 1. Combo health ───────────────────────────────────────────────────
    if (
        _valid(conv.combo_usage_rate.quality.status)
        and conv.combo_usage_rate.value < _COMBO_RATE_THRESHOLD
    ):
        rate = conv.combo_usage_rate.value
        pct  = round(rate * 100, 1)
        thr  = round(_COMBO_RATE_THRESHOLD * 100)
        prev_pct = (
            round(conv.combo_usage_rate.prev_value * 100, 1)
            if conv.combo_usage_rate.prev_value is not None else None
        )
        trend_note = (
            f" (dün %{prev_pct} idi)"
            if prev_pct is not None and conv.combo_usage_rate.trend == "down"
            else ""
        )
        signals.append({
            "id":   "metric_combo_health",
            "type": "metric_combo_health",
            "severity": "medium",
            "decision_score": min(60.0, _METRIC_SCORE_CAP),
            "blocking_vs_non_blocking": False,
            "title": "Kombinasyon kullanımı düşük",
            "description": (
                f"Bugünün kombinasyon kullanım oranı %{pct}{trend_note}. "
                f"Hedef %{thr} üzeri. "
                "Müşteriler beklendiği kadar çok malzemeyi bir arada seçmiyor."
            ),
            "impact": (
                "Kombinasyonsuz siparişlerde ortalama sepet tutarı düşük kalıyor. "
                "Kombinasyon önerileri beklenen dönüşümü sağlamıyor."
            ),
            "recommended_action": (
                "1. Popüler kombinasyon rozetlerinin müşteri menüsünde göründüğünü doğrulayın. "
                "2. Popüler Kombinasyonlar panelindeki en çok tercih edilen ikilileri öne çıkarın. "
                "3. İlk kez gelen müşteriler için süreli bir kombinasyon önerisi deneyin."
            ),
            "why_now": (
                f"Kombinasyon kullanım oranı bugün %{pct} ile %{thr} eşiğinin altında kaldı. "
                "Menü sıralaması kombinasyon malzemelerini zaten öne çıkarıyor — "
                "sorun sürüyorsa mesele sıralama değil, menüdeki görünürlük."
            ),
            "expected_impact": (
                "Kombinasyon görünürlüğünü %30 kullanım seviyesine çıkarmak, kombinasyonlu ve "
                "kombinasyonsuz siparişler arasındaki fark göz önüne alındığında ortalama sepet "
                "tutarını genellikle %15–25 artırır."
            ),
            "data": {
                "metric": "combo_usage_rate",
                "value_at_trigger": rate,
                "threshold": _COMBO_RATE_THRESHOLD,
                "prev_value": conv.combo_usage_rate.prev_value,
                "trend": conv.combo_usage_rate.trend,
                "sample_size": conv.combo_usage_rate.quality.sample_size,
                "source": "measurement_layer",
            },
        })

    # ── 2. Upsell visibility ──────────────────────────────────────────────
    if (
        _valid(conv.upsell_acceptance_rate.quality.status)
        and conv.upsell_acceptance_rate.value < _UPSELL_RATE_THRESHOLD
    ):
        rate = conv.upsell_acceptance_rate.value
        pct  = round(rate * 100, 1)
        thr  = round(_UPSELL_RATE_THRESHOLD * 100)
        signals.append({
            "id":   "metric_upsell_visibility",
            "type": "metric_upsell_visibility",
            "severity": "low",
            "decision_score": min(40.0, _METRIC_SCORE_CAP),
            "blocking_vs_non_blocking": False,
            "title": "Ek malzeme önerileri tutmuyor",
            "description": (
                f"Bugün sipariş kalemlerinin yalnızca %{pct} kadarında 2 veya daha fazla malzeme var "
                f"(hedef %{thr} üzeri). "
                "Müşterilerin çoğu kalem başına tek malzeme seçiyor."
            ),
            "impact": (
                "Tek malzemeli kalemler, kalem başına mümkün olan en düşük ciroyu getiriyor. "
                "Kabul edilmeyen her öneri, kaçırılmış bir satış fırsatı."
            ),
            "recommended_action": (
                "1. Öneri kutusunun, müşteri kalemi onaylamadan ÖNCE göründüğünü kontrol edin. "
                "2. Önerilen 3 kombinasyonun gerçekten ilgili olduğunu doğrulayın "
                "(Popüler Kombinasyonlar paneline bakın). "
                "3. Öneri görünüyor ama tutmuyorsa metni değiştirin: "
                "'Bunu seçenler genelde [X] de ekliyor' ifadesi, düz bir 'Siparişine ekle'den daha iyi çalışır."
            ),
            "why_now": (
                f"Kalem bazında kabul oranı bugün %{pct} — %{thr} alt sınırının altında. "
                "Bu, elimizdeki en ayrıntılı dönüşüm sinyali."
            ),
            "expected_impact": (
                "Öneri kabul oranını %15'ten %30'a çıkarmak, kalem başına yaklaşık 0,15 malzeme ekler; "
                "bu da doğrudan ortalama sepet tutarına yansır."
            ),
            "data": {
                "metric": "upsell_acceptance_rate",
                "value_at_trigger": rate,
                "threshold": _UPSELL_RATE_THRESHOLD,
                "prev_value": conv.upsell_acceptance_rate.prev_value,
                "trend": conv.upsell_acceptance_rate.trend,
                "sample_size": conv.upsell_acceptance_rate.quality.sample_size,
                "source": "measurement_layer",
            },
        })

    # ── 3. Owner engagement ───────────────────────────────────────────────
    if (
        dec.decisions_seen >= _DECISIONS_SEEN_MIN
        and _valid(dec.completion_rate.quality.status)
        and dec.completion_rate.value < _COMPLETION_RATE_LOW
    ):
        cr_pct  = round(dec.completion_rate.value * 100, 1)
        thr_pct = round(_COMPLETION_RATE_LOW * 100)
        signals.append({
            "id":   "metric_owner_engagement",
            "type": "metric_owner_engagement",
            "severity": "medium",
            "decision_score": min(55.0, _METRIC_SCORE_CAP),
            "blocking_vs_non_blocking": False,
            "title": "Uyarılar kapatılmıyor",
            "description": (
                f"Bugün {dec.decisions_seen} uyarı görüntülendi ama yalnızca "
                f"{dec.decisions_completed} tanesi tamamlandı (tamamlanma oranı %{cr_pct}, "
                f"hedef %{thr_pct} üzeri). "
                "Görülüp bırakılan uyarılar ciroyu korumaz."
            ),
            "impact": (
                "Görülüp çözülmeyen stok riskleri stok tükenmesine, "
                "üzerine gidilmeyen süre aşımı riskleri müşteri kaybına dönüşüyor. "
                "Uyarılar ancak tamamlandıklarında işe yarar."
            ),
            "recommended_action": (
                f"Tamamlanmamış {dec.decisions_seen - dec.decisions_completed} uyarıyı gözden geçirin. "
                "Her biri için ya sonucunu girip tamamlayın ya da nedenini yazıp kapatın. "
                "Uyarılara göre hareket etmek zor geliyorsa, önerilen adımların mutfakta "
                "gerçekten uygulanabilir olup olmadığını değerlendirin."
            ),
            "why_now": (
                f"Bugün görülen {dec.decisions_seen} uyarıda tamamlanma oranı %{cr_pct}. "
                "Oranın %30'un altında olması ya uyarı yorgunluğuna ya da önerilen adımların "
                "uygulanabilir olmadığına işaret eder."
            ),
            "expected_impact": (
                "Tamamlanma oranını %50'nin üzerine çıkarmak, tespit edilen risklerin çoğunun "
                "ciro kaybına dönüşmeden gerçekten çözülmesini sağlar."
            ),
            "data": {
                "metric": "completion_rate",
                "value_at_trigger": dec.completion_rate.value,
                "threshold": _COMPLETION_RATE_LOW,
                "decisions_seen": dec.decisions_seen,
                "decisions_completed": dec.decisions_completed,
                "decisions_acknowledged": dec.decisions_acknowledged,
                "source": "measurement_layer",
            },
        })

    # ── 4. Kitchen performance ────────────────────────────────────────────
    if (
        _valid(kitchen.sla_breach_rate.quality.status)
        and kitchen.sla_breach_rate.value > _SLA_BREACH_THRESHOLD
    ):
        breach_pct = round(kitchen.sla_breach_rate.value * 100, 1)
        thr_pct    = round(_SLA_BREACH_THRESHOLD * 100)
        avg_prep   = (
            f"ortalama {kitchen.avg_prep_time_minutes.value:.1f} dk"
            if _valid(kitchen.avg_prep_time_minutes.quality.status)
            else "ortalama bilinmiyor"
        )
        signals.append({
            "id":   "metric_kitchen_performance",
            "type": "metric_kitchen_performance",
            "severity": "high",
            "decision_score": min(80.0, _METRIC_SCORE_CAP),
            "blocking_vs_non_blocking": True,
            "title": "Mutfak sürekli süre aşımı yapıyor",
            "description": (
                f"Bugünkü siparişlerin %{breach_pct} kadarı 10 dakikalık hazırlık süresini aştı "
                f"({avg_prep}; hedef %{thr_pct} altı). "
                "Bu tek bir aksama değil, gün geneline yayılmış bir örüntü."
            ),
            "impact": (
                "Süregelen süre aşımları, mutfakta yapısal bir kapasite sorununa işaret eder. "
                "Uzun bekleyen müşterilerin geri gelmeme ihtimali belirgin şekilde artar; "
                "süre aşımındaki her dakika olumsuz yorum riskini büyütür."
            ),
            "recommended_action": (
                "1. Mutfak ekibini kontrol edin — bugünkü talebe göre personel yetersiz mi? "
                "2. Mutfak ekranındaki birlikte hazırlama önerilerini değerlendirin. "
                "3. En karmaşık malzemeleri geçici olarak önerilerden çıkarın "
                "(sistem öneri sayısını zaten 1'e düşürdü). "
                "4. Süre aşımı oranı %35'i geçtiyse yeni siparişleri kısa süre durdurmayı düşünün."
            ),
            "why_now": (
                f"Bugünkü siparişlerin %{breach_pct} kadarı 10 dakikalık süreyi aştı. "
                "Bu oran tamamlanmış sipariş verilerinden hesaplanıyor — anlık mutfak sırası "
                "yaşını gösteren canlı uyarının aksine, sorunun gün boyu sürdüğünü doğruluyor."
            ),
            "expected_impact": (
                "Mutfaktaki yoğunluğun kaynağını gidermek, süre aşımı oranını müdahaleden sonraki "
                f"2–3 saat içinde %{thr_pct} altına indirir."
            ),
            "data": {
                "metric": "sla_breach_rate",
                "value_at_trigger": kitchen.sla_breach_rate.value,
                "threshold": _SLA_BREACH_THRESHOLD,
                "avg_prep_time_minutes": (
                    kitchen.avg_prep_time_minutes.value
                    if _valid(kitchen.avg_prep_time_minutes.quality.status) else None
                ),
                "p90_prep_time_minutes": (
                    kitchen.p90_prep_time_minutes.value
                    if _valid(kitchen.p90_prep_time_minutes.quality.status) else None
                ),
                "sample_size": kitchen.sla_breach_rate.quality.sample_size,
                "source": "measurement_layer",
            },
        })

    return signals


# ---------------------------------------------------------------------------
# Public: GET
# ---------------------------------------------------------------------------

def get_owner_decisions(db: Session, store_id: int) -> dict:
    """
    1. Compute all fresh signals (realtime + metric-driven) for this store.
    2. Upsert into owner_decisions keyed by (store_id, decision_id).
    3. Sort by decision_score DESC, then decision_id ASC.
    4. Return envelope.

    Store scoping:
      EVERY evaluator is store-scoped, including the two inventory ones. This
      used to be the awkward case: stock_risk and slow_moving read global
      inventory tables, so they had to be SKIPPED entirely once a second store
      existed — a multi-branch owner simply lost their stock signals. Now that
      physical stock carries a store_id, they are ordinary store-scoped
      evaluators like the rest, and a second branch opening costs nobody their
      decision feed.
    """
    now = _now_utc()
    all_signals: list[dict] = []

    # Distinct signal evaluators for this request. All take (db, store_id) and
    # all always run, so signals_evaluated is a constant of the code rather than
    # something that silently shrinks with the shape of the installation.
    signal_fns = [
        _stock_risk_signals,
        _slow_moving_signals,
        _demand_spike_signals,
        _sla_risk_signals,
        _revenue_anomaly_signals,
        _metric_driven_signals,
    ]

    # signals_evaluated = number of distinct evaluators executed for this store
    # and request. Deterministic and computed from the evaluator set itself so
    # it can never drift from the code, and independent of how many decisions
    # each evaluator emits (an evaluator that returns zero decisions still ran).
    signals_evaluated = len(signal_fns)

    for fn in signal_fns:
        try:
            all_signals.extend(fn(db, store_id))
        except Exception as exc:
            logger.error("decision_engine signal_fn=%s err=%s", fn.__name__, exc)

    visible: list[dict] = []
    for signal in all_signals:
        try:
            row = _upsert_decision(db, signal, now, store_id)
            if row is not None:
                visible.append(_row_to_dict(row))
        except Exception as exc:
            logger.error("decision_engine upsert decision_id=%s err=%s", signal["id"], exc)

    try:
        db.commit()
    except Exception as exc:
        logger.error("decision_engine commit err=%s", exc)
        db.rollback()

    # Sort: score DESC, then id ASC for deterministic tiebreak
    visible.sort(key=lambda d: (-d["decision_score"], d["id"]))

    summary = {
        "high":   sum(1 for d in visible if d["severity"] == "high"),
        "medium": sum(1 for d in visible if d["severity"] == "medium"),
        "low":    sum(1 for d in visible if d["severity"] == "low"),
    }

    return {
        "decisions":         visible,
        "generated_at":      now.isoformat(),
        "signals_evaluated": signals_evaluated,
        "active_count":      len(visible),
        "summary":           summary,
    }


# ---------------------------------------------------------------------------
# Public: PATCH — lifecycle transition
# ---------------------------------------------------------------------------

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "acknowledge": {"pending"},
    "complete":    {"pending", "acknowledged"},
    "dismiss":     {"pending", "acknowledged"},
}


def apply_decision_action(
    db: Session,
    store_id: int,
    decision_id: str,
    action: str,
    actor_id: str | None = None,
    resolution_note: str | None = None,
    resolution_quality: str | None = None,
    estimated_revenue_saved: float | None = None,
) -> dict:
    """
    Transition a decision to a new lifecycle status within the authenticated
    store. A decision belonging to another store is reported as not-found so
    cross-store existence is never disclosed.

    Returns the updated decision dict.
    Raises ValueError on invalid transition, LookupError if not found.
    The `actor_id` must be the authenticated user's id — callers never pass a
    client-supplied actor.
    """
    row: OwnerDecision | None = db.get(OwnerDecision, (store_id, decision_id))
    if row is None:
        raise LookupError(f"Decision '{decision_id}' not found.")

    allowed = _VALID_TRANSITIONS.get(action)
    if allowed is None:
        raise ValueError(f"Unknown action '{action}'. Valid: acknowledge, complete, dismiss.")

    if row.status not in allowed:
        raise ValueError(
            f"Cannot '{action}' a decision in status '{row.status}'. "
            f"Allowed from: {sorted(allowed)}."
        )

    now = _now_utc()

    if action == "acknowledge":
        row.status          = "acknowledged"
        row.acknowledged_at = now
        row.actor_id        = actor_id

    elif action == "complete":
        row.status       = "completed"
        row.completed_at = now
        row.actor_id     = actor_id
        if resolution_note is not None:
            row.resolution_note = resolution_note
        if resolution_quality is not None:
            row.resolution_quality = resolution_quality
        if estimated_revenue_saved is not None:
            row.estimated_revenue_saved = estimated_revenue_saved

    elif action == "dismiss":
        row.status       = "dismissed"
        row.completed_at = now
        row.actor_id     = actor_id
        if resolution_note is not None:
            row.resolution_note = resolution_note

    db.commit()
    db.refresh(row)
    return _row_to_dict(row)
