import asyncio
import logging
import time
from collections import deque
from typing import Dict, Any, Optional, List, Union
import aiohttp
import lighter
from lighter.configuration import Configuration
from Backend.config import AccountConfig, settings
from Backend.cache import cache
from Backend.latency import latency_tracker
from Backend.supabase_client import supabase_client
from Backend.error_collector import error_collector

logger = logging.getLogger(__name__)

MAX_REQUEST_HISTORY = 300

SNAPSHOT_INTERVAL = 60

RETRY_PHASE_1_INTERVAL = 60
RETRY_PHASE_1_MAX_ATTEMPTS = 5
RETRY_PHASE_2_INTERVAL = 300
REQUEST_TIMEOUT = 30

class AccountRestConnection:
    """Tracks REST API connection state and retry logic for a single account"""
    
    def __init__(self, account_name: str, account_index: int):
        self.account_name = account_name
        self.account_index = account_index
        self._connected = True
        self._last_successful_request: float = 0
        self._last_failed_request: float = 0
        self._total_requests: int = 0
        self._successful_requests: int = 0
        self._failed_requests: int = 0
        self._retry_phase: int = 1
        self._phase_attempts: int = 0
        self._connection_start_time: float = time.time()
        self._consecutive_failures: int = 0
        self._last_error: str = ""
        self._request_timestamps: deque = deque(maxlen=MAX_REQUEST_HISTORY)
    
    def record_success(self):
        """Record a successful request"""
        now = time.time()
        self._total_requests += 1
        self._successful_requests += 1
        self._last_successful_request = now
        self._connected = True
        self._consecutive_failures = 0
        self._request_timestamps.append(now)
        self._reset_retry_state()
    
    def record_failure(self, error: str = ""):
        """Record a failed request"""
        now = time.time()
        self._total_requests += 1
        self._failed_requests += 1
        self._last_failed_request = now
        self._consecutive_failures += 1
        self._last_error = error
        
        if self._consecutive_failures >= 3:
            self._connected = False
            self._advance_retry_phase()
    
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
            logger.info(f"[{self.account_name}] REST switching to phase 2 retry")
    
    def _reset_retry_state(self):
        """Reset retry state after successful connection"""
        self._retry_phase = 1
        self._phase_attempts = 0
    
    def should_skip_request(self) -> bool:
        """Check if we should skip this request based on retry interval"""
        if self._connected:
            return False
        
        time_since_failure = time.time() - self._last_failed_request
        retry_interval = self._get_retry_interval()
        
        return time_since_failure < retry_interval
    
    def get_health_status(self) -> Dict[str, Any]:
        """Return health status of this connection"""
        now = time.time()
        uptime = now - self._connection_start_time if self._connection_start_time > 0 else 0
        return {
            "account_id": self.account_index,
            "account_name": self.account_name,
            "connected": self._connected,
            "last_success_age": now - self._last_successful_request if self._last_successful_request > 0 else -1,
            "last_failure_age": now - self._last_failed_request if self._last_failed_request > 0 else -1,
            "total_requests": self._total_requests,
            "successful_requests": self._successful_requests,
            "failed_requests": self._failed_requests,
            "success_rate": round(self._successful_requests / self._total_requests * 100, 1) if self._total_requests > 0 else 100,
            "retry_phase": self._retry_phase,
            "phase_attempts": self._phase_attempts,
            "consecutive_failures": self._consecutive_failures,
            "uptime_seconds": round(uptime, 1),
            "last_error": self._last_error[:100] if self._last_error else "",
            "requests_per_minute": self.get_requests_per_minute()
        }
    
    def get_requests_per_minute(self) -> int:
        """Count requests in the last 60 seconds"""
        now = time.time()
        cutoff = now - 60
        return sum(1 for ts in self._request_timestamps if ts > cutoff)


class LighterClient:
    def __init__(self):
        self.api_clients: Dict[str, lighter.ApiClient] = {}
        self.signer_clients: Dict[str, lighter.SignerClient] = {}
        self.account_apis: Dict[str, lighter.AccountApi] = {}
        self.running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._http_sessions: Dict[str, aiohttp.ClientSession] = {}
        self._account_proxies: Dict[str, Optional[str]] = {}
        self.last_update_times: Dict[int, float] = {}
        self.last_orders_update: Dict[int, float] = {}
        self._cached_orders: Dict[int, List[Dict[str, Any]]] = {}
        self._last_snapshot_times: Dict[int, float] = {}
        self._account_exchanges: Dict[str, str] = {}
        self._rest_connections: Dict[int, AccountRestConnection] = {}
        self._start_time: float = 0
    
    def _get_exchange_for_account(self, account_name: str) -> str:
        if account_name in self._account_exchanges:
            return self._account_exchanges[account_name]
        for account in settings.accounts:
            if account.name == account_name:
                return account.exchange
        return "lighter"
    
    async def _get_http_session_for_account(self, account_name: str) -> aiohttp.ClientSession:
        if account_name not in self._http_sessions or self._http_sessions[account_name].closed:
            proxy = self._account_proxies.get(account_name)
            connector = aiohttp.TCPConnector(limit=10)
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            self._http_sessions[account_name] = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self._http_sessions[account_name]
    
    def _get_auth_token(self, account_name: str) -> Optional[str]:
        signer = self.signer_clients.get(account_name)
        if not signer:
            return None
        try:
            auth_token, err = signer.create_auth_token_with_expiry(
                lighter.SignerClient.DEFAULT_10_MIN_AUTH_EXPIRY
            )
            if err:
                logger.error(f"Auth token error for {account_name}: {err}")
                return None
            return auth_token
        except Exception as e:
            logger.error(f"Failed to create auth token for {account_name}: {e}")
            return None
    
    def _get_rest_connection(self, account_name: str, account_index: int) -> AccountRestConnection:
        """Get or create REST connection tracker for account"""
        if account_index not in self._rest_connections:
            self._rest_connections[account_index] = AccountRestConnection(account_name, account_index)
        return self._rest_connections[account_index]
    
    async def fetch_active_orders(self, account_name: str, account_index: int, market_id: int) -> List[Dict[str, Any]]:
        rest_conn = self._get_rest_connection(account_name, account_index)
        
        if rest_conn.should_skip_request():
            logger.debug(f"[{account_name}] Skipping orders request (in retry backoff)")
            return self._cached_orders.get(account_index, [])
        
        try:
            auth_token = self._get_auth_token(account_name)
            if not auth_token:
                return []
            
            session = await self._get_http_session_for_account(account_name)
            url = f"{settings.lighter_base_url}/api/v1/accountActiveOrders"
            params = {"account_index": account_index, "market_id": market_id}
            headers = {"Authorization": auth_token}
            
            proxy = self._account_proxies.get(account_name)
            
            async with session.get(url, params=params, headers=headers, proxy=proxy) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    orders = data.get("orders", [])
                    rest_conn.record_success()
                    if orders:
                        logger.debug(f"Fetched {len(orders)} orders for {account_name} market {market_id}")
                    return orders
                elif resp.status == 429:
                    rest_conn.record_failure("Rate limited (429)")
                    error_collector.add_error(account_index, account_name, "429", "Too Many Requests", "rest", 429)
                    logger.warning(f"Rate limited (429) for account {account_name} market {market_id}")
                    return []
                else:
                    body = await resp.text()
                    rest_conn.record_failure(f"HTTP {resp.status}")
                    error_collector.add_error(account_index, account_name, f"HTTP_{resp.status}", body[:100], "rest", resp.status)
                    logger.warning(f"Active orders request failed for {account_name} market {market_id}: {resp.status} - {body[:200]}")
                return []
        except asyncio.TimeoutError:
            rest_conn.record_failure("Timeout")
            error_collector.add_error(account_index, account_name, "timeout", "Request timeout", "rest")
            logger.error(f"Timeout fetching active orders for {account_index}")
            return []
        except Exception as e:
            rest_conn.record_failure(str(e))
            error_collector.add_error(account_index, account_name, "exception", str(e)[:100], "rest")
            logger.error(f"Error fetching active orders for {account_index}: {e}")
            return []
    
    async def fetch_all_active_orders(self, account_name: str, account_index: int, position_markets: List[int] = None) -> List[Dict[str, Any]]:
        """Fetch active orders for all markets in parallel"""
        if not position_markets:
            self._cached_orders[account_index] = []
            self.last_orders_update[account_index] = time.time()
            return []
        
        tasks = [
            self.fetch_active_orders(account_name, account_index, market_id)
            for market_id in position_markets
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_orders = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Error fetching orders for account {account_index}: {result}")
            elif result:
                all_orders.extend(result)
        
        self._cached_orders[account_index] = all_orders
        self.last_orders_update[account_index] = time.time()
        return all_orders
    
    async def initialize(self, accounts: List[AccountConfig]):
        self._start_time = time.time()
        for account in accounts:
            try:
                config = Configuration()
                config.host = settings.lighter_base_url
                
                self._account_proxies[account.name] = account.proxy_url
                
                if account.proxy_url:
                    config.proxy = account.proxy_url
                    logger.info(f"Using proxy for {account.name}: {account.proxy_url[:40]}...")
                
                api_client = lighter.ApiClient(configuration=config)
                self.api_clients[account.name] = api_client
                self.account_apis[account.name] = lighter.AccountApi(api_client)
                
                if account.private_key:
                    api_private_keys = {account.api_key_index: account.private_key}
                    signer = lighter.SignerClient(
                        url=settings.lighter_base_url,
                        account_index=account.account_index,
                        api_private_keys=api_private_keys
                    )
                    if account.proxy_url:
                        signer.api_client.configuration.proxy = account.proxy_url
                        if hasattr(signer.api_client, 'rest_client'):
                            signer.api_client.rest_client.proxy = account.proxy_url
                    self.signer_clients[account.name] = signer
                
                self._rest_connections[account.account_index] = AccountRestConnection(account.name, account.account_index)
                
                logger.info(f"Initialized client for account: {account.name} (index: {account.account_index})")
            except Exception as e:
                logger.error(f"Failed to initialize client for {account.name}: {e}")
    
    def _serialize_account_data(self, account_data: Any) -> Union[Dict[str, Any], List[Any], Any]:
        if hasattr(account_data, 'to_dict'):
            return account_data.to_dict()
        elif hasattr(account_data, '__dict__'):
            return {k: self._serialize_account_data(v) for k, v in account_data.__dict__.items() if not k.startswith('_')}
        elif isinstance(account_data, list):
            return [self._serialize_account_data(item) for item in account_data]
        elif isinstance(account_data, dict):
            return {k: self._serialize_account_data(v) for k, v in account_data.items()}
        else:
            return account_data
    
    async def fetch_account_data(self, account_name: str, account_index: int) -> Optional[Dict[str, Any]]:
        rest_conn = self._get_rest_connection(account_name, account_index)
        
        if rest_conn.should_skip_request():
            logger.debug(f"[{account_name}] Skipping account request (in retry backoff)")
            cached = await cache.get(f"account:{account_index}")
            return cached.get("data", cached) if cached else None
        
        try:
            account_api = self.account_apis.get(account_name)
            if not account_api:
                return None
            
            start_time = time.time()
            account_data = await account_api.account(by="index", value=str(account_index))
            latency_ms = (time.time() - start_time) * 1000
            latency_tracker.record_rest_poll(latency_ms)
            
            rest_conn.record_success()
            
            serialized_data = self._serialize_account_data(account_data)
            
            active_orders = self._cached_orders.get(account_index, [])
            
            current_time = time.time()
            self.last_update_times[account_index] = current_time
            
            latency_tracker.update_balance_time()
            latency_tracker.update_positions_time()
            
            data = {
                "account_index": account_index,
                "account_name": account_name,
                "raw_data": serialized_data,
                "active_orders": active_orders,
                "last_update": current_time
            }
            
            await cache.set(f"account:{account_index}", data)
            
            last_snapshot = self._last_snapshot_times.get(account_index, 0)
            if supabase_client.is_initialized and (current_time - last_snapshot) >= SNAPSHOT_INTERVAL:
                exchange = self._get_exchange_for_account(account_name)
                asyncio.create_task(supabase_client.save_account_snapshot(account_index, data, exchange))
                
                positions = []
                if isinstance(serialized_data, dict):
                    acc_list = serialized_data.get("accounts", [])
                    if acc_list:
                        positions = acc_list[0].get("positions", [])
                if positions:
                    asyncio.create_task(supabase_client.save_positions(account_index, positions, exchange))
                
                if active_orders:
                    asyncio.create_task(supabase_client.save_orders(account_index, active_orders, exchange))
                
                self._last_snapshot_times[account_index] = current_time
            
            return data
            
        except asyncio.TimeoutError:
            rest_conn.record_failure("Timeout")
            error_collector.add_error(account_index, account_name, "timeout", "Account fetch timeout", "rest")
            logger.error(f"Timeout fetching account {account_index}")
            return None
        except Exception as e:
            rest_conn.record_failure(str(e))
            error_msg = str(e)
            error_type = "429" if "429" in error_msg else "exception"
            error_code = 429 if "429" in error_msg else None
            error_collector.add_error(account_index, account_name, error_type, error_msg[:100], "rest", error_code)
            logger.error(f"Error fetching account {account_index}: {e}")
            return None
    
    async def fetch_all_accounts(self) -> Dict[str, Any]:
        """Fetch all accounts in parallel using asyncio.gather"""
        async def fetch_single(account):
            return await self.fetch_account_data(account.name, account.account_index)
        
        tasks = [fetch_single(account) for account in settings.accounts]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        
        results = {}
        for account, data in zip(settings.accounts, results_list):
            if isinstance(data, Exception):
                logger.error(f"Error fetching account {account.account_index}: {data}")
            elif data:
                results[str(account.account_index)] = data
        return results
    
    def _get_position_markets(self, account_index: int) -> List[int]:
        try:
            entry = cache._cache.get(f"account:{account_index}")
            if entry is None:
                return []
            cached_data = entry.data
            if cached_data is None:
                return []
            raw_data = cached_data.get("raw_data", {})
            if isinstance(raw_data, dict):
                acc_list = raw_data.get("accounts", [])
                if acc_list:
                    positions = acc_list[0].get("positions", [])
                    return [int(pos.get("market_id", 0)) for pos in positions if float(pos.get("position", 0) or pos.get("signed_size", 0) or 0) != 0]
        except Exception:
            pass
        return []
    
    async def start_polling(self):
        self.running = True
        logger.info(f"Starting account polling with interval: {settings.poll_interval}s")
        
        while self.running:
            try:
                await self.fetch_all_accounts()
                await asyncio.sleep(settings.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(1)
    
    async def stop_polling(self):
        self.running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
    
    async def close(self):
        await self.stop_polling()
        for session in self._http_sessions.values():
            if session and not session.closed:
                await session.close()
        self._http_sessions.clear()
        for client in self.api_clients.values():
            try:
                if hasattr(client, 'close'):
                    result = client.close()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:
                pass
        self.api_clients.clear()
        self.signer_clients.clear()
        self.account_apis.clear()
    
    def get_all_health_status(self) -> Dict[str, Any]:
        """Get health status for all REST connections (same format as WebSocket)"""
        connections_health = []
        connected_count = 0
        total_requests = 0
        total_failures = 0
        
        for conn in self._rest_connections.values():
            health = conn.get_health_status()
            connections_health.append(health)
            if health["connected"]:
                connected_count += 1
            total_requests += health["total_requests"]
            total_failures += health["failed_requests"]
        
        now = time.time()
        uptime = now - self._start_time if self._start_time > 0 else 0
        
        return {
            "type": "rest_api",
            "total_connections": len(self._rest_connections),
            "connected_count": connected_count,
            "disconnected_count": len(self._rest_connections) - connected_count,
            "total_requests": total_requests,
            "total_failures": total_failures,
            "success_rate": round((total_requests - total_failures) / total_requests * 100, 1) if total_requests > 0 else 100,
            "uptime_seconds": round(uptime, 1),
            "polling": self.running,
            "poll_interval": settings.poll_interval,
            "connections": connections_health
        }
    
    async def force_reconnect(self, account_index: int) -> bool:
        """Force reset retry state for a specific account"""
        if account_index in self._rest_connections:
            conn = self._rest_connections[account_index]
            conn._reset_retry_state()
            conn._connected = True
            conn._consecutive_failures = 0
            logger.info(f"Force reset REST connection for account {account_index}")
            return True
        return False
    
    async def force_reconnect_all(self) -> int:
        """Force reset retry state for all accounts"""
        count = 0
        for account_index in list(self._rest_connections.keys()):
            if await self.force_reconnect(account_index):
                count += 1
        logger.info(f"Force reset {count} REST connections")
        return count

lighter_client = LighterClient()
