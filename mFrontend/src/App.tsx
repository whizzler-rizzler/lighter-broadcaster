import { useState, useEffect } from 'react';
import { AccountCard } from './components/AccountCard';
import type { PortfolioData, WsHealthData, Position } from './types';
import { fetchPortfolio, fetchWsHealth, fetchLatency, reconnectWs, fetchRestHealth, fetchErrors, clearErrors, reconnectRest, fetchRawWsMessages } from './api';
import type { LatencyData, RestHealthData, ErrorsData, RawWsData } from './api';
import { formatMoney, getPositionSymbol } from './utils';
import './App.css';

function App() {
  const [portfolio, setPortfolio] = useState<PortfolioData | null>(null);
  const [wsHealth, setWsHealth] = useState<WsHealthData | null>(null);
  const [restHealth, setRestHealth] = useState<RestHealthData | null>(null);
  const [latency, setLatency] = useState<LatencyData | null>(null);
  const [errors, setErrors] = useState<ErrorsData | null>(null);
  const [rawWsMessages, setRawWsMessages] = useState<RawWsData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const [portfolioData, healthData, restHealthData, latencyData, errorsData, rawWsData] = await Promise.all([
          fetchPortfolio(),
          fetchWsHealth().catch(() => null),
          fetchRestHealth().catch(() => null),
          fetchLatency().catch(() => null),
          fetchErrors().catch(() => null),
          fetchRawWsMessages().catch(() => null)
        ]);
        setPortfolio(portfolioData);
        setWsHealth(healthData);
        setRestHealth(restHealthData);
        setLatency(latencyData);
        setErrors(errorsData);
        setRawWsMessages(rawWsData);
        setError(null);
      } catch (e) {
        setError('Failed to load portfolio');
        console.error(e);
      }
    };

    load();
    const interval = setInterval(load, 500);
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

  const handleReconnectWs = async (accountIndex?: number) => {
    try {
      await reconnectWs(accountIndex);
    } catch (e) {
      console.error('Reconnect failed:', e);
    }
  };

  const handleReconnectRest = async (accountIndex?: number) => {
    try {
      await reconnectRest(accountIndex);
      const restHealthData = await fetchRestHealth();
      setRestHealth(restHealthData);
    } catch (e) {
      console.error('REST reconnect failed:', e);
    }
  };

  const handleClearErrors = async () => {
    try {
      await clearErrors();
      const errorsData = await fetchErrors();
      setErrors(errorsData);
    } catch (e) {
      console.error('Clear errors failed:', e);
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
                <span className="timing-label">REST Poll Avg</span>
                <span className="timing-value">{latency.rest.interval_avg?.toFixed(0) || '-'} ms</span>
              </div>
              <div className="timing-item">
                <span className="timing-label">Balance Age</span>
                <span className="timing-value">{latency.backend_polling.balance_age?.toFixed(0) || '-'} ms</span>
              </div>
              <div className="timing-item">
                <span className="timing-label">Positions Age</span>
                <span className="timing-value">{latency.backend_polling.positions_age?.toFixed(0) || '-'} ms</span>
              </div>
              <div className="timing-item">
                <span className="timing-label">REST Requests</span>
                <span className="timing-value">{latency.rest.request_count || 0}</span>
              </div>
              <div className="timing-item">
                <span className="timing-label">WebSocket</span>
                <span className={`timing-value ${latency.websocket.connected ? 'connected' : 'disconnected'}`}>
                  {latency.websocket.connected ? 'Connected' : 'Disconnected'}
                </span>
              </div>
              <div className="timing-item">
                <span className="timing-label">Accounts Live</span>
                <span className="timing-value">{latency.backend_polling.active_accounts || 0}/{latency.backend_polling.total_accounts || 0}</span>
              </div>
              <div className="timing-item">
                <span className="timing-label">WS Uptime</span>
                <span className="timing-value">{latency.websocket.connection_uptime?.toFixed(0) || '-'}s</span>
              </div>
              <div className="timing-item">
                <span className="timing-label">Broadcast Clients</span>
                <span className="timing-value">{latency.backend_polling.connected_clients || 0}</span>
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
                <button className="reconnect-btn" onClick={() => handleReconnectWs()}>Reconnect All</button>
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

        <div className="section-panel">
          <h2>REST API Connections</h2>
          {restHealth ? (
            <>
              <div className="ws-summary">
                <span>Connected: {restHealth.connected_count}/{restHealth.total_connections}</span>
                <span>Requests: {restHealth.total_requests}</span>
                <span>Success: {restHealth.success_rate}%</span>
                <button className="reconnect-btn" onClick={() => handleReconnectRest()}>Reset All</button>
              </div>
              <div className="ws-connections">
                {restHealth.connections.map(conn => (
                  <div key={conn.account_id} className={`ws-conn ${conn.connected ? 'connected' : 'disconnected'}`}>
                    <span className="ws-name">{conn.account_name}</span>
                    <span className={`ws-status ${conn.connected ? 'online' : 'offline'}`}>
                      {conn.connected ? 'OK' : 'OFF'}
                    </span>
                    <span className="ws-msg-age">{conn.success_rate}%</span>
                    <span className="ws-msgs">{conn.total_requests} req</span>
                    <span className="ws-rpm">{conn.requests_per_minute} ok/min</span>
                    {conn.failed_requests > 0 && <span className="ws-error">{conn.failed_requests} fail</span>}
                  </div>
                ))}
              </div>
            </>
          ) : (
            <div className="no-data">Loading REST health...</div>
          )}
        </div>

        <div className="section-panel error-panel">
          <h2>Error Log {errors?.summary?.errors_last_5min ? <span className="error-badge">{errors.summary.errors_last_5min}</span> : null}</h2>
          {errors ? (
            <>
              <div className="ws-summary">
                <span>Last 1min: {errors.summary.errors_last_1min}</span>
                <span>Last 5min: {errors.summary.errors_last_5min}</span>
                <span>Total: {errors.summary.total_errors}</span>
                <button className="reconnect-btn clear-btn" onClick={handleClearErrors}>Clear</button>
              </div>
              <div className="error-list">
                {errors.errors.length > 0 ? (
                  errors.errors.slice(0, 20).map((err, i) => (
                    <div key={i} className={`error-item ${err.error_type === '429' ? 'rate-limit' : ''}`}>
                      <span className="error-time">{err.time_str}</span>
                      <span className="error-account">{err.account_name.replace('Lighter_', '').split('_')[0]}</span>
                      <span className={`error-type ${err.source}`}>{err.error_type}</span>
                      <span className="error-source">{err.source}</span>
                      <span className="error-msg">{err.message}</span>
                    </div>
                  ))
                ) : (
                  <div className="no-errors">No recent errors</div>
                )}
              </div>
            </>
          ) : (
            <div className="no-data">Loading errors...</div>
          )}
        </div>

        <div className="section-panel raw-ws-panel wide">
          <h2>
            <span className="raw-ws-dot"></span>
            RAW WEBSOCKET DEBUG
            <button className="reconnect-btn" onClick={() => handleReconnectWs()}>Reconnect</button>
            <span className={`ws-status-badge ${rawWsMessages?.connected_count && rawWsMessages?.connected_count > 0 ? 'connected' : ''}`}>
              {rawWsMessages?.connected_count || 0}/{rawWsMessages?.total_connections || 0} polaczonych
            </span>
            <span className="event-count">lacznie eventow: {rawWsMessages?.total_events || 0}</span>
          </h2>
          {rawWsMessages ? (
            <div className="raw-ws-list">
              {rawWsMessages.messages.length > 0 ? (
                rawWsMessages.messages.map((msg, i) => (
                  <div key={i} className="raw-ws-item">
                    <div className="raw-ws-header">
                      <span className="raw-ws-index">#{msg.account_name.replace('Lighter_', '').split('_')[0]}</span>
                      <span className="raw-ws-time">{msg.time_str}</span>
                    </div>
                    <div className="raw-ws-content">
                      {JSON.stringify(msg.data)}
                    </div>
                  </div>
                ))
              ) : (
                <div className="no-data">Brak wiadomosci WebSocket</div>
              )}
            </div>
          ) : (
            <div className="no-data">Loading raw WS messages...</div>
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
                    <th>Cena Wejscia</th>
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
