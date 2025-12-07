import asyncio
import logging
import time
from typing import Dict, Any, Optional, List, Union
import aiohttp
import lighter
from lighter.configuration import Configuration
from src.config import AccountConfig, settings
from src.cache import cache

logger = logging.getLogger(__name__)

class LighterClient:
    def __init__(self):
        self.api_clients: Dict[str, lighter.ApiClient] = {}
        self.signer_clients: Dict[str, lighter.SignerClient] = {}
        self.account_apis: Dict[str, lighter.AccountApi] = {}
        self.running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._orders_poll_task: Optional[asyncio.Task] = None
        self._http_sessions: Dict[str, aiohttp.ClientSession] = {}
        self._account_proxies: Dict[str, Optional[str]] = {}
        self.last_update_times: Dict[int, float] = {}
        self.last_orders_update: Dict[int, float] = {}
        self._cached_orders: Dict[int, List[Dict[str, Any]]] = {}
    
    async def _get_http_session_for_account(self, account_name: str) -> aiohttp.ClientSession:
        if account_name not in self._http_sessions or self._http_sessions[account_name].closed:
            proxy = self._account_proxies.get(account_name)
            connector = aiohttp.TCPConnector(limit=10)
            self._http_sessions[account_name] = aiohttp.ClientSession(connector=connector)
        return self._http_sessions[account_name]
    
    async def fetch_active_orders(self, account_name: str, account_index: int, market_id: int) -> List[Dict[str, Any]]:
        try:
            session = await self._get_http_session_for_account(account_name)
            url = f"{settings.lighter_base_url}/api/v1/accountActiveOrders"
            params = {"account_index": account_index, "market_id": market_id}
            
            proxy = self._account_proxies.get(account_name)
            
            async with session.get(url, params=params, proxy=proxy) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("orders", [])
                elif resp.status == 429:
                    logger.warning(f"Rate limited (429) for account {account_name} market {market_id}")
                    return []
                elif resp.status != 400:
                    logger.warning(f"Active orders request failed for market {market_id}: {resp.status}")
                return []
        except Exception as e:
            logger.error(f"Error fetching active orders for {account_index}: {e}")
            return []
    
    async def fetch_all_active_orders(self, account_name: str, account_index: int, position_markets: List[int] = None) -> List[Dict[str, Any]]:
        all_orders = []
        main_markets = {1, 2, 3}
        
        if position_markets:
            markets_to_check = main_markets.union(set(position_markets))
        else:
            markets_to_check = main_markets
        
        for market_id in sorted(markets_to_check):
            orders = await self.fetch_active_orders(account_name, account_index, market_id)
            all_orders.extend(orders)
            await asyncio.sleep(0.15)
        
        self._cached_orders[account_index] = all_orders
        self.last_orders_update[account_index] = time.time()
        return all_orders
    
    async def initialize(self, accounts: List[AccountConfig]):
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
        try:
            account_api = self.account_apis.get(account_name)
            if not account_api:
                return None
            
            account_data = await account_api.account(by="index", value=str(account_index))
            
            serialized_data = self._serialize_account_data(account_data)
            
            active_orders = self._cached_orders.get(account_index, [])
            
            current_time = time.time()
            self.last_update_times[account_index] = current_time
            
            data = {
                "account_index": account_index,
                "account_name": account_name,
                "raw_data": serialized_data,
                "active_orders": active_orders,
                "last_update": current_time
            }
            
            await cache.set(f"account:{account_index}", data)
            return data
            
        except Exception as e:
            logger.error(f"Error fetching account {account_index}: {e}")
            return None
    
    async def fetch_all_accounts(self) -> Dict[str, Any]:
        results = {}
        for account in settings.accounts:
            data = await self.fetch_account_data(account.name, account.account_index)
            if data:
                results[str(account.account_index)] = data
            await asyncio.sleep(0.3)
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
    
    async def poll_active_orders(self):
        logger.info("Starting active orders polling (every 2s)")
        while self.running:
            try:
                for account in settings.accounts:
                    if not self.running:
                        break
                    position_markets = self._get_position_markets(account.account_index)
                    await self.fetch_all_active_orders(account.name, account.account_index, position_markets)
                    await asyncio.sleep(0.3)
                
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Orders polling error: {e}")
                await asyncio.sleep(2)
    
    async def start_polling(self):
        self.running = True
        logger.info(f"Starting account polling with interval: {settings.poll_interval}s")
        
        self._orders_poll_task = asyncio.create_task(self.poll_active_orders())
        
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
        if self._orders_poll_task:
            self._orders_poll_task.cancel()
            try:
                await self._orders_poll_task
            except asyncio.CancelledError:
                pass
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

lighter_client = LighterClient()
