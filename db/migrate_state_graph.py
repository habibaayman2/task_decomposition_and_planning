"""
Applies db/state_graph_schema.sql to the SAME procurement.db the rest of
the system already uses (see mcp_server/db.py's DB_PATH resolution) --
this deliberately does NOT stand up a parallel database.

Safe to run more than once: every statement in the schema file is
CREATE TABLE IF NOT EXISTS.

Usage (from repo root, with your venv active):
    python -m db.migrate_state_graph
"""

from pathlib import Path

from mcp_server.db import get_conn, DB_PATH

SCHEMA_PATH = Path(__file__).resolve().parent / "state_graph_schema.sql"


def main() -> None:
    sql = SCHEMA_PATH.read_text()
    with get_conn() as conn:
        conn.executescript(sql)
    print(f"state_graph schema applied to {DB_PATH}")
    print("New tables: StateGraphRuns, StateGraphCheckpoints, HITLTasks, Tickets")


if __name__ == "__main__":
    main()
