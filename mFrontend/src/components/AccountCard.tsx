import type { Account, Position, Order, Trade } from '../types';
import { formatMoney, formatAccountName, getPositionSymbol, formatTime } from '../utils';
import './AccountCard.css';

interface Props {
  account: Account;
}

export function AccountCard({ account }: Props) {
  const equity = parseFloat(account.equity || '0');
  const pnl = parseFloat(account.unrealized_pnl || '0');
  const volume24h = parseFloat(account.volume_24h || '0');
  const weeklyVolume = parseFloat(account.weekly_volume || '0');
  const monthlyVolume = parseFloat(account.monthly_volume || '0');
  const totalVolume = parseFloat(account.total_volume || '0');
  const dataAge = account.last_update ? (Date.now() / 1000) - account.last_update : 0;

  const openPositions = account.positions.filter(p => {
    const size = parseFloat(p.position || p.signed_size || p.size || '0');
    return size !== 0;
  });

  return (
    <div className="account-card">
      <div className="account-header">
        <div>
          <div className="account-name">
            <span className="checkmark">✓</span>
            {formatAccountName(account.name)}
          </div>
          <div className="account-index">account_{account.account_index}</div>
        </div>
        <div className={`badge ${account.is_live ? 'live' : 'offline'}`}>
          <span className="dot"></span>
          {account.is_live ? 'Live' : 'Offline'}
        </div>
      </div>

      <div className="account-stats">
        <div className="stat-item">
          <div className="stat-label">Equity</div>
          <div className="stat-value">{formatMoney(equity)}</div>
        </div>
        <div className="stat-item">
          <div className="stat-label">Unrealized PnL</div>
          <div className={`stat-value ${pnl >= 0 ? 'positive' : 'negative'}`}>
            {formatMoney(pnl)}
          </div>
        </div>
        <div className="stat-item">
          <div className="stat-label">Active Orders</div>
          <div className="stat-value">{account.active_orders.length}</div>
        </div>
        <div className="stat-item">
          <div className="stat-label">Data Age</div>
          <div className={`stat-value ${dataAge > 5 ? 'stale' : ''}`}>
            {dataAge.toFixed(1)}s
          </div>
        </div>
      </div>

      <div className="section">
        <div className="section-header">Wolumeny</div>
        <div className="volume-grid">
          <div className="stat-item"><div className="stat-label">24H</div><div className="stat-value">{formatMoney(volume24h)}</div></div>
          <div className="stat-item"><div className="stat-label">7D</div><div className="stat-value">{formatMoney(weeklyVolume)}</div></div>
          <div className="stat-item"><div className="stat-label">30D</div><div className="stat-value">{formatMoney(monthlyVolume)}</div></div>
          <div className="stat-item"><div className="stat-label">Total</div><div className="stat-value">{formatMoney(totalVolume)}</div></div>
        </div>
      </div>

      <div className="section">
        <div className="section-header">Otwarte Pozycje ({openPositions.length})</div>
        <div className="list">
          {openPositions.length > 0 ? openPositions.map((pos, i) => (
            <PositionRow key={i} position={pos} />
          )) : <span className="no-data">Brak otwartych pozycji</span>}
        </div>
      </div>

      <div className="section">
        <div className="section-header">Otwarte Zlecenia ({account.active_orders.length})</div>
        <div className="list">
          {account.active_orders.length > 0 ? account.active_orders.slice(0, 5).map((order, i) => (
            <OrderRow key={i} order={order} />
          )) : <span className="no-data">Brak otwartych zleceń</span>}
          {account.active_orders.length > 5 && (
            <div className="more">+{account.active_orders.length - 5} więcej zleceń</div>
          )}
        </div>
      </div>

      <div className="section">
        <div className="section-header">Historia Transakcji ({account.trades.length})</div>
        <div className="list">
          {account.trades.length > 0 ? account.trades.slice(0, 3).map((trade, i) => (
            <TradeRow key={i} trade={trade} />
          )) : <span className="no-data">Brak transakcji</span>}
          {account.trades.length > 3 && (
            <div className="more">+{account.trades.length - 3} więcej transakcji</div>
          )}
        </div>
      </div>
    </div>
  );
}

function PositionRow({ position }: { position: Position }) {
  const size = parseFloat(position.position || position.signed_size || position.size || '0');
  const sign = parseInt(position.sign || '1') || (size >= 0 ? 1 : -1);
  const signedSize = size * sign;
  const isLong = signedSize > 0;
  const symbol = position.symbol || getPositionSymbol(position.market_id || position.market_index);
  const entryPrice = parseFloat(position.avg_entry_price || position.entry_price || '0');
  const pnl = parseFloat(position.unrealized_pnl || '0');

  return (
    <div className="row">
      <span className={`tag ${isLong ? 'long' : 'short'}`}>
        {symbol} {isLong ? 'LONG' : 'SHORT'}
      </span>
      <span className="details">{Math.abs(size).toFixed(4)} @ {formatMoney(entryPrice)}</span>
      <span className={`pnl ${pnl >= 0 ? 'positive' : 'negative'}`}>
        {pnl >= 0 ? '+' : ''}{formatMoney(pnl)}
      </span>
    </div>
  );
}

function OrderRow({ order }: { order: Order }) {
  const symbol = getPositionSymbol(order.market_id || order.market_index);
  const side = order.is_ask ? 'S' : 'L';
  const sideClass = order.is_ask ? 'short' : 'long';
  const price = parseFloat(order.price || '0');
  const size = parseFloat(order.remaining_base_amount || order.size || '0');

  return (
    <div className="row">
      <span className={`tag ${sideClass}`}>{symbol} {side}</span>
      <span className="details">{size.toFixed(4)} @ {formatMoney(price)}</span>
    </div>
  );
}

function TradeRow({ trade }: { trade: Trade }) {
  const symbol = getPositionSymbol(trade.market_id || trade.market_index);
  const size = parseFloat(trade.size || trade.signed_size || '0');
  const price = parseFloat(trade.price || '0');
  const isBuy = size > 0;

  return (
    <div className="row">
      <span className={`tag ${isBuy ? 'long' : 'short'}`}>{symbol} {isBuy ? 'BUY' : 'SELL'}</span>
      <span className="details">{Math.abs(size).toFixed(4)} @ {formatMoney(price)}</span>
      <span className="time">{formatTime(trade.timestamp)}</span>
    </div>
  );
}
