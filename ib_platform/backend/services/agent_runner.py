"""
Agent Runner — bridge between platform backend and every live agent.
"""

import uuid
from typing import Any, Dict, List, Optional, Tuple

from mcp_server.db import get_conn

_state_graph_agents: Dict[str, Any] = {}

def _load_state_graph_agents():
    global _state_graph_agents
    if _state_graph_agents:
        return _state_graph_agents
    try:
        from state_graph.equipment_recovery.graph import build_equipment_recovery_graph
        _state_graph_agents["equipment_recovery"] = build_equipment_recovery_graph()
    except Exception as e:
        print(f"[agent_runner] equipment_recovery not loaded: {e}")
    try:
        from state_graph.change_order.graph import build_change_order_graph
        _state_graph_agents["change_order"] = build_change_order_graph()
    except Exception as e:
        print(f"[agent_runner] change_order not loaded: {e}")
    try:
        from state_graph.safety_incident.graph import build_safety_incident_graph
        _state_graph_agents["safety_incident"] = build_safety_incident_graph()
    except Exception as e:
        print(f"[agent_runner] safety_incident not loaded: {e}")
    return _state_graph_agents

_legacy_agents: Dict[str, Any] = {}

def _load_legacy_agents():
    global _legacy_agents
    if _legacy_agents:
        return _legacy_agents
    try:
        from agent.agent import run_agent as run_memory_rag
        _legacy_agents["memory_rag"] = run_memory_rag
    except Exception as e:
        print(f"[agent_runner] memory_rag not loaded: {e}")
    try:
        from agent.planning_agent import run_agent as run_planning
        _legacy_agents["planning"] = run_planning
    except Exception as e:
        print(f"[agent_runner] planning not loaded: {e}")
    return _legacy_agents

def list_available_agents() -> List[Dict[str, Any]]:
    agents = []
    for name in _load_state_graph_agents().keys():
        agents.append({
            "name": name,
            "type": "state_graph",
            "description": f"{name.replace('_', ' ').title()} agent",
            "status": "available"
        })
    for name in _load_legacy_agents().keys():
        agents.append({
            "name": name,
            "type": "legacy",
            "description": f"{name.replace('_', ' ').title()} agent",
            "status": "available"
        })
    return agents

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