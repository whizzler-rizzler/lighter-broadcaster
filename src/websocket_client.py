import asyncio
import logging
from typing import Optional, Callable, List, Any
import lighter
from src.config import settings

logger = logging.getLogger(__name__)

class LighterWebSocketClient:
    def __init__(self):
        self.ws_client: Optional[lighter.WsClient] = None
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self._callbacks: List[Callable] = []
        self._connected = False
    
    def add_callback(self, callback: Callable):
        self._callbacks.append(callback)
    
    def remove_callback(self, callback: Callable):
        if callback in self._callbacks:
            self._callbacks.remove(callback)
    
    async def _notify_callbacks(self, data: Any):
        for callback in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    def _on_account_update(self, account_id: Any, data: Any):
        logger.debug(f"Account update received for {account_id}: {data}")
        asyncio.create_task(self._notify_callbacks({"type": "account_update", "account_id": account_id, "data": data}))
    
    def _on_orderbook_update(self, orderbook_id: Any, data: Any):
        logger.debug(f"Orderbook update received for {orderbook_id}: {data}")
        asyncio.create_task(self._notify_callbacks({"type": "orderbook_update", "orderbook_id": orderbook_id, "data": data}))
    
    async def connect(self):
        while self.running:
            try:
                account_ids = [acc.account_index for acc in settings.accounts]
                
                host = settings.lighter_ws_url.replace("wss://", "").replace("/stream", "")
                
                self.ws_client = lighter.WsClient(
                    host=host,
                    path="/stream",
                    account_ids=account_ids,
                    order_book_ids=[],
                    on_account_update=self._on_account_update,
                    on_order_book_update=self._on_orderbook_update
                )
                
                logger.info(f"Starting Lighter WebSocket client for accounts: {account_ids}")
                self._connected = True
                
                await self.ws_client.run_async()
                
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                self._connected = False
            
            if self.running:
                logger.info("Reconnecting WebSocket in 5 seconds...")
                await asyncio.sleep(5)
    
    async def start(self):
        self.running = True
        if settings.accounts:
            self._task = asyncio.create_task(self.connect())
        else:
            logger.warning("No accounts configured, WebSocket client not started")
    
    async def stop(self):
        self.running = False
        self._connected = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
    
    @property
    def is_connected(self) -> bool:
        return self._connected

ws_client = LighterWebSocketClient()
