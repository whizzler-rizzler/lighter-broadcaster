import asyncio
import time
from typing import Dict, Any, Optional
from dataclasses import dataclass, field

@dataclass
class CacheEntry:
    data: Any
    timestamp: float
    ttl: int

class DataCache:
    def __init__(self, default_ttl: int = 5):
        self._cache: Dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()
        self.default_ttl = default_ttl
    
    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if time.time() - entry.timestamp > entry.ttl:
                del self._cache[key]
                return None
            return entry.data
    
    async def set(self, key: str, data: Any, ttl: Optional[int] = None):
        async with self._lock:
            self._cache[key] = CacheEntry(
                data=data,
                timestamp=time.time(),
                ttl=ttl or self.default_ttl
            )
    
    async def get_all(self) -> Dict[str, Any]:
        async with self._lock:
            current_time = time.time()
            result = {}
            expired_keys = []
            
            for key, entry in self._cache.items():
                if current_time - entry.timestamp > entry.ttl:
                    expired_keys.append(key)
                else:
                    result[key] = {
                        "data": entry.data,
                        "age": current_time - entry.timestamp,
                        "ttl": entry.ttl
                    }
            
            for key in expired_keys:
                del self._cache[key]
            
            return result
    
    async def clear(self):
        async with self._lock:
            self._cache.clear()
    
    async def get_stats(self) -> Dict[str, Any]:
        async with self._lock:
            current_time = time.time()
            valid_entries = 0
            expired_entries = 0
            
            for entry in self._cache.values():
                if current_time - entry.timestamp <= entry.ttl:
                    valid_entries += 1
                else:
                    expired_entries += 1
            
            return {
                "total_entries": len(self._cache),
                "valid_entries": valid_entries,
                "expired_entries": expired_entries
            }

cache = DataCache()
