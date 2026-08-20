"""
ib_platform/backend/routes/agents.py
Owner: Person A (A4)

Dynamic agent roster. Combines:
1. Static baseline (the 5 known agents from the course)
2. Live discovery from the checkpoint store (state_graph agents with active runs)
3. Health status from recent checkpoint activity

This is distinct from mcp_server's tool registry: there is only ONE MCP
server with ONE global tool set, but multiple agent processes connect to it.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------
_current_file = Path(__file__).resolve()
REPO_ROOT = next(
    (p for p in [_current_file] + list(_current_file.parents) if (p / "mcp_server").exists()),
    _current_file.parent.parent.parent
)

for path_entry in (str(REPO_ROOT), str(REPO_ROOT / "mcp_server")):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from fastapi import APIRouter

# Try to import checkpoint store for live discovery
try:
    from state_graph.core.checkpoint_store import default_store
    _HAS_CHECKPOINTS = True
except ImportError:
    _HAS_CHECKPOINTS = False
    default_store = None

router = APIRouter(prefix="/api/agents", tags=["agents"])

# Static baseline -- the agents we know exist in the repo
KNOWN_AGENTS = [
    {
        "agent_id": "memory_rag_agent",
        "label": "Memory & RAG Agent",
        "entrypoint": "agent/agent.py",
        "type": "static",
        "description": "Front-desk triage and clinical-policy questions via RAG retrieval",
    },
    {
        "agent_id": "planning_agent",
        "label": "Delay-Response Planning Agent",
        "entrypoint": "agent/planning_agent.py",
        "type": "static",
        "description": "Task decomposition, ToT, LATS for delay mitigation planning",
    },
    {
        "agent_id": "change_order_agent",
        "label": "Change Order Approval",
        "entrypoint": "state_graph/change_order/graph.py",
        "type": "state_graph",
        "description": "Stateful change-order workflow with HITL sign-off and ticket recovery",
    },
    {
        "agent_id": "equipment_recovery_agent",
        "label": "Equipment Breakdown Recovery",
        "entrypoint": "state_graph/equipment_recovery/graph.py",
        "type": "state_graph",
        "description": "Equipment failure triage with RAG manuals and ToT repair strategies",
    },
    {
        "agent_id": "safety_incident_agent",
        "label": "Safety Incident Reporting",
        "entrypoint": "state_graph/safety_incident/graph.py",
        "type": "state_graph",
        "description": "Safety incident investigation with LATS and regulator submission",
    },
]


def _discover_active_agents() -> list[dict]:
    """Query the checkpoint store for state_graph agents with active runs.
    Returns agents that have runs in 'running', 'paused_hitl', or 'ticket_open'."""
    if not _HAS_CHECKPOINTS or default_store is None:
        return []

    try:
        with default_store._get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT GraphName, Status, COUNT(*) as run_count "
                "FROM StateGraphRuns WHERE Status IN ('running', 'paused_hitl', 'ticket_open') "
                "GROUP BY GraphName, Status"
            ).fetchall()
    except Exception:
        return []

    active = {}
    for row in rows:
        graph_name = row["GraphName"]
        if graph_name not in active:
            active[graph_name] = {"graph_name": graph_name, "active_runs": 0, "statuses": []}
        active[graph_name]["active_runs"] += row["run_count"]
        active[graph_name]["statuses"].append(row["Status"])

    return list(active.values())


@router.get("")
def list_agents():
    """Return the full agent roster: static baseline + live discovery from
    checkpoint store + health summary."""
    static = [dict(a) for a in KNOWN_AGENTS]

    # Merge live discovery data into static entries
    active = _discover_active_agents()
    active_by_graph = {a["graph_name"]: a for a in active}

    for agent in static:
        graph_module = agent["entrypoint"].split("/")[-2] if "state_graph" in agent["entrypoint"] else None
        if graph_module and graph_module in active_by_graph:
            agent["health"] = {
                "active_runs": active_by_graph[graph_module]["active_runs"],
                "statuses": active_by_graph[graph_module]["statuses"],
                "state": "active",
            }
        else:
            agent["health"] = {"state": "idle", "active_runs": 0, "statuses": []}

    return {
        "agents": static,
        "total_count": len(static),
        "active_state_graphs": len(active),
    }


@router.get("/{agent_id}")
def get_agent(agent_id: str):
    """Return details for a single agent, including health and tool scope
    if available."""
    for agent in KNOWN_AGENTS:
        if agent["agent_id"] == agent_id:
            result = dict(agent)
            # Try to fetch tool scope from MCP server
            try:
                import mcp_bridge
                # Use a generic call -- in production this would need auth
                scope_json = mcp_bridge.call_tool_sync(
                    "get_agent_tool_scope",
                    {"agent_id": agent_id},
                    employee_id=1,  # Would need real auth in production
                    pin="0000",
                )
                import json
                result["tool_scope"] = json.loads(scope_json)
            except Exception:
                result["tool_scope"] = None
            return result
    return {"error": f"Agent '{agent_id}' not found"}