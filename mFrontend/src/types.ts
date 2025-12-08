export interface Position {
  market_id?: number;
  market_index?: number;
  symbol?: string;
  position?: string;
  signed_size?: string;
  size?: string;
  sign?: string;
  avg_entry_price?: string;
  entry_price?: string;
  unrealized_pnl?: string;
}

export interface Order {
  market_id?: number;
  market_index?: number;
  is_ask?: boolean;
  price?: string;
  remaining_base_amount?: string;
  size?: string;
}

export interface Trade {
  market_id?: number;
  market_index?: number;
  size?: string;
  signed_size?: string;
  price?: string;
  timestamp?: number;
}

export interface Account {
  name: string;
  account_index: number;
  is_live: boolean;
  last_update: number;
  equity?: string;
  unrealized_pnl?: string;
  volume_24h?: string;
  weekly_volume?: string;
  monthly_volume?: string;
  total_volume?: string;
  positions: Position[];
  active_orders: Order[];
  trades: Trade[];
}

export interface PortfolioData {
  accounts: Account[];
  aggregated: {
    total_equity: number;
    total_unrealized_pnl: number;
    accounts_live: number;
    accounts_total: number;
  };
}

export interface HealthConnection {
  account_id: number;
  account_name: string;
  connected: boolean;
  last_message_age: number;
  last_pong_age: number;
  reconnect_count: number;
  total_messages: number;
  has_proxy: boolean;
}

export interface WsHealthData {
  total_connections: number;
  connected_count: number;
  disconnected_count: number;
  total_messages_received: number;
  total_reconnect_attempts: number;
  connections: HealthConnection[];
}
