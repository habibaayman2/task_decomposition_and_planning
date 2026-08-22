# Safety Incident Response Agent

> **Domain:** Job-site safety incidents and regulatory compliance  
> **Agent Type:** Stateful graph with HITL gating and external waits  
> **Location:** `state_graph/safety_incident/`

---

## Problem

Safety incidents on construction sites range from minor equipment scrapes to serious injuries requiring immediate regulatory notification. The response protocol is governed by company policy and local regulations, and the wrong sequence of actions can expose IronBridge to liability, fines, or operational shutdown.

The agent must:
- Classify incident severity using a real rubric, not the model's intuition
- Notify the correct parties in the correct order
- Schedule inspections within regulatory time windows
- Recommend corrective actions grounded in safety policy
- Obtain explicit approval before ordering work stoppages or evacuations

This cannot be a single-pass script because:
- Severity classification may need to wait for photo evidence or witness statements
- Inspections may be scheduled for the next business day
- Work-stoppage decisions have massive cost and must be made by a qualified safety officer

---

## Graph Topology

```
[receive_report]
      ↓
[classify_severity]
      ↓
[plan_response]  ← LATS (search over response orderings)
      ↓
[actions_include_stoppage?] ──→ yes ──→ [hitl_safety_officer] ──→ approved? ──→ [execute_actions]
      │                                                          ↑
      └─→ no ────────────────────────────────────────────────────┘
      ↓
[await_inspection]  ← external wait
      ↓
[inspection_complete?]
      ↓
[update_incident_record]
      ↓
[notify_regulatory_body]  (if severity >= high)
      ↓
[close_incident]
      ↓
[END]
```

---

## Nodes

### `receive_report`

Captures the incident report: location, time, description, reporter identity, and any attached photos or witness statements.

### `classify_severity`

Queries the safety policy database and classifies the incident as low, medium, high, or critical based on a structured rubric (injury type, equipment involved, environmental hazard, number of people affected).

### `plan_response`

LATS node. Searches over candidate response orderings:
- Notify safety officer → schedule inspection → recommend actions
- Schedule inspection → notify safety officer → recommend actions
- Immediate stoppage → notify all parties → schedule inspection
- Document only → notify supervisor → close

Each path is scored against the severity rubric and regulatory checklist. Failed paths are pruned with verbal reflection.

### `actions_include_stoppage?`

Conditional router. If the planned response includes a work stoppage or evacuation, routes to `hitl_safety_officer`. Otherwise, proceeds directly to `execute_actions`.

### `hitl_safety_officer`

Human-in-the-loop node. Opens a task on the platform for the safety officer with the full incident context, severity classification, planned response, and justification. The officer can approve, reject, or modify the response. The graph resumes only after the officer acts.

### `execute_actions`

Constrained ReAct node. Executes only whitelisted low-severity actions automatically: send notifications, log the incident, schedule non-urgent inspections. If the safety officer approved a stoppage, the stoppage order is issued here.

### `await_inspection`

Waiting state. The graph checkpoints here and waits for the inspector to submit their report. This can span hours or days.

### `inspection_complete?`

Conditional router. If the inspection reveals additional hazards, loops back to `plan_response` with updated context. Otherwise, proceeds to `update_incident_record`.

### `update_incident_record`

Writes the final incident status, corrective actions taken, and inspection results to the database.

### `notify_regulatory_body`

If severity is high or critical, sends the required regulatory notification within the mandated time window.

### `close_incident`

Archives the incident and sends closure notifications.

---

## State Held Across the Run

| Key | Description | Populated By |
|:---|:---|:---|
| `incident_id` | Unique incident identifier | `receive_report` |
| `site_id` | Affected job site | `receive_report` |
| `reporter_id` | Employee who reported | `receive_report` |
| `description` | Incident narrative | `receive_report` |
| `severity` | low / medium / high / critical | `classify_severity` |
| `response_plan` | Ordered list of actions | `plan_response` |
| `hitl_decision` | Safety officer's decision | `hitl_safety_officer` (platform) |
| `actions_executed` | List of completed actions | `execute_actions` |
| `inspection_report` | Inspector's findings | `await_inspection` (platform) |
| `regulatory_notified` | Boolean | `notify_regulatory_body` |
| `status` | open / closed | `close_incident` |

---

## HITL Conditions

- **Trigger:** Planned response includes a work stoppage, site evacuation, or any action that would halt operations
- **Approver:** Safety Officer
- **Payload:** Full incident report, severity classification, response plan with justification, regulatory requirements triggered
- **Resume behavior:** If approved, proceed to `execute_actions` with the stoppage order enabled. If rejected or modified, proceed with the officer's revised plan.

---

## Failure Modes (Tickets)

- Incident report parsing fails (missing required fields)
- Severity classifier returns an unparseable response
- Safety policy database query fails
- Inspection scheduling tool errors
- Regulatory notification API fails

In each case, the node fails, checkpoints, and opens a ticket. The run resumes from the same node once resolved.

---

## LLM Techniques

| Node | Technique | Why It Fits |
|:---|:---|:---|
| `plan_response` | LATS | The order of safety response actions matters. Notifying the wrong person first, or delaying a regulatory notification past its deadline, has real legal consequences. LATS searches over candidate orderings and scores them against a real severity rubric and regulatory checklist, pruning bad paths with reflection. |
| `execute_actions` | Constrained ReAct | Only whitelisted actions (notifications, logging, scheduling) execute automatically. Any action that would stop work or evacuate is gated behind the HITL node. This prevents the agent from taking irreversible operational actions on its own. |

---

## Files

| File | Purpose |
|:---|:---|
| `graph.py` | Graph topology, edges, conditional routing |
| `nodes.py` | Node implementations, state mutations |
| `tools.py` | MCP tool wrappers for safety incidents, notifications, and scheduling |
| `lats.py` | LATS search logic for response plan orderings |

