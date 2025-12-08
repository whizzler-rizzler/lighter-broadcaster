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

export interface RestHealthConnection {
  account_id: number;
  account_name: string;
  connected: boolean;
  last_success_age: number;
  last_failure_age: number;
  total_requests: number;
  successful_requests: number;
  failed_requests: number;
  success_rate: number;
  retry_phase: number;
  phase_attempts: number;
  consecutive_failures: number;
  uptime_seconds: number;
  last_error: string;
}

export interface RestHealthData {
  type: string;
  total_connections: number;
  connected_count: number;
  disconnected_count: number;
  total_requests: number;
  total_failures: number;
  success_rate: number;
  uptime_seconds: number;
  polling: boolean;
  poll_interval: number;
  connections: RestHealthConnection[];
}

export async function fetchRestHealth(): Promise<RestHealthData> {
  const res = await fetch(`${API_BASE}/api/rest/health`);
  if (!res.ok) throw new Error('Failed to fetch REST health');
  return res.json();
}

export interface ErrorEntry {
  timestamp: number;
  account_index: number;
  account_name: string;
  error_type: string;
  error_code: number | null;
  message: string;
  source: string;
  age_seconds: number;
  time_str: string;
}

export interface ErrorSummary {
  total_errors: number;
  errors_last_1min: number;
  errors_last_5min: number;
  error_counts_all_time: Record<string, number>;
  errors_by_account_5min: Record<number, number>;
  errors_by_type_5min: Record<string, number>;
  uptime_seconds: number;
}

export interface ErrorsData {
  errors: ErrorEntry[];
  summary: ErrorSummary;
}

export async function fetchErrors(): Promise<ErrorsData> {
  const res = await fetch(`${API_BASE}/api/errors?limit=30`);
  if (!res.ok) throw new Error('Failed to fetch errors');
  return res.json();
}

export async function clearErrors(): Promise<{ success: boolean }> {
  const res = await fetch(`${API_BASE}/api/errors/clear`, { method: 'POST' });
  if (!res.ok) throw new Error('Failed to clear errors');
  return res.json();
}

export async function reconnectRest(accountIndex?: number): Promise<{ success: boolean }> {
  const url = accountIndex !== undefined 
    ? `${API_BASE}/api/rest/reconnect?account_index=${accountIndex}`
    : `${API_BASE}/api/rest/reconnect`;
  const res = await fetch(url, { method: 'POST' });
  if (!res.ok) throw new Error('Failed to reconnect');
  return res.json();
}
