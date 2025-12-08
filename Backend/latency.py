import time
from typing import Dict, List, Any
from collections import deque
from dataclasses import dataclass, field

@dataclass
class LatencyMetrics:
    samples: deque = field(default_factory=lambda: deque(maxlen=30))
    last_timestamp: float = 0
    
    @property
    def min(self) -> float:
        if not self.samples:
            return 0
        return min(self.samples)
    
    @property
    def max(self) -> float:
        if not self.samples:
            return 0
        return max(self.samples)
    
    @property
    def avg(self) -> float:
        if not self.samples:
            return 0
        return sum(self.samples) / len(self.samples)
    
    @property
    def count(self) -> int:
        return len(self.samples)
    
    def add_sample(self, latency_ms: float):
        self.samples.append(latency_ms)
        self.last_timestamp = time.time()
    
    def get_samples_list(self) -> List[float]:
        return list(self.samples)


class LatencyTracker:
    def __init__(self):
        self.rest_polling = LatencyMetrics()
        self.ws_messages = LatencyMetrics()
        self.stats_fetch = LatencyMetrics()
        
        self.rest_request_count = 0
        self.ws_message_count = 0
        
        self.ws_connected = False
        self.ws_last_message_time = 0
        self.ws_connection_time = 0
        
        self.last_rest_update = 0
        self.last_ws_update = 0
        self.last_stats_update = 0
        
        self.positions_last_update = 0
        self.balance_last_update = 0
        
        self.active_accounts = 0
        self.total_accounts = 0
        self.connected_clients = 0
    
    def record_rest_poll(self, latency_ms: float):
        self.rest_polling.add_sample(latency_ms)
        self.rest_request_count += 1
        self.last_rest_update = time.time()
    
    def record_ws_message(self, latency_ms: float = 0):
        if latency_ms > 0:
            self.ws_messages.add_sample(latency_ms)
        self.ws_message_count += 1
        self.ws_last_message_time = time.time()
        self.last_ws_update = time.time()
    
    def record_stats_fetch(self, latency_ms: float):
        self.stats_fetch.add_sample(latency_ms)
        self.last_stats_update = time.time()
    
    def set_ws_status(self, connected: bool):
        was_connected = self.ws_connected
        self.ws_connected = connected
        if connected and not was_connected:
            self.ws_connection_time = time.time()
    
    def update_positions_time(self):
        self.positions_last_update = time.time()
    
    def update_balance_time(self):
        self.balance_last_update = time.time()
    
    def set_account_stats(self, active: int, total: int, clients: int):
        self.active_accounts = active
        self.total_accounts = total
        self.connected_clients = clients
    
    def get_metrics(self) -> Dict[str, Any]:
        now = time.time()
        
        return {
            "frontend_polling": {
                "ws_interval_avg": round(self.ws_messages.avg, 0),
                "time_since_ws": round((now - self.last_ws_update) * 1000, 0) if self.last_ws_update else None,
                "rest_interval_avg": round(self.rest_polling.avg, 0),
                "time_since_rest": round((now - self.last_rest_update) * 1000, 0) if self.last_rest_update else None,
                "stats_poll_interval": round((now - self.last_stats_update) * 1000, 0) if self.last_stats_update else None,
                "stats_fetch_time": round(self.stats_fetch.avg, 0)
            },
            "backend_polling": {
                "api_poll_rate": round(self.rest_polling.avg, 0) if self.rest_polling.count > 0 else None,
                "positions_age": round((now - self.positions_last_update) * 1000, 0) if self.positions_last_update else None,
                "balance_age": round((now - self.balance_last_update) * 1000, 0) if self.balance_last_update else None,
                "active_accounts": self.active_accounts,
                "total_accounts": self.total_accounts,
                "connected_clients": self.connected_clients
            },
            "websocket": {
                "connected": self.ws_connected,
                "message_count": self.ws_message_count,
                "last_message_age": round((now - self.ws_last_message_time) * 1000, 0) if self.ws_last_message_time else None,
                "connection_uptime": round(now - self.ws_connection_time, 0) if self.ws_connected and self.ws_connection_time else None,
                "interval_min": round(self.ws_messages.min, 0),
                "interval_avg": round(self.ws_messages.avg, 0),
                "interval_max": round(self.ws_messages.max, 0),
                "samples": self.ws_messages.get_samples_list()
            },
            "rest": {
                "request_count": self.rest_request_count,
                "last_update": self.last_rest_update,
                "interval_min": round(self.rest_polling.min, 0),
                "interval_avg": round(self.rest_polling.avg, 0),
                "interval_max": round(self.rest_polling.max, 0),
                "samples": self.rest_polling.get_samples_list()
            },
            "timestamps": {
                "ws": round(self.last_ws_update, 0) if self.last_ws_update else None,
                "rest": round(self.last_rest_update, 0) if self.last_rest_update else None,
                "stats": round(self.last_stats_update, 0) if self.last_stats_update else None,
                "now": round(now, 0)
            }
        }


latency_tracker = LatencyTracker()
