from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.db import get_db
from fastapi import HTTPException

from app.schemas.owner_analytics import (
    KPIsResponse, TopIngredientsResponse,
    HourlyDemandResponse, DailySalesResponse,
    OwnerDecision, OwnerDecisionsResponse, DecisionActionRequest,
)
from app.services import owner_analytics_service as service
from app.services.decision_engine import apply_decision_action, get_owner_decisions
from app.services.operational_context_service import compute_operational_context, context_to_dict
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock

router = APIRouter(prefix="/owner", tags=["Owner Analytics"])

@router.get("/kpis", response_model=KPIsResponse)
def get_kpis(db: Session = Depends(get_db)):
    return service.fetch_kpis(db)

@router.get("/top-ingredients", response_model=TopIngredientsResponse)
def get_top_ingredients(db: Session = Depends(get_db)):
    return service.fetch_top_ingredients(db, limit=5)

@router.get("/hourly-demand", response_model=HourlyDemandResponse)
def get_hourly_demand(db: Session = Depends(get_db)):
    return service.fetch_hourly_demand(db)

@router.get("/daily-sales", response_model=DailySalesResponse)
def get_daily_sales(db: Session = Depends(get_db)):
    return service.fetch_daily_sales(db)

@router.get("/ingredient-forecast", response_model=service.IngredientForecastResponse)
def get_ingredient_forecast(db: Session = Depends(get_db)):
    return service.fetch_ingredient_forecast(db)

@router.get("/decisions/", response_model=OwnerDecisionsResponse)
def get_decisions(db: Session = Depends(get_db)):
    """
    Owner decision command centre.

    Returns actionable signals across five categories:
      stock_risk      — velocity-based stockout prediction
      demand_spike    — last-1h order rate vs 23h baseline
      slow_moving     — ingredients with stock but no recent demand
      sla_risk        — orders breaching SLA thresholds
      revenue_anomaly — hourly revenue vs baseline

    Results are sorted high → medium → low severity.
    All computations use raw transactional data (no dbt dependency).
    """
    return get_owner_decisions(db)


@router.patch("/decisions/{decision_id}", response_model=OwnerDecision)
def patch_decision(
    decision_id: str,
    body: DecisionActionRequest,
    db: Session = Depends(get_db),
):
    """
    Transition a decision through its lifecycle.

    Actions:
      acknowledge — pending → acknowledged  (owner has seen it)
      complete    — pending | acknowledged → completed  (action taken)
      dismiss     — pending | acknowledged → dismissed  (owner chose to ignore)

    Errors:
      404 — decision not found
      409 — invalid transition for current status
    """
    try:
        return apply_decision_action(
            db,
            decision_id=decision_id,
            action=body.action,
            actor_id=body.actor_id,
            resolution_note=body.resolution_note,
            resolution_quality=body.resolution_quality,
            estimated_revenue_saved=body.estimated_revenue_saved,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/operational-context")
def get_operational_context(db: Session = Depends(get_db)):
    """
    Current operational mode derived from today's metrics.

    Mode hierarchy (highest priority wins):
      sla_critical      — sla_breach_rate > 35%: kitchen severely overloaded
      high_kitchen_load — sla_breach_rate > 20% or avg_prep > 9min: reduce order complexity
      boost_combos      — combo_usage_rate < 30% or upsell_acceptance < 15%: increase combo visibility
      normal            — all metrics within expected thresholds

    Downstream effects (applied automatically):
      boost_combos:      menu ranking promotes combo ingredients (combo_boost=1.6×)
                         upsell shows full 3 suggestions
      high_kitchen_load: upsell reduced to 1 suggestion; no combo ranking boost
      sla_critical:      upsell reduced to 1 suggestion; no combo ranking boost

    Consumed by:
      - GET /public/menu/   (menu ranking adapts)
      - GET /public/menu/upsell  (max_suggestions adapts)
      - Owner dashboard MetricAttentionBanner (shows the reason and suggested action)

    Sample response:
    {
      "mode": "boost_combos",
      "reasons": ["Combo usage rate is 24% (threshold: 30%). Increasing combo ingredient visibility."],
      "combo_boost": 1.6,
      "max_upsell_suggestions": 3,
      "computed_at": "2026-04-02T10:30:00+00:00",
      "metrics_date": "2026-04-02",
      "metric_values": { "combo_usage_rate": 0.24, "sla_breach_rate": 0.08, ... },
      "thresholds": { ... }
    }
    """
    ctx = compute_operational_context(db)
    return context_to_dict(ctx)


@router.get("/stock-status")
def get_stock_status(db: Session = Depends(get_db)):
    """Return stock status for all ingredients with severity levels."""
    stocks = db.query(
        Ingredient.id,
        Ingredient.name,
        Ingredient.category,
        Ingredient.unit,
        IngredientStock.stock_quantity,
        IngredientStock.reorder_level,
    ).join(
        IngredientStock, IngredientStock.ingredient_id == Ingredient.id
    ).filter(
        Ingredient.is_active == True
    ).all()

    items = []
    critical_count = 0
    warning_count = 0

    for row in stocks:
        stock_qty = float(row.stock_quantity) if row.stock_quantity else 0
        reorder = float(row.reorder_level) if row.reorder_level else 0

        if stock_qty <= 0:
            severity = "critical"
            message = "Stok tükendi!"
            critical_count += 1
        elif reorder > 0 and stock_qty <= reorder:
            severity = "warning"
            message = "Stok azalıyor"
            warning_count += 1
        elif reorder > 0 and stock_qty <= reorder * 1.5:
            severity = "low"
            message = "Stok düşük"
        else:
            severity = "ok"
            message = "Stok yeterli"

        items.append({
            "ingredient_id": row.id,
            "ingredient_name": row.name,
            "category": row.category,
            "unit": row.unit,
            "stock_quantity": stock_qty,
            "reorder_level": reorder,
            "severity": severity,
            "message": message,
        })

    # Sort: critical first, then warning, then low, then ok
    severity_order = {"critical": 0, "warning": 1, "low": 2, "ok": 3}
    items.sort(key=lambda x: (severity_order.get(x["severity"], 3), x["ingredient_name"]))

    return {
        "total": len(items),
        "critical_count": critical_count,
        "warning_count": warning_count,
        "items": items,
    }
