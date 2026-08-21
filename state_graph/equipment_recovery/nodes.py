"""
Node functions for the equipment_recovery state graph (Problem 2 --
"Equipment breakdown recovery", owned by Person B).

Why this needs a state graph and not a straight-line script:
  - report_breakdown -> diagnose_issue -> evaluate_options can take
    real wall-clock time (a technician may need to physically inspect
    equipment between diagnose and evaluate), so the run genuinely
    spans more than one sitting.
  - approval_gate is a real branch on something outside the model's
    control: an admin's decision, which may never come immediately.
  - A rejected proposal is a real cycle: it loops back to
    evaluate_options with the rejection reason folded into state, not
    a retry of the same node.

LLM-call additions used here (2 of 4, per the project brief):
  - RAG            -> diagnose_issue (grounds the diagnosis in the
                       equipment/manual RAG corpus instead of letting
                       the model guess at failure causes)
  - Tree of Thoughts -> evaluate_options (compares repair / rent /
                       reroute as competing branches and scores them,
                       rather than committing to the first plausible
                       option)

HITL condition (must be defensible, not just "high risk"):
  approval_gate fires when the proposed option's cost exceeds the
  OWNING PROJECT'S live RemainingBudget (mcp_server.db.get_project),
  not a hardcoded dollar figure. This mirrors the project's own
  "grounded vs ungrounded" lesson from the Decomposition & Planning
  Lab (see README.md's $42,000 Riverside Tower case): a threshold
  that isn't checked against the real DB value is exactly the kind of
  proposal IronBridgeEnvironment was built to catch.
"""

from typing import Any, Dict
import json
import re
from mcp_server.db import get_project
from state_graph.core.hitl import require_hitl
from state_graph.equipment_recovery.tools import (
    diagnose_from_manuals,
    reserve_rental_equipment,
    schedule_repair,
    reroute_to_alternate_equipment,
)
from state_graph.equipment_recovery.tot import evaluate_recovery_options


_REQUIRED_BREAKDOWN_FIELDS = ("equipment_id", "project_id", "site", "reported_symptom")


def call_llm(prompt: str, temperature: float = 0.2) -> str:
    """Same shared bridge Person A's change_order uses -- routes through
    planning.model_provider so this stays consistent with the rest of
    the repo's LLM calls rather than inventing a second path."""
    from planning.model_provider import get_planning_llm
    from langchain_core.messages import HumanMessage

    llm = get_planning_llm()
    response = llm.invoke([HumanMessage(content=prompt)])
    return getattr(response, "content", str(response))


def _build_breakdown_prompt(raw_request: str) -> str:
    return f"""You are an equipment-breakdown intake assistant for a
construction company. A site worker has reported equipment trouble in
plain language. Decompose it into a JSON object with exactly these keys:
  - equipment_id: integer (the equipment's ID number)
  - project_id: integer (which project/site this equipment belongs to)
  - site: string (the site name)
  - reported_symptom: string (what's wrong, in the worker's own words)

If any field cannot be determined, set it to null.

EXAMPLE:
Raw request: "The crane at Riverside Tower (equipment 2, project 1) has
a hydraulic failure, not responding at all."
Response: {{"equipment_id": 2, "project_id": 1, "site": "Riverside Tower", "reported_symptom": "Hydraulic failure, not responding"}}

Now decompose this request:
{raw_request}

Respond with ONLY valid JSON, no markdown, no explanation."""


def _parse_breakdown_response(response: str) -> Dict[str, Any]:
    cleaned = re.sub(r"```(?:json)?\s*", "", response).strip()
    cleaned = re.sub(r"\s*```", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM breakdown decomposition produced unparseable JSON: {exc}")
    missing = set(_REQUIRED_BREAKDOWN_FIELDS) - set(parsed.keys())
    if missing:
        raise ValueError(f"LLM breakdown decomposition missing fields: {missing}")

    unresolved = [k for k in _REQUIRED_BREAKDOWN_FIELDS if parsed.get(k) is None]
    if unresolved:
        # Human-facing message, not a technical one -- surfaces
        # directly in the chat UI if this bubbles up as a ticket, so
        # the user knows exactly what to add to their message instead
        # of seeing a raw Python error.
        field_hints = {
            "equipment_id": "the equipment's ID number (e.g. 'equipment 2')",
            "project_id": "which project it belongs to (e.g. 'project 1')",
            "site": "the site name",
            "reported_symptom": "what's wrong with it",
        }
        needed = ", ".join(field_hints[k] for k in unresolved)
        raise ValueError(
            f"Could not determine {needed} from your message. "
            f"Please include these details and try again."
        )

    return {
        "equipment_id": int(parsed["equipment_id"]),
        "project_id": int(parsed["project_id"]),
        "site": str(parsed["site"]),
        "reported_symptom": str(parsed["reported_symptom"]),
    }

def _llm_decompose_breakdown_with_retry(raw_request: str, max_retries: int = 2) -> Dict[str, Any]:
    prompt = _build_breakdown_prompt(raw_request)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = call_llm(prompt, temperature=0.2)
            return _parse_breakdown_response(response)
        except ValueError as exc:
            last_error = exc
            if attempt < max_retries:
                prompt += f"\n\nYour previous response was invalid: {exc}. Please fix and respond with ONLY valid JSON."
                continue
            raise last_error

    raise last_error  # pragma: no cover

def report_breakdown(state: Dict[str, Any]) -> Dict[str, Any]:
    """Entry node. Just records that a run has started for this piece
    of equipment -- no diagnosis yet, on purpose (see module docstring
    on why this is a separate node from diagnose_issue).

    TASK DECOMPOSITION addition: accepts either a ready-made structured
    state (backward compatible -- test_equipment_recovery.py and any
    programmatic caller keep working unchanged) OR a single free-text
    "request" field, which an LLM call decomposes into the required
    fields. Mirrors state_graph/change_order/nodes.py's
    decompose_change_order_node, so a real end user typing a plain
    sentence in the chat UI works the same way across both agents
    instead of the frontend needing its own separate parsing logic.
    """
    if all(k in state for k in _REQUIRED_BREAKDOWN_FIELDS):
        structured = {k: state[k] for k in _REQUIRED_BREAKDOWN_FIELDS}
    else:
        raw_request = state.get("request")
        if not raw_request:
            raise ValueError(
                f"report_breakdown needs either {_REQUIRED_BREAKDOWN_FIELDS} "
                f"directly, or a 'request' free-text field to decompose."
            )
        # If this raises (e.g. equipment_id couldn't be determined),
        # it propagates up as-is -- no partial 'structured' to fall
        # back on, which is correct: a half-decomposed request isn't
        # safe to proceed with.
        structured = _llm_decompose_breakdown_with_retry(raw_request)

    return {
        **structured,
        "status_note": f"Breakdown reported for equipment {structured['equipment_id']} "
                        f"at {structured['site']}: {structured['reported_symptom']}",
    }


def diagnose_issue(state: Dict[str, Any]) -> Dict[str, Any]:
    """RAG addition: grounds the diagnosis in the equipment manuals /
    safety-catalog corpus (rag/) instead of letting the model guess
    at a failure cause from the symptom text alone."""
    diagnosis = diagnose_from_manuals(
        equipment_id=state["equipment_id"],
        symptom=state["reported_symptom"],
    )
    return {
        "diagnosis": diagnosis["cause"],
        "diagnosis_confidence": diagnosis["confidence"],
        "diagnosis_sources": diagnosis["sources"],
    }


def evaluate_options(state: Dict[str, Any]) -> Dict[str, Any]:
    """Tree of Thoughts addition: branches over repair / rent / reroute,
    scores each against cost, downtime, and diagnosis confidence, and
    proposes the best one. Feeds the rejection reason back in on a
    resumed run (loop from approval_gate) so it doesn't re-propose the
    same rejected option."""
    rejection_reason = state.get("rejection_reason")

    proposal = evaluate_recovery_options(
        equipment_id=state["equipment_id"],
        diagnosis=state["diagnosis"],
        site=state["site"],
        previously_rejected=state.get("rejected_options", []),
        rejection_reason=rejection_reason,
    )

    rejected_options = list(state.get("rejected_options", []))

    return {
        "proposed_action": proposal["action"],       # "repair" | "rent" | "reroute"
        "proposed_cost": proposal["estimated_cost"],
        "proposal_rationale": proposal["rationale"],
        "rejected_options": rejected_options,
        # clear the previous rejection reason now that ToT has used it
        "rejection_reason": None,
    }


def approval_gate(state: Dict[str, Any]) -> Dict[str, Any]:
    """HITL node. Grounded threshold: only pauses for a human decision
    when proposed_cost exceeds the OWNING project's real, current
    RemainingBudget -- not a fixed number picked out of the air.
    Cheap options (repair for a few hundred pounds, well inside
    budget) skip the human entirely and go straight to execution.
    """
    project_id = state.get("project_id") or 1  # Default to demo project
    project = get_project(project_id)
    if project is None:
        raise ValueError(f"Project {project_id} not found")
    remaining_budget = project["RemainingBudget"]

    if state["proposed_cost"] <= remaining_budget:
        # Under budget: no HITL needed, proceed straight to execution.
        return {"approval_status": "auto_approved", "hitl_decision": None}
    decision_key = f"hitl_decision__{state['proposed_action']}"
    state["remaining_budget"] = remaining_budget
    decision = require_hitl(
        state,
        reason=(
            f"Proposed {state['proposed_action']} costs "
            f"${state['proposed_cost']:,.2f}, which exceeds Project "
            f"{state['project_id']}'s remaining budget of "
            f"${remaining_budget:,.2f}."
        ),
        payload={
            "equipment_id": state["equipment_id"],
            "project_id": state["project_id"],
            "proposed_action": state["proposed_action"],
            "proposed_cost": state["proposed_cost"],
            "remaining_budget": remaining_budget,
            "rationale": state["proposal_rationale"],
        },
          decision_key=decision_key,
    )

    if decision == "approved":
        # Clear hitl_decision now that it's been consumed -- otherwise
        # it would leak into a LATER cycle through this same node (a
        # different HITL-gated proposal after a future rejection) and
        # require_hitl() would silently reuse this stale decision
        # instead of pausing for a fresh one. Caught via testing the
        # reject -> loop-back -> re-propose -> HITL-again path.
        return {"approval_status": "approved", decision_key: None}

    # Rejected: record why, and remember this option was already
    # rejected so evaluate_options doesn't propose it again on the
    # loop-back.
    rejected = list(state.get("rejected_options", []))
    rejected.append(state["proposed_action"])
    return {
        "approval_status": "rejected",
        "rejected_options": rejected,
        "rejection_reason": decision if isinstance(decision, str) else "rejected by admin",
        decision_key: None,
    }


def execute_recovery_action(state: Dict[str, Any]) -> Dict[str, Any]:
    """Constrained ReAct addition: calls exactly one whitelisted MCP
    tool based on the approved action -- never a free-form action the
    model invents. This is the node that turns an approved decision
    into a real change (a reservation, a scheduled repair, an updated
    equipment assignment)."""
    action = state["proposed_action"]

    if action == "repair":
        result = schedule_repair(
            equipment_id=state["equipment_id"],
            diagnosis=state["diagnosis"],
        )
    elif action == "rent":
        result = reserve_rental_equipment(
            equipment_id=state["equipment_id"],
            site=state["site"],
            estimated_cost=state["proposed_cost"],
        )
    elif action == "reroute":
        result = reroute_to_alternate_equipment(
            equipment_id=state["equipment_id"],
            site=state["site"],
        )
    else:
        # Genuinely unexpected -- the model returned an action outside
        # the 3 whitelisted ones. Not a HITLPause: this is exactly the
        # "model returned something the graph can't act on" case
        # tickets.py describes.
        raise ValueError(f"execute_recovery_action: unrecognized action '{action}'")

    return {
        "execution_result": result,
        "recovery_complete": True,
    }