"""
planning/router.py

Routes each DAG sub-task to the planning algorithm that actually fits its shape:
- diagnose    → direct executor (deterministic DB calls, no search needed)
- rank_options → Tree-of-Thoughts (compare multiple strategies)
- propose_plan → LATS (validate against real budget/supplier constraints)
- notify      → Plan-and-Solve (simple text generation)
"""

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent
for _p in (_THIS_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from langchain_core.language_models.chat_models import BaseChatModel

from planning.algorithms.decomposition import DEFAULT_EXECUTORS
from planning.algorithms.plan_and_solve import plan_and_solve
from planning.algorithms.tree_of_thoughts import tree_of_thoughts
from planning.algorithms.lats import lats
from planning.algorithms.environment import IronBridgeEnvironment

def route_subtask(
    task_id: str,
    instruction: str,
    llm: BaseChatModel,
    context: dict,
) -> str:
    """
    Route a single DAG sub-task to the algorithm whose shape fits it.
    Returns the final output string (or best candidate string).
    """
    env = IronBridgeEnvironment()

    if task_id == "diagnose":
        # Deterministic DB lookups — no search or reasoning needed
        return DEFAULT_EXECUTORS["diagnose"](None, context, llm)

    elif task_id == "rank_options":
        # Needs to compare multiple candidate strategies — Tree-of-Thoughts
        candidates = tree_of_thoughts(problem=instruction, llm=llm, depth=2, beam_width=2)
        if candidates:
            best = max(candidates, key=lambda c: c.score)
            return f"Best strategy: {best.state}\nRationale: {best.rationale}"
        return "No viable strategies found."

    elif task_id == "propose_plan":
        # Must survive real budget/supplier checks — LATS with grounded environment
        result = lats(task=instruction, llm=llm, environment=env, iterations=2, n_actions=2)
        return (
            f"LATS result (success={result.success}):\n"
            f"Best plan: {result.output}\n"
            f"Score: {result.best_score}\n"
            f"Iterations: {result.iterations}"
        )

    elif task_id == "notify":
        # Simple synthesis — Plan-and-Solve is sufficient
        return plan_and_solve(question=instruction, llm=llm)

    else:
        # Unknown task — fallback to direct LLM
        response = llm.invoke([
            ("system", "Execute this construction-planning sub-task."),
            ("human", instruction),
        ], temperature=0.2)
        return response.content if isinstance(response.content, str) else str(response.content)