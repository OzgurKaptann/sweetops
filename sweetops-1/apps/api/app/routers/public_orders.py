from fastapi import APIRouter, Depends, BackgroundTasks, Header, Request
from sqlalchemy.orm import Session
from typing import Optional

from app.core.db import get_db
from app.schemas.order import OrderCreateRequest, OrderCreatedResponse
from app.services.order_service import create_order

router = APIRouter(prefix="/public/orders", tags=["Public Orders"])


@router.post("/", response_model=OrderCreatedResponse)
def create_new_order(
    order_data: OrderCreateRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    Create a new order.

    Idempotency:
        Send header `Idempotency-Key: <uuid>` to prevent duplicate orders
        on network retry. Same key returns the existing order with HTTP 200.

    Stock:
        If any ingredient is out of stock, returns HTTP 422 with the list
        of unavailable items. No partial orders are created.
    """
    ip = request.client.host if request.client else None
    return create_order(
        db=db,
        order_data=order_data,
        background_tasks=background_tasks,
        idempotency_key=idempotency_key,
        ip_address=ip,
    )
