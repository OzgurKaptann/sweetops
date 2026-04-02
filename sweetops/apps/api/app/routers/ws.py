from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.services.websocket_manager import kitchen_ws_manager

router = APIRouter(tags=["WebSockets"])

@router.websocket("/ws/kitchen")
async def websocket_kitchen_endpoint(websocket: WebSocket):
    await kitchen_ws_manager.connect(websocket)
    try:
        while True:
            # Client'dan gelen mesajları dinle. (Şu an tek yönlü server->client kullanıyoruz)
            # Ama bağlantının açık kalması ve ping/pong için receive_text block'ta bekler.
            _ = await websocket.receive_text()
    except WebSocketDisconnect:
        kitchen_ws_manager.disconnect(websocket)
    except Exception:
        kitchen_ws_manager.disconnect(websocket)
