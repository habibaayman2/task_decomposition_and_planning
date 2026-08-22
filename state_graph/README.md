# State Graph Agents

> **Purpose:** Persistent, recoverable, human-gated operational workflows for IronBridge Construction.

This directory contains the state-graph layer of the IronBridge AI platform. Unlike the planning and RAG agents, which complete their work in a single pass, the agents here are designed for problems that genuinely span multiple interactions, multiple days, and multiple decision points. They pause when they need human judgment. They recover when they encounter unexpected failures. They resume from exactly where they left off, even if the server was restarted in between.

---

## Design Philosophy

A state graph is not a DAG. A DAG is acyclic and finite by design: it starts, it runs to a topological end, and it is done. A state graph can loop back to a state it has already visited. It can sit in a waiting state indefinitely until something outside the model happens. It needs a persisted checkpoint at every meaningful transition, not just an execution log written after the fact.

The three problems modeled here were chosen because each one has:
- A genuine wait — something the agent cannot control and cannot predict
- A genuine branch — a decision that depends on something outside the model's own reasoning
- A genuine cost to losing progress — restarting from scratch would waste real operational time or real money

If a problem could be solved identically by a for-loop wrapped in a try/except, it does not belong here.

---

## The Three Agents

### Change-Order Negotiation

**Directory:** `change_order/`  
**Trigger:** A client requests a scope change to an active project.

When a change order arrives, the agent must assess the impact on materials, labor, and schedule; estimate the cost; check the remaining budget; and obtain approval before executing. This is not a single-turn conversation. The client may come back with a counter-offer. The project manager may reject the cost. A material shortage discovered mid-assessment may force a different approach entirely.

**State held across the run:**
- Original project baseline (budget, timeline, materials committed)
- Change request details (scope, client contact, request date)
- Impact assessment (material deltas, labor deltas, schedule shift)
- Cost estimate and budget headroom check
- Approval status (pending, approved, rejected, counter-offered)
- Revised schedule and material allocations

**Why it pauses:**
- **HITL:** Any cost estimate that exceeds 15% of the remaining budget requires project-manager sign-off before the agent is allowed to present the figure to the client.
- **Wait:** After presenting the estimate to the client, the graph enters an `awaiting_client_response` state and only transitions when the platform receives the client's reply.
- **Failure:** If the materials database returns an unexpected schema during impact assessment, the node fails, checkpoints, and opens a ticket.

**Techniques:**
- **Task Decomposition** — The `assess_impact` node decomposes the change into material, labor, and schedule sub-assessments, each verified against live database constraints.
- **Constrained ReAct** — The `propose_revision` node executes only whitelisted actions (update schedule, reserve materials, notify contractor) and validates each against the current project state before committing.

---

### Equipment Recovery

**Directory:** `equipment_recovery/`  
**Trigger:** A critical piece of equipment is reported broken or unavailable.

When equipment fails, every hour of downtime costs the project money. The agent must diagnose the failure, search for replacement options (rental, repair, subcontract, schedule shift), price each option, and execute the best one. But "best" depends on time-to-recovery, cost, and whether the project manager is willing to spend unbudgeted money.

**State held across the run:**
- Equipment identity, failure description, and criticality flag
- Diagnosis (self-diagnosis vs. technician-required)
- Alternative options explored (vendor, cost, availability, delivery time)
- Scored ranking of options
- Selected option and booking confirmation
- Recovery completion status

**Why it pauses:**
- **HITL:** Any rental or repair cost exceeding $5,000 requires project-manager approval. The graph pauses, opens a task on the platform, and resumes only after the manager acts.
- **Wait:** After requesting a technician dispatch, the graph waits in `awaiting_technician_report` until the technician's assessment arrives via the platform.
- **Failure:** If the rental vendor API is unreachable, the search node fails and opens a ticket rather than returning a fake "no results" response.

**Techniques:**
- **Tree of Thoughts** — The `explore_options` node generates multiple recovery strategies (rent from Vendor A, rent from Vendor B, repair in-house, subcontract the work) and evaluates each against a scoring function that weights cost, time, and reliability.
- **Constrained ReAct** — The `execute_recovery` node books rentals or calls repair services only through whitelisted MCP tools, and validates that the selected option is still available and within budget before committing.

---

### Safety Incident Response

**Directory:** `safety_incident/`  
**Trigger:** A safety incident is reported on an active job site.

Safety incidents have regulatory consequences. The agent must classify severity, notify the safety officer and relevant authorities, schedule an inspection, and recommend corrective actions. Some actions — like ordering a work stoppage — have massive operational cost and must never be taken without human confirmation.

**State held across the run:**
- Incident report (location, time, description, reporter, photos)
- Severity classification (low, medium, high, critical)
- Notifications sent (safety officer, site supervisor, regulatory body)
- Inspection scheduled (date, inspector assigned)
- Corrective actions recommended and approved
- Work-stoppage status (not applicable, requested, approved, lifted)

**Why it pauses:**
- **HITL:** Any recommendation that includes a work stoppage or site evacuation requires safety-officer approval. The graph pauses and opens a task with the full incident context and recommended actions.
- **Wait:** After scheduling an inspection, the graph waits in `awaiting_inspection_results` until the inspector submits their report through the platform.
- **Failure:** If the severity classifier returns an unparseable response, the node fails and opens a ticket rather than guessing the severity.

**Techniques:**
- **LATS (Language Agent Tree Search)** — The `plan_response` node searches over candidate response orderings (notify first then inspect, inspect first then notify, stoppage first then inspect) and scores each path against a real severity rubric and regulatory checklist. Failed paths are pruned with verbal reflection.
- **Constrained ReAct** — The `execute_actions` node performs only whitelisted low-severity actions automatically. Any action that would stop work or evacuate is gated behind the HITL node.

---

## Shared Core

The `core/` directory contains the infrastructure shared by all three agents. It is not agent-specific logic; it is the engine that makes stateful execution possible.

### `checkpoint_store.py`

Durable checkpointing to SQLite. Every meaningful transition writes the full state to the database. The store supports:
- `save_checkpoint(run_id, state, current_node)` — persists state after a node completes
- `load_checkpoint(run_id)` — restores the most recent state
- `list_checkpoints(run_id)` — audit trail of all transitions

Crash recovery is built on this store. Kill the process mid-run, restart it, and `graph_base.py` will load the last checkpoint and resume from the next node.

### `graph_base.py`

The state-graph execution engine. Key behaviors:
- Nodes receive the current state dict and return a partial dict that is merged in.
- Conditional edges route to the next node based on state.
- The `END` sentinel terminates the run.
- The runner catches `HITLPause` and exceptions differently, producing HITL tasks and tickets respectively.

### `hitl.py`

Human-in-the-loop primitives. The `require_hitl()` function:
- On first execution: raises `HITLPause`, which the runner converts into a pending HITL task.
- On resume: finds the administrator's decision in state and returns it, allowing the node to proceed.

The decision is stored under a configurable key (default: `hitl_decision`) so that multiple HITL nodes in the same run do not collide.

### `tickets.py`

Failure ticket primitives. When a node raises any exception other than `HITLPause`, the runner:
- Saves a checkpoint at the failed node
- Creates a ticket with status `open`
- Sets the run status to `failed`

To resolve: `store.resolve_ticket(ticket_id, resolution)` sets the run back to `running`. The next `g.run(run_id)` re-executes the same failed node.

### `models.py`

Shared Pydantic models for run state, checkpoint records, HITL tasks, and tickets.

---

## How to Build a New Graph

```python
from state_graph.core import StateGraph, END, require_hitl, CheckpointStore

def diagnose_node(state: dict) -> dict:
    # Do work, return partial state updates
    return {"diagnosis": "equipment_failure"}

def approval_node(state: dict) -> dict:
    # HITL-gated: pauses first time, resumes with decision second time
    decision = require_hitl(
        state,
        reason="Estimated repair cost exceeds $5,000 threshold",
        payload={"cost": state["estimated_cost"], "equipment": state["equipment_id"]},
    )
    return {"approval": decision}

def route_after_approval(state: dict) -> str:
    return "book_repair" if state.get("approval") == "approved" else "reject"

def book_repair_node(state: dict) -> dict:
    # Constrained ReAct: whitelist + validation
    return {"booking_confirmed": True}

def reject_node(state: dict) -> dict:
    return {"status": "rejected"}

# Build the graph
g = StateGraph("equipment_recovery")
g.add_node("diagnose", diagnose_node)
g.add_node("approval", approval_node)
g.add_node("book_repair", book_repair_node)
g.add_node("reject", reject_node)
g.set_entry("diagnose")
g.add_edge("diagnose", "approval")
g.add_conditional_edge("approval", route_after_approval)
g.add_edge("book_repair", END)
g.add_edge("reject", END)

# Run (or resume) — the same call handles both
store = CheckpointStore()
g.run("run-123", initial_state={"equipment_id": 42}, store=store)
```

---

## Testing Crash Recovery

```bash
# Run the crash-resume demo
python -m state_graph.demo_crash_resume run demo-1
# Wait for the "kill me now" message, then press Ctrl+C
python -m state_graph.demo_crash_resume run demo-1
# The second invocation resumes from the interrupted node
```

---

## Integration with the Platform

HITL tasks and tickets are not console print statements. They are database rows that the platform's admin panel queries and displays:

- **HITL Inbox:** Queries the `HITLTasks` table for `Status='pending'`. Displays the reason, payload, and full checkpointed state. Admin actions call `store.resolve_hitl_task()`.
- **Ticket Board:** Queries the `Tickets` table for `Status='open'`. Displays the exception, node name, and checkpointed state. Admin resolution calls `store.resolve_ticket()`.

Both tables live in the same `db/procurement.db` that the rest of the system uses.

---

## Directory Map

```
state_graph/
├── core/
│   ├── checkpoint_store.py      # Durable SQLite checkpointing
│   ├── graph_base.py            # StateGraph execution engine
│   ├── hitl.py                  # Human-in-the-loop primitives
│   ├── tickets.py               # Failure ticket primitives
│   └── models.py                # Shared Pydantic models
├── change_order/
│   ├── graph.py                 # Graph topology
│   ├── nodes.py                 # Node implementations
│   ├── tools.py                 # MCP/DB tool wrappers
│   └── demo.py                  # Standalone demo script
├── equipment_recovery/
│   ├── graph.py                 # Graph topology
│   ├── nodes.py                 # Node implementations
│   ├── tools.py                 # MCP/DB tool wrappers
│   └── tot.py                   # Tree-of-Thoughts scoring
├── safety_incident/
│   ├── graph.py                 # Graph topology
│   ├── nodes.py                 # Node implementations
│   ├── tools.py                 # MCP/DB tool wrappers
│   └── lats.py                  # LATS search over response orderings
└── demo_crash_resume.py         # Crash-recovery proof script
```
