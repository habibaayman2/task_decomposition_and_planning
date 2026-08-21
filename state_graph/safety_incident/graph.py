from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Dict

# --------------------------------------------------------------------------
# Path & Module Resolution
# --------------------------------------------------------------------------
_current_dir = Path(__file__).resolve().parent
PROJECT_ROOT = next(
    (p for p in [_current_dir] + list(_current_dir.parents) if (p / "mcp_server").exists()),
    _current_dir.parent.parent  # Fallback
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from state_graph.safety_incident.nodes import (
    file_regulator_report_node,
    handle_officer_decision_node,
    investigate_node,
    report_incident_node,
    safety_officer_signoff_node,
)
from state_graph.core.checkpoint_store import default_store
from state_graph.core.graph_base import StateGraph


def _decision_router(state: Dict[str, Any]) -> str:
    """Routes back to investigate_node on 'needs_more_investigation'
    (the genuine cycle), to file_regulator_report_node when a report is
    required, or ends when the officer closes with no report."""
    decision = state.get("hitl_decision")
    if decision == "needs_more_investigation":
        return "investigate"
    if decision == "regulator_report_required":
        return "file_regulator_report"
    return "__END__"


def build_safety_incident_graph() -> StateGraph:
    """Constructs the StateGraph with node transitions and conditional
    cyclic routing back to investigate_node."""
    graph = StateGraph(name="safety_incident")

    # Register Nodes
    graph.add_node("report_incident", report_incident_node)
    graph.add_node("investigate", investigate_node)
    graph.add_node("safety_officer_signoff", safety_officer_signoff_node)
    graph.add_node("handle_officer_decision", handle_officer_decision_node)
    graph.add_node("file_regulator_report", file_regulator_report_node)

    # Set Entry Point
    graph.set_entry("report_incident")

    # Define Fixed Edges
    graph.add_edge("report_incident", "investigate")
    graph.add_edge("investigate", "safety_officer_signoff")
    graph.add_edge("safety_officer_signoff", "handle_officer_decision")
    graph.add_edge("file_regulator_report", "__END__")

    # Define Conditional Edge for Cyclic Re-investigation Loop
    graph.add_conditional_edge("handle_officer_decision", _decision_router)

    return graph


def start_new_incident(run_id: str, request: dict) -> Dict[str, Any]:
    """Starts a new safety-incident run.
    request = {project_id, employee_id, description, severity}
    """
    graph = build_safety_incident_graph()
    initial_state = {"run_id": run_id, "request": request}
    return graph.run(run_id, initial_state=initial_state)


def resume_after_signoff(run_id: str) -> Dict[str, Any]:
    """Resumes an existing run after an admin resolves the officer's
    HITL task via CheckpointStore.resolve_hitl_task() (which already
    merges the decision into persisted state under the SAME decision_key
    safety_officer_signoff_node used -- nothing needs to be passed here)."""
    graph = build_safety_incident_graph()

    loaded = default_store.load(run_id)
    if loaded is None:
        raise ValueError(f"No checkpoint found for run_id={run_id}")

    return graph.run(run_id)


def resume_after_ticket(run_id: str, updated_state: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Resumes execution of a run stalled by an unplanned failure ticket.

    Re-runs the node that failed (any exception other than HITLPause)
    using graph.run(), optionally merging updated state context supplied
    by an operator during resolution.
    """
    graph = build_safety_incident_graph()

    loaded = default_store.load(run_id)
    if loaded is None:
        raise ValueError(f"No checkpoint found for run_id={run_id}")

    state, _, status = loaded
    if status != "ticket_open":
        raise ValueError(f"Run '{run_id}' is not in 'ticket_open' status (current: '{status}')")

    if updated_state:
        state.update(updated_state)
        default_store.save_checkpoint(run_id, state, loaded[1], status="running")

    return graph.run(run_id)


if __name__ == "__main__":
    demo_run_id = "demo-safety-001"

    state = start_new_incident(
        demo_run_id,
        request={
            "project_id": 1,
            "employee_id": 1,
            "description": "Worker reported near-miss with unsecured scaffolding on level 4",
            "severity": "high",
        },
    )

    loaded_checkpoint = default_store.load(demo_run_id)
    current_node = loaded_checkpoint[1] if loaded_checkpoint else "Unknown"
    current_status = loaded_checkpoint[2] if loaded_checkpoint else "Unknown"
    print(f"Paused at node: {current_node} (Status: {current_status})")
    print(f"Current State:   {state}")
