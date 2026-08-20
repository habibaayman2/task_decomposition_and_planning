"""
ib_platform/backend/routes/tools.py
Owner: Person A (A2 + A4 + A6)

Admin tool management, backed by the REAL MCP server via mcp_bridge.py.
This version works with the EXISTING server.py (unmodified) which has:
  - list_registered_tools
  - deregister_tool
  - authenticate_as_approver (which re-adds missing approver tools)

It does NOT require server.py to have register_tool, set_agent_tool_scope,
or get_agent_tool_scope -- those are handled at the bridge level.

All routes require approver auth (Project Manager or Finance Officer PIN).
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Optional

# --------------------------------------------------------------------------
# Path & Module Resolution
# --------------------------------------------------------------------------
_current_file = Path(__file__).resolve()
BACKEND_DIR = _current_file.parent.parent
REPO_ROOT = next(
    (p for p in [_current_file] + list(_current_file.parents) if (p / "mcp_server").exists()),
    BACKEND_DIR.parent
)

for path_entry in (str(REPO_ROOT), str(BACKEND_DIR)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import mcp_bridge

router = APIRouter(prefix="/api/tools", tags=["tools"])


# --------------------------------------------------------------------------
# Request schemas
# --------------------------------------------------------------------------

class ApproverAuth(BaseModel):
    employee_id: int = Field(ge=1, description="Project Manager or Finance Officer employee ID")
    pin: str = Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")


class DeregisterRequest(ApproverAuth):
    tool_name: str = Field(min_length=1, description="Exact tool name to remove")


class RegisterRequest(ApproverAuth):
    tool_name: str = Field(min_length=1, description="Tool name to re-register (must be in server\'s approver pool)")


class ScopeRequest(ApproverAuth):
    agent_id: str = Field(min_length=1, description="Agent to scope, e.g. 'change_order_agent'")
    tool_names: list[str] = Field(default=[], description="Tools this agent may use. Empty = remove restriction")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.post("/list")
def list_registered_tools(auth: ApproverAuth, agent_id: Optional[str] = None):
    """List every tool currently on the live MCP server, optionally filtered
    by a specific agent's tool scope."""
    try:
        tools = mcp_bridge.list_registered_tools_sync(auth.employee_id, auth.pin, agent_id=agent_id)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"tools": tools, "agent_id": agent_id, "count": len(tools)}


@router.post("/register")
def register_tool(req: RegisterRequest):
    """Re-register a previously-deregistered tool on the live MCP server.

    The existing server.py does not have a standalone register_tool.
    Instead, calling authenticate_as_approver re-adds ALL missing approver
    tools from its hardcoded pool. This route triggers that auth flow,
    which has the side effect of restoring any missing tool.
    """
    try:
        result = mcp_bridge.register_tool_sync(req.tool_name, req.employee_id, req.pin)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return {"result": result, "tool_name": req.tool_name}


@router.post("/deregister")
def deregister_tool(req: DeregisterRequest):
    """Remove a tool from the live MCP server. Always-on tools (inventory,
    budget, etc.) and deregister_tool itself cannot be removed."""
    try:
        result = mcp_bridge.deregister_tool_sync(req.tool_name, req.employee_id, req.pin)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return {"result": result, "tool_name": req.tool_name}


@router.post("/scope")
def set_agent_tool_scope(req: ScopeRequest):
    """Restrict which tools a specific agent can see. Pass empty tool_names
    to remove restrictions and let the agent see all tools again.

    Scoping is enforced at the bridge level (server.py does not have native
    per-agent scoping). The bridge filters tool lists before returning them."""
    try:
        result = mcp_bridge.set_agent_scope_sync(req.agent_id, req.tool_names, req.employee_id, req.pin)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"result": result, "agent_id": req.agent_id, "tool_names": req.tool_names}


@router.post("/scope/get")
def get_agent_tool_scope(auth: ApproverAuth, agent_id: str):
    """Get the current tool scope for a specific agent."""
    try:
        result = mcp_bridge.get_agent_scope_sync(agent_id, auth.employee_id, auth.pin)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return {"agent_id": agent_id, "visible_tools": result, "count": len(result)}