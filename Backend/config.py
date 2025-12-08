import os
from typing import List, Optional
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

class AccountConfig(BaseModel):
    name: str
    account_index: int
    api_key_index: int
    private_key: str
    public_key: str
    proxy_url: Optional[str] = None

def convert_proxy_format(raw_proxy: str) -> str:
    """Convert ip:port:user:pass to http://user:pass@ip:port format"""
    if not raw_proxy:
        return None
    if raw_proxy.startswith("http://") or raw_proxy.startswith("https://"):
        return raw_proxy
    parts = raw_proxy.split(":")
    if len(parts) == 4:
        ip, port, user, password = parts
        return f"http://{user}:{password}@{ip}:{port}"
    elif len(parts) == 2:
        return f"http://{raw_proxy}"
    return raw_proxy

class Settings(BaseModel):
    lighter_base_url: str = "https://mainnet.zklighter.elliot.ai"
    lighter_ws_url: str = "wss://mainnet.zklighter.elliot.ai/stream"
    
    host: str = "0.0.0.0"
    port: int = 5000
    
    poll_interval: float = 0.5
    cache_ttl: int = 5
    
    rate_limit: str = "100/minute"
    
    accounts: List[AccountConfig] = []

def load_accounts_from_env() -> List[AccountConfig]:
    accounts = []
    env_vars = os.environ
    
    account_prefixes = set()
    for key in env_vars:
        if key.startswith("Lighter_") and "_Account_Index" in key:
            prefix = key.replace("_Account_Index", "")
            account_prefixes.add(prefix)
    
    for prefix in sorted(account_prefixes):
        account_index = env_vars.get(f"{prefix}_Account_Index")
        api_key_index = env_vars.get(f"{prefix}_API_KEY_Index")
        private_key = env_vars.get(f"{prefix}_PRIVATE")
        public_key = env_vars.get(f"{prefix}_PUBLIC")
        
        parts = prefix.split("_")
        if len(parts) >= 2:
            account_num = parts[1]
            proxy_url = None
            for key in env_vars:
                if f"Lighter_{account_num}_PROXY" in key and "_URL" in key:
                    proxy_url = env_vars.get(key)
                    break
        else:
            proxy_url = None
        
        if account_index and private_key:
            try:
                acc_idx = int(account_index)
                api_idx = int(api_key_index) if api_key_index else 2
                accounts.append(AccountConfig(
                    name=prefix,
                    account_index=acc_idx,
                    api_key_index=api_idx,
                    private_key=private_key,
                    public_key=public_key or "",
                    proxy_url=convert_proxy_format(proxy_url) if proxy_url else None
                ))
            except ValueError as e:
                print(f"Warning: Skipping account {prefix} - invalid account_index or api_key_index: {e}")
                continue
    
    return accounts

def get_settings() -> Settings:
    accounts = load_accounts_from_env()
    return Settings(
        lighter_base_url=os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
        lighter_ws_url=os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream"),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5000")),
        poll_interval=float(os.getenv("POLL_INTERVAL", "0.5")),
        cache_ttl=int(os.getenv("CACHE_TTL", "5")),
        rate_limit=os.getenv("RATE_LIMIT", "100/minute"),
        accounts=accounts
    )

settings = get_settings()
