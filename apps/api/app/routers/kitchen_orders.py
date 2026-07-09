from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import require_permission
from app.core.permissions import PERM_KITCHEN_READ, PERM_KITCHEN_ORDERS_WRITE
from app.schemas.order import KitchenDashboardResponse, StatusUpdateRequest
from app.services.auth_service import CurrentStaff
from app.services.kitchen_service import get_kitchen_orders, update_order_status

router = APIRouter(prefix="/kitchen/orders", tags=["Kitchen Orders"])


@router.get("/", response_model=KitchenDashboardResponse)
def read_kitchen_orders(
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_KITCHEN_READ)),
):
    """
    Kitchen dashboard for the authenticated staff member's store.

    The store is derived from the session — a client-supplied store_id is never
    trusted (and is not accepted).
    """
    return get_kitchen_orders(db, staff.store_id)


@router.patch("/{order_id}/status")
def patch_order_status(
    order_id: int,
    status_update: StatusUpdateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_KITCHEN_ORDERS_WRITE)),
):
    """
    Transition an order to a new status.

    Store isolation: the order must belong to the authenticated user's store,
    otherwise a non-disclosing 404 is returned (a Store-A user cannot even learn
    that a Store-B order exists). The audit actor is the authenticated user —
    X-Actor-Id is neither read nor trusted.
    """
    order = update_order_status(
        db=db,
        order_id=order_id,
        new_status=status_update.status,
        background_tasks=background_tasks,
        store_id=staff.store_id,
        actor_type="STAFF",
        actor_id=str(staff.user_id),
    )
    return {
        "order_id": order.id,
        "new_status": order.status,
        "updated_at": order.updated_at,
    }
