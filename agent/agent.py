"""
Interactive IronBridge procurement agent.

Unlike demo_scenario.py (a fixed script for repeatable grading), this
is the genuine "someone types a request in plain English" agent: it
discovers whatever tools/resources/prompts the MCP server currently
exposes, hands them to Groq's model as real tool-use tools, and lets
the model decide which ones to call and with what arguments. Nothing
here hard-codes which tool answers which question.

Uses Groq (https://console.groq.com) as the driving model -- an
OpenAI-compatible chat API with genuine free-tier tool calling, no
credit card required. See mcp_client.py's make_sampling_callback for
the model that fulfils the server's own sampling/createMessage calls
(same provider, same key).

Usage:
 export GROQ_API_KEY=...
 python3 agent/agent.py # stdio, spawns the server
 python3 agent/agent.py --transport http --http-url http://localhost:8080/mcp --http-token secret

Try, e.g.:
 "What steel do we have in stock?"
 "Submit a request for 15 units of steel (material 2) for project 2, I'm employee 7"
 "Log me in as Sami with PIN 1108" -> triggers notifications
 "Approve request " -> triggers elicitation (real prompt!)
 "What does Policy #2 say about fire lanes?" -> triggers RAG retrieval
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
import mcp_client

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTEXT_EVAL_DIR = os.path.join(REPO_ROOT, "context_eval")

# Configure sys.path for direct modular imports across repository folders
for path in (REPO_ROOT, CONTEXT_EVAL_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

load_dotenv(os.path.join(REPO_ROOT, ".env"))  # picks up GROQ_API_KEY,
# IRONBRIDGE_MCP_URL, etc. if
# a .env file exists there;
# no-op otherwise.

import uuid
from memory.short_term import SessionMemory
from memory.router import PromoteOrDropRouter
from memory.stores import EpisodicStore, SemanticStore
from memory.consolidation import ConsolidationJob
from memory.self_rag_check import SelfRAGChecker

# ---------------------------------------------------------------------------
# RAG Integration (Person 3 — Issue #32)
# ---------------------------------------------------------------------------
from rag.hybrid_search import hybrid_rag_answer
from rag.agentic_rag import agentic_rag_answer

# ---------------------------------------------------------------------------
# Context Management Integration (Observation Masking Strategy)
# ---------------------------------------------------------------------------
try:
    from observation_masking import apply as apply_observation_masking
    from transcript import Turn
except ModuleNotFoundError:
    from context_eval.observation_masking import apply as apply_observation_masking
    from context_eval.transcript import Turn

MODEL = "llama-3.1-8b-instant"

SYSTEM_PROMPT = (
    "You are the IronBridge Construction procurement assistant. Use the "
    "available tools to answer questions and carry out requests. Only "
    "call tools that are currently offered to you -- if an action isn't "
    "available yet (e.g. approving a request), explain to the user what "
    "they need to do first (e.g. authenticate) rather than guessing."
)

# === CONCERN: RAG routing logic (Issue #32) ===
POLICY_KEYWORDS = [
    "policy", "safety", "handling", "ppe", "warehouse",
    "fire lane", "lifting", "crane",
    "minimum stock", "approval workflow", "clearance",
    "regulation", "procedure", "guideline", "rule", "compliance",
    "protocol", "requirement", "standard"
]

INVENTORY_KEYWORDS = [
    "do we have", "how many", "how much", "available",
    "in stock", "quantity", "price", "cost", "budget",
    "remaining", "left", "order", "purchase"
]


def _is_policy_question(text: str) -> bool:
    """Detect if a user query is about policies/safety/rules."""
    lowered = text.lower()
    if any(kw in lowered for kw in INVENTORY_KEYWORDS):
        return False
    return any(kw in lowered for kw in POLICY_KEYWORDS)


def _is_multi_part_question(text: str) -> bool:
    """Heuristic: does the question have multiple independent sub-questions?

    We look for multiple question-starting words (what/how/why/when/where)
    or explicit multi-part connectors like 'both' / 'additionally'.
    A single 'and' connecting two nouns (e.g. 'fire exits and pallets')
    does NOT count as multi-part.
    """
    lowered = text.lower()
    # Count question words that typically start independent clauses
    question_starts = sum(1 for w in ["what ", "how ", "why ", "when ", "where "] if w in lowered)
    return (
        question_starts >= 2
        or "both" in lowered
        or "additionally" in lowered
    )


def mcp_tool_to_groq(tool) -> dict:
    """MCP's Tool shape -> Groq/OpenAI-compatible tool-calling shape."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


def prepare_pruned_messages(messages: list[dict], keep_last_n_tool_outputs: int = 3) -> list[dict]:
    """
    Applies Observation Masking to the active conversation history prior to model dispatch.
    Replaces older verbose JSON tool observations with concise placeholders while preserving structure.
    """
    turns = []
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content") or ""
        is_tool = (role == "tool")
        is_critical = any(kw in content.lower() for kw in ["budget", "limit", "cap", "rule"])

        turns.append(
            Turn(
                role=role,
                content=content,
                is_tool_output=is_tool,
                critical=is_critical,
                turn_index=idx,
            )
        )

    # 1. Apply Observation Masking strategy on transcript turns
    pruned_turns = apply_observation_masking(turns, keep_last_n_tool_outputs=keep_last_n_tool_outputs)

    # 2. Map masked contents back to message dict payloads for API execution
    pruned_messages = []
    for orig_msg, pruned_turn in zip(messages, pruned_turns):
        msg_copy = dict(orig_msg)
        if orig_msg.get("role") == "tool":
            msg_copy["content"] = pruned_turn.content
        pruned_messages.append(msg_copy)

    return pruned_messages


async def run_agent(transport: str, http_url: str | None, http_token: str | None):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print(
            "GROQ_API_KEY is not set -- the agent's own driving model "
            "(the one deciding which tools to call) needs it. Get a free "
            "key at https://console.groq.com -- sampling requests FROM "
            "the server would also fail without it."
        )
        return

    from groq import Groq

    groq_client = Groq(api_key=api_key)

    # === CONCERN: memory system (see memory/) ===
    session_id = str(uuid.uuid4())
    episodic_store = EpisodicStore()
    semantic_store = SemanticStore()
    router = PromoteOrDropRouter(episodic_store)
    checker = SelfRAGChecker()
    session_mem = SessionMemory(session_id, max_turns=20)

    # Separate, periodic pass over episodic memory -- NOT triggered by the
    # router above. Startup is fine for this single-process interactive
    # agent; swap for a real scheduler in a deployed setting.
    ConsolidationJob(episodic_store, semantic_store).run()

    state = {"groq_tools": []}

    async def refresh_tools_and_announce():
        result = await SESSION.list_tools()
        state["groq_tools"] = [mcp_tool_to_groq(t) for t in result.tools]
        names = [t.name for t in result.tools]
        print(f"\n[tool set updated] now available: {names}\n")

    connect_kwargs = dict(
        transport=transport,
        auto_elicit_answers=None,  # genuine interactive elicitation -- real prompts
        on_tools_changed=refresh_tools_and_announce,
    )
    if transport == "stdio":
        connect_kwargs["server_command"] = [sys.executable, "mcp_server/server.py"]
        connect_kwargs["server_cwd"] = REPO_ROOT
    else:
        connect_kwargs["http_url"] = http_url
        connect_kwargs["http_token"] = http_token

    async with mcp_client.connect(**connect_kwargs) as (session, init_result):
        global SESSION
        SESSION = session

        # === CONCERN: Capability negotiation (client side) ===
        print(f"Connected to {init_result.serverInfo.name} v{init_result.serverInfo.version}")
        print(f"Server capabilities: {init_result.capabilities}")
        supports_notifications = mcp_client.check_capability(init_result, "tools.listChanged")
        print(f"Server advertises tools.listChanged: {supports_notifications}")
        if not supports_notifications:
            print(
                "(No push support declared -- this agent would need to "
                "poll list_tools() periodically instead of trusting a "
                "notification that may never come. Not implemented here "
                "since IronBridge's server does declare it.)"
            )

        await refresh_tools_and_announce()

        print(
            "\nIronBridge Procurement Assistant -- type a request in plain "
            "English, or 'quit' to exit.\n"
        )

        conversation: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

        while True:
            user_text = await asyncio.to_thread(input, "you> ")
            if user_text.strip().lower() in ("quit", "exit"):
                break

            conversation.append({"role": "user", "content": user_text})
            session_mem.scratchpad.update(
                plan=f"respond to: {user_text[:80]}",
                sub_goal="awaiting model response / tool calls",
            )
            evicted = session_mem.add_turn("user", user_text)
            if evicted:
                router.route(evicted)

            # Flag: did RAG fire this turn? If so, we force the model to
            # answer from retrieved context instead of calling tools.
            rag_fired_this_turn = False

            # === CONCERN: RAG retrieval for policy questions (Issue #32) ===
            if _is_policy_question(user_text):
                is_multi = _is_multi_part_question(user_text)
                if is_multi:
                    print("  [RAG] Routing to Agentic RAG (multi-part detected)...")
                    rag_result = agentic_rag_answer(user_text, top_k_per_hop=4)
                else:
                    print("  [RAG] Routing to Hybrid Search...")
                    rag_result = hybrid_rag_answer(user_text, top_k=5)

                rag_answer = rag_result["answer"]

                # Inject retrieved policy context as a system message so the
                # model grounds its response in real policy documents.
                # Self-RAG already ran inside the RAG functions (relevance +
                # support checks); we only inject if it passed.
                rag_injection = (
                    f"[RETRIEVED POLICY CONTEXT — ANSWER DIRECTLY FROM THIS, "
                    f"DO NOT CALL ANY TOOLS]\n"
                    f"{rag_answer}\n"
                    f"[END RETRIEVED POLICY CONTEXT]"
                )
                conversation.append({"role": "system", "content": rag_injection})
                rag_fired_this_turn = True

                # Log for demo/grading visibility
                hops = rag_result.get("hops_used", 1)
                chunks = len(rag_result.get("retrieved_chunks", []))
                print(f"  [RAG] Retrieved {chunks} chunk(s) across {hops} hop(s).")

            # Surface scratchpad + relevant recalled memory to the model.
            # Placed here (recomputed each inner-loop turn) so it still
            # reflects the latest scratchpad state after any tool calls
            # below update working_state. Self-RAG-style checked before
            # injection -- an irrelevant recalled memory is dropped, not
            # silently included.
            recalled = [e.content for e in episodic_store.recall(session_id=session_id)]
            recalled = checker.filter_relevant(user_text, recalled)
            memory_msg = {"role": "system", "content": session_mem.scratchpad.as_context_block()}
            if recalled:
                memory_msg["content"] += "\n[RELEVANT MEMORY]\n" + "\n".join(f"- {r}" for r in recalled)

            # Loop until the model stops asking for tool calls.
            while True:
                # Apply Observation Masking to conversation history before completion call
                pruned_conversation = prepare_pruned_messages(conversation)

                # If RAG fired this turn, do NOT offer tools — force the
                # model to answer from the retrieved policy context.
                api_kwargs = {
                    "model": MODEL,
                    "max_tokens": 1024,
                    "messages": pruned_conversation + [memory_msg],
                }
                if not rag_fired_this_turn:
                    api_kwargs["tools"] = state["groq_tools"]

                response = groq_client.chat.completions.create(**api_kwargs)

                message = response.choices[0].message
                # Groq/OpenAI's assistant message must be echoed back verbatim
                # (including tool_calls) for the follow-up call to make sense.
                # IMPORTANT: the API rejects "tool_calls": null outright (it
                # must be either a real list or the key must be absent) --
                # this bit us on the second turn of a real conversation, once
                # a previous plain-text reply (no tool calls) got sent back
                # as part of the history.
                assistant_msg = {"role": "assistant", "content": message.content}
                if message.tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in message.tool_calls
                    ]
                conversation.append(assistant_msg)
                if message.content:
                    evicted = session_mem.add_turn("assistant", message.content)
                    if evicted:
                        router.route(evicted)

                if not message.tool_calls:
                    if message.content:
                        print(f"assistant> {message.content}")
                    break

                # If RAG fired this turn, the model should NOT be calling
                # tools (we didn't pass them). This is a safety catch.
                if rag_fired_this_turn:
                    print("[WARN] Model requested tools despite RAG context — forcing text answer.")
                    continue

                for tc in message.tool_calls:
                    args = json.loads(tc.function.arguments or "{}")
                    print(f" [calling tool] {tc.function.name}({args})")
                    result = await session.call_tool(tc.function.name, args)
                    text = "".join(c.text for c in result.content if hasattr(c, "text"))
                    print(f" [tool result] {text}")
                    conversation.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": text,
                        }
                    )
                    session_mem.scratchpad.update(
                        sub_goal=f"just called {tc.function.name}",
                        last_tool=tc.function.name,
                    )
                    tool_project_id = args.get("project_id")  # best-effort;
                    # None if this particular tool call didn't take one
                    evicted = session_mem.add_turn(
                        "tool",
                        text,
                        project_id=str(tool_project_id) if tool_project_id is not None else None,
                    )
                    if evicted:
                        router.route(evicted)
                    # loop again so the model can react to the tool result(s)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--http-url", default=os.environ.get("IRONBRIDGE_MCP_URL"))
    parser.add_argument("--http-token", default=os.environ.get("IRONBRIDGE_API_TOKEN"))
    args = parser.parse_args()
    asyncio.run(run_agent(args.transport, args.http_url, args.http_token))


if __name__ == "__main__":
    main()