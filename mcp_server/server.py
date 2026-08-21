
import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Optional
from pydantic import BaseModel, Field
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.types as types
import validation
import warnings
warnings.filterwarnings("ignore", message=".*unresolved forward reference.*")
# --------------------------------------------------------------------------
# Database backend selection -- IRONBRIDGE_DB_ENGINE=sqlite (default) or
# ------------------------------------------------------------------------
import db

def _find_policy_dir() -> Path:
    try:
        this_dir = Path(__file__).resolve().parent
    except NameError:
        this_dir = None

    candidates = []
    if this_dir is not None:
        candidates.append(this_dir / "policies")
    cwd = Path.cwd()
    candidates.append(cwd / "policies")
    candidates.append(cwd / "mcp_server" / "policies")

    for candidate in candidates:
        if candidate.exists():
            return candidate

    # Nothing found -- default to cwd/policies so the error, if the
    # resources are actually read, points somewhere sensible.
    return cwd / "policies"


POLICY_DIR = _find_policy_dir()

mcp = FastMCP(
    "ironbridge-procurement-assistant",
    instructions=(
        "Procurement assistant for IronBridge Construction. Read tools are "
        "always available; approve/escalate/reserve tools require calling "
        "authenticate_as_approver first."
    ),
)

# --------------------------------------------------------------------------
# In-memory session state. A real multi-user HTTP deployment needs this
# keyed per transport session, not a module-level dict -- fine for this
# ------------------------------------------------------------------------
SESSION = {"employee": None}  # validation.ActingEmployee once authenticated

EXPENSIVE_PURCHASE_THRESHOLD = validation.EXPENSIVE_PURCHASE_THRESHOLD


def _harden(tool_name: str) -> None:
    tool = mcp._tool_manager._tools[tool_name]
    tool.parameters["additionalProperties"] = False


class ConfirmSchema(BaseModel):
    confirm: bool = Field(description="True to proceed, false to hold/cancel the action.")


# ==========================================================================
# === CONCERN: Defensive tool design (read tools) ==========================
# ==========================================================================
# Every parameter below is typed (int/float/str), constrained where it
# matters (ge=1 on IDs, gt=0 on quantities), and required unless it has an
# explicit default. additionalProperties:false is patched on via _harden()
# below. Business-rule validation (budget, stock, project scope) lives in

@mcp.tool()
async def check_material_inventory(
    category: Annotated[Optional[str], Field(description="e.g. Cement, Steel, Plumbing, Electrical, Concrete")] = None,
    name_contains: Annotated[Optional[str], Field(description="Substring match on material name.")] = None,
) -> str:
    """Look up available construction material inventory, optionally filtered by category or name."""
    rows = db.find_materials(category, name_contains)
    return json.dumps(rows, default=str)


@mcp.tool()
async def view_project_budget(
    project_id: Annotated[int, Field(ge=1)],
) -> str:
    """View a project's total and remaining budget, location, client, and status."""
    project = db.get_project(project_id)
    if project is None:
        return f"No project with id {project_id}."
    return json.dumps(project, default=str)


@mcp.tool()
async def track_equipment_availability(
    equipment_type: Annotated[Optional[str], Field(description="e.g. Excavator, Crane, Concrete Mixer, Bulldozer")] = None,
    site: Annotated[Optional[str], Field(description="Current site name, e.g. 'Riverside Tower'.")] = None,
) -> str:
    """Check construction equipment availability and maintenance status, optionally filtered by type or current site."""
    rows = db.equipment_status(equipment_type, site)
    return json.dumps(rows, default=str)


# --------------------------------------------------------------------------
# === CONCERN: Progress tracking + === CONCERN: Sampling ====================
# --------------------------------------------------------------------------
# Walks every purchase request for a project in a date range one at a
# time (simulating per-record reconciliation work), reporting progress
# after each via ctx.report_progress(). Then hands summarization to the
# CLIENT's model via ctx.session.create_message (sampling/createMessage)
# rather than templating the summary server-side.

@mcp.tool()
async def generate_procurement_report(
    project_id: Annotated[int, Field(ge=1)],
    start_date: Annotated[str, Field(description="YYYY-MM-DD")],
    end_date: Annotated[str, Field(description="YYYY-MM-DD")],
    ctx: Context,
) -> str:
    """Generate a narrative procurement report for a project over a date range. Long-running: reports progress per purchase request, then uses the client's model (sampling) to summarize."""
    rows = db.project_requests_in_range(project_id, start_date, end_date)
    total = max(len(rows), 1)

    processed = []
    for i, row in enumerate(rows, start=1):
        processed.append(row)
        # === CONCERN: Progress tracking
        await ctx.report_progress(progress=i, total=total, message=f"Processed request {row['RequestID']} ({i}/{total})")

    if not processed:
        return "No purchase requests found in that range."

    raw_summary = json.dumps(processed, default=str)

    # === CONCERN: Sampling -- the CLIENT's model writes the narrative.
    result = await ctx.session.create_message(
        messages=[
            types.SamplingMessage(
                role="user",
                content=types.TextContent(
                    type="text",
                    text=(
                        "You are drafting a short procurement report for a construction project "
                        "manager. Summarize the following purchase request records in 3-5 "
                        "sentences: call out pending approvals, escalations, and any expensive "
                        "or rejected requests specifically. Records (JSON):\n\n" + raw_summary
                    ),
                ),
            )
        ],
        max_tokens=400,
        system_prompt="You write concise, factual construction procurement summaries. No fluff.",
    )
    return result.content.text if hasattr(result.content, "text") else str(result.content)


# --------------------------------------------------------------------------
# create_purchase_request -- open write tool, no approver auth needed
# (matches the problem statement: site engineers submit as their normal
# job), but still defensively validated.
# --------------------------------------------------------------------------

@mcp.tool()
async def create_purchase_request(
    project_id: Annotated[int, Field(ge=1)],
    employee_id: Annotated[int, Field(ge=1, description="Employee submitting the request.")],
    material_id: Annotated[int, Field(ge=1)],
    quantity: Annotated[float, Field(gt=0)],
) -> str:
    """Submit a new material purchase request for a project. Open to any employee -- approval is a separate, gated step."""
    project = db.get_project(project_id)
    if project is None:
        return f"Denied: No such project {project_id}."
    if project["Status"] != "Active":
        return f"Denied: Project {project['ProjectID']} is '{project['Status']}', not Active -- cannot submit new requests."

    material = db.get_material(material_id)
    if material is None:
        return f"Denied: No such material {material_id}."

    employee = db.get_employee(employee_id)
    if employee is None:
        return f"Denied: No such employee {employee_id}."

    request_id, cost = db.create_purchase_request(project_id, employee_id, material_id, quantity)
    return f"Created request {request_id} -- {quantity} x {material['MaterialName']}, estimated cost ${cost:,.2f}."


# --------------------------------------------------------------------------
# === CONCERN: Notifications (tools/list_changed) ============================
# --------------------------------------------------------------------------
# The three approver tools below are NOT decorated with @mcp.tool() at
# import time -- they're plain async functions, added to the live server
# via mcp.add_tool() only after a successful authenticate_as_approver
# call, which then pushes send_tool_list_changed(). This is a REAL
# runtime change to the registered tool set (not just a filtered listing)
# -- request_material's tool, approve/escalate/reserve genuinely don't
# exist on the server until someone authenticates.
#
# Handler-level role checks (require_role/require_project_scope) still
# gate each action individually once the tools exist -- being registered
# is not the same as being authorized for a specific approver's role,
# which is why e.g. a Warehouse Supervisor login still can't successfully
# call approve_purchase_request even though the tool is now visible.

async def approve_purchase_request(
    request_id: Annotated[int, Field(ge=1)],
    ctx: Context,
) -> str:
    """Approve a pending purchase request. Requests over $10,000 require explicit human confirmation via elicitation. Requests that exceed the project's remaining budget are rejected outright and must go through escalate_purchase_request instead. Requires an authenticated Project Manager or Finance Officer session."""
    approver = SESSION["employee"]
    try:
        validation.require_role(approver, validation.APPROVER_ROLES)

        req = db.get_purchase_request(request_id)
        validation.validate_request_pending(req)
        project = db.get_project(req["ProjectID"])
        validation.require_project_scope(approver, req["ProjectID"])

        # Hard block: over-budget requests can never be auto-approved --
        # no elicitation can override this; they must be escalated.
        validation.validate_within_budget(req, project)
    except (validation.AuthorizationError, validation.ValidationError) as e:
        return f"Denied: {e}"

    # === CONCERN: Elicitation -- expensive purchases always require
    # explicit human confirmation, independent of the budget check above.
    if validation.needs_elicitation(req["EstimatedCost"]):
        result = await ctx.elicit(
            message=(
                f"Request {req['RequestID']} costs ${req['EstimatedCost']:,.2f}, above the "
                f"${EXPENSIVE_PURCHASE_THRESHOLD:,.2f} auto-approval threshold. Confirm this purchase?"
            ),
            schema=ConfirmSchema,
        )
        if result.action != "accept" or not result.data.confirm:
            return f"Approval for request {req['RequestID']} was not confirmed -- no changes made."

    db.approve_purchase_request(req["RequestID"], approver.id)
    return f"Request {req['RequestID']} approved -- ${req['EstimatedCost']:,.2f}."


async def escalate_purchase_request(
    request_id: Annotated[int, Field(ge=1)],
    reason: Annotated[str, Field(min_length=3, max_length=300)],
) -> str:
    """Escalate a purchase request that exceeds the project's remaining budget to management. Requires an authenticated Project Manager or Finance Officer session."""
    approver = SESSION["employee"]
    try:
        validation.require_role(approver, validation.APPROVER_ROLES)
        req = db.get_purchase_request(request_id)
        if req is None:
            raise validation.ValidationError(f"No such request {request_id}.")
        validation.require_project_scope(approver, req["ProjectID"])
    except (validation.AuthorizationError, validation.ValidationError) as e:
        return f"Denied: {e}"

    db.escalate_purchase_request(req["RequestID"], reason, approver.id)
    return f"Request {req['RequestID']} escalated to management. Reason: {reason}"


async def reserve_material(
    request_id: Annotated[int, Field(ge=1)],
    ctx: Context,
) -> str:
    """Reserve and dispatch material for an approved purchase request. If the reservation would drop stock below MinimumStockLevel, requires explicit confirmation via elicitation per the Warehouse Safety Regulations low-stock workflow. Requires an authenticated Warehouse Supervisor session."""
    approver = SESSION["employee"]
    try:
        validation.require_role(approver, validation.WAREHOUSE_ROLES)
        req = db.get_purchase_request(request_id)
        validation.validate_request_approved(req)
        material = db.get_material(req["MaterialID"])
        validation.validate_sufficient_stock(material, req["Quantity"])
    except (validation.AuthorizationError, validation.ValidationError) as e:
        return f"Denied: {e}"

    # === CONCERN: Elicitation (second, independent trigger) -- a
    # reservation that would breach MinimumStockLevel requires explicit
    # confirmation per the Warehouse Safety Regulations low-stock workflow.
    if validation.would_breach_min_stock(material, req["Quantity"]):
        result = await ctx.elicit(
            message=(
                f"Reserving {req['Quantity']} units of {material['MaterialName']} would drop stock to "
                f"{material['QuantityAvailable'] - req['Quantity']}, below the minimum of "
                f"{material['MinimumStockLevel']}. Proceed anyway?"
            ),
            schema=ConfirmSchema,
        )
        if result.action != "accept" or not result.data.confirm:
            return f"Reservation for request {req['RequestID']} was not confirmed -- stock left untouched."

    db.reserve_material(req["RequestID"], material["MaterialID"], req["Quantity"], approver.id)
    return f"Reserved {req['Quantity']} units of {material['MaterialName']} for request {req['RequestID']}."


# --------------------------------------------------------------------------
# === CONCERN: Extending Notifications (Final Project) -- deregister_tool
# --------------------------------------------------------------------------
# The lab's server could ADD tools at runtime (authenticate_as_approver ->
# mcp.add_tool + send_tool_list_changed) but had no corresponding way to
# REMOVE one -- once unlocked, approver tools stayed registered for the
# rest of the process's life. This closes that gap the same way the
# existing mechanism works: mutate mcp._tool_manager._tools directly, then
# push a genuine tools/list_changed notification so connected clients
# actually re-fetch the tool list rather than caching a stale one.
#
# This is also the admin platform's live "toggle a tool off" surface --
# the admin backend calls this exact tool through mcp_client.connect()
# (see ib_platform/backend/mcp_bridge.py) rather than reaching into the
# server's internals directly.

async def deregister_tool(
    tool_name: Annotated[str, Field(min_length=1, description="Exact tool name to remove, e.g. 'reserve_material'.")],
    ctx: Context,
) -> str:
    """Remove a previously-registered tool from the live server and notify connected clients via tools/list_changed. Cannot remove an always-on tool or deregister_tool itself. Requires an authenticated Project Manager or Finance Officer session."""
    approver = SESSION["employee"]
    try:
        validation.require_role(approver, validation.APPROVER_ROLES)
    except (validation.AuthorizationError, validation.ValidationError) as e:
        return f"Denied: {e}"

    if tool_name in ALWAYS_ON_TOOLS or tool_name == "deregister_tool":
        return f"Denied: '{tool_name}' cannot be deregistered."

    existed = tool_name in mcp._tool_manager._tools
    if existed:
        del mcp._tool_manager._tools[tool_name]
        await ctx.session.send_tool_list_changed()

    return f"Tool '{tool_name}' {'deregistered' if existed else 'was not registered -- nothing to do'}."


APPROVER_TOOL_FNS = {
    "approve_purchase_request": approve_purchase_request,
    "escalate_purchase_request": escalate_purchase_request,
    "reserve_material": reserve_material,
    "deregister_tool": deregister_tool,
}

# Tools hardened at import time (see the loop below) can never be
# deregistered -- they're the fixed, always-available surface every
# session gets regardless of authentication.
ALWAYS_ON_TOOLS = {
    "check_material_inventory", "view_project_budget", "track_equipment_availability",
    "generate_procurement_report", "create_purchase_request", "authenticate_as_approver",
    "list_registered_tools",
}


@mcp.tool()
async def list_registered_tools() -> str:
    """List every tool currently registered on the server, including any approver tools unlocked earlier this process by authenticate_as_approver. Useful for the admin platform to show what's actually callable right now, not just what's documented."""
    return json.dumps(sorted(mcp._tool_manager._tools.keys()))


@mcp.tool()
async def authenticate_as_approver(
    employee_id: Annotated[int, Field(ge=1)],
    pin: Annotated[str, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")],
    ctx: Context,
) -> str:
    """Elevate the session to an approver identity (Project Manager, Finance Officer, or Warehouse Supervisor) by PIN, unlocking approve/escalate/reserve tools for the rest of the session."""
    employee = db.get_employee(employee_id)
    if employee is None or not employee.get("PinHash"):
        return "Authentication failed: no such approver account."
    if hashlib.sha256(pin.encode()).hexdigest() != employee["PinHash"]:
        return "Authentication failed: incorrect PIN."

    SESSION["employee"] = validation.ActingEmployee(
        id=employee["EmployeeID"], name=employee["Name"], role=employee["Role"], project_id=employee["ProjectID"]
    )

    # === CONCERN: Notifications --register new tools, then tell the client.
    already_added = "approve_purchase_request" in mcp._tool_manager._tools
    if not already_added:
        for name, fn in APPROVER_TOOL_FNS.items():
            mcp.add_tool(fn, name=name)
            _harden(name)
        await ctx.session.send_tool_list_changed()

    return f"Authenticated as {employee['Name']} ({employee['Role']}). Approver tools are now available for this session."


# Harden the always-on tools' schemas at import time.
for _name in ("check_material_inventory", "view_project_budget", "track_equipment_availability",
              "generate_procurement_report", "create_purchase_request", "authenticate_as_approver",
              "list_registered_tools"):
    _harden(_name)


# ==========================================================================
# === CONCERN: Resources ====================================================
# ==========================================================================
# The two safety policy documents are data the model should read and
# reason over once, not a function it calls per-question.

@mcp.resource("policy://material-handling", name="Material Handling Procedures", mime_type="text/markdown")
def material_handling_policy() -> str:
    return (POLICY_DIR / "material_handling_procedures.md").read_text()


@mcp.resource("policy://warehouse-safety", name="Warehouse Safety Regulations", mime_type="text/markdown")
def warehouse_safety_policy() -> str:
    return (POLICY_DIR / "warehouse_safety_regulations.md").read_text()

@mcp.resource("policy://equipment-operation", name="Equipment Operation Safety Rules", mime_type="text/markdown")
def equipment_operation_policy() -> str:
    return (POLICY_DIR / "equipment_operation_safety_rules.md").read_text()



# ==========================================================================
# === CONCERN: Prompts ======================================================
# ==========================================================================

@mcp.prompt()
def draft_purchase_justification(request_id: str) -> str:
    """Draft a short justification note for an expensive purchase request, for the requester to attach before submitting it to a Project Manager for approval."""
    req = db.get_purchase_request(int(request_id))
    if req is None:
        raise ValueError(f"No purchase request found for request_id {request_id}")
    material = db.get_material(req["MaterialID"])

    return (
        f"Draft a brief, plain-language justification for purchase request {request_id}: "
        f"{req['Quantity']} units of {material['MaterialName']}, estimated cost "
        f"${req['EstimatedCost']:,.2f}. Explain why this material is needed for the project "
        "and why the quantity/cost is reasonable. Keep it to 2-3 sentences, no jargon."
    )


# ==========================================================================
# === CONCERN: Capability negotiation =======================================
# ==========================================================================
# See the module docstring: FastMCP's own run() would declare
# tools.listChanged=False (its create_initialization_options() default).
# We call the same underlying method with the flag we actually need.

def make_init_options() -> InitializationOptions:
    return mcp._mcp_server.create_initialization_options(
        notification_options=NotificationOptions(
            tools_changed=True,   # required: we push tools/list_changed on approver login
            prompts_changed=False,
            resources_changed=False,
        )
    )


# ==========================================================================
# === CONCERN: Transport (both) =============================================
# ==========================================================================

async def main():
    transport = os.environ.get("TRANSPORT", "stdio")
    init_options = make_init_options()

    if transport == "http":
        import uvicorn
        from http_app import build_asgi_app
        app = build_asgi_app(mcp._mcp_server, init_options)
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
    else:
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            await mcp._mcp_server.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    import asyncio

    try:
        _running_loop = asyncio.get_running_loop()
    except RuntimeError:
        _running_loop = None

    if _running_loop is not None and _running_loop.is_running():
        _task = _running_loop.create_task(main())
        print(
            "Detected an already-running event loop. "
            "Scheduled server main() as a background task instead of calling "
            "asyncio.run(). If you need to wait for it, `await _task`."
        )
    else:
        asyncio.run(main())
