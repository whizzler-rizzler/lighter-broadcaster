import asyncio
import logging
import json
import time
from typing import Optional, Callable, List, Any, Dict
import aiohttp
from aiohttp_socks import ProxyConnector
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
        self._session = None
    
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
    
    def _generate_auth_token(self, account_name: str = None) -> Optional[str]:
        if not self._signer_clients:
            return None
        
        if account_name and account_name in self._signer_clients:
            signer = self._signer_clients[account_name]
        else:
            signer = next(iter(self._signer_clients.values()), None)
        
        if signer:
            try:
                token, err = signer.create_auth_token_with_expiry(
                    lighter.SignerClient.DEFAULT_10_MIN_AUTH_EXPIRY
                )
                if err:
                    logger.error(f"Auth token error for {account_name}: {err}")
                    return None
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
        max_retries = 5
        
        while self.running:
            try:
                account_ids = [acc.account_index for acc in settings.accounts]
                ws_url = settings.lighter_ws_url
                proxy_url = self._get_proxy_url()
                
                auth_token = self._generate_auth_token()
                
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Origin": "https://lighter.xyz"
                }
                if auth_token:
                    headers["Authorization"] = f"Bearer {auth_token}"
                
                if proxy_url:
                    logger.info(f"Connecting to WebSocket via proxy: {proxy_url[:30]}...")
                    connector = ProxyConnector.from_url(proxy_url)
                    self._session = aiohttp.ClientSession(connector=connector)
                else:
                    logger.info(f"Connecting to WebSocket directly (no proxy)")
                    self._session = aiohttp.ClientSession()
                
                logger.info(f"WebSocket URL: {ws_url}, accounts: {account_ids}")
                
                async with self._session.ws_connect(
                    ws_url,
                    headers=headers,
                    heartbeat=30,
                    timeout=aiohttp.ClientWSTimeout(ws_close=10)
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    retry_count = 0
                    logger.info("WebSocket connected successfully via proxy!")
                    
                    for acc in settings.accounts:
                        account_id = acc.account_index
                        auth_token = self._generate_auth_token(acc.name)
                        
                        if not auth_token:
                            logger.warning(f"No auth token for {acc.name}, skipping subscriptions")
                            continue
                        
                        positions_msg = {
                            "type": "subscribe",
                            "channel": f"account_all_positions/{account_id}",
                            "auth": auth_token
                        }
                        await ws.send_json(positions_msg)
                        logger.info(f"Subscribed to account_all_positions/{account_id}")
                        
                        orders_msg = {
                            "type": "subscribe",
                            "channel": f"account_all_orders/{account_id}",
                            "auth": auth_token
                        }
                        await ws.send_json(orders_msg)
                        logger.info(f"Subscribed to account_all_orders/{account_id}")
                        
                        trades_msg = {
                            "type": "subscribe",
                            "channel": f"account_all_trades/{account_id}",
                            "auth": auth_token
                        }
                        await ws.send_json(trades_msg)
                        logger.info(f"Subscribed to account_all_trades/{account_id}")
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                channel = data.get("channel", "")
                                msg_type = data.get("type", "")
                                if "orders" in channel or "positions" in channel or "trades" in channel:
                                    orders_count = len(data.get("orders", []))
                                    positions_count = len(data.get("positions", []))
                                    trades_count = len(data.get("trades", {}))
                                    logger.info(f"WS [{msg_type}] {channel} - orders:{orders_count} pos:{positions_count} trades:{trades_count}")
                                await self._notify_callbacks(data)
                            except json.JSONDecodeError:
                                logger.warning(f"Invalid JSON from WebSocket: {msg.data}")
                            except Exception as e:
                                logger.error(f"Error processing WS message: {e}")
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"WebSocket error: {ws.exception()}")
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.warning("WebSocket closed by server")
                            break
                
            except aiohttp.ClientError as e:
                retry_count += 1
                logger.warning(f"WebSocket connection error (attempt {retry_count}/{max_retries}): {e}")
                self._connected = False
            except Exception as e:
                retry_count += 1
                logger.warning(f"WebSocket error (attempt {retry_count}/{max_retries}): {e}")
                self._connected = False
            finally:
                if self._session and not self._session.closed:
                    await self._session.close()
                    self._session = None
            
            if retry_count >= max_retries:
                logger.warning("WebSocket unavailable after max retries. REST API polling will continue.")
                self.running = False
                return
            
            if self.running:
                wait_time = min(5 * retry_count, 30)
                logger.info(f"Reconnecting WebSocket in {wait_time} seconds...")
                await asyncio.sleep(wait_time)
    
    async def start(self):
        self.running = True
        if settings.accounts:
            proxy_url = self._get_proxy_url()
            if proxy_url:
                logger.info(f"WebSocket client starting with proxy support")
            else:
                logger.info(f"WebSocket client starting without proxy")
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
        if self._session and not self._session.closed:
            await self._session.close()
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
