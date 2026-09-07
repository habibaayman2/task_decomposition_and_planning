# State Graph Agents

The `state_graph/` package provides the persistent workflow-execution layer for the IronBridge Construction AI Operations Platform.

It is designed for operational workflows that require:

* durable execution state,
* conditional routing,
* workflow cycles,
* human-in-the-loop decisions,
* failure tickets,
* checkpoint-based recovery,
* and execution that can continue after a process restart.

The implementation contains three domain-specific state graphs:

1. **Change-Order Negotiation**
2. **Equipment Recovery**
3. **Safety Incident Response**

All three workflows use the shared execution infrastructure under `state_graph/core/`.

---

## 1. Architecture

```text
state_graph/
├── change_order/
├── equipment_recovery/
├── safety_incident/
├── core/
├── cli.py
└── demo_crash_resume.py
```

The architecture is divided into two layers.

### Domain Layer

Each operational problem owns its:

* graph definition,
* node implementations,
* domain-specific tools,
* and reasoning logic.

### Shared Core

The shared core provides:

* graph execution,
* checkpoint persistence,
* HITL handling,
* failure-ticket handling,
* and common state models.

```text
                         State Graph Layer
                                │
          ┌─────────────────────┼─────────────────────┐
          │                     │                     │
          ▼                     ▼                     ▼
   Change Order        Equipment Recovery     Safety Incident
          │                     │                     │
          └─────────────────────┼─────────────────────┘
                                │
                                ▼
                           Shared Core
                                │
             ┌──────────────────┼──────────────────┐
             │                  │                  │
             ▼                  ▼                  ▼
      StateGraph        CheckpointStore       HITL / Tickets
             │                  │                  │
             └──────────────────┼──────────────────┘
                                ▼
                         SQLite Persistence
```

---

# 2. `StateGraph` Execution Model

The common graph runner is implemented in:

```text
state_graph/core/graph_base.py
```

The implementation defines:

```python
END = "__END__"
```

and the `StateGraph` class.

A graph is constructed using:

```python
StateGraph(name)
```

and supports:

```python
add_node(name, fn)
set_entry(name)
add_edge(from_node, to_node)
add_conditional_edge(from_node, router)
run(run_id, initial_state=None, store=None)
```

The node contract is:

```python
def my_node(state: dict) -> dict:
    ...
    return {"some_key": "some_value"}
```

The returned dictionary is merged into the current state.

A conditional edge uses a router:

```python
def router(state: dict) -> str:
    ...
    return "next_node"
```

The router may also return:

```python
END
```

to terminate execution.

The graph runner explicitly supports cycles: a conditional router may return a node that has already been visited.

---

# 3. Run Creation and Resumption

`StateGraph.run()` uses the same interface for both new executions and resumed executions.

```python
graph.run(run_id, initial_state=...)
```

If the `run_id` does not exist, the graph:

1. creates a new state,
2. uses the configured entry point,
3. persists the initial run,
4. begins execution.

If the `run_id` already exists, the graph loads the persisted checkpoint and resumes from the stored node.

A completed run is returned without re-executing its nodes.

For runs with status:

```text
running
paused_hitl
ticket_open
```

the stored `current_node` is executed when the run resumes.

This allows the same API to be used after:

* a normal execution,
* a process crash,
* an HITL resolution,
* or a ticket resolution.

---

# 4. Checkpoint Semantics

Checkpoint persistence is implemented by:

```text
state_graph/core/checkpoint_store.py
```

The checkpoint mechanism is a core part of workflow correctness rather than simple logging.

The runner checkpoints the workflow **before advancing to the next node**.

## Successful node

For a successful node:

```text
Current Node
     │
     ▼
Execute
     │
     ▼
Merge Returned State
     │
     ▼
Determine Next Node
     │
     ▼
Checkpoint Next Node
     │
     ▼
Continue
```

Therefore, after successful execution:

```text
current_node = next node
```

## HITL pause

When a node raises `HITLPause`:

```text
Current Node
     │
     ▼
HITLPause
     │
     ▼
Checkpoint Current Node
     │
     ▼
Create HITL Task
     │
     ▼
paused_hitl
```

The current node is deliberately retained because it has **not completed**.

When the run resumes, the same node is executed again with the resolved decision merged into the persisted state.

## Failure ticket

Unexpected exceptions follow the same checkpoint principle:

```text
Current Node
     │
     ▼
Exception
     │
     ▼
Checkpoint Current Node
     │
     ▼
Create Ticket
     │
     ▼
ticket_open
```

The failed node therefore remains the next node to execute after resolution.

---

# 5. Run Statuses

The shared status vocabulary is defined in:

```text
state_graph/core/models.py
```

The implementation defines exactly four statuses:

```text
running
paused_hitl
ticket_open
completed
```

| Status        | Meaning                                         |
| ------------- | ----------------------------------------------- |
| `running`     | Actively executing or ready to resume.          |
| `paused_hitl` | Paused because a human decision is required.    |
| `ticket_open` | Paused because an unplanned exception occurred. |
| `completed`   | The graph reached `END`.                        |

`GraphRun.current_node` represents the node that should execute **next**. This is particularly important when a run is paused or has an open ticket.

---

# 6. Human-in-the-Loop

HITL functionality is implemented in:

```text
state_graph/core/hitl.py
```

A node pauses intentionally by raising:

```python
HITLPause
```

The shared graph runner catches this exception separately from ordinary exceptions.

The resulting lifecycle is:

```text
Node
 │
 ▼
Human Decision Required
 │
 ▼
HITLPause
 │
 ▼
Checkpoint Current Node
 │
 ▼
Open HITL Task
 │
 ▼
paused_hitl
 │
 ▼
Administrator Resolves Task
 │
 ▼
Persist Decision
 │
 ▼
Resume Same Run
```

This distinction is intentional:

> An HITL pause is an expected workflow state, not an application failure.

The shared models represent an HITL task with information including:

```text
task_id
run_id
node_name
reason
payload
status
decision
resolved_by
```

---

# 7. Failure Tickets

Failure handling is implemented in:

```text
state_graph/core/tickets.py
```

An exception raised by a node, other than `HITLPause`, is treated as an unplanned failure.

The graph runner:

1. checkpoints the current unfinished node,
2. opens a ticket,
3. changes the run status to `ticket_open`,
4. returns the current state.

The shared core also defines:

```python
TicketableError
```

for failures that need explicit ticket-related context.

A ticket is therefore different from an HITL task:

| HITL Task                  | Ticket                      |
| -------------------------- | --------------------------- |
| Expected workflow pause    | Unexpected failure          |
| Requires a human decision  | Requires failure resolution |
| Status: `paused_hitl`      | Status: `ticket_open`       |
| Raised through `HITLPause` | Created from an exception   |

The shared `Ticket` model contains:

```text
ticket_id
run_id
node_name
error_message
status
resolution
```

---

# 8. Change-Order Negotiation

Location:

```text
state_graph/change_order/
```

Files:

```text
change_order/
├── __init__.py
├── README.md
├── demo.py
├── graph.py
├── nodes.py
└── tools.py
```

The graph is constructed by:

```python
build_change_order_graph()
```

and is named:

```text
change_order
```

## 8.1 Graph Topology

The implemented topology is:

```text
decompose_change_order
          │
          ▼
file_change_order
          │
          ▼
await_client_signoff
          │
          ▼
handle_decision
          │
          ▼
       Decision
        /     \
       /       \
countered     END
   │
   ▼
decompose_change_order
```

The graph registers exactly these nodes:

```text
decompose_change_order
file_change_order
await_client_signoff
handle_decision
```

The entry point is:

```text
decompose_change_order
```

The fixed transitions are:

```text
decompose_change_order → file_change_order
file_change_order → await_client_signoff
await_client_signoff → handle_decision
```

The conditional router after `handle_decision` returns:

```text
decompose_change_order
```

when:

```python
state.get("hitl_decision") == "countered"
```

Otherwise it returns:

```text
__END__
```

## 8.2 Node Responsibilities

### `decompose_change_order`

Processes the incoming change-order request and produces the structured state required by the following nodes.

The graph's request model includes information such as:

```text
project_id
employee_id
description
cost_delta
schedule_delta_days
```

### `file_change_order`

Performs the change-order filing stage using the domain-specific implementation in the change-order nodes and tools.

### `await_client_signoff`

Represents the client-signoff boundary.

The workflow can pause while awaiting a decision rather than requiring the application process to remain active.

### `handle_decision`

Processes the persisted decision and determines whether the graph should terminate or begin another decomposition cycle.

## 8.3 Public Operations

The change-order graph exposes operations including:

```python
build_change_order_graph()
start_new_change_order(run_id, request)
resume_after_signoff(run_id, decision, resolved_by, counter_note=None)
resume_after_ticket(run_id, resolution, updated_state=None)
get_run_status(run_id)
```

The graph implementation uses the shared checkpoint store for persistent execution.

---

# 9. Equipment Recovery

Location:

```text
state_graph/equipment_recovery/
```

Files:

```text
equipment_recovery/
├── __init__.py
├── README.md
├── graph.py
├── nodes.py
├── tools.py
└── tot.py
```

The graph is constructed by:

```python
build_equipment_recovery_graph()
```

and is named:

```text
equipment_recovery
```

## 9.1 Graph Topology

The implemented topology is:

```text
report_breakdown
       │
       ▼
diagnose_issue
       │
       ▼
evaluate_options
       │
       ▼
approval_gate
       │
       ▼
     Decision
      /     \
     /       \
rejected   approved /
           auto_approved
   │            │
   ▼            ▼
evaluate     execute_recovery_action
options             │
                    ▼
                   END
```

The graph registers:

```text
report_breakdown
diagnose_issue
evaluate_options
approval_gate
execute_recovery_action
```

The entry point is:

```text
report_breakdown
```

The fixed transitions are:

```text
report_breakdown → diagnose_issue
diagnose_issue → evaluate_options
evaluate_options → approval_gate
execute_recovery_action → END
```

The conditional router after `approval_gate` behaves as follows:

```text
approval_status == "rejected"
        ↓
evaluate_options
```

Otherwise:

```text
approved / auto_approved
        ↓
execute_recovery_action
```

## 9.2 Approval Logic

The `approval_gate` node determines whether human approval is required.

The implementation specifically bases its HITL threshold on the project's actual remaining budget rather than hard-coding a standalone dollar threshold into the graph routing.

The graph cycle occurs only after an explicit rejection.

This makes the approval decision an external operational condition rather than merely an internal model retry.

## 9.3 Tree of Thoughts

The package includes:

```text
state_graph/equipment_recovery/tot.py
```

for the Tree-of-Thoughts component used by the equipment-recovery workflow.

The graph itself remains responsible for orchestration and routing; the domain implementation is responsible for evaluating recovery alternatives.

---

# 10. Safety Incident Response

Location:

```text
state_graph/safety_incident/
```

The graph is named:

```text
safety_incident
```

## 10.1 Graph Topology

The implementation registers:

```text
report_incident
investigate
safety_officer_signoff
handle_officer_decision
file_regulator_report
```

The entry point is:

```text
report_incident
```

The fixed transitions are:

```text
report_incident → investigate
investigate → safety_officer_signoff
safety_officer_signoff → handle_officer_decision
file_regulator_report → END
```

The decision router after `handle_officer_decision` implements three outcomes:

```text
needs_more_investigation
        ↓
investigate
```

```text
regulator_report_required
        ↓
file_regulator_report
```

```text
other decision
        ↓
END
```

Therefore the complete topology is:

```text
report_incident
       │
       ▼
investigate
       │
       ▼
safety_officer_signoff
       │
       ▼
handle_officer_decision
       │
       ├── needs_more_investigation ──→ investigate
       │
       ├── regulator_report_required
       │                 │
       │                 ▼
       │       file_regulator_report
       │                 │
       │                 ▼
       │                END
       │
       └── other ─────────────────────→ END
```

## 10.2 Incident Request

The graph's `start_new_incident()` operation expects:

```python
{
    "project_id": ...,
    "employee_id": ...,
    "description": ...,
    "severity": ...,
}
```

The initial persisted state includes:

```python
{
    "run_id": run_id,
    "request": request,
}
```

## 10.3 Resume After Sign-Off

The implemented:

```python
resume_after_signoff(run_id)
```

loads the existing checkpoint and resumes the same graph run after the administrator has resolved the HITL task.

The decision is already merged into the persisted state by the checkpoint-store HITL resolution mechanism; therefore the caller does not need to pass the decision again to `resume_after_signoff()`.

---

# 11. Shared State Models

The shared models are defined in:

```text
state_graph/core/models.py
```

They are read-model conveniences over the persisted SQLite state-graph tables.

The database tables remain the source of truth.

## `RunStatus`

```python
class RunStatus(str, Enum):
    RUNNING = "running"
    PAUSED_HITL = "paused_hitl"
    TICKET_OPEN = "ticket_open"
    COMPLETED = "completed"
```

## `GraphRun`

The model contains:

```text
run_id
graph_name
status
current_node
state
```

The important semantic rule is:

```text
current_node = node to execute NEXT
```

not necessarily the node that most recently completed.

## `HITLTask`

The model represents:

```text
task_id
run_id
node_name
reason
payload
status
decision
resolved_by
```

## `Ticket`

The model represents:

```text
ticket_id
run_id
node_name
error_message
status
resolution
```

---

# 12. Database Persistence

The state-graph checkpoint mechanism is integrated with the existing IronBridge SQLite persistence layer.

The shared state models explicitly describe the SQLite tables as the source of truth.

The checkpoint store is responsible for reading and writing the persisted execution state, while the models provide the common Python vocabulary used by the graphs and platform backend.

This separation is intentional:

```text
Database
   │
   ▼
CheckpointStore
   │
   ▼
StateGraph
   │
   ▼
Domain Graph
```

The state models themselves do not perform database I/O.

---

# 13. Cyclic Workflows

The state-graph implementation differs from a strictly linear workflow because conditional routing may return to a previously visited node.

## Change Order

```text
handle_decision
      │
      └── countered
             │
             ▼
   decompose_change_order
```

## Equipment Recovery

```text
approval_gate
      │
      └── rejected
             │
             ▼
      evaluate_options
```

## Safety Incident

```text
handle_officer_decision
      │
      └── needs_more_investigation
                    │
                    ▼
               investigate
```

These are explicit graph transitions implemented through `add_conditional_edge()`. They are not generic retry loops.

---

# 14. State Graphs and Planning Workflows

The repository contains both planning workflows and state-graph workflows.

The state-graph layer exists for processes where execution state must remain durable across interruptions, external decisions, failures, or workflow cycles.

The distinction can be summarized as:

| Planning Workflow                        | State Graph                               |
| ---------------------------------------- | ----------------------------------------- |
| Planning/decomposition oriented          | Persistent operational execution          |
| Primarily DAG-oriented                   | Supports cycles                           |
| Usually completes in one execution path  | May span multiple execution periods       |
| Less dependent on durable workflow state | Durable checkpoints are fundamental       |
| External decisions are secondary         | HITL is a first-class execution mechanism |
| Failure handling is separate             | Failures become persisted tickets         |

The `StateGraph` implementation explicitly describes itself as the cyclic counterpart to the repository's planning DAGs.

---

# 15. Reasoning and Domain Logic

The graph runner does not prescribe a single reasoning technique.

Instead, each domain graph contains its own reasoning implementation.

The repository architecture associates the workflows with:

| Workflow           | Reasoning Approach                     |
| ------------------ | -------------------------------------- |
| Change Order       | Task Decomposition + Constrained ReAct |
| Equipment Recovery | Tree of Thoughts + Constrained ReAct   |
| Safety Incident    | LATS + Constrained ReAct               |

The shared `StateGraph` layer is independent of these reasoning techniques.

Its responsibility is execution orchestration:

```text
Reasoning / Node Logic
        │
        ▼
State Update
        │
        ▼
Graph Routing
        │
        ▼
Checkpoint
        │
        ▼
Next Node / HITL / Ticket / END
```

---

# 16. Development Guidelines

## Keep Graph Topology in `graph.py`

Graph construction and routing belong in:

```text
graph.py
```

For example:

```python
graph.add_node(...)
graph.set_entry(...)
graph.add_edge(...)
graph.add_conditional_edge(...)
```

## Keep Node Logic in `nodes.py`

Individual workflow operations belong in the domain's:

```text
nodes.py
```

## Keep External Operations in `tools.py`

Database, MCP, or other external operations should remain behind the domain-specific tool layer.

## Preserve Run Identity

A resumed workflow must retain its original:

```text
run_id
```

The run ID identifies the persistent workflow execution.

## Preserve Checkpoint Semantics

Do not bypass the shared checkpoint mechanism.

A checkpoint determines precisely which node is executed when a run resumes.

## Distinguish HITL from Failure

Use:

```python
HITLPause
```

for intentional human intervention.

Allow unexpected exceptions to follow the ticket path.

## Preserve Conditional Cycles

If a workflow requires another attempt because of an external decision, model that transition explicitly through a conditional edge rather than implementing an unrelated retry loop.

---

# 17. Crash and Resume Demonstration

The repository includes:

```text
state_graph/demo_crash_resume.py
```

to demonstrate durable state and crash recovery.

The documented command is:

```bash
python -m state_graph.demo_crash_resume run demo-1
```

The demonstration exists to verify that a process can be terminated and subsequently resumed from the persisted graph state rather than starting the workflow from the beginning.

The graph runner itself uses the same `run()` method for both initial execution and resumption.

---

# 18. Change-Order Demonstration

The repository also provides a change-order demonstration:

```bash
python -m state_graph.change_order.demo
```

This demonstrates the change-order state graph and its persistent execution behavior.

---

# 19. Repository Structure

```text
state_graph/
│
├── __init__.py
├── cli.py
├── demo_crash_resume.py
│
├── core/
│   ├── __init__.py
│   ├── checkpoint_store.py
│   ├── graph_base.py
│   ├── hitl.py
│   ├── models.py
│   └── tickets.py
│
├── change_order/
│   ├── __init__.py
│   ├── README.md
│   ├── demo.py
│   ├── graph.py
│   ├── nodes.py
│   └── tools.py
│
├── equipment_recovery/
│   ├── __init__.py
│   ├── README.md
│   ├── graph.py
│   ├── nodes.py
│   ├── tools.py
│   └── tot.py
│
└── safety_incident/
    ├── __init__.py
    ├── graph.py
    ├── nodes.py
    └── tools.py
```

---

# 20. Operational Lifecycle

## Successful Execution

```text
running
   │
   ▼
execute node
   │
   ▼
merge state
   │
   ▼
checkpoint next node
   │
   ▼
continue
   │
   ▼
completed
```

## HITL Execution

```text
running
   │
   ▼
HITLPause
   │
   ▼
checkpoint current node
   │
   ▼
paused_hitl
   │
   ▼
administrator resolves task
   │
   ▼
resume same run
   │
   ▼
execute paused node
   │
   ▼
continue
```

## Failure Recovery

```text
running
   │
   ▼
unexpected exception
   │
   ▼
checkpoint current node
   │
   ▼
ticket_open
   │
   ▼
ticket resolution
   │
   ▼
resume same run
   │
   ▼
execute failed node
   │
   ▼
continue
```

## Conditional Cycle

```text
Node A
  │
  ▼
Node B
  │
  ▼
Decision
  │
  └──────────► Node A
```

This execution model is implemented directly by the shared `StateGraph` runner.

---

# 21. Design Principles

The state-graph implementation follows these principles.

### Durable Execution

Workflow state is persisted rather than maintained only in process memory.

### Resume from Checkpoint

The same `run_id` can be used to resume an existing execution.

### Explicit Human Intervention

Human decisions are represented as HITL tasks and `paused_hitl` state.

### Explicit Failure Handling

Unexpected exceptions become tickets and `ticket_open` state.

### Conditional Routing

Workflow decisions are represented through explicit graph edges and routers.

### Intentional Cycles

Graphs may return to previously executed nodes when the workflow requires another iteration.

### Domain Isolation

Each operational workflow owns its domain logic while sharing the same execution infrastructure.

### Separation of Concerns

Graph construction, node behavior, tools, persistence, HITL handling, and ticket handling remain separate responsibilities.

---

# 22. Authoritative Implementation Files

For implementation-level behavior, the following files are the source of truth:

```text
state_graph/core/graph_base.py
state_graph/core/checkpoint_store.py
state_graph/core/hitl.py
state_graph/core/models.py
state_graph/core/tickets.py

state_graph/change_order/graph.py
state_graph/change_order/nodes.py
state_graph/change_order/tools.py

state_graph/equipment_recovery/graph.py
state_graph/equipment_recovery/nodes.py
state_graph/equipment_recovery/tools.py
state_graph/equipment_recovery/tot.py

state_graph/safety_incident/graph.py
state_graph/safety_incident/nodes.py
state_graph/safety_incident/tools.py
```

The shared graph runner, for example, explicitly documents the cyclic execution model, checkpoint timing, HITL behavior, ticket behavior, and resume semantics.

---
