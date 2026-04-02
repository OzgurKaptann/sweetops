from fastapi import APIRouter, Depends, BackgroundTasks, Header
from sqlalchemy.orm import Session
from typing import List, Optional

from app.core.db import get_db
from app.schemas.order import OrderListResponse, StatusUpdateRequest
from app.services.kitchen_service import get_kitchen_orders, update_order_status

router = APIRouter(prefix="/kitchen/orders", tags=["Kitchen Orders"])


@router.get("/", response_model=List[OrderListResponse])
def read_kitchen_orders(store_id: int = 1, db: Session = Depends(get_db)):
    return get_kitchen_orders(db, store_id)


@router.patch("/{order_id}/status")
def patch_order_status(
    order_id: int,
    status_update: StatusUpdateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    x_actor_id: Optional[str] = Header(None, alias="X-Actor-Id"),
):
    """
    Transition an order to a new status.

    State machine:
        NEW → IN_PREP | CANCELLED
        IN_PREP → READY | CANCELLED
        READY → DELIVERED

    Undo:
        IN_PREP → NEW and READY → IN_PREP are allowed within 60 seconds
        of the forward transition. After 60s, returns HTTP 410.

    Errors:
        409 — invalid transition or terminal state
        410 — undo window expired
        404 — order not found
    """
    order = update_order_status(
        db=db,
        order_id=order_id,
        new_status=status_update.status,
        background_tasks=background_tasks,
        actor_type="STAFF",
        actor_id=x_actor_id,
    )
    return {
        "order_id": order.id,
        "new_status": order.status,
        "updated_at": order.updated_at,
    }
