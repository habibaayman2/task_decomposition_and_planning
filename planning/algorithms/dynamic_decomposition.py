from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional

_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent.parent
for _p in (_THIS_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict

# Import the shared executors from decomposition.py (same registry, no duplication)
from .decomposition import DEFAULT_EXECUTORS, _extract_project_id


class DynamicDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    done: bool
    next_task: str


# Keyword -> canonical task id for routing dynamic decisions to real executors
_KNOWN_TASK_KEYWORDS: dict[str, str] = {
    "diagnose": "diagnose",
    "rank": "rank_options",
    "propose": "propose_plan",
    "notify": "notify",
}


def _resolve_task_id(instruction: str) -> Optional[str]:
    """Map a free-text next_task (e.g. 'Rank mitigation options') to a canonical executor id."""
    lowered = instruction.lower()
    for kw, task_id in _KNOWN_TASK_KEYWORDS.items():
        if kw in lowered:
            return task_id
    return None


def dynamic_decomposition(
    goal: str,
    llm: BaseChatModel,
    executors: Optional[dict[str, Callable[[Any, dict, BaseChatModel], str]]] = None,
    max_steps: int = 6,
) -> list[tuple[str, str]]:
    """
    Dynamic / interleaved decomposition for IronBridge.
    
    Step 0 is ALWAYS a real diagnosis (grounded DB call, not an LLM guess).
    Each subsequent step is decided dynamically after observing the previous result.
    """
    executors = executors or DEFAULT_EXECUTORS
    project_id = _extract_project_id(goal)
    context: dict[str, Any] = {"project_id": project_id, "goal": goal}
    history: list[tuple[str, str]] = []

    # -----------------------------------------------------------------------
    # Step 0: Real diagnosis (grounded, not LLM guess)
    # -----------------------------------------------------------------------
    diag_output = executors["diagnose"](None, context, llm)
    history.append(("diagnose", diag_output))
    context["diagnose_result"] = diag_output

    # -----------------------------------------------------------------------
    # Dynamic loop: decide next step based on REAL observations so far
    # -----------------------------------------------------------------------
    for step in range(1, max_steps + 1):
        observation = "\n".join(f"{task}: {result[:300]}" for task, result in history) or "None"
        
        decision = llm.with_structured_output(
            DynamicDecision,
            method="json_schema",
        ).invoke([
            ("system", (
                "You are IronBridge's dynamic delay-response planner. "
                "You see only what has actually happened so far — no future step is fixed. "
                "Decide whether the delay-risk request is now fully resolved (done=true) "
                "or propose exactly one next concrete sub-task grounded in the observed results. "
                "Change course if an earlier observation makes the expected next step irrelevant "
                "(e.g. skip ranking material-shortage strategies if diagnosis shows no shortage)."
            )),
            ("human", f"""Goal: {goal}
Completed work and observations:
{observation}

Decide the single best next task. Set done to true only when the goal is met.
When done is true, use an empty string for next_task."""),
        ], temperature=0.3)

        if decision.done or not decision.next_task.strip():
            break

        task_text = decision.next_task.strip()
        task_id = _resolve_task_id(task_text)

        # -------------------------------------------------------------------
        # Execute: real executor if known, LLM fallback if novel
        # -------------------------------------------------------------------
        if task_id and task_id in executors:
            # Real IronBridge executor (DB call, grounded)
            result = executors[task_id](None, context, llm)
            history.append((task_text, result))
            context[f"{task_id}_result"] = result
        else:
            # Unknown task: LLM fallback
            response = llm.invoke([
                ("system", "Execute the next adaptive sub-task using the observations provided."),
                ("human", f"Goal: {goal}\nNext task: {task_text}\nPrior observations:\n{observation}"),
            ], temperature=0.2)
            result = response.content
            if not isinstance(result, str) or not result.strip():
                raise RuntimeError("The chat model returned an empty or unsupported response")
            result = result.strip()
            history.append((task_text, result))

    return history