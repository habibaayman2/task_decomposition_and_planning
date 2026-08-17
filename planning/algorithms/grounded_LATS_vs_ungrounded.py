"""
planning_eval/lats_grounded_vs_ungrounded.py

Dedicated evaluation harness comparing LATS with grounded environment
(IronBridgeEnvironment, real DB checks) vs. LATS with ungrounded environment
(toolkit's randomized default).

This produces:
  1. artifacts/lats_grounded_vs_ungrounded.json — per-case traces
  2. Console report with the side-by-side comparison table
  3. A concrete demo showing exactly which failure the grounded version catches

Run: python -m planning_eval.lats_grounded_vs_ungrounded
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any
import threading

THIS_DIR = Path(__file__).resolve().parent
ROOT_DIR = THIS_DIR.parent
for _p in (THIS_DIR, ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from planning.algorithms.lats import lats, LATSResult
from planning.algorithms.environment import IronBridgeEnvironment, Environment
from planning.model_provider import get_planning_llm

_LLM_GLOBAL_LOCK = threading.Lock()

# Token/cost estimation
INPUT_COST_PER_1K = 0.00027
OUTPUT_COST_PER_1K = 0.00027
AVG_OUTPUT_TOKENS_PER_CALL = 400


@dataclass
class CallCounter:
    llm: Any
    count: int = field(default=0, init=False)
    input_tokens: int = field(default=0, init=False)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def invoke(self, *args, **kwargs):
        with _LLM_GLOBAL_LOCK:
            with self._lock:
                self.count += 1
                prompt = args[0] if args else kwargs.get("input", "")
                self.input_tokens += len(str(prompt)) // 4
                return self.llm.invoke(*args, **kwargs)

    def with_structured_output(self, *args, **kwargs):
        inner = self.llm.with_structured_output(*args, **kwargs)
        return _StructuredOutputWrapper(inner, self)


class _StructuredOutputWrapper:
    def __init__(self, inner: Any, counter: CallCounter):
        self._inner = inner
        self._counter = counter

    def invoke(self, *args, **kwargs):
        with _LLM_GLOBAL_LOCK:
            with self._counter._lock:
                self._counter.count += 1
                prompt = args[0] if args else kwargs.get("input", "")
                self._counter.input_tokens += len(str(prompt)) // 4
                return self._inner.invoke(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _est_cost(calls: int, input_tokens: int) -> float:
    output_tokens = calls * AVG_OUTPUT_TOKENS_PER_CALL
    return (input_tokens / 1000 * INPUT_COST_PER_1K) + (output_tokens / 1000 * OUTPUT_COST_PER_1K)


# ---------------------------------------------------------------------------
# Fixed test suite designed to stress-test grounded vs. ungrounded LATS
# ---------------------------------------------------------------------------
TEST_CASES = [
    {
        "id": "LATS_OVER_BUDGET",
        "prompt": (
            "ProjectID 1 is at risk because rebar delivery is 9 days late. "
            "Propose a mitigation. Consider rush-ordering steel from Steel Yard "
            "but do not exceed the remaining budget."
        ),
        "expected_fail_reason": "rush cost exceeds RemainingBudget",
        "why_grounded_wins": (
            "The naive LLM often proposes a rush order at $45,000+ without "
            "checking db.get_project(1).RemainingBudget ($42,000). Grounded "
            "LATS evaluates every branch against the real budget and prunes "
            "the over-budget branch. Ungrounded LATS randomly scores branches "
            "and may keep the bad one."
        ),
    },
    {
        "id": "LATS_INACTIVE_SUPPLIER",
        "prompt": (
            "ProjectID 2 faces equipment maintenance delays. Recommend sourcing "
            "a replacement excavator from Cement Co to stay on schedule."
        ),
        "expected_fail_reason": "ContractStatus != Active",
        "why_grounded_wins": (
            "Cement Co may have ContractStatus='Expired' in the real DB. "
            "Grounded LATS checks Suppliers table and rejects the branch. "
            "Ungrounded LATS uses random.betavariate and accepts it ~70% of the time."
        ),
    },
    {
        "id": "LATS_LOW_STOCK_RESEQUENCE",
        "prompt": (
            "ProjectID 3 is behind schedule. Propose resequencing the work "
            "to avoid the delay without ordering new materials."
        ),
        "expected_fail_reason": "resequences without addressing low stock",
        "why_grounded_wins": (
            "If the material log shows items below MinimumStockLevel, resequencing "
            "alone is insufficient. Grounded env checks db.find_materials() and "
            "scores down. Ungrounded env randomly approves."
        ),
    },
    {
        "id": "LATS_VALID_PLAN",
        "prompt": (
            "ProjectID 1: Resequence unaffected trades ahead of the blocked item "
            "so the crew stays productive while the blocking issue is resolved."
        ),
        "expected_fail_reason": None,
        "why_grounded_wins": (
            "Both should pass, but grounded gives a higher-confidence score because "
            "it verifies no materials are below minimum stock before approving."
        ),
    },
    {
        "id": "LATS_BUDGET_CONSTRAINED",
        "prompt": (
            "ProjectID 2: Equipment maintenance is pushing the schedule back. "
            "Propose a fix but do NOT recommend anything that would exceed the remaining budget."
        ),
        "expected_fail_reason": "proposed cost exceeds remaining budget",
        "why_grounded_wins": (
            "The request explicitly forbids exceeding budget. Grounded LATS enforces "
            "this via db.get_project().RemainingBudget. Ungrounded LATS has no access "
            "to real budget data and may propose an over-budget fix."
        ),
    },
]


def run_lats_case(case: dict, llm, grounded: bool = True, iterations: int = 2, n_actions: int = 2) -> dict:
    """Run LATS on a single case with either grounded or ungrounded environment."""
    if grounded:
        env = IronBridgeEnvironment(success_threshold=0.6)
        label = "LATS (grounded)"
    else:
        env = Environment(success_threshold=0.6)
        label = "LATS (ungrounded)"

    counter = CallCounter(llm)
    t0 = time.time()

    try:
        result: LATSResult = lats(
            task=case["prompt"],
            llm=counter,
            environment=env,
            iterations=iterations,
            n_actions=n_actions,
        )
        output = getattr(result, "output", str(result))
        fb = env.evaluate(output)
        latency = round(time.time() - t0, 3)
        total_tokens = counter.input_tokens + (counter.count * AVG_OUTPUT_TOKENS_PER_CALL)
        cost = _est_cost(counter.count, counter.input_tokens)

        return {
            "method": label,
            "grounded": grounded,
            "case_id": case["id"],
            "success": getattr(fb, "success", False),
            "score": round(getattr(fb, "score", 0.0), 4),
            "calls": counter.count,
            "input_tokens": counter.input_tokens,
            "total_tokens": total_tokens,
            "latency_sec": latency,
            "est_cost_usd": round(cost, 4),
            "output_preview": output[:300] + "..." if len(output) > 300 else output,
            "env_details": getattr(fb, "details", []),
            "lats_iterations": getattr(result, "iterations", 0),
            "lats_best_score": getattr(result, "best_score", 0.0),
            "error": None,
        }
    except Exception as e:
        latency = round(time.time() - t0, 3)
        total_tokens = counter.input_tokens + (counter.count * AVG_OUTPUT_TOKENS_PER_CALL)
        cost = _est_cost(counter.count, counter.input_tokens)
        return {
            "method": label,
            "grounded": grounded,
            "case_id": case["id"],
            "success": False,
            "score": 0.0,
            "calls": counter.count,
            "input_tokens": counter.input_tokens,
            "total_tokens": total_tokens,
            "latency_sec": latency,
            "est_cost_usd": round(cost, 4),
            "output_preview": "",
            "env_details": [f"ERROR: {type(e).__name__}: {e}"],
            "lats_iterations": 0,
            "lats_best_score": 0.0,
            "error": f"{type(e).__name__}: {e}",
        }


def print_side_by_side(case: dict, grounded_result: dict, ungrounded_result: dict) -> None:
    """Print a detailed side-by-side comparison for a single case."""
    print("\n" + "=" * 95)
    print(f"CASE: {case['id']}")
    print(f"Prompt: {case['prompt'][:100]}...")
    print(f"Expected fail reason: {case['expected_fail_reason'] or 'None (should pass)'}")
    print("=" * 95)

    print("\n--- LATS (GROUNDED) ---")
    print(f"Success: {grounded_result['success']} | Score: {grounded_result['score']}")
    print(f"Calls: {grounded_result['calls']} | Tokens: {grounded_result['total_tokens']} | Latency: {grounded_result['latency_sec']}s | Cost: ${grounded_result['est_cost_usd']}")
    print(f"Env details: {grounded_result['env_details']}")
    print(f"Output preview: {grounded_result['output_preview'][:150]}...")

    print("\n--- LATS (UNGROUNDED) ---")
    print(f"Success: {ungrounded_result['success']} | Score: {ungrounded_result['score']}")
    print(f"Calls: {ungrounded_result['calls']} | Tokens: {ungrounded_result['total_tokens']} | Latency: {ungrounded_result['latency_sec']}s | Cost: ${ungrounded_result['est_cost_usd']}")
    print(f"Env details: {ungrounded_result['env_details']}")
    print(f"Output preview: {ungrounded_result['output_preview'][:150]}...")

    # Diff analysis
    print("\n--- DIFF (what grounded caught that ungrounded missed) ---")
    if grounded_result["success"] and not ungrounded_result["success"]:
        print("✓ Grounded PASSED where ungrounded FAILED (grounded found a valid plan)")
    elif not grounded_result["success"] and ungrounded_result["success"]:
        print("✗ Grounded FAILED where ungrounded PASSED (ungrounded falsely approved)")
    elif grounded_result["success"] and ungrounded_result["success"]:
        g_score = grounded_result["score"]
        u_score = ungrounded_result["score"]
        if g_score > u_score:
            print(f"✓ Both passed, but grounded scored higher ({g_score} vs {u_score}) — more confident validation")
        else:
            print(f"~ Both passed; ungrounded scored higher by chance ({u_score} vs {g_score})")
    else:
        print("~ Both failed (different reasons)")
        g_issues = set(str(d).lower() for d in grounded_result["env_details"])
        u_issues = set(str(d).lower() for d in ungrounded_result["env_details"])
        only_in_grounded = g_issues - u_issues
        only_in_ungrounded = u_issues - g_issues
        if only_in_grounded:
            print("  Issues only in grounded:")
            for issue in only_in_grounded:
                print(f"    • {issue}")
        if only_in_ungrounded:
            print("  Issues only in ungrounded:")
            for issue in only_in_ungrounded:
                print(f"    • {issue}")


def main() -> None:
    llm = get_planning_llm()
    artifacts_dir = ROOT_DIR / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    all_results = []

    print("\n" + "#" * 95)
    print("# LATS GROUNDED vs. UNGROUNDED — SIDE-BY-SIDE COMPARISON")
    print("#" * 95)

    for case in TEST_CASES:
        print(f"\nRunning case: {case['id']}...")

        print("  -> LATS (grounded)...")
        grounded = run_lats_case(case, llm, grounded=True, iterations=2, n_actions=2)
        all_results.append(grounded)

        print("  -> LATS (ungrounded)...")
        ungrounded = run_lats_case(case, llm, grounded=False, iterations=2, n_actions=2)
        all_results.append(ungrounded)

        print_side_by_side(case, grounded, ungrounded)

    # Build aggregate comparison table
    table = []
    for grounded in [True, False]:
        label = "LATS (grounded)" if grounded else "LATS (ungrounded)"
        runs = [r for r in all_results if r["grounded"] == grounded]
        successes = sum(1 for r in runs if r["success"])
        total = len(runs)
        avg_calls = round(sum(r["calls"] for r in runs) / total, 2) if total else 0
        avg_tokens = round(sum(r["total_tokens"] for r in runs) / total, 1) if total else 0
        avg_latency = round(sum(r["latency_sec"] for r in runs) / total, 3) if total else 0
        avg_score = round(sum(r["score"] for r in runs) / total, 4) if total else 0
        total_cost = round(sum(r["est_cost_usd"] for r in runs), 4)

        table.append({
            "method": label,
            "success_rate": f"{successes}/{total}",
            "accuracy_percent": round((successes / total) * 100, 1) if total else 0,
            "avg_llm_calls": avg_calls,
            "avg_tokens": avg_tokens,
            "avg_latency_sec": avg_latency,
            "avg_score": avg_score,
            "total_est_cost_usd": total_cost,
        })

    print("\n" + "=" * 95)
    print("AGGREGATE COMPARISON TABLE: LATS (Grounded) vs. LATS (Ungrounded)")
    print("=" * 95)
    print(f"{'Method':<22} | {'Success':<8} | {'Acc%':<8} | {'Calls':<8} | {'Tokens':<10} | {'Latency':<10} | {'AvgScore':<10} | {'Total $':<8}")
    print("-" * 95)
    for row in table:
        print(
            f"{row['method']:<22} | {row['success_rate']:<8} | {row['accuracy_percent']:<8} | "
            f"{row['avg_llm_calls']:<8} | {row['avg_tokens']:<10} | {row['avg_latency_sec']:<10} | "
            f"{row['avg_score']:<10} | ${row['total_est_cost_usd']:<7}"
        )
    print("=" * 95)

    # Key finding
    grounded_row = next(r for r in table if r["method"] == "LATS (grounded)")
    ungrounded_row = next(r for r in table if r["method"] == "LATS (ungrounded)")
    print("\nKEY FINDING:")
    print(f"  Grounded LATS:   {grounded_row['success_rate']} success ({grounded_row['accuracy_percent']}%) | Avg score: {grounded_row['avg_score']}")
    print(f"  Ungrounded LATS: {ungrounded_row['success_rate']} success ({ungrounded_row['accuracy_percent']}%) | Avg score: {ungrounded_row['avg_score']}")
    diff = grounded_row["accuracy_percent"] - ungrounded_row["accuracy_percent"]
    if diff > 0:
        print(f"  → Grounded LATS wins by {diff} percentage points.")
        print(f"  → Ungrounded LATS is 'expensive theater' — it explores branches the DB would reject.")
    elif diff < 0:
        print(f"  → Unexpected: ungrounded won by {abs(diff)} points (random variance).")
    else:
        print(f"  → Tied — run with more cases or higher iterations to see separation.")

    # Save artifact
    output_path = artifacts_dir / "lats_grounded_vs_ungrounded.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "comparison_table": table,
                "cases": all_results,
            },
            f,
            indent=2,
        )
    print(f"\n[Artifact Saved] {output_path}")


if __name__ == "__main__":
    main()