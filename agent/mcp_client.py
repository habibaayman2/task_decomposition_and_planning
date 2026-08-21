"""
Shared MCP client wiring for the IronBridge procurement agent.

This module owns every protocol-level concern that lives on the CLIENT
side of the handshake:

  * Capability negotiation -- passing real `sampling_callback` /
    `elicitation_callback` implementations (not the SDK's stub
    defaults) is what causes the client to actually *declare*
    sampling + elicitation support in its InitializeRequest. After
    `initialize()` we also read back the SERVER's declared
    capabilities and only rely on the ones it actually advertised --
    e.g. we only register a tools/list_changed reaction if
    `result.capabilities.tools.listChanged` is true, rather than
    assuming every server pushes that notification.
  * Notifications -- `message_handler` below reacts to a genuine
    ToolListChangedNotification by re-fetching the tool list, instead
    of polling or guessing when the approver tools appear.
  * Elicitation -- `make_elicitation_callback` is a REAL human-in-the-
    loop prompt: it inspects the server's requestedSchema and asks the
    person at the terminal, it does not auto-approve.
  * Sampling -- `make_sampling_callback` forwards the request to the
    CLIENT's own model (a direct Groq API call from *this*
    process), never the server's model -- the server has no model of
    its own to fall back on.

Both transports the assignment asks for (stdio for development,
Streamable HTTP for the deployed version) are supported by
`connect()` below via a single `transport` argument, so agent.py and
demo_scenario.py don't need their own connection logic.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.shared.context import RequestContext
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client


# --------------------------------------------------------------------------
# Elicitation -- real human-in-the-loop, not silent auto-accept.
# --------------------------------------------------------------------------
# The callback below is generic: it inspects params.requestedSchema
# (a plain JSON Schema dict) and prompts once per property, rather than
# hard-coding IronBridge's ConfirmSchema shape. That keeps the agent
# correct even if the server adds a differently-shaped elicitation
# later.

def make_elicitation_callback(auto_answers: Optional[dict] = None):
    """
    auto_answers: if given, a dict of {property_name: value} used to
    answer elicitations non-interactively (used by demo_scenario.py so
    the demo is repeatable rather than requiring a human to sit at the
    keyboard). If None (the default for the interactive agent), the
    person is actually prompted at the terminal -- this is the genuine
    elicitation path.
    """

    async def elicitation_callback(
        context: RequestContext,
        params: types.ElicitRequestFormParams,
    ) -> types.ElicitResult | types.ErrorData:
        print(f"\n[elicitation] {params.message}")

        properties = (params.requestedSchema or {}).get("properties", {})

        if auto_answers is not None:
            missing = [k for k in properties if k not in auto_answers]
            if missing:
                return types.ErrorData(
                    code=types.INVALID_REQUEST,
                    message=f"demo script has no canned answer for {missing}",
                )
            print(f"[elicitation] auto-answering (scripted demo): {auto_answers}")
            return types.ElicitResult(action="accept", content=auto_answers)

        # Genuine interactive path: ask a real person.
        content: dict[str, Any] = {}
        for name, spec in properties.items():
            prompt = spec.get("description", name)
            if spec.get("type") == "boolean":
                raw = await asyncio.to_thread(input, f"  {prompt} [y/n]: ")
                content[name] = raw.strip().lower() in ("y", "yes", "true", "1")
            else:
                raw = await asyncio.to_thread(input, f"  {prompt}: ")
                content[name] = raw

        confirmed = await asyncio.to_thread(
            input, "Submit this response to the server? [y/n]: "
        )
        if confirmed.strip().lower() not in ("y", "yes"):
            return types.ElicitResult(action="decline")

        return types.ElicitResult(action="accept", content=content)

    return elicitation_callback


# --------------------------------------------------------------------------
# Sampling -- the CLIENT's own model, called directly from this process.
# --------------------------------------------------------------------------
# Deliberately NOT calling back into the MCP server for this -- the
# whole point of sampling/createMessage is that the server has no model
# of its own and borrows the client's.

def make_sampling_callback():
    async def sampling_callback(
        context: RequestContext,
        params: types.CreateMessageRequestParams,
    ) -> types.CreateMessageResult | types.ErrorData:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            return types.ErrorData(
                code=types.INVALID_REQUEST,
                message=(
                    "Client declared sampling support but has no "
                    "GROQ_API_KEY configured -- cannot fulfill this "
                    "sampling/createMessage request."
                ),
            )

        from groq import Groq

        client = Groq(api_key=api_key)

        # Groq's chat.completions API (OpenAI-compatible) takes the system
        # prompt as a normal message with role="system" at the front of the
        # list, unlike Anthropic's separate top-level `system` parameter.
        groq_messages = []
        if params.systemPrompt:
            groq_messages.append({"role": "system", "content": params.systemPrompt})
        groq_messages += [
            {
                "role": m.role,
                "content": m.content.text if hasattr(m.content, "text") else str(m.content),
            }
            for m in params.messages
        ]

        print(f"\n[sampling] server asked the CLIENT's model to reason over "
              f"{len(groq_messages)} message(s) ({params.maxTokens} max tokens)")

        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            max_tokens=params.maxTokens or 512,
            messages=groq_messages,
        )
        text = response.choices[0].message.content or ""

        return types.CreateMessageResult(
            role="assistant",
            content=types.TextContent(type="text", text=text),
            model=response.model,
            stopReason="endTurn",
        )

    return sampling_callback


# --------------------------------------------------------------------------
# Notifications -- react to a genuine tools/list_changed push.
# --------------------------------------------------------------------------

def make_message_handler(on_tools_changed):
    """
    IMPORTANT: the SDK awaits this handler *inline*, inside the single
    background task that also reads every response off the wire (see
    mcp.shared.session.BaseSession._receive_loop). If this handler
    turned around and awaited a new request itself (e.g.
    `await session.list_tools()`), that new request's response could
    never be delivered -- the very loop that would deliver it is the
    one blocked awaiting us. So we only ever schedule the reaction as
    a separate task and return immediately.
    """

    async def message_handler(message) -> None:
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root, types.ToolListChangedNotification
        ):
            print("\n[notification] tools/list_changed received from server -- refreshing tool list")
            asyncio.create_task(on_tools_changed())

    return message_handler


# --------------------------------------------------------------------------
# Connection -- stdio (dev) or Streamable HTTP (deployed), same caller API.
# --------------------------------------------------------------------------

@asynccontextmanager
async def connect(
    transport: str = "stdio",
    server_command: Optional[list[str]] = None,
    server_cwd: Optional[str] = None,
    http_url: Optional[str] = None,
    http_token: Optional[str] = None,
    auto_elicit_answers: Optional[dict] = None,
    on_tools_changed=None,
):
    """
    Yields a ready `ClientSession` (post-initialize) plus the raw
    InitializeResult, so callers can inspect server-declared
    capabilities before relying on them.
    """
    sampling_cb = make_sampling_callback()
    elicit_cb = make_elicitation_callback(auto_elicit_answers)
    msg_handler = make_message_handler(on_tools_changed or (lambda: asyncio.sleep(0)))

    if transport == "stdio":
        command = server_command or [sys.executable, "mcp_server/server.py"]
        params = StdioServerParameters(command=command[0], args=command[1:], cwd=server_cwd)
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read,
                write,
                sampling_callback=sampling_cb,
                elicitation_callback=elicit_cb,
                message_handler=msg_handler,
            ) as session:
                init_result = await session.initialize()
                yield session, init_result

    elif transport == "http":
        if not http_url:
            raise ValueError("http_url is required when transport='http'")
        headers = {"Authorization": f"Bearer {http_token}"} if http_token else None
        async with streamablehttp_client(http_url, headers=headers) as (read, write, _get_session_id):
            async with ClientSession(
                read,
                write,
                sampling_callback=sampling_cb,
                elicitation_callback=elicit_cb,
                message_handler=msg_handler,
            ) as session:
                init_result = await session.initialize()
                yield session, init_result

    else:
        raise ValueError(f"Unknown transport {transport!r}, expected 'stdio' or 'http'")


def check_capability(init_result, path: str) -> bool:
    """
    Client-side half of capability negotiation: walk a dotted path
    (e.g. 'tools.listChanged') off the server's declared capabilities
    and return False (never raise) if the server didn't advertise it.
    Callers use this to decide whether to rely on a capability instead
    of assuming every server supports everything.
    """
    node = init_result.capabilities
    for part in path.split("."):
        node = getattr(node, part, None)
        if node is None:
            return False
    return bool(node)
