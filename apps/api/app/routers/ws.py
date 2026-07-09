import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.deps import safe_origin_label, websocket_origin_allowed
from app.core.permissions import PERM_KITCHEN_READ, role_has_permission
from app.services.auth_service import resolve_session
from app.services.kitchen_service import get_kitchen_orders
from app.services.websocket_manager import kitchen_ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSockets"])

# Application-specific WebSocket close codes.
WS_UNAUTHENTICATED = 4401  # missing / expired / revoked session
WS_FORBIDDEN = 4403        # untrusted Origin, or role lacks kitchen access


@router.websocket("/ws/kitchen")
async def websocket_kitchen_endpoint(websocket: WebSocket):
    """
    Kitchen real-time channel — part of the authorization boundary.

    Handshake (order matters — Origin is an ADDITIONAL boundary, never a
    replacement for session/permission checks):
      1. Validate the handshake Origin against the trusted staff origins
         (Cross-Site WebSocket Hijacking defence); reject with 4403.
      2. Authenticate from the HttpOnly session cookie (no query params trusted).
      3. Reject unauthenticated/expired/revoked sessions with 4401.
      4. Reject roles without kitchen:read with 4403.
      5. Register the connection under the session's store; the initial_state
         and all subsequent broadcasts contain only that store's orders.
    """
    # ── 1. Origin (CSWSH) — checked before the cookie is even read ──────────
    origin = websocket.headers.get("origin")
    if not websocket_origin_allowed(origin):
        # Log only a normalized origin label — never the cookie, token or URL.
        logger.warning("ws_origin_rejected origin=%s", safe_origin_label(origin))
        await websocket.close(code=WS_FORBIDDEN)
        return

    raw_token = websocket.cookies.get(settings.SESSION_COOKIE_NAME)
    db = SessionLocal()
    registered = False
    try:
        resolved = resolve_session(db, raw_token)
        if resolved is None:
            await websocket.close(code=WS_UNAUTHENTICATED)
            return

        _session, user, role = resolved
        if not role_has_permission(role.name, PERM_KITCHEN_READ):
            await websocket.close(code=WS_FORBIDDEN)
            return

        store_id = user.store_id
        conn_id = await kitchen_ws_manager.connect(websocket, store_id)
        registered = True

        # ── Initial state sync (this store only) ────────────────────────────
        try:
            dashboard = get_kitchen_orders(db, store_id)
            await websocket.send_text(json.dumps({
                "event": "initial_state",
                "data": {
                    "store_id": store_id,
                    "orders": dashboard["orders"],
                    "kitchen_load": dashboard["kitchen_load"],
                    "batching_suggestions": dashboard["batching_suggestions"],
                },
            }))
            logger.info(
                "ws_initial_state_sent conn_id=%s store_id=%s orders=%d",
                conn_id, store_id, len(dashboard["orders"]),
            )
        except Exception as exc:
            logger.error("ws_initial_state_failed conn_id=%s err=%s", conn_id, exc)

        # ── Keep-alive loop (server → client only) ──────────────────────────
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        if registered:
            kitchen_ws_manager.disconnect(websocket)
    except Exception as exc:
        logger.warning("ws_error err=%s", exc)
        if registered:
            kitchen_ws_manager.disconnect(websocket)
    finally:
        db.close()
