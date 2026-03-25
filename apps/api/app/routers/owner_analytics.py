from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.schemas.owner_analytics import (
    KPIsResponse, TopIngredientsResponse, 
    HourlyDemandResponse, DailySalesResponse
)
from app.services import owner_analytics_service as service
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
