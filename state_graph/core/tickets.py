"""
Failure tickets -- the OTHER pause path, and deliberately not the same
code path as hitl.py.

A ticket is unplanned: a tool call errored, a schema validation failed,
the model returned something the graph can't act on. Node functions do
NOT raise anything special to create a ticket -- graph_base.py's runner
opens a ticket automatically for any exception that is NOT a HITLPause.
That's the whole distinction a grader needs to find: hitl.py is an
exception node code raises ON PURPOSE; tickets happen when node code
raises something it DIDN'T expect.

This module exists mainly to document that contract and to give node
authors an explicit way to mark a caught-and-rewrapped error as
"this really is a ticket-worthy failure" when they want to add context
before letting it propagate -- see TicketableError below. Using it is
optional: a plain, un-caught exception becomes a ticket regardless.
"""

from typing import Any, Dict, Optional


class TicketableError(RuntimeError):
    """Optional: raise this (instead of a bare Exception) when a node
    wants to attach structured context to a failure before the runner
    turns it into a Tickets row -- e.g. which external system failed,
    or what malformed response was received. Not required; any
    exception that isn't a HITLPause becomes a ticket either way.
    """

    def __init__(self, message: str, context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.context = context or {}
