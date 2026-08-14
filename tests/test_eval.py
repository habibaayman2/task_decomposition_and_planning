from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Resolve project root for cross-module imports
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from planning.environment import IronBridgeEnvironment
from planning.model_provider import get_planning_llm
from planning.plan_and_solve import plan_and_solve, PlanAndSolveError
from planning.tree_of_thoughts import tree_of_thoughts
from planning.lats import lats, LATSResult, flatten_lats_tree, LATSNode

# Global lock to protect non-thread-safe LLM clients during parallel evaluation
_LLM_GLOBAL_LOCK = threading.Lock()


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    """Safely get an attribute from an object or a key from a dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass
class CallCounter:
    """Thread-safe LLM call counter with a global invocation lock."""
    llm: Any
    count: int = field(default=0, init=False)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def invoke(self, *args, **kwargs):
        with _LLM_GLOBAL_LOCK:
            with self._lock:
                self.count += 1
            return self.llm.invoke(*args, **kwargs)

    def with_structured_output(self, *args, **kwargs):
        inner = self.llm.with_structured_output(*args, **kwargs)
        return _StructuredOutputWrapper(inner, self)


class _StructuredOutputWrapper:
    """Thread-safe wrapper for structured-output invocations."""

    def __init__(self, inner: Any, counter: CallCounter):
        self._inner = inner
        self._counter = counter

    def invoke(self, *args, **kwargs):
        with _LLM_GLOBAL_LOCK:
            with self._counter._lock:
                self._counter.count += 1
            return self._inner.invoke(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# Test-case tasks extracted from test.py for algorithm benchmarking
# ---------------------------------------------------------------------------
TEST_SUITE_TASKS: list[str] = [
    "ProjectID 1: Mitigate delay risk by analyzing schedule dependencies.",
    "ProjectID 1: Material shortage requires trade resequencing options.",
    "ProjectID 1: Address material shortage with a rush order within budget.",
    "ProjectID 1: Address material shortage.",
    "ProjectID 1: Place a rush order with premium cost $999999 to expedite the missing material.",
    "ProjectID 1: Resequence unaffected trades ahead of the blocked item so the crew stays productive while the blocking issue is resolved.",
    "ProjectID 1: Rush order $5000.",
    "ProjectID 1: Resequence trades.",
    "ProjectID 1: Switch to steel yard supplier.",
    "ProjectID 1: Rush order $1000.",
]


class PureAlgorithmEvaluator:
    """Evaluates pure planning algorithms with parallel execution and statistical analysis."""

    def __init__(self, success_threshold: float = 0.35):
        self.env = IronBridgeEnvironment(success_threshold=success_threshold)
        self.base_llm = get_planning_llm()
        self.llm_provider = "Groq (ChatGroq)" if os.getenv("GROQ_API_KEY") else "Deterministic Fallback"
        self.artifacts_dir = _PROJECT_ROOT / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def run_pas(self, task: str) -> dict:
        counter = CallCounter(self.base_llm)
        t0 = time.time()
        try:
            sol = plan_and_solve(question=task, llm=counter)
            sol_str = str(sol) if not isinstance(sol, str) else sol
            fb = self.env.evaluate(sol_str)
            return {
                "method": "Plan-and-Solve",
                "success": _get_attr(fb, "success", False),
                "score": _get_attr(fb, "score", 0.0),
                "calls": counter.count,
                "latency_sec": round(time.time() - t0, 3),
                "output_preview": sol_str[:200] + "..." if len(sol_str) > 200 else sol_str,
            }
        except Exception as e:
            return {
                "method": "Plan-and-Solve",
                "success": False,
                "score": 0.0,
                "calls": counter.count,
                "latency_sec": round(time.time() - t0, 3),
                "error": f"{type(e).__name__}: {str(e)}",
            }

    def run_tot(self, task: str) -> dict:
        counter = CallCounter(self.base_llm)
        t0 = time.time()
        try:
            thoughts = tree_of_thoughts(problem=task, llm=counter, depth=2, beam_width=2)

            if not thoughts:
                return {
                    "method": "Tree-of-Thoughts",
                    "success": False,
                    "score": 0.0,
                    "calls": counter.count,
                    "latency_sec": round(time.time() - t0, 3),
                    "error": "No thoughts generated",
                }

            best_state = ""
            best_fb_score = -float("inf")
            best_success = False
            best_details: list[str] = []

            for t in thoughts:
                state = _get_attr(t, "state", str(t))
                fb = self.env.evaluate(state)
                score = _get_attr(fb, "score", 0.0)
                if score > best_fb_score:
                    best_fb_score = score
                    best_state = state
                    best_success = _get_attr(fb, "success", False)
                    best_details = _get_attr(fb, "details", []) or []

            return {
                "method": "Tree-of-Thoughts",
                "success": best_success,
                "score": best_fb_score,
                "calls": counter.count,
                "latency_sec": round(time.time() - t0, 3),
                "output_preview": best_state[:200] + "..." if len(best_state) > 200 else best_state,
                "environment_details": best_details,
            }
        except Exception as e:
            return {
                "method": "Tree-of-Thoughts",
                "success": False,
                "score": 0.0,
                "calls": counter.count,
                "latency_sec": round(time.time() - t0, 3),
                "error": f"{type(e).__name__}: {str(e)}",
            }

    def run_lats(self, task: str) -> dict:
        counter = CallCounter(self.base_llm)
        t0 = time.time()
        try:
            res = lats(task=task, llm=counter, environment=self.env, iterations=2, n_actions=2)
            output = _get_attr(res, "output", "")
            output_str = str(output) if not isinstance(output, str) else output
            return {
                "method": "LATS (Grounded)",
                "success": _get_attr(res, "success", False),
                "score": _get_attr(res, "best_score", 0.0),
                "calls": counter.count,
                "latency_sec": round(time.time() - t0, 3),
                "output_preview": output_str[:200] + "..." if len(output_str) > 200 else output_str,
                "iterations_used": _get_attr(res, "iterations", 0),
            }
        except Exception as e:
            return {
                "method": "LATS (Grounded)",
                "success": False,
                "score": 0.0,
                "calls": counter.count,
                "latency_sec": round(time.time() - t0, 3),
                "error": f"{type(e).__name__}: {str(e)}",
            }

    def benchmark(self, tasks: list[str], parallel: bool = True):
        summary = {
            "Plan-and-Solve": {"success": 0, "calls": 0, "score": 0.0, "latency": 0.0, "total": 0, "scores": []},
            "Tree-of-Thoughts": {"success": 0, "calls": 0, "score": 0.0, "latency": 0.0, "total": 0, "scores": []},
            "LATS (Grounded)": {"success": 0, "calls": 0, "score": 0.0, "latency": 0.0, "total": 0, "scores": []},
        }
        detailed_runs: list[dict] = []

        print("\n" + "=" * 80)
        print(f"PURE PLANNING ALGORITHM BENCHMARK (Provider: {self.llm_provider})")
        print("=" * 80)

        for idx, task in enumerate(tasks, 1):
            print(f"\n[Task {idx}] {task}")

            if parallel:
                methods = {
                    "pas": self.run_pas,
                    "tot": self.run_tot,
                    "lats": self.run_lats,
                }
                case_evals: list[dict] = []
                with ThreadPoolExecutor(max_workers=3) as pool:
                    futures = {pool.submit(fn, task): name for name, fn in methods.items()}
                    for future in as_completed(futures):
                        case_evals.append(future.result())
                order = {"Plan-and-Solve": 0, "Tree-of-Thoughts": 1, "LATS (Grounded)": 2}
                case_evals.sort(key=lambda x: order.get(x["method"], 99))
            else:
                case_evals = [
                    self.run_pas(task),
                    self.run_tot(task),
                    self.run_lats(task),
                ]

            detailed_runs.append({"case_id": idx, "task": task, "results": case_evals})

            for res in case_evals:
                m = res["method"]
                summary[m]["total"] += 1
                if res["success"]:
                    summary[m]["success"] += 1
                summary[m]["calls"] += res["calls"]
                summary[m]["score"] += res["score"]
                summary[m]["latency"] += res["latency_sec"]
                summary[m]["scores"].append(res["score"])

                status = "PASS" if res["success"] else "FAIL"
                print(f"  \u2514\u2500 {m:<18} | Status: {status:<4} | Score: {res['score']:.2f} | Calls: {res['calls']} | Time: {res['latency_sec']}s")

        comparison_matrix = {}
        for method, data in summary.items():
            n = data["total"]
            if n == 0:
                comparison_matrix[method] = {
                    "success_rate": "0/0",
                    "accuracy_percent": 0.0,
                    "avg_llm_calls": 0.0,
                    "avg_score": 0.0,
                    "score_variance": 0.0,
                    "score_std": 0.0,
                    "avg_latency_sec": 0.0,
                }
                continue

            scores = data["scores"]
            mean_score = sum(scores) / n
            variance = sum((s - mean_score) ** 2 for s in scores) / n if n > 1 else 0.0
            std = variance ** 0.5
            comparison_matrix[method] = {
                "success_rate": f"{data['success']}/{n}",
                "accuracy_percent": round((data["success"] / n) * 100, 2),
                "avg_llm_calls": round(data["calls"] / n, 2),
                "avg_score": round(mean_score, 4),
                "score_variance": round(variance, 6),
                "score_std": round(std, 4),
                "avg_latency_sec": round(data["latency"] / n, 3),
            }
            if mean_score != 0:
                comparison_matrix[method]["consistency_cv"] = round(std / abs(mean_score), 4)

        output_path = self.artifacts_dir / "algorithm_comparison.json"
        export_payload = {
            "provider": self.llm_provider,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "parallel_execution": parallel,
            "comparison_matrix": comparison_matrix,
            "detailed_runs": detailed_runs,
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(export_payload, f, indent=2)

        print(f"\n[Artifact Saved] Algorithm comparison matrix saved to: {output_path}")
        return export_payload


# ---------------------------------------------------------------------------
# Unit tests (merged from test.py)
# ---------------------------------------------------------------------------
class TestGroqPlanningAlgorithms(unittest.TestCase):
    """Direct unit tests for pure planning algorithms using Groq LLM or deterministic fallback."""

    @classmethod
    def setUpClass(cls):
        cls.test_records: list[dict] = []
        cls.artifacts_dir = _PROJECT_ROOT / "artifacts"
        cls.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.llm = get_planning_llm()
        self.env = IronBridgeEnvironment(success_threshold=0.6)
        self.start_time = time.time()

    def tearDown(self):
        duration = round(time.time() - self.start_time, 4)
        result = self._outcome.result if hasattr(self, "_outcome") else None
        error_msg = None
        if result:
            for test, trace in result.failures + result.errors:
                if test is self:
                    error_msg = trace.split("\n")[-1] if trace else "Unknown error"
                    break

        outcome = "FAILED" if error_msg else "PASSED"
        self.__class__.test_records.append({
            "test_name": self._testMethodName,
            "status": outcome,
            "duration_sec": duration,
            "error": error_msg,
        })

    @classmethod
    def tearDownClass(cls):
        output_file = cls.artifacts_dir / "unit_test_results.json"
        report = {
            "summary": {
                "total_tests": len(cls.test_records),
                "passed": sum(1 for t in cls.test_records if t["status"] == "PASSED"),
                "failed": sum(1 for t in cls.test_records if t["status"] == "FAILED"),
            },
            "tests": cls.test_records,
        }
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"\n[Artifact Saved] Test results saved to: {output_file}")

    # ------------------------------------------------------------------
    # 1. Plan-and-Solve
    # ------------------------------------------------------------------
    def test_plan_and_solve_execution(self):
        question = "ProjectID 1: Mitigate delay risk by analyzing schedule dependencies."
        solution = plan_and_solve(question=question, llm=self.llm)
        self.assertIsInstance(solution, str)
        self.assertGreater(len(solution.strip()), 0)
        lowered = solution.lower()
        self.assertTrue(
            "project" in lowered or "mitigat" in lowered or "plan" in lowered,
            f"Solution lacks expected keywords. Got: {solution[:200]}",
        )

    def test_plan_and_solve_empty_response_error(self):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "   "
        mock_llm.invoke.return_value = mock_response
        with self.assertRaises(PlanAndSolveError):
            plan_and_solve(question="Test question", llm=mock_llm, max_retries=1)

    def test_plan_and_solve_retry_on_failure(self):
        mock_llm = MagicMock()
        bad_response = MagicMock()
        bad_response.content = "   "
        good_response = MagicMock()
        good_response.content = "Valid plan after retry."
        mock_llm.invoke.side_effect = [bad_response, good_response]
        result = plan_and_solve(question="Test", llm=mock_llm, max_retries=2, retry_delay_sec=0.01)
        self.assertEqual(result, "Valid plan after retry.")
        self.assertEqual(mock_llm.invoke.call_count, 2)

    # ------------------------------------------------------------------
    # 2. Tree-of-Thoughts
    # ------------------------------------------------------------------
    def test_tree_of_thoughts_execution(self):
        problem = "ProjectID 1: Material shortage requires trade resequencing options."
        thoughts = tree_of_thoughts(problem=problem, llm=self.llm, depth=2, beam_width=2)
        self.assertIsInstance(thoughts, list)
        self.assertGreater(len(thoughts), 0)
        best = thoughts[0]
        self.assertGreater(len(best.state.strip()), 10, "Best thought state is too short")
        self.assertGreaterEqual(best.score, 0.0)
        self.assertLessEqual(best.score, 1.0)

    def test_tree_of_thoughts_batch_evaluation(self):
        problem = "ProjectID 1: Simple task."
        thoughts = tree_of_thoughts(problem=problem, llm=self.llm, depth=1, beam_width=1, batch_evaluate=True)
        self.assertIsInstance(thoughts, list)

    def test_tree_of_thoughts_invalid_params(self):
        with self.assertRaises(ValueError):
            tree_of_thoughts(problem="test", llm=self.llm, depth=0, beam_width=1)
        with self.assertRaises(ValueError):
            tree_of_thoughts(problem="test", llm=self.llm, depth=1, beam_width=0)

    # ------------------------------------------------------------------
    # 3. LATS Search
    # ------------------------------------------------------------------
    def test_lats_execution(self):
        task = "ProjectID 1: Address material shortage with a rush order within budget."
        result = lats(task=task, llm=self.llm, environment=self.env, iterations=2, n_actions=2)
        self.assertIsInstance(result, LATSResult)
        self.assertIsInstance(result.success, bool)
        self.assertGreater(len(result.output.strip()), 0)
        lowered = result.output.lower()
        self.assertTrue(
            "project" in lowered or "budget" in lowered or "order" in lowered,
            f"LATS output lacks expected keywords. Got: {result.output[:200]}",
        )
        self.assertGreaterEqual(result.best_score, 0.0)
        self.assertLessEqual(result.best_score, 1.0)
        self.assertGreaterEqual(result.iterations, 1)

    def test_lats_with_pruning(self):
        task = "ProjectID 1: Address material shortage."
        result = lats(
            task=task, llm=self.llm, environment=self.env,
            iterations=2, n_actions=2, prune_threshold=0.9,
        )
        self.assertIsInstance(result, LATSResult)
        self.assertGreaterEqual(result.pruned_count, 0)

    def test_lats_tree_flattening(self):
        root = LATSNode(state="root")
        child1 = LATSNode(state="child1", parent=root)
        child2 = LATSNode(state="child2", parent=root)
        grandchild = LATSNode(state="grandchild", parent=child1)
        root.children = [child1, child2]
        child1.children = [grandchild]

        flat = flatten_lats_tree(root)
        self.assertEqual(len(flat), 4)
        by_id = {r["id"]: r for r in flat}
        self.assertIsNone(by_id["n0"]["parent_id"])
        self.assertEqual(by_id["n1"]["parent_id"], "n0")
        self.assertEqual(by_id["n2"]["parent_id"], "n0")
        self.assertEqual(by_id["n3"]["parent_id"], "n1")

    def test_lats_invalid_params(self):
        with self.assertRaises(ValueError):
            lats(task="test", llm=self.llm, environment=self.env, iterations=0, n_actions=1)
        with self.assertRaises(ValueError):
            lats(task="test", llm=self.llm, environment=self.env, iterations=1, n_actions=0)
        with self.assertRaises(ValueError):
            lats(task="test", llm=self.llm, environment=self.env, iterations=1, n_actions=1, max_depth=0)

    # ------------------------------------------------------------------
    # 4. Environment Grounding
    # ------------------------------------------------------------------
    def test_environment_rejects_over_budget_rush(self):
        fake_state = (
            "ProjectID 1: Place a rush order with premium cost $999999 "
            "to expedite the missing material."
        )
        fb = self.env.evaluate(fake_state)
        self.assertFalse(fb.success, "Environment should reject over-budget rush order")
        self.assertLess(fb.score, 0.5, "Score should be low for over-budget proposal")
        self.assertTrue(
            any("exceeds" in d.lower() or "budget" in d.lower() for d in fb.details),
            f"Details should mention budget issue. Got: {fb.details}",
        )

    def test_environment_accepts_in_budget_plan(self):
        fake_state = (
            "ProjectID 1: Resequence unaffected trades ahead of the blocked item "
            "so the crew stays productive while the blocking issue is resolved."
        )
        fb = self.env.evaluate(fake_state)
        budget_failures = [d for d in fb.details if "budget" in d.lower() and "exceeds" in d.lower()]
        self.assertEqual(len(budget_failures), 0, "Resequencing should not trigger budget failures")

    def test_environment_score_bounds(self):
        test_states = [
            "ProjectID 1: Rush order $5000.",
            "ProjectID 1: Resequence trades.",
            "ProjectID 1: Switch to steel yard supplier.",
        ]
        for state in test_states:
            fb = self.env.evaluate(state)
            self.assertGreaterEqual(fb.score, 0.0, f"Score below 0 for: {state}")
            self.assertLessEqual(fb.score, 1.0, f"Score above 1 for: {state}")

    def test_environment_cache(self):
        state = "ProjectID 1: Rush order $1000."
        t0 = time.time()
        for _ in range(10):
            self.env.evaluate(state)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 5.0, "Environment evaluation is unexpectedly slow; cache may not be working")


# ---------------------------------------------------------------------------
# Unified runner: executes both benchmark and unit tests, then merges reports
# ---------------------------------------------------------------------------
def run_merged_evaluation(parallel: bool = True):
    """Run the full merged evaluation suite."""
    artifacts_dir = _PROJECT_ROOT / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # 1. Run algorithm benchmark on test-suite tasks
    print("\n" + "#" * 80)
    print("# PHASE 1: ALGORITHM BENCHMARK (test-suite tasks)")
    print("#" * 80)
    evaluator = PureAlgorithmEvaluator(success_threshold=0.35)
    benchmark_result = evaluator.benchmark(TEST_SUITE_TASKS, parallel=parallel)

    # 2. Run unit tests
    print("\n" + "#" * 80)
    print("# PHASE 2: UNIT TESTS")
    print("#" * 80)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestGroqPlanningAlgorithms)
    runner = unittest.TextTestRunner(verbosity=2)
    test_result = runner.run(suite)

    # 3. Merge reports
    merged_report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "llm_provider": evaluator.llm_provider,
        "parallel_execution": parallel,
        "benchmark": benchmark_result,
        "unit_tests": {
            "total": test_result.testsRun,
            "failures": len(test_result.failures),
            "errors": len(test_result.errors),
            "skipped": len(test_result.skipped),
            "success": test_result.wasSuccessful(),
        },
    }

    merged_path = artifacts_dir / "merged_evaluation_report.json"
    with open(merged_path, "w", encoding="utf-8") as f:
        json.dump(merged_report, f, indent=2)

    print("\n" + "=" * 80)
    print(f"MERGED EVALUATION COMPLETE")
    print("=" * 80)
    print(f"Benchmark artifact:   {artifacts_dir / 'algorithm_comparison.json'}")
    print(f"Unit-test artifact:   {artifacts_dir / 'unit_test_results.json'}")
    print(f"Merged report:        {merged_path}")
    print("=" * 80)
    return merged_report


if __name__ == "__main__":
    run_merged_evaluation(parallel=True)