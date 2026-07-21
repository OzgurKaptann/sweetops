"""
Order issue & controlled refund API.

An order issue coordinates the existing payment/inventory/shift systems; it never
bypasses them. The store is always derived from the authenticated session — never
from a query string or request body. Reads require ``payments:read``; recording an
issue and resolving it require ``payments:collect`` (so a cashier can act), and a
refunding resolution additionally requires ``payments:refund`` (enforced inside the
service — a cashier records the problem, a supervisor approves the money back).

Every state-changing route enforces trusted-origin + CSRF (via ``require_permission``)
and an ``Idempotency-Key`` header. All responses are ``Cache-Control: no-store``.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import HTTPException
from sqlalchemy.orm import Session

from app.core import messages
from app.core.db import get_db
from app.core.deps import require_permission
from app.core.permissions import PERM_PAYMENTS_COLLECT, PERM_PAYMENTS_READ
from app.schemas.order_issue import (
    IssueCreateRequest,
    IssueResolveRequest,
    OrderIssueListResponse,
    OrderIssueResponse,
)
from app.services import order_issue_service as issues
from app.services.auth_service import CurrentStaff

router = APIRouter(tags=["Order Issues"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _idem_key(request: Request) -> str | None:
    return request.headers.get("Idempotency-Key")


def _parse_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": "not_found", "message": messages.ISSUE_NOT_FOUND}
    )


# ── Create / resolve ──────────────────────────────────────────────────────────

@router.post("/orders/{order_id}/issues", response_model=OrderIssueResponse)
def create_order_issue(
    order_id: int,
    body: IssueCreateRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_COLLECT)),
):
    """Record a problem against an order (moves no money and no stock)."""
    _no_store(response)
    return issues.create_issue(
        db, staff, order_id, body,
        idempotency_key=_idem_key(request),
        ip_address=_client_ip(request),
    )


@router.post("/order-issues/{issue_id}/resolve", response_model=OrderIssueResponse)
def resolve_order_issue(
    issue_id: int,
    body: IssueResolveRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_COLLECT)),
):
    """Resolve an OPEN issue. A FULL/PARTIAL refund additionally needs payments:refund."""
    _no_store(response)
    return issues.resolve_issue(
        db, staff, issue_id, body,
        idempotency_key=_idem_key(request),
        ip_address=_client_ip(request),
    )


# ── Reads ─────────────────────────────────────────────────────────────────────

@router.get("/orders/{order_id}/issues", response_model=OrderIssueListResponse)
def list_order_issues(
    order_id: int,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_READ)),
):
    """Every issue raised against one order, store-scoped."""
    _no_store(response)
    return issues.list_order_issues(db, staff, order_id)


@router.get("/order-issues", response_model=OrderIssueListResponse)
def list_issues(
    response: Response,
    status: str | None = None,
    issue_type: str | None = None,
    order_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_READ)),
):
    """Store-scoped issue history with optional filters."""
    _no_store(response)
    return issues.list_issues(
        db, staff,
        status=status,
        issue_type=issue_type,
        order_id=order_id,
        date_from=_parse_dt(date_from),
        date_to=_parse_dt(date_to),
        limit=limit,
    )


@router.get("/order-issues/{issue_id}", response_model=OrderIssueResponse)
def order_issue_detail(
    issue_id: int,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_READ)),
):
    """Store-scoped detail for one issue."""
    _no_store(response)
    detail = issues.get_issue(db, staff, issue_id)
    if detail is None:
        raise _not_found()
    return detail
