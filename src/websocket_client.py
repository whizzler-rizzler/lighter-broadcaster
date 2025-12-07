import asyncio
import logging
import json
import time
from typing import Optional, Callable, List, Any, Dict
import websockets
import lighter
from src.config import settings

logger = logging.getLogger(__name__)

class LighterWebSocketClient:
    def __init__(self):
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self._callbacks: List[Callable] = []
        self._connected = False
        self._signer_clients: Dict[str, lighter.SignerClient] = {}
        self._ws = None
    
    def set_signer_clients(self, signer_clients: Dict[str, lighter.SignerClient]):
        self._signer_clients = signer_clients
    
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
    
    def _generate_auth_token(self) -> Optional[str]:
        if not self._signer_clients:
            return None
        first_signer = next(iter(self._signer_clients.values()), None)
        if first_signer:
            try:
                expiry_timestamp = int(time.time()) + (8 * 60 * 60)
                token = first_signer.create_auth_token_with_expiry(expiry_timestamp)
                logger.info("Generated WebSocket auth token")
                return token
            except Exception as e:
                logger.error(f"Failed to generate auth token: {e}")
        return None
    
    def _get_proxy_url(self) -> Optional[str]:
        for acc in settings.accounts:
            if acc.proxy_url:
                return acc.proxy_url
        return None
    
    async def connect(self):
        retry_count = 0
        max_retries = 3
        
        while self.running:
            try:
                account_ids = [acc.account_index for acc in settings.accounts]
                ws_url = settings.lighter_ws_url
                
                auth_token = self._generate_auth_token()
                
                if auth_token:
                    logger.info(f"Connecting to WebSocket: {ws_url} with auth token for accounts: {account_ids}")
                else:
                    logger.info(f"Connecting to WebSocket: {ws_url} (no auth) for accounts: {account_ids}")
                
                extra_headers = {
                    "User-Agent": "LighterBroadcaster/1.0",
                    "Origin": "https://lighter.xyz"
                }
                if auth_token:
                    extra_headers["Authorization"] = f"Bearer {auth_token}"
                
                async with websockets.connect(
                    ws_url,
                    additional_headers=extra_headers,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5
                ) as websocket:
                    self._ws = websocket
                    self._connected = True
                    retry_count = 0
                    logger.info("WebSocket connected successfully")
                    
                    for account_id in account_ids:
                        subscribe_msg = {
                            "type": "subscribe",
                            "channel": f"account_all/{account_id}"
                        }
                        await websocket.send(json.dumps(subscribe_msg))
                        logger.info(f"Subscribed to account_all/{account_id}")
                    
                    async for message in websocket:
                        try:
                            data = json.loads(message)
                            logger.debug(f"WS received: {data}")
                            await self._notify_callbacks(data)
                        except json.JSONDecodeError:
                            logger.warning(f"Invalid JSON from WebSocket: {message}")
                        except Exception as e:
                            logger.error(f"Error processing WS message: {e}")
                
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
                self._connected = False
            except Exception as e:
                retry_count += 1
                logger.warning(f"WebSocket error (attempt {retry_count}/{max_retries}): {e}")
                self._connected = False
                
                if retry_count >= max_retries:
                    logger.warning("WebSocket unavailable after max retries. REST API polling will continue to provide data updates.")
                    self.running = False
                    return
            
            if self.running:
                wait_time = min(5 * retry_count, 30)
                logger.info(f"Reconnecting WebSocket in {wait_time} seconds...")
                await asyncio.sleep(wait_time)
    
    async def start(self):
        self.running = True
        if settings.accounts:
            self._task = asyncio.create_task(self.connect())
        else:
            logger.warning("No accounts configured, WebSocket client not started")
    
    async def stop(self):
        self.running = False
        self._connected = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
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
