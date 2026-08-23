"""
planning_eval/finalize_partial.py

Builds the SAME master comparison table full_comparison.py produces at
the end of a complete run, but from artifacts/full_comparison_table.partial.json
-- i.e. from whatever calls finished before you Ctrl+C'd or it crashed,
not the full 10-cases x 10-methods sweep.

Use this when you don't want to wait out a rate limit (daily caps in
particular won't resolve no matter how long the retry loop waits) but
still want a usable table from the coverage you already have.

Usage (from repo root):
    python -m planning_eval.finalize_partial

Writes artifacts/full_comparison_table.json (SAME filename/shape the
full run writes, so your README table code doesn't need to change) but
with "partial": true and a "coverage" block listing which case/method
cells are present vs missing, so nothing is silently presented as more
complete than it is.

Works with partial files from EITHER version of full_comparison.py:
if the results already carry a "case_id" field, that's used directly;
if not (an older partial file, from before that field was added),
case_id is inferred positionally -- each case runs exactly len(METHODS)
calls in a fixed order, so index // len(METHODS) recovers it reliably,
even for a case that was cut off partway through.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

METHODS = [
    "Decomposition-first", "Dynamic",
    "Plan-and-Solve", "Tree-of-Thoughts",
    "LATS (Grounded)", "LATS (Ungrounded)",
    "Self-Refine (Grounded)", "Self-Refine (Ungrounded)",
    "Reflexion (Grounded)", "Reflexion (Ungrounded)",
]

# Must match planning_eval/full_comparison.py's TEST_SUITE ids, in order.
CASE_IDS = [
    "T01_MITIGATE_DELAY", "T02_MATERIAL_SHORTFALL", "T03_RUSH_IN_BUDGET",
    "T04_SIMPLE_CAPACITY", "T05_OVER_BUDGET_RUSH", "T06_RESEQUENCE",
    "T07_SWITCH_SUPPLIER", "T08_EMERGENCY_POUR", "T09_EQUIPMENT_RENTAL",
    "T10_MULTI_TRADE_RESEQ",
]


def _case_id_for(idx: int, r: dict) -> str:
    if r.get("case_id"):
        return r["case_id"]
    case_idx = idx // len(METHODS)
    return CASE_IDS[case_idx] if case_idx < len(CASE_IDS) else f"UNKNOWN_CASE_{case_idx}"


def build_table(all_results: list[dict]) -> list[dict]:
    """Identical aggregation logic to full_comparison.py's main() --
    kept in sync by hand since duplicating it here avoids importing
    the whole eval module (and its LLM/env dependencies) just to
    finalize a partial file."""
    table = []
    for method in METHODS:
        runs = [r for r in all_results if r["method"] == method]
        rate_limited_runs = [r for r in runs if r.get("error_type") == "rate_limited"]
        json_failed_runs = [r for r in runs if r.get("error_type") == "json_generation_failed"]
        tool_failed_runs = [r for r in runs if r.get("error_type") == "tool_generation_failed"]
        scored_runs = [r for r in runs if r.get("error_type") not in
                       ("rate_limited", "json_generation_failed", "tool_generation_failed")]
        scored_total = len(scored_runs)

        successes = sum(1 for r in scored_runs if r["success"])
        avg_calls = round(sum(r["llm_calls"] for r in scored_runs) / scored_total, 2) if scored_total else None
        avg_tokens = round(sum(r["tokens"] for r in scored_runs) / scored_total, 1) if scored_total else None
        avg_latency = round(sum(r["latency"] for r in scored_runs) / scored_total, 3) if scored_total else None
        avg_score = round(sum(r["score"] for r in scored_runs) / scored_total, 3) if scored_total else None
        total_cost = round(sum(r["cost"] for r in runs), 4)
        self_graded = any(r.get("self_graded") for r in scored_runs)

        table.append({
            "method": method,
            "cases_seen": len(runs),
            "success_rate": f"{successes}/{scored_total}" if scored_total else "0/0 (no data yet)",
            "accuracy_pct": round((successes / scored_total) * 100, 1) if scored_total else None,
            "avg_score": avg_score,
            "avg_llm_calls": avg_calls,
            "avg_tokens": avg_tokens,
            "avg_latency_sec": avg_latency,
            "total_est_cost_usd": total_cost,
            "rate_limited_excluded": len(rate_limited_runs),
            "json_generation_failed_excluded": len(json_failed_runs),
            "tool_generation_failed_excluded": len(tool_failed_runs),
            "self_graded": self_graded,
        })
    return table


def build_coverage(all_results: list[dict]) -> dict:
    seen = set()
    for idx, r in enumerate(all_results):
        seen.add((_case_id_for(idx, r), r["method"]))

    missing = [
        {"case_id": cid, "method": m}
        for cid in CASE_IDS
        for m in METHODS
        if (cid, m) not in seen
    ]
    return {
        "expected_cells": len(CASE_IDS) * len(METHODS),
        "completed_cells": len(seen),
        "missing_cells": len(missing),
        "missing": missing,
    }


def main() -> None:
    artifacts_dir = ROOT_DIR / "artifacts"
    partial_path = artifacts_dir / "full_comparison_table.partial.json"

    if not partial_path.exists():
        print(f"No partial file found at {partial_path}.")
        print("Either the run completed cleanly already (check for "
              "full_comparison_table.json instead) or it crashed before "
              "the first call finished.")
        return

    with open(partial_path, "r", encoding="utf-8") as f:
        partial = json.load(f)

    all_results = partial.get("results", [])
    if not all_results:
        print("Partial file exists but has zero completed calls -- nothing to finalize.")
        return

    # Backfill case_id positionally for results that don't have it yet
    # (partial files from before this field was added to full_comparison.py).
    for idx, r in enumerate(all_results):
        r.setdefault("case_id", _case_id_for(idx, r))

    table = build_table(all_results)
    coverage = build_coverage(all_results)

    print("\n" + "=" * 100)
    print(f"PARTIAL COMPARISON TABLE  ({coverage['completed_cells']}/{coverage['expected_cells']} "
          f"cells completed)")
    print("=" * 100)
    print(f"{'Method':<26}{'Seen':<6}{'Success':<10}{'Acc%':<8}{'AvgScore':<10}{'RateLim':<9}")
    print("-" * 100)
    for row in table:
        print(f"{row['method']:<26}{row['cases_seen']:<6}{row['success_rate']:<10}"
              f"{str(row['accuracy_pct']):<8}{str(row['avg_score']):<10}{row['rate_limited_excluded']:<9}")

    if coverage["missing_cells"]:
        print(f"\n{coverage['missing_cells']} cell(s) not yet run. First few missing:")
        for m in coverage["missing"][:10]:
            print(f"  - {m['case_id']} / {m['method']}")
        if coverage["missing_cells"] > 10:
            print(f"  ... and {coverage['missing_cells'] - 10} more")

    output_path = artifacts_dir / "full_comparison_table.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "partial": True,
            "coverage": coverage,
            "comparison_table": table,
            "routing_recommendations": "see_console_output",
            "cases": all_results,
        }, f, indent=2)

    print(f"\n[Artifact Saved] {output_path}  (partial=true, {coverage['completed_cells']}/"
          f"{coverage['expected_cells']} cells)")
    print("Re-run the full sweep later with: python -m planning_eval.full_comparison")
    print("(it restarts from T01 -- this doesn't resume call-by-call, it just makes sure")
    print(" what you already paid for in API calls isn't thrown away)")


if __name__ == "__main__":
    main()
