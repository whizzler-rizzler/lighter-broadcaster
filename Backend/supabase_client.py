import os
import asyncio
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
from supabase import create_client, Client

logger = logging.getLogger(__name__)

class SupabaseClient:
    def __init__(self):
        self._client: Optional[Client] = None
        self._initialized = False
    
    def initialize(self) -> bool:
        url = os.getenv("Supabase_Url")
        key = os.getenv("Supabase_service_role")
        
        if not url or not key:
            logger.warning("Supabase credentials not found, persistence disabled")
            return False
        
        try:
            self._client = create_client(url, key)
            self._initialized = True
            logger.info("Supabase client initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Supabase: {e}")
            return False
    
    @property
    def is_initialized(self) -> bool:
        return self._initialized and self._client is not None
    
    def _insert_sync(self, table: str, data: Any):
        return self._client.table(table).insert(data).execute()
    
    def _select_sync(self, table: str, account_index: int, limit: int):
        return self._client.table(table)\
            .select("*")\
            .eq("account_index", account_index)\
            .order("timestamp", desc=True)\
            .limit(limit)\
            .execute()
    
    async def save_account_snapshot(self, account_index: int, data: Dict[str, Any]) -> bool:
        if not self.is_initialized:
            return False
        
        try:
            raw_data = data.get("raw_data", {})
            accounts = raw_data.get("accounts", [])
            account_info = accounts[0] if accounts else {}
            
            snapshot = {
                "account_index": account_index,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "equity": account_info.get("equity"),
                "margin": account_info.get("margin"),
                "available_balance": account_info.get("available_balance"),
                "pnl": account_info.get("pnl"),
                "positions_count": len(account_info.get("positions", [])),
                "orders_count": len(data.get("active_orders", [])),
                "raw_data": data
            }
            
            await asyncio.to_thread(self._insert_sync, "account_snapshots", snapshot)
            logger.debug(f"Saved snapshot for account {account_index}")
            return True
        except Exception as e:
            logger.error(f"Failed to save account snapshot: {e}")
            return False
    
    async def save_positions(self, account_index: int, positions: List[Dict]) -> bool:
        if not self.is_initialized or not positions:
            return False
        
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            records = []
            
            for pos in positions:
                records.append({
                    "account_index": account_index,
                    "timestamp": timestamp,
                    "market": pos.get("market_name") or pos.get("market"),
                    "side": pos.get("side"),
                    "size": pos.get("size"),
                    "entry_price": pos.get("entry_price"),
                    "mark_price": pos.get("mark_price"),
                    "unrealized_pnl": pos.get("unrealized_pnl"),
                    "raw_data": pos
                })
            
            if records:
                await asyncio.to_thread(self._insert_sync, "positions", records)
                logger.debug(f"Saved {len(records)} positions for account {account_index}")
            return True
        except Exception as e:
            logger.error(f"Failed to save positions: {e}")
            return False
    
    async def save_orders(self, account_index: int, orders: List[Dict]) -> bool:
        if not self.is_initialized or not orders:
            return False
        
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            records = []
            
            for order in orders:
                records.append({
                    "account_index": account_index,
                    "timestamp": timestamp,
                    "order_id": order.get("id") or order.get("order_id"),
                    "market": order.get("market_name") or order.get("market"),
                    "side": order.get("side"),
                    "order_type": order.get("type") or order.get("order_type"),
                    "price": order.get("price"),
                    "size": order.get("size"),
                    "filled": order.get("filled"),
                    "status": order.get("status"),
                    "raw_data": order
                })
            
            if records:
                await asyncio.to_thread(self._insert_sync, "orders", records)
                logger.debug(f"Saved {len(records)} orders for account {account_index}")
            return True
        except Exception as e:
            logger.error(f"Failed to save orders: {e}")
            return False
    
    async def save_trade(self, account_index: int, trade: Dict) -> bool:
        if not self.is_initialized:
            return False
        
        try:
            record = {
                "account_index": account_index,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "trade_id": trade.get("id") or trade.get("trade_id"),
                "market": trade.get("market_name") or trade.get("market"),
                "side": trade.get("side"),
                "price": trade.get("price"),
                "size": trade.get("size"),
                "fee": trade.get("fee"),
                "raw_data": trade
            }
            
            await asyncio.to_thread(self._insert_sync, "trades", record)
            logger.debug(f"Saved trade for account {account_index}")
            return True
        except Exception as e:
            logger.error(f"Failed to save trade: {e}")
            return False
    
    async def get_account_history(self, account_index: int, limit: int = 100) -> List[Dict]:
        if not self.is_initialized:
            return []
        
        try:
            result = await asyncio.to_thread(self._select_sync, "account_snapshots", account_index, limit)
            return result.data if result.data else []
        except Exception as e:
            logger.error(f"Failed to get account history: {e}")
            return []
    
    async def get_recent_trades(self, account_index: int, limit: int = 50) -> List[Dict]:
        if not self.is_initialized:
            return []
        
        try:
            result = await asyncio.to_thread(self._select_sync, "trades", account_index, limit)
            return result.data if result.data else []
        except Exception as e:
            logger.error(f"Failed to get recent trades: {e}")
            return []


supabase_client = SupabaseClient()
