"""
Durable, SQLite-backed checkpoint store for every state_graph run.

Reuses mcp_server.db.get_conn() -- the SAME procurement.db connection
the rest of the system already uses. This is deliberate: the project
brief explicitly requires "visibly reusing the existing server and
database rather than standing up a parallel one."

Every meaningful transition (a node finishing, a HITL pause, a ticket
opening) is written here BEFORE the runner moves on. That's what makes
crash/resume real: kill the process after any of these calls returns,
and the next `graph.run(run_id)` picks up from exactly that point --
see state_graph/demo_crash_resume.py for a runnable proof.
"""

import json
from typing import Any, Dict, Optional, Tuple

from mcp_server.db import get_conn


class CheckpointStore:
    # -- run lifecycle -----------------------------------------------

    def create_run(self, run_id: str, graph_name: str, entry_node: str, state: Dict[str, Any]) -> None:
        """Called once, when a run starts for the first time."""
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO StateGraphRuns (RunID, GraphName, Status, CurrentNode, StateJSON) "
                "VALUES (?, ?, 'running', ?, ?)",
                (run_id, graph_name, entry_node, json.dumps(state)),
            )
            conn.execute(
                "INSERT INTO StateGraphCheckpoints (RunID, NodeName, StateJSON, Status) "
                "VALUES (?, ?, ?, 'running')",
                (run_id, entry_node, json.dumps(state)),
            )

    def load(self, run_id: str) -> Optional[Tuple[Dict[str, Any], str, str]]:
        """Returns (state, current_node, status) for an existing run, or
        None if run_id has never been seen. graph_base.py calls this
        first on every .run() -- that's the crash-recovery entry point.
        """
        with get_conn() as conn:
            row = conn.execute(
                "SELECT StateJSON, CurrentNode, Status FROM StateGraphRuns WHERE RunID = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            return json.loads(row["StateJSON"]), row["CurrentNode"], row["Status"]

    def save_checkpoint(self, run_id: str, state: Dict[str, Any], next_node: str, status: str) -> None:
        """Called after every node transition -- success, HITL pause, or
        ticket. `next_node` is whichever node should run when this run
        is next resumed (for a successful step, that's the node the
        edge points to; for a pause/ticket, it's the SAME node that
        paused/failed, since that node has not completed).
        """
        with get_conn() as conn:
            conn.execute(
                "UPDATE StateGraphRuns SET StateJSON = ?, CurrentNode = ?, Status = ?, "
                "UpdatedAt = datetime('now') WHERE RunID = ?",
                (json.dumps(state), next_node, status, run_id),
            )
            conn.execute(
                "INSERT INTO StateGraphCheckpoints (RunID, NodeName, StateJSON, Status) "
                "VALUES (?, ?, ?, ?)",
                (run_id, next_node, json.dumps(state), status),
            )

    # -- HITL -----------------------------------------------------------

    def open_hitl_task(self, run_id: str, node_name: str, reason: str, payload: Dict[str, Any]) -> int:
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO HITLTasks (RunID, NodeName, Reason, PayloadJSON) VALUES (?, ?, ?, ?)",
                (run_id, node_name, reason, json.dumps(payload)),
            )
            return cur.lastrowid

    def resolve_hitl_task(
        self,
        task_id: int,
        decision: str,
        resolved_by: int,
        decision_key: str = "hitl_decision",
    ) -> str:
        """Called by the platform's admin backend when an admin acts on a
        pending HITL task. Marks the task resolved AND merges the
        decision into the run's persisted state (under `decision_key`,
        the same key the node's require_hitl() call checks) so the
        re-executed node sees it. Sets the run back to 'running' so the
        very next graph.run(run_id) resumes it. Returns the RunID so the
        caller can immediately call graph.run(run_id) to resume.
        """
        with get_conn() as conn:
            row = conn.execute(
                "SELECT RunID FROM HITLTasks WHERE TaskID = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"No HITL task with TaskID {task_id}")
            run_id = row["RunID"]

            conn.execute(
                "UPDATE HITLTasks SET Status = 'resolved', Decision = ?, ResolvedBy = ?, "
                "ResolvedAt = datetime('now') WHERE TaskID = ?",
                (decision, resolved_by, task_id),
            )

            run_row = conn.execute(
                "SELECT StateJSON FROM StateGraphRuns WHERE RunID = ?", (run_id,)
            ).fetchone()
            state = json.loads(run_row["StateJSON"])
            state[decision_key] = decision
            conn.execute(
                "UPDATE StateGraphRuns SET StateJSON = ?, Status = 'running', "
                "UpdatedAt = datetime('now') WHERE RunID = ?",
                (json.dumps(state), run_id),
            )
            return run_id

    def list_pending_hitl_tasks(self):
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM HITLTasks WHERE Status = 'pending' ORDER BY CreatedAt"
            ).fetchall()
            return [dict(r) for r in rows]

    # -- tickets ----------------------------------------------------------

    def open_ticket(self, run_id: str, node_name: str, error_message: str) -> int:
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO Tickets (RunID, NodeName, ErrorMessage) VALUES (?, ?, ?)",
                (run_id, node_name, error_message),
            )
            return cur.lastrowid

    def resolve_ticket(self, ticket_id: int, resolution: str) -> str:
        """Marks a ticket resolved and sets its run back to 'running' so
        the SAME node that failed re-executes on the next graph.run()
        call -- not a restart from the beginning. Returns the RunID.
        """
        with get_conn() as conn:
            row = conn.execute(
                "SELECT RunID FROM Tickets WHERE TicketID = ?", (ticket_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"No ticket with TicketID {ticket_id}")
            run_id = row["RunID"]

            conn.execute(
                "UPDATE Tickets SET Status = 'resolved', Resolution = ?, "
                "ResolvedAt = datetime('now') WHERE TicketID = ?",
                (resolution, ticket_id),
            )
            conn.execute(
                "UPDATE StateGraphRuns SET Status = 'running', UpdatedAt = datetime('now') "
                "WHERE RunID = ?",
                (run_id,),
            )
            return run_id

    def list_open_tickets(self):
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM Tickets WHERE Status != 'resolved' ORDER BY CreatedAt"
            ).fetchall()
            return [dict(r) for r in rows]

    def _get_conn(self):
        """Expose the connection context manager for external SQL."""
        return get_conn()
    

# Shared default instance -- graphs can pass their own, but sharing one
# keeps things simple for Day 2/3 node authors who don't need to think
# about it.
default_store = CheckpointStore()
