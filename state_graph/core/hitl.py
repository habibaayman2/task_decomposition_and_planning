"""
Human-in-the-loop pausing.

HITL is an EXPECTED pause for a decision the graph isn't allowed to
make on its own -- an amount above a threshold, an action that
contradicts a stated policy, a confidence score below a bar you can
defend. It is a deliberate, named exception (HITLPause), which is what
lets graph_base.py's runner tell HITL pauses apart from genuine
unplanned failures (see tickets.py) -- the grading rubric explicitly
wants these two code paths to be distinguishable.
"""

from typing import Any, Dict, Optional


class HITLPause(Exception):
    """Raise this inside a node function to pause the graph for a human
    decision. The runner (graph_base.py) catches ONLY this exception
    type as a HITL pause -- anything else it catches is treated as an
    unplanned failure and becomes a ticket instead.
    """

    def __init__(self, reason: str, payload: Optional[Dict[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.payload = payload or {}


def require_hitl(
    state: Dict[str, Any],
    reason: str,
    payload: Optional[Dict[str, Any]] = None,
    decision_key: str = "hitl_decision",
) -> Any:
    """Call this at the top of any HITL-gated node.

    First call (no decision yet in state): raises HITLPause, which the
    runner catches, checkpoints, and turns into a HITLTasks row for an
    admin to see on the platform.

    On resume, after an admin has called CheckpointStore.resolve_hitl_task(),
    the SAME node re-executes (nothing before it re-runs) and this
    function returns the admin's decision instead of raising, so the
    node can branch on it and continue.

    Example, inside a node function:

        def client_signoff_node(state):
            decision = require_hitl(
                state,
                reason="Change order exceeds $10,000 -- needs client sign-off",
                payload={"change_order_id": state["change_order_id"],
                         "amount": state["amount"]},
            )
            if decision == "approved":
                return {"signoff": "approved"}
            return {"signoff": "rejected"}

    CYCLE SAFETY (finalized Day 2, after a bug found reviewing A3's
    change_order graph): if a graph can revisit a HITL node more than
    once in the SAME run -- any real cycle, e.g. "sent back for more
    review" -- do NOT rely on the default decision_key. Once a key is
    written into state it stays there for the rest of the run (state
    only ever merges, see graph_base.py); a revisit that reuses the
    same key will see the OLD decision already present and skip the
    pause entirely instead of asking a human again. That silently fakes
    HITL on the second cycle -- exactly what the project brief
    prohibits.

    Scope decision_key to something that is unique to the SPECIFIC
    pause, not just the run: an id that changes across cycles (e.g.
    safety_incident's `f"hitl_decision_incident{id}_round{round}"`,
    keyed on InvestigationRound, NOT on a resettable per-record Version
    number -- a Version that resets to 1 on each new record collides
    across different records). resolve_hitl_task() must be called with
    that SAME decision_key (its default only matches an unscoped call);
    the payload passed to open_hitl_task() should carry decision_key so
    whichever platform route resolves the task can read it back.
    """
    if decision_key in state:
        return state[decision_key]
    raise HITLPause(reason, payload)
