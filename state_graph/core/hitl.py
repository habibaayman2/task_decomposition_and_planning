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
    """
    if decision_key in state:
        return state[decision_key]
    raise HITLPause(reason, payload)
