from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.schemas.order import OrderCreateRequest, OrderCreatedResponse
from app.services.order_service import create_order

router = APIRouter(prefix="/public/orders", tags=["Public Orders"])

@router.post("/", response_model=OrderCreatedResponse)
def create_new_order(order_data: OrderCreateRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    order, item_count = create_order(db, order_data, background_tasks)
    return order
