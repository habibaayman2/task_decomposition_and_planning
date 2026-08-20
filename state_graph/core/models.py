"""
Shared state model for every state_graph agent (change_order,
equipment_recovery, safety_incident).

These are read-model conveniences over the rows in db/state_graph_schema.sql
-- the SQLite tables are the actual source of truth (see checkpoint_store.py).
Nothing here does I/O; it's just the vocabulary the rest of state_graph/
is written against, so all three graphs (and the platform backend that
reads them) agree on shape.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class RunStatus(str, Enum):
    """Every status a StateGraphRuns.Status column can hold.

    RUNNING       -- actively executing, or ready to resume immediately
    PAUSED_HITL   -- stopped at a HITL node, waiting on an admin decision
    TICKET_OPEN   -- stopped because a node raised an unplanned exception
    COMPLETED     -- reached the graph's END node
    """
    RUNNING = "running"
    PAUSED_HITL = "paused_hitl"
    TICKET_OPEN = "ticket_open"
    COMPLETED = "completed"


@dataclass
class GraphRun:
    """A snapshot of one run, as stored in StateGraphRuns."""
    run_id: str
    graph_name: str
    status: RunStatus
    current_node: str          # node to execute NEXT (not yet executed if paused/ticket)
    state: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HITLTask:
    """A pending-or-resolved human-in-the-loop request, as stored in HITLTasks."""
    task_id: int
    run_id: str
    node_name: str
    reason: str
    payload: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"            # 'pending' | 'resolved'
    decision: Optional[str] = None
    resolved_by: Optional[int] = None  # Employees.EmployeeID


@dataclass
class Ticket:
    """A failure ticket, as stored in Tickets. Distinct from HITLTask:
    a ticket is an UNPLANNED failure (a tool call errored, a schema
    validation failed) -- never something the graph paused for on
    purpose."""
    ticket_id: int
    run_id: str
    node_name: str
    error_message: str
    status: str = "open"               # 'open' | 'investigating' | 'resolved'
    resolution: Optional[str] = None
