from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional

_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent.parent
for _p in (_THIS_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict

from ..models import Plan, Task


def _extract_project_id(text: str) -> Optional[int]:
    m = re.search(r"[Pp]roject(?:ID)?\s*(\d+)", text)
    return int(m.group(1)) if m else None


def _exec_diagnose(task: Task | None, context: dict, llm: BaseChatModel) -> str:
    try:
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
 
    except ImportError:
        return "Error: Could not import mcp_server.db. Check your PYTHONPATH."

def _exec_rank_options(task: Task | None, context: dict, llm: BaseChatModel) -> str:
    from planning.model_provider import RESPONSE_STRATEGIES
    diagnosis = context.get("diagnose_result", "")
    project = context.get("project")
    remaining = project["RemainingBudget"] if project else 0.0
    low_stock = context.get("low_stock", [])

    strategies_text = "\n".join(f"- {s['name']}: {s['text']}" for s in RESPONSE_STRATEGIES)

    # Build constraint hint based on real data
    constraint_hint = ""
    if low_stock:
        names = ", ".join(m["MaterialName"] for m in low_stock)
        constraint_hint += f"\nNOTE: Materials below minimum stock: {names}. If you choose schedule_resequence, you MUST also plan to order these materials."

    response = llm.invoke([
        ("system", "You are IronBridge's strategy ranker. Rank based on the actual diagnosis and remaining budget."),
        ("human", f"""Project diagnosis:
{diagnosis}

Remaining budget: ${remaining:,.2f}

Available strategies:
{strategies_text}
{constraint_hint}

Rank these strategies from best to worst for THIS specific situation.
For EACH strategy, state:
1. Strategy name (rush_order, supplier_switch, equipment_rental, or schedule_resequence)
2. Estimated cost in dollars
3. Whether it fits the ${remaining:,.2f} budget
4. One-sentence justification

IMPORTANT: If materials are below minimum stock, do NOT rank schedule_resequence as #1 unless the plan also includes ordering those materials."""),
    ], temperature=0.2)

    result = response.content.strip()
    context["ranked_strategies"] = [{"text": result}]
    return f"Ranked mitigation strategies given diagnosis ({diagnosis[:80]}...):\n{result}"


def _exec_propose_plan(task: Task | None, context: dict, llm: BaseChatModel) -> str:
    from mcp_server import db
    project_id = context.get("project_id")
    project = context.get("project") or db.get_project(project_id)
    remaining = project["RemainingBudget"] if project else 0.0
    diagnosis = context.get("diagnose_result", "")
    ranked = context.get("ranked_strategies") or []
    ranked_text = ranked[0]["text"] if ranked else "no strategy available"
    low_stock = context.get("low_stock", [])

    # Build constraint hint
    constraint_hint = ""
    if low_stock:
        names = ", ".join(m["MaterialName"] for m in low_stock)
        constraint_hint += f"\nCRITICAL: These materials are below minimum stock: {names}. Your proposal MUST include ordering/replenishing them."

    response = llm.invoke([
        ("system", "You write concrete construction mitigation proposals. Use exact strategy keywords and dollar amounts."),
        ("human", f"""Diagnosis: {diagnosis}
Ranked strategies: {ranked_text}
Project {project_id} remaining budget: ${remaining:,.2f}
{constraint_hint}

Write a SPECIFIC proposal. You MUST:
1. Start with the exact strategy name: rush_order, supplier_switch, equipment_rental, OR schedule_resequence
2. State the estimated cost like: "Estimated cost: $X" 
3. Say explicitly: "This fits within the remaining budget of $Y" OR "This exceeds the remaining budget of $Y"
4. List 2-3 concrete next steps

If choosing rush_order, use the words "rush order" and state the premium cost.
If choosing schedule_resequence AND there is low stock, you MUST also say "Order [material names] to replenish stock."
If choosing supplier_switch, name the supplier and say "ContractStatus is Active".
If choosing equipment_rental, state the rental cost in dollars.

Do NOT use generic language."""),
    ], temperature=0.2)

    proposal = response.content.strip()
    context["proposal"] = proposal
    return proposal


def _exec_notify(task: Task | None, context: dict, llm: BaseChatModel) -> str:
    project_id = context.get("project_id")
    proposal = context.get("proposal", "")
    return f"Notification for Project {project_id}: a delay risk was identified. Recommended mitigation: {proposal}"


DEFAULT_EXECUTORS: dict[str, Callable[[Task | None, dict, BaseChatModel], str]] = {
    "diagnose": _exec_diagnose,
    "rank_options": _exec_rank_options,
    "propose_plan": _exec_propose_plan,
    "notify": _exec_notify,
}


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


def decompose_goal(
    goal: str,
    llm: BaseChatModel,
    project_id: Optional[int] = None,
) -> Plan:
    if project_id is None:
        project_id = _extract_project_id(goal)

    generated = llm.with_structured_output(
        GeneratedPlan,
        method="function_calling"
    ).invoke([
        ("system", PLANNER_SYSTEM),
        ("human", f"""Decompose this goal: {goal!r}
IMPORTANT:
1. Use exactly these task IDs: diagnose, rank_options, propose_plan, notify.
2. The 'notify' task MUST depend on 'propose_plan'.
3. Do not mark the plan as finished until a concrete proposal is made."""),
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
        executor_tasks: dict[str, Task] = {}
        llm_tasks: dict[str, str] = {}

        for task_id in batch:
            task = plan.task(task_id)
            if task_id in executors:
                executor_tasks[task_id] = task
            else:
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

        for task_id, task in executor_tasks.items():
            output = executors[task_id](task, context, llm)
            outputs[task_id] = output
            context[f"{task_id}_result"] = output

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
    if not terminals:
        # إذا لم يجد مهام نهائية، خذ آخر مهمة تم تنفيذها
        return list(outputs.values())[-1] if outputs else "No output generated."
    
    # إذا وجد أكثر من نهاية، اجمعي مخرجاتهم
    return "\n\n".join(outputs[t] for t in terminals if t in outputs)