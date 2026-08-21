# Change Order State Graph

A durable, checkpointed state graph for managing construction-project **change orders** — requests to modify scope, cost, or schedule that require formal client sign-off, LLM-powered intake parsing, and constrained reasoning before any irreversible filing action.

## Overview

This module implements a LangGraph-style `StateGraph` workflow with **two LLM-call additions** inside its nodes:

| Node | Technique | Why It Fits |
|------|-----------|-------------|
| `decompose_change_order` | **Task Decomposition** | Client requests arrive as unstructured natural language. The LLM extracts `project_id`, `cost_delta`, `schedule_delta_days`, and `description` into a validated JSON schema before any database write occurs. |
| `file_change_order` | **Constrained ReAct** | Filing is irreversible — once `PendingReview` is set, the review clock starts and client notifications trigger. The LLM must *reason* (Thought) then *choose* from a closed whitelist (`submit_for_review`, `abort_draft`). Non-whitelisted actions are caught and ticketed. |

The graph then:
3. **Pauses** execution and raises a **HITL** task, waiting for the project manager to **approve**, **reject**, or **counter**.
4. **Handles the decision**: closes approved/rejected orders, or loops back to re-draft if countered (with version bumping).
5. **Escalates** stalled reviews via **ticketed errors** when client response exceeds the configured timeout.

Every meaningful transition is checkpointed to durable SQLite storage. Kill the process mid-run, restart it, and `graph.run(run_id)` resumes from exactly where it left off — no re-execution of completed steps.

---

## Project Structure

| File | Purpose |
|------|---------|
| `tools.py` | Database layer — CRUD + queries for `ChangeOrders`, audit logging via `mcp_db.log_action()`. |
| `nodes.py` | **Task Decomposition** node (LLM #1), **Constrained ReAct** node (LLM #2), HITL pause, decision handler, stall escalation. |
| `graph.py` | `StateGraph` builder, public API (`start_new_change_order`, `resume_after_signoff`, `resume_after_ticket`), admin helper `get_run_status()`. |
| `demo.py` | Full `pytest` suite covering structured input, natural-language decomposition, ReAct submit, ReAct abort, HITL lifecycle, ticket lifecycle, and admin status. |

---

## Workflow State Graph

```
                         CHANGE ORDER STATE GRAPH
================================================================================

                         [start_new_change_order(request)]
                                         │
                                         ▼
                    ┌─────────────────────────────────────┐
                    │   decompose_change_order_node       │  ← LLM #1
                    │   (Task Decomposition)              │     TASK DECOMPOSITION
                    │   Parses raw NL → structured JSON   │     (natural language
                    │   Validates against live DB         │      → project_id,
                    └──────────────┬──────────────────────┘        cost_delta,
                                   │                                 schedule, etc.)
          (Missing request?)  ─────┼─────► [ TicketableError ]
                                   │
                                   ▼
                    ┌─────────────────────────────────────┐
                    │    file_change_order_node           │  ← LLM #2
                    │    (Constrained ReAct)              │     CONSTRAINED ReAct
                    │    Thought: <reasoning>             │     (whitelist:
                    │    Action: submit_for_review        │      submit_for_review
                    │         | abort_draft               │      | abort_draft)
                    └──────────────┬──────────────────────┘
                                   │
          (Non-whitelisted    ─────┼─────► [ TicketableError ]
           action?)
                                   │
                                   ▼
                    ┌─────────────────────────────────────┐
                    │   await_client_signoff_node         │  ← HITL PAUSE
                    │   (Human-in-the-Loop)               │     raises HITLPause
                    └──────────────┬──────────────────────┘     opens HITLTasks row
                                   │
                    [ Admin resolves via platform ]
                                   │
                    [resume_after_signoff(decision, resolved_by)]
                                   │
                                   ▼
                    ┌─────────────────────────────────────┐
                    │      handle_decision_node           │
                    └──────────────┬──────────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
              ▼                    ▼                    ▼
        Decision ==         Decision ==          Decision ==
        'approved'          'rejected'           'countered'
              │                    │                    │
              ▼                    ▼                    ▼
        [Status: Closed]    [Status: Closed]     bump_version()
                                                        │
                                                        │ (loop back)
                                                        ▼
                                              ┌─────────────────┐
                                              │  decompose...   │  (reuses same
                                              │  (Version + 1)  │   ChangeOrderID)
                                              └─────────────────┘

================================================================================
```

---

## Database Schema

The `ChangeOrders` table is created automatically on first access (`_ensure_schema`):

| Column | Type | Constraints |
|--------|------|-------------|
| `ChangeOrderID` | `INTEGER` | PRIMARY KEY |
| `RunID` | `TEXT` | NOT NULL |
| `ProjectID` | `INTEGER` | NOT NULL, FK → `Projects(ProjectID)` |
| `Description` | `TEXT` | NOT NULL |
| `CostDelta` | `REAL` | NOT NULL |
| `ScheduleDeltaDays` | `INTEGER` | NOT NULL |
| `Status` | `TEXT` | NOT NULL — `Drafting`, `PendingReview`, `Approved`, `Rejected`, `Countered`, `Closed` |
| `CounterNote` | `TEXT` | Nullable |
| `SubmittedAt` | `REAL` | Unix timestamp |
| `DecidedAt` | `REAL` | Unix timestamp |
| `Version` | `INTEGER` | NOT NULL, DEFAULT 1 |

---

## Core Concepts

### 1. Task Decomposition (LLM Addition #1)

**Location:** `decompose_change_order_node`

**Why here:**
- Client requests arrive as natural language: *"The soil survey came back bad, we need to reinforce the foundation. Budget impact is maybe 18k and it\'ll push us out a week."*
- A deterministic regex pass cannot reliably extract `project_id`, `cost_delta`, `schedule_delta_days`, and a clean description.
- The LLM decomposes the request into four structured components, each validated against live DB constraints before the draft is created.

**Why not ToT/LATS/RAG:**
- There is no branching search space — this is a single-parse problem with a deterministic JSON schema.
- No external policy documents are needed to parse a request.

**Resilience:**
- If the LLM returns malformed JSON or missing fields, the parser raises `TicketableError` (becomes a ticket, not a crash).
- A **retry loop** (`_llm_decompose_with_retry`, max 2 retries) re-prompts the LLM with the previous error before giving up.
- **Backward compatibility:** If the caller already passes a fully structured dict, the node skips the LLM call entirely (fast path).

### 2. Constrained ReAct (LLM Addition #2)

**Location:** `file_change_order_node`

**Why here:**
- Filing a change order is **irreversible**: once `PendingReview` is set, client notification triggers and the review clock starts.
- The LLM must explicitly reason about readiness (completeness, budget sanity, description clarity) before being allowed to file.
- The action space is strictly limited to: `submit_for_review`, `abort_draft`.
- Any attempt to call a non-whitelisted tool is caught and ticketed.

**Why not RAG:**
- Filing decisions are based on the change order\'s own fields, not on external policy documents.

**Pattern:**
```
Thought: The description is clear, cost is positive, and schedule delta is non-negative. Ready to submit.
Action: submit_for_review
```
The parser (`_parse_react_response`) validates the format. If the action is not in the whitelist, `TicketableError` is raised.

### 3. HITL (Human-in-the-Loop)

Execution pauses at `await_client_signoff` and raises `HITLPause`. The checkpoint store opens a `HITLTasks` row with:

- **Reason** — human-readable summary of the change order and project.
- **Approving employee ID** — the project\'s `ProjectManagerID`.
- **Decision key** — `hitl_decision_co{ChangeOrderID}_v{Version}` to prevent infinite loops on resubmission.

An admin resolves the task via:
```python
resume_after_signoff(run_id, decision="approved", resolved_by=2)
```

### 4. Ticketable Errors (Failure & Recovery)

Unexpected failures raise `TicketableError`:
- Missing `request` payload in state
- LLM decomposition produced unparseable JSON or missing fields
- Constrained ReAct produced a non-whitelisted action
- Missing `change_order_id` or `employee_id`
- Change order record not found in DB
- Review stalled beyond `REVIEW_TIMEOUT_SECONDS` (3 days)

The graph engine catches these, opens a `Tickets` row, and parks the run in `ticket_open` status. An operator inspects the ticket, fixes the underlying issue, and resumes with:
```python
resume_after_ticket(run_id, resolution="Fixed missing project_id", updated_state={...})
```

### 5. Versioning & Resubmission Loop

When a client **counters** a change order:

1. `handle_decision_node` calls `bump_version_for_resubmission()` — sets `Status = 'Drafting'` and increments `Version`.
2. The conditional router (`_decision_router`) sends execution back to `decompose_change_order_node`.
3. The node **reuses** the existing row (no duplicate records).
4. The next HITL pause uses a new decision key (`..._v{Version}`) so the old cycle\'s resolved decision is never mistaken for the current one.

---

## LLM Integration

The state graph nodes call the same LLM provider used by the planning agent (`planning/model_provider.py`):

```python
from planning.model_provider import get_planning_llm
llm = get_planning_llm()
response = llm.invoke([HumanMessage(content=prompt)])
```

**Production:** Set `GROQ_API_KEY` and `GROQ_MODEL` in your `.env` file.

**Testing / CI:** If no API key is present, `DeterministicPlanningLLM` returns context-aware stubs:
- Decomposition prompts → valid JSON with example fields
- ReAct prompts → valid `Thought: ...\nAction: ...` pairs

This ensures the test suite passes without network calls or API costs.

---

## Running the Tests

```bash
# From the repository root (where mcp_server/ lives)
pytest state_graph/change_order/demo.py -v -s
```

### Test Coverage

| Test | What It Validates |
|------|-----------------|
| `test_display_flow_graph` | ASCII diagram includes both LLM technique labels |
| `test_structured_request_backward_compat` | Structured dict bypasses LLM (fast path) |
| `test_natural_language_decomposition` | Raw string triggers LLM task decomposition; fields extracted correctly |
| `test_constrained_react_submits_valid_draft` | Clean change order → `submit_for_review` action |
| `test_constrained_react_aborts_invalid_draft` | Negative cost / empty description → `abort_draft` action |
| `test_change_order_workflow_lifecycle` | Full happy path: Draft → Submit → HITL → Counter → HITL → Approve → Close |
| `test_ticketable_error_on_missing_request` | Malformed state → ticket, not exception propagation |
| `test_resume_after_ticket_reruns_failed_node_and_resolves_ticket` | Ticket resolution re-runs failed node, marks ticket resolved |
| `test_get_run_status` | Admin platform can query run state via `get_run_status()` |

---

## Public API

### Start a new run

```python
from state_graph.change_order.graph import start_new_change_order

# Structured request (fast path)
state = start_new_change_order("run-001", {
    "project_id": 1,
    "employee_id": 1,
    "description": "Add reinforced foundation",
    "cost_delta": 18500.0,
    "schedule_delta_days": 6,
})

# Natural language request (triggers LLM decomposition)
state = start_new_change_order("run-002",
    "Project 1 needs a new HVAC unit. Cost is 12000 and it adds 3 days.")
```

### Resume after HITL

```python
from state_graph.change_order.graph import resume_after_signoff

final_state = resume_after_signoff(
    run_id="run-001",
    decision="approved",      # or "rejected" / "countered"
    resolved_by=2,            # admin employee ID
    counter_note="Reduce cost by 10%",  # only for countered
)
```

### Resume after ticket

```python
from state_graph.change_order.graph import resume_after_ticket

final_state = resume_after_ticket(
    run_id="run-002",
    resolution="Operator supplied the missing request payload.",
    updated_state={"request": {"project_id": 1, ...}},
)
```

### Admin platform helper

```python
from state_graph.change_order.graph import get_run_status

status = get_run_status("run-001")
# {
#   "run_id": "run-001",
#   "current_node": "await_client_signoff",
#   "status": "paused_hitl",
#   "change_order_id": 42,
#   "hitl_decision": None
# }
```

---

## Configuration

| Constant | Value | Description |
|----------|-------|-------------|
| `REVIEW_TIMEOUT_SECONDS` | `259_200` (3 days) | Max time a change order may sit in `PendingReview` before raising a stall ticket. |
| LLM temperature (decomposition) | `0.2` | Slightly creative for parsing varied client language. |
| LLM temperature (ReAct) | `0.1` | Near-deterministic for strict whitelist compliance. |
| Decomposition retry limit | `2` | Re-prompts the LLM on parse failure before ticketing. |

---

## Dependencies

- `db` (local MCP server module) — `get_conn()`, `log_action()`, `get_project()`
- `planning.model_provider` — `get_planning_llm()`, `has_real_llm()`
- `state_graph.core.checkpoint_store` — durable checkpoint / HITL / ticket store
- `state_graph.core.graph_base` — `StateGraph` cyclic runner
- `state_graph.core.hitl` — `require_hitl()`, `HITLPause`
- `state_graph.core.tickets` — `TicketableError`

---

## Design Decisions

1. **Resume via store, not `initial_state`** — `StateGraph.run()` always prefers the durable checkpoint over caller-supplied state. Resumption functions resolve the HITL task or ticket through the store first, then invoke `graph.run()`.
2. **Decision key includes Version** — Prevents infinite loops on resubmission by ensuring each counter round has a unique key in the merged state.
3. **Single-row lifecycle** — Countered change orders reuse the same `ChangeOrderID`; only `Version` increments. This avoids orphaned rows and keeps audit history on one record.
4. **Constrained tool exposure** — `file_change_order_node` may only call `tools.submit_for_review()` or `tools.close()`, guaranteeing every DB write is explicitly logged and audited.
5. **LLM bridge with fallback stubs** — Production uses Groq/OpenAI via `planning.model_provider`. Tests use deterministic stubs so the suite passes without API keys or network.
6. **Retry on decomposition failure** — A single bad LLM parse does not immediately ticket; the node re-prompts with the error context, reducing noise for operators.
