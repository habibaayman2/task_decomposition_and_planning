# Equipment Recovery Agent

> **Domain:** Equipment breakdown and replacement logistics  
> **Agent Type:** Stateful graph with HITL gating and external waits  
> **Location:** `state_graph/equipment_recovery/`

---

## Problem

When critical equipment fails on an IronBridge job site, downtime is expensive. The site engineer needs a replacement path fast, but the optimal choice depends on multiple variables that change in real time: rental availability, repair technician schedules, subcontractor capacity, and the project's remaining budget. Moreover, some recovery paths cost more than the site engineer is authorized to spend.

The agent must:
- Diagnose the failure and classify severity
- Explore multiple recovery strategies in parallel
- Price each option against live data
- Obtain approval before committing significant spend
- Execute the chosen path and confirm recovery

This cannot be a single-pass script because the technician's report may not arrive for hours, the project manager may be unreachable, and the vendor API may be temporarily down.

---

## Graph Topology

```
[report_failure]
      ↓
[diagnose]
      ↓
[explore_options]  ← Tree of Thoughts (parallel strategies)
      ↓
[score_options]
      ↓
[select_best]
      ↓
[cost > threshold?] ──→ yes ──→ [hitl_approval] ──→ approved? ──→ [execute_recovery]
      │                                                              ↑
      └─→ no ────────────────────────────────────────────────────────┘
      ↓
[await_technician]  ← external wait (optional, if repair selected)
      ↓
[confirm_recovery]
      ↓
[notify_team]
      ↓
[END]
```

---

## Nodes

### `report_failure`

Captures the equipment ID, failure description, and job site from the user or monitoring system.

### `diagnose`

Queries the equipment database for the asset's maintenance history and current status. Classifies the failure as repairable on-site, requiring a technician, or total loss.

### `explore_options`

Tree-of-Thoughts node. Generates and explores multiple recovery strategies simultaneously:
- Rent replacement from Vendor A
- Rent replacement from Vendor B
- Dispatch in-house technician
- Call external repair service
- Subcontract the work to another crew
- Shift schedule to use idle equipment from another site

Each branch is evaluated against cost, time-to-recovery, and reliability.

### `score_options`

Scores each explored option using a weighted function that prioritizes time for critical-path equipment and cost for non-critical equipment.

### `select_best`

Selects the highest-scoring option.

### `cost > threshold?`

Conditional router. If the selected option's cost exceeds $5,000, routes to `hitl_approval`. Otherwise, proceeds directly to `execute_recovery`.

### `hitl_approval`

Human-in-the-loop node. Opens a task on the platform for the project manager with the equipment details, failure description, recommended option, and cost. Resumes only after the manager acts.

### `execute_recovery`

Constrained ReAct node. Executes the selected option through whitelisted MCP tools: book rental, dispatch technician, call repair service, or notify subcontractor. Validates availability and budget before committing.

### `await_technician`

Waiting state (only if repair was selected). The graph checkpoints here and waits for the technician's assessment report. This can span hours or days.

### `confirm_recovery`

Verifies that the equipment is operational or the replacement is on site and functional.

### `notify_team`

Notifies the site engineer, project manager, and scheduling team of the recovery status.

---

## State Held Across the Run

| Key | Description | Populated By |
|:---|:---|:---|
| `equipment_id` | Failed equipment | `report_failure` |
| `failure_description` | What went wrong | `report_failure` |
| `site_id` | Affected job site | `report_failure` |
| `diagnosis` | repairable / technician / total-loss | `diagnose` |
| `options_explored` | List of strategies with scores | `explore_options` |
| `selected_option` | Best strategy | `select_best` |
| `estimated_cost` | Cost of selected option | `select_best` |
| `hitl_decision` | Manager's approval/rejection | `hitl_approval` (platform) |
| `technician_report` | Assessment details | `await_technician` (platform) |
| `recovery_confirmed` | True when equipment is back online | `confirm_recovery` |

---

## HITL Conditions

- **Trigger:** Selected recovery option costs more than $5,000
- **Approver:** Project Manager
- **Payload:** Equipment ID, failure description, recommended option, estimated cost, time to recover, alternative options considered
- **Resume behavior:** If approved, proceed to `execute_recovery`. If rejected, loop back to `explore_options` with a constraint to exclude the rejected option.

---

## Failure Modes (Tickets)

- Equipment database query fails (equipment ID not found)
- Vendor API unreachable during `explore_options`
- Cost scoring produces an invalid result
- Booking tool errors during `execute_recovery`
- Technician report parsing fails

In each case, the node fails, checkpoints, and opens a ticket. The run resumes from the same node once resolved.

---

## LLM Techniques

| Node | Technique | Why It Fits |
|:---|:---|:---|
| `explore_options` | Tree of Thoughts | There are multiple valid recovery strategies, and the best one depends on a trade-off between cost, time, and reliability that the model cannot know a priori. Exploring branches in parallel and scoring them against live data prevents premature commitment to a suboptimal path. |
| `execute_recovery` | Constrained ReAct | The actions here have financial and contractual consequences. A whitelist of permissible booking/dispatch actions, plus real-time validation against vendor availability and project budget, ensures the agent does not overcommit or overspend. |

---

## Files

| File | Purpose |
|:---|:---|
| `graph.py` | Graph topology, edges, conditional routing |
| `nodes.py` | Node implementations, state mutations |
| `tools.py` | MCP tool wrappers for equipment, vendor, and budget queries |
| `tot.py` | Tree-of-Thoughts scoring logic for recovery strategies |

