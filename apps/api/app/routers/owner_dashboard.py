"""
Owner operational dashboard.

GET /owner/operational-dashboard — one read-only, store-scoped snapshot that lets
an owner/manager answer "how is today going?" at a glance: active orders, money
collected/refunded today, kitchen tempo, open issues, cashier shifts, critical
stock, and a deterministic attention list.

It is protected (OWNER/MANAGER via ``owner:read``), store-scoped from the session
(no client-supplied store_id), read-only, and ``Cache-Control: no-store`` — an
operational snapshot must never be served stale from a cache. It aggregates the
existing source-of-truth systems and mutates nothing.
"""
from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import require_permission
from app.core.permissions import PERM_OWNER_READ
from app.schemas.owner_dashboard import OperationalDashboardResponse
from app.services.auth_service import CurrentStaff
from app.services.operational_dashboard_service import fetch_operational_dashboard

router = APIRouter(prefix="/owner", tags=["Owner Dashboard"])


@router.get("/operational-dashboard", response_model=OperationalDashboardResponse)
def operational_dashboard(
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_OWNER_READ)),
):
    response.headers["Cache-Control"] = "no-store"
    return fetch_operational_dashboard(db, staff.store_id)
