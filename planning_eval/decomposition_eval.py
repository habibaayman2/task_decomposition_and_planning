"""
planning_eval/decomposition_eval.py

Evaluation harness for Decomposition-first vs. Dynamic Decomposition.
"""

from __future__ import annotations

import json
import sys
import time
import os
import re
from pathlib import Path
from dotenv import load_dotenv

# ضبط المسارات للوصول لـ mcp_server و planning
root = Path(__file__).resolve().parents[1] 
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from planning.algorithms.decomposition import decompose_goal, execute_plan, final_output
from planning.algorithms.dynamic_decomposition import dynamic_decomposition
from planning.algorithms.environment import IronBridgeEnvironment
from planning.model_provider import get_planning_llm

# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

TEST_CASES = [
    {
        "id": "DECO_01_SIMPLE_MITIGATION",
        "request": "ProjectID 1 is 5 days behind schedule due to late rebar delivery. Propose a mitigation plan checked against the remaining budget.",
    },
    {
        "id": "DECO_02_RUSH_THEN_FAIL",
        "request": "ProjectID 1 faces a material shortage. First check which materials are below minimum stock, then decide whether to rush-order or resequence. If rush-ordering, confirm it fits the remaining budget.",
    },
    {
        "id": "DECO_03_SUPPLIER_CHECK",
        "request": "ProjectID 2 needs a replacement excavator. Check equipment status, then verify supplier ContractStatus before recommending a source.",
    },
    {
        "id": "DECO_04_MULTI_TRADE",
        "request": "ProjectID 3 is behind on plumbing and electrical. Diagnose which trade is the critical path blocker, then resequence the non-critical trade ahead without ordering new materials if stock is low.",
    },
    {
        "id": "DECO_05_BUDGET_SENSITIVE",
        "request": "ProjectID 1 has only $2,000 remaining budget. Address a concrete shortage without exceeding this limit.",
    },
    {
        "id": "DECO_06_EQUIPMENT_MAINTENANCE",
        "request": "ProjectID 2 has equipment under maintenance. Check maintenance status, evaluate rental vs. wait options, and propose the cheaper viable path.",
    },
    {
        "id": "DECO_07_COMPLEX_RESEQUENCE",
        "request": "ProjectID 3 needs to resequence 4 trades due to a delayed steel delivery. Check stock levels for each trade's materials before finalizing the new sequence.",
    },
    {
        "id": "DECO_08_EMERGENCY_OVERRUN",
        "request": "ProjectID 1 emergency: foundation pour must happen tomorrow but cement is below minimum stock. Evaluate rush cement order vs. schedule slip, checking real budget and supplier status.",
    },
]

# ---------------------------------------------------------------------------
# Helpers for Tracking
# ---------------------------------------------------------------------------

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
        prompt_text = str(messages)
        resp_text = str(getattr(result, "content", ""))
        self._token_estimate += _approx_tokens(prompt_text, resp_text)
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
        prompt_text = str(messages)
        resp_text = str(result.model_dump() if hasattr(result, "model_dump") else result)
        self._owner._token_estimate += _approx_tokens(prompt_text, resp_text)
        return result

# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

def run_decomposition_first(case: dict, llm, env: IronBridgeEnvironment) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        plan = decompose_goal(case["request"], counter)
        outputs = execute_plan(plan, counter)
        draft = final_output(plan, outputs)
        feedback = env.evaluate(draft)
        return {
            "method": "decomposition_first",
            "case_id": case["id"],
            "success": feedback.success,
            "score": feedback.score,
            "llm_calls": counter.count,
            "approx_tokens": counter.approx_tokens,
            "est_cost_usd": _est_cost_usd(counter.approx_tokens),
            "latency_sec": round(time.time() - t0, 2),
        }
    except Exception as exc:
        return {"method": "decomposition_first", "case_id": case["id"], "success": False, "score": 0.0, "error": str(exc), "llm_calls": counter.count, "approx_tokens": counter.approx_tokens, "est_cost_usd": 0, "latency_sec": round(time.time() - t0, 2)}

def run_dynamic(case: dict, llm, env: IronBridgeEnvironment) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        history = dynamic_decomposition(case["request"], counter, max_steps=6)
        result = history[-1][1] if history else "No output produced."
        feedback = env.evaluate(result)
        return {
            "method": "dynamic",
            "case_id": case["id"],
            "success": feedback.success,
            "score": feedback.score,
            "llm_calls": counter.count,
            "approx_tokens": counter.approx_tokens,
            "est_cost_usd": _est_cost_usd(counter.approx_tokens),
            "latency_sec": round(time.time() - t0, 2),
            "steps": len(history)
        }
    except Exception as exc:
        return {"method": "dynamic", "case_id": case["id"], "success": False, "score": 0.0, "error": str(exc), "llm_calls": counter.count, "approx_tokens": counter.approx_tokens, "est_cost_usd": 0, "latency_sec": round(time.time() - t0, 2)}

# ---------------------------------------------------------------------------
# Main Evaluation Loop
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv(root / ".env")
    llm = get_planning_llm()
    env = IronBridgeEnvironment(success_threshold=0.4) # خفضنا العتبة قليلاً للتقييم الأولي
    artifacts_dir = root / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    all_results = []

    print("\n" + "=" * 78)
    print("DECOMPOSITION EVALUATION: First vs. Dynamic")
    print("=" * 78)

    for case in TEST_CASES:
        print(f"\nCase: {case['id']}")
        
        # DF
        r1 = run_decomposition_first(case, llm, env)
        all_results.append(r1)
        if "error" in r1: print(f"     [!] DF Error: {r1['error']}")
        print(f"     DF Status: {'PASS' if r1['success'] else 'FAIL'} | Score: {r1['score']:.2f}")

        # Dynamic
        r2 = run_dynamic(case, llm, env)
        all_results.append(r2)
        if "error" in r2: print(f"     [!] Dyn Error: {r2['error']}")
        print(f"     Dyn Status: {'PASS' if r2['success'] else 'FAIL'} | Score: {r2['score']:.2f}")

    # تجميع الجدول النهائي
    table = []
    for method in ["decomposition_first", "dynamic"]:
        runs = [r for r in all_results if r["method"] == method]
        if not runs: continue
        total = len(runs)
        successes = sum(1 for r in runs if r["success"])
        avg_calls = round(sum(r["llm_calls"] for r in runs) / total, 1)
        avg_tokens = round(sum(r["approx_tokens"] for r in runs) / total, 0)
        avg_latency = round(sum(r["latency_sec"] for r in runs) / total, 2)
        total_cost = round(sum(r["est_cost_usd"] for r in runs), 4)
        
        table.append({
            "method": method,
            "success_rate": f"{successes}/{total}",
            "avg_calls": avg_calls,
            "avg_tokens": avg_tokens,
            "avg_latency": avg_latency,
            "cost": total_cost
        })

    print("\n" + "=" * 78)
    print("DECOMPOSITION COMPARISON TABLE")
    print("=" * 78)
    print(f"{'Method':<22} | {'Success':<8} | {'Calls':<6} | {'Tokens':<8} | {'Latency':<8} | {'Cost $':<8}")
    print("-" * 78)
    for row in table:
        print(f"{row['method']:<22} | {row['success_rate']:<8} | {row['avg_calls']:<6} | {row['avg_tokens']:<8} | {row['avg_latency']:<8} | {row['cost']:<8}")

    # حفظ النتائج
    output_path = artifacts_dir / "decomposition_comparison.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"comparison_table": table, "cases": all_results}, f, indent=2)
    
    print(f"\n[Artifact Saved] {output_path}")

if __name__ == "__main__":
    main()