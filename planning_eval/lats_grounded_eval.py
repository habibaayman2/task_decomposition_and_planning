"""
planning_eval/lats_grounded_eval.py

Evaluation harness for LATS: Grounded (IronBridgeEnvironment) vs.
Ungrounded (toolkit's randomized Environment).

Produces:
  1. artifacts/lats_grounded_comparison.json
  2. Console report showing exactly where grounded catches failures
     the ungrounded randomized evaluator misses.

Keep this test suite FIXED once evaluation starts.
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

from planning.algorithms.lats import lats, LATSResult
from planning.algorithms.environment import IronBridgeEnvironment
from planning.algorithms import Environment as RandomizedEnvironment
from planning.model_provider import get_planning_llm


TEST_CASES = [
    {
        "id": "LATS_RUSH_OVER_BUDGET",
        "request": (
            "ProjectID 1: Place a rush order with premium cost $999999 to "
            "expedite the missing material."
        ),
        "note": (
            "Grounded env rejects because $999999 > RemainingBudget. "
            "Randomized env may accept by chance."
        ),
    },
    {
        "id": "LATS_INACTIVE_SUPPLIER",
        "request": (
            "ProjectID 2: Source replacement equipment from Cement Co "
            "to avoid schedule slip."
        ),
        "note": (
            "Grounded env checks ContractStatus in DB. Randomized env ignores "
            "real supplier status."
        ),
    },
    {
        "id": "LATS_VALID_RESEQUENCE",
        "request": (
            "ProjectID 1: Resequence unaffected trades ahead of the blocked item "
            "so the crew stays productive while the blocking issue is resolved."
        ),
        "note": (
            "Grounded env accepts if no budget overrun and stock OK. "
            "Randomized env may spuriously reject."
        ),
    },
    {
        "id": "LATS_LOW_STOCK_RESEQUENCE",
        "request": (
            "ProjectID 3: Resequence plumbing work to avoid concrete delay "
            "without ordering new materials."
        ),
        "note": (
            "Grounded env fails if materials are below minimum stock because "
            "resequencing alone cannot fix a stock shortfall."
        ),
    },
    {
        "id": "LATS_RUSH_IN_BUDGET",
        "request": (
            "ProjectID 1: Rush order $500 for missing rebar, checked against budget."
        ),
        "note": "Small rush cost likely within budget; both should pass.",
    },
]


def _approx_tokens(*texts: str) -> int:
    return sum(len(t) for t in texts) // 4


def _est_cost_usd(tokens: int) -> float:
    return round(tokens * 0.00001, 4)


class CallCounter:
    def __init__(self, llm):
        self._llm = llm
        self.count = 0
        self._token_estimate = 0

    def invoke(self, messages, **kwargs):
        self.count += 1
        result = self._llm.invoke(messages, **kwargs)
        prompt_text = ""
        if isinstance(messages, list):
            prompt_text = "\n".join(getattr(m, "content", str(m)) for m in messages)
        else:
            prompt_text = str(messages)
        self._token_estimate += _approx_tokens(prompt_text, str(getattr(result, "content", "")))
        return result

    def with_structured_output(self, schema, **kwargs):
        inner = self._llm.with_structured_output(schema, **kwargs)
        return _StructuredCounter(inner, self)

    @property
    def approx_tokens(self):
        return self._token_estimate


class _StructuredCounter:
    def __init__(self, inner, owner: CallCounter):
        self._inner = inner
        self._owner = owner

    def invoke(self, messages, **kwargs):
        self._owner.count += 1
        result = self._inner.invoke(messages, **kwargs)
        prompt_text = ""
        if isinstance(messages, list):
            prompt_text = "\n".join(getattr(m, "content", str(m)) for m in messages)
        else:
            prompt_text = str(messages)
        resp_text = str(result.model_dump() if hasattr(result, "model_dump") else result)
        self._owner._token_estimate += _approx_tokens(prompt_text, resp_text)
        return result


def run_lats(case: dict, llm, environment, label: str) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()

    try:
        result: LATSResult = lats(
            task=case["request"],
            llm=counter,
            environment=environment,
            iterations=2,
            n_actions=2,
        )
        latency = round(time.time() - t0, 3)
        return {
            "method": f"lats_{label}",
            "case_id": case["id"],
            "success": result.success,
            "score": result.best_score,
            "output": result.output,
            "iterations": result.iterations,
            "pruned": result.pruned_count,
            "llm_calls": counter.count,
            "approx_tokens": counter.approx_tokens,
            "est_cost_usd": _est_cost_usd(counter.approx_tokens),
            "latency_sec": latency,
        }
    except Exception as exc:
        latency = round(time.time() - t0, 3)
        return {
            "method": f"lats_{label}",
            "case_id": case["id"],
            "success": False,
            "score": 0.0,
            "error": f"{type(exc).__name__}: {exc}",
            "llm_calls": counter.count,
            "approx_tokens": counter.approx_tokens,
            "est_cost_usd": _est_cost_usd(counter.approx_tokens),
            "latency_sec": latency,
        }


def main() -> None:
    llm = get_planning_llm()
    grounded_env = IronBridgeEnvironment(success_threshold=0.6)
    ungrounded_env = RandomizedEnvironment(success_threshold=0.6)

    artifacts_dir = ROOT_DIR / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    all_results = []

    print("\n" + "=" * 78)
    print("LATS EVALUATION: Grounded vs. Ungrounded Environment")
    print("=" * 78)

    for case in TEST_CASES:
        print(f"\nCase: {case['id']}")
        print(f"  Request: {case['request'][:100]}...")

        print("  -> LATS (Grounded)...")
        r_g = run_lats(case, llm, grounded_env, "grounded")
        all_results.append(r_g)
        status = "PASS" if r_g["success"] else "FAIL"
        print(f"     Status: {status} | Score: {r_g.get('score', 0):.2f} | Calls: {r_g['llm_calls']} | Time: {r_g['latency_sec']}s")

        print("  -> LATS (Ungrounded / Randomized)...")
        r_u = run_lats(case, llm, ungrounded_env, "ungrounded")
        all_results.append(r_u)
        status = "PASS" if r_u["success"] else "FAIL"
        print(f"     Status: {status} | Score: {r_u.get('score', 0):.2f} | Calls: {r_u['llm_calls']} | Time: {r_u['latency_sec']}s")

    # Comparison table
    table = []
    for method in ["lats_grounded", "lats_ungrounded"]:
        runs = [r for r in all_results if r["method"] == method]
        successes = sum(1 for r in runs if r["success"])
        total = len(runs)
        avg_calls = round(sum(r["llm_calls"] for r in runs) / total, 2) if total else 0
        avg_tokens = round(sum(r["approx_tokens"] for r in runs) / total, 1) if total else 0
        avg_latency = round(sum(r["latency_sec"] for r in runs) / total, 3) if total else 0
        avg_score = round(sum(r.get("score", 0) for r in runs) / total, 3) if total else 0
        total_cost = round(sum(r["est_cost_usd"] for r in runs), 4)
        table.append({
            "method": method,
            "success_rate": f"{successes}/{total}",
            "avg_score": avg_score,
            "avg_llm_calls": avg_calls,
            "avg_approx_tokens": avg_tokens,
            "avg_latency_sec": avg_latency,
            "total_est_cost_usd": total_cost,
        })

    print("\n" + "=" * 78)
    print("LATS GROUNDED vs. UNGROUNDED COMPARISON TABLE")
    print("=" * 78)
    print(f"{'Method':<22} | {'Success':<8} | {'Score':<6} | {'Calls':<6} | {'Tokens':<8} | {'Latency':<8} | {'Est. $':<8}")
    print("-" * 86)
    for row in table:
        print(
            f"{row['method']:<22} | {row['success_rate']:<8} | "
            f"{row['avg_score']:<6} | {row['avg_llm_calls']:<6} | "
            f"{row['avg_approx_tokens']:<8} | {row['avg_latency_sec']:<8} | "
            f"{row['total_est_cost_usd']:<8}"
        )

    # Find cases where grounded caught something ungrounded missed
    catches = []
    by_case = {}
    for r in all_results:
        by_case.setdefault(r["case_id"], {})[r["method"]] = r

    for case_id, methods in by_case.items():
        g = methods.get("lats_grounded", {})
        u = methods.get("lats_ungrounded", {})
        if not g.get("success") and u.get("success"):
            catches.append({
                "case_id": case_id,
                "grounded_rejected": g.get("env_details", "Grounded env rejected"),
                "ungrounded_accepted": u.get("output", "")[:200],
            })

    if catches:
        print("\n" + "=" * 78)
        print("GROUNDED CAUGHT WHAT UNGROUNDED MISSED")
        print("=" * 78)
        for c in catches:
            print(f"\nCase: {c['case_id']}")
            print(f"  Grounded rejected because: {c['grounded_rejected']}")
            print(f"  Ungrounded incorrectly accepted: {c['ungrounded_accepted']}...")
    else:
        print("\nNo cases where grounded rejected and ungrounded accepted in this run.")
        print("(This is expected for some seeds; the key is the score difference.)")

    output_path = artifacts_dir / "lats_grounded_comparison.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "comparison_table": table,
            "catches": catches,
            "cases": all_results,
        }, f, indent=2)
    print(f"\n[Artifact Saved] {output_path}")


if __name__ == "__main__":
    main()