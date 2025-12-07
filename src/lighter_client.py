import asyncio
import logging
from typing import Dict, Any, Optional, List, Union
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
    
    async def initialize(self, accounts: List[AccountConfig]):
        for account in accounts:
            try:
                config = Configuration()
                config.host = settings.lighter_base_url
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
            
            data = {
                "account_index": account_index,
                "account_name": account_name,
                "raw_data": serialized_data
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
            await asyncio.sleep(0.5)
        return results
    
    async def start_polling(self):
        self.running = True
        logger.info(f"Starting polling with interval: {settings.poll_interval}s")
        
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
