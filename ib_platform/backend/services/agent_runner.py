"""
Agent Runner — bridge between platform backend and every live agent.
"""

import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Path resolution (same pattern used by routes/agents.py, routes/tools.py, etc.)
# ---------------------------------------------------------------
_current_file = Path(__file__).resolve()
REPO_ROOT = next(
    (p for p in [_current_file] + list(_current_file.parents) if (p / "mcp_server").exists()),
    _current_file.parent.parent.parent
)

for path_entry in (str(REPO_ROOT), str(REPO_ROOT / "mcp_server")):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

# --------------------------------------------------------------------------

import uuid
from typing import Any, Dict, List, Optional, Tuple

from mcp_server.db import get_conn

_state_graph_agents: Dict[str, Any] = {}
_legacy_agents: Dict[str, Any] = {}

# Static fallback roster — always returned even if dynamic imports fail,
# so the admin/user frontends never appear empty.
STATIC_AGENT_ROSTER = [
    {"name": "change_order", "type": "state_graph", "description": "Change Order Approval & Appeal", "status": "available"},
    {"name": "equipment_recovery", "type": "state_graph", "description": "Equipment Breakdown Recovery", "status": "available"},
    {"name": "safety_incident", "type": "state_graph", "description": "Safety Incident Reporting", "status": "available"},
    {"name": "memory_rag", "type": "legacy", "description": "Memory & RAG Agent", "status": "available"},
    {"name": "planning", "type": "legacy", "description": "Delay-Response Planning Agent", "status": "available"},
]

def _load_state_graph_agents():
    global _state_graph_agents
    if _state_graph_agents:
        return _state_graph_agents
    loaders = [
        ("equipment_recovery", "state_graph.equipment_recovery.graph", "build_equipment_recovery_graph"),
        ("change_order", "state_graph.change_order.graph", "build_change_order_graph"),
        ("safety_incident", "state_graph.safety_incident.graph", "build_safety_incident_graph"),
    ]
    for name, module, factory in loaders:
        try:
            mod = __import__(module, fromlist=[factory])
            fn = getattr(mod, factory)
            _state_graph_agents[name] = fn()
        except Exception as e:
            print(f"[agent_runner] {name} not loaded: {e}")
    return _state_graph_agents

def _load_legacy_agents():
    global _legacy_agents
    if _legacy_agents:
        return _legacy_agents
    loaders = [
        ("memory_rag", "agent.agent", "run_agent"),
        ("planning", "agent.planning_agent", "run_agent"),
    ]
    for name, module, fn_name in loaders:
        try:
            mod = __import__(module, fromlist=[fn_name])
            _legacy_agents[name] = getattr(mod, fn_name)
        except Exception as e:
            print(f"[agent_runner] {name} not loaded: {e}")
    return _legacy_agents

def list_available_agents() -> List[Dict[str, Any]]:
    """Returns dynamically loaded agents, falling back to static roster
    if nothing could be imported (so the UI never appears empty)."""
    dynamic = []
    for name in _load_state_graph_agents().keys():
        dynamic.append({
            "name": name,
            "type": "state_graph",
            "description": name.replace("_", " ").title() + " agent",
            "status": "available"
        })
    for name in _load_legacy_agents().keys():
        dynamic.append({
            "name": name,
            "type": "legacy",
            "description": name.replace("_", " ").title() + " agent",
            "status": "available"
        })

    # If dynamic loading failed entirely, return static roster so frontends work
    if not dynamic:
        return [dict(a) for a in STATIC_AGENT_ROSTER]
    return dynamic

def is_state_graph_agent(agent_name: str) -> bool:
    return agent_name in _load_state_graph_agents()

def is_legacy_agent(agent_name: str) -> bool:
    return agent_name in _load_legacy_agents()

def run_state_graph_agent(
    agent_name: str,
    initial_state: Dict[str, Any],
    run_id: Optional[str] = None,
) -> Tuple[str, str, str, Dict[str, Any]]:
    graph = _load_state_graph_agents().get(agent_name)
    if graph is None:
        return "", "error", f"Agent '{agent_name}' is not available.", {}

    run_id = run_id or str(uuid.uuid4())

    try:
        final_state = graph.run(run_id=run_id, initial_state=initial_state)
    except Exception as e:
        return run_id, "error", f"Graph crashed: {str(e)}", {}

    from state_graph.core.checkpoint_store import default_store
    loaded = default_store.load(run_id)
    if loaded is None:
        return run_id, "error", "Run disappeared from store.", {}

    state, current_node, status = loaded

    if status == "completed":
        msg = _format_completion_message(agent_name, state)
        return run_id, "completed", msg, state
    elif status == "paused_hitl":
        msg = _format_hitl_message(agent_name, state, current_node)
        return run_id, "paused_hitl", msg, state
    elif status == "ticket_open":
        msg = _format_ticket_message(agent_name, state, current_node)
        return run_id, "ticket_open", msg, state
    else:
        return run_id, "error", f"Unknown run status: {status}", state

def run_legacy_agent(agent_name: str, message: str) -> Tuple[str, str]:
    runner = _load_legacy_agents().get(agent_name)
    if runner is None:
        return "error", f"Agent '{agent_name}' is not available."
    try:
        response = runner(message)
        return "completed", str(response)
    except Exception as e:
        return "error", f"Agent error: {str(e)}"

def _format_completion_message(agent_name: str, state: Dict[str, Any]) -> str:
    if agent_name == "equipment_recovery":
        action = state.get("proposed_action", "unknown")
        result = state.get("execution_result", {})
        return f"✅ Recovery complete! Action: **{action}**.\n\nDetails: {result}"
    elif agent_name == "change_order":
        return "✅ Change order processed successfully."
    elif agent_name == "safety_incident":
        return "✅ Safety incident report submitted."
    return "✅ Task completed."

def _format_hitl_message(agent_name: str, state: Dict[str, Any], node: str) -> str:
    if agent_name == "equipment_recovery":
        cost = state.get("proposed_cost", 0)
        action = state.get("proposed_action", "unknown")
        remaining = state.get("remaining_budget", "unknown")
        return (
            f"⏸️ **Waiting for admin approval**\n\n"
            f"Proposed action: **{action}** (cost: ${cost:,.2f})\n"
            f"This exceeds the project's remaining budget (${remaining:,.2f}).\n"
            f"An admin has been notified and will review this shortly."
        )
    return f"⏸️ **Waiting for admin approval** at step '{node}'."

def _format_ticket_message(agent_name: str, state: Dict[str, Any], node: str) -> str:
    return (
        f"⚠️ **An error occurred** at step '{node}'.\n\n"
        f"A support ticket has been opened automatically. "
        f"An engineer will investigate and resume this task."
    )
