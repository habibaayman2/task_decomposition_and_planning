from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Dict

# --------------------------------------------------------------------------
# Path & Module Resolution
# --------------------------------------------------------------------------
_current_dir = Path(__file__).resolve().parent
PROJECT_ROOT = next(
    (p for p in [_current_dir] + list(_current_dir.parents) if (p / "mcp_server").exists()),
    _current_dir.parent.parent
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from state_graph.change_order.nodes import (
    await_client_signoff_node,
    decompose_change_order_node,
    file_change_order_node,
    handle_decision_node,
)
from state_graph.core.checkpoint_store import default_store
from state_graph.core.graph_base import StateGraph


# --------------------------------------------------------------------------
# Graph Construction
# --------------------------------------------------------------------------

def _decision_router(state: Dict[str, Any]) -> str:
    """Routes back to decomposition if countered, otherwise finishes execution."""
    if state.get("hitl_decision") == "countered":
        return "decompose_change_order"
    return "__END__"


def build_change_order_graph() -> StateGraph:
    """Constructs the StateGraph with node transitions and conditional cyclic routing."""
    graph = StateGraph(name="change_order")

    graph.add_node("decompose_change_order", decompose_change_order_node)
    graph.add_node("file_change_order", file_change_order_node)
    graph.add_node("await_client_signoff", await_client_signoff_node)
    graph.add_node("handle_decision", handle_decision_node)

    graph.set_entry("decompose_change_order")

    graph.add_edge("decompose_change_order", "file_change_order")
    graph.add_edge("file_change_order", "await_client_signoff")
    graph.add_edge("await_client_signoff", "handle_decision")
    graph.add_conditional_edge("handle_decision", _decision_router)

    return graph


# --------------------------------------------------------------------------
# Public API — Start / Resume
# --------------------------------------------------------------------------

def start_new_change_order(run_id: str, request: dict) -> Dict[str, Any]:
    """Starts a new change order run.

    request may be either:
      - A structured dict: {project_id, employee_id, description, cost_delta, schedule_delta_days}
      - A natural language string that the task-decomposition node will parse.
    """
    graph = build_change_order_graph()
    initial_state = {"run_id": run_id, "request": request}
    return graph.run(run_id, initial_state=initial_state)


def resume_after_signoff(
    run_id: str,
    decision: str,
    resolved_by: int,
    counter_note: str | None = None,
) -> Dict[str, Any]:
    """Resumes an existing run after an admin resolves the pending HITL task.

    Reads the decision_key from the task\'s stored payload (the same key
    the node used when it opened the task) so resubmitted change orders
    (Version > 1) resolve correctly instead of silently no-oping.
    """
    loaded = default_store.load(run_id)
    if loaded is None:
        raise ValueError(f"No checkpoint found for run_id={run_id}")
    state, current_node, status = loaded
    if status != "paused_hitl":
        raise ValueError(
            f"Run \'{run_id}\' is not paused for a HITL decision (current status: \'{status}\')"
        )

    pending = [
        t for t in default_store.list_pending_hitl_tasks()
        if t["RunID"] == run_id
    ]
    if not pending:
        raise ValueError(f"No pending HITL task found for run_id={run_id}")
    task = max(pending, key=lambda t: t["TaskID"])
    task_id = task["TaskID"]
    payload = json.loads(task["PayloadJSON"]) if task["PayloadJSON"] else {}
    decision_key = payload.get("decision_key")
    if not decision_key:
        raise ValueError(
            f"HITL task {task_id} has no decision_key in its payload — "
            f"can\'t resolve safely (would risk resolving the wrong pause)."
        )

    default_store.resolve_hitl_task(task_id, decision, resolved_by, decision_key=decision_key)

    if counter_note:
        fresh_state, fresh_node, fresh_status = default_store.load(run_id)
        fresh_state["counter_note"] = counter_note
        default_store.save_checkpoint(run_id, fresh_state, fresh_node, status=fresh_status)

    graph = build_change_order_graph()
    return graph.run(run_id)


def resume_after_ticket(
    run_id: str,
    resolution: str,
    updated_state: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Resumes execution of a run stalled by an unplanned failure ticket.

    Re-runs the node that failed using graph.run(), optionally merging
    operator-supplied state corrections first via a direct checkpoint save
    (not through initial_state, which resume ignores).
    """
    loaded = default_store.load(run_id)
    if loaded is None:
        raise ValueError(f"No checkpoint found for run_id={run_id}")
    state, current_node, status = loaded
    if status != "ticket_open":
        raise ValueError(
            f"Run \'{run_id}\' is not in \'ticket_open\' status (current: \'{status}\')"
        )

    open_tickets = [t for t in default_store.list_open_tickets() if t["RunID"] == run_id]
    if not open_tickets:
        raise ValueError(f"No open ticket found for run_id={run_id}")
    ticket_id = max(open_tickets, key=lambda t: t["TicketID"])["TicketID"]

    default_store.resolve_ticket(ticket_id, resolution)

    if updated_state:
        state.update(updated_state)
        default_store.save_checkpoint(run_id, state, current_node, status="running")

    graph = build_change_order_graph()
    return graph.run(run_id)


# --------------------------------------------------------------------------
# Utility — Platform / Admin helpers
# --------------------------------------------------------------------------

def get_run_status(run_id: str) -> Dict[str, Any] | None:
    """Returns the current status of a run for the admin platform."""
    loaded = default_store.load(run_id)
    if loaded is None:
        return None
    state, current_node, status = loaded
    return {
        "run_id": run_id,
        "current_node": current_node,
        "status": status,
        "change_order_id": state.get("change_order_id"),
        "hitl_decision": state.get("hitl_decision"),
    }