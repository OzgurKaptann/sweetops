import json
import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.services.kitchen_service import get_kitchen_orders
from app.services.websocket_manager import kitchen_ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSockets"])


@router.websocket("/ws/kitchen")
async def websocket_kitchen_endpoint(
    websocket: WebSocket,
    store_id: int = 1,
    db: Session = Depends(get_db),
):
    """
    Kitchen real-time channel.

    On connect:
      1. Registers the connection.
      2. Immediately sends an `initial_state` event with all current active
         orders so the client never starts with a blank screen.

    After that, the server pushes `order_created` and `order_status_updated`
    events whenever business operations occur.

    Reconnect safety:
      Clients should reconnect on close and handle `initial_state` to
      rebuild their local state from scratch — do not assume continuity.
    """
    conn_id = await kitchen_ws_manager.connect(websocket)
    try:
        # ── Initial state sync ──────────────────────────────────────────
        try:
            orders = get_kitchen_orders(db, store_id)
            await websocket.send_text(json.dumps({
                "event": "initial_state",
                "data": {
                    "store_id": store_id,
                    "orders": orders,
                },
            }))
            logger.info("ws_initial_state_sent conn_id=%s orders=%d", conn_id, len(orders))
        except Exception as exc:
            logger.error("ws_initial_state_failed conn_id=%s err=%s", conn_id, exc)

        # ── Keep-alive loop ─────────────────────────────────────────────
        # Server → client only. receive_text() blocks until the client
        # sends a message or disconnects; we ignore any client messages.
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        kitchen_ws_manager.disconnect(websocket)
    except Exception as exc:
        logger.warning("ws_error conn_id=%s err=%s", conn_id, exc)
        kitchen_ws_manager.disconnect(websocket)
