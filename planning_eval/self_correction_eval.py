"""
planning_eval/self_correction_eval.py

Evaluation harness for Self-Refine vs. Reflexion, including grounded vs.
ungrounded critique comparison.

Produces:
  1. artifacts/self_correction_comparison.json — per-case + aggregate
     table (llm_calls, approx_tokens, latency) backing the README's
     comparison table.
  2. Console report with the comparison table (accuracy, LLM calls, tokens,
     latency).
  3. compare_grounded_vs_ungrounded() demo showing exactly where the
     grounded environment catches a failure the ungrounded self-critique
     misses.

Keep this test suite FIXED once real evaluation starts (per the lab's
guardrails) — add new cases as new entries rather than editing these once
you've reported numbers.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
ROOT_DIR = THIS_DIR.parent
for _p in (THIS_DIR, ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from planning.algorithms.self_refine import self_refine, SelfRefineResult
from planning.algorithms.reflexion import reflexion, ReflexionResult
from planning.algorithms.environment import IronBridgeEnvironment
from planning.model_provider import get_planning_llm


# ---------------------------------------------------------------------------
# Fixed test suite. Each case is a real request shape that exercises
# self-correction. Keep IDs stable, add new cases as new entries rather
# than editing these once you've reported numbers.
# ---------------------------------------------------------------------------

TEST_CASES = [
    {
        "id": "CASE_RUSH_OVER_BUDGET",
        "request": (
            "ProjectID 1 is at risk because rebar delivery is 9 days late. "
            "Propose a mitigation. Consider rush-ordering steel from Steel Yard "
            "but do not exceed the remaining budget."
        ),
        "note": (
            "The naive draft often proposes a rush order without checking the "
            "real remaining budget. Grounded critique catches this because "
            "environment.py checks rush_cost against db.get_project(). "
            "Ungrounded LLM critique often misses the specific dollar mismatch."
        ),
    },
    {
        "id": "CASE_INACTIVE_SUPPLIER",
        "request": (
            "ProjectID 2 faces equipment maintenance delays. Recommend sourcing "
            "a replacement excavator from Cement Co to stay on schedule."
        ),
        "note": (
            "Cement Co may have ContractStatus != Active in the real DB. "
            "Grounded environment catches inactive suppliers; ungrounded "
            "self-critique often assumes the supplier is valid."
        ),
    },
    {
        "id": "CASE_RESEQUENCE_IGNORES_STOCK",
        "request": (
            "ProjectID 3 is behind schedule. Propose resequencing the work "
            "to avoid the delay without ordering new materials."
        ),
        "note": (
            "If the material log shows items below minimum stock, resequencing "
            "alone is insufficient. Grounded check catches this; ungrounded "
            "critique may praise the 'creative' resequencing plan."
        ),
    },
    {
        "id": "CASE_REFLEXION_MEMORY",
        "request": (
            "ProjectID 1: Place a rush order with premium cost $999999 to "
            "expedite the missing material."
        ),
        "note": (
            "Should fail trial 1 (over budget), learn from reflection, "
            "succeed trial 2."
        ),
    },
]


def _approx_tokens(*texts: str) -> int:
    """Rough token estimate: 1 token ≈ 4 characters."""
    return sum(len(t) for t in texts) // 4


def _est_cost_usd(tokens: int) -> float:
    """$10 per 1M tokens, a round planning-time estimate -- not tied to any
    specific provider's actual pricing."""
    return round(tokens * 0.00001, 4)


def run_self_refine(case: dict, llm, grounded: bool = True) -> dict:
    """Run Self-Refine on a single case and return metrics."""
    env = IronBridgeEnvironment() if grounded else None
    t0 = time.time()
    result: SelfRefineResult = self_refine(case["request"], llm, environment=env)
    latency = round(time.time() - t0, 3)

    # self_refine() now tracks its own approx_tokens; fall back to a local
    # recompute only if an older SelfRefineResult without the field is used.
    tokens = getattr(result, "approx_tokens", None)
    if not tokens:
        tokens = _approx_tokens(case["request"], result.draft, result.critique, result.revised)

    return {
        "method": "self_refine",
        "grounded": grounded,
        "case_id": case["id"],
        "success": result.environment_feedback.success if result.environment_feedback else False,
        "score": result.environment_feedback.score if result.environment_feedback else 0.0,
        "draft": result.draft,
        "critique": result.critique,
        "revised": result.revised,
        "grounded_issues": result.grounded_issues,
        "env_details": result.environment_feedback.details if result.environment_feedback else [],
        "llm_calls": result.llm_calls,
        "approx_tokens": tokens,
        "est_cost_usd": _est_cost_usd(tokens),
        "latency_sec": latency,
    }


def run_reflexion(case: dict, llm, grounded: bool = True) -> dict:
    """Run Reflexion on a single case and return metrics."""
    env = IronBridgeEnvironment() if grounded else None
    t0 = time.time()
    result: ReflexionResult = reflexion(case["request"], llm, environment=env, max_trials=3)
    latency = round(time.time() - t0, 3)

    # reflexion() now tracks its own approx_tokens; fall back to a local
    # recompute only if an older ReflexionResult without the field is used.
    tokens = getattr(result, "approx_tokens", None)
    if not tokens:
        token_texts = [case["request"]]
        for t in result.trials:
            token_texts.extend([t.attempt, t.reflection or ""])
        tokens = _approx_tokens(*token_texts)

    return {
        "method": "reflexion",
        "grounded": grounded,
        "case_id": case["id"],
        "success": result.success,
        "score": result.trials[-1].feedback.score if result.trials else 0.0,
        "trials": [
            {
                "number": t.number,
                "attempt": t.attempt,
                "reflection": t.reflection,
                "score": t.feedback.score,
                "success": t.feedback.success,
            }
            for t in result.trials
        ],
        "memory": result.memory,
        "llm_calls": result.total_llm_calls,
        "approx_tokens": tokens,
        "est_cost_usd": _est_cost_usd(tokens),
        "latency_sec": latency,
    }


def compare_grounded_vs_ungrounded() -> None:
    """
    Demo: run the SAME case through both grounded and ungrounded Self-Refine
    and show exactly where the grounded environment catches a failure the
    ungrounded version misses.
    """
    print("\n" + "=" * 78)
    print("DEMO: compare_grounded_vs_ungrounded()")
    print("=" * 78)

    llm = get_planning_llm()
    case = TEST_CASES[0]  # CASE_RUSH_OVER_BUDGET — classic budget mismatch

    print(f"\nCase: {case['id']}")
    print(f"Request: {case['request'][:120]}...")

    # Ungrounded
    print("\n--- UNGROUNDED Self-Refine ---")
    ungrounded = run_self_refine(case, llm, grounded=False)
    print(f"Draft (first 200 chars):\n{ungrounded['draft'][:200]}...")
    print(f"Critique: {ungrounded['critique'][:200]}...")
    print(f"Success: {ungrounded['success']} | Score: {ungrounded['score']}")
    print(f"Grounded issues found: {ungrounded['grounded_issues']}")

    # Grounded
    print("\n--- GROUNDED Self-Refine ---")
    grounded = run_self_refine(case, llm, grounded=True)
    print(f"Draft (first 200 chars):\n{grounded['draft'][:200]}...")
    print(f"Critique: {grounded['critique'][:200]}...")
    print(f"Success: {grounded['success']} | Score: {grounded['score']}")
    print(f"Grounded issues found: {grounded['grounded_issues']}")
    print(f"Environment details: {grounded['env_details']}")

    # Diff
    print("\n--- DIFF (what grounded caught that ungrounded missed) ---")
    ug_issues = set(line.lower() for line in ungrounded["grounded_issues"])
    g_issues = set(line.lower() for line in grounded["grounded_issues"])
    env_only = [d for d in grounded["env_details"] if d not in ungrounded["env_details"]]

    missed = g_issues - ug_issues
    if missed:
        print("Deterministic checks only in grounded:")
        for m in missed:
            print(f"  • {m}")
    if env_only:
        print("Environment feedback only in grounded:")
        for e in env_only:
            print(f"  • {e}")
    if not missed and not env_only:
        print("Both versions flagged the same issues this run (rare but possible).")


def main() -> None:
    llm = get_planning_llm()
    artifacts_dir = ROOT_DIR / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    all_results = []

    for case in TEST_CASES:
        print(f"\nRunning case: {case['id']}")

        # Self-Refine grounded
        print("  -> Self-Refine (grounded)...")
        sr_g = run_self_refine(case, llm, grounded=True)
        all_results.append(sr_g)

        # Self-Refine ungrounded
        print("  -> Self-Refine (ungrounded)...")
        sr_u = run_self_refine(case, llm, grounded=False)
        all_results.append(sr_u)

        # Reflexion grounded
        print("  -> Reflexion (grounded)...")
        ref_g = run_reflexion(case, llm, grounded=True)
        all_results.append(ref_g)

        # Reflexion ungrounded
        print("  -> Reflexion (ungrounded)...")
        ref_u = run_reflexion(case, llm, grounded=False)
        all_results.append(ref_u)

    # Build comparison table
    table = []
    methods = [
        ("self_refine", True),
        ("self_refine", False),
        ("reflexion", True),
        ("reflexion", False),
    ]
    for method, grounded in methods:
        label = f"{method}_{'grounded' if grounded else 'ungrounded'}"
        runs = [r for r in all_results if r["method"] == method and r["grounded"] == grounded]
        successes = sum(1 for r in runs if r["success"])
        total = len(runs)
        avg_calls = round(sum(r["llm_calls"] for r in runs) / total, 2) if total else 0
        avg_tokens = round(sum(r["approx_tokens"] for r in runs) / total, 1) if total else 0
        avg_latency = round(sum(r["latency_sec"] for r in runs) / total, 3) if total else 0
        total_cost = round(sum(r["est_cost_usd"] for r in runs), 4)
        table.append({
            "method": label,
            "success_rate": f"{successes}/{total}",
            "avg_llm_calls": avg_calls,
            "avg_approx_tokens": avg_tokens,
            "avg_latency_sec": avg_latency,
            "total_est_cost_usd": total_cost,
        })

    print("\n" + "=" * 78)
    print("SELF-CORRECTION COMPARISON TABLE")
    print("=" * 78)
    print(f"{'Method':<24} | {'Success':<8} | {'Calls':<6} | {'Tokens':<8} | {'Latency':<8} | {'Est. $':<8}")
    print("-" * 84)
    for row in table:
        print(
            f"{row['method']:<24} | {row['success_rate']:<8} | "
            f"{row['avg_llm_calls']:<6} | {row['avg_approx_tokens']:<8} | "
            f"{row['avg_latency_sec']:<8} | {row['total_est_cost_usd']:<8}"
        )

    output_path = artifacts_dir / "self_correction_comparison.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "comparison_table": table,
                "cases": all_results,
            },
            f,
            indent=2,
        )
    print(f"\n[Artifact Saved] {output_path}")

    # Run the divergence demo
    compare_grounded_vs_ungrounded()


if __name__ == "__main__":
    main()
