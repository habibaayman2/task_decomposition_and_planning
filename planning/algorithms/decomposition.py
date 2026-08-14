from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional

# Path resolution for mcp_server imports
_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent.parent
for _p in (_THIS_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict

from ..models import Plan, Task

# ---------------------------------------------------------------------------
# IronBridge real executors — grounded in mcp_server/db.py, not LLM guesses.
# These are the SAME executors used by dynamic_decomposition.py so the
# "real work" is defined exactly once.
# ---------------------------------------------------------------------------

def _extract_project_id(text: str) -> Optional[int]:
    m = re.search(r"[Pp]roject(?:ID)?\s*(\d+)", text)
    return int(m.group(1)) if m else None


def _exec_diagnose(task: Task | None, context: dict, llm: BaseChatModel) -> str:
    from mcp_server import db
    project_id = context.get("project_id")
    project = db.get_project(project_id) if project_id else None
    low_stock = [m for m in db.find_materials(None, None) if m["QuantityAvailable"] < m["MinimumStockLevel"]]
    equipment_issues = [e for e in db.equipment_status(None, None) if e["Availability"] == "Under Maintenance"]

    lines = []
    if project:
        lines.append(
            f"Project {project_id} ({project['ProjectName']}): remaining budget "
            f"${project['RemainingBudget']:,.2f} of ${project['Budget']:,.2f}, status={project['Status']}."
        )
    else:
        lines.append(f"Project {project_id}: not found in database.")
    if low_stock:
        names = ", ".join(f"{m['MaterialName']} ({m['QuantityAvailable']}/{m['MinimumStockLevel']} min)" for m in low_stock)
        lines.append(f"Materials currently below minimum stock: {names}.")
    else:
        lines.append("No materials currently below minimum stock.")
    if equipment_issues:
        names = ", ".join(f"{e['EquipmentName']} ({e['MaintenanceStatus']})" for e in equipment_issues)
        lines.append(f"Equipment under maintenance: {names}.")
    else:
        lines.append("No equipment currently under maintenance.")

    context["low_stock"] = low_stock
    context["equipment_issues"] = equipment_issues
    context["project"] = project
    return "Diagnosis: " + " ".join(lines)


def _exec_rank_options(task: Task | None, context: dict, llm: BaseChatModel) -> str:
    from planning.model_provider import RESPONSE_STRATEGIES, _deterministic_score_for_text
    diagnosis = context.get("diagnose_result", "")
    ranked = sorted(RESPONSE_STRATEGIES, key=lambda s: _deterministic_score_for_text(s["text"]), reverse=True)
    context["ranked_strategies"] = ranked
    lines = [f"{i + 1}. {s['text']}" for i, s in enumerate(ranked)]
    return f"Ranked mitigation strategies given diagnosis ({diagnosis[:80]}...):\n" + "\n".join(lines)


def _exec_propose_plan(task: Task | None, context: dict, llm: BaseChatModel) -> str:
    from mcp_server import db
    project_id = context.get("project_id")
    project = context.get("project") or db.get_project(project_id)
    remaining = project["RemainingBudget"] if project else 0.0
    ranked = context.get("ranked_strategies") or []
    best = ranked[0] if ranked else {"text": "no strategy available"}
    proposal = (
        f"Proposed plan for Project {project_id}: {best['text']} "
        f"Remaining budget on record: ${remaining:,.2f} -- this proposal must be "
        f"checked against that figure before approval."
    )
    context["proposal"] = proposal
    return proposal


def _exec_notify(task: Task | None, context: dict, llm: BaseChatModel) -> str:
    project_id = context.get("project_id")
    proposal = context.get("proposal", "")
    return f"Notification for Project {project_id}: a delay risk was identified. Recommended mitigation: {proposal}"


# Registry keyed by canonical task id. Shared with dynamic_decomposition.py.
DEFAULT_EXECUTORS: dict[str, Callable[[Task | None, dict, BaseChatModel], str]] = {
    "diagnose": _exec_diagnose,
    "rank_options": _exec_rank_options,
    "propose_plan": _exec_propose_plan,
    "notify": _exec_notify,
}


# ---------------------------------------------------------------------------
# Toolkit wire schemas (kept for structured output with the LLM)
# ---------------------------------------------------------------------------

class PlannedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    instruction: str
    depends_on: list[str]


class GeneratedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    tasks: list[PlannedTask]


PLANNER_SYSTEM = """You are IronBridge's delay-response task-decomposition planner.
Produce a small executable DAG, not a prose checklist. Every task must make a concrete
contribution to resolving a construction delay risk. The plan must end with exactly
one synthesis/notification task depending on every necessary branch.
Use task ids: diagnose, rank_options, propose_plan, notify unless the request
genuinely needs different tasks. Do not invent steps that only a person could judge."""


# ---------------------------------------------------------------------------
# Decomposition-first: one LLM call generates the full DAG, then execute
# in topological order using real executors where available.
# ---------------------------------------------------------------------------

def decompose_goal(
    goal: str,
    llm: BaseChatModel,
    project_id: Optional[int] = None,
) -> Plan:
    if project_id is None:
        project_id = _extract_project_id(goal)

    generated = llm.with_structured_output(
        GeneratedPlan,
        method="json_schema",
    ).invoke([
        ("system", PLANNER_SYSTEM),
        ("human", f"""Decompose this delay-risk request into 3-6 tasks: {goal!r}
Use short task ids such as diagnose, rank_options, propose_plan, notify.
Dependencies may refer only to tasks in the plan.
Preserve the supplied goal exactly in the plan's goal field."""),
    ], temperature=0.1)

    payload = generated.model_dump()
    payload["goal"] = goal
    return Plan.model_validate(payload)


def execute_plan(
    plan: Plan,
    llm: BaseChatModel,
    executors: Optional[dict[str, Callable[[Task | None, dict, BaseChatModel], str]]] = None,
    max_workers: int = 4,
) -> dict[str, str]:
    executors = executors or DEFAULT_EXECUTORS
    outputs: dict[str, str] = {}
    context: dict[str, Any] = {"project_id": _extract_project_id(plan.goal)}

    for batch in plan.execution_batches():
        # Separate real-executor tasks from LLM-fallback tasks
        executor_tasks: dict[str, Task] = {}
        llm_tasks: dict[str, str] = {}

        for task_id in batch:
            task = plan.task(task_id)
            if task_id in executors:
                executor_tasks[task_id] = task
            else:
                # Fallback: build LLM prompt for unknown task ids
                prereq = "\n\n".join(
                    f"OUTPUT FROM {dep}:\n{outputs[dep]}"
                    for dep in task.depends_on
                ) or "No prerequisite outputs."
                llm_tasks[task_id] = (
                    f"Overall goal: {plan.goal}\n"
                    f"Current task: {task.instruction}\n"
                    f"Prerequisite outputs:\n{prereq}\n"
                    "Complete only the current task. Be concrete and concise. Do not invent sources."
                )

        # Run real executors (DB calls — fast, no LLM needed)
        for task_id, task in executor_tasks.items():
            output = executors[task_id](task, context, llm)
            outputs[task_id] = output
            context[f"{task_id}_result"] = output

        # Run LLM fallback for unknown tasks (parallel)
        if llm_tasks:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(llm_tasks))) as pool:
                futures = {
                    pool.submit(
                        llm.invoke,
                        [
                            ("system", "You execute one node in a validated task DAG."),
                            ("human", prompt),
                        ],
                        temperature=0.2,
                    ): task_id
                    for task_id, prompt in llm_tasks.items()
                }
                for future in as_completed(futures):
                    content = future.result().content
                    if not isinstance(content, str) or not content.strip():
                        raise RuntimeError("The chat model returned an empty or unsupported response")
                    task_id = futures[future]
                    outputs[task_id] = content.strip()
                    context[f"{task_id}_result"] = content.strip()

    return outputs


def final_output(plan: Plan, outputs: dict[str, str]) -> str:
    terminals = plan.terminal_tasks()
    if len(terminals) != 1:
        raise ValueError(f"Expected exactly one terminal synthesis task, found {terminals}")
    return outputs[terminals[0]]