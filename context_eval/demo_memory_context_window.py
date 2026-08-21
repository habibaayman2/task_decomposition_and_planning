import os
import sys
import time
from pathlib import Path
from typing import List, Tuple
from dotenv import load_dotenv
from groq import Groq

# ---------------------------------------------------------------------------
# Path Configuration: Safely resolve paths regardless of execution folder
# ---------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent

if CURRENT_DIR.name == "context_eval":
    REPO_ROOT = CURRENT_DIR.parent
    CONTEXT_EVAL_DIR = CURRENT_DIR
else:
    REPO_ROOT = CURRENT_DIR
    CONTEXT_EVAL_DIR = REPO_ROOT / "context_eval"

MEMORY_DIR = REPO_ROOT / "memory"

for path in (REPO_ROOT, CONTEXT_EVAL_DIR, MEMORY_DIR):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

# 1. Modular Imports via sys.path
# ---------------------------------------------------------------------------
try:
    from transcript import Turn, approx_tokens, transcript_tokens
    from sliding_window import apply as apply_sliding_window
    from observation_masking import apply as apply_observation_masking
    from recursive_summarization import apply as apply_recursive_summarization
    from zone_based_pruning import apply as apply_zone_based_pruning
except ModuleNotFoundError:
    from context_eval.transcript import Turn, approx_tokens, transcript_tokens
    from context_eval.sliding_window import apply as apply_sliding_window
    from context_eval.observation_masking import apply as apply_observation_masking
    from context_eval.recursive_summarization import apply as apply_recursive_summarization
    from context_eval.zone_based_pruning import apply as apply_zone_based_pruning

# Import Memory / Self-RAG verification from memory/
try:
    from self_rag_check import SelfRAGChecker
except ModuleNotFoundError:
    from memory.self_rag_check import SelfRAGChecker

load_dotenv()

GROQ_MODEL = "openai/gpt-oss-120b"
class AgentScratchpad:
    """Distinct working state/scratchpad that survives transcript pruning."""

    def __init__(self) -> None:
        self.current_goal = "Evaluate SupplierID 3 budget compliance and heavy steel lifting safety protocols."
        self.sub_tasks = [
            "Check initial budget cap for SupplierID 3",
            "Verify heavy lifting rules (>50kg)",
            "Determine if additional $10k order exceeds total cap",
        ]
        self.working_state = "Awaiting final decision on $10k order approval."

    def format_scratchpad(self) -> str:
        tasks = "\n".join(f"   - {t}" for t in self.sub_tasks)
        return (
            f"[ACTIVE AGENT SCRATCHPAD - UNTOUCHED BY TRANSCRIPT PRUNING]\n"
            f"Goal: {self.current_goal}\n"
            f"Sub-tasks:\n{tasks}\n"
            f"State: {self.working_state}\n"
            f"--------------------------------------------------\n"
        )


def build_synthetic_turn_transcript() -> List[Turn]:
    """Builds a multi-turn, tool-heavy transcript using the Turn dataclass."""
    raw_data = [
        ("user", "Opening Session: Initial constraint - SupplierID 3 budget cap is set to $50,000 max.", False, True, None),
        ("assistant", "Acknowledged. Budget cap of $50,000 for SupplierID 3 is anchored in session memory.", False, False, None),
        ("assistant", "Checking inventory status...", False, False, "check_inventory"),
        ("tool", '{"status": "success", "data": {"stock": 1400, "warehouse": "Zone B", "units": "tons", "heavy_lifting_threshold_kg": 50}}', True, False, "check_inventory"),
        ("user", "Check steel delivery schedule and supplier limits.", False, False, None),
        ("assistant", "Checking schedule...", False, False, "check_schedule"),
        ("tool", '{"status": "success", "deliveries": [{"id": 101, "date": "2026-08-15", "amount": "$12,000"}, {"id": 102, "date": "2026-08-20", "amount": "$35,000"}]}', True, False, "check_schedule"),
        ("user", "What are the rules for heavy steel lifting above 50kg?", False, False, None),
        ("assistant", "Querying safety policy...", False, False, "query_safety_policy"),
        ("tool", '{"policy_ref": "SAF-09", "rule": "All steel items >50kg require dual-crane mechanical hoist and certified crane operator."}', True, False, "query_safety_policy"),
        ("user", "Can we approve an additional $10,000 order for SupplierID 3?", False, False, None),
        ("assistant", "Checking cumulative spending against the $50,000 initial budget cap...", False, False, None),
        ("user", "Summarize whether the $10k addition exceeds the initial budget constraint.", False, False, None),
    ]

    return [
        Turn(
            role=role,
            content=content,
            is_tool_output=is_tool,
            critical=critical,
            turn_index=idx,
            tool_name=tool,
        )
        for idx, (role, content, is_tool, critical, tool) in enumerate(raw_data)
    ]


def format_turns_to_context(turns: List[Turn], scratchpad: AgentScratchpad) -> str:
    """Combines the un-pruned scratchpad with pruned Turn objects into LLM context."""
    blocks = [scratchpad.format_scratchpad(), "[PRUNED TRANSCRIPT HISTORY]"]
    for turn in turns:
        prefix = f"TOOL ({turn.tool_name})" if turn.tool_name else turn.role.upper()
        blocks.append(f"{prefix}: {turn.content}")
    return "\n".join(blocks)


def run_llm_generation(query: str, context: str) -> Tuple[str, int, int]:
    """Generates an answer using Groq and captures prompt & completion token counts."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return "Simulated answer: Budget cap is $50,000.", approx_tokens(context), 20

    client = Groq(api_key=api_key)
    prompt = f"Context:\n{context}\n\nQuestion: {query}"

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.0,
        messages=[
            {
                "role": "system",
                "content": "You are the IronBridge procurement assistant. Answer strictly using the context provided.",
            },
            {"role": "user", "content": prompt},
        ],
    )

    answer = response.choices[0].message.content or ""
    usage = response.usage
    input_tokens = usage.prompt_tokens if usage else approx_tokens(context)
    output_tokens = usage.completion_tokens if usage else approx_tokens(answer)

    return answer, input_tokens, output_tokens


def run_integration_demo():
    print("=" * 88)
    print("   IRONBRIDGE MEMORY & CONTEXT EVALUATION INTEGRATION DEMO")
    print("=" * 88)

    scratchpad = AgentScratchpad()
    raw_turns = build_synthetic_turn_transcript()
    target_query = "What was the initial budget cap set for SupplierID 3?"

    raw_token_count = transcript_tokens(raw_turns)

    print(f"\n[Scratchpad Initialized] Active Goal: '{scratchpad.current_goal}'")
    print(f"[Transcript Loaded] {len(raw_turns)} Turns | {raw_token_count} Transcript Tokens")
    print(f"[Target Question] '{target_query}'\n")

    strategies = {
        "sliding_window": lambda t: apply_sliding_window(t, window_turns=4),
        "observation_masking": lambda t: apply_observation_masking(t, keep_last_n_tool_outputs=1),
        "recursive_summarization": lambda t: apply_recursive_summarization(t, summarize_every=5, keep_recent=3),
        "zone_based_pruning": lambda t: apply_zone_based_pruning(t, anchor_turns=2, recent_turns=3),
    }

    checker = SelfRAGChecker()
    results_table = []

    for name, strategy_fn in strategies.items():
        start_time = time.perf_counter()

        # 1. Apply Context Strategy on List[Turn]
        pruned_turns = strategy_fn(raw_turns)

        # 2. Format Context with Untouched Scratchpad
        full_context = format_turns_to_context(pruned_turns, scratchpad)

        # 3. Generate Answer & Capture Token Metrics
        answer, in_tok, out_tok = run_llm_generation(target_query, full_context)
        latency = time.perf_counter() - start_time

        # 4. Self-RAG Memory Checks (Relevance + Support)
        rel_check = checker.relevance_check(target_query, full_context)
        sup_check = checker.support_check(answer, full_context) if rel_check.passed else None

        passed_all = rel_check.passed and (sup_check.passed if sup_check else False)

        results_table.append({
            "Strategy": name,
            "Input Tokens": in_tok,
            "Output Tokens": out_tok,
            "Latency": f"{latency:.3f}s",
            "Self-RAG Rel": "PASS" if rel_check.passed else "FAIL",
            "Self-RAG Sup": "PASS" if sup_check and sup_check.passed else ("FAIL" if rel_check.passed else "N/A"),
            "Accuracy": "100%" if passed_all else "0%",
            "Reason": rel_check.reason if not rel_check.passed else (sup_check.reason if sup_check else ""),
        })

    # Print Project Benchmark Table
    print("-" * 88)
    print(f"{'Strategy':<24} | {'In Tokens':<9} | {'Out Tokens':<10} | {'Latency':<8} | {'Self-RAG (Rel/Sup)':<18} | {'Accuracy':<8}")
    print("-" * 88)
    for r in results_table:
        self_rag_str = f"{r['Self-RAG Rel']} / {r['Self-RAG Sup']}"
        print(f"{r['Strategy']:<24} | {r['Input Tokens']:<9} | {r['Output Tokens']:<10} | {r['Latency']:<8} | {self_rag_str:<18} | {r['Accuracy']:<8}")
    print("-" * 88)

    print("\n[Self-RAG Verification Feedback]:")
    for r in results_table:
        note = r['Reason'][:70] + "..." if len(r['Reason']) > 70 else r['Reason']
        print(f" • [{r['Strategy']}]: {note}")


if __name__ == "__main__":
    run_integration_demo()