# Change-Order Negotiation Agent

> **Domain:** Client scope changes and contract amendments  
> **Agent Type:** Stateful graph with HITL gating and external waits  
> **Location:** `state_graph/change_order/`

---

## Problem

IronBridge Construction receives scope-change requests from clients throughout the lifecycle of a project. A single change order can affect materials, labor, equipment, schedule, and budget. The process of evaluating, pricing, approving, and executing a change order is not instantaneous. It involves:

- Querying current stock levels and supplier lead times
- Estimating labor deltas and contractor availability
- Checking the remaining budget against the estimated cost
- Presenting the estimate to the client and waiting for a response
- Obtaining internal approval if the cost exceeds policy thresholds
- Re-sequencing the project schedule once approved

A linear script that assumes all of this happens in one sitting will fail the moment the client takes a day to reply or the project manager is in a meeting.

---

## Graph Topology

```
[receive_request]
      ↓
[decompose_impact]  ← Task Decomposition
      ↓
[assess_materials] ──┐
[assess_labor]       ├── parallel assessment branches
[assess_schedule] ───┘
      ↓
[compute_cost]
      ↓
[budget_check] ──→ exceeds threshold? ──→ [hitl_approval] ──→ approved? ──→ [present_to_client]
      │                                                                                ↑
      └─→ within budget ───────────────────────────────────────────────────────────────┘
      ↓
[await_client_response]  ← external wait
      ↓
[client_replied?] ──→ counter-offer? ──→ [recompute_cost] ──→ loop back to [budget_check]
      │
      └─→ accepted ──→ [execute_change]  ← Constrained ReAct
      └─→ rejected ──→ [archive_rejection]
      ↓
[notify_stakeholders]
      ↓
[END]
```

---

## Nodes

### `receive_request`

Captures the change-order request from the user or platform. Validates that the project exists and is active.

### `decompose_impact`

Breaks the change request into material, labor, and schedule sub-assessments. Uses task decomposition to generate a verifiable plan for each dimension.

### `assess_materials`, `assess_labor`, `assess_schedule`

Parallel nodes that query the database for current stock, contractor availability, and schedule constraints. Each returns a delta against the baseline.

### `compute_cost`

Aggregates the deltas into a total cost estimate.

### `budget_check`

Compares the estimate to the remaining project budget. If the ratio exceeds 15%, routes to `hitl_approval`. Otherwise, proceeds directly to `present_to_client`.

### `hitl_approval`

Human-in-the-loop node. Pauses execution and opens a task on the platform for the project manager. The manager can approve, reject, or request revision. The graph resumes only after the manager acts.

### `present_to_client`

Formats the estimate into a client-facing proposal and sends it. Transitions to `await_client_response`.

### `await_client_response`

Waiting state. The graph checkpoints here and remains paused until the client's reply arrives through the platform. This wait can span hours or days.

### `client_replied?`

Conditional router. If the client accepts, proceeds to `execute_change`. If the client rejects, proceeds to `archive_rejection`. If the client counters, routes to `recompute_cost` and loops back.

### `execute_change`

Constrained ReAct node. Executes only whitelisted actions: update project budget, reserve materials, notify contractors, revise schedule. Each action is validated against live database state before committing.

### `archive_rejection`

Records the rejection reason and closes the run.

### `notify_stakeholders`

Sends final notifications to the project team.

---

## State Held Across the Run

| Key | Description | Populated By |
|:---|:---|:---|
| `project_id` | Target project | `receive_request` |
| `change_description` | Client's requested change | `receive_request` |
| `material_delta` | Added/removed materials | `assess_materials` |
| `labor_delta` | Added/removed labor hours | `assess_labor` |
| `schedule_shift` | Days added or removed | `assess_schedule` |
| `estimated_cost` | Total cost estimate | `compute_cost` |
| `budget_headroom` | Remaining budget after estimate | `budget_check` |
| `hitl_decision` | Manager's approval/rejection | `hitl_approval` (platform) |
| `client_response` | accept / reject / counter | `await_client_response` (platform) |
| `executed_actions` | List of committed changes | `execute_change` |

---

## HITL Conditions

- **Trigger:** Estimated cost exceeds 15% of remaining project budget
- **Approver:** Project Manager
- **Payload:** Project ID, current budget, estimated cost, breakdown by material/labor/schedule
- **Resume behavior:** If approved, proceed to `present_to_client`. If rejected, archive and close.

---

## Failure Modes (Tickets)

- Materials database returns unexpected schema during `assess_materials`
- Cost computation produces a non-numeric result
- Client response parsing fails (malformed reply)
- Schedule update tool errors during `execute_change`

In each case, the node fails, checkpoints, and opens a ticket. The run resumes from the same node once resolved.

---

## LLM Techniques

| Node | Technique | Why It Fits |
|:---|:---|:---|
| `decompose_impact` | Task Decomposition | The change request is inherently multi-dimensional. Decomposing into material, labor, and schedule ensures each dimension is assessed against the correct database tables and constraints. |
| `execute_change` | Constrained ReAct | The actions here have real side effects on the database. A whitelist of permissible actions (update budget, reserve stock, notify contractor) plus live validation prevents the agent from making unauthorized or inconsistent changes. |

---

## Files

| File | Purpose |
|:---|:---|
| `graph.py` | Graph topology, edges, conditional routing |
| `nodes.py` | Node implementations, state mutations |
| `tools.py` | MCP tool wrappers for project, budget, material, and contractor queries |
| `demo.py` | Standalone demo script showing a full change-order flow |
