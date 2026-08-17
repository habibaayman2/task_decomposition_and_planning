"""
agent/agent.py

Interactive IronBridge procurement agent with Week 4 Planning Agent integration.

The planning agent sits alongside (not replacing) the memory/RAG agent:
  - Policy questions         -> RAG retrieval
  - Procurement lookups      -> MCP tools
  - Delay/risk/mitigation    -> Planning Agent (decomposition -> planning -> self-correction)

Usage:
  export GROQ_API_KEY=...
  python3 agent/agent.py

Try:
  "What steel do we have in stock?"              -> MCP tools
  "What does Policy #2 say about fire lanes?"    -> RAG retrieval
  "Project 1 delay risk"                         -> Planning Agent (Week 4)
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

for path in (REPO_ROOT, CONTEXT_EVAL_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

load_dotenv(os.path.join(REPO_ROOT, ".env"))

import uuid
from memory.short_term import SessionMemory
from memory.router import PromoteOrDropRouter
from memory.stores import EpisodicStore, SemanticStore
from memory.consolidation import ConsolidationJob
from memory.self_rag_check import SelfRAGChecker

from rag.hybrid_search import hybrid_rag_answer
from rag.agentic_rag import agentic_rag_answer

# ---------------------------------------------------------------------------
# Planning Agent Integration (Week 4 — Delay-Response Planning)
# ---------------------------------------------------------------------------
from planning.algorithms.dynamic_decomposition import dynamic_decomposition
from planning.algorithms.decomposition import decompose_goal, execute_plan, final_output
from planning.algorithms.environment import IronBridgeEnvironment
from planning.model_provider import get_planning_llm as get_planning_llm

PLANNING_KEYWORDS = [
    "delay", "risk", "mitigate", "recovery plan", "resequence",
    "rush order", "supplier switch", "behind schedule", "at risk",
    "material shortage", "critical path", "expedite", "propose a plan"
]


def _is_planning_request(text: str) -> bool:
    """Detect if a user query is a planning problem requiring decomposition."""
    lowered = text.lower()
    return any(kw in lowered for kw in PLANNING_KEYWORDS)


# ---------------------------------------------------------------------------
# Context Management (Observation Masking)
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
    "call tools that are currently offered to you."
)

# === RAG routing ===
POLICY_KEYWORDS = [
    "policy", "safety", "handling", "ppe", "warehouse",
    "fire lane", "lifting", "crane", "minimum stock",
    "approval workflow", "clearance", "regulation", "procedure",
    "guideline", "rule", "compliance", "protocol", "requirement", "standard"
]

INVENTORY_KEYWORDS = [
    "do we have", "how many", "how much", "available",
    "in stock", "quantity", "price", "cost", "budget",
    "remaining", "left", "order", "purchase"
]


def _is_policy_question(text: str) -> bool:
    lowered = text.lower()
    if any(kw in lowered for kw in INVENTORY_KEYWORDS):
        return False
    return any(kw in lowered for kw in POLICY_KEYWORDS)


def _is_multi_part_question(text: str) -> bool:
    lowered = text.lower()
    question_starts = sum(1 for w in ["what ", "how ", "why ", "when ", "where "] if w in lowered)
    return question_starts >= 2 or "both" in lowered or "additionally" in lowered


def mcp_tool_to_groq(tool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


def prepare_pruned_messages(messages: list[dict], keep_last_n_tool_outputs: int = 3) -> list[dict]:
    turns = []
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content") or ""
        is_tool = (role == "tool")
        is_critical = any(kw in content.lower() for kw in ["budget", "limit", "cap", "rule"])
        turns.append(Turn(role=role, content=content, is_tool_output=is_tool, critical=is_critical, turn_index=idx))
    pruned_turns = apply_observation_masking(turns, keep_last_n_tool_outputs=keep_last_n_tool_outputs)
    pruned_messages = []
    for orig_msg, pruned_turn in zip(messages, pruned_turns):
        msg_copy = dict(orig_msg)
        if orig_msg.get("role") == "tool":
            msg_copy["content"] = pruned_turn.content
        pruned_messages.append(msg_copy)
    return pruned_messages


# ---------------------------------------------------------------------------
# Planning Agent Runner
# ---------------------------------------------------------------------------
async def run_planning_agent(user_text: str) -> str:
    """Week 4: Run the delay-response planning agent.

    Routes vague requests through dynamic decomposition (reacts to real DB data)
    and clear requests through decomposition-first (static plan is sufficient).
    Final output is checked against IronBridgeEnvironment.
    """
    planning_llm = get_planning_llm()
    env = IronBridgeEnvironment(success_threshold=0.6)

    # Heuristic: if the request is vague (no specific cause mentioned), use dynamic
    vague = not any(word in user_text.lower() for word in
                    ["rebar", "concrete", "steel", "equipment", "budget", "supplier"])

    if vague:
        print(" [PLANNING] Request is vague — using dynamic decomposition...")
        history = dynamic_decomposition(user_text, llm=planning_llm)
        output_lines = [f"Step {i+1}. [{instr}]\n{out}" for i, (instr, out) in enumerate(history)]
        output = "\n\n".join(output_lines)
    else:
        print(" [PLANNING] Request has clear shape — using decomposition-first...")
        plan = decompose_goal(user_text, llm=planning_llm)
        outputs = execute_plan(plan, llm=planning_llm)
        output = final_output(plan, outputs)

    # Grounded final check
    fb = env.evaluate(output)
    output += f"\n\n[Grounded check] Score: {fb.score} | Success: {fb.success}"
    if not fb.success:
        output += f"\nIssues: {fb.details}"
    return output


# ---------------------------------------------------------------------------
# Main Agent Loop
# ---------------------------------------------------------------------------
async def run_agent(transport: str, http_url: str | None, http_token: str | None):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("GROQ_API_KEY is not set — get a free key at https://console.groq.com")
        return

    from groq import Groq
    groq_client = Groq(api_key=api_key)

    # Memory system
    session_id = str(uuid.uuid4())
    episodic_store = EpisodicStore()
    semantic_store = SemanticStore()
    router = PromoteOrDropRouter(episodic_store)
    checker = SelfRAGChecker()
    session_mem = SessionMemory(session_id, max_turns=20)
    ConsolidationJob(episodic_store, semantic_store).run()

    state = {"groq_tools": []}

    async def refresh_tools_and_announce():
        result = await SESSION.list_tools()
        state["groq_tools"] = [mcp_tool_to_groq(t) for t in result.tools]
        names = [t.name for t in result.tools]
        print(f"\n[tool set updated] now available: {names}\n")

    connect_kwargs = dict(transport=transport, auto_elicit_answers=None, on_tools_changed=refresh_tools_and_announce)
    if transport == "stdio":
        connect_kwargs["server_command"] = [sys.executable, "mcp_server/server.py"]
        connect_kwargs["server_cwd"] = REPO_ROOT
    else:
        connect_kwargs["http_url"] = http_url
        connect_kwargs["http_token"] = http_token

    async with mcp_client.connect(**connect_kwargs) as (session, init_result):
        global SESSION
        SESSION = session

        print(f"Connected to {init_result.serverInfo.name} v{init_result.serverInfo.version}")
        await refresh_tools_and_announce()

        print("\nIronBridge Procurement Assistant — type a request, or 'quit' to exit.")
        print("Planning problems (delay, risk, mitigation) are routed automatically.\n")

        conversation: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

        while True:
            user_text = await asyncio.to_thread(input, "you> ")
            if user_text.strip().lower() in ("quit", "exit"):
                break

            conversation.append({"role": "user", "content": user_text})
            session_mem.scratchpad.update(plan=f"respond to: {user_text[:80]}", sub_goal="awaiting model response")
            evicted = session_mem.add_turn("user", user_text)
            if evicted:
                router.route(evicted)

            # === PLANNING AGENT ROUTING (Week 4) ===
            if _is_planning_request(user_text):
                print(" [PLANNING] Routing to Delay-Response Planning Agent...")
                try:
                    planning_output = await run_planning_agent(user_text)
                    print(f"assistant> {planning_output}")
                    conversation.append({"role": "assistant", "content": planning_output})
                    evicted = session_mem.add_turn("assistant", planning_output)
                    if evicted:
                        router.route(evicted)
                    continue
                except Exception as e:
                    print(f" [PLANNING ERROR] {type(e).__name__}: {e}")
                    print(" [PLANNING] Falling back to standard tool-based agent...")

            # === RAG ROUTING ===
            rag_fired_this_turn = False
            if _is_policy_question(user_text):
                is_multi = _is_multi_part_question(user_text)
                if is_multi:
                    print(" [RAG] Routing to Agentic RAG...")
                    rag_result = agentic_rag_answer(user_text, top_k_per_hop=4)
                else:
                    print(" [RAG] Routing to Hybrid Search...")
                    rag_result = hybrid_rag_answer(user_text, top_k=5)
                rag_answer = rag_result["answer"]
                rag_injection = (
                    f"[RETRIEVED POLICY CONTEXT — ANSWER DIRECTLY FROM THIS, "
                    f"DO NOT CALL ANY TOOLS]\n{rag_answer}\n[END CONTEXT]"
                )
                conversation.append({"role": "system", "content": rag_injection})
                rag_fired_this_turn = True
                hops = rag_result.get("hops_used", 1)
                chunks = len(rag_result.get("retrieved_chunks", []))
                print(f" [RAG] Retrieved {chunks} chunk(s) across {hops} hop(s).")

            # Memory injection
            recalled = [e.content for e in episodic_store.recall(session_id=session_id)]
            recalled = checker.filter_relevant(user_text, recalled)
            memory_msg = {"role": "system", "content": session_mem.scratchpad.as_context_block()}
            if recalled:
                memory_msg["content"] += "\n[RELEVANT MEMORY]\n" + "\n".join(f"- {r}" for r in recalled)

            # Tool-call loop
            while True:
                pruned_conversation = prepare_pruned_messages(conversation)
                api_kwargs = {"model": MODEL, "max_tokens": 1024, "messages": pruned_conversation + [memory_msg]}
                if not rag_fired_this_turn:
                    api_kwargs["tools"] = state["groq_tools"]

                response = groq_client.chat.completions.create(**api_kwargs)
                message = response.choices[0].message
                assistant_msg = {"role": "assistant", "content": message.content}
                if message.tool_calls:
                    assistant_msg["tool_calls"] = [
                        {"id": tc.id, "type": tc.type, "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
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

                if rag_fired_this_turn:
                    print("[WARN] Model requested tools despite RAG context — forcing text answer.")
                    continue

                for tc in message.tool_calls:
                    args = json.loads(tc.function.arguments or "{}")
                    print(f" [calling tool] {tc.function.name}({args})")
                    result = await session.call_tool(tc.function.name, args)
                    text = "".join(c.text for c in result.content if hasattr(c, "text"))
                    print(f" [tool result] {text}")
                    conversation.append({"role": "tool", "tool_call_id": tc.id, "content": text})
                    session_mem.scratchpad.update(sub_goal=f"just called {tc.function.name}", last_tool=tc.function.name)
                    tool_project_id = args.get("project_id")
                    evicted = session_mem.add_turn("tool", text, project_id=str(tool_project_id) if tool_project_id is not None else None)
                    if evicted:
                        router.route(evicted)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--http-url", default=os.environ.get("IRONBRIDGE_MCP_URL"))
    parser.add_argument("--http-token", default=os.environ.get("IRONBRIDGE_API_TOKEN"))
    args = parser.parse_args()
    asyncio.run(run_agent(args.transport, args.http_url, args.http_token))


if __name__ == "__main__":
    main()