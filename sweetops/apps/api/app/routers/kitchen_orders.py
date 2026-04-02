from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List
from app.core.db import get_db
from app.schemas.order import OrderListResponse, StatusUpdateRequest
from app.services.kitchen_service import get_kitchen_orders, update_order_status

router = APIRouter(prefix="/kitchen/orders", tags=["Kitchen Orders"])

@router.get("/", response_model=List[OrderListResponse])
def read_kitchen_orders(store_id: int = 1, db: Session = Depends(get_db)):
    # store_id defaults to 1 for MVP testing
    return get_kitchen_orders(db, store_id)

@router.patch("/{order_id}/status")
def patch_order_status(order_id: int, status_update: StatusUpdateRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    order = update_order_status(db, order_id, status_update.status, background_tasks)
    return {
        "order_id": order.id,
        "new_status": order.status
    }
