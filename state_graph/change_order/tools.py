from __future__ import annotations
from pathlib import Path
import sys
import time
from typing import Optional, List

# --------------------------------------------------------------------------
# Path & Module Resolution
# --------------------------------------------------------------------------
_current_dir = Path(__file__).resolve().parent
REPO_ROOT = next(
    (p for p in [_current_dir] + list(_current_dir.parents) if (p / "mcp_server").exists()),
    _current_dir.parent.parent
)

for path_entry in (str(REPO_ROOT), str(REPO_ROOT / "mcp_server")):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

import db as mcp_db

# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ChangeOrders (
    ChangeOrderID       INTEGER PRIMARY KEY,
    RunID               TEXT NOT NULL,
    ProjectID           INTEGER NOT NULL,
    Description         TEXT NOT NULL,
    CostDelta           REAL NOT NULL,
    ScheduleDeltaDays   INTEGER NOT NULL,
    Status              TEXT NOT NULL CHECK (
        Status IN ('Drafting', 'PendingReview', 'Approved', 'Rejected', 'Countered', 'Closed')
    ),
    CounterNote         TEXT,
    SubmittedAt         REAL,
    DecidedAt           REAL,
    Version             INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (ProjectID) REFERENCES Projects(ProjectID)
);
"""


def _ensure_schema(conn) -> None:
    conn.executescript(_SCHEMA)


# --------------------------------------------------------------------------
# CRUD + Lifecycle
# --------------------------------------------------------------------------

def create_draft(
    run_id: str,
    project_id: int,
    description: str,
    cost_delta: float,
    schedule_delta_days: int,
    actor_id: int,
) -> int:
    """Creates a new ChangeOrder in Drafting status. Returns the new ID."""
    with mcp_db.get_conn() as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "INSERT INTO ChangeOrders "
            "(RunID, ProjectID, Description, CostDelta, ScheduleDeltaDays, Status, Version) "
            "VALUES (?, ?, ?, ?, ?, 'Drafting', 1)",
            (run_id, project_id, description, cost_delta, schedule_delta_days),
        )
        change_order_id = cur.lastrowid
        mcp_db.log_action(
            conn,
            actor_id,
            "create_change_order_draft",
            change_order_id,
            f"cost_delta={cost_delta}, schedule_delta_days={schedule_delta_days}",
        )
        return change_order_id


def submit_for_review(change_order_id: int, actor_id: int) -> None:
    """The one whitelisted \'filing\' action a constrained-ReAct node may
    take -- nothing else is exposed to the LLM from this module."""
    with mcp_db.get_conn() as conn:
        conn.execute(
            "UPDATE ChangeOrders SET Status = 'PendingReview', SubmittedAt = ? WHERE ChangeOrderID = ?",
            (time.time(), change_order_id),
        )
        mcp_db.log_action(conn, actor_id, "submit_change_order_for_review", change_order_id, "")


def record_decision(
    change_order_id: int,
    decision: str,
    actor_id: int,
    counter_note: Optional[str] = None,
) -> None:
    """Records an admin decision: approved, rejected, or countered."""
    status_map = {"approved": "Approved", "rejected": "Rejected", "countered": "Countered"}
    if decision not in status_map:
        raise ValueError(f"unknown decision {decision!r}")
    with mcp_db.get_conn() as conn:
        conn.execute(
            "UPDATE ChangeOrders SET Status = ?, CounterNote = ?, DecidedAt = ? WHERE ChangeOrderID = ?",
            (status_map[decision], counter_note, time.time(), change_order_id),
        )
        mcp_db.log_action(conn, actor_id, "record_change_order_decision", change_order_id, decision)


def bump_version_for_resubmission(change_order_id: int, actor_id: int) -> None:
    """Bumps version and resets status to Drafting for a countered resubmission."""
    with mcp_db.get_conn() as conn:
        conn.execute(
            "UPDATE ChangeOrders SET Status = 'Drafting', Version = Version + 1 WHERE ChangeOrderID = ?",
            (change_order_id,),
        )
        mcp_db.log_action(conn, actor_id, "resubmit_change_order", change_order_id, "")


def close(change_order_id: int, actor_id: int) -> None:
    """Closes a change order (terminal status)."""
    with mcp_db.get_conn() as conn:
        conn.execute("UPDATE ChangeOrders SET Status = 'Closed' WHERE ChangeOrderID = ?", (change_order_id,))
        mcp_db.log_action(conn, actor_id, "close_change_order", change_order_id, "")


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------

def get(change_order_id: int) -> Optional[dict]:
    """Returns a single change order by ID, or None."""
    with mcp_db.get_conn() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM ChangeOrders WHERE ChangeOrderID = ?", (change_order_id,)
        ).fetchone()
    return dict(row) if row else None


def list_by_status(status: str) -> List[dict]:
    """Returns all change orders with the given status."""
    with mcp_db.get_conn() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM ChangeOrders WHERE Status = ? ORDER BY SubmittedAt DESC",
            (status,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_by_project(project_id: int) -> List[dict]:
    """Returns all change orders for a project."""
    with mcp_db.get_conn() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM ChangeOrders WHERE ProjectID = ? ORDER BY ChangeOrderID DESC",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def seconds_since_submission(change_order_id: int) -> Optional[float]:
    """Returns elapsed seconds since submission, or None if not yet submitted."""
    co = get(change_order_id)
    if not co or not co["SubmittedAt"]:
        return None
    return time.time() - co["SubmittedAt"]