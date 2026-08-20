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

from mcp_server.db import get_project
from state_graph.core.hitl import require_hitl
from state_graph.equipment_recovery.tools import (
    diagnose_from_manuals,
    reserve_rental_equipment,
    schedule_repair,
    reroute_to_alternate_equipment,
)
from state_graph.equipment_recovery.tot import evaluate_recovery_options


def report_breakdown(state: Dict[str, Any]) -> Dict[str, Any]:
    """Entry node. Just records that a run has started for this piece
    of equipment -- no diagnosis yet, on purpose (see module docstring
    on why this is a separate node from diagnose_issue)."""
    required = ("equipment_id", "project_id", "site", "reported_symptom")
    missing = [k for k in required if k not in state]
    if missing:
        # Not a HITLPause -- this is a genuine unplanned failure (bad
        # input), so it becomes a ticket, per tickets.py's contract.
        raise ValueError(f"report_breakdown missing required state keys: {missing}")

    return {
        "status_note": f"Breakdown reported for equipment {state['equipment_id']} "
                        f"at {state['site']}: {state['reported_symptom']}",
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
    project = get_project(state["project_id"])
    remaining_budget = project["RemainingBudget"]

    if state["proposed_cost"] <= remaining_budget:
        # Under budget: no HITL needed, proceed straight to execution.
        return {"approval_status": "auto_approved", "hitl_decision": None}
    decision_key = f"hitl_decision__{state['proposed_action']}"
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