"""
Thin bridge between the admin FastAPI routes and the REAL MCP server.

IMPORTANT: This does NOT modify server.py. It works with the existing
server's API surface:
  - list_registered_tools()      → returns all tools
  - deregister_tool(name)        → removes a tool
  - authenticate_as_approver()   → re-adds ALL missing approver tools

For register_tool: we call authenticate_as_approver() which re-adds any
missing approver tools from the APPROVER_TOOL_FNS pool (this is how the
existing server.py works -- see its authenticate_as_approver implementation).

For per-agent scoping: maintained in this bridge module since server.py
does not have AGENT_TOOL_SCOPES. The bridge filters tool lists before
returning them to callers.

Transport: HTTP required for deregistration to be visible across sessions.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import warnings
from typing import Optional

# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------
_current_file = Path(__file__).resolve()
REPO_ROOT = next(
    (p for p in [_current_file] + list(_current_file.parents) if (p / "mcp_server").exists()),
    _current_file.parent.parent.parent
)
AGENT_DIR = REPO_ROOT / "agent"

for path_entry in (str(REPO_ROOT), str(AGENT_DIR)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

import mcp_client


# --------------------------------------------------------------------------
# Per-agent tool scoping (bridge-level, since server.py lacks this)
# --------------------------------------------------------------------------
AGENT_TOOL_SCOPES: dict[str, set[str]] = {}


def _get_visible_tools(all_tools: list[str], agent_id: Optional[str] = None) -> list[str]:
    """Filters a tool list by agent scope. No scope = all tools."""
    if agent_id is None or agent_id not in AGENT_TOOL_SCOPES:
        return sorted(all_tools)
    allowed = AGENT_TOOL_SCOPES[agent_id]
    return sorted(t for t in all_tools if t in allowed)


# --------------------------------------------------------------------------
# Connection & auth helpers
# --------------------------------------------------------------------------

def _connect_kwargs() -> dict:
    http_url = os.environ.get("IRONBRIDGE_MCP_URL")
    if http_url:
        return dict(
            transport="http",
            http_url=http_url,
            http_token=os.environ.get("IRONBRIDGE_API_TOKEN"),
        )

    warnings.warn(
        "IRONBRIDGE_MCP_URL is not set -- falling back to a private stdio "
        "subprocess. Any deregister/register call only affects that one "
        "throwaway process, NOT whatever server real agents talk to. "
        "Set IRONBRIDGE_MCP_URL before relying on the admin panel.",
        stacklevel=3,
    )
    return dict(
        transport="stdio",
        server_command=[sys.executable, str(REPO_ROOT / "mcp_server" / "server.py")],
        server_cwd=str(REPO_ROOT),
    )


async def _with_session(coro_fn, *, employee_id: int, pin: str):
    """Opens a session, authenticates as approver, then runs coro_fn(session)."""
    async with mcp_client.connect(**_connect_kwargs()) as (session, _init_result):
        auth = await session.call_tool(
            "authenticate_as_approver", {"employee_id": employee_id, "pin": pin}
        )
        auth_text = auth.content[0].text if auth.content else ""
        if "Authenticated" not in auth_text:
            raise PermissionError(f"MCP authentication failed: {auth_text}")
        return await coro_fn(session)


def _run(coro):
    """Sync wrapper for plain `def` FastAPI routes."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Public sync API
# --------------------------------------------------------------------------

def list_registered_tools_sync(employee_id: int, pin: str, agent_id: Optional[str] = None) -> list[str]:
    """List tools from the live server, optionally filtered by agent scope."""
    async def _call(session):
        result = await session.call_tool("list_registered_tools", {})
        all_tools = json.loads(result.content[0].text)
        return _get_visible_tools(all_tools, agent_id)
    return _run(_with_session(_call, employee_id=employee_id, pin=pin))


def register_tool_sync(tool_name: str, employee_id: int, pin: str) -> str:
    """Re-register a deregistered tool.

    The existing server.py does not have a standalone register_tool.
    Instead, authenticate_as_approver() re-adds ALL missing approver tools
    from APPROVER_TOOL_FNS if any are missing. So we call auth, which
    triggers re-registration of the missing tool.
    """
    async def _call(session):
        # authenticate_as_approver was already called by _with_session.
        # If the tool was missing, it was just re-added during auth.
        # Verify by listing tools.
        result = await session.call_tool("list_registered_tools", {})
        all_tools = json.loads(result.content[0].text)
        if tool_name in all_tools:
            return f"Tool '{tool_name}' is now registered (re-added via approver auth)."
        return (
            f"Tool '{tool_name}' could not be registered. "
            f"It may not be in the server's APPROVER_TOOL_FNS pool. "
            f"Currently registered: {all_tools}"
        )
    return _run(_with_session(_call, employee_id=employee_id, pin=pin))


def deregister_tool_sync(tool_name: str, employee_id: int, pin: str) -> str:
    """Remove a tool from the live server."""
    async def _call(session):
        result = await session.call_tool("deregister_tool", {"tool_name": tool_name})
        return result.content[0].text if result.content else ""
    return _run(_with_session(_call, employee_id=employee_id, pin=pin))


def set_agent_scope_sync(agent_id: str, tool_names: list[str], employee_id: int, pin: str) -> str:
    """Restrict which tools an agent can see (bridge-level scoping)."""
    # Validate that all named tools actually exist on the server
    async def _call(session):
        result = await session.call_tool("list_registered_tools", {})
        all_tools = set(json.loads(result.content[0].text))
        invalid = [t for t in tool_names if t not in all_tools]
        if invalid:
            raise ValueError(f"Unknown tools: {invalid}. Registered: {sorted(all_tools)}")
        return sorted(all_tools)

    _run(_with_session(_call, employee_id=employee_id, pin=pin))

    if not tool_names:
        AGENT_TOOL_SCOPES.pop(agent_id, None)
        return f"Removed tool scope for agent '{agent_id}' -- now sees all tools."

    AGENT_TOOL_SCOPES[agent_id] = set(tool_names)
    return f"Agent '{agent_id}' scoped to {len(tool_names)} tools."


def get_agent_scope_sync(agent_id: str, employee_id: int, pin: str) -> list[str]:
    """Get the current tool scope for an agent."""
    async def _call(session):
        result = await session.call_tool("list_registered_tools", {})
        all_tools = json.loads(result.content[0].text)
        return _get_visible_tools(all_tools, agent_id)
    return _run(_with_session(_call, employee_id=employee_id, pin=pin))


def call_tool_sync(tool_name: str, arguments: dict, employee_id: int, pin: str) -> str:
    """Generic escape hatch for any other approver tool."""
    async def _call(session):
        result = await session.call_tool(tool_name, arguments)
        return result.content[0].text if result.content else ""
    return _run(_with_session(_call, employee_id=employee_id, pin=pin))