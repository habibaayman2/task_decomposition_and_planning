"""
Fixed, repeatable demo scenario for the IronBridge procurement MCP
server. Every step below is a scripted call (no LLM in the loop) so the
transcript is reproducible run to run -- see the lab guardrails
("keep a small, fixed set of test inputs ... not lucky").

Run:
    python3 agent/demo_scenario.py

Requires the server importable from mcp_server/ and its db/ next to
it (see mcp_client.connect(server_cwd=...) below). GROQ_API_KEY is
optional: without it, every concern except the final sampling call
inside generate_procurement_report still fires and is shown; the
report step will show the server's ErrorData response explaining why
sampling failed, which is itself the correct "client capability not
usable" behavior, not a crash.

Each step is labelled with the protocol concern it demonstrates.
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
import mcp_client

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(REPO_ROOT, ".env"))  # picks up GROQ_API_KEY if
                                                # a .env file exists; no-op
                                                # otherwise.


def header(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def show(label: str, result) -> None:
    if result.isError:
        text = "".join(b.text for b in result.content if hasattr(b, "text"))
        print(f"{label} -> ERROR: {text}")
    else:
        text = "".join(b.text for b in result.content if hasattr(b, "text"))
        print(f"{label} -> {text}")


async def main():
    tools_cache = {"names": []}

    async def refresh_tools():
        result = await SESSION.list_tools()
        tools_cache["names"] = [t.name for t in result.tools]
        print(f"[notification handler] tool set is now: {tools_cache['names']}")

    async with mcp_client.connect(
        transport="stdio",
        server_command=[sys.executable, "mcp_server/server.py"],
        server_cwd=REPO_ROOT,
        # Fixed answers so every elicitation in this script resolves the
        # same way every run -- swap to None (interactive) in agent.py.
        auto_elicit_answers={"confirm": True},
        on_tools_changed=lambda: refresh_tools(),
    ) as (session, init_result):
        global SESSION
        SESSION = session

        # ------------------------------------------------------------
        # CONCERN: Capability negotiation
        # ------------------------------------------------------------
        header("CONCERN: Capability negotiation")
        print(f"Server: {init_result.serverInfo.name} v{init_result.serverInfo.version}")
        print(f"Declared server capabilities: {init_result.capabilities}")
        supports_tool_notifications = mcp_client.check_capability(init_result, "tools.listChanged")
        print(f"Client-side check -- server advertises tools.listChanged: {supports_tool_notifications}")
        if not supports_tool_notifications:
            print("(If this were False, we would NOT wait around for a "
                  "tools/list_changed push -- we'd poll list_tools() "
                  "manually instead, since relying on an unadvertised "
                  "capability is exactly the mistake this check prevents.)")

        # ------------------------------------------------------------
        # Initial tool discovery (always-on / read tools only, pre-auth)
        # ------------------------------------------------------------
        header("Initial tool discovery (no approver session yet)")
        await refresh_tools()

        # ------------------------------------------------------------
        # CONCERN: Resources -- read, don't call, the policy documents
        # ------------------------------------------------------------
        header("CONCERN: Resources")
        resources = await session.list_resources()
        for r in resources.resources:
            print(f"Resource available: {r.uri}  ({r.name})")
        read = await session.read_resource(resources.resources[0].uri)
        print(f"Read {resources.resources[0].uri} -> {len(read.contents[0].text)} chars, "
              f"first line: {read.contents[0].text.splitlines()[0]!r}")

        # ------------------------------------------------------------
        # CONCERN: Prompts
        # ------------------------------------------------------------
        header("CONCERN: Prompts")
        prompts = await session.list_prompts()
        print(f"Prompts available: {[p.name for p in prompts.prompts]}")

        # ------------------------------------------------------------
        # Read tools (always available, no auth)
        # ------------------------------------------------------------
        header("Read-only tools (no auth required)")
        show("check_material_inventory(category=Steel)",
             await session.call_tool("check_material_inventory", {"category": "Steel"}))
        show("view_project_budget(project_id=2)",
             await session.call_tool("view_project_budget", {"project_id": 2}))
        show("track_equipment_availability(site='Ironbridge Overpass')",
             await session.call_tool("track_equipment_availability", {"site": "Ironbridge Overpass"}))

        # ------------------------------------------------------------
        # Open write tool: any employee can submit a request
        # ------------------------------------------------------------
        header("create_purchase_request -- open write tool, still validated")
        # 15 units of Steel (MaterialID 2, UnitPrice 780) = $11,700:
        #   - over the $10k elicitation threshold
        #   - within Project 2's $210,000 remaining budget
        #   - within the 18 units currently in stock
        #   - but will drop stock to 3, under the MinimumStockLevel of 20
        # chosen deliberately so this one request can later demonstrate
        # BOTH elicitation triggers (expensive-approval, then low-stock).
        create_result = await session.call_tool(
            "create_purchase_request",
            {"project_id": 2, "employee_id": 7, "material_id": 2, "quantity": 15},
        )
        show("create_purchase_request(project=2, employee=7/Layla, material=2/Steel, qty=15)", create_result)
        created_text = "".join(b.text for b in create_result.content if hasattr(b, "text"))
        new_request_id = int(created_text.split("request ")[1].split(" ")[0])
        print(f"(parsed new RequestID = {new_request_id})")

        # ------------------------------------------------------------
        # CONCERN: Notifications (part 1) -- tool doesn't exist yet
        # ------------------------------------------------------------
        header("Attempting an approver action before authenticating")
        try:
            result = await session.call_tool("approve_purchase_request", {"request_id": new_request_id})
            show("approve_purchase_request (pre-auth)", result)
        except Exception as e:
            print(f"approve_purchase_request (pre-auth) -> tool not found, as expected: {e}")

        # ------------------------------------------------------------
        # CONCERN: Notifications (part 2) -- real tools/list_changed push
        # ------------------------------------------------------------
        header("CONCERN: Notifications -- authenticating flips on the approver tools")
        show("authenticate_as_approver(Sami, PIN 1108)",
             await session.call_tool("authenticate_as_approver", {"employee_id": 6, "pin": "1108"}))
        # The tools/list_changed reaction runs as a background task (see
        # mcp_client.make_message_handler) -- give it a beat to finish
        # and print before we move on.
        await asyncio.sleep(0.3)

        # ------------------------------------------------------------
        # CONCERN: Elicitation (trigger #1) -- expensive-purchase confirmation
        # ------------------------------------------------------------
        header("CONCERN: Elicitation -- expensive purchase requires confirmation")
        show(f"approve_purchase_request({new_request_id}) as Sami (PM, Project 2)",
             await session.call_tool("approve_purchase_request", {"request_id": new_request_id}))

        # ------------------------------------------------------------
        # Authorization: project-scope check blocks cross-project approval
        # ------------------------------------------------------------
        header("Defensive tool design -- project-scope authorization check")
        show("approve_purchase_request(2) as Sami (manages Project 2, request belongs to Project 1)",
             await session.call_tool("approve_purchase_request", {"request_id": 2}))

        # ------------------------------------------------------------
        # Hard budget block (never auto-approved, elicitation doesn't apply)
        # ------------------------------------------------------------
        header("Defensive tool design -- hard budget block (no elicitation override)")
        show("authenticate_as_approver(Omar, Finance, PIN 3390)",
             await session.call_tool("authenticate_as_approver", {"employee_id": 4, "pin": "3390"}))
        await asyncio.sleep(0.3)
        show("approve_purchase_request(2) as Omar (Finance, cross-project) -- exceeds remaining budget",
             await session.call_tool("approve_purchase_request", {"request_id": 2}))
        show("escalate_purchase_request(2, reason=...) as Omar",
             await session.call_tool("escalate_purchase_request", {
                 "request_id": 2,
                 "reason": "Cost exceeds Project 1's remaining budget; needs management sign-off.",
             }))

        # ------------------------------------------------------------
        # CONCERN: Elicitation (trigger #2) -- low-stock confirmation
        # ------------------------------------------------------------
        header("CONCERN: Elicitation -- reservation would breach MinimumStockLevel")
        show("authenticate_as_approver(Dalia, Warehouse Supervisor, PIN 7744)",
             await session.call_tool("authenticate_as_approver", {"employee_id": 5, "pin": "7744"}))
        await asyncio.sleep(0.3)
        show(f"reserve_material({new_request_id}) as Dalia",
             await session.call_tool("reserve_material", {"request_id": new_request_id}))

        # ------------------------------------------------------------
        # CONCERN: Progress tracking + Sampling
        # ------------------------------------------------------------
        header("CONCERN: Progress tracking + Sampling")

        async def on_progress(progress, total, message):
            print(f"[progress] {message} ({progress}/{total})")

        try:
            result = await session.call_tool(
                "generate_procurement_report",
                {"project_id": 2, "start_date": "2026-01-01", "end_date": "2026-12-31"},
                progress_callback=on_progress,
            )
            show("generate_procurement_report(project=2)", result)
        except Exception as e:
            print(f"generate_procurement_report -> {e}")
            if not os.environ.get("GROQ_API_KEY"):
                print("(Expected without GROQ_API_KEY set -- progress "
                      "notifications above still prove progress tracking "
                      "works; only the final sampling/createMessage call "
                      "needs a real client model to complete.)")

        header("Demo complete")


if __name__ == "__main__":
    asyncio.run(main())
