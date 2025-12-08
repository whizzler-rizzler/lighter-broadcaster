import asyncio
import logging
import json
import time
from typing import Optional, Callable, List, Any, Dict
import aiohttp
from aiohttp_socks import ProxyConnector
import lighter
from src.config import settings, AccountConfig

logger = logging.getLogger(__name__)

class AccountWebSocketConnection:
    """Single WebSocket connection for one account through its proxy"""
    
    def __init__(self, account: AccountConfig, signer: Optional[lighter.SignerClient] = None):
        self.account = account
        self.signer = signer
        self.running = False
        self._ws = None
        self._session = None
        self._connected = False
        self._callbacks: List[Callable] = []
        self._task: Optional[asyncio.Task] = None
    
    def add_callback(self, callback: Callable):
        if callback not in self._callbacks:
            self._callbacks.append(callback)
    
    async def _notify_callbacks(self, data: Any):
        for callback in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception as e:
                logger.error(f"Callback error for account {self.account.account_index}: {e}")
    
    def _generate_auth_token(self) -> Optional[str]:
        if not self.signer:
            return None
        try:
            token, err = self.signer.create_auth_token_with_expiry(
                lighter.SignerClient.DEFAULT_10_MIN_AUTH_EXPIRY
            )
            if err:
                logger.error(f"Auth token error for {self.account.name}: {err}")
                return None
            return token
        except Exception as e:
            logger.error(f"Failed to generate auth token for {self.account.name}: {e}")
            return None
    
    async def connect(self):
        retry_count = 0
        max_retries = 5
        account_id = self.account.account_index
        proxy_url = self.account.proxy_url
        
        while self.running:
            try:
                ws_url = settings.lighter_ws_url
                auth_token = self._generate_auth_token()
                
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Origin": "https://lighter.xyz"
                }
                if auth_token:
                    headers["Authorization"] = f"Bearer {auth_token}"
                
                if proxy_url:
                    logger.info(f"[{self.account.name}] Connecting WS via proxy: {proxy_url[:30]}...")
                    connector = ProxyConnector.from_url(proxy_url)
                    self._session = aiohttp.ClientSession(connector=connector)
                else:
                    logger.info(f"[{self.account.name}] Connecting WS directly (no proxy)")
                    self._session = aiohttp.ClientSession()
                
                async with self._session.ws_connect(
                    ws_url,
                    headers=headers,
                    heartbeat=30,
                    timeout=aiohttp.ClientWSTimeout(ws_close=10)
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    retry_count = 0
                    logger.info(f"[{self.account.name}] WebSocket connected!")
                    
                    auth_token = self._generate_auth_token()
                    if not auth_token:
                        logger.warning(f"[{self.account.name}] No auth token, skipping subscriptions")
                    else:
                        subscriptions = [
                            {"type": "subscribe", "channel": f"account_all_positions/{account_id}", "auth": auth_token},
                            {"type": "subscribe", "channel": f"account_all_orders/{account_id}", "auth": auth_token},
                            {"type": "subscribe", "channel": f"account_all_trades/{account_id}", "auth": auth_token}
                        ]
                        for sub in subscriptions:
                            await ws.send_json(sub)
                        logger.info(f"[{self.account.name}] Subscribed to positions, orders, trades")
                    
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
                                    logger.debug(f"[{self.account.name}] WS [{msg_type}] {channel} - orders:{orders_count} pos:{positions_count} trades:{trades_count}")
                                await self._notify_callbacks(data)
                            except json.JSONDecodeError:
                                logger.warning(f"[{self.account.name}] Invalid JSON from WS")
                            except Exception as e:
                                logger.error(f"[{self.account.name}] Error processing WS message: {e}")
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"[{self.account.name}] WebSocket error: {ws.exception()}")
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.warning(f"[{self.account.name}] WebSocket closed by server")
                            break
                
            except aiohttp.ClientError as e:
                retry_count += 1
                logger.warning(f"[{self.account.name}] WS connection error (attempt {retry_count}/{max_retries}): {e}")
                self._connected = False
            except Exception as e:
                retry_count += 1
                logger.warning(f"[{self.account.name}] WS error (attempt {retry_count}/{max_retries}): {e}")
                self._connected = False
            finally:
                if self._session and not self._session.closed:
                    await self._session.close()
                    self._session = None
            
            if retry_count >= max_retries:
                logger.warning(f"[{self.account.name}] WS unavailable after max retries. REST polling continues.")
                self.running = False
                return
            
            if self.running:
                wait_time = min(5 * retry_count, 30)
                logger.info(f"[{self.account.name}] Reconnecting WS in {wait_time}s...")
                await asyncio.sleep(wait_time)
    
    async def start(self):
        self.running = True
        self._task = asyncio.create_task(self.connect())
    
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


class LighterWebSocketClient:
    """Manager for multiple parallel WebSocket connections (one per account)"""
    
    def __init__(self):
        self.running = False
        self._connections: Dict[int, AccountWebSocketConnection] = {}
        self._callbacks: List[Callable] = []
        self._signer_clients: Dict[str, lighter.SignerClient] = {}
    
    def set_signer_clients(self, signer_clients: Dict[str, lighter.SignerClient]):
        self._signer_clients = signer_clients
    
    def add_callback(self, callback: Callable):
        self._callbacks.append(callback)
        for conn in self._connections.values():
            conn.add_callback(callback)
    
    def remove_callback(self, callback: Callable):
        if callback in self._callbacks:
            self._callbacks.remove(callback)
        for conn in self._connections.values():
            if callback in conn._callbacks:
                conn._callbacks.remove(callback)
    
    async def start(self):
        """Start all WebSocket connections in parallel"""
        self.running = True
        
        if not settings.accounts:
            logger.warning("No accounts configured, WebSocket client not started")
            return
        
        logger.info(f"Starting {len(settings.accounts)} parallel WebSocket connections...")
        
        start_tasks = []
        for account in settings.accounts:
            signer = self._signer_clients.get(account.name)
            conn = AccountWebSocketConnection(account, signer)
            
            for callback in self._callbacks:
                conn.add_callback(callback)
            
            self._connections[account.account_index] = conn
            start_tasks.append(conn.start())
        
        await asyncio.gather(*start_tasks, return_exceptions=True)
        logger.info(f"All {len(start_tasks)} WebSocket connections started in parallel")
    
    async def stop(self):
        self.running = False
        
        stop_tasks = [conn.stop() for conn in self._connections.values()]
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)
        
        self._connections.clear()
    
    @property
    def is_connected(self) -> bool:
        return any(conn.is_connected for conn in self._connections.values())
    
    def get_connection_status(self) -> Dict[int, bool]:
        return {acc_id: conn.is_connected for acc_id, conn in self._connections.items()}

ws_client = LighterWebSocketClient()
