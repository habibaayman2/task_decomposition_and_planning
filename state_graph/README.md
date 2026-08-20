# state_graph/ — shared core + your graphs

This is the Day 1 deliverable for C1. `state_graph/core/` is done, tested,
and unblocks A3 (`change_order/`) and B3 (`equipment_recovery/`) for Day 2.
This doc is the contract: what's here, how to build a node, how HITL and
tickets actually work under the hood, and what to bring back to Day 2's
HITL/ticket contract review (C2).

## One-time setup (do this before Day 2)

```bash
python -m db.migrate_state_graph
```

Adds four tables (`StateGraphRuns`, `StateGraphCheckpoints`, `HITLTasks`,
`Tickets`) to the existing `db/procurement.db` — same database the rest
of the system uses, nothing parallel. Safe to re-run.

Then try the crash/resume proof yourself:

```bash
python -m state_graph.demo_crash_resume run demo-1
# wait for "[step_two] ... kill me now", then Ctrl+C
python -m state_graph.demo_crash_resume run demo-1
# watch it resume from step_two, not re-run step_one
```

## Building your graph on the core

Your `graph.py` builds a `StateGraph`, your `nodes.py` writes the node
functions, your `tools.py` holds whatever MCP/DB calls those nodes need.

```python
from state_graph.core import StateGraph, END, require_hitl, HITLPause

def diagnose_node(state: dict) -> dict:
    # do whatever work this node does, return a partial dict --
    # it gets merged into state, not replace it
    return {"diagnosis": "..."}

def signoff_node(state: dict) -> dict:
    # HITL-gated: pauses the first time through, resumes with the
    # admin's decision the second time (see "How HITL actually works" below)
    decision = require_hitl(
        state,
        reason="Rental cost exceeds $5,000 -- needs PM sign-off",
        payload={"cost": state["estimated_cost"]},
    )
    return {"signoff": decision}

def route_after_signoff(state: dict) -> str:
    return "book_rental" if state["signoff"] == "approved" else "reject"

g = StateGraph("equipment_recovery")
g.add_node("diagnose", diagnose_node)
g.add_node("signoff", signoff_node)
g.add_node("book_rental", book_rental_node)
g.add_node("reject", reject_node)
g.set_entry("diagnose")
g.add_edge("diagnose", "signoff")
g.add_conditional_edge("signoff", route_after_signoff)
g.add_edge("book_rental", END)
g.add_edge("reject", END)
```

Running/resuming is the SAME call either way — the core figures out
whether `run_id` is new or being resumed:

```python
from state_graph.core import CheckpointStore

store = CheckpointStore()
g.run(run_id, initial_state={"equipment_id": 42}, store=store)
```

## How HITL actually works

1. A node calls `require_hitl(state, reason, payload)`.
2. First time through: state has no decision yet, so it raises `HITLPause`.
   The runner catches this, checkpoints, and writes a `HITLTasks` row
   (`Status='pending'`) — this is what your platform's admin inbox
   (A6) queries and displays.
3. Admin acts on the platform. The backend calls
   `store.resolve_hitl_task(task_id, decision, resolved_by)`. This marks
   the task resolved AND merges the decision into the run's state under
   the key `"hitl_decision"` (configurable via `decision_key=` if a
   node needs a different key), then sets the run back to `running`.
4. Backend calls `g.run(run_id)` again. The SAME node re-executes —
   nothing before it re-runs — `require_hitl()` now finds the decision
   in state and returns it instead of pausing, and the node proceeds.

**Bring to C2:** whether one `decision_key` per run is enough for your
graph, or whether a node needs a graph-specific key (e.g. if a single
run could hit two different HITL nodes before finishing).

## How tickets actually work

Nodes don't do anything special for tickets — that's the point. If a
node raises ANY exception that isn't `HITLPause` (a tool call failing,
a `KeyError`, whatever), the runner catches it, checkpoints at that
node, and opens a `Tickets` row (`Status='open'`) automatically. This
is the code-level way a grader can tell "the graph paused for a reason
you named" apart from "something broke unexpectedly."

To resolve one: `store.resolve_ticket(ticket_id, resolution)` sets the
run back to `running`; `g.run(run_id)` re-executes the SAME failed
node. If the fix requires changing something in state first (like the
demo in `checkpoint_store.py`'s docstring shows), load the run, mutate
state, `save_checkpoint()`, then resume.

## What's NOT done yet (Day 2/3, owned by A/B/C individually)

- Your actual graph topology and node logic
- Your two LLM-call additions wired into specific nodes
- Wiring `HITLTasks`/`Tickets` reads into the admin platform (A6)
- Whatever graph-specific `decision_key`s you need beyond the default
