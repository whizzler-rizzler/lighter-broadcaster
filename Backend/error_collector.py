import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional


@dataclass
class ErrorEntry:
    timestamp: float
    account_index: int
    account_name: str
    error_type: str
    error_code: Optional[int]
    message: str
    source: str


class ErrorCollector:
    def __init__(self, max_errors: int = 100):
        self._errors: deque[ErrorEntry] = deque(maxlen=max_errors)
        self._error_counts: Dict[str, int] = {}
        self._start_time: float = time.time()
    
    def add_error(
        self,
        account_index: int,
        account_name: str,
        error_type: str,
        message: str,
        source: str = "rest",
        error_code: Optional[int] = None
    ):
        entry = ErrorEntry(
            timestamp=time.time(),
            account_index=account_index,
            account_name=account_name,
            error_type=error_type,
            error_code=error_code,
            message=message[:200],
            source=source
        )
        self._errors.append(entry)
        
        key = f"{source}:{error_type}"
        self._error_counts[key] = self._error_counts.get(key, 0) + 1
    
    def get_recent_errors(self, limit: int = 50, source: Optional[str] = None) -> List[Dict[str, Any]]:
        errors = list(self._errors)
        if source:
            errors = [e for e in errors if e.source == source]
        
        errors = sorted(errors, key=lambda e: e.timestamp, reverse=True)[:limit]
        
        now = time.time()
        result = []
        for e in errors:
            d = asdict(e)
            d["age_seconds"] = round(now - e.timestamp, 1)
            d["time_str"] = time.strftime("%H:%M:%S", time.localtime(e.timestamp))
            result.append(d)
        return result
    
    def get_error_summary(self) -> Dict[str, Any]:
        now = time.time()
        last_5min = [e for e in self._errors if now - e.timestamp < 300]
        last_1min = [e for e in self._errors if now - e.timestamp < 60]
        
        error_by_account: Dict[int, int] = {}
        for e in last_5min:
            error_by_account[e.account_index] = error_by_account.get(e.account_index, 0) + 1
        
        error_by_type: Dict[str, int] = {}
        for e in last_5min:
            error_by_type[e.error_type] = error_by_type.get(e.error_type, 0) + 1
        
        return {
            "total_errors": len(self._errors),
            "errors_last_1min": len(last_1min),
            "errors_last_5min": len(last_5min),
            "error_counts_all_time": self._error_counts,
            "errors_by_account_5min": error_by_account,
            "errors_by_type_5min": error_by_type,
            "uptime_seconds": round(now - self._start_time, 1)
        }
    
    def clear(self):
        self._errors.clear()
        self._error_counts.clear()


error_collector = ErrorCollector()
