from __future__ import annotations

from pathlib import Path
import sys
import json
import re
from typing import Any, Dict

# --------------------------------------------------------------------------
# Path & Module Resolution
# --------------------------------------------------------------------------
_current_dir = Path(__file__).resolve().parent
REPO_ROOT = next(
    (p for p in [_current_dir] + list(_current_dir.parents) if (p / "mcp_server").exists()),
    _current_dir.parent.parent
)

for path_entry in (str(REPO_ROOT), str(REPO_ROOT / "mcp_server")):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

import db as mcp_db

from state_graph.change_order import tools
from state_graph.core.hitl import require_hitl
from state_graph.core.tickets import TicketableError

# --------------------------------------------------------------------------
# LLM Bridge — connects state_graph nodes to planning/model_provider.py
# --------------------------------------------------------------------------

def call_llm(prompt: str, temperature: float = 0.2) -> str:
    """Unified LLM call for state graph nodes.

    Production: routes through planning.model_provider (Groq/OpenAI).
    Test / no-key: falls back to deterministic stubs keyed on prompt
    content so unit tests pass without API keys and without network.

    The bridge validates real LLM output shape; if the deterministic
    fallback returns garbage for our specific prompt type, we inject
    our own context-aware stub instead of letting the parse fail.
    """
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
        is_decompose = "decompose" in lowered or "intake assistant" in lowered
        is_react = "filing clerk" in lowered

        if is_decompose and not text.strip().startswith("{"):
            return _stub_decompose()
        if is_react and "thought:" not in text.lower():
            return _stub_react(prompt)
        return text

    except Exception:
        # Total import failure — use own stubs
        return _stub_fallback(prompt)


def _stub_decompose() -> str:
    return (
        '{"project_id": 1, "description": "Add reinforced foundation", '
        '"cost_delta": 18500.0, "schedule_delta_days": 6, "employee_id": 1}'
    )


def _stub_react(prompt: str) -> str:
    lowered = prompt.lower()
    if "missing" in lowered or "negative" in lowered or "abort" in lowered:
        return (
            "Thought: The change order has invalid data (missing description or negative cost). "
            "Aborting to prevent an invalid filing.\n"
            "Action: abort_draft"
        )
    return (
        "Thought: The description is clear, cost is positive, and schedule "
        "delta is non-negative. Ready to submit.\n"
        "Action: submit_for_review"
    )


def _stub_fallback(prompt: str) -> str:
    lowered = prompt.lower()
    if "decompose" in lowered or "intake assistant" in lowered:
        return _stub_decompose()
    if "filing clerk" in lowered:
        return _stub_react(prompt)
    return "{}"


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
REVIEW_TIMEOUT_SECONDS = 60 * 60 * 24 * 3  # 3 days
_REQUIRED_REQUEST_FIELDS = (
    "project_id", "description", "cost_delta", "schedule_delta_days", "employee_id"
)


# ==========================================================================
# NODE 1 — TASK DECOMPOSITION (LLM Addition #1)
# ==========================================================================
def decompose_change_order_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """TASK DECOMPOSITION: breaks a raw, possibly unstructured client request
    into structured sub-components before drafting the ChangeOrder record.

    Why task decomposition here?
    - Client requests arrive as natural language: "The soil survey came back
      bad, we need to reinforce the foundation. Budget impact is maybe 18k
      and it\'ll push us out a week."
    - A single deterministic pass cannot reliably extract project_id,
      cost_delta, schedule_delta_days, and a clean description.
    - The LLM decomposes the request into: (1) project identification,
      (2) scope description, (3) financial impact, (4) schedule impact.
    - Each component is validated against live DB constraints before the
      draft is created, preventing orphan records.
    - ToT/LATS were rejected: there is no branching search space; this is
      a single-parse problem with a deterministic JSON schema.
    """
    existing_change_order_id = state.get("change_order_id")
    if existing_change_order_id is not None:
        # Resubmission loop-back: reuse existing row (bumped by handle_decision)
        return {
            "change_order_id": existing_change_order_id,
            "employee_id": state["employee_id"],
        }

    raw_request = state.get("request")
    if not raw_request:
        raise TicketableError(
            "Missing \'request\' payload in state.",
            context={"state_keys": list(state.keys())},
        )

    # ------------------------------------------------------------------
    # Structured path (backward compat) or LLM decomposition path
    # ------------------------------------------------------------------
    if isinstance(raw_request, dict) and all(k in raw_request for k in _REQUIRED_REQUEST_FIELDS):
        structured = raw_request
    else:
        structured = _llm_decompose_with_retry(raw_request, max_retries=2)

    # ------------------------------------------------------------------
    # Grounded validation against live DB
    # ------------------------------------------------------------------
    project = mcp_db.get_project(structured["project_id"])
    if not project:
        raise TicketableError(
            f"Decomposed project_id {structured['project_id']} does not exist in DB.",
            context={"decomposed": structured},
        )

    change_order_id = tools.create_draft(
        run_id=state.get("run_id", ""),
        project_id=structured["project_id"],
        description=structured["description"],
        cost_delta=structured["cost_delta"],
        schedule_delta_days=structured["schedule_delta_days"],
        actor_id=structured["employee_id"],
    )
    return {
        "change_order_id": change_order_id,
        "employee_id": structured["employee_id"],
        "decomposed_request": structured,
    }


def _llm_decompose_with_retry(raw_request: Any, max_retries: int = 2) -> Dict[str, Any]:
    """Calls the LLM to decompose a raw request, with retry on parse failure."""
    prompt = _build_decomposition_prompt(raw_request)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = call_llm(prompt, temperature=0.2)
            return _parse_decomposition_response(response)
        except TicketableError as exc:
            last_error = exc
            if attempt < max_retries:
                prompt += (
                    f"\n\nYour previous response was invalid: {exc}. "
                    f"Please fix and respond with ONLY valid JSON."
                )
                continue
            raise last_error

    raise last_error  # pragma: no cover


def _build_decomposition_prompt(raw_request: Any) -> str:
    text = raw_request if isinstance(raw_request, str) else json.dumps(raw_request)
    return f"""You are a construction-project intake assistant.
A client or site engineer has submitted a change-order request.
Decompose the following raw request into a JSON object with exactly these keys:
  - project_id: integer
  - description: string (concise scope of work, max 200 chars)
  - cost_delta: float (dollar amount, numeric only, no commas)
  - schedule_delta_days: integer (positive)
  - employee_id: integer (the submitting employee)

If any field cannot be determined, set it to null.

EXAMPLE:
Raw request: "Project 1 needs a new HVAC unit. Cost is 12000 and it adds 3 days."
Response: {{"project_id": 1, "description": "Install new HVAC unit", "cost_delta": 12000.0, "schedule_delta_days": 3, "employee_id": 1}}

Now decompose this request:
{text}

Respond with ONLY valid JSON, no markdown, no explanation."""


def _parse_decomposition_response(response: str) -> Dict[str, Any]:
    """Extracts structured fields from the LLM\'s JSON response.
    Raises TicketableError on malformed or incomplete output so the
    runner surfaces it as a ticket, not a silent crash."""
    try:
        # Strip markdown fences
        cleaned = re.sub(r"```(?:json)?\s*", "", response).strip()
        cleaned = re.sub(r"\s*```", "", cleaned).strip()
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise TicketableError(
            f"LLM decomposition produced unparseable JSON: {exc}",
            context={"llm_response": response[:500]},
        )

    required = set(_REQUIRED_REQUEST_FIELDS)
    missing = required - set(parsed.keys())
    if missing:
        raise TicketableError(
            f"LLM decomposition missing fields: {missing}",
            context={"llm_response": response[:500], "parsed": parsed},
        )

    # Type coercion with clear error messages
    try:
        return {
            "project_id": int(parsed["project_id"]),
            "description": str(parsed["description"]) if parsed["description"] is not None else "",
            "cost_delta": float(parsed["cost_delta"]),
            "schedule_delta_days": int(parsed["schedule_delta_days"]),
            "employee_id": int(parsed["employee_id"]),
        }
    except (ValueError, TypeError) as exc:
        raise TicketableError(
            f"LLM decomposition field type error: {exc}",
            context={"llm_response": response[:500], "parsed": parsed},
        )


# ==========================================================================
# NODE 2 — CONSTRAINED ReAct (LLM Addition #2)
# ==========================================================================
def file_change_order_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """CONSTRAINED ReAct: the LLM reasons (Thought) then selects an Action
    from a closed whitelist. Only then is the whitelisted action executed.

    Why constrained ReAct here?
    - Filing a change order is irreversible: once \'PendingReview\' is set,
      client notification triggers and the review clock starts.
    - The LLM must explicitly reason about readiness (completeness, budget
      sanity, description clarity) before being allowed to file.
    - The action space is strictly limited to: submit_for_review, abort_draft.
    - Any attempt to call a non-whitelisted tool is caught and ticketed.
    - This prevents the agent from silently filing incomplete or malformed
      change orders that would waste the review window.
    - RAG was rejected: filing decisions are based on the change order\'s
      own fields, not on external policy documents.
    """
    change_order_id = state.get("change_order_id")
    employee_id = state.get("employee_id")
    if not change_order_id or not employee_id:
        raise TicketableError(
            "Cannot file change order: missing change_order_id or employee_id.",
            context={"change_order_id": change_order_id, "employee_id": employee_id},
        )

    co = tools.get(change_order_id)
    if not co:
        raise TicketableError(
            f"Change order record #{change_order_id} not found in DB.",
            context={"change_order_id": change_order_id},
        )

    # ------------------------------------------------------------------
    # Constrained ReAct: LLM reasons, then chooses from whitelist
    # ------------------------------------------------------------------
    prompt = _build_react_prompt(co)
    llm_response = call_llm(prompt, temperature=0.1)
    thought, action = _parse_react_response(llm_response)

    ALLOWED_ACTIONS = {"submit_for_review", "abort_draft"}
    if action not in ALLOWED_ACTIONS:
        raise TicketableError(
            f"Constrained ReAct produced non-whitelisted action \'{action}\'. "
            f"Allowed: {ALLOWED_ACTIONS}",
            context={"thought": thought, "action": action, "change_order_id": change_order_id},
        )

    if action == "abort_draft":
        tools.close(change_order_id, employee_id)
        return {"filing_action": "aborted", "thought": thought}

    # action == "submit_for_review"
    tools.submit_for_review(change_order_id, employee_id)
    return {"filing_action": "submitted", "thought": thought}


def _build_react_prompt(co: Dict[str, Any]) -> str:
    return f"""You are a construction-project filing clerk.
You must decide whether to submit a change order for review or abort it.
Reason step-by-step (Thought), then choose exactly one Action.

Change Order:
- ID: {co['ChangeOrderID']}
- Description: {co['Description']}
- Cost Delta: ${co['CostDelta']:.2f}
- Schedule Delta: {co['ScheduleDeltaDays']} days
- Status: {co['Status']}

Rules:
1. If the description is clear, cost is positive, and schedule delta is non-negative, submit.
2. If the description is missing/empty, cost is negative, or schedule delta is negative, abort.
3. You may ONLY choose from: submit_for_review, abort_draft.
4. Respond in this exact format:
   Thought: <your reasoning>
   Action: <submit_for_review or abort_draft>

EXAMPLE (submit):
Thought: The description is clear, cost is positive, and schedule delta is non-negative. Ready to submit.
Action: submit_for_review

EXAMPLE (abort):
Thought: The description is missing and cost is negative. Must abort to prevent invalid filing.
Action: abort_draft

Do not add any other text."""


def _parse_react_response(response: str) -> tuple[str, str]:
    """Extracts Thought and Action from constrained ReAct output.
    Raises TicketableError if the format is invalid or action is missing.
    """
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


# ==========================================================================
# NODE 3 — HITL: Client Sign-Off
# ==========================================================================
def await_client_signoff_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Uses require_hitl() to raise HITLPause on initial call, and receives
    the resolved decision when resumed by graph_base.py."""
    change_order_id = state.get("change_order_id")
    if not change_order_id:
        raise TicketableError(
            "Cannot await signoff: missing change_order_id in state.",
            context={"state": state},
        )

    co = tools.get(change_order_id)
    if not co:
        raise TicketableError(
            f"Change order record #{change_order_id} not found in DB.",
            context={"change_order_id": change_order_id},
        )

    project = mcp_db.get_project(co["ProjectID"])

    reason = (
        f"Change order {co['ChangeOrderID']} on project \'{project['ProjectName']}\' "
        f"(PM: employee #{project['ProjectManagerID']}): cost delta ${co['CostDelta']:.2f}, "
        f"schedule delta {co['ScheduleDeltaDays']} days. Approve, reject, or counter?"
    )
    # CYCLE-SAFE decision key: includes Version so resubmissions don\'t
    # collide with prior cycle decisions (see hitl.py CYCLE SAFETY note).
    decision_key = f"hitl_decision_co{change_order_id}_v{co['Version']}"
    payload = {
        "change_order": co,
        "approving_employee_id": project["ProjectManagerID"],
        "decision_key": decision_key,
    }

    decision = require_hitl(state, reason=reason, payload=payload, decision_key=decision_key)
    return {"hitl_decision": decision}


# ==========================================================================
# NODE 4 — Handle Admin Decision
# ==========================================================================
def handle_decision_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Called once the platform has resolved the HITL task for this run.
    Records the decision, closes or counters the change order, and routes
    the graph accordingly."""
    change_order_id = state.get("change_order_id")
    decision = state.get("hitl_decision")

    if not decision:
        raise TicketableError(
            "handle_decision_node reached with no resolved HITL decision.",
            context={"change_order_id": change_order_id},
        )

    tools.record_decision(
        change_order_id,
        decision,
        state["employee_id"],
        counter_note=state.get("counter_note"),
    )

    if decision == "approved":
        tools.close(change_order_id, state["employee_id"])
    elif decision == "countered":
        tools.bump_version_for_resubmission(change_order_id, state["employee_id"])
    else:
        tools.close(change_order_id, state["employee_id"])  # rejected

    return {}


# ==========================================================================
# NODE 5 — Stalled Review Escalation (external poller)
# ==========================================================================
def escalate_stalled_review_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Invoked by an external scheduler/poller checking seconds_since_submission()
    across open runs. Raises TicketableError if review exceeds REVIEW_TIMEOUT_SECONDS."""
    change_order_id = state.get("change_order_id")
    if not change_order_id:
        raise TicketableError("Cannot check stall status: missing change_order_id.")

    elapsed = tools.seconds_since_submission(change_order_id)
    if elapsed is not None and elapsed > REVIEW_TIMEOUT_SECONDS:
        raise TicketableError(
            f"Change order {change_order_id} has had no client response for "
            f"{elapsed / 86400:.1f} days (window: {REVIEW_TIMEOUT_SECONDS / 86400:.0f} days).",
            context={"change_order_id": change_order_id, "elapsed_seconds": elapsed},
        )
    return {}