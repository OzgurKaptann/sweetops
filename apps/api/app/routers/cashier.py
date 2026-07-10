"""
Cashier / payment-settlement API.

All responses are ``Cache-Control: no-store``. The store is always derived from
the authenticated session — never from a query string or request body. Read
routes require ``payments:read``; collection routes require
``payments:collect``; refund routes require ``payments:refund``. Every
state-changing route additionally enforces trusted-origin + CSRF (via
``require_permission``) and an ``Idempotency-Key`` header.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import HTTPException
from sqlalchemy.orm import Session

from app.core import messages
from app.core.db import get_db
from app.core.deps import require_permission
from app.core.permissions import (
    PERM_PAYMENTS_COLLECT,
    PERM_PAYMENTS_READ,
    PERM_PAYMENTS_REFUND,
)
from app.schemas.payment import (
    OpenTablesResponse,
    OrderDetailResponse,
    OrderPaymentRequest,
    RecentTransactionsResponse,
    RefundCreateRequest,
    RefundReceipt,
    SettlementCreateRequest,
    SettlementReceipt,
    TableBillResponse,
)
from app.services import cashier_query_service as query
from app.services import payment_service
from app.services.auth_service import CurrentStaff

router = APIRouter(prefix="/cashier", tags=["Cashier"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _idem_key(request: Request) -> str | None:
    return request.headers.get("Idempotency-Key")


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "not_found", "message": messages.PAY_NOT_FOUND})


# ── Reads ─────────────────────────────────────────────────────────────────────

@router.get("/tables/open", response_model=OpenTablesResponse)
def open_tables(
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_READ)),
):
    _no_store(response)
    return query.list_open_tables(db, staff.store_id)


@router.get("/orders/search", response_model=OrderDetailResponse)
def search_orders(
    response: Response,
    q: str,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_READ)),
):
    """Search by staff-facing order code (SIP-000123) or plain id."""
    _no_store(response)
    order_id = payment_service.parse_order_code(q)
    if order_id is None:
        raise _not_found()
    detail = query.search_order(db, staff.store_id, order_id)
    if detail is None:
        raise _not_found()
    return detail


@router.get("/orders/{order_id}", response_model=OrderDetailResponse)
def order_detail(
    order_id: int,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_READ)),
):
    _no_store(response)
    detail = query.get_order_detail(db, staff.store_id, order_id)
    if detail is None:
        raise _not_found()
    return detail


@router.get("/tables/{table_id}/bill", response_model=TableBillResponse)
def table_bill(
    table_id: int,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_READ)),
):
    _no_store(response)
    bill = query.get_table_bill(db, staff.store_id, table_id)
    if bill is None:
        raise _not_found()
    return bill


@router.get("/settlements/{settlement_id}", response_model=SettlementReceipt)
def settlement_receipt(
    settlement_id: int,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_READ)),
):
    _no_store(response)
    row = query.get_settlement_receipt_row(db, staff.store_id, settlement_id)
    if row is None:
        raise _not_found()
    return payment_service.build_settlement_receipt(db, row)


@router.get("/recent-transactions", response_model=RecentTransactionsResponse)
def recent(
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_READ)),
):
    _no_store(response)
    return query.recent_transactions(db, staff.store_id)


# ── Collection ────────────────────────────────────────────────────────────────

@router.post("/settlements", response_model=SettlementReceipt)
def create_settlement(
    body: SettlementCreateRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_COLLECT)),
):
    """Settle the exact outstanding balance of the selected table orders."""
    _no_store(response)
    return payment_service.collect_settlement(
        db, staff, body,
        idempotency_key=_idem_key(request),
        ip_address=_client_ip(request),
    )


@router.post("/orders/{order_id}/payments", response_model=SettlementReceipt)
def create_order_payment(
    order_id: int,
    body: OrderPaymentRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_COLLECT)),
):
    """Collect full or partial payment for a single order."""
    _no_store(response)
    return payment_service.collect_order_payment(
        db, staff, order_id, body,
        idempotency_key=_idem_key(request),
        ip_address=_client_ip(request),
    )


# ── Refund ────────────────────────────────────────────────────────────────────

@router.post("/allocations/{allocation_id}/refunds", response_model=RefundReceipt)
def create_refund(
    allocation_id: int,
    body: RefundCreateRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_PAYMENTS_REFUND)),
):
    """Refund previously-collected money for one allocation (MANAGER/OWNER only)."""
    _no_store(response)
    return payment_service.refund_allocation(
        db, staff, allocation_id, body,
        idempotency_key=_idem_key(request),
        ip_address=_client_ip(request),
    )
