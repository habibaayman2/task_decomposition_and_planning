"""
tests/test_router.py — Smoke test for planning/router.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from planning.model_provider import get_planning_llm
from planning.router import route_subtask


def test_router():
    llm = get_planning_llm()
    context = {"project_id": 1, "goal": "Test delay risk"}

    test_cases = [
        ("diagnose", "Diagnose root cause for Project 1"),
        ("rank_options", "Rank mitigation strategies for Project 1"),
        ("propose_plan", "Propose final plan for Project 1"),
        ("notify", "Draft notification for site engineer"),
    ]

    print("=" * 60)
    print("ROUTER SMOKE TEST")
    print("=" * 60)

    all_passed = True
    for task_id, instruction in test_cases:
        print(f"\n[{task_id}] {instruction}")
        try:
            result = route_subtask(task_id, instruction, llm, context)
            assert isinstance(result, dict), "route_subtask must return a dict"
            assert "output" in result and "method" in result, "missing expected keys"
            print(f"  Status: OK  | method={result['method']}")
            print(f"  Output preview: {result['output'][:120]}...")
        except Exception as e:
            print(f"  Status: FAIL ❌")
            print(f"  Error: {type(e).__name__}: {e}")
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("ALL TESTS PASSED ✅")
    else:
        print("SOME TESTS FAILED ❌")
    print("=" * 60)

    assert all_passed, "One or more router smoke-test cases failed — see output above"


if __name__ == "__main__":
    test_router()
