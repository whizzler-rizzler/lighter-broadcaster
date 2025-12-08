import asyncio
import json
import logging
from typing import Set, Dict, Any
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)
        logger.info(f"Client connected. Total connections: {len(self.active_connections)}")
    
    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            self.active_connections.discard(websocket)
        logger.info(f"Client disconnected. Total connections: {len(self.active_connections)}")
    
    async def broadcast(self, data: Dict[str, Any]):
        if not self.active_connections:
            return
        
        message = json.dumps(data)
        disconnected = set()
        
        async with self._lock:
            for connection in self.active_connections:
                try:
                    await connection.send_text(message)
                except Exception as e:
                    logger.warning(f"Failed to send to client: {e}")
                    disconnected.add(connection)
        
        for conn in disconnected:
            await self.disconnect(conn)
    
    async def send_to_client(self, websocket: WebSocket, data: Dict[str, Any]):
        try:
            await websocket.send_text(json.dumps(data))
        except Exception as e:
            logger.warning(f"Failed to send to client: {e}")
    
    @property
    def connection_count(self) -> int:
        return len(self.active_connections)

manager = ConnectionManager()
