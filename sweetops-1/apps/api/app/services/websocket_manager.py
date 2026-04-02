"""
WebSocket connection manager for kitchen real-time updates.

Lifecycle:
  connect()    — accept handshake, register with a UUID connection ID.
  disconnect() — remove from registry; idempotent (safe to call multiple times).
  broadcast()  — send to all registered connections; dead connections are
                 removed automatically, broadcast never raises.

Reconnect safety:
  Each call to connect() is independent. Clients that disconnect and
  reconnect receive a fresh connection ID and must re-fetch current state
  via the initial_state event sent by the WS endpoint.

Thread / async safety:
  All mutations happen in the same async event loop; no locking needed.
"""
import json
import logging
import uuid
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class KitchenWebSocketManager:
    def __init__(self) -> None:
        # ws → connection_id mapping; connection_id used only for logging
        self._connections: dict[WebSocket, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def active_connections(self) -> list[WebSocket]:
        """Ordered list of currently registered WebSocket connections."""
        return list(self._connections.keys())

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> str:
        """
        Accept the WebSocket handshake and register the connection.
        Returns the assigned connection_id (for logging / diagnostics).
        """
        await websocket.accept()
        conn_id = uuid.uuid4().hex[:8]
        self._connections[websocket] = conn_id
        logger.info("ws_connected conn_id=%s total=%d", conn_id, self.connection_count)
        return conn_id

    def disconnect(self, websocket: WebSocket) -> None:
        """
        Unregister a connection. Safe to call multiple times for the same socket.
        """
        conn_id = self._connections.pop(websocket, None)
        if conn_id is not None:
            logger.info("ws_disconnected conn_id=%s total=%d", conn_id, self.connection_count)

    async def broadcast_kitchen_event(self, event: str, data: dict) -> None:
        """
        Send an event payload to every registered client.

        Dead connections (send raises) are collected and removed after the
        broadcast loop so iteration is never mutated mid-flight.
        The method never raises regardless of how many connections fail.
        """
        if not self._connections:
            return

        message = json.dumps({"event": event, "data": data})
        dead: list[WebSocket] = []

        for ws in list(self._connections):  # snapshot keys to avoid mutation during iteration
            try:
                await ws.send_text(message)
            except Exception as exc:
                conn_id = self._connections.get(ws, "unknown")
                logger.warning("ws_send_failed conn_id=%s event=%s err=%s", conn_id, event, exc)
                dead.append(ws)

        for ws in dead:
            self.disconnect(ws)

        if dead:
            logger.info("ws_cleaned_dead count=%d remaining=%d", len(dead), self.connection_count)


kitchen_ws_manager = KitchenWebSocketManager()
