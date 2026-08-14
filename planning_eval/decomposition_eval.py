"""
planning_eval/decomposition_eval.py

Task 1 evaluation harness: decomposition-first vs. dynamic/interleaved
decomposition, run against the SAME real IronBridge request type
(delay-risk resolution), producing:

  1. artifacts/decomposition_comparison.json -- per-case + aggregate
     table (llm_calls, approx_tokens, latency) backing the README's
     comparison table.
  2. A printed divergence report for CASE_DIVERGENCE, showing exactly
     where dynamic decomposition changes course after an early
     observation that decomposition-first would have executed past
     regardless.
  3. test_cycle_rejected() -- demonstrates planning.models.Plan's
     acyclicity enforcement firing at construction time (Suggested
     Exercise #1 in the toolkit README), so the DAG concern is shown,
     not just asserted.

Keep this test suite FIXED once real evaluation starts (per the lab's
guardrails) -- add new cases as new functions, don't edit these once
numbers have been reported in the README.
"""

from __future__ import annotations

import sys
import json
import time
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
ROOT_DIR = THIS_DIR.parent
for _p in (THIS_DIR, ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from pydantic import ValidationError

from planning.models import Plan, Task




from planning.algorithms.decomposition import decompose_goal, execute_plan
from planning.algorithms.dynamic_decomposition import dynamic_decomposition
from planning.model_provider import get_planning_llm
# ---------------------------------------------------------------------------
# Fixed test suite. Each case is a real request shape the office /
# scheduling desk actually sends. Keep IDs stable, add new cases as new
# entries rather than editing these once you've reported numbers.
# ---------------------------------------------------------------------------

TEST_CASES = [
    {
        "id": "CASE_BASELINE_CONVERGE",
        "project_id": 1,
        "request": (
            "ProjectID 1 is flagged at risk: rebar delivery is 9 days late "
            "and the material log shows it's below minimum stock. Recommend "
            "a mitigation that fits the remaining budget."
        ),
        "note": (
            "A real, unambiguous material shortage. Both methods should "
            "converge on the same diagnose -> rank -> propose -> notify "
            "sequence, since the diagnosis confirms the assumption baked "
            "into decomposition-first's up-front plan. Used as a baseline, "
            "not the divergence case."
        ),
    },
    {
        "id": "CASE_DIVERGENCE",
        "project_id": 4,
        "request": (
            "ProjectID 4 is flagged at risk by the site engineer with no "
            "further detail beyond 'we are going to miss the deadline, "
            "please advise.'"
        ),
        "note": (
            "The request gives no cause up front -- exactly the case where "
            "decomposition-first has to guess the DAG shape before any real "
            "data exists. If the real diagnosis shows no material shortage "
            "and no equipment issue, decomposition-first's fixed "
            "rank_options step (written against the four generic "
            "material/equipment strategies) still runs and produces a "
            "recommendation that doesn't match the real cause. Dynamic "
            "decomposition observes the diagnosis first and can route "
            "around the irrelevant step."
        ),
    },
    {
        "id": "CASE_BUDGET_CONSTRAINED",
        "project_id": 2,
        "request": (
            "ProjectID 2: equipment maintenance is pushing the schedule "
            "back. Propose a fix but do NOT recommend anything that would "
            "exceed the remaining budget."
        ),
        "note": (
            "Real budget constraint -- exercises propose_plan's grounding "
            "against db.get_project()'s RemainingBudget, same real check "
            "used by planning/environment.py for LATS/Reflexion (task 3)."
        ),
    },
]


from planning.algorithms.decomposition import DEFAULT_EXECUTORS  

def _approx_tokens(*texts: str) -> int:
    return sum(len(t) for t in texts) // 4


def run_case(case: dict, llm) -> dict:
    # -----------------------------------------------------------------------
    # Decomposition-first
    # -----------------------------------------------------------------------
    t0 = time.time()
    plan = decompose_goal(case["request"], llm, project_id=case["project_id"])
    df_outputs = execute_plan(plan, llm)
    df_latency = round(time.time() - t0, 3)
    
    # Metrics
    df_token_texts = [case["request"], plan.goal] + [t.instruction for t in plan.tasks] + list(df_outputs.values())
    df_tokens = _approx_tokens(*df_token_texts)
    df_calls = 1 + sum(1 for t in plan.tasks if t.id not in DEFAULT_EXECUTORS)

    # -----------------------------------------------------------------------
    # Dynamic decomposition
    # -----------------------------------------------------------------------
    t0 = time.time()
    dd_history = dynamic_decomposition(case["request"], llm)
    dd_latency = round(time.time() - t0, 3)
    
    dd_token_texts = [case["request"]] + [instr + out for instr, out in dd_history]
    dd_tokens = _approx_tokens(*dd_token_texts)
    dd_calls = len(dd_history)  # 1 LLM decision call per step

    return {
        "case_id": case["id"],
        "request": case["request"],
        "note": case["note"],
        "decomposition_first": {
            "plan_tasks": [t.id for t in plan.tasks],
            "execution_batches": plan.execution_batches(),
            "node_outputs": df_outputs,
            "llm_calls": df_calls,
            "approx_tokens": df_tokens,
            "latency_sec": df_latency,
        },
        "dynamic_decomposition": {
            "steps": [{"instruction": instr, "output": out} for instr, out in dd_history],
            "llm_calls": dd_calls,
            "approx_tokens": dd_tokens,
            "latency_sec": dd_latency,
            "stopped_reason": "done",  # toolkit version doesn't track this
        },
    }

def print_divergence_report(case_result: dict) -> None:
    print("\n" + "=" * 78)
    print(f"DIVERGENCE REPORT: {case_result['case_id']}")
    print("=" * 78)
    df = case_result["decomposition_first"]
    dd = case_result["dynamic_decomposition"]

    print("\n[decomposition-first] committed plan (generated before any real data):")
    for t in df["plan_tasks"]:
        print(f"    - {t}")
    print(f"  -> executed all {len(df['plan_tasks'])} nodes regardless of what diagnose() returned.")

    print("\n[dynamic decomposition] steps actually taken, one at a time:")
    for i, s in enumerate(dd["steps"]):
        print(f"    {i}. [{s['instruction']}]")
    if len(dd["steps"]) != len(df["plan_tasks"]):
        print(
            f"  -> took {len(dd['steps'])} step(s) vs. decomposition-first's fixed "
            f"{len(df['plan_tasks'])}-node plan: the dynamic method reacted to the "
            f"real diagnosis instead of executing a step decided before it existed."
        )
    else:
        print("  -> same step count this run; inspect step *content* above for routing differences.")


def test_cycle_rejected() -> None:
    """Suggested Exercise #1: introduce a cycle and confirm Plan rejects
    it at construction time, before any execution happens."""
    print("\n" + "=" * 78)
    print("ACYCLICITY ENFORCEMENT CHECK (planning.models.Plan)")
    print("=" * 78)
    try:
        Plan(
            goal="Deliberately cyclic plan for the acyclicity demo",
            tasks=[
                Task(id="a", instruction="Step A depends on step B", depends_on=["b"]),
                Task(id="b", instruction="Step B depends on step A", depends_on=["a"]),
            ],
        )
        print("FAIL: cyclic plan was NOT rejected -- this should never happen.")
    except ValidationError as e:
        print("PASS: cyclic plan rejected at construction time, as required.")
        print(f"  validator message: {e.errors()[0]['msg']}")


def main() -> None:
    llm = get_planning_llm()
    artifacts_dir = ROOT_DIR / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    test_cycle_rejected()

    all_results = []
    for case in TEST_CASES:
        print(f"\nRunning case: {case['id']}")
        result = run_case(case, llm)
        all_results.append(result)
        if case["id"] == "CASE_DIVERGENCE":
            print_divergence_report(result)

    # Aggregate table
    agg = {"decomposition_first": {"calls": 0, "tokens": 0, "latency": 0.0},
           "dynamic_decomposition": {"calls": 0, "tokens": 0, "latency": 0.0}}
    for r in all_results:
        agg["decomposition_first"]["calls"] += r["decomposition_first"]["llm_calls"]
        agg["decomposition_first"]["tokens"] += r["decomposition_first"]["approx_tokens"]
        agg["decomposition_first"]["latency"] += r["decomposition_first"]["latency_sec"]
        agg["dynamic_decomposition"]["calls"] += r["dynamic_decomposition"]["llm_calls"]
        agg["dynamic_decomposition"]["tokens"] += r["dynamic_decomposition"]["approx_tokens"]
        agg["dynamic_decomposition"]["latency"] += r["dynamic_decomposition"]["latency_sec"]

    n = len(all_results)
    comparison_table = {
        method: {
            "avg_llm_calls": round(v["calls"] / n, 2),
            "avg_approx_tokens": round(v["tokens"] / n, 1),
            "avg_latency_sec": round(v["latency"] / n, 3),
        }
        for method, v in agg.items()
    }

    print("\n" + "=" * 78)
    print("AGGREGATE COMPARISON (decomposition-first vs. dynamic decomposition)")
    print("=" * 78)
    for method, stats in comparison_table.items():
        print(f"  {method:24s} | calls={stats['avg_llm_calls']:<6} tokens={stats['avg_approx_tokens']:<8} latency={stats['avg_latency_sec']}s")

    output_path = artifacts_dir / "decomposition_comparison.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "comparison_table": comparison_table,
                "cases": all_results,
            },
            f,
            indent=2,
        )
    print(f"\n[Artifact Saved] {output_path}")


if __name__ == "__main__":
    main()
    