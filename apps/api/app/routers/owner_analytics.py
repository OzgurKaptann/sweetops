from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.deps import require_permission
from app.core.permissions import PERM_OWNER_READ, PERM_OWNER_DECISIONS_WRITE
from app.schemas.owner_analytics import (
    KPIsResponse, TopIngredientsResponse,
    HourlyDemandResponse, DailySalesResponse,
    IngredientForecastResponse,
    OwnerDecision, OwnerDecisionsResponse, DecisionActionRequest,
)
from app.services import owner_analytics_service as service
from app.services.auth_service import CurrentStaff
from app.services.decision_engine import apply_decision_action, get_owner_decisions
from app.services.inventory_guard import assert_single_operational_store
from app.services.operational_context_service import compute_operational_context, context_to_dict
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock

router = APIRouter(prefix="/owner", tags=["Owner Analytics"])


@router.get("/kpis", response_model=KPIsResponse)
def get_kpis(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    return service.fetch_kpis(db, staff.store_id)


@router.get("/top-ingredients", response_model=TopIngredientsResponse)
def get_top_ingredients(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    return service.fetch_top_ingredients(db, staff.store_id, limit=5)


@router.get("/hourly-demand", response_model=HourlyDemandResponse)
def get_hourly_demand(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    return service.fetch_hourly_demand(db, staff.store_id)


@router.get("/daily-sales", response_model=DailySalesResponse)
def get_daily_sales(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    return service.fetch_daily_sales(db, staff.store_id)


@router.get("/ingredient-forecast", response_model=IngredientForecastResponse)
def get_ingredient_forecast(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    return service.fetch_ingredient_forecast(db, staff.store_id)


@router.get("/decisions/", response_model=OwnerDecisionsResponse)
def get_decisions(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    """
    Owner decision command centre for the authenticated store.

    Order-derived signals (demand_spike, sla_risk, revenue_anomaly) are scoped
    to the store. Inventory-derived signals (stock_risk, slow_moving) rely on
    the global inventory tables and are only produced while a single operational
    store exists (fail-closed otherwise).
    """
    return get_owner_decisions(db, staff.store_id)


@router.patch("/decisions/{decision_id}", response_model=OwnerDecision)
def patch_decision(
    decision_id: str,
    body: DecisionActionRequest,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_DECISIONS_WRITE)),
):
    """
    Transition a decision through its lifecycle.

    Store isolation: the decision must belong to the authenticated store or a
    404 is returned. The lifecycle actor is the authenticated user — any
    client-supplied actor_id in the body is ignored.
    """
    try:
        return apply_decision_action(
            db,
            store_id=staff.store_id,
            decision_id=decision_id,
            action=body.action,
            actor_id=str(staff.user_id),
            resolution_note=body.resolution_note,
            resolution_quality=body.resolution_quality,
            estimated_revenue_saved=body.estimated_revenue_saved,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/operational-context")
def get_operational_context(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    """Current operational mode derived from this store's metrics."""
    ctx = compute_operational_context(db, store_id=staff.store_id)
    return context_to_dict(ctx)


@router.get("/stock-status")
def get_stock_status(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    """
    Return stock status for all ingredients with severity levels.

    Severity is judged on AVAILABLE stock (on_hand - reserved) — what the shop
    can still sell — while on-hand and reserved are reported alongside it so the
    owner can tell a physical shortage ("we are out of pistachio") apart from a
    demand shortage ("we still have pistachio, but it is all promised to open
    orders"). Those need opposite responses, so they must never be one number.

    ``stock_quantity`` is retained as an alias of on_hand_quantity so the
    existing owner-web contract does not break; new clients should read the
    explicit fields.

    Inventory is global in the current schema, so this fails closed with a
    Turkish error when more than one operational store exists.
    """
    assert_single_operational_store(db)

    stocks = db.query(
        Ingredient.id,
        Ingredient.name,
        Ingredient.category,
        Ingredient.unit,
        IngredientStock.on_hand_quantity,
        IngredientStock.reserved_quantity,
        IngredientStock.available_quantity,
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
        on_hand = float(row.on_hand_quantity or 0)
        reserved = float(row.reserved_quantity or 0)
        avail = float(row.available_quantity or 0)
        reorder = float(row.reorder_level) if row.reorder_level else 0

        if avail <= 0:
            severity = "critical"
            # Distinguish the two very different ways of having nothing to sell.
            message = (
                "Stok tükendi!" if on_hand <= 0
                else "Kalan stok bekleyen siparişler için ayrıldı"
            )
            critical_count += 1
        elif reorder > 0 and avail <= reorder:
            severity = "warning"
            message = "Stok azalıyor"
            warning_count += 1
        elif reorder > 0 and avail <= reorder * 1.5:
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
            "on_hand_quantity": on_hand,
            "reserved_quantity": reserved,
            "available_quantity": avail,
            "stock_quantity": on_hand,  # legacy alias — see docstring
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
