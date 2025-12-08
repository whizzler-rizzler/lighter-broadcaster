# Lighter Broadcaster Service

A Python-based broadcaster service that acts as a data intermediary for Lighter.xyz trading platform.

## Overview

This service monitors Lighter.xyz accounts using REST API polling (2x per second) and WebSocket connections, then redistributes the data through its own REST API and WebSocket endpoints with rate limiting and caching.

## Architecture

```
Backend/
  __init__.py
  api.py              - FastAPI application with REST endpoints and WebSocket server
  cache.py            - In-memory caching layer with TTL support
  config.py           - Configuration and environment variable loading
  lighter_client.py   - Lighter SDK wrapper for REST API polling
  latency.py          - Latency tracking utilities
  supabase_client.py  - Supabase integration for data persistence
  websocket_client.py - WebSocket client for real-time Lighter updates
  websocket_server.py - WebSocket server for broadcasting to clients
mFrontend/            - React frontend (Vite)
main.py               - Application entry point
supabase_schema.sql   - SQL schema for Supabase tables
```

## Features

- **REST API Polling**: Polls Lighter accounts at configurable intervals (default: 0.5s)
- **Active Orders Polling**: Fetches active orders for each account every 2 seconds
- **WebSocket Client**: Real-time updates from Lighter.xyz via proxy (using aiohttp-socks)
- **Caching**: In-memory cache with configurable TTL
- **REST API**: Serves cached account data with rate limiting
- **WebSocket Broadcasting**: Pushes updates to connected clients
- **Status Dashboard**: Visual display of connections, positions, and orders
- **Proxy Support**: Both REST API and WebSocket connections use configured proxies
- **Supabase Persistence**: Optional data persistence to Supabase (snapshots, positions, orders, trades)
- **React Frontend**: Modern dashboard built with Vite/React

## API Endpoints

- `GET /` - React dashboard
- `GET /health` - Health check
- `GET /api/status` - Service status and metrics
- `GET /api/accounts` - All cached accounts
- `GET /api/accounts/{index}` - Specific account data
- `GET /api/history/accounts/{index}` - Account historical snapshots (Supabase)
- `GET /api/history/trades/{index}` - Trade history (Supabase)
- `GET /api/supabase/status` - Supabase connection status
- `WS /ws` - WebSocket for real-time updates

## Configuration

Environment variables for account configuration follow this pattern:
- `Lighter_{N}_{ID}_Account_Index` - Account index
- `Lighter_{N}_{ID}_API_KEY_Index` - API key index
- `Lighter_{N}_{ID}_PRIVATE` - Private key
- `Lighter_{N}_{ID}_PUBLIC` - Public key
- `Lighter_{N}_PROXY_{M}_URL` - Optional proxy URL

Other settings:
- `LIGHTER_BASE_URL` - Lighter API URL (default: mainnet)
- `LIGHTER_WS_URL` - Lighter WebSocket URL
- `POLL_INTERVAL` - Polling interval in seconds (default: 0.5)
- `CACHE_TTL` - Cache TTL in seconds (default: 5)
- `RATE_LIMIT` - API rate limit (default: 100/minute)

Supabase settings (optional):
- `Supabase_Url` - Supabase project URL
- `Supabase_service_role` - Supabase service role key (for server-side access)

## Deployment

Configured for Render.com deployment via GitHub integration. See `render.yaml` for configuration.

## Running Locally

```bash
python main.py
```

The service will start on port 5000 by default.
