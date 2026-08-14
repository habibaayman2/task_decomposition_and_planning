from __future__ import annotations

import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional, Type

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import BaseModel

# Resolve project root for cross-module imports
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

_warned = False


def has_real_llm() -> bool:
    return bool(os.environ.get("GROQ_API_KEY"))


def _warn_once():
    global _warned
    if not _warned:
        print(
            "[planning.model_provider] GROQ_API_KEY not set -- using "
            "DeterministicPlanningLLM. Set GROQ_API_KEY for real generations."
        )
        _warned = True


RESPONSE_STRATEGIES = [
    {
        "name": "rush_order",
        "text": "Place a rush order with the current supplier to expedite the missing material, "
                "accepting the rush premium to protect the schedule.",
    },
    {
        "name": "supplier_switch",
        "text": "Switch to an alternate qualified supplier for the material category, accepting a "
                "longer lead time in exchange for standard (non-rush) pricing.",
    },
    {
        "name": "equipment_rental",
        "text": "Rent replacement equipment for the maintenance window instead of waiting on the "
                "in-house unit, absorbing the rental cost to avoid a schedule slip.",
    },
    {
        "name": "schedule_resequence",
        "text": "Resequence unaffected trades/tasks ahead of the blocked item so the crew stays "
                "productive while the blocking issue is resolved, at no extra material cost.",
    },
]

_PROJECT_RE = re.compile(r"[Pp]roject(?:\s*ID)?\s*(\d+)")
_MATERIAL_RE = re.compile(r"[Mm]aterial(?:\s*ID)?\s*(\d+)")
_AMOUNT_RE = re.compile(r"\$?([\d,]+(?:\.\d+)?)\s*(?:dollars|usd)?", re.IGNORECASE)


def _extract_ironbridge_context(prompt_text: str) -> dict:
    ctx = {}
    m = _PROJECT_RE.search(prompt_text)
    if m:
        ctx["project_id"] = int(m.group(1))
    m = _MATERIAL_RE.search(prompt_text)
    if m:
        ctx["material_id"] = int(m.group(1))
    m = _AMOUNT_RE.search(prompt_text)
    if m:
        try:
            ctx["mentioned_amount"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return ctx


@lru_cache(maxsize=8)
def _cached_get_project(project_id: int):
    try:
        from mcp_server import db
        return db.get_project(project_id)
    except Exception:
        return None


@lru_cache(maxsize=1)
def _cached_low_stock():
    try:
        from mcp_server import db
        return [m for m in db.find_materials(None, None) if m["QuantityAvailable"] < m["MinimumStockLevel"]]
    except Exception:
        return []


@lru_cache(maxsize=8)
def _cached_equipment_issues():
    try:
        from mcp_server import db
        return [e for e in db.equipment_status(None, None) if e["Availability"] == "Under Maintenance"]
    except Exception:
        return []


class _StructuredOutputRunnable(Runnable):
    def __init__(self, schema: Type[BaseModel], call_count_ref: list[int]):
        self.schema = schema
        self._call_count_ref = call_count_ref

    def invoke(self, input: Any, config: Optional[dict] = None, **kwargs) -> BaseModel:
        self._call_count_ref[0] += 1
        prompt_text = self._render(input)
        return _fake_structured_response(self.schema, prompt_text, self._call_count_ref[0])

    def _render(self, input: Any) -> str:
        if isinstance(input, str):
            return input
        if isinstance(input, list):
            return "\n".join(getattr(m, "content", str(m)) for m in input)
        if hasattr(input, "to_messages"):
            return "\n".join(getattr(m, "content", str(m)) for m in input.to_messages())
        return str(input)


class GroqFallbackWrapper:
    def invoke(self, input_messages: Any, **kwargs: Any):
        class DummyResponse:
            content = "Groq Plan Step 1: Analyze project constraints.\nGroq Plan Step 2: Execute solution."
        return DummyResponse()

    def with_structured_output(self, schema: Type[BaseModel], **kwargs: Any):
        class StructuredRunnable:
            def invoke(self, input_messages: Any, config: Optional[dict] = None, **kwargs: Any) -> Any:
                return _fake_structured_response(schema, str(input_messages), 1)
        return StructuredRunnable()


def _fake_structured_response(schema: Type[BaseModel], prompt_text: str, call_count: int) -> BaseModel:
    fields = set(schema.model_fields.keys())
    ctx = _extract_ironbridge_context(prompt_text)

    # GeneratedPlan (decomposition.py): goal, tasks
    if fields == {"goal", "tasks"}:
        return _fake_generated_plan(schema, prompt_text, ctx)

    # DynamicDecision (dynamic_decomposition.py): done, next_task
    if fields == {"done", "next_task"}:
        step = call_count
        if step > len(RESPONSE_STRATEGIES):
            return schema(done=True, next_task="")
        strat = RESPONSE_STRATEGIES[step - 1]
        return schema(done=False, next_task=strat["text"])

    # ThoughtCandidates (tree_of_thoughts.py): candidates
    if fields == {"candidates"}:
        idx = (call_count - 1) * 2 % len(RESPONSE_STRATEGIES)
        picks = [RESPONSE_STRATEGIES[idx]["text"], RESPONSE_STRATEGIES[(idx + 1) % len(RESPONSE_STRATEGIES)]["text"]]
        return schema(candidates=picks)

    # ThoughtEvaluationItem (tree_of_thoughts.py batch): candidate_index, score, rationale
    if fields == {"candidate_index", "score", "rationale"}:
        score = _deterministic_score_for_text(prompt_text)
        return schema(candidate_index=0, score=score, rationale=f"deterministic heuristic score ({score:.2f})")

    # ThoughtEvaluations (tree_of_thoughts.py batch wrapper): evaluations
    if fields == {"evaluations"}:
        item_cls = schema.model_fields["evaluations"].annotation.__args__[0]
        score = _deterministic_score_for_text(prompt_text)
        return schema(evaluations=[
            item_cls(candidate_index=0, score=score, rationale=f"batch eval score ({score:.2f})")
        ])

    # LATSAction (lats.py): action, state
    if fields == {"action", "state"}:
        idx = (call_count - 1) % len(RESPONSE_STRATEGIES)
        strat = RESPONSE_STRATEGIES[idx]
        return schema(action=strat["name"], state=strat["text"])

    # LATSActionBatch (lats.py): actions
    if fields == {"actions"}:
        action_cls = schema.model_fields["actions"].annotation.__args__[0]
        actions = [action_cls(action=s["name"], state=s["text"]) for s in RESPONSE_STRATEGIES[:2]]
        return schema(actions=actions)

    # ValueEstimate (lats.py): score
    if fields == {"score"}:
        return schema(score=_deterministic_score_for_text(prompt_text))

    # EnvironmentFeedback
    if fields == {"success", "score", "details"}:
        return schema(success=True, score=0.5, details=["deterministic fallback -- no real evaluation performed"])

    raise NotImplementedError(
        f"DeterministicPlanningLLM has no fake generator for a schema with fields {fields}. "
        "Add a case in planning/model_provider.py::_fake_structured_response."
    )

def _deterministic_score_for_text(text: str) -> float:
    lowered = text.lower()
    for strat, base in [
        ("schedule_resequence", 0.8),
        ("supplier_switch", 0.65),
        ("rush_order", 0.55),
        ("equipment_rental", 0.5),
    ]:
        strat_text = next(s["text"] for s in RESPONSE_STRATEGIES if s["name"] == strat)
        if any(word in lowered for word in strat_text.lower().split()[:3]):
            return base
    return 0.5


def _fake_generated_plan(schema: Type[BaseModel], prompt_text: str, ctx: dict) -> BaseModel:
    task_cls = schema.model_fields["tasks"].annotation.__args__[0]
    pid = ctx.get("project_id", 1)
    tasks = [
        task_cls(id="diagnose", instruction=f"Diagnose the root cause of the delay risk for project {pid} by checking material stock, equipment status, and supplier contract status.", depends_on=[]),
        task_cls(id="rank_options", instruction="Rank the available mitigation strategies (rush order, supplier switch, equipment rental, schedule resequence) by cost and time impact.", depends_on=["diagnose"]),
        task_cls(id="propose_plan", instruction=f"Propose the final mitigation plan for project {pid}, checked against the remaining project budget.", depends_on=["rank_options"]),
        task_cls(id="notify", instruction="Draft a short delay-notification summary for the site engineer explaining the chosen plan.", depends_on=["propose_plan"]),
    ]
    return schema(goal=prompt_text[:200] or "Resolve project delay risk", tasks=tasks)


class DeterministicPlanningLLM(BaseChatModel):
    call_count: list = [0]

    @property
    def _llm_type(self) -> str:
        return "ironbridge-deterministic-planning-fallback"

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None, **kwargs) -> ChatResult:
        prompt_text = "\n".join(getattr(m, "content", "") for m in messages)
        text = _fake_freetext_response(prompt_text)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    def with_structured_output(self, schema: Type[BaseModel], **kwargs) -> Runnable:
        return _StructuredOutputRunnable(schema, self.call_count)


def _fake_freetext_response(prompt_text: str) -> str:
    lowered = prompt_text.lower()
    ctx = _extract_ironbridge_context(prompt_text)
    pid = ctx.get("project_id", 1)

    if "critique" in lowered or "review" in lowered:
        return (
            "The draft is directionally correct but doesn't explicitly confirm the proposed cost "
            "stays within the project's remaining budget -- add an explicit budget check."
        )
    if "revise" in lowered or "improve" in lowered:
        return (
            "Revised: the plan now explicitly states the estimated cost and confirms it against "
            "the project's remaining budget before recommending the mitigation strategy."
        )
    if "reflect" in lowered:
        return (
            "Reflection: the prior attempt didn't check the real remaining budget before proposing "
            "a rush order; check it before proposing next time."
        )

    if "diagnose" in lowered:
        return _diagnose_project(pid)
    if "rank" in lowered and ("strateg" in lowered or "option" in lowered or "mitigat" in lowered):
        ranked = sorted(RESPONSE_STRATEGIES, key=lambda s: _deterministic_score_for_text(s["text"]), reverse=True)
        lines = [f"{i+1}. {s['text']}" for i, s in enumerate(ranked)]
        return "Ranked mitigation strategies (highest-value first):\n" + "\n".join(lines)
    if "propose" in lowered and "plan" in lowered:
        return _propose_plan(pid)
    if "notify" in lowered or "notification" in lowered or "draft" in lowered:
        return (
            f"Notification for Project {pid}: A delay risk was identified and the recommended "
            f"mitigation ({RESPONSE_STRATEGIES[0]['name']}) is being pursued. See the proposed plan for cost details."
        )

    lines = [l.strip() for l in prompt_text.splitlines() if len(l.strip()) > 30 and "complete only" not in l.lower() and "do not invent" not in l.lower()]
    return lines[-1][:300] if lines else "(no extractable content)"


def _diagnose_project(project_id: int) -> str:
    project = _cached_get_project(project_id)
    low_stock = _cached_low_stock()
    equipment_issues = _cached_equipment_issues()

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
    return "Diagnosis: " + " ".join(lines)


def _propose_plan(project_id: int) -> str:
    project = _cached_get_project(project_id)
    remaining = project["RemainingBudget"] if project else 0.0
    best = max(RESPONSE_STRATEGIES, key=lambda s: _deterministic_score_for_text(s["text"]))
    return (
        f"Proposed plan for Project {project_id}: {best['text']} "
        f"Remaining budget on record: ${remaining:,.2f} -- this proposal must be checked "
        f"against that figure before approval."
    )


def get_planning_llm() -> BaseChatModel:
    if has_real_llm():
        try:
            from dotenv import load_dotenv
            from langchain_groq import ChatGroq
            load_dotenv()
            model_name = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
            return ChatGroq(
                model=model_name,
                groq_api_key=os.environ["GROQ_API_KEY"],
                temperature=0.2,
            )
        except ImportError:
            print(
                "[Warning] GROQ_API_KEY is set, but 'langchain_groq' is not installed. "
                "Falling back to DeterministicPlanningLLM."
            )
    _warn_once()
    return DeterministicPlanningLLM(call_count=[0])