import asyncio
import logging
import json
import time
from typing import Optional, Callable, List, Any, Dict
import aiohttp
from aiohttp_socks import ProxyConnector
import lighter
from Backend.config import settings, AccountConfig
from Backend.error_collector import error_collector

logger = logging.getLogger(__name__)

PING_INTERVAL = 30
PONG_TIMEOUT = 60
HEALTH_CHECK_INTERVAL = 10

RETRY_PHASE_1_INTERVAL = 60
RETRY_PHASE_1_MAX_ATTEMPTS = 5
RETRY_PHASE_2_INTERVAL = 300

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
        self._ping_task: Optional[asyncio.Task] = None
        self._last_pong_time: float = 0
        self._last_message_time: float = 0
        self._reconnect_count: int = 0
        self._total_messages: int = 0
        self._retry_phase: int = 1
        self._phase_attempts: int = 0
        self._last_successful_connect: float = 0
        self._connection_start_time: float = 0
    
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
    
    async def _ping_loop(self):
        """Send ping every PING_INTERVAL seconds and check for pong timeout"""
        while self.running and self._connected:
            try:
                await asyncio.sleep(PING_INTERVAL)
                
                if not self._ws or self._ws.closed:
                    logger.warning(f"[{self.account.name}] Ping: WS closed, triggering reconnect")
                    self._connected = False
                    break
                
                time_since_last_activity = time.time() - max(self._last_pong_time, self._last_message_time)
                if time_since_last_activity > PONG_TIMEOUT:
                    logger.warning(f"[{self.account.name}] No activity for {time_since_last_activity:.0f}s, triggering reconnect")
                    self._connected = False
                    if self._ws and not self._ws.closed:
                        await self._ws.close()
                    break
                
                await self._ws.ping()
                logger.debug(f"[{self.account.name}] Ping sent")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[{self.account.name}] Ping error: {e}")
                self._connected = False
                break
    
    def _handle_pong(self):
        """Called when pong is received"""
        self._last_pong_time = time.time()
        logger.debug(f"[{self.account.name}] Pong received")
    
    def _get_retry_interval(self) -> float:
        """Calculate retry interval based on current phase"""
        if self._retry_phase == 1:
            return RETRY_PHASE_1_INTERVAL
        else:
            return RETRY_PHASE_2_INTERVAL
    
    def _advance_retry_phase(self):
        """Move to next retry phase after max attempts"""
        self._phase_attempts += 1
        if self._retry_phase == 1 and self._phase_attempts >= RETRY_PHASE_1_MAX_ATTEMPTS:
            self._retry_phase = 2
            self._phase_attempts = 0
            logger.info(f"[{self.account.name}] Switching to phase 2 retry (every {RETRY_PHASE_2_INTERVAL}s)")
    
    def _reset_retry_state(self):
        """Reset retry state after successful connection"""
        self._retry_phase = 1
        self._phase_attempts = 0
    
    def get_health_status(self) -> Dict[str, Any]:
        """Return health status of this connection"""
        now = time.time()
        uptime = now - self._connection_start_time if self._connected and self._connection_start_time > 0 else 0
        return {
            "account_id": self.account.account_index,
            "account_name": self.account.name,
            "connected": self._connected,
            "last_message_age": now - self._last_message_time if self._last_message_time > 0 else -1,
            "last_pong_age": now - self._last_pong_time if self._last_pong_time > 0 else -1,
            "reconnect_count": self._reconnect_count,
            "total_messages": self._total_messages,
            "has_proxy": bool(self.account.proxy_url),
            "retry_phase": self._retry_phase,
            "phase_attempts": self._phase_attempts,
            "uptime_seconds": round(uptime, 1) if uptime > 0 else 0,
            "last_successful_connect": self._last_successful_connect
        }
    
    async def connect(self):
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
                    timeout=aiohttp.ClientWSTimeout(ws_close=10)
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._last_pong_time = time.time()
                    self._last_message_time = time.time()
                    self._connection_start_time = time.time()
                    self._last_successful_connect = time.time()
                    self._reset_retry_state()
                    logger.info(f"[{self.account.name}] WebSocket connected! (reconnects: {self._reconnect_count})")
                    
                    self._ping_task = asyncio.create_task(self._ping_loop())
                    
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
                            self._last_message_time = time.time()
                            self._total_messages += 1
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
                        elif msg.type == aiohttp.WSMsgType.PONG:
                            self._handle_pong()
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"[{self.account.name}] WebSocket error: {ws.exception()}")
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.warning(f"[{self.account.name}] WebSocket closed by server")
                            break
                    
                    if self._ping_task:
                        self._ping_task.cancel()
                        try:
                            await self._ping_task
                        except asyncio.CancelledError:
                            pass
                
            except aiohttp.ClientError as e:
                self._reconnect_count += 1
                self._advance_retry_phase()
                error_msg = str(e)
                error_type = "429" if "429" in error_msg else "connection"
                error_code = 429 if "429" in error_msg else None
                error_collector.add_error(self.account.account_index, self.account.name, error_type, error_msg[:100], "websocket", error_code)
                logger.warning(f"[{self.account.name}] WS connection error (phase {self._retry_phase}, attempt {self._phase_attempts}): {e}")
                self._connected = False
            except Exception as e:
                self._reconnect_count += 1
                self._advance_retry_phase()
                error_msg = str(e)
                error_type = "429" if "429" in error_msg else "exception"
                error_code = 429 if "429" in error_msg else None
                error_collector.add_error(self.account.account_index, self.account.name, error_type, error_msg[:100], "websocket", error_code)
                logger.warning(f"[{self.account.name}] WS error (phase {self._retry_phase}, attempt {self._phase_attempts}): {e}")
                self._connected = False
            finally:
                if self._ping_task:
                    self._ping_task.cancel()
                    try:
                        await self._ping_task
                    except asyncio.CancelledError:
                        pass
                    self._ping_task = None
                if self._session and not self._session.closed:
                    await self._session.close()
                    self._session = None
            
            if self.running:
                wait_time = self._get_retry_interval()
                logger.info(f"[{self.account.name}] Reconnecting WS in {wait_time}s (phase {self._retry_phase}, attempt {self._phase_attempts})...")
                await asyncio.sleep(wait_time)
    
    async def start(self):
        self.running = True
        self._task = asyncio.create_task(self.connect())
    
    async def stop(self):
        self.running = False
        self._connected = False
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
            self._ping_task = None
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
        self._start_time: float = 0
    
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
        self._start_time = time.time()
        
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
    
    def get_all_health_status(self) -> Dict[str, Any]:
        """Get health status for all connections"""
        connections_health = []
        connected_count = 0
        total_messages = 0
        total_reconnects = 0
        
        for conn in self._connections.values():
            health = conn.get_health_status()
            connections_health.append(health)
            if health["connected"]:
                connected_count += 1
            total_messages += health["total_messages"]
            total_reconnects += health["reconnect_count"]
        
        now = time.time()
        uptime = now - self._start_time if self._start_time > 0 else 0
        
        return {
            "type": "websocket",
            "total_connections": len(self._connections),
            "connected_count": connected_count,
            "disconnected_count": len(self._connections) - connected_count,
            "total_messages_received": total_messages,
            "total_reconnect_attempts": total_reconnects,
            "uptime_seconds": round(uptime, 1),
            "connections": connections_health
        }
    
    async def force_reconnect(self, account_id: int) -> bool:
        """Force reconnect a specific account connection"""
        conn = self._connections.get(account_id)
        if not conn:
            logger.warning(f"No connection found for account {account_id}")
            return False
        
        logger.info(f"Force reconnecting account {account_id}...")
        await conn.stop()
        conn.running = True
        conn._reset_retry_state()
        conn._task = asyncio.create_task(conn.connect())
        return True
    
    async def force_reconnect_all(self) -> int:
        """Force reconnect all connections"""
        logger.info("Force reconnecting all WebSocket connections...")
        count = 0
        for acc_id in list(self._connections.keys()):
            if await self.force_reconnect(acc_id):
                count += 1
        return count

ws_client = LighterWebSocketClient()
