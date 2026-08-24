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

# --- 3. Start the platform backend (serves the API + the /admin and /user --
#        static frontends via FastAPI StaticFiles, see ib_platform/backend/app.py)
echo "[entrypoint] Starting IronBridge platform backend on :8000 ..."
exec uvicorn ib_platform.backend.app:app --host 0.0.0.0 --port 8000
