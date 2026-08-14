from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent.parent
for _p in (_THIS_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict

from .decomposition import DEFAULT_EXECUTORS, _extract_project_id


class DynamicDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    done: bool
    next_task: str


_KNOWN_TASK_KEYWORDS: dict[str, str] = {
    "diagnose": "diagnose",
    "rank": "rank_options",
    "propose": "propose_plan",
    "notify": "notify",
}


def _resolve_task_id(instruction: str) -> Optional[str]:
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
    executors = executors or DEFAULT_EXECUTORS
    project_id = _extract_project_id(goal)
    context: dict[str, Any] = {"project_id": project_id, "goal": goal}
    history: list[tuple[str, str]] = []

    # Step 0: Real diagnosis
    diag_output = executors["diagnose"](None, context, llm)
    history.append(("diagnose", diag_output))
    context["diagnose_result"] = diag_output

    # Dynamic loop
    for step in range(1, max_steps + 1):
        observation = "\n".join(f"{task}: {result[:300]}" for task, result in history) or "None"
        
        # Retry loop for Groq XML tag issues
        max_retries = 2
        last_err = None
        base_messages = [
            ("system", (
                "You are IronBridge's dynamic delay-response planner. "
                "You see only what has actually happened so far — no future step is fixed. "
                "Decide whether the delay-risk request is now fully resolved (done=true) "
                "or propose exactly one next concrete sub-task grounded in the observed results. "
                "Change course if an earlier observation makes the expected next step irrelevant."
            )),
            ("human", f"""Goal: {goal}
Completed work and observations:
{observation}

Decide the single best next task. Set done to true only when the goal is met.
When done is true, use an empty string for next_task.
Respond with ONLY the JSON fields defined by the schema — no extra keys, no explanation."""),
        ]
        
        for attempt in range(max_retries + 1):
            try:
                structured = llm.with_structured_output(DynamicDecision, method="function_calling")
                decision = structured.invoke(base_messages, temperature=0.3)
                break
            except Exception as e:
                last_err = e
                base_messages.append((
                    "human",
                    "Your previous response was malformed. Respond again with ONLY the fields "
                    "defined by the schema — no extra keys, no explanation text."
                ))
                if attempt < max_retries:
                    time.sleep(0.5 * (attempt + 1))
        else:
            raise last_err

        if decision.done or not decision.next_task.strip():
            break

        task_text = decision.next_task.strip()
        task_id = _resolve_task_id(task_text)

        if task_id and task_id in executors:
            result = executors[task_id](None, context, llm)
            history.append((task_text, result))
            context[f"{task_id}_result"] = result
        else:
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