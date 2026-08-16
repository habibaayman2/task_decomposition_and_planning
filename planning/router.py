"""
planning/router.py

Routes each DAG sub-task to the planning algorithm that actually fits its shape.
This is the integration layer between decomposition and planning algorithms.

Routing decisions (justified by the full comparison table in the README):
  • diagnose      → Plan-and-Solve  (deterministic DB lookups, no search needed)
  • rank_options  → Tree-of-Thoughts (compare multiple strategies, needs lookahead)
  • propose_plan  → LATS (validate against real budget/supplier/stock constraints)
  • notify        → Plan-and-Solve (simple text synthesis)
  • draft_*       → Self-Refine (cheap to redo, one critique + revision)
  • retry_*       → Reflexion (multi-trial, episodic memory across attempts)
  • <unknown>     → fallback to Plan-and-Solve

The router is called by the main agent loop after decomposition produces a DAG.
Each task is executed in topological order; prerequisite outputs are passed as
context so downstream tasks can build on upstream results.
"""

from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent
for _p in (_THIS_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from langchain_core.language_models.chat_models import BaseChatModel

from planning_lab.algorithms.plan_and_solve import plan_and_solve
from planning_lab.algorithms.tree_of_thoughts import tree_of_thoughts
from planning_lab.algorithms.lats import lats
from planning_lab.algorithms.self_refine import self_refine
from planning_lab.algorithms.reflexion import reflexion
from planning_lab.algorithms.environment import IronBridgeEnvironment


def _diagnose_executor(instruction: str, context: dict, llm: BaseChatModel) -> str:
    """Deterministic execution for diagnose tasks.

    Uses direct LLM invocation with full context. No search or branching needed
    because the task is a structured lookup (project status, stock levels,
    equipment status) synthesized into a report.
    """
    context_text = "\n\n".join(
        f"OUTPUT FROM {k}:\n{v}" for k, v in context.items()
    ) or "No prerequisite outputs."

    response = llm.invoke([
        ("system", "You are a construction project diagnostician. Use only factual data. Be concise."),
        ("human", f"""Task: {instruction}

Prerequisite outputs:
{context_text}

Produce a factual diagnosis with concrete numbers (budgets, stock counts, equipment status)."""),
    ], temperature=0.1)

    content = response.content
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Diagnose executor received empty response from LLM")
    return content.strip()


def route_subtask(
    task_id: str,
    instruction: str,
    llm: BaseChatModel,
    context: dict,
) -> dict:
    """Route a single DAG sub-task to the algorithm whose shape fits it.

    Args:
        task_id: The task identifier from the decomposition DAG (e.g., "diagnose").
        instruction: The task instruction text.
        llm: The language model instance.
        context: Mapping of prerequisite task_id -> output string.

    Returns:
        A dict with keys:
            - method: str — which algorithm was used
            - output: str — the final output text
            - success: bool — from environment evaluation (if applicable)
            - score: float — from environment evaluation (if applicable)
            - llm_calls: int — approximate call count for cost tracking
    """
    env = IronBridgeEnvironment(success_threshold=0.6)

    # ------------------------------------------------------------------
    # 1. DIAGNOSE — deterministic lookup, no search
    # ------------------------------------------------------------------
    if task_id == "diagnose":
        output = _diagnose_executor(instruction, context, llm)
        fb = env.evaluate(output)
        return {
            "method": "Plan-and-Solve",
            "shape": "deterministic",
            "output": output,
            "success": fb.success,
            "score": fb.score,
            "llm_calls": 1,
        }

    # ------------------------------------------------------------------
    # 2. RANK_OPTIONS — needs lookahead across multiple strategies
    # ------------------------------------------------------------------
    elif task_id == "rank_options":
        candidates = tree_of_thoughts(
            problem=instruction,
            llm=llm,
            depth=2,
            beam_width=2,
        )
        if candidates:
            best = max(candidates, key=lambda c: c.score)
            fb = env.evaluate(best.state)
            return {
                "method": "Tree-of-Thoughts",
                "shape": "ranking",
                "output": best.state,
                "success": fb.success,
                "score": fb.score,
                "llm_calls": 3,  # 1 generate + ~2 eval calls
            }
        return {
            "method": "Tree-of-Thoughts",
            "shape": "ranking",
            "output": "No viable strategies found.",
            "success": False,
            "score": 0.0,
            "llm_calls": 1,
        }

    # ------------------------------------------------------------------
    # 3. PROPOSE_PLAN — high-stakes, must survive real DB validation
    # ------------------------------------------------------------------
    elif task_id == "propose_plan":
        result = lats(
            task=instruction,
            llm=llm,
            environment=env,
            iterations=2,
            n_actions=2,
        )
        return {
            "method": "LATS",
            "shape": "validation",
            "output": result.output,
            "success": result.success,
            "score": result.best_score,
            "llm_calls": 5,  # varies by iterations; approximate
        }

    # ------------------------------------------------------------------
    # 4. NOTIFY — simple synthesis, cheap and fast
    # ------------------------------------------------------------------
    elif task_id == "notify":
        output = plan_and_solve(question=instruction, llm=llm)
        fb = env.evaluate(output)
        return {
            "method": "Plan-and-Solve",
            "shape": "simple",
            "output": output,
            "success": fb.success,
            "score": fb.score,
            "llm_calls": 1,
        }

    # ------------------------------------------------------------------
    # 5. DRAFT_* — cheap to redo, benefits from critique + revision
    # ------------------------------------------------------------------
    elif task_id.startswith("draft_"):
        res = self_refine(goal=instruction, llm=llm, environment=env)
        fb = res.environment_feedback
        return {
            "method": "Self-Refine",
            "shape": "draft",
            "output": res.revised,
            "success": fb.success if fb else False,
            "score": fb.score if fb else 0.0,
            "llm_calls": res.llm_calls,
        }

    # ------------------------------------------------------------------
    # 6. RETRY_* / ADAPT_* — needs multi-trial learning
    # ------------------------------------------------------------------
    elif task_id.startswith(("retry_", "adapt_")):
        res = reflexion(
            task=instruction,
            llm=llm,
            environment=env,
            max_trials=3,
            memory_size=2,
        )
        fb = res.trials[-1].feedback if res.trials else None
        return {
            "method": "Reflexion",
            "shape": "learning",
            "output": res.output,
            "success": res.success,
            "score": fb.score if fb else 0.0,
            "llm_calls": res.total_llm_calls,
        }

    # ------------------------------------------------------------------
    # 7. UNKNOWN — safe fallback
    # ------------------------------------------------------------------
    else:
        output = plan_and_solve(question=instruction, llm=llm)
        fb = env.evaluate(output)
        return {
            "method": "Plan-and-Solve (fallback)",
            "shape": "unknown",
            "output": output,
            "success": fb.success,
            "score": fb.score,
            "llm_calls": 1,
        }


def execute_routed_plan(plan, llm: BaseChatModel) -> dict[str, dict]:
    """Execute a full Plan DAG with routing.

    Iterates through execution_batches (topological generations) and routes
    each task to its fitting algorithm. Prerequisite outputs are accumulated
    and passed as context.

    Args:
        plan: A validated Plan instance.
        llm: The language model.

    Returns:
        Mapping of task_id -> result dict (from route_subtask).
    """
    results: dict[str, dict] = {}

    for batch in plan.execution_batches():
        for task_id in batch:
            task = plan.task(task_id)

            # Build context from dependencies
            context = {
                dep: results[dep]["output"]
                for dep in task.depends_on
                if dep in results
            }

            result = route_subtask(
                task_id=task_id,
                instruction=task.instruction,
                llm=llm,
                context=context,
            )
            results[task_id] = result

    return results