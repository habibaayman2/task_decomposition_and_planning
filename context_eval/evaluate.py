import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# ---------------------------------------------------------------------------
# Dynamic Path Resolution Fix: Adds project root to sys.path so imports work
# regardless of how or where the script is executed from the terminal.
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from context_eval import (
    observation_masking,
    recursive_summarization,
    sliding_window,
    zone_based_pruning,
)
from context_eval.test_cases.generator import (
    EXPECTED_ANSWER_KEYWORDS,
    generate_test_suite,
)
from context_eval.transcript import Turn, transcript_tokens

# Pre-lowercase expected keywords once to eliminate per-evaluation string allocations
_LOWERED_KEYWORDS = [kw.lower() for kw in EXPECTED_ANSWER_KEYWORDS]


def _heuristic_summarizer(turns: list[Turn]) -> str:
    """Fast, deterministic local summarizer callable used during benchmarking."""
    user_snippets = [
        t.content[:50].replace("\n", " ") for t in turns if t.role == "user"
    ]
    tool_count = sum(1 for t in turns if t.is_tool_output)
    return (
        f"Compressed {len(turns)} turns ({tool_count} tool checks). "
        f"Topics discussed: {'; '.join(user_snippets[:2])}"
    )


def _render_for_answer(turns: list[Turn]) -> str:
    """Efficiently renders transcript turns into context text."""
    return "\n".join(
        f"[tool result]: {t.content}" if t.is_tool_output else f"{t.role}: {t.content}"
        for t in turns
    )


def _recalled_correctly(context_text: str) -> bool:
    """
    Checks if any ground-truth keyword survives in the pruned context string.
    Works deterministically without needing an external LLM call.
    """
    lowered_context = context_text.lower()
    return any(kw in lowered_context for kw in _LOWERED_KEYWORDS)


# Map of strategy names to execution lambdas using your callable implementations
STRATEGIES: Dict[str, Callable[[list[Turn]], list[Turn]]] = {
    "sliding_window": lambda turns: sliding_window.apply(turns, window_turns=10),
    "observation_masking": lambda turns: observation_masking.apply(
        turns, keep_last_n_tool_outputs=3
    ),
    "recursive_summarization": lambda turns: recursive_summarization.apply(
        turns,
        summarize_every=15,
        keep_recent=8,
        summarizer=_heuristic_summarizer,  # Passes local callable
    ),
    "zone_based_pruning": lambda turns: zone_based_pruning.apply(
        turns, anchor_turns=4, recent_turns=8
    ),
}


def run_eval(
    n_variations: int = 10,
    answer_generator: Optional[Callable[[str, str], str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Runs all 4 context strategies against the generated test suite.
    
    Args:
        n_variations: Number of transcript variations to generate.
        answer_generator: Optional LLM callable (system_prompt, context) -> answer.
                          If None, evaluates ground-truth keyword survival directly.
    """
    suite = generate_test_suite(n_variations=n_variations)
    results = {
        name: {
            "correct": 0,
            "total": 0,
            "input_tokens": [],
            "output_tokens": [],
            "latencies": [],
        }
        for name in STRATEGIES
    }

    for transcript in suite:
        history, question_turn = transcript[:-1], transcript[-1]

        for name, strategy_fn in STRATEGIES.items():
            start_time = time.perf_counter()

            # 1. Apply pruning strategy & append final question
            pruned_history = strategy_fn(history)
            pruned_transcript = pruned_history + [question_turn]

            # 2. Render pruned transcript into text context
            context = _render_for_answer(pruned_transcript)

            # 3. Evaluate recall & track metrics
            if answer_generator is not None:
                system_prompt = (
                    "You are the IronBridge procurement assistant. Answer the question "
                    "using only the context provided."
                )
                answer = answer_generator(system_prompt, context)
                is_correct = _recalled_correctly(answer)
                output_tok = max(len(answer) // 4, 1)
            else:
                # Direct ground-truth recall check against pruned context
                is_correct = _recalled_correctly(context)
                output_tok = 0  # Pure context evaluation mode

            latency = time.perf_counter() - start_time

            # 4. Record metrics
            r = results[name]
            r["correct"] += int(is_correct)
            r["total"] += 1
            r["input_tokens"].append(transcript_tokens(pruned_transcript))
            r["output_tokens"].append(output_tok)
            r["latencies"].append(latency)

    return results


def print_results(results: Dict[str, Dict[str, Any]], markdown_format: bool = True):
    """Prints comparative evaluation table formatted for terminal or Markdown README."""
    if markdown_format:
        print("\n### Context Management Strategy Comparison\n")
        print(
            "| Strategy | Recall Accuracy | Avg Input Tokens | Avg Output Tokens | Avg Latency (s) |"
        )
        print("| :--- | :---: | :---: | :---: | :---: |")
        for name, r in results.items():
            count = len(r["input_tokens"]) or 1
            acc_pct = (r["correct"] / r["total"]) * 100 if r["total"] else 0
            acc_str = f"{r['correct']}/{r['total']} ({acc_pct:.0f}%)"
            avg_in = sum(r["input_tokens"]) / count
            avg_out = sum(r["output_tokens"]) / count
            avg_lat = sum(r["latencies"]) / count
            print(
                f"| `{name}` | {acc_str} | {avg_in:.0f} | {avg_out:.0f} | {avg_lat:.5f}s |"
            )
    else:
        print(
            f"\n{'Strategy':<26} {'Recall Accuracy':<18} {'Avg Input Tok':<16} {'Avg Output Tok':<16} {'Avg Latency':<12}"
        )
        print("-" * 92)
        for name, r in results.items():
            count = len(r["input_tokens"]) or 1
            acc = f"{r['correct']}/{r['total']}"
            avg_in = sum(r["input_tokens"]) / count
            avg_out = sum(r["output_tokens"]) / count
            avg_lat = sum(r["latencies"]) / count
            print(
                f"{name:<26} {acc:<18} {avg_in:<16.0f} {avg_out:<16.0f} {avg_lat:<12.5f}s"
            )


if __name__ == "__main__":
    eval_results = run_eval(n_variations=10)
    print_results(eval_results, markdown_format=True)
