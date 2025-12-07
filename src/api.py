import asyncio
import logging
import time
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
from src.latency import latency_tracker

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
        
        channel = data.get("channel", "")
        msg_type = data.get("type", "")
        
        if "account_all_orders" in channel:
            parts = channel.replace("account_all_orders:", "").replace("account_all_orders/", "")
            try:
                account_id = int(parts)
                orders = data.get("orders", [])
                await cache.set(f"ws_orders:{account_id}", {
                    "orders": orders,
                    "timestamp": time.time()
                }, ttl=120)
                logger.info(f"Cached {len(orders)} orders for account {account_id}")
            except (ValueError, TypeError) as e:
                logger.error(f"Failed to parse orders channel {channel}: {e}")
        
        elif "account_all_positions" in channel:
            parts = channel.replace("account_all_positions:", "").replace("account_all_positions/", "")
            try:
                account_id = int(parts)
                positions = data.get("positions", [])
                await cache.set(f"ws_positions:{account_id}", {
                    "positions": positions,
                    "timestamp": time.time()
                }, ttl=120)
                if positions:
                    logger.debug(f"Cached {len(positions)} positions for account {account_id}")
            except (ValueError, TypeError):
                pass
        
        elif "account_all_trades" in channel:
            parts = channel.replace("account_all_trades:", "").replace("account_all_trades/", "")
            try:
                account_id = int(parts)
                trades = data.get("trades", {})
                volumes = {
                    "total_volume": data.get("total_volume"),
                    "monthly_volume": data.get("monthly_volume"),
                    "weekly_volume": data.get("weekly_volume"),
                    "daily_volume": data.get("daily_volume")
                }
                await cache.set(f"ws_trades:{account_id}", {
                    "trades": trades,
                    "volumes": volumes,
                    "timestamp": time.time()
                }, ttl=120)
            except (ValueError, TypeError):
                pass
        
        elif "account_index" in data:
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

@app.get("/api/latency")
async def get_latency():
    cached_data = await cache.get_all()
    live_accounts = sum(1 for k, v in cached_data.items() 
                       if k.startswith("account:") and 
                       (time.time() - v.get("data", v).get("last_update", 0)) < 10)
    
    latency_tracker.set_account_stats(
        active=live_accounts,
        total=len(settings.accounts),
        clients=manager.connection_count
    )
    latency_tracker.set_ws_status(ws_client.is_connected)
    
    return latency_tracker.get_metrics()

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
            account_index = account_data.get("account_index")
            
            ws_orders_key = f"ws_orders:{account_index}"
            ws_orders_entry = cached_data.get(ws_orders_key, {})
            ws_orders_inner = ws_orders_entry.get("data", ws_orders_entry) if ws_orders_entry else {}
            ws_orders_raw = ws_orders_inner.get("orders", []) if isinstance(ws_orders_inner, dict) else []
            
            if isinstance(ws_orders_raw, dict):
                ws_orders = []
                for market_orders in ws_orders_raw.values():
                    if isinstance(market_orders, list):
                        ws_orders.extend(market_orders)
            else:
                ws_orders = ws_orders_raw if isinstance(ws_orders_raw, list) else []
            
            active_orders = ws_orders if ws_orders else (account_data.get("active_orders", []) or [])
            
            ws_trades_key = f"ws_trades:{account_index}"
            ws_trades_entry = cached_data.get(ws_trades_key, {})
            ws_trades = ws_trades_entry.get("data", ws_trades_entry) if ws_trades_entry else {}
            
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
                        pos_size = float(pos.get("position", 0) or 0)
                        if pos_size != 0:
                            positions.append(pos)
                    
                    if equity > 0:
                        margin_ratio = margin_used / equity
                
                trades = raw_data.get("trades", []) or []
                
                day_ago = now - 86400
                for trade in trades:
                    trade_ts = float(trade.get("timestamp", 0) or 0)
                    trade_time = trade_ts / 1000 if trade_ts > 10000000000 else trade_ts
                    if trade_time >= day_ago:
                        size = abs(float(trade.get("size", 0) or 0))
                        price = float(trade.get("price", 0) or 0)
                        volume_24h += size * price
            
            ws_volumes = ws_trades.get("volumes", {}) if ws_trades else {}
            ws_trades_list = ws_trades.get("trades", {}) if ws_trades else {}
            
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
                "volume_24h": ws_volumes.get("daily_volume") or volume_24h,
                "total_volume": ws_volumes.get("total_volume"),
                "monthly_volume": ws_volumes.get("monthly_volume"),
                "weekly_volume": ws_volumes.get("weekly_volume"),
                "positions": positions,
                "active_orders": active_orders,
                "trades": trades,
                "ws_trades": ws_trades_list
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
        .latency-section {
            margin-top: 32px;
            background: #14141f;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            padding: 24px;
        }
        .latency-section h2 {
            font-size: 1.2rem;
            margin-bottom: 20px;
            color: #ff6b35;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .latency-section h2::before {
            content: '';
            display: inline-block;
            width: 4px;
            height: 20px;
            background: #ff6b35;
            border-radius: 2px;
        }
        .latency-panel {
            background: rgba(255,255,255,0.02);
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 16px;
        }
        .latency-panel-header {
            font-size: 0.75rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .latency-panel-header .icon { font-size: 1rem; }
        .latency-metrics {
            display: grid;
            grid-template-columns: repeat(6, 1fr);
            gap: 12px;
        }
        @media (max-width: 1200px) {
            .latency-metrics { grid-template-columns: repeat(3, 1fr); }
        }
        @media (max-width: 768px) {
            .latency-metrics { grid-template-columns: repeat(2, 1fr); }
        }
        .latency-metric {
            background: rgba(0,0,0,0.3);
            border-radius: 8px;
            padding: 12px;
            text-align: center;
        }
        .latency-metric-label {
            font-size: 0.65rem;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 4px;
        }
        .latency-metric-value {
            font-size: 1.3rem;
            font-weight: 700;
            color: #00ff88;
        }
        .latency-metric-value.warning { color: #ffa502; }
        .latency-metric-value.error { color: #ff4757; }
        .latency-metric-value.neutral { color: #aaa; }
        .latency-metric-unit {
            font-size: 0.7rem;
            color: #666;
            margin-left: 2px;
        }
        .latency-charts {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-top: 16px;
        }
        @media (max-width: 900px) {
            .latency-charts { grid-template-columns: 1fr; }
        }
        .latency-chart {
            background: rgba(0,0,0,0.2);
            border-radius: 8px;
            padding: 16px;
        }
        .latency-chart-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }
        .latency-chart-title {
            font-size: 0.85rem;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .latency-chart-stats {
            display: flex;
            gap: 16px;
            font-size: 0.7rem;
        }
        .latency-chart-stat { color: #666; }
        .latency-chart-stat span { color: #fff; font-weight: 600; }
        .latency-bars {
            display: flex;
            align-items: flex-end;
            gap: 3px;
            height: 60px;
            padding: 4px 0;
        }
        .latency-bar {
            flex: 1;
            min-width: 8px;
            border-radius: 2px 2px 0 0;
            transition: height 0.3s ease;
        }
        .latency-bar.green { background: linear-gradient(to top, #00cc6a, #00ff88); }
        .latency-bar.yellow { background: linear-gradient(to top, #cc9900, #ffcc00); }
        .latency-bar.red { background: linear-gradient(to top, #cc3333, #ff4757); }
        .latency-status-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 16px;
            background: rgba(0,0,0,0.3);
            border-radius: 8px;
            margin-top: 16px;
            font-size: 0.8rem;
        }
        .latency-status-item {
            display: flex;
            align-items: center;
            gap: 6px;
            color: #888;
        }
        .latency-status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }
        .latency-status-dot.connected { background: #00ff88; box-shadow: 0 0 8px #00ff8866; }
        .latency-status-dot.disconnected { background: #ff4757; }
        .latency-status-value { color: #fff; font-weight: 500; }
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
    
    <div class="latency-section">
        <h2>LATENCY MONITOR</h2>
        
        <div class="latency-panel">
            <div class="latency-panel-header"><span class="icon">&#9881;</span> FRONTEND POLLING</div>
            <div class="latency-metrics">
                <div class="latency-metric">
                    <div class="latency-metric-label">WS Interval (avg)</div>
                    <div class="latency-metric-value neutral" id="ws-interval">N/A</div>
                </div>
                <div class="latency-metric">
                    <div class="latency-metric-label">Time Since WS</div>
                    <div class="latency-metric-value neutral" id="time-since-ws">N/A</div>
                </div>
                <div class="latency-metric">
                    <div class="latency-metric-label">REST Interval (avg)</div>
                    <div class="latency-metric-value" id="rest-interval">0<span class="latency-metric-unit">ms</span></div>
                </div>
                <div class="latency-metric">
                    <div class="latency-metric-label">Time Since REST</div>
                    <div class="latency-metric-value" id="time-since-rest">0<span class="latency-metric-unit">ms</span></div>
                </div>
                <div class="latency-metric">
                    <div class="latency-metric-label">Stats Poll Interval</div>
                    <div class="latency-metric-value" id="stats-poll-interval">0<span class="latency-metric-unit">ms</span></div>
                </div>
                <div class="latency-metric">
                    <div class="latency-metric-label">Stats Fetch Time</div>
                    <div class="latency-metric-value" id="stats-fetch-time">0<span class="latency-metric-unit">ms</span></div>
                </div>
            </div>
        </div>
        
        <div class="latency-panel">
            <div class="latency-panel-header"><span class="icon">&#9741;</span> BACKEND POLLING (Broadcaster -> Lighter API)</div>
            <div class="latency-metrics" style="grid-template-columns: repeat(5, 1fr);">
                <div class="latency-metric">
                    <div class="latency-metric-label">API Poll Rate</div>
                    <div class="latency-metric-value" id="api-poll-rate">N/A</div>
                </div>
                <div class="latency-metric">
                    <div class="latency-metric-label">Positions Age</div>
                    <div class="latency-metric-value" id="positions-age">0<span class="latency-metric-unit">ms</span></div>
                </div>
                <div class="latency-metric">
                    <div class="latency-metric-label">Balance Age</div>
                    <div class="latency-metric-value" id="balance-age">0<span class="latency-metric-unit">ms</span></div>
                </div>
                <div class="latency-metric">
                    <div class="latency-metric-label">Active Accounts</div>
                    <div class="latency-metric-value" id="active-accounts">0<span class="latency-metric-unit">/0</span></div>
                </div>
                <div class="latency-metric">
                    <div class="latency-metric-label">Connected Clients</div>
                    <div class="latency-metric-value" id="connected-clients">0</div>
                </div>
            </div>
        </div>
        
        <div class="latency-charts">
            <div class="latency-chart">
                <div class="latency-chart-header">
                    <div class="latency-chart-title"><span>&#128246;</span> WebSocket Message Intervals</div>
                    <div class="latency-chart-stats">
                        <div class="latency-chart-stat">Min: <span id="ws-min">0ms</span></div>
                        <div class="latency-chart-stat">Avg: <span id="ws-avg">0ms</span></div>
                        <div class="latency-chart-stat">Max: <span id="ws-max">0ms</span></div>
                    </div>
                </div>
                <div class="latency-chart-stat" style="margin-bottom: 8px;">Samples: <span id="ws-samples">0</span></div>
                <div class="latency-bars" id="ws-chart"></div>
            </div>
            <div class="latency-chart">
                <div class="latency-chart-header">
                    <div class="latency-chart-title"><span>&#128462;</span> REST Polling Intervals</div>
                    <div class="latency-chart-stats">
                        <div class="latency-chart-stat">Min: <span id="rest-min">0ms</span></div>
                        <div class="latency-chart-stat">Avg: <span id="rest-avg">0ms</span></div>
                        <div class="latency-chart-stat">Max: <span id="rest-max">0ms</span></div>
                    </div>
                </div>
                <div class="latency-chart-stat" style="margin-bottom: 8px;">Samples: <span id="rest-samples">0</span></div>
                <div class="latency-bars" id="rest-chart"></div>
            </div>
        </div>
        
        <div class="latency-status-bar">
            <div class="latency-status-item">
                <span class="latency-status-dot" id="ws-status-dot"></span>
                <span id="ws-status-text">WS: Disconnected</span>
                <span class="latency-status-value" id="ws-msg-count">WS msgs: 0</span>
                <span class="latency-status-value" id="rest-req-count">REST reqs: 0</span>
            </div>
            <div class="latency-status-item">
                <span>WS: <span class="latency-status-value" id="ts-ws">--:--:--</span></span>
                <span>REST: <span class="latency-status-value" id="ts-rest">--:--:--</span></span>
                <span>Stats: <span class="latency-status-value" id="ts-stats">--:--:--</span></span>
            </div>
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
        
        function formatMs(value) {
            if (value === null || value === undefined) return 'N/A';
            return Math.round(value) + 'ms';
        }
        
        function formatTimestamp(ts) {
            if (!ts) return '--:--:--';
            const d = new Date(ts * 1000);
            return d.toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        }
        
        function getLatencyClass(value, thresholds) {
            if (value === null || value === undefined) return 'neutral';
            if (value <= thresholds.good) return '';
            if (value <= thresholds.warning) return 'warning';
            return 'error';
        }
        
        function renderLatencyBars(containerId, samples, maxValue) {
            const container = document.getElementById(containerId);
            if (!container) return;
            
            if (!samples || samples.length === 0) {
                container.innerHTML = '<div style="color: #666; font-size: 0.75rem;">No data yet</div>';
                return;
            }
            
            const max = maxValue || Math.max(...samples, 1);
            let html = '';
            samples.forEach(val => {
                const height = Math.max(5, (val / max) * 100);
                let colorClass = 'green';
                if (val > 1000) colorClass = 'red';
                else if (val > 500) colorClass = 'yellow';
                html += '<div class="latency-bar ' + colorClass + '" style="height: ' + height + '%;"></div>';
            });
            container.innerHTML = html;
        }
        
        function renderLatencyMonitor(data) {
            const fp = data.frontend_polling || {};
            const bp = data.backend_polling || {};
            const ws = data.websocket || {};
            const rest = data.rest || {};
            const ts = data.timestamps || {};
            
            const wsInt = document.getElementById('ws-interval');
            wsInt.innerHTML = ws.interval_avg ? formatMs(ws.interval_avg) : 'N/A';
            wsInt.className = 'latency-metric-value ' + (ws.connected ? '' : 'neutral');
            
            const timeSinceWs = document.getElementById('time-since-ws');
            timeSinceWs.innerHTML = fp.time_since_ws !== null ? formatMs(fp.time_since_ws) : 'N/A';
            timeSinceWs.className = 'latency-metric-value ' + getLatencyClass(fp.time_since_ws, {good: 2000, warning: 5000});
            
            const restInt = document.getElementById('rest-interval');
            restInt.innerHTML = formatMs(fp.rest_interval_avg);
            restInt.className = 'latency-metric-value ' + getLatencyClass(fp.rest_interval_avg, {good: 500, warning: 1000});
            
            const timeSinceRest = document.getElementById('time-since-rest');
            timeSinceRest.innerHTML = formatMs(fp.time_since_rest);
            timeSinceRest.className = 'latency-metric-value ' + getLatencyClass(fp.time_since_rest, {good: 1000, warning: 3000});
            
            const statsInt = document.getElementById('stats-poll-interval');
            statsInt.innerHTML = formatMs(fp.stats_poll_interval);
            statsInt.className = 'latency-metric-value ' + getLatencyClass(fp.stats_poll_interval, {good: 3000, warning: 5000});
            
            const statsFetch = document.getElementById('stats-fetch-time');
            statsFetch.innerHTML = formatMs(fp.stats_fetch_time);
            statsFetch.className = 'latency-metric-value ' + getLatencyClass(fp.stats_fetch_time, {good: 200, warning: 500});
            
            const apiPoll = document.getElementById('api-poll-rate');
            apiPoll.innerHTML = bp.api_poll_rate !== null ? formatMs(bp.api_poll_rate) : 'N/A';
            apiPoll.className = 'latency-metric-value ' + getLatencyClass(bp.api_poll_rate, {good: 500, warning: 1000});
            
            const posAge = document.getElementById('positions-age');
            posAge.innerHTML = formatMs(bp.positions_age);
            posAge.className = 'latency-metric-value ' + getLatencyClass(bp.positions_age, {good: 2000, warning: 5000});
            
            const balAge = document.getElementById('balance-age');
            balAge.innerHTML = formatMs(bp.balance_age);
            balAge.className = 'latency-metric-value ' + getLatencyClass(bp.balance_age, {good: 2000, warning: 5000});
            
            document.getElementById('active-accounts').innerHTML = bp.active_accounts + '<span class="latency-metric-unit">/' + bp.total_accounts + '</span>';
            document.getElementById('connected-clients').textContent = bp.connected_clients;
            
            document.getElementById('ws-min').textContent = formatMs(ws.interval_min);
            document.getElementById('ws-avg').textContent = formatMs(ws.interval_avg);
            document.getElementById('ws-max').textContent = formatMs(ws.interval_max);
            document.getElementById('ws-samples').textContent = (ws.samples || []).length;
            
            document.getElementById('rest-min').textContent = formatMs(rest.interval_min);
            document.getElementById('rest-avg').textContent = formatMs(rest.interval_avg);
            document.getElementById('rest-max').textContent = formatMs(rest.interval_max);
            document.getElementById('rest-samples').textContent = (rest.samples || []).length;
            
            renderLatencyBars('ws-chart', ws.samples, ws.interval_max || 5000);
            renderLatencyBars('rest-chart', rest.samples, rest.interval_max || 1000);
            
            const wsDot = document.getElementById('ws-status-dot');
            wsDot.className = 'latency-status-dot ' + (ws.connected ? 'connected' : 'disconnected');
            document.getElementById('ws-status-text').textContent = ws.connected ? 'WS: Connected' : 'WS: Disconnected';
            document.getElementById('ws-msg-count').textContent = 'WS msgs: ' + ws.message_count;
            document.getElementById('rest-req-count').textContent = 'REST reqs: ' + rest.request_count;
            
            document.getElementById('ts-ws').textContent = formatTimestamp(ts.ws);
            document.getElementById('ts-rest').textContent = formatTimestamp(ts.rest);
            document.getElementById('ts-stats').textContent = formatTimestamp(ts.stats);
        }
        
        async function fetchLatency() {
            try {
                const start = performance.now();
                const res = await fetch('/api/latency');
                const data = await res.json();
                const fetchTime = performance.now() - start;
                renderLatencyMonitor(data);
            } catch (e) {
                console.error('Failed to fetch latency:', e);
            }
        }
        
        fetchPortfolio();
        fetchApiSamples();
        fetchLatency();
        setInterval(fetchPortfolio, 500);
        setInterval(fetchApiSamples, 5000);
        setInterval(fetchLatency, 2000);
    </script>
</body>
</html>
"""
