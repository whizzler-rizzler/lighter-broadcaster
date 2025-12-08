import type { PortfolioData, WsHealthData } from './types';

const API_BASE = '';

export async function fetchPortfolio(): Promise<PortfolioData> {
  const res = await fetch(`${API_BASE}/api/portfolio`);
  if (!res.ok) throw new Error('Failed to fetch portfolio');
  return res.json();
}

export async function fetchWsHealth(): Promise<WsHealthData> {
  const res = await fetch(`${API_BASE}/api/ws/health`);
  if (!res.ok) throw new Error('Failed to fetch WS health');
  return res.json();
}

export interface LatencyData {
  frontend_polling: {
    ws_interval_avg: number;
    time_since_ws: number | null;
    rest_interval_avg: number;
    time_since_rest: number | null;
    stats_poll_interval: number | null;
    stats_fetch_time: number;
  };
  backend_polling: {
    api_poll_rate: number;
    positions_age: number;
    balance_age: number;
    active_accounts: number;
    total_accounts: number;
    connected_clients: number;
  };
  websocket: {
    connected: boolean;
    message_count: number;
    last_message_age: number | null;
    connection_uptime: number;
    interval_min: number;
    interval_avg: number;
    interval_max: number;
  };
  rest: {
    request_count: number;
    last_update: number;
    interval_min: number;
    interval_avg: number;
    interval_max: number;
  };
  timestamps: {
    ws: number | null;
    rest: number | null;
    stats: number | null;
    now: number;
  };
}

export async function fetchLatency(): Promise<LatencyData> {
  const res = await fetch(`${API_BASE}/api/latency`);
  if (!res.ok) throw new Error('Failed to fetch latency');
  return res.json();
}

export async function reconnectWs(accountIndex?: number): Promise<{ success: boolean }> {
  const url = accountIndex !== undefined 
    ? `${API_BASE}/api/ws/reconnect?account_index=${accountIndex}`
    : `${API_BASE}/api/ws/reconnect`;
  const res = await fetch(url, { method: 'POST' });
  if (!res.ok) throw new Error('Failed to reconnect');
  return res.json();
}
