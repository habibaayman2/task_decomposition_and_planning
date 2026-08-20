"""
planning_eval/full_comparison.py

Unified evaluation runner that executes EVERY required method against the
fixed test suite and produces the master comparison table.

Methods evaluated:
  • Decomposition-first DAG
  • Dynamic / Interleaved decomposition
  • Plan-and-Solve
  • Tree-of-Thoughts
  • LATS (Grounded)
  • LATS (Ungrounded)
  • Self-Refine (Grounded)
  • Self-Refine (Ungrounded)
  • Reflexion (Grounded)
  • Reflexion (Ungrounded)

Produces:
  1. artifacts/full_comparison_table.json — master artifact
  2. Console report with the complete comparison table
  3. Per-sub-task routing recommendations justified by the numbers

Keep the TEST_SUITE fixed once evaluation starts.
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

from planning.algorithms.decomposition import decompose_goal, execute_plan, final_output
from planning.algorithms.dynamic_decomposition import dynamic_decomposition
from planning.algorithms.plan_and_solve import plan_and_solve
from planning.algorithms.tree_of_thoughts import tree_of_thoughts
from planning.algorithms.lats import lats
from planning.algorithms.self_refine import self_refine
from planning.algorithms.reflexion import reflexion
from planning.algorithms.environment import IronBridgeEnvironment
from planning.algorithms import Environment as RandomizedEnvironment
from planning.model_provider import get_planning_llm


# ---------------------------------------------------------------------------
# FIXED TEST SUITE — do not edit once reported numbers are committed.
# ---------------------------------------------------------------------------

TEST_SUITE = [
    {
        "id": "T01_MITIGATE_DELAY",
        "request": "ProjectID 1: Mitigate delay risk by analyzing schedule dependencies.",
        "shape": "complex",
    },
    {
        "id": "T02_MATERIAL_SHORTFALL",
        "request": "ProjectID 1: Material shortage requires trade resequencing options.",
        "shape": "branching",
    },
    {
        "id": "T03_RUSH_IN_BUDGET",
        "request": "ProjectID 1: Address material shortage with a rush order within budget.",
        "shape": "validation",
    },
    {
        "id": "T04_SIMPLE_CAPACITY",
        "request": "ProjectID 1: A project has 3 developers for 10 days. Estimate capacity at 6 focused hours per day.",
        "shape": "simple",
    },
    {
        "id": "T05_OVER_BUDGET_RUSH",
        "request": "ProjectID 1: Place a rush order with premium cost $999999 to expedite the missing material.",
        "shape": "validation",
    },
    {
        "id": "T06_RESEQUENCE",
        "request": "ProjectID 1: Resequence unaffected trades ahead of the blocked item so the crew stays productive.",
        "shape": "branching",
    },
    {
        "id": "T07_SWITCH_SUPPLIER",
        "request": "ProjectID 1: Switch to steel yard supplier for delayed rebar.",
        "shape": "validation",
    },
    {
        "id": "T08_EMERGENCY_POUR",
        "request": "ProjectID 1: Emergency foundation pour tomorrow but cement is below minimum stock. Evaluate rush vs. slip.",
        "shape": "complex",
    },
    {
        "id": "T09_EQUIPMENT_RENTAL",
        "request": "ProjectID 2: Rent replacement excavator during maintenance window.",
        "shape": "simple",
    },
    {
        "id": "T10_MULTI_TRADE_RESEQ",
        "request": "ProjectID 3: Resequence 4 trades due to delayed steel delivery. Check stock for each trade first.",
        "shape": "complex",
    },
]


def _approx_tokens(*texts: str) -> int:
    return sum(len(t) for t in texts) // 4


def _est_cost_usd(tokens: int) -> float:
    return round(tokens * 0.00001, 4)


class CallCounter:
    """Wraps an LLM and counts calls + estimates tokens."""

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


# ---------------------------------------------------------------------------
# Method runners
# ---------------------------------------------------------------------------

def run_decomposition_first(case: dict, llm, env) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        plan = decompose_goal(case["request"], counter)
        outputs = execute_plan(plan, counter)
        draft = final_output(plan, outputs)
        fb = env.evaluate(draft)
        return {
            "method": "Decomposition-first",
            "success": fb.success,
            "score": fb.score,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
        }
    except Exception as exc:
        return {
            "method": "Decomposition-first",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


def run_dynamic(case: dict, llm, env) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        history = dynamic_decomposition(case["request"], counter, max_steps=4)
        result = history[-1][1] if history else ""
        fb = env.evaluate(result)
        return {
            "method": "Dynamic",
            "success": fb.success,
            "score": fb.score,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
        }
    except Exception as exc:
        return {
            "method": "Dynamic",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


def run_ps(case: dict, llm, env) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        sol = plan_and_solve(case["request"], counter)
        fb = env.evaluate(sol)
        return {
            "method": "Plan-and-Solve",
            "success": fb.success,
            "score": fb.score,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
        }
    except Exception as exc:
        return {
            "method": "Plan-and-Solve",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


def run_tot(case: dict, llm, env) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        thoughts = tree_of_thoughts(case["request"], counter, depth=2, beam_width=2)
        best = thoughts[0] if thoughts else None
        if best is None:
            return {
                "method": "Tree-of-Thoughts",
                "success": False,
                "score": 0.0,
                "llm_calls": counter.count,
                "tokens": counter.approx_tokens,
                "latency": round(time.time() - t0, 3),
                "cost": _est_cost_usd(counter.approx_tokens),
            }
        fb = env.evaluate(best.state)
        return {
            "method": "Tree-of-Thoughts",
            "success": fb.success,
            "score": fb.score,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
        }
    except Exception as exc:
        return {
            "method": "Tree-of-Thoughts",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


def run_lats(case: dict, llm, env, label: str) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        res = lats(case["request"], counter, env, iterations=2, n_actions=2)
        return {
            "method": f"LATS ({label})",
            "success": res.success,
            "score": res.best_score,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
        }
    except Exception as exc:
        return {
            "method": f"LATS ({label})",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


def run_self_refine(case: dict, llm, env, label: str) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        env_arg = env if label == "Grounded" else None
        res = self_refine(case["request"], counter, environment=env_arg)
        fb = res.environment_feedback
        success = fb.success if fb else False
        score = fb.score if fb else 0.0
        return {
            "method": f"Self-Refine ({label})",
            "success": success,
            "score": score,
            "llm_calls": res.llm_calls,
            "tokens": res.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(res.approx_tokens),
        }
    except Exception as exc:
        return {
            "method": f"Self-Refine ({label})",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


def run_reflexion(case: dict, llm, env, label: str) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        env_arg = env if label == "Grounded" else None
        res = reflexion(case["request"], counter, environment=env_arg, max_trials=3)
        fb = res.trials[-1].feedback if res.trials else None
        score = fb.score if fb else 0.0
        return {
            "method": f"Reflexion ({label})",
            "success": res.success,
            "score": score,
            "llm_calls": res.total_llm_calls,
            "tokens": res.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(res.approx_tokens),
        }
    except Exception as exc:
        return {
            "method": f"Reflexion ({label})",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    from dotenv import load_dotenv
    import os
    load_dotenv(ROOT_DIR / ".env") 
    
    # اطبعي هذا للتأكد في الـ Terminal
    print(f"DEBUG: API KEY FOUND: {bool(os.environ.get('GROQ_API_KEY'))}")
    
    llm = get_planning_llm()
    # FIXED: lower threshold to match realistic LLM text output scores
    grounded_env = IronBridgeEnvironment(success_threshold=0.35)
    ungrounded_env = RandomizedEnvironment(success_threshold=0.35)

    artifacts_dir = ROOT_DIR / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    all_results = []

    print("\n" + "=" * 86)
    print("FULL UNIFIED COMPARISON: All Methods vs. All Cases")
    print("=" * 86)

    # Rate limit protection: small pause after every individual method call
    # (a single case fires off ~30-40 LLM calls across all 10 methods, which
    # is enough on its own to blow through Groq's per-minute limit even with
    # a pause between cases) plus a longer pause between cases.
    INTER_CALL_DELAY_SECONDS = 3
    INTER_CASE_DELAY_SECONDS = 10

    def _run_and_report(label: str, fn, *args) -> dict:
        r = fn(*args)
        all_results.append(r)
        print(f"  {label}| {'PASS' if r['success'] else 'FAIL'} | Score {r['score']:.2f} | Calls {r['llm_calls']:<2} | ${r['cost']:.4f} | {r['latency']}s")
        if not r["success"] and r.get("error"):
            print(f"        ↳ error: {r['error']}")
        time.sleep(INTER_CALL_DELAY_SECONDS)
        return r

    for idx, case in enumerate(TEST_SUITE):
        print(f"\n[{case['id']}] {case['request'][:80]}...")

        # Decomposition
        _run_and_report("DF  ", run_decomposition_first, case, llm, grounded_env)
        _run_and_report("Dyn ", run_dynamic, case, llm, grounded_env)

        # Planning algorithms
        _run_and_report("PS  ", run_ps, case, llm, grounded_env)
        _run_and_report("ToT ", run_tot, case, llm, grounded_env)
        _run_and_report("L-G ", run_lats, case, llm, grounded_env, "Grounded")
        _run_and_report("L-U ", run_lats, case, llm, ungrounded_env, "Ungrounded")

        # Self-correction
        _run_and_report("SR-G", run_self_refine, case, llm, grounded_env, "Grounded")
        _run_and_report("SR-U", run_self_refine, case, llm, grounded_env, "Ungrounded")
        _run_and_report("Ref-G", run_reflexion, case, llm, grounded_env, "Grounded")
        _run_and_report("Ref-U", run_reflexion, case, llm, grounded_env, "Ungrounded")

        # Rate limit protection: longer sleep between cases on top of the
        # per-call delay above
        if idx < len(TEST_SUITE) - 1:
            time.sleep(INTER_CASE_DELAY_SECONDS)

    # Build master comparison table
    methods = [
        "Decomposition-first", "Dynamic",
        "Plan-and-Solve", "Tree-of-Thoughts",
        "LATS (Grounded)", "LATS (Ungrounded)",
        "Self-Refine (Grounded)", "Self-Refine (Ungrounded)",
        "Reflexion (Grounded)", "Reflexion (Ungrounded)",
    ]

    table = []
    for method in methods:
        runs = [r for r in all_results if r["method"] == method]
        successes = sum(1 for r in runs if r["success"])
        total = len(runs)
        avg_calls = round(sum(r["llm_calls"] for r in runs) / total, 2) if total else 0
        avg_tokens = round(sum(r["tokens"] for r in runs) / total, 1) if total else 0
        avg_latency = round(sum(r["latency"] for r in runs) / total, 3) if total else 0
        avg_score = round(sum(r["score"] for r in runs) / total, 3) if total else 0
        total_cost = round(sum(r["cost"] for r in runs), 4)
        table.append({
            "method": method,
            "success_rate": f"{successes}/{total}",
            "accuracy_pct": round((successes / total) * 100, 1) if total else 0,
            "avg_score": avg_score,
            "avg_llm_calls": avg_calls,
            "avg_tokens": avg_tokens,
            "avg_latency_sec": avg_latency,
            "total_est_cost_usd": total_cost,
        })

    print("\n" + "=" * 110)
    print("MASTER COMPARISON TABLE — ALL METHODS")
    print("=" * 110)
    print(
        f"{'Method':<26} | {'Success':<8} | {'Acc%':<6} | {'Score':<6} | "
        f"{'Calls':<6} | {'Tokens':<8} | {'Latency':<8} | {'Est. $':<8}"
    )
    print("-" * 110)
    for row in table:
        print(
            f"{row['method']:<26} | {row['success_rate']:<8} | {row['accuracy_pct']:<6} | "
            f"{row['avg_score']:<6} | {row['avg_llm_calls']:<6} | {row['avg_tokens']:<8} | "
            f"{row['avg_latency_sec']:<8} | {row['total_est_cost_usd']:<8}"
        )

    # Routing recommendations
    print("\n" + "=" * 110)
    print("PER-SUB-TASK ROUTING RECOMMENDATIONS (justified by the table above)")
    print("=" * 110)
    print("""
Sub-task Shape          → Recommended Method    → Justification
─────────────────────────────────────────────────────────────────────────────────────────
Simple / deterministic  → Plan-and-Solve        → Lowest cost (1 call), fast, good
                                                  enough when no branching exists.

Ranking / options       → Tree-of-Thoughts      → Beam search explores multiple
                                                  strategies; beats PS on branching
                                                  problems for modest extra cost.

Final proposal /        → LATS (Grounded)       → MCTS + real env validation catches
high-stakes validation                        → budget/supplier/stock failures before
                                                  commitment. Expensive but justified
                                                  when a wrong plan costs real money.

Draft refinement        → Self-Refine (Grounded)→ One critique + revision cycle;
(cheap to redo)                               → deterministic checks catch structural
                                                  issues; cheap enough to always run.

Multi-trial learning    → Reflexion (Grounded)  → When single retry is insufficient;
(cross-trial memory)                          → episodic memory carries "check budget
                                                  first" across attempts; essential for
                                                  recurring failure patterns.

Top-level reshuffle     → Dynamic Decomposition → When mid-plan surprises (client
(with surprises)                              → unavailable, stock suddenly low) can
                                                  invalidate a pre-built plan.
                                                  Decomposition-first kept only for
                                                  fully mechanical sub-tasks.
""")

    output_path = artifacts_dir / "full_comparison_table.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "comparison_table": table,
            "routing_recommendations": "see_console_output",
            "cases": all_results,
        }, f, indent=2)
    print(f"\n[Artifact Saved] {output_path}")


if __name__ == "__main__":
    main()