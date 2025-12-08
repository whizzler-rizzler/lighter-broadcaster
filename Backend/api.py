import asyncio
import logging
import time
import os
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from Backend.config import settings
from Backend.cache import cache
from Backend.lighter_client import lighter_client
from Backend.websocket_client import ws_client
from Backend.websocket_server import manager
from Backend.latency import latency_tracker
from Backend.supabase_client import supabase_client
from Backend.error_collector import error_collector

logger = logging.getLogger(__name__)

def _get_exchange_for_account_id(account_id: int) -> str:
    for account in settings.accounts:
        if account.account_index == account_id:
            return account.exchange
    return "lighter"

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Lighter Broadcaster", version="1.0.0")

FRONTEND_DIR = Path(__file__).parent.parent / "mFrontend" / "dist"

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
    
    supabase_client.initialize()
    if supabase_client.is_initialized:
        logger.info("Supabase persistence enabled")
    
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
                new_trades = data.get("trades", {})
                volumes = {
                    "total_volume": data.get("total_volume"),
                    "monthly_volume": data.get("monthly_volume"),
                    "weekly_volume": data.get("weekly_volume"),
                    "daily_volume": data.get("daily_volume")
                }
                
                MAX_TRADES_PER_MARKET = 500
                
                existing = await cache.get(f"ws_trades:{account_id}")
                existing_data = existing.get("data", existing) if existing else {}
                existing_trades = existing_data.get("trades", {}) if existing_data else {}
                
                if not isinstance(existing_trades, dict):
                    existing_trades = {}
                if not isinstance(new_trades, dict):
                    new_trades = {}
                
                exchange = _get_exchange_for_account_id(account_id)
                
                for market_id, market_trades in new_trades.items():
                    if not isinstance(market_trades, list):
                        continue
                    
                    if market_id in existing_trades:
                        if not isinstance(existing_trades[market_id], list):
                            existing_trades[market_id] = []
                        
                        existing_ids = set()
                        for t in existing_trades[market_id]:
                            trade_key = t.get("id") or t.get("trade_id") or t.get("timestamp")
                            if trade_key:
                                existing_ids.add(trade_key)
                        
                        for trade in market_trades:
                            trade_key = trade.get("id") or trade.get("trade_id") or trade.get("timestamp")
                            if trade_key and trade_key not in existing_ids:
                                existing_trades[market_id].append(trade)
                                existing_ids.add(trade_key)
                                if supabase_client.is_initialized:
                                    asyncio.create_task(supabase_client.save_trade(account_id, trade, exchange))
                        
                        if len(existing_trades[market_id]) > MAX_TRADES_PER_MARKET:
                            existing_trades[market_id] = existing_trades[market_id][-MAX_TRADES_PER_MARKET:]
                    else:
                        existing_trades[market_id] = market_trades[-MAX_TRADES_PER_MARKET:]
                        if supabase_client.is_initialized:
                            for trade in market_trades:
                                asyncio.create_task(supabase_client.save_trade(account_id, trade, exchange))
                
                await cache.set(f"ws_trades:{account_id}", {
                    "trades": existing_trades,
                    "volumes": volumes,
                    "timestamp": time.time()
                }, ttl=3600)
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
            ws_trades_raw = ws_trades.get("trades", {}) if ws_trades else {}
            
            if isinstance(ws_trades_raw, dict):
                ws_trades_list = []
                for market_trades in ws_trades_raw.values():
                    if isinstance(market_trades, list):
                        ws_trades_list.extend(market_trades)
            else:
                ws_trades_list = ws_trades_raw if isinstance(ws_trades_raw, list) else []
            
            all_trades = ws_trades_list if ws_trades_list else trades
            
            account_index_val = account_data.get("account_index", 0)
            exchange = _get_exchange_for_account_id(account_index_val)
            
            account_entry = {
                "account_index": str(account_index_val),
                "name": account_data.get("account_name", ""),
                "exchange": exchange,
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
                "trades": all_trades
            }
            accounts_list.append(account_entry)
            
            total_equity += equity
            total_unrealized_pnl += unrealized_pnl
            total_margin_used += margin_used
            total_positions += len(positions)
            total_active_orders += len(active_orders)
            total_trades += len(all_trades)
    
    def get_account_sort_key(account):
        name = account.get("name", "")
        import re
        match = re.search(r'_(\d+)_', name)
        if match:
            return int(match.group(1))
        return 999
    
    accounts_list.sort(key=get_account_sort_key)
    
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

@app.get("/api/ws/positions")
@limiter.limit(settings.rate_limit)
async def get_ws_positions(request: Request):
    """Get all positions from WebSocket cache for all accounts"""
    cached_data = await cache.get_all()
    result = []
    
    for key, value in cached_data.items():
        if key.startswith("ws_positions:"):
            account_id = key.replace("ws_positions:", "")
            data = value.get("data", value)
            positions = data.get("positions", [])
            timestamp = data.get("timestamp", 0)
            result.append({
                "account_index": account_id,
                "positions": positions,
                "timestamp": timestamp,
                "age_seconds": round(time.time() - timestamp, 2) if timestamp else None
            })
    
    return {"accounts": result, "total_accounts": len(result)}


@app.get("/api/ws/positions/{account_index}")
@limiter.limit(settings.rate_limit)
async def get_ws_positions_by_account(request: Request, account_index: int):
    """Get positions from WebSocket cache for specific account"""
    cached = await cache.get(f"ws_positions:{account_index}")
    if not cached:
        raise HTTPException(status_code=404, detail=f"No positions data for account {account_index}")
    
    data = cached.get("data", cached)
    positions = data.get("positions", [])
    timestamp = data.get("timestamp", 0)
    
    return {
        "account_index": account_index,
        "positions": positions,
        "timestamp": timestamp,
        "age_seconds": round(time.time() - timestamp, 2) if timestamp else None
    }


@app.get("/api/ws/orders")
@limiter.limit(settings.rate_limit)
async def get_ws_orders(request: Request):
    """Get all orders from WebSocket cache for all accounts"""
    cached_data = await cache.get_all()
    result = []
    
    for key, value in cached_data.items():
        if key.startswith("ws_orders:"):
            account_id = key.replace("ws_orders:", "")
            data = value.get("data", value)
            orders = data.get("orders", [])
            timestamp = data.get("timestamp", 0)
            result.append({
                "account_index": account_id,
                "orders": orders,
                "orders_count": len(orders),
                "timestamp": timestamp,
                "age_seconds": round(time.time() - timestamp, 2) if timestamp else None
            })
    
    return {"accounts": result, "total_accounts": len(result)}


@app.get("/api/ws/orders/{account_index}")
@limiter.limit(settings.rate_limit)
async def get_ws_orders_by_account(request: Request, account_index: int):
    """Get orders from WebSocket cache for specific account"""
    cached = await cache.get(f"ws_orders:{account_index}")
    if not cached:
        raise HTTPException(status_code=404, detail=f"No orders data for account {account_index}")
    
    data = cached.get("data", cached)
    orders = data.get("orders", [])
    timestamp = data.get("timestamp", 0)
    
    return {
        "account_index": account_index,
        "orders": orders,
        "orders_count": len(orders),
        "timestamp": timestamp,
        "age_seconds": round(time.time() - timestamp, 2) if timestamp else None
    }


@app.get("/api/ws/trades")
@limiter.limit(settings.rate_limit)
async def get_ws_trades(request: Request):
    """Get all trades from WebSocket cache for all accounts"""
    cached_data = await cache.get_all()
    result = []
    
    for key, value in cached_data.items():
        if key.startswith("ws_trades:"):
            account_id = key.replace("ws_trades:", "")
            data = value.get("data", value)
            trades_raw = data.get("trades", {})
            volumes = data.get("volumes", {})
            timestamp = data.get("timestamp", 0)
            
            if isinstance(trades_raw, dict):
                trades = trades_raw
                total_trades = sum(len(t) for t in trades_raw.values() if isinstance(t, list))
            elif isinstance(trades_raw, list):
                trades = trades_raw
                total_trades = len(trades_raw)
            else:
                trades = {}
                total_trades = 0
            
            result.append({
                "account_index": account_id,
                "trades": trades,
                "trades_count": total_trades,
                "volumes": volumes,
                "timestamp": timestamp,
                "age_seconds": round(time.time() - timestamp, 2) if timestamp else None
            })
    
    return {"accounts": result, "total_accounts": len(result)}


@app.get("/api/ws/trades/{account_index}")
@limiter.limit(settings.rate_limit)
async def get_ws_trades_by_account(request: Request, account_index: int):
    """Get trades from WebSocket cache for specific account"""
    cached = await cache.get(f"ws_trades:{account_index}")
    if not cached:
        raise HTTPException(status_code=404, detail=f"No trades data for account {account_index}")
    
    data = cached.get("data", cached)
    trades_raw = data.get("trades", {})
    volumes = data.get("volumes", {})
    timestamp = data.get("timestamp", 0)
    
    if isinstance(trades_raw, dict):
        trades = trades_raw
        total_trades = sum(len(t) for t in trades_raw.values() if isinstance(t, list))
    elif isinstance(trades_raw, list):
        trades = trades_raw
        total_trades = len(trades_raw)
    else:
        trades = {}
        total_trades = 0
    
    return {
        "account_index": account_index,
        "trades": trades,
        "trades_count": total_trades,
        "volumes": volumes,
        "timestamp": timestamp,
        "age_seconds": round(time.time() - timestamp, 2) if timestamp else None
    }


@app.get("/api/ws/health")
@limiter.limit(settings.rate_limit)
async def get_ws_health(request: Request):
    """Get WebSocket connections health status"""
    return ws_client.get_all_health_status()


@app.get("/api/rest/health")
@limiter.limit(settings.rate_limit)
async def get_rest_health(request: Request):
    """Get REST API connections health status (same format as WebSocket)"""
    return lighter_client.get_all_health_status()


@app.get("/api/connections/health")
@limiter.limit(settings.rate_limit)
async def get_all_connections_health(request: Request):
    """Get combined health status for both WebSocket and REST API connections"""
    ws_health = ws_client.get_all_health_status()
    rest_health = lighter_client.get_all_health_status()
    
    return {
        "websocket": ws_health,
        "rest_api": rest_health,
        "summary": {
            "ws_connected": ws_health["connected_count"],
            "ws_total": ws_health["total_connections"],
            "rest_connected": rest_health["connected_count"],
            "rest_total": rest_health["total_connections"],
            "all_healthy": (
                ws_health["connected_count"] == ws_health["total_connections"] and
                rest_health["connected_count"] == rest_health["total_connections"]
            )
        }
    }


@app.post("/api/rest/reconnect")
@limiter.limit("10/minute")
async def force_rest_reconnect(request: Request, account_index: int = None):
    """Force reset REST API connections retry state"""
    if account_index is not None:
        success = await lighter_client.force_reconnect(account_index)
        return {"success": success, "account_index": account_index}
    else:
        count = await lighter_client.force_reconnect_all()
        return {"success": True, "reset_count": count}


@app.post("/api/connections/reconnect")
@limiter.limit("5/minute")
async def force_all_reconnect(request: Request):
    """Force reconnect all WebSocket and reset all REST connections"""
    ws_count = await ws_client.force_reconnect_all()
    rest_count = await lighter_client.force_reconnect_all()
    return {
        "success": True,
        "websocket_reconnected": ws_count,
        "rest_reset": rest_count
    }


@app.post("/api/ws/reconnect")
@limiter.limit("10/minute")
async def force_ws_reconnect(request: Request, account_index: int = None):
    """Force reconnect WebSocket connections"""
    if account_index is not None:
        success = await ws_client.force_reconnect(account_index)
        return {"success": success, "account_index": account_index}
    else:
        count = await ws_client.force_reconnect_all()
        return {"success": True, "reconnected_count": count}


@app.get("/api/history/accounts/{account_index}")
@limiter.limit(settings.rate_limit)
async def get_account_history(request: Request, account_index: int, limit: int = 100):
    """Get historical snapshots for an account from Supabase"""
    if not supabase_client.is_initialized:
        raise HTTPException(status_code=503, detail="Supabase persistence not configured")
    
    history = await supabase_client.get_account_history(account_index, limit)
    return {"account_index": account_index, "snapshots": history, "count": len(history)}


@app.get("/api/history/trades/{account_index}")
@limiter.limit(settings.rate_limit)
async def get_trade_history(request: Request, account_index: int, limit: int = 50):
    """Get historical trades for an account from Supabase"""
    if not supabase_client.is_initialized:
        raise HTTPException(status_code=503, detail="Supabase persistence not configured")
    
    trades = await supabase_client.get_recent_trades(account_index, limit)
    return {"account_index": account_index, "trades": trades, "count": len(trades)}


@app.get("/api/supabase/status")
@limiter.limit(settings.rate_limit)
async def get_supabase_status(request: Request):
    """Check Supabase connection status"""
    return {
        "initialized": supabase_client.is_initialized,
        "persistence_enabled": supabase_client.is_initialized
    }


@app.get("/api/errors")
@limiter.limit(settings.rate_limit)
async def get_errors(request: Request, limit: int = 50, source: str = None):
    """Get recent errors and error summary"""
    errors = error_collector.get_recent_errors(limit=limit, source=source)
    summary = error_collector.get_error_summary()
    return {
        "errors": errors,
        "summary": summary
    }


@app.post("/api/errors/clear")
@limiter.limit("5/minute")
async def clear_errors(request: Request):
    """Clear error log"""
    error_collector.clear()
    return {"success": True, "message": "Error log cleared"}


if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="static")


@app.get("/")
async def serve_frontend():
    """Serve React frontend"""
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file, media_type="text/html")
    return HTMLResponse("<h1>Frontend not built. Run: cd mFrontend && npm run build</h1>", status_code=503)
