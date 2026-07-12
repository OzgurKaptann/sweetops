from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.deps import require_permission
from app.core.permissions import PERM_OWNER_READ
from app.services import owner_insights_service as insights
from app.services.auth_service import CurrentStaff

router = APIRouter(prefix="/owner/insights", tags=["Owner Insights"])


@router.get("/critical-alerts")
def get_critical_alerts(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    """
    Low-stock alerts with estimated lost revenue, for the caller's own store.

    Both inputs are now the same branch's: demand comes from this store's orders
    and the runway it is divided into comes from this store's shelves. It no
    longer fails closed when a second branch exists.
    """
    return insights.fetch_critical_alerts(db, staff.store_id)


@router.get("/prep-time")
def get_prep_time(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    """Average waffle prep time tracking for this store."""
    return insights.fetch_prep_time_stats(db, staff.store_id)


@router.get("/trending-ingredients")
def get_trending_ingredients(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    """Week-over-week ingredient usage trends for this store."""
    return insights.fetch_trending_ingredients(db, staff.store_id)


@router.get("/popular-combos")
def get_popular_combos(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    """Most popular ingredient combinations for this store."""
    return insights.fetch_popular_combinations(db, staff.store_id, limit=5)


@router.get("/value-summary")
def get_value_summary(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    """One-screen value proof for the owner, scoped to their own store."""
    return insights.fetch_value_summary(db, staff.store_id)
