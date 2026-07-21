"""
Kitchen preparation timing — read-only operational visibility.

  GET /kitchen/timing/orders   — per-order live timing for the active board
  GET /kitchen/timing/summary  — live counts + today's completed prep averages

Both are DERIVED from the existing order lifecycle (orders.created_at +
order_status_events); see app.services.kitchen_timing_service. Neither mutates
anything, and nothing here touches payment, refund, or inventory state.

Authorization
-------------
Store is derived from the authenticated staff session — a client-supplied
store_id is never read. ``kitchen:read`` gates access, so KITCHEN, MANAGER and
OWNER staff may read, exactly as for the kitchen dashboard; CASHIER (no
kitchen:read) is rejected. Cross-store reads are impossible: the query is always
filtered to ``staff.store_id``.

Caching
-------
Operational timing is per-second-fresh and identity-scoped, so every response is
``Cache-Control: no-store`` — the same policy the kitchen dashboard and cashier
surfaces already use.
"""
from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import require_permission
from app.core.permissions import PERM_KITCHEN_READ
from app.schemas.kitchen_timing import ActiveTimingResponse, TimingSummaryResponse
from app.services.auth_service import CurrentStaff
from app.services import kitchen_timing_service

router = APIRouter(prefix="/kitchen/timing", tags=["Kitchen Timing"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/orders", response_model=ActiveTimingResponse)
def read_timing_orders(
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_KITCHEN_READ)),
) -> ActiveTimingResponse:
    """
    Live timing for the store's active orders (NEW / IN_PREP / READY), most
    delayed first, with a live summary strip. The store comes from the session.
    """
    _no_store(response)
    return kitchen_timing_service.get_active_order_timing(db, staff.store_id)


@router.get("/summary", response_model=TimingSummaryResponse)
def read_timing_summary(
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_KITCHEN_READ)),
) -> TimingSummaryResponse:
    """
    Store timing summary: live active/waiting/in-prep/ready/delayed counts plus
    today's completed prep and time-to-ready averages (and p95 prep). Completed
    figures are ``null`` when no order has completed prep today.
    """
    _no_store(response)
    return kitchen_timing_service.get_timing_summary(db, staff.store_id)
