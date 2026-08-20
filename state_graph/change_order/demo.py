"""
state_graph/change_order/test_demo.py

Unit test suite and flow graph demonstration validating tools.py, nodes.py,
graph.py, and core execution state transitions — including both LLM-addition
paths (task decomposition and constrained ReAct).
"""

from __future__ import annotations

from pathlib import Path
import sys
import pytest

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

from state_graph.change_order import tools
from state_graph.change_order.graph import (
    build_change_order_graph,
    resume_after_signoff,
    resume_after_ticket,
    start_new_change_order,
    get_run_status,
)
from state_graph.core.checkpoint_store import default_store
from state_graph.core.hitl import HITLPause
from state_graph.core.tickets import TicketableError


# --------------------------------------------------------------------------
# Flow Graph Visual
# --------------------------------------------------------------------------

FLOW_GRAPH_ASCII = """
                         WORKFLOW STATE GRAPH FLOW
========================================================================================

                                [start_new_change_order()]
                                            │
                                            ▼
                              ┌───────────────────────────┐
                              │ decompose_change_order    │  ← TASK DECOMPOSITION (LLM #1)
                              │   (LLM parses raw NL      │
                              │    into structured fields) │
                              └─────────────┬─────────────┘
                                            │
                         (Missing request?) ──► [ TicketableError ]
                                            │
                                            ▼
                              ┌───────────────────────────┐
                              │    file_change_order      │  ← CONSTRAINED ReAct (LLM #2)
                              │   (LLM reasons, then      │
                              │    picks from whitelist)   │
                              └─────────────┬─────────────┘
                                            │
                         (Non-whitelisted   ──► [ TicketableError ]
                          action?)
                                            │
                                            ▼
                              ┌───────────────────────────┐
                              │   await_client_signoff    │  ← HITL PAUSE
                              └─────────────┬─────────────┘
                                            │
                                 [resume_after_signoff()]
                                            │
                                            ▼
                              ┌───────────────────────────┐
                              │      handle_decision      │
                              └─────────────┬─────────────┘
                                            │
                                            ├──► Decision == 'countered'
                                            │       (Version + 1) ──┐
                                            │                        │
                                            ├──► Decision == 'approved'
                                            │       [ Status: Closed ]
                                            │
                                            └──► Decision == 'rejected'
                                                    [ Status: Closed ]

========================================================================================
"""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _reset_store_for_test(run_id: str) -> None:
    """Best-effort cleanup of prior test state so tests are idempotent."""
    # Note: default_store uses the shared procurement.db; in a real CI
    # environment you would use a temp DB per test. For demo purposes
    # we rely on unique run_ids.
    pass


# --------------------------------------------------------------------------
# Tests — Graph Structure & Visual
# --------------------------------------------------------------------------

def test_display_flow_graph():
    """Prints the flow graph diagram during verbose test runs (-s)."""
    print(FLOW_GRAPH_ASCII)
    assert "WORKFLOW STATE GRAPH FLOW" in FLOW_GRAPH_ASCII
    assert "TASK DECOMPOSITION" in FLOW_GRAPH_ASCII
    assert "CONSTRAINED ReAct" in FLOW_GRAPH_ASCII


# --------------------------------------------------------------------------
# Tests — Task Decomposition (LLM Addition #1)
# --------------------------------------------------------------------------

def test_structured_request_backward_compat():
    """Structured dict requests bypass LLM decomposition (fast path)."""
    run_id = "test-structured-001"
    request = {
        "project_id": 1,
        "employee_id": 1,
        "description": "HVAC system revision",
        "cost_delta": 12000.00,
        "schedule_delta_days": 3,
    }
    state = start_new_change_order(run_id, request)
    assert "change_order_id" in state
    co = tools.get(state["change_order_id"])
    assert co["Description"] == "HVAC system revision"


def test_natural_language_decomposition():
    """Raw string requests trigger LLM task decomposition into structured fields."""
    run_id = "test-nl-decompose-001"
    nl_request = (
        "Project 1 needs a reinforced foundation after soil survey. "
        "Budget impact around 18500 dollars and it pushes the schedule by 6 days."
    )
    state = start_new_change_order(run_id, nl_request)
    assert "change_order_id" in state
    assert "decomposed_request" in state

    co = tools.get(state["change_order_id"])
    assert co["ProjectID"] == 1
    assert co["CostDelta"] == 18500.0
    assert co["ScheduleDeltaDays"] == 6
    assert co["Status"] == "PendingReview"  # filed by constrained ReAct


# --------------------------------------------------------------------------
# Tests — Constrained ReAct (LLM Addition #2)
# --------------------------------------------------------------------------

def test_constrained_react_submits_valid_draft():
    """A clean change order is submitted for review by the ReAct node."""
    run_id = "test-react-submit-001"
    request = {
        "project_id": 1,
        "employee_id": 1,
        "description": "Valid scope description",
        "cost_delta": 5000.0,
        "schedule_delta_days": 2,
    }
    state = start_new_change_order(run_id, request)
    co_id = state["change_order_id"]
    co = tools.get(co_id)
    assert co["Status"] == "PendingReview"
    assert state.get("filing_action") == "submitted"
    assert "thought" in state


def test_constrained_react_aborts_invalid_draft():
    """A change order with negative cost triggers the abort action."""
    run_id = "test-react-abort-001"
    request = {
        "project_id": 1,
        "employee_id": 1,
        "description": "",  # empty description
        "cost_delta": -1000.0,  # negative cost
        "schedule_delta_days": 1,
    }
    state = start_new_change_order(run_id, request)
    co_id = state["change_order_id"]
    co = tools.get(co_id)
    assert co["Status"] == "Closed"  # abort_draft calls tools.close()
    assert state.get("filing_action") == "aborted"
    assert "thought" in state


# --------------------------------------------------------------------------
# Tests — Full HITL Lifecycle
# --------------------------------------------------------------------------

def test_change_order_workflow_lifecycle():
    """Happy-path lifecycle: Draft -> Submit -> HITL Pause -> Counter Loop
    -> Approve -> Close."""
    run_id = "test-run-lifecycle-001"
    request_payload = {
        "project_id": 1,
        "employee_id": 1,
        "description": "HVAC system revision based on client request",
        "cost_delta": 12000.00,
        "schedule_delta_days": 3,
    }

    # 1. Start execution -> pauses at await_client_signoff
    state = start_new_change_order(run_id, request_payload)
    checkpoint = default_store.load(run_id)
    assert checkpoint is not None
    _, node, status = checkpoint
    assert node == "await_client_signoff"
    assert status == "paused_hitl"

    co_id = state["change_order_id"]
    co_record = tools.get(co_id)
    assert co_record["Status"] == "PendingReview"
    assert co_record["Version"] == 1

    pending = default_store.list_pending_hitl_tasks()
    assert len([t for t in pending if t["RunID"] == run_id]) == 1

    # 2. Resume with 'countered' -> loops back and pauses again
    state = resume_after_signoff(
        run_id, decision="countered", resolved_by=2, counter_note="Reduce cost by 10%"
    )
    checkpoint = default_store.load(run_id)
    _, node, status = checkpoint
    assert node == "await_client_signoff"
    assert status == "paused_hitl"

    co_record_countered = tools.get(co_id)
    assert co_record_countered["Version"] == 2
    assert co_record_countered["CounterNote"] == "Reduce cost by 10%"

    still_pending = [t for t in default_store.list_pending_hitl_tasks() if t["RunID"] == run_id]
    assert len(still_pending) == 1

    # 3. Resume with 'approved' -> completes and closes
    final_state = resume_after_signoff(run_id, decision="approved", resolved_by=2)
    final_checkpoint = default_store.load(run_id)
    _, final_node, final_status = final_checkpoint
    assert final_status == "completed"

    co_record_final = tools.get(co_id)
    assert co_record_final["Status"] == "Closed"
    assert final_state.get("hitl_decision") == "approved"
    assert len([t for t in default_store.list_pending_hitl_tasks() if t["RunID"] == run_id]) == 0


# --------------------------------------------------------------------------
# Tests — Ticket Path
# --------------------------------------------------------------------------

def test_ticketable_error_on_missing_request():
    """Unexpected state shapes become a ticket, not a propagated exception."""
    run_id = "test-run-error-001"
    graph = build_change_order_graph()
    state = graph.run(run_id, initial_state={"invalid_key": True})

    checkpoint = default_store.load(run_id)
    assert checkpoint is not None
    _, node, status = checkpoint
    assert status == "ticket_open"
    assert node == "decompose_change_order"

    open_tickets = [t for t in default_store.list_open_tickets() if t["RunID"] == run_id]
    assert len(open_tickets) == 1
    assert "Missing 'request' payload" in open_tickets[0]["ErrorMessage"]


def test_resume_after_ticket_reruns_failed_node_and_resolves_ticket():
    """A ticket resume must re-run the failed node and mark the ticket resolved."""
    run_id = "test-run-ticket-resume-001"
    graph = build_change_order_graph()
    graph.run(run_id, initial_state={"invalid_key": True})

    checkpoint = default_store.load(run_id)
    assert checkpoint[2] == "ticket_open"

    final_state = resume_after_ticket(
        run_id,
        resolution="Operator supplied the missing request payload.",
        updated_state={
            "request": {
                "project_id": 1, "employee_id": 1,
                "description": "Corrected after ticket", "cost_delta": 1000.0,
                "schedule_delta_days": 1,
            }
        },
    )

    checkpoint_after = default_store.load(run_id)
    assert checkpoint_after[2] == "paused_hitl"
    assert "change_order_id" in final_state

    open_tickets = [t for t in default_store.list_open_tickets() if t["RunID"] == run_id]
    assert len(open_tickets) == 0


# --------------------------------------------------------------------------
# Tests — Admin Platform Helpers
# --------------------------------------------------------------------------

def test_get_run_status():
    """get_run_status exposes run state for the admin platform."""
    run_id = "test-status-001"
    start_new_change_order(run_id, {
        "project_id": 1, "employee_id": 1,
        "description": "Test", "cost_delta": 100.0, "schedule_delta_days": 1,
    })
    status = get_run_status(run_id)
    assert status is not None
    assert status["run_id"] == run_id
    assert status["status"] == "paused_hitl"
    assert status["current_node"] == "await_client_signoff"
    assert "change_order_id" in status


if __name__ == "__main__":
    print(FLOW_GRAPH_ASCII)