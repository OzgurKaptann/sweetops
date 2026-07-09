"""
WebSocket connection manager for kitchen real-time updates — store-partitioned.

Connections are grouped by store_id. A broadcast for store A is delivered ONLY
to sockets registered for store A; an order from store A can never reach a
store-B connection. The store_id is always server-derived (from the
authenticated session), never a client-supplied query parameter.

Lifecycle:
  connect(ws, store_id) — accept handshake, register under store_id.
  disconnect(ws)        — remove from its store bucket; idempotent.
  broadcast_kitchen_event(store_id, event, data) — send to that store only.

Thread / async safety:
  All mutations happen in the same async event loop; no locking needed.
Logging:
  Never logs raw session or CSRF tokens.
"""
import json
import logging
import uuid

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class KitchenWebSocketManager:
    def __init__(self) -> None:
        # store_id → { ws → connection_id }
        self._by_store: dict[int, dict[WebSocket, str]] = {}
        # reverse index ws → store_id for O(1) disconnect
        self._store_of: dict[WebSocket, int] = {}

    # ------------------------------------------------------------------
    # Introspection (used by tests / diagnostics)
    # ------------------------------------------------------------------

    def connections_for_store(self, store_id: int) -> list[WebSocket]:
        return list(self._by_store.get(store_id, {}).keys())

    @property
    def connection_count(self) -> int:
        return len(self._store_of)

    def store_connection_count(self, store_id: int) -> int:
        return len(self._by_store.get(store_id, {}))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, websocket: WebSocket, store_id: int) -> str:
        """Accept the handshake and register the connection under store_id."""
        await websocket.accept()
        conn_id = uuid.uuid4().hex[:8]
        self._by_store.setdefault(store_id, {})[websocket] = conn_id
        self._store_of[websocket] = store_id
        logger.info(
            "ws_connected conn_id=%s store_id=%s total=%d",
            conn_id, store_id, self.connection_count,
        )
        return conn_id

    def disconnect(self, websocket: WebSocket) -> None:
        """Unregister a connection. Safe to call multiple times."""
        store_id = self._store_of.pop(websocket, None)
        if store_id is None:
            return
        bucket = self._by_store.get(store_id)
        conn_id = bucket.pop(websocket, None) if bucket else None
        if bucket is not None and not bucket:
            self._by_store.pop(store_id, None)
        if conn_id is not None:
            logger.info(
                "ws_disconnected conn_id=%s store_id=%s total=%d",
                conn_id, store_id, self.connection_count,
            )

    # ------------------------------------------------------------------
    # Broadcast (partitioned)
    # ------------------------------------------------------------------

    async def broadcast_kitchen_event(self, store_id: int, event: str, data: dict) -> None:
        """
        Send an event to every connection registered for `store_id` only.

        Dead connections are removed after the loop. Never raises.
        """
        bucket = self._by_store.get(store_id)
        if not bucket:
            return

        message = json.dumps({"event": event, "data": data})
        dead: list[WebSocket] = []

        for ws in list(bucket):  # snapshot to avoid mutation during iteration
            try:
                await ws.send_text(message)
            except Exception as exc:
                conn_id = bucket.get(ws, "unknown")
                logger.warning(
                    "ws_send_failed conn_id=%s store_id=%s event=%s err=%s",
                    conn_id, store_id, event, exc,
                )
                dead.append(ws)

        for ws in dead:
            self.disconnect(ws)

        if dead:
            logger.info(
                "ws_cleaned_dead store_id=%s count=%d remaining=%d",
                store_id, len(dead), self.store_connection_count(store_id),
            )


kitchen_ws_manager = KitchenWebSocketManager()
