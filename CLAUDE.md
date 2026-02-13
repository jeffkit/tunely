# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Tunely is a WebSocket tunnel framework that provides transparent reverse proxy functionality, allowing external access to internal network services through WebSocket connections. The project consists of:

- **Python SDK** (`python/`): Server SDK (embeddable in FastAPI) and client SDK with CLI
- **TypeScript SDK** (`typescript/`): Standalone client SDK and CLI
- **Admin Console** (`admin-console/`): React-based web UI for tunnel management

## Key Architecture

### Communication Protocol

The protocol is defined in both `python/tunely/protocol.py` and `typescript/src/protocol.ts`. These files must stay in sync. The protocol supports:

- **Authentication**: Token-based auth with domain registration
- **Request-Response**: Standard HTTP forwarding through WebSocket messages
- **SSE Streaming**: Server-Sent Events support (v1.1 protocol) with `stream_start`, `stream_chunk`, `stream_end` messages
- **Heartbeat**: Ping/pong keepalive mechanism

### Python Implementation Structure

```
python/tunely/
├── protocol.py          # Message definitions (Pydantic models)
├── server.py            # TunnelServer SDK (FastAPI router)
├── client.py            # TunnelClient SDK
├── config.py            # Configuration (Pydantic Settings)
├── database.py          # SQLAlchemy async database manager
├── repository.py        # Data access layer
├── models.py            # SQLAlchemy ORM models
├── cli.py               # Click-based CLI tool
└── app.py               # Standalone FastAPI app factory
```

Key components:
- **TunnelServer**: Main server class that manages WebSocket connections, handles HTTP forwarding, and maintains tunnel state
- **TunnelManager**: Manages active connections and pending requests (in-memory state)
- **ActiveConnection**: Represents a connected tunnel client with websocket, tunnel_id, domain
- **PendingRequest / PendingStreamRequest**: Futures/queues for tracking requests waiting for responses

### TypeScript Implementation Structure

```
typescript/src/
├── protocol.ts          # Message definitions (TypeScript interfaces)
├── client.ts            # TunnelClient SDK
├── cli.ts               # Commander-based CLI tool
└── index.ts             # Exports
```

### Database and Migrations

- Uses **Alembic** for migrations (located in `python/alembic/`)
- SQLAlchemy async with support for SQLite (default), MySQL, PostgreSQL
- Default database: `sqlite+aiosqlite:///./data/tunnels.db`
- When modifying models in `models.py`, create new migration: `alembic revision --autogenerate -m "description"`
- Apply migrations: `alembic upgrade head`

## Common Commands

### Python Development

```bash
cd python

# Install in editable mode
pip install -e .

# Install with dev dependencies
pip install -e ".[dev]"

# Run linting (ruff)
ruff check tunely

# Run tests
pytest

# Run specific test file
pytest tests/test_server.py

# Run tests with coverage
pytest --cov=tunely

# Database migrations
alembic revision --autogenerate -m "description"
alembic upgrade head

# Run example showcase
cd examples && python demo.py

# Run standalone server
tunely server --port 8080

# Run CLI client
tunely connect --server ws://localhost:8080/ws/tunnel --token tun_xxx --target http://localhost:8090
```

### TypeScript Development

```bash
cd typescript

# Install dependencies
pnpm install

# Build
pnpm build

# Watch mode
pnpm dev

# Run tests
pnpm test

# Run CLI (after building)
node dist/cli.js connect --server ws://localhost:8080/ws/tunnel --token tun_xxx --target http://localhost:8090
```

### Admin Console

```bash
cd admin-console

# Install dependencies
pnpm install

# Dev server
pnpm dev

# Build for production
pnpm build

# Preview production build
pnpm preview

# Run tests
pnpm test

# Type checking
pnpm typecheck
```

## Important Implementation Details

### SSE (Server-Sent Events) Flow

When the client detects `Content-Type: text/event-stream`:
1. Client sends `StreamStartMessage` with status and headers
2. Client sends multiple `StreamChunkMessage` for each SSE data chunk
3. Client sends `StreamEndMessage` when stream completes

On the server side, use `TunnelServer.forward_stream()` which returns an `AsyncIterator` yielding `StreamStartMessage`, `StreamChunkMessage`, or `StreamEndMessage`.

### Connection Protection

By default, the server prevents duplicate connections with the same token. A client can use the `--force` flag (CLI) or `force: true` (SDK) to disconnect existing connections and establish a new one.

### Request Matching

The `id` field in messages is critical for matching requests to responses:
- Server generates unique request ID and sends in `TunnelRequest`
- Client must return the same ID in `TunnelResponse` or stream messages
- Server uses this ID to resolve the `asyncio.Future` or push to the `asyncio.Queue`

### Error Handling

Common error scenarios:
- **Invalid token**: Return `AuthErrorMessage` with code `auth_failed`
- **Target service unavailable**: Return `TunnelResponse` with status 503 and error message
- **Timeout**: Return `TunnelResponse` with status 504
- **Connection lost**: Server cleans up pending requests after timeout

## Testing Strategy

- Python tests use `pytest` with `pytest-asyncio` for async tests
- TypeScript tests use `vitest`
- Admin console uses `vitest` with `@testing-library/react`
- Repository tests mock database with SQLite in-memory
- Server tests use WebSocket test clients

## Protocol Synchronization

When updating the protocol:
1. Update `python/tunely/protocol.py` (Pydantic models)
2. Update `typescript/src/protocol.ts` (TypeScript interfaces)
3. Update `docs/PROTOCOL.md` documentation
4. Increment protocol version in both files

## Version Information

- Python package version: `0.2.0` (in `python/pyproject.toml`)
- TypeScript package version: `0.2.1` (in `typescript/package.json`)
- Current protocol version: `1.1` (SSE support)
