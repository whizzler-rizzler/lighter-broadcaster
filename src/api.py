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
    total_open_orders = 0
    total_trades = 0
    
    for key, value in cached_data.items():
        if key.startswith("account:"):
            account_data = value.get("data", {})
            raw_data = account_data.get("raw_data", {})
            
            equity = 0
            available_balance = 0
            unrealized_pnl = 0
            margin_used = 0
            margin_ratio = 0
            positions = []
            open_orders = []
            trades = []
            
            if isinstance(raw_data, dict):
                if "balances" in raw_data and raw_data["balances"]:
                    balance = raw_data["balances"][0] if isinstance(raw_data["balances"], list) else raw_data["balances"]
                    equity = float(balance.get("equity", 0) or 0)
                    available_balance = float(balance.get("available_for_trade", 0) or 0)
                    unrealized_pnl = float(balance.get("unrealised_pnl", 0) or 0)
                    margin_used = float(balance.get("initial_margin", 0) or 0)
                    margin_ratio = float(balance.get("margin_ratio", 0) or 0)
                
                positions = raw_data.get("positions", []) or []
                open_orders = raw_data.get("open_orders", []) or []
                trades = raw_data.get("trades", []) or []
            
            account_entry = {
                "account_index": str(account_data.get("account_index", "")),
                "name": account_data.get("account_name", ""),
                "ws_connected": ws_client.is_connected,
                "last_update": int(time.time()),
                "last_disconnect_reason": None,
                "equity": equity,
                "available_balance": available_balance,
                "unrealized_pnl": unrealized_pnl,
                "margin_used": margin_used,
                "margin_ratio": margin_ratio,
                "positions": positions,
                "open_orders": open_orders,
                "trades": trades
            }
            accounts_list.append(account_entry)
            
            total_equity += equity
            total_unrealized_pnl += unrealized_pnl
            total_margin_used += margin_used
            total_positions += len(positions)
            total_open_orders += len(open_orders)
            total_trades += len(trades)
    
    return {
        "accounts": accounts_list,
        "aggregated": {
            "total_equity": total_equity,
            "total_unrealized_pnl": total_unrealized_pnl,
            "total_margin_used": total_margin_used,
            "total_positions": total_positions,
            "total_open_orders": total_open_orders,
            "total_trades": total_trades,
            "accounts_connected": len([a for a in accounts_list if a["ws_connected"]]),
            "accounts_total": len(settings.accounts)
        },
        "timestamp": int(time.time())
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
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: #fff;
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 {
            text-align: center;
            margin-bottom: 30px;
            font-size: 2.5rem;
            background: linear-gradient(90deg, #00d9ff, #00ff88);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .status-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .status-card {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 16px;
            padding: 24px;
            backdrop-filter: blur(10px);
        }
        .status-card h3 {
            font-size: 0.9rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
        }
        .status-value {
            font-size: 2rem;
            font-weight: bold;
        }
        .status-value.connected { color: #00ff88; }
        .status-value.disconnected { color: #ff4757; }
        .accounts-section {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 20px;
        }
        .accounts-section h2 {
            margin-bottom: 20px;
            color: #00d9ff;
        }
        .account-row {
            display: flex;
            justify-content: space-between;
            padding: 15px;
            background: rgba(0,0,0,0.2);
            border-radius: 8px;
            margin-bottom: 10px;
        }
        .account-name { font-weight: bold; }
        .account-data { color: #888; font-size: 0.9rem; }
        .log-section {
            background: rgba(0,0,0,0.3);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 16px;
            padding: 24px;
            max-height: 300px;
            overflow-y: auto;
        }
        .log-section h2 { margin-bottom: 15px; color: #00d9ff; }
        .log-entry {
            font-family: monospace;
            font-size: 0.85rem;
            padding: 8px;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            color: #aaa;
        }
        .log-entry.update { color: #00ff88; }
        .log-entry.error { color: #ff4757; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Lighter Broadcaster</h1>
        
        <div class="status-grid">
            <div class="status-card">
                <h3>WebSocket Status</h3>
                <div class="status-value" id="ws-status">Connecting...</div>
            </div>
            <div class="status-card">
                <h3>Connected Clients</h3>
                <div class="status-value" id="client-count">0</div>
            </div>
            <div class="status-card">
                <h3>Accounts Monitored</h3>
                <div class="status-value" id="account-count">0</div>
            </div>
            <div class="status-card">
                <h3>Cache Entries</h3>
                <div class="status-value" id="cache-count">0</div>
            </div>
        </div>
        
        <div class="accounts-section">
            <h2>Account Data</h2>
            <div id="accounts-container">
                <p style="color: #888;">Loading account data...</p>
            </div>
        </div>
        
        <div class="log-section">
            <h2>Live Updates</h2>
            <div id="log-container"></div>
        </div>
    </div>
    
    <script>
        let ws;
        const maxLogs = 50;
        
        function connect() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
            
            ws.onopen = () => {
                document.getElementById('ws-status').textContent = 'Connected';
                document.getElementById('ws-status').className = 'status-value connected';
                addLog('Connected to broadcaster', 'update');
            };
            
            ws.onclose = () => {
                document.getElementById('ws-status').textContent = 'Disconnected';
                document.getElementById('ws-status').className = 'status-value disconnected';
                addLog('Disconnected from broadcaster', 'error');
                setTimeout(connect, 3000);
            };
            
            ws.onerror = (err) => {
                addLog('WebSocket error', 'error');
            };
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                handleMessage(data);
            };
        }
        
        function handleMessage(data) {
            if (data.type === 'initial_data') {
                updateAccountsDisplay(data.data);
                addLog('Received initial data', 'update');
            } else if (data.type === 'lighter_update') {
                addLog(`Update: ${JSON.stringify(data.data).substring(0, 100)}...`, 'update');
            }
        }
        
        function updateAccountsDisplay(data) {
            const container = document.getElementById('accounts-container');
            let html = '';
            
            for (const [key, value] of Object.entries(data)) {
                if (key.startsWith('account:')) {
                    const accountData = value.data;
                    html += `
                        <div class="account-row">
                            <div>
                                <div class="account-name">Account ${accountData.account_index}</div>
                                <div class="account-data">${accountData.account_name}</div>
                            </div>
                            <div class="account-data">Age: ${value.age.toFixed(1)}s</div>
                        </div>
                    `;
                }
            }
            
            container.innerHTML = html || '<p style="color: #888;">No accounts loaded yet</p>';
            document.getElementById('cache-count').textContent = Object.keys(data).length;
        }
        
        function addLog(message, type = '') {
            const container = document.getElementById('log-container');
            const entry = document.createElement('div');
            entry.className = `log-entry ${type}`;
            entry.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
            container.insertBefore(entry, container.firstChild);
            
            while (container.children.length > maxLogs) {
                container.removeChild(container.lastChild);
            }
        }
        
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                document.getElementById('client-count').textContent = data.broadcast_clients;
                document.getElementById('account-count').textContent = data.accounts_configured;
                document.getElementById('cache-count').textContent = data.cache.valid_entries;
            } catch (e) {
                console.error('Failed to fetch status:', e);
            }
        }
        
        connect();
        fetchStatus();
        setInterval(fetchStatus, 5000);
    </script>
</body>
</html>
"""
