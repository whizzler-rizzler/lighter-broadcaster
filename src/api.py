import asyncio
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from src.config import settings
from src.cache import cache
from src.lighter_client import lighter_client
from src.websocket_client import ws_client
from src.websocket_server import manager

logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Lighter Broadcaster", version="1.0.0")

app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"error": "Rate limit exceeded", "detail": str(exc.detail)}
    )

@app.on_event("startup")
async def startup():
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Lighter Broadcaster...")
    
    await lighter_client.initialize(settings.accounts)
    
    asyncio.create_task(lighter_client.start_polling())
    
    async def on_ws_message(data):
        await manager.broadcast({"type": "lighter_update", "data": data})
        if "account_index" in data:
            await cache.set(f"ws_update:{data['account_index']}", data)
    
    ws_client.set_signer_clients(lighter_client.signer_clients)
    ws_client.add_callback(on_ws_message)
    await ws_client.start()
    
    logger.info(f"Broadcaster started with {len(settings.accounts)} accounts")

@app.on_event("shutdown")
async def shutdown():
    logger.info("Shutting down Lighter Broadcaster...")
    await ws_client.stop()
    await lighter_client.close()

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "accounts_configured": len(settings.accounts),
        "ws_connected": ws_client.is_connected,
        "broadcast_clients": manager.connection_count
    }

@app.get("/api/accounts")
@limiter.limit(settings.rate_limit)
async def get_accounts(request: Request):
    cached_data = await cache.get_all()
    accounts = {}
    
    for key, value in cached_data.items():
        if key.startswith("account:"):
            account_id = key.replace("account:", "")
            accounts[account_id] = value
    
    return {"accounts": accounts}

@app.get("/api/accounts/{account_index}")
@limiter.limit(settings.rate_limit)
async def get_account(request: Request, account_index: int):
    cached = await cache.get(f"account:{account_index}")
    if cached:
        return {"account": cached, "source": "cache"}
    
    for account in settings.accounts:
        if account.account_index == account_index:
            data = await lighter_client.fetch_account_data(account.name, account_index)
            if data:
                return {"account": data, "source": "fresh"}
    
    raise HTTPException(status_code=404, detail="Account not found")

@app.get("/api/status")
async def get_status():
    cache_stats = await cache.get_stats()
    return {
        "polling": lighter_client.running,
        "poll_interval": settings.poll_interval,
        "ws_connected": ws_client.is_connected,
        "broadcast_clients": manager.connection_count,
        "accounts_configured": len(settings.accounts),
        "cache": cache_stats
    }

@app.get("/api/portfolio")
async def get_portfolio():
    import time
    cached_data = await cache.get_all()
    accounts_list = []
    
    total_equity = 0
    total_unrealized_pnl = 0
    total_margin_used = 0
    total_positions = 0
    total_active_orders = 0
    total_trades = 0
    
    now = time.time()
    
    for key, value in cached_data.items():
        if key.startswith("account:"):
            account_data = value.get("data", value)
            raw_data = account_data.get("raw_data", {})
            active_orders = account_data.get("active_orders", []) or []
            last_update = account_data.get("last_update", 0)
            
            is_live = (now - last_update) < 10
            
            equity = 0
            available_balance = 0
            unrealized_pnl = 0
            margin_used = 0
            margin_ratio = 0
            volume_24h = 0
            positions = []
            trades = []
            
            if isinstance(raw_data, dict):
                acc_list = raw_data.get("accounts", [])
                if acc_list and len(acc_list) > 0:
                    acc = acc_list[0]
                    equity = float(acc.get("collateral", 0) or 0)
                    available_balance = float(acc.get("available_balance", 0) or 0)
                    margin_used = equity - available_balance
                    
                    pos_list = acc.get("positions", [])
                    for pos in pos_list:
                        pnl = float(pos.get("unrealized_pnl", 0) or 0)
                        unrealized_pnl += pnl
                    
                    if equity > 0:
                        margin_ratio = margin_used / equity
                    
                    positions = pos_list
                
                trades = raw_data.get("trades", []) or []
                
                day_ago = now - 86400
                for trade in trades:
                    trade_ts = float(trade.get("timestamp", 0) or 0)
                    trade_time = trade_ts / 1000 if trade_ts > 10000000000 else trade_ts
                    if trade_time >= day_ago:
                        size = abs(float(trade.get("size", 0) or 0))
                        price = float(trade.get("price", 0) or 0)
                        volume_24h += size * price
            
            account_entry = {
                "account_index": str(account_data.get("account_index", "")),
                "name": account_data.get("account_name", ""),
                "is_live": is_live,
                "last_update": int(last_update),
                "equity": equity,
                "available_balance": available_balance,
                "unrealized_pnl": unrealized_pnl,
                "margin_used": margin_used,
                "margin_ratio": margin_ratio,
                "volume_24h": volume_24h,
                "positions": positions,
                "active_orders": active_orders,
                "trades": trades
            }
            accounts_list.append(account_entry)
            
            total_equity += equity
            total_unrealized_pnl += unrealized_pnl
            total_margin_used += margin_used
            total_positions += len(positions)
            total_active_orders += len(active_orders)
            total_trades += len(trades)
    
    return {
        "accounts": accounts_list,
        "aggregated": {
            "total_equity": total_equity,
            "total_unrealized_pnl": total_unrealized_pnl,
            "total_margin_used": total_margin_used,
            "total_positions": total_positions,
            "total_active_orders": total_active_orders,
            "total_trades": total_trades,
            "accounts_live": len([a for a in accounts_list if a["is_live"]]),
            "accounts_total": len(settings.accounts)
        },
        "timestamp": int(now)
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        cached_data = await cache.get_all()
        await manager.send_to_client(websocket, {
            "type": "initial_data",
            "data": cached_data
        })
        
        while True:
            data = await websocket.receive_text()
            logger.debug(f"Received from client: {data}")
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        await manager.disconnect(websocket)

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Lighter Broadcaster Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0d0d1a;
            color: #fff;
            min-height: 100vh;
            padding: 24px;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        .header h1 {
            font-size: 1.5rem;
            font-weight: 600;
            color: #fff;
        }
        .header-stats {
            display: flex;
            gap: 24px;
            align-items: center;
        }
        .header-stat {
            text-align: right;
        }
        .header-stat-label {
            font-size: 0.75rem;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .header-stat-value {
            font-size: 1.25rem;
            font-weight: 600;
            color: #00ff88;
        }
        .header-stat-value.negative { color: #ff4757; }
        .accounts-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 16px;
        }
        @media (max-width: 1200px) {
            .accounts-grid { grid-template-columns: repeat(2, 1fr); }
        }
        @media (max-width: 768px) {
            .accounts-grid { grid-template-columns: 1fr; }
        }
        .account-card {
            background: #14141f;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            padding: 20px;
        }
        .account-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 16px;
        }
        .account-name {
            display: flex;
            align-items: center;
            gap: 8px;
            font-weight: 600;
            font-size: 1rem;
        }
        .account-name .checkmark {
            color: #00ff88;
            font-size: 1rem;
        }
        .account-index {
            font-size: 0.8rem;
            color: #666;
            margin-top: 4px;
        }
        .live-badge {
            display: flex;
            align-items: center;
            gap: 6px;
            background: rgba(0, 255, 136, 0.1);
            color: #00ff88;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 0.75rem;
            font-weight: 500;
        }
        .live-badge .dot {
            width: 6px;
            height: 6px;
            background: #00ff88;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
        .offline-badge {
            background: rgba(255, 71, 87, 0.1);
            color: #ff4757;
        }
        .offline-badge .dot {
            background: #ff4757;
            animation: none;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .account-stats {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-bottom: 16px;
        }
        .stat-item {
            background: rgba(255,255,255,0.03);
            padding: 12px;
            border-radius: 8px;
        }
        .stat-label {
            font-size: 0.7rem;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 4px;
        }
        .stat-value {
            font-size: 1.1rem;
            font-weight: 600;
            color: #fff;
        }
        .stat-value.positive { color: #00ff88; }
        .stat-value.negative { color: #ff4757; }
        .positions-section {
            border-top: 1px solid rgba(255,255,255,0.08);
            padding-top: 12px;
        }
        .positions-header {
            font-size: 0.7rem;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
        }
        .positions-list {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }
        .position-row {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 6px 8px;
            background: rgba(255,255,255,0.02);
            border-radius: 6px;
        }
        .position-details {
            font-size: 0.8rem;
            color: #aaa;
            flex: 1;
        }
        .position-pnl {
            font-size: 0.8rem;
            font-weight: 600;
        }
        .position-pnl.positive { color: #00ff88; }
        .position-pnl.negative { color: #ff4757; }
        .position-tag {
            display: flex;
            align-items: center;
            gap: 4px;
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 500;
        }
        .position-tag.long {
            background: rgba(0, 255, 136, 0.15);
            color: #00ff88;
        }
        .position-tag.short {
            background: rgba(255, 71, 87, 0.15);
            color: #ff4757;
        }
        .no-positions {
            color: #444;
            font-size: 0.8rem;
            font-style: italic;
        }
        .orders-list {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }
        .order-row {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 6px 8px;
            background: rgba(255,255,255,0.02);
            border-radius: 6px;
        }
        .order-row.more {
            color: #666;
            font-size: 0.75rem;
            font-style: italic;
        }
        .order-details {
            font-size: 0.8rem;
            color: #aaa;
            flex: 1;
        }
        .trade-time {
            font-size: 0.7rem;
            color: #666;
        }
        .positions-section + .positions-section {
            margin-top: 12px;
        }
        .loading {
            text-align: center;
            padding: 60px;
            color: #666;
        }
        .loading-spinner {
            width: 40px;
            height: 40px;
            border: 3px solid #222;
            border-top-color: #00ff88;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 16px;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        .api-section {
            margin-top: 32px;
            background: #14141f;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            padding: 24px;
        }
        .api-section h2 {
            font-size: 1.2rem;
            margin-bottom: 20px;
            color: #fff;
        }
        .api-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 16px;
        }
        @media (max-width: 768px) {
            .api-grid { grid-template-columns: 1fr; }
        }
        .api-card {
            background: rgba(255,255,255,0.03);
            border-radius: 8px;
            padding: 16px;
        }
        .api-card h3 {
            font-size: 0.9rem;
            color: #00ff88;
            margin-bottom: 8px;
        }
        .api-endpoint {
            font-family: monospace;
            font-size: 0.85rem;
            background: rgba(0,0,0,0.3);
            padding: 8px 12px;
            border-radius: 6px;
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .api-method {
            font-size: 0.7rem;
            font-weight: 600;
            padding: 2px 6px;
            border-radius: 4px;
        }
        .api-method.get { background: #00ff88; color: #000; }
        .api-method.ws { background: #9b59b6; color: #fff; }
        .api-desc {
            font-size: 0.8rem;
            color: #888;
        }
        .api-sample {
            margin-top: 12px;
            background: rgba(0,0,0,0.4);
            border-radius: 6px;
            padding: 12px;
            font-family: monospace;
            font-size: 0.75rem;
            color: #aaa;
            max-height: 200px;
            overflow-y: auto;
            white-space: pre-wrap;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>Lighter Broadcaster</h1>
        <div class="header-stats">
            <div class="header-stat">
                <div class="header-stat-label">Total Equity</div>
                <div class="header-stat-value" id="total-equity">$0.00</div>
            </div>
            <div class="header-stat">
                <div class="header-stat-label">Total PnL</div>
                <div class="header-stat-value" id="total-pnl">$0.00</div>
            </div>
            <div class="header-stat">
                <div class="header-stat-label">Accounts</div>
                <div class="header-stat-value" id="accounts-count">0/0</div>
            </div>
        </div>
    </div>
    
    <div class="accounts-grid" id="accounts-container">
        <div class="loading">
            <div class="loading-spinner"></div>
            <p>Loading accounts...</p>
        </div>
    </div>
    
    <div class="api-section">
        <h2>API Endpoints</h2>
        <div class="api-grid">
            <div class="api-card">
                <h3>Portfolio Data</h3>
                <div class="api-endpoint"><span class="api-method get">GET</span>/api/portfolio</div>
                <div class="api-desc">Returns all accounts with equity, PnL, positions, volume 24H. Auto-refreshes every 2s.</div>
                <div class="api-sample" id="portfolio-sample">Loading...</div>
            </div>
            <div class="api-card">
                <h3>All Accounts</h3>
                <div class="api-endpoint"><span class="api-method get">GET</span>/api/accounts</div>
                <div class="api-desc">Raw cached account data from Lighter API.</div>
                <div class="api-sample" id="accounts-sample">Loading...</div>
            </div>
            <div class="api-card">
                <h3>Service Status</h3>
                <div class="api-endpoint"><span class="api-method get">GET</span>/api/status</div>
                <div class="api-desc">Polling status, WebSocket connection, cache stats.</div>
                <div class="api-sample" id="status-sample">Loading...</div>
            </div>
            <div class="api-card">
                <h3>WebSocket Stream</h3>
                <div class="api-endpoint"><span class="api-method ws">WS</span>/ws</div>
                <div class="api-desc">Real-time updates from Lighter. Connect with ws://host/ws</div>
                <div class="api-sample">Messages:
- initial_data: Full cache on connect
- lighter_update: Real-time updates

Example:
{"type": "lighter_update", "data": {...}}</div>
            </div>
            <div class="api-card">
                <h3>Single Account</h3>
                <div class="api-endpoint"><span class="api-method get">GET</span>/api/accounts/{index}</div>
                <div class="api-desc">Get specific account by index (e.g. /api/accounts/634023)</div>
            </div>
            <div class="api-card">
                <h3>Health Check</h3>
                <div class="api-endpoint"><span class="api-method get">GET</span>/health</div>
                <div class="api-desc">Simple health check for monitoring.</div>
                <div class="api-sample" id="health-sample">Loading...</div>
            </div>
        </div>
    </div>
    
    <script>
        function formatMoney(value) {
            const num = parseFloat(value) || 0;
            return '$' + num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        }
        
        function formatAccountName(name) {
            const match = name.match(/Lighter_(\\d+)_([a-fA-F0-9]+)/);
            if (match) {
                return 'Lighter ' + match[1] + ' (' + match[2] + ')';
            }
            return name;
        }
        
        function getPositionSymbol(marketIndex) {
            const markets = {
                '1': 'BTC', '2': 'ETH', '3': 'SOL', '4': 'AVAX', '5': 'ARB',
                '6': 'OP', '7': 'MATIC', '8': 'DOGE', '9': 'LINK', '10': 'SUI',
                '11': 'PEPE', '12': 'WIF', '13': 'NEAR', '14': 'FTM', '15': 'TIA'
            };
            return markets[String(marketIndex)] || 'MKT' + marketIndex;
        }
        
        function formatTime(timestamp) {
            if (!timestamp) return '-';
            const ts = timestamp > 10000000000 ? timestamp / 1000 : timestamp;
            const d = new Date(ts * 1000);
            return d.toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        }
        
        function renderAccounts(data) {
            const container = document.getElementById('accounts-container');
            const accounts = data.accounts || [];
            
            if (accounts.length === 0) {
                container.innerHTML = '<div class="loading"><p>No accounts configured</p></div>';
                return;
            }
            
            let html = '';
            accounts.forEach(acc => {
                const isLive = acc.is_live;
                const equity = parseFloat(acc.equity) || 0;
                const pnl = parseFloat(acc.unrealized_pnl) || 0;
                const volume24h = parseFloat(acc.volume_24h) || 0;
                const positions = acc.positions || [];
                const activeOrders = acc.active_orders || [];
                const trades = acc.trades || [];
                
                const pnlClass = pnl >= 0 ? 'positive' : 'negative';
                const badgeClass = isLive ? 'live-badge' : 'live-badge offline-badge';
                const statusText = isLive ? 'Live' : 'Offline';
                
                let positionsHtml = '';
                if (positions.length > 0) {
                    positions.forEach(pos => {
                        const size = parseFloat(pos.position || pos.signed_size || pos.size || 0);
                        const sign = parseInt(pos.sign) || (size >= 0 ? 1 : -1);
                        const signedSize = size * sign;
                        if (size !== 0) {
                            const isLong = signedSize > 0;
                            const tagClass = isLong ? 'long' : 'short';
                            const direction = isLong ? 'LONG' : 'SHORT';
                            const symbol = pos.symbol || getPositionSymbol(pos.market_id || pos.market_index);
                            const entryPrice = parseFloat(pos.avg_entry_price || pos.entry_price || 0);
                            const pnl = parseFloat(pos.unrealized_pnl || 0);
                            const pnlClass = pnl >= 0 ? 'positive' : 'negative';
                            const pnlSign = pnl >= 0 ? '+' : '';
                            positionsHtml += '<div class="position-row"><span class="position-tag ' + tagClass + '">' + symbol + ' ' + direction + '</span><span class="position-details">' + size.toFixed(4) + ' @ ' + formatMoney(entryPrice) + '</span><span class="position-pnl ' + pnlClass + '">' + pnlSign + formatMoney(pnl) + '</span></div>';
                        }
                    });
                }
                if (!positionsHtml) {
                    positionsHtml = '<span class="no-positions">Brak otwartych pozycji</span>';
                }
                
                let ordersHtml = '';
                if (activeOrders.length > 0) {
                    activeOrders.slice(0, 5).forEach(order => {
                        const symbol = getPositionSymbol(order.market_id || order.market_index);
                        const side = order.is_ask ? 'S' : 'L';
                        const sideClass = order.is_ask ? 'short' : 'long';
                        const price = parseFloat(order.price) || 0;
                        const size = parseFloat(order.remaining_base_amount || order.size) || 0;
                        ordersHtml += '<div class="order-row"><span class="position-tag ' + sideClass + '">' + symbol + ' ' + side + '</span><span class="order-details">' + size.toFixed(4) + ' @ ' + formatMoney(price) + '</span></div>';
                    });
                    if (activeOrders.length > 5) {
                        ordersHtml += '<div class="order-row more">+' + (activeOrders.length - 5) + ' więcej zleceń</div>';
                    }
                } else {
                    ordersHtml = '<span class="no-positions">Brak otwartych zleceń</span>';
                }
                
                let tradesHtml = '';
                if (trades.length > 0) {
                    trades.slice(0, 3).forEach(trade => {
                        const symbol = getPositionSymbol(trade.market_id || trade.market_index);
                        const size = parseFloat(trade.size || trade.signed_size) || 0;
                        const price = parseFloat(trade.price) || 0;
                        const isBuy = size > 0;
                        const sideClass = isBuy ? 'long' : 'short';
                        const sideText = isBuy ? 'BUY' : 'SELL';
                        tradesHtml += '<div class="order-row"><span class="position-tag ' + sideClass + '">' + symbol + ' ' + sideText + '</span><span class="order-details">' + Math.abs(size).toFixed(4) + ' @ ' + formatMoney(price) + '</span><span class="trade-time">' + formatTime(trade.timestamp) + '</span></div>';
                    });
                    if (trades.length > 3) {
                        tradesHtml += '<div class="order-row more">+' + (trades.length - 3) + ' więcej transakcji</div>';
                    }
                } else {
                    tradesHtml = '<span class="no-positions">Brak transakcji</span>';
                }
                
                html += '<div class="account-card">';
                html += '  <div class="account-header">';
                html += '    <div>';
                html += '      <div class="account-name"><span class="checkmark">&#10003;</span>' + formatAccountName(acc.name) + '</div>';
                html += '      <div class="account-index">account_' + acc.account_index + '</div>';
                html += '    </div>';
                html += '    <div class="' + badgeClass + '"><span class="dot"></span>' + statusText + '</div>';
                html += '  </div>';
                html += '  <div class="account-stats">';
                html += '    <div class="stat-item">';
                html += '      <div class="stat-label">Equity</div>';
                html += '      <div class="stat-value">' + formatMoney(equity) + '</div>';
                html += '    </div>';
                html += '    <div class="stat-item">';
                html += '      <div class="stat-label">Unrealized PnL</div>';
                html += '      <div class="stat-value ' + pnlClass + '">' + formatMoney(pnl) + '</div>';
                html += '    </div>';
                html += '    <div class="stat-item">';
                html += '      <div class="stat-label">Volume 24H</div>';
                html += '      <div class="stat-value">' + formatMoney(volume24h) + '</div>';
                html += '    </div>';
                html += '    <div class="stat-item">';
                html += '      <div class="stat-label">Active Orders</div>';
                html += '      <div class="stat-value">' + activeOrders.length + '</div>';
                html += '    </div>';
                html += '  </div>';
                const openPositionsCount = positions.filter(p => parseFloat(p.position || p.signed_size || p.size || 0) !== 0).length;
                html += '  <div class="positions-section">';
                html += '    <div class="positions-header">Otwarte Pozycje (' + openPositionsCount + ')</div>';
                html += '    <div class="positions-list">' + positionsHtml + '</div>';
                html += '  </div>';
                html += '  <div class="positions-section">';
                html += '    <div class="positions-header">Otwarte Zlecenia (' + activeOrders.length + ')</div>';
                html += '    <div class="orders-list">' + ordersHtml + '</div>';
                html += '  </div>';
                html += '  <div class="positions-section">';
                html += '    <div class="positions-header">Historia Transakcji (' + trades.length + ')</div>';
                html += '    <div class="orders-list">' + tradesHtml + '</div>';
                html += '  </div>';
                html += '</div>';
            });
            
            container.innerHTML = html;
            
            const agg = data.aggregated || {};
            document.getElementById('total-equity').textContent = formatMoney(agg.total_equity || 0);
            const totalPnl = agg.total_unrealized_pnl || 0;
            const pnlEl = document.getElementById('total-pnl');
            pnlEl.textContent = formatMoney(totalPnl);
            pnlEl.className = 'header-stat-value ' + (totalPnl >= 0 ? '' : 'negative');
            document.getElementById('accounts-count').textContent = (agg.accounts_live || 0) + '/' + (agg.accounts_total || 0);
        }
        
        async function fetchPortfolio() {
            try {
                const res = await fetch('/api/portfolio');
                const data = await res.json();
                renderAccounts(data);
            } catch (e) {
                console.error('Failed to fetch portfolio:', e);
            }
        }
        
        async function fetchApiSamples() {
            try {
                const [portfolio, accounts, status, health] = await Promise.all([
                    fetch('/api/portfolio').then(r => r.json()),
                    fetch('/api/accounts').then(r => r.json()),
                    fetch('/api/status').then(r => r.json()),
                    fetch('/health').then(r => r.json())
                ]);
                
                document.getElementById('portfolio-sample').textContent = JSON.stringify(portfolio, null, 2);
                document.getElementById('accounts-sample').textContent = JSON.stringify(accounts, null, 2);
                document.getElementById('status-sample').textContent = JSON.stringify(status, null, 2);
                document.getElementById('health-sample').textContent = JSON.stringify(health, null, 2);
            } catch (e) {
                console.error('Failed to fetch API samples:', e);
            }
        }
        
        fetchPortfolio();
        fetchApiSamples();
        setInterval(fetchPortfolio, 500);
        setInterval(fetchApiSamples, 5000);
    </script>
</body>
</html>
"""
