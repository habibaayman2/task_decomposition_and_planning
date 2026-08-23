"""
agent/planning_agent.py

Interactive IronBridge procurement agent with Week 4 Planning Agent integration.

The planning agent sits alongside (not replacing) the memory/RAG agent:
  - Policy questions         -> RAG retrieval
  - Procurement lookups      -> MCP tools
  - Delay/risk/mitigation    -> Planning Agent (decomposition -> routed planning -> self-correction)

Usage:
  export GROQ_API_KEY=...
  # Run from the repo root as a module (adds repo root to sys.path via -m):
  python -m agent.planning_agent

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

# ---------------------------------------------------------------------------
# Path setup MUST happen before any repo-local absolute imports
# (planning.*, memory.*, rag.*, mcp_client). Not strictly required when
# running via `python -m agent.planning_agent` from the repo root (which
# already puts the repo root on sys.path), but doing this setup after those
# imports would break a direct `python3 agent/planning_agent.py` invocation,
# since Python only puts the *script's own* directory on sys.path
# automatically in that case, not the repo root. Kept first defensively so
# both invocation styles work.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTEXT_EVAL_DIR = os.path.join(REPO_ROOT, "context_eval")
for _path in (REPO_ROOT, CONTEXT_EVAL_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from dotenv import load_dotenv

load_dotenv(os.path.join(REPO_ROOT, ".env"))

import mcp_client

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
from planning.algorithms.self_refine import self_refine
from planning.algorithms.dynamic_decomposition import dynamic_decomposition
from planning.algorithms.decomposition import decompose_goal, final_output
from planning.algorithms.environment import IronBridgeEnvironment
from planning.model_provider import get_planning_llm
from planning.router import execute_routed_plan  # <-- routes rank_options -> ToT,
                                                   #     propose_plan -> LATS, instead
                                                   #     of the plain-LLM DEFAULT_EXECUTORS

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

MODEL = "openai/gpt-oss-20b"

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
async def run_planning_agent(user_text: str, episodic_memory: list[str] = None) -> str:
    """
    Week 4 Integration:
    1. Decompose (decomposition-first / dynamic, chosen by request shape)
    2. Route + execute each sub-task through the algorithm that fits it
       (diagnose -> direct, rank_options -> Tree-of-Thoughts,
        propose_plan -> LATS, notify -> Plan-and-Solve) via planning.router
    3. Evaluate the combined result (grounded, real DB check)
    4. Self-Refine if the grounded check is weak
    """
    planning_llm = get_planning_llm()
    env = IronBridgeEnvironment(success_threshold=0.6)

    memory_context = "\n".join(episodic_memory) if episodic_memory else "No prior trial memory."

    is_vague = not any(
        word in user_text.lower()
        for word in ["rebar", "concrete", "steel", "excavator", "budget", "supplier"]
    )

    if is_vague:
        print(" [PLANNING] Vague request detected — using Dynamic Decomposition...")
        history = dynamic_decomposition(f"{user_text}\nContext: {memory_context}", llm=planning_llm)
        raw_output = "\n\n".join([f"Task: {instr}\nResult: {out}" for instr, out in history])
    else:
        print(" [PLANNING] Clear shape detected — using Decomposition-first (Static DAG)...")
        plan = decompose_goal(user_text, llm=planning_llm)

        # Route each sub-task to the algorithm that actually fits its shape
        # (this is what exercises Tree-of-Thoughts on rank_options and LATS
        # on propose_plan — execute_plan()'s DEFAULT_EXECUTORS alone would
        # only ever make plain LLM calls).
        results = execute_routed_plan(plan, planning_llm)
        for task_id, result in results.items():
            print(f" [PLANNING] {task_id} -> {result['method']} "
                  f"(success={result['success']}, score={result['score']:.2f})")
        raw_output = final_output(plan, {tid: r["output"] for tid, r in results.items()})

    print(" [PLANNING] Running Grounded Evaluation against IronBridge DB...")
    feedback = env.evaluate(raw_output)

    if not feedback.success or feedback.score < 0.5:
        print(f" [PLANNING] Grounded check failed (Score: {feedback.score}). Triggering Self-Refine...")
        refinement_result = self_refine(goal=user_text, llm=planning_llm, environment=env)
        final_result = refinement_result.revised
        final_fb = env.evaluate(final_result)
    else:
        print(f" [PLANNING] Grounded check passed (Score: {feedback.score}).")
        final_result = raw_output
        final_fb = feedback

    response_header = "### IronBridge Planning Agent Response\n"
    grounding_footer = (
        f"\n\n---\n**Grounded Safety Check:**\n"
        f"- Success: {'✅' if final_fb.success else '❌'}\n"
        f"- Confidence Score: {final_fb.score:.2f}\n"
        f"- DB Observations: {', '.join(final_fb.details) if final_fb.details else 'All checks passed.'}"
    )

    return response_header + final_result + grounding_footer


# ---------------------------------------------------------------------------
# Main Agent Loop
# ---------------------------------------------------------------------------
async def run_agent(transport: str, http_url: str | None, http_token: str | None):
    global SESSION
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
                    recalled_memories = [e.content for e in episodic_store.recall(session_id=session_id)]
                    planning_output = await run_planning_agent(user_text, recalled_memories)

                    print(f"assistant> {planning_output}")
                    conversation.append({"role": "assistant", "content": planning_output})
                    session_mem.add_turn("assistant", planning_output)
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
