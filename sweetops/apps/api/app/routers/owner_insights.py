from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.services import owner_insights_service as insights

router = APIRouter(prefix="/owner/insights", tags=["Owner Insights"])


@router.get("/critical-alerts")
def get_critical_alerts(db: Session = Depends(get_db)):
    """Low stock alerts with estimated lost revenue."""
    return insights.fetch_critical_alerts(db)


@router.get("/prep-time")
def get_prep_time(db: Session = Depends(get_db)):
    """Average waffle prep time tracking."""
    return insights.fetch_prep_time_stats(db)


@router.get("/trending-ingredients")
def get_trending_ingredients(db: Session = Depends(get_db)):
    """Week-over-week ingredient usage trends."""
    return insights.fetch_trending_ingredients(db)


@router.get("/popular-combos")
def get_popular_combos(db: Session = Depends(get_db)):
    """Most popular ingredient combinations."""
    return insights.fetch_popular_combinations(db, limit=5)


@router.get("/value-summary")
def get_value_summary(db: Session = Depends(get_db)):
    """One-screen value proof for the owner."""
    return insights.fetch_value_summary(db)
