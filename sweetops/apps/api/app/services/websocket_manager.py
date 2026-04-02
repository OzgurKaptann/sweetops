from fastapi import WebSocket
from typing import List
import json
import logging

logger = logging.getLogger(__name__)

class KitchenWebSocketManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"Kitchen WS Client connected. Total active: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"Kitchen WS Client disconnected. Total active: {len(self.active_connections)}")

    async def broadcast_kitchen_event(self, event: str, data: dict):
        payload = {
            "event": event,
            "data": data
        }
        message = json.dumps(payload)
        
        # Olası kopuk bağlantıları temizlemek için
        failed_connections = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                logger.warning(f"Failed to send WS message: {e}")
                failed_connections.append(connection)
                
        # Başarısızları listeden sil
        for connection in failed_connections:
            self.disconnect(connection)

kitchen_ws_manager = KitchenWebSocketManager()
