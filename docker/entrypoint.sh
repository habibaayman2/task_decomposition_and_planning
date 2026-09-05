#!/usr/bin/env bash
set -euo pipefail

cd /app

export IRONBRIDGE_DB_PATH="${IRONBRIDGE_DB_PATH:-/app/db/procurement.db}"
export IRONBRIDGE_DB_ENGINE="${IRONBRIDGE_DB_ENGINE:-sqlite}"
export PYTHONPATH="/app:${PYTHONPATH:-}"

# --- 1. Build the database if it doesn't exist yet -------------------------
if [ ! -f "${IRONBRIDGE_DB_PATH}" ]; then
    echo "[entrypoint] No DB found at ${IRONBRIDGE_DB_PATH} -- building from schema.sql + seed.sql"
    python3 db/build_db.py
fi

# --- 2. Apply the state-graph + chat schema migrations (idempotent) --------
echo "[entrypoint] Applying state_graph schema migration..."
python3 -m db.migrate_state_graph || echo "[entrypoint][WARN] state_graph migration failed, continuing"

echo "[entrypoint] Applying chat schema migration..."
python3 run_chat_schema.py || echo "[entrypoint][WARN] chat schema migration failed, continuing"

# --- 3. Start the MCP HTTP server in the background ------------------------
# If IRONBRIDGE_MCP_URL is set, we need a local HTTP MCP server running.
# server.py already supports HTTP when TRANSPORT=http.
if [ -n "${IRONBRIDGE_MCP_URL:-}" ]; then
    echo "[entrypoint] Starting MCP HTTP server on :8080 ..."
    export TRANSPORT=http
    export PORT=8080
    python3 -m mcp_server.server &
    MCP_PID=$!
    
    # Wait for MCP health endpoint (up to 30s)
    for i in $(seq 1 30); do
        if curl -sf http://localhost:8080/health > /dev/null 2>&1; then
            echo "[entrypoint] MCP HTTP server is ready"
            break
        fi
        if [ "$i" -eq 30 ]; then
            echo "[entrypoint][WARN] MCP HTTP server did not become ready in time, continuing anyway..."
        fi
        sleep 1
    done
    
    # Clean up MCP server when this script exits
    trap 'echo "[entrypoint] Stopping MCP server..."; kill $MCP_PID 2>/dev/null || true' EXIT
fi

# --- 4. Start the platform backend ----------------------------------------
echo "[entrypoint] Starting IronBridge platform backend on :8000 ..."
exec uvicorn ib_platform.backend.app:app --host 0.0.0.0 --port 8000
