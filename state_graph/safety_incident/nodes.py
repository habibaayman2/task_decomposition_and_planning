"""
state_graph/safety_incident/nodes.py
Owner: Person C

LLM additions (wired for real as of Day 3 / C3):
  1. LATS            -> investigate_node() searches over candidate
     root-cause / corrective-action paths (see lats.py) scored against
     the incident's real DB severity AND a grounded regulatory-
     exposure check against the actual policy corpus in rag/policies/
     -- not the model's own opinion of urgency (see the assignment's
     own intake-triage example). An ungrounded round is pruned and the
     search expands before it commits.
  2. Constrained ReAct -> file_regulator_report_node() reasons (Thought)
     about whether the investigation record supports filing, then
     chooses from a closed whitelist. The ONLY DB-touching action is
     tools.submit_regulator_report(); no free-form DB access, matching
     the constrained-ReAct guarantee used in A3's
     file_change_order_node(). A non-whitelisted or "incomplete"
     verdict is ticketed rather than silently filed.

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

import json
import re
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
from state_graph.safety_incident.lats import investigate_incident
from state_graph.core.hitl import require_hitl
from state_graph.core.tickets import TicketableError

REVIEW_TIMEOUT_SECONDS = 60 * 60 * 24  # 1 day -- tune to real policy


# --------------------------------------------------------------------------
# LLM Bridge — connects state_graph nodes to planning/model_provider.py
# (same bridge pattern as change_order/nodes.py and
# equipment_recovery/nodes.py; own prompt-keyed stubs so tests/demos
# run deterministically without a GROQ_API_KEY).
# --------------------------------------------------------------------------

def call_llm(prompt: str, temperature: float = 0.2) -> str:
    """Unified LLM call for safety_incident nodes."""
    try:
        from planning.model_provider import get_planning_llm, has_real_llm
        from langchain_core.messages import HumanMessage

        llm = get_planning_llm()
        response = llm.invoke([HumanMessage(content=prompt)])
        text = getattr(response, "content", str(response))

        if has_real_llm():
            return text

        # DeterministicPlanningLLM fallback validation
        lowered = prompt.lower()
        is_investigation = "safety investigator" in lowered
        is_regulator_react = "regulator filing officer" in lowered

        if is_investigation and not text.strip().startswith("["):
            return _stub_investigation()
        if is_regulator_react and "thought:" not in text.lower():
            return _stub_regulator_react(prompt)
        return text

    except Exception:
        # Total import failure — use own stubs
        return _stub_fallback(prompt)


def _stub_investigation() -> str:
    return json.dumps(
        [
            {
                "root_cause": "Unsecured scaffolding component not caught by pre-shift inspection",
                "corrective_action": "Enforce documented pre-shift scaffolding inspection with sign-off",
                "rationale": "Matches the reported near-miss pattern and known scaffolding safety procedure.",
            },
            {
                "root_cause": "Inadequate crew training on fall-protection anchor points",
                "corrective_action": "Schedule refresher fall-protection training for the affected crew",
                "rationale": "Training gaps are a common contributing factor to scaffolding incidents.",
            },
            {
                "root_cause": "Missing guardrail signage at the level-4 work area",
                "corrective_action": "Install compliant guardrail signage and re-tag the area",
                "rationale": "Signage gaps reduce awareness of an active fall hazard.",
            },
        ]
    )


def _stub_regulator_react(prompt: str) -> str:
    lowered = prompt.lower()
    if "investigation notes: (none yet)" in lowered:
        return (
            "Thought: There are no investigation findings on record yet, so there is "
            "nothing to file. Flagging for review rather than filing an empty report.\n"
            "Action: flag_incomplete_for_review"
        )
    return (
        "Thought: Investigation findings are on record and the officer has required a "
        "regulator report. Filing it now.\n"
        "Action: submit_regulator_report"
    )


def _stub_fallback(prompt: str) -> str:
    lowered = prompt.lower()
    if "safety investigator" in lowered:
        return _stub_investigation()
    if "regulator filing officer" in lowered:
        return _stub_regulator_react(prompt)
    return "Thought: Unable to reach the LLM provider; defaulting to a safe fallback.\nAction: flag_incomplete_for_review"

_REQUIRED_INCIDENT_FIELDS = ("project_id", "description", "severity", "employee_id")


def _build_incident_prompt(raw_request: str) -> str:
    return f"""You are a safety-incident intake assistant for a
construction company. A worker or site engineer has reported a safety
incident in plain language. Decompose it into a JSON object with
exactly these keys:
  - project_id: integer (which project/site this happened at)
  - description: string (concise summary of what happened, max 200 chars)
  - severity: string, one of: "low", "medium", "high", "critical"
  - employee_id: integer (the person reporting/submitting this)

If any field cannot be determined, set it to null.

EXAMPLE:
Raw request: "A worker fell from scaffolding at the Riverside Tower
site (project 1), broke his arm, reported by employee 3."
Response: {{"project_id": 1, "description": "Worker fell from scaffolding, broken arm", "severity": "high", "employee_id": 3}}

Now decompose this request:
{raw_request}

Respond with ONLY valid JSON, no markdown, no explanation."""


def _parse_incident_response(response: str) -> Dict[str, Any]:
    cleaned = re.sub(r"```(?:json)?\s*", "", response).strip()
    cleaned = re.sub(r"\s*```", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise TicketableError(
            f"LLM incident decomposition produced unparseable JSON: {exc}",
            context={"llm_response": response[:500]},
        )

    missing = set(_REQUIRED_INCIDENT_FIELDS) - set(parsed.keys())
    if missing:
        raise TicketableError(
            f"LLM incident decomposition missing fields: {missing}",
            context={"llm_response": response[:500], "parsed": parsed},
        )

    field_hints = {
        "project_id": "which project/site this happened at (e.g. 'project 1')",
        "description": "what happened",
        "severity": "how severe it was (low/medium/high/critical)",
        "employee_id": "who is reporting this (e.g. 'employee 1')",
    }
    unresolved = [k for k in _REQUIRED_INCIDENT_FIELDS if parsed.get(k) is None]
    if unresolved:
        needed = ", ".join(field_hints[k] for k in unresolved)
        raise TicketableError(
            f"Could not determine {needed} from your message. "
            f"Please include these details and try again.",
            context={"llm_response": response[:500], "parsed": parsed},
        )

    try:
        return {
            "project_id": int(parsed["project_id"]),
            "description": str(parsed["description"]),
            "severity": str(parsed["severity"]).lower(),
            "employee_id": int(parsed["employee_id"]),
        }
    except (ValueError, TypeError) as exc:
        raise TicketableError(
            f"LLM incident decomposition field type error: {exc}",
            context={"llm_response": response[:500], "parsed": parsed},
        )


def _llm_decompose_incident_with_retry(raw_request: str, max_retries: int = 2) -> Dict[str, Any]:
    prompt = _build_incident_prompt(raw_request)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = call_llm(prompt, temperature=0.2)
            return _parse_incident_response(response)
        except TicketableError as exc:
            last_error = exc
            if attempt < max_retries:
                prompt += f"\n\nYour previous response was invalid: {exc}. Please fix and respond with ONLY valid JSON."
                continue
            raise last_error

    raise last_error  # pragma: no cover


def report_incident_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Intake: creates the SafetyIncidents record from the raw report.

    TASK DECOMPOSITION addition: accepts either a ready-made structured
    dict under state["request"] (backward compatible with any existing
    programmatic caller), OR a plain free-text string, which an LLM
    call decomposes into the required fields -- mirrors
    change_order/nodes.py's decompose_change_order_node and
    equipment_recovery/nodes.py's report_breakdown, so a real user
    typing a plain sentence in the chat UI works the same way across
    all three state-graph agents.
    """
    req = state.get("request")
    if not req:
        raise TicketableError(
            "Missing 'request' payload in state.",
            context={"state_keys": list(state.keys())},
        )

    if isinstance(req, dict) and all(k in req for k in _REQUIRED_INCIDENT_FIELDS):
        structured = req
    elif isinstance(req, str):
        structured = _llm_decompose_incident_with_retry(req)
    else:
        raise TicketableError(
            f"'request' must be a structured dict with {_REQUIRED_INCIDENT_FIELDS} "
            f"or a free-text string to decompose, got {type(req).__name__}.",
            context={"state_keys": list(state.keys())},
        )

    incident_id = tools.create_incident(
        run_id=state.get("run_id", ""),
        project_id=structured["project_id"],
        description=structured["description"],
        severity=structured["severity"],
        actor_id=structured["employee_id"],
    )
    return {
        "incident_id": incident_id,
        "employee_id": structured["employee_id"],
    }

def investigate_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LATS addition (C3): searches over candidate root-cause /
    corrective-action paths (see lats.py) for this investigation
    round, scored against the incident's real DB severity plus a
    grounded regulatory-exposure check against rag/policies/ -- not
    the model's own opinion of urgency (see the assignment's
    intake-triage worked example). Root causes already explored this
    run (including ones surfaced in an earlier round before the
    officer sent it back via 'needs_more_investigation') are threaded
    through state and pruned so a re-investigation round doesn't just
    repeat the same guess."""
    incident_id = state.get("incident_id")
    if not incident_id:
        raise TicketableError(
            "Cannot investigate: missing incident_id in state.",
            context={"state": state},
        )

    incident = tools.get(incident_id)
    if not incident:
        raise TicketableError(
            f"Safety incident record #{incident_id} not found in DB.",
            context={"incident_id": incident_id},
        )

    explored = list(state.get("explored_root_causes", []))

    result = investigate_incident(
        description=incident["Description"],
        severity=incident["Severity"],
        prior_notes=incident["InvestigationNotes"] or "",
        previously_explored=explored,
        call_llm=call_llm,
    )

    notes = (
        f"Root cause: {result.root_cause}. Corrective action: {result.corrective_action}. "
        f"Rationale: {result.rationale} "
        f"[LATS: {result.iterations} candidate(s) evaluated across {result.rounds} round(s), "
        f"{result.pruned_count} pruned, regulatory-grounding confidence={result.confidence:.2f}]"
    )
    tools.record_investigation_notes(
        incident_id,
        notes=notes,
        actor_id=state["employee_id"],
    )

    explored.append(result.root_cause)
    return {
        "explored_root_causes": explored,
        "regulator_report_recommended": result.regulator_report_recommended,
        "investigation_confidence": result.confidence,
    }


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

    recommendation = state.get("regulator_report_recommended")
    recommendation_txt = (
        f" LATS investigation recommends filing a regulator report "
        f"(confidence={state.get('investigation_confidence', 0):.2f})."
        if recommendation
        else " LATS investigation did not find clear regulatory exposure -- recommendation is advisory only."
    )

    reason = (
        f"Safety incident {incident['IncidentID']} (severity={incident['Severity']}), "
        f"investigation round {round_no}: {incident['InvestigationNotes'] or '(no notes yet)'}."
        f"{recommendation_txt} "
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
    """CONSTRAINED ReAct (C3): the LLM reasons (Thought) about whether
    the investigation record actually supports filing, then must
    choose from a closed whitelist. Only 'submit_regulator_report'
    touches the DB, and it is the ONE tool this node exposes --
    tools.submit_regulator_report(); no free-form DB access. Matches
    the constrained-ReAct guarantee used in A3's
    file_change_order_node(). An 'incomplete' verdict, or any
    non-whitelisted action, is ticketed rather than silently filed --
    filing a regulator report is irreversible, so the model must
    explicitly justify readiness before the whitelisted action runs.
    """
    incident_id = state.get("incident_id")
    if not incident_id:
        raise TicketableError(
            "Cannot file regulator report: missing incident_id in state.",
            context={"state": state},
        )

    incident = tools.get(incident_id)
    if not incident:
        raise TicketableError(
            f"Safety incident record #{incident_id} not found in DB.",
            context={"incident_id": incident_id},
        )

    prompt = _build_regulator_react_prompt(incident)
    llm_response = call_llm(prompt, temperature=0.1)
    thought, action = _parse_regulator_react_response(llm_response)

    ALLOWED_ACTIONS = {"submit_regulator_report", "flag_incomplete_for_review"}
    if action not in ALLOWED_ACTIONS:
        raise TicketableError(
            f"Constrained ReAct produced non-whitelisted action '{action}'. "
            f"Allowed: {ALLOWED_ACTIONS}",
            context={"thought": thought, "action": action, "incident_id": incident_id},
        )

    if action == "flag_incomplete_for_review":
        raise TicketableError(
            f"Regulator filing flagged incomplete by constrained ReAct: {thought}",
            context={"incident_id": incident_id, "thought": thought},
        )

    # action == "submit_regulator_report"
    case_number = f"REG-{incident_id}-R{incident['InvestigationRound']}"
    tools.submit_regulator_report(incident_id, case_number, state["employee_id"])
    return {"filing_action": "submitted", "regulator_case_number": case_number, "thought": thought}


def _build_regulator_react_prompt(incident: Dict[str, Any]) -> str:
    return f"""You are a regulator filing officer for a construction company.
You must decide whether the investigation record for this safety
incident supports filing a regulator report now, or whether it is too
incomplete to file.
Reason step-by-step (Thought), then choose exactly one Action.

Safety Incident:
- ID: {incident['IncidentID']}
- Severity: {incident['Severity']}
- Status: {incident['Status']}
- Investigation Notes: {incident['InvestigationNotes'] or '(none yet)'}

Rules:
1. If there are investigation notes on record describing findings, submit the report.
2. If there are no investigation notes on record, flag it for review instead of filing.
3. You may ONLY choose from: submit_regulator_report, flag_incomplete_for_review.
4. Respond in this exact format:
   Thought: <your reasoning>
   Action: <submit_regulator_report or flag_incomplete_for_review>

EXAMPLE (submit):
Thought: Investigation findings are on record and support filing. Filing now.
Action: submit_regulator_report

EXAMPLE (flag):
Thought: There are no investigation notes on record yet. Flagging instead of filing an empty report.
Action: flag_incomplete_for_review

Do not add any other text."""


def _parse_regulator_react_response(response: str) -> tuple[str, str]:
    """Extracts Thought and Action from constrained ReAct output.
    Raises TicketableError if the format is invalid or action is missing."""
    thought_match = re.search(
        r"Thought:\s*(.+?)(?:\nAction:|$)", response, re.DOTALL | re.IGNORECASE
    )
    action_match = re.search(r"Action:\s*(\w+)", response, re.IGNORECASE)

    if not thought_match or not action_match:
        raise TicketableError(
            "Constrained ReAct response missing Thought or Action line.",
            context={"llm_response": response[:500]},
        )

    thought = thought_match.group(1).strip()
    action = action_match.group(1).strip().lower()
    return thought, action


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
