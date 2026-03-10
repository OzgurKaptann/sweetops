from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.schemas.owner_analytics import (
    KPIsResponse, TopIngredientsResponse, 
    HourlyDemandResponse, DailySalesResponse
)
from app.services import owner_analytics_service as service

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
