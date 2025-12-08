import { useState, useEffect } from 'react';
import { AccountCard } from './components/AccountCard';
import type { PortfolioData, WsHealthData, Position } from './types';
import { fetchPortfolio, fetchWsHealth, fetchLatency, LatencyData, reconnectWs } from './api';
import { formatMoney, getPositionSymbol } from './utils';
import './App.css';

function App() {
  const [portfolio, setPortfolio] = useState<PortfolioData | null>(null);
  const [wsHealth, setWsHealth] = useState<WsHealthData | null>(null);
  const [latency, setLatency] = useState<LatencyData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const [portfolioData, healthData, latencyData] = await Promise.all([
          fetchPortfolio(),
          fetchWsHealth().catch(() => null),
          fetchLatency().catch(() => null)
        ]);
        setPortfolio(portfolioData);
        setWsHealth(healthData);
        setLatency(latencyData);
        setError(null);
      } catch (e) {
        setError('Failed to load portfolio');
        console.error(e);
      }
    };

    load();
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, []);

  const agg = portfolio?.aggregated;

  const allPositions: { accountName: string; accountIndex: string; position: Position }[] = [];
  portfolio?.accounts.forEach(account => {
    account.positions.forEach(pos => {
      const size = parseFloat(pos.position || pos.signed_size || pos.size || '0');
      if (size !== 0) {
        allPositions.push({
          accountName: account.name,
          accountIndex: account.account_index.toString(),
          position: pos
        });
      }
    });
  });

  const handleReconnect = async (accountIndex?: number) => {
    try {
      await reconnectWs(accountIndex);
    } catch (e) {
      console.error('Reconnect failed:', e);
    }
  };

  return (
    <div className="app">
      <header className="header">
        <h1>Lighter Broadcaster Dashboard</h1>
        <div className="header-stats">
          <div className="header-stat">
            <div className="header-stat-label">Total Equity</div>
            <div className="header-stat-value">{formatMoney(agg?.total_equity || 0)}</div>
          </div>
          <div className="header-stat">
            <div className="header-stat-label">Total PnL</div>
            <div className={`header-stat-value ${(agg?.total_unrealized_pnl || 0) >= 0 ? '' : 'negative'}`}>
              {formatMoney(agg?.total_unrealized_pnl || 0)}
            </div>
          </div>
          <div className="header-stat">
            <div className="header-stat-label">Accounts</div>
            <div className="header-stat-value">{agg?.accounts_live || 0}/{agg?.accounts_total || 0}</div>
          </div>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="accounts-grid">
        {portfolio?.accounts.map(account => (
          <AccountCard key={account.account_index} account={account} />
        ))}
      </div>

      <div className="bottom-sections">
        <div className="section-panel">
          <h2>Timing / Latency</h2>
          {latency ? (
            <div className="timing-grid">
              <div className="timing-item">
                <span className="timing-label">REST Poll Latency</span>
                <span className="timing-value">{latency.rest_poll_latency_ms?.toFixed(0) || '-'} ms</span>
              </div>
              <div className="timing-item">
                <span className="timing-label">Last Balance Update</span>
                <span className="timing-value">{latency.last_balance_update || '-'}</span>
              </div>
              <div className="timing-item">
                <span className="timing-label">Last Positions Update</span>
                <span className="timing-value">{latency.last_positions_update || '-'}</span>
              </div>
              <div className="timing-item">
                <span className="timing-label">Last Orders Update</span>
                <span className="timing-value">{latency.last_orders_update || '-'}</span>
              </div>
              <div className="timing-item">
                <span className="timing-label">WebSocket</span>
                <span className={`timing-value ${latency.ws_connected ? 'connected' : 'disconnected'}`}>
                  {latency.ws_connected ? 'Connected' : 'Disconnected'}
                </span>
              </div>
              <div className="timing-item">
                <span className="timing-label">Broadcast Clients</span>
                <span className="timing-value">{latency.broadcast_clients || 0}</span>
              </div>
            </div>
          ) : (
            <div className="no-data">Loading timing data...</div>
          )}
        </div>

        <div className="section-panel">
          <h2>WebSocket Connections</h2>
          {wsHealth ? (
            <>
              <div className="ws-summary">
                <span>Connected: {wsHealth.connected_count}/{wsHealth.total_connections}</span>
                <span>Messages: {wsHealth.total_messages_received}</span>
                <span>Reconnects: {wsHealth.total_reconnect_attempts}</span>
                <button className="reconnect-btn" onClick={() => handleReconnect()}>Reconnect All</button>
              </div>
              <div className="ws-connections">
                {wsHealth.connections.map(conn => (
                  <div key={conn.account_id} className={`ws-conn ${conn.connected ? 'connected' : 'disconnected'}`}>
                    <span className="ws-name">{conn.account_name}</span>
                    <span className={`ws-status ${conn.connected ? 'online' : 'offline'}`}>
                      {conn.connected ? 'OK' : 'OFF'}
                    </span>
                    <span className="ws-msg-age">{conn.last_message_age?.toFixed(0)}s ago</span>
                    <span className="ws-msgs">{conn.total_messages} msgs</span>
                    {conn.has_proxy && <span className="ws-proxy">proxy</span>}
                  </div>
                ))}
              </div>
            </>
          ) : (
            <div className="no-data">Loading WS health...</div>
          )}
        </div>

        <div className="section-panel wide">
          <h2>Wszystkie Otwarte Pozycje ({allPositions.length})</h2>
          <div className="all-positions">
            {allPositions.length > 0 ? (
              <table className="positions-table">
                <thead>
                  <tr>
                    <th>Konto</th>
                    <th>Symbol</th>
                    <th>Strona</th>
                    <th>Rozmiar</th>
                    <th>Cena Wejścia</th>
                    <th>PnL</th>
                  </tr>
                </thead>
                <tbody>
                  {allPositions.map((item, i) => {
                    const pos = item.position;
                    const size = parseFloat(pos.position || pos.signed_size || pos.size || '0');
                    const sign = parseInt(pos.sign || '1') || (size >= 0 ? 1 : -1);
                    const signedSize = size * sign;
                    const isLong = signedSize > 0;
                    const symbol = pos.symbol || getPositionSymbol(pos.market_id || pos.market_index);
                    const entryPrice = parseFloat(pos.avg_entry_price || pos.entry_price || '0');
                    const pnl = parseFloat(pos.unrealized_pnl || '0');

                    return (
                      <tr key={i}>
                        <td>{item.accountName.replace('Lighter_', '').split('_')[0]}</td>
                        <td>{symbol}</td>
                        <td className={isLong ? 'long' : 'short'}>{isLong ? 'LONG' : 'SHORT'}</td>
                        <td>{Math.abs(signedSize).toFixed(4)}</td>
                        <td>{formatMoney(entryPrice)}</td>
                        <td className={pnl >= 0 ? 'positive' : 'negative'}>
                          {pnl >= 0 ? '+' : ''}{formatMoney(pnl)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            ) : (
              <div className="no-data">Brak otwartych pozycji</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;
