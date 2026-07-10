"""
Owner/manager payment analytics.

GET /owner/payment-summary — store-scoped financial summary distinguishing
ordered value from collected cash. Additive: it does NOT replace the existing
/owner/kpis gross-revenue metric, which continues to report gross ordered value.
"""
from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import require_permission
from app.core.permissions import PERM_PAYMENTS_READ
from app.schemas.payment import PaymentSummaryResponse
from app.services.auth_service import CurrentStaff
from app.services.payment_analytics_service import fetch_payment_summary

router = APIRouter(prefix="/owner", tags=["Owner Payments"])


@router.get("/payment-summary", response_model=PaymentSummaryResponse)
def payment_summary(
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_READ)),
):
    response.headers["Cache-Control"] = "no-store"
    return fetch_payment_summary(db, staff.store_id)
