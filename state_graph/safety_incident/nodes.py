"""
state_graph/safety_incident/nodes.py
Owner: Person C

LLM additions (wired for real on Day 3 / C3 -- this is the Day-2
skeleton, structurally complete and runnable end to end, but
investigate_node and file_regulator_report_node currently use a
deterministic stand-in where the real technique plugs in):
  1. LATS            -> investigate_node() will search over candidate
     root-cause / corrective-action paths scored against a real
     severity check, not the model's own opinion of urgency (see the
     assignment's own intake-triage example). Day-3 TODO marked below.
  2. Constrained ReAct -> file_regulator_report_node() may ONLY call
     tools.submit_regulator_report(); no free-form DB access. Day-3
     TODO marked below wires the actual regulator-form-filling reasoning.

HITL: safety_officer_signoff_node() -- pauses for a real safety
officer to decide whether the incident needs another investigation
round, needs a regulator report, or can close with no report. This is
the genuine cycle in this graph: 'needs_more_investigation' routes
back to investigate_node, and only a human decides that, not a retry.

Cycle-safety note (found while reviewing A3's change_order graph on
Day 2 -- see require_hitl()'s docstring in core/hitl.py): a HITL node
that can be revisited in the SAME run must use a decision_key that is
unique to the specific pause, not the require_hitl() default. Here
that's incident_id + InvestigationRound, since bump_investigation_round()
increments the round on every 'needs_more_investigation' cycle. Using
a stale/shared key would silently skip the pause on the second officer
review instead of asking again -- exactly the "auto-approve where a
grader isn't likely to read" failure mode the project brief warns
against.

Ticket path: escalate_stalled_review_node() -- fires when an incident
sits in PendingOfficerReview past REVIEW_TIMEOUT_SECONDS; an
unavailable safety officer isn't something a retry can fix.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Dict

# --------------------------------------------------------------------------
# Path & Module Resolution
# --------------------------------------------------------------------------
_current_dir = Path(__file__).resolve().parent
REPO_ROOT = next(
    (p for p in [_current_dir] + list(_current_dir.parents) if (p / "mcp_server").exists()),
    _current_dir.parent.parent  # Fallback
)

for path_entry in (str(REPO_ROOT), str(REPO_ROOT / "mcp_server")):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from state_graph.safety_incident import tools
from state_graph.core.hitl import require_hitl
from state_graph.core.tickets import TicketableError

REVIEW_TIMEOUT_SECONDS = 60 * 60 * 24  # 1 day -- tune to real policy


def report_incident_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Intake: creates the SafetyIncidents record from the raw report."""
    req = state.get("request")
    if not req:
        raise TicketableError(
            "Missing 'request' payload in state.",
            context={"state_keys": list(state.keys())},
        )

    incident_id = tools.create_incident(
        run_id=state.get("run_id", ""),
        project_id=req["project_id"],
        description=req["description"],
        severity=req["severity"],
        actor_id=req["employee_id"],
    )
    return {
        "incident_id": incident_id,
        "employee_id": req["employee_id"],
    }


def investigate_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """DAY-3 TODO (C3): replace this deterministic stand-in with a real
    LATS search over candidate investigation paths -- e.g. weighted by
    a genuine severity/regulatory-exposure check pulled from
    mcp_server's policy docs, not the model's own opinion of urgency
    (see the assignment's intake-triage worked example). For the Day-2
    skeleton this just records that a round happened, which is enough
    to prove the graph shape, the cycle, and the checkpointing."""
    incident_id = state.get("incident_id")
    if not incident_id:
        raise TicketableError(
            "Cannot investigate: missing incident_id in state.",
            context={"state": state},
        )

    co = tools.get(incident_id)
    if not co:
        raise TicketableError(
            f"Safety incident record #{incident_id} not found in DB.",
            context={"incident_id": incident_id},
        )

    tools.record_investigation_notes(
        incident_id,
        notes=f"[skeleton] investigation round {co['InvestigationRound']} placeholder findings",
        actor_id=state["employee_id"],
    )
    return {}


def safety_officer_signoff_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Uses require_hitl() to raise HITLPause on the initial call for
    THIS investigation round, and receives the resolved decision when
    resumed. decision_key is scoped to incident_id + InvestigationRound
    so a second (or third) officer review pauses fresh -- see the
    cycle-safety note in this file's module docstring."""
    incident_id = state.get("incident_id")
    if not incident_id:
        raise TicketableError(
            "Cannot await officer sign-off: missing incident_id in state.",
            context={"state": state},
        )

    incident = tools.get(incident_id)
    if not incident:
        raise TicketableError(
            f"Safety incident record #{incident_id} not found in DB.",
            context={"incident_id": incident_id},
        )

    round_no = incident["InvestigationRound"]
    decision_key = f"hitl_decision_incident{incident_id}_round{round_no}"

    reason = (
        f"Safety incident {incident['IncidentID']} (severity={incident['Severity']}), "
        f"investigation round {round_no}: {incident['InvestigationNotes'] or '(no notes yet)'}. "
        f"Close with no report, require a regulator report, or send back for more investigation?"
    )
    payload = {
        "incident": incident,
        # A's HITL inbox route (or C's own, if C ends up owning the
        # safety-incident admin surface) MUST read this back and pass
        # it to resolve_hitl_task(..., decision_key=payload["decision_key"]).
        # Resolving with the default key silently no-ops on round 2+.
        "decision_key": decision_key,
    }

    decision = require_hitl(state, reason=reason, payload=payload, decision_key=decision_key)
    return {"hitl_decision": decision}


def handle_officer_decision_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Applies whichever decision the officer made. Routing itself
    happens in _decision_router (graph.py) -- this node only performs
    the corresponding DB side effect."""
    incident_id = state.get("incident_id")
    decision = state.get("hitl_decision")
    actor_id = state["employee_id"]

    if not decision:
        raise TicketableError(
            "handle_officer_decision_node reached with no resolved HITL decision.",
            context={"incident_id": incident_id},
        )

    if decision == "needs_more_investigation":
        tools.bump_investigation_round(incident_id, actor_id)
    elif decision == "regulator_report_required":
        tools.mark_regulator_report_required(incident_id, actor_id)
    elif decision == "closed_no_report":
        tools.close(incident_id, actor_id)
    else:
        raise TicketableError(
            f"Unknown officer decision '{decision}'.",
            context={"incident_id": incident_id, "decision": decision},
        )

    return {}


def file_regulator_report_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """DAY-3 TODO (C3): replace the hard-coded case number with real
    constrained ReAct -- the model reasons about which regulator form
    fields to fill from the incident record, but the ONLY tool call it
    may make is tools.submit_regulator_report(); no free-form DB
    access, matching the constrained-ReAct guarantee used in A3's
    file_change_order_node()."""
    incident_id = state.get("incident_id")
    if not incident_id:
        raise TicketableError(
            "Cannot file regulator report: missing incident_id in state.",
            context={"state": state},
        )

    case_number = f"REG-{incident_id}"  # placeholder until C3 wires real form-filling
    tools.submit_regulator_report(incident_id, case_number, state["employee_id"])
    return {}


def escalate_stalled_review_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Invoked by an external scheduler/poller checking how long an
    incident has sat in PendingOfficerReview. Raises TicketableError if
    it exceeds REVIEW_TIMEOUT_SECONDS -- an unavailable safety officer
    is a real failure a single retry cannot fix."""
    incident_id = state.get("incident_id")
    if not incident_id:
        raise TicketableError("Cannot check stall status: missing incident_id.")

    incident = tools.get(incident_id)
    if not incident or not incident.get("ReportedAt"):
        return {}

    import time
    elapsed = time.time() - incident["ReportedAt"]
    if incident["Status"] == "PendingOfficerReview" and elapsed > REVIEW_TIMEOUT_SECONDS:
        raise TicketableError(
            f"Safety incident {incident_id} has had no officer review for "
            f"{elapsed / 3600:.1f} hours (window: {REVIEW_TIMEOUT_SECONDS / 3600:.0f}h).",
            context={"incident_id": incident_id, "elapsed_seconds": elapsed},
        )
    return {}
