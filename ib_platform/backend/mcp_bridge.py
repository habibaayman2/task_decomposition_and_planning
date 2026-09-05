"""
IronBridge Platform ↔ MCP Server Bridge

Handles both HTTP and stdio transport, with automatic fallback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

# Repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent import mcp_client
from mcp_server.db import get_conn


def _run(coro):
    """Sync wrapper for plain `def` FastAPI routes."""
    return asyncio.run(coro)


def _hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


def _http_kwargs() -> Optional[dict]:
    http_url = os.environ.get("IRONBRIDGE_MCP_URL")
    if not http_url:
        return None
    return dict(
        transport="http",
        http_url=http_url,
        http_token=os.environ.get("IRONBRIDGE_API_TOKEN"),
    )


def _stdio_kwargs() -> dict:
    return dict(
        transport="stdio",
        server_command=[sys.executable, str(REPO_ROOT / "mcp_server" / "server.py")],
    )


async def _call_tool_once(name: str, arguments: dict, kwargs: dict) -> Any:
    """Execute one tool call with a fresh connection."""
    async with mcp_client.connect(**kwargs) as (session, _init_result):
        result = await session.call_tool(name, arguments=arguments)
        if result.content:
            first = result.content[0]
            if hasattr(first, "text"):
                return json.loads(first.text)
        return result.content


async def _call_tool(name: str, arguments: dict) -> Any:
    """
    Call an MCP tool. Tries HTTP first (if configured), then falls back
    to stdio transport automatically on connection failure.
    """
    http = _http_kwargs()
    if http:
        try:
            return await _call_tool_once(name, arguments, http)
        except Exception as exc:
            print(f"[mcp_bridge] HTTP MCP at {http['http_url']} failed: {exc}")
            print(f"[mcp_bridge] Falling back to stdio transport...")

    # stdio fallback — spawns mcp_server/server.py as a local subprocess
    return await _call_tool_once(name, arguments, _stdio_kwargs())


def call_tool_sync(name: str, arguments: dict) -> Any:
    try:
        return _run(_call_tool(name, arguments))
    except Exception as exc:
        traceback.print_exc()
        raise RuntimeError(f"MCP tool '{name}' failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Employee auth (local DB, no MCP round-trip)
# ---------------------------------------------------------------------------

def authenticate_employee(employee_id: int, pin: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT EmployeeID, Name, Role, AuthorizationLevel, ProjectID, PinHash "
            "FROM Employees WHERE EmployeeID = ?",
            (employee_id,),
        ).fetchone()

    if row is None:
        raise ValueError(f"Employee {employee_id} not found.")
    if not row["PinHash"]:
        raise ValueError("This employee has no PIN set — cannot authenticate.")
    if row["PinHash"] != _hash_pin(pin):
        raise ValueError("Incorrect PIN.")

    return dict(row)


# ---------------------------------------------------------------------------
# Agent scoping
# ---------------------------------------------------------------------------

def get_agent_scope_sync(agent_id: str, employee_id: int, pin: str) -> dict:
    emp = authenticate_employee(employee_id, pin)

    # Ask the MCP server for the live tool list
    raw = call_tool_sync("list_registered_tools", {})
    if isinstance(raw, str):
        all_tools = json.loads(raw)
    else:
        all_tools = raw or []

    level = emp["AuthorizationLevel"]
    READ_CREATE = {
        "check_material_inventory",
        "view_project_budget",
        "track_equipment_availability",
        "generate_procurement_report",
        "create_purchase_request",
        "authenticate_as_approver",
        "list_registered_tools",
    }

    scoped = all_tools if level >= 3 else [t for t in all_tools if t in READ_CREATE]

    return {"agent_id": agent_id, "employee_id": employee_id, "tools": scoped}


def get_available_tools_sync() -> List[Dict[str, Any]]:
    raw = call_tool_sync("list_registered_tools", {})
    if isinstance(raw, str):
        return json.loads(raw)
    return raw or []


def list_registered_tools_sync(
    employee_id: int,
    pin: str,
    agent_id: Optional[str] = None,
) -> List[Any]:
    """Return tools available to an authenticated employee.

    Compatibility wrapper for routes that expect list_registered_tools_sync.
    Authorization is delegated to get_agent_scope_sync.
    """
    scope = get_agent_scope_sync(
        agent_id=agent_id or "",
        employee_id=employee_id,
        pin=pin,
    )
    return scope["tools"]


def call_tool_for_agent_sync(
    agent_id: str,
    tool_name: str,
    arguments: dict,
    employee_id: int,
    pin: str,
) -> dict:
    emp = authenticate_employee(employee_id, pin)
    level = emp["AuthorizationLevel"]
    READ_CREATE = {
        "check_material_inventory",
        "view_project_budget",
        "track_equipment_availability",
        "generate_procurement_report",
        "create_purchase_request",
        "authenticate_as_approver",
        "list_registered_tools",
    }

    if level < 3 and tool_name not in READ_CREATE:
        raise PermissionError(
            f"Employee {employee_id} (level {level}) is not authorised "
            f"to call tool '{tool_name}'."
        )

    result = call_tool_sync(tool_name, arguments)
    return {
        "agent_id": agent_id,
        "tool_name": tool_name,
        "arguments": arguments,
        "result": result,
    }


# ---------------------------------------------------------------------------
# HITL & Tickets
# ---------------------------------------------------------------------------

def get_hitl_state_sync(run_id: Optional[str] = None) -> Any:
    with get_conn() as conn:
        if run_id:
            row = conn.execute(
                "SELECT TaskID, RunID, NodeName, Reason, PayloadJSON, Status, Decision, ResolvedBy, CreatedAt, ResolvedAt "
                "FROM HITLTasks WHERE RunID = ? ORDER BY TaskID DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return dict(row) if row else None
        else:
            rows = conn.execute(
                "SELECT TaskID, RunID, NodeName, Reason, PayloadJSON, Status, Decision, ResolvedBy, CreatedAt, ResolvedAt "
                "FROM HITLTasks ORDER BY TaskID DESC",
            ).fetchall()
            return [dict(r) for r in rows]


def resolve_hitl_sync(run_id: str, decision: str, resolved_by: int) -> dict:
    with get_conn() as conn:
        conn.execute(
            "UPDATE HITLTasks SET Status = 'resolved', Decision = ?, ResolvedBy = ?, ResolvedAt = datetime('now') "
            "WHERE RunID = ? AND Status = 'pending'",
            (decision, resolved_by, run_id),
        )
        task = conn.execute(
            "SELECT TaskID, NodeName, Decision FROM HITLTasks WHERE RunID = ? ORDER BY TaskID DESC LIMIT 1",
            (run_id,),
        ).fetchone()

    if task is None:
        raise ValueError(f"No pending HITL task for run {run_id}")

    return {
        "task_id": task["TaskID"],
        "node_name": task["NodeName"],
        "decision": task["Decision"],
        "status": "resolved",
    }


def get_tickets_sync(run_id: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        if run_id:
            rows = conn.execute(
                "SELECT TicketID, RunID, NodeName, ErrorMessage, Status, Resolution, CreatedAt, ResolvedAt "
                "FROM Tickets WHERE RunID = ? ORDER BY TicketID DESC",
                (run_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT TicketID, RunID, NodeName, ErrorMessage, Status, Resolution, CreatedAt, ResolvedAt "
                "FROM Tickets ORDER BY TicketID DESC",
            ).fetchall()
    return [dict(r) for r in rows]


def resolve_ticket_sync(ticket_id: int, resolution: str) -> dict:
    with get_conn() as conn:
        conn.execute(
            "UPDATE Tickets SET Status = 'resolved', Resolution = ?, ResolvedAt = datetime('now') "
            "WHERE TicketID = ? AND Status = 'open'",
            (resolution, ticket_id),
        )
        ticket = conn.execute(
            "SELECT TicketID, RunID, NodeName, Resolution FROM Tickets WHERE TicketID = ?",
            (ticket_id,),
        ).fetchone()

    if ticket is None:
        raise ValueError(f"No open ticket with ID {ticket_id}")

    return {
        "ticket_id": ticket["TicketID"],
        "run_id": ticket["RunID"],
        "node_name": ticket["NodeName"],
        "resolution": ticket["Resolution"],
        "status": "resolved",
    }
