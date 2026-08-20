"""
Wires the equipment_recovery nodes into a StateGraph.

    report_breakdown -> diagnose_issue -> evaluate_options -> approval_gate
                                                 ^                  |
                                                 |   (rejected)     |
                                                 +------------------+
                                                                    | (approved / auto_approved)
                                                                    v
                                                        execute_recovery_action -> END

approval_gate itself decides (via approval_gate's own HITL threshold
check) whether a human is even paused for -- see nodes.py's docstring
for why the threshold is the project's real RemainingBudget rather
than a fixed number. The cycle back to evaluate_options only happens
on an explicit rejection, which is the "real branch that depends on
something outside the model's control" this problem needed to earn a
state graph instead of a linear script.
"""

from state_graph.core.graph_base import StateGraph, END
from state_graph.equipment_recovery.nodes import (
    report_breakdown,
    diagnose_issue,
    evaluate_options,
    approval_gate,
    execute_recovery_action,
)


def _route_after_approval(state: dict) -> str:
    status = state.get("approval_status")
    if status == "rejected":
        return "evaluate_options"          # cycle: try a different option
    return "execute_recovery_action"       # "approved" or "auto_approved"


def build_equipment_recovery_graph() -> StateGraph:
    graph = StateGraph("equipment_recovery")

    graph.add_node("report_breakdown", report_breakdown)
    graph.add_node("diagnose_issue", diagnose_issue)
    graph.add_node("evaluate_options", evaluate_options)
    graph.add_node("approval_gate", approval_gate)
    graph.add_node("execute_recovery_action", execute_recovery_action)

    graph.set_entry("report_breakdown")

    graph.add_edge("report_breakdown", "diagnose_issue")
    graph.add_edge("diagnose_issue", "evaluate_options")
    graph.add_edge("evaluate_options", "approval_gate")
    graph.add_conditional_edge("approval_gate", _route_after_approval)
    graph.add_edge("execute_recovery_action", END)

    return graph