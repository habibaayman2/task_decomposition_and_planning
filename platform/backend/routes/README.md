# Admin Backend 


## Table of Contents

- [Overview](#overview)
- [Agent List & Tool Management](#agent-list--tool-management)
  - [Agent Discovery API](#agent-discovery-api)
  - [Tool Management API](#tool-management-api)
- [HITL Inbox, Ticket Inbox, RAG Documents](hitl-inbox-ticket-inbox-rag-documents)
  - [HITL Inbox API](#hitl-inbox-api)
  - [Ticket Inbox API](#ticket-inbox-api)
  - [RAG Document Management API](#rag-document-management-api)
- [Key Design Decisions](#key-design-decisions)
- [Grading Checklist](#grading-checklist)

---

## Overview

The admin backend provides the HTTP API surface for the platform's admin panel. It covers five core concerns:

| Concern | File | Rubric |
|---------|------|--------|
| **Agent Discovery** | `routes/agents.py` | "see every agent connected to the MCP server" |
| **Tool Management** | `routes/tools.py` + `mcp_bridge.py` | "add or remove the tools available to each one" |
| **HITL Inbox** | `routes/hitl.py` | "HITL tasks actually surface and get resolved" |
| **Ticket Inbox** | `routes/tickets.py` | "failure tickets inspectable and resolvable" |
| **RAG Documents** | `routes/rag_docs.py` | "add and remove documents ... reflected on next query" |

All routes are mounted under `/api/*` and combined in `routes/__init__.py`.

---

## Agent List & Tool Management

### Files

| File | Purpose |
|------|---------|
| `platform/backend/routes/agents.py` | Agent roster — static baseline + live checkpoint discovery |
| `platform/backend/routes/tools.py` | Tool management — list, register, deregister, per-agent scope |
| `platform/backend/mcp_bridge.py` | Sync bridge to live MCP server + `AGENT_TOOL_SCOPES` registry |

### Architecture

```
┌─────────────────┐     HTTP/MCP      ┌─────────────────┐
│  Admin Panel    │ ◄───────────────► │  MCP Server     │
│  (FastAPI)      │   mcp_bridge.py   │  (server.py)    │
│                 │                   │  UNMODIFIED     │
└─────────────────┘                   └─────────────────┘
       │                                      │
       │ GET  /api/agents                     │ (checkpoint store query)
       │ POST /api/tools/list                 │ list_registered_tools()
       │ POST /api/tools/register             │ authenticate_as_approver()
       │ POST /api/tools/deregister           │ deregister_tool()
       │ POST /api/tools/scope                │ (bridge-level AGENT_TOOL_SCOPES)
       └──────────────────────────────────────┘
```

---

### Agent Discovery API

#### `GET /api/agents`

Returns the full agent roster with health status.

```bash
curl http://localhost:8000/api/agents
```

**Response:**
```json
{
  "agents": [
    {
      "agent_id": "memory_rag_agent",
      "label": "Memory & RAG Agent",
      "entrypoint": "agent/agent.py",
      "type": "static",
      "description": "Front-desk triage and clinical-policy questions via RAG retrieval",
      "health": {"state": "idle", "active_runs": 0, "statuses": []}
    },
    {
      "agent_id": "change_order_agent",
      "label": "Change Order Approval",
      "entrypoint": "state_graph/change_order/graph.py",
      "type": "state_graph",
      "description": "Stateful change-order workflow with HITL sign-off and ticket recovery",
      "health": {"state": "active", "active_runs": 3, "statuses": ["paused_hitl", "running"]}
    }
  ],
  "total_count": 5,
  "active_state_graphs": 2
}
```

**How it works:**
- **Static baseline** — 5 known agents from the course
- **Live enrichment** — Queries `StateGraphRuns` for active runs, merges `active_runs` and `statuses`
- **Health inference** — `state: "active"` if any runs are `running`/`paused_hitl`/`ticket_open`

#### `GET /api/agents/{agent_id}`

Returns a single agent with its current tool scope.

```bash
curl http://localhost:8000/api/agents/change_order_agent
```

**Response:**
```json
{
  "agent_id": "change_order_agent",
  "label": "Change Order Approval",
  "health": {"state": "active", "active_runs": 3, "statuses": ["paused_hitl"]},
  "tool_scope": ["check_material_inventory", "view_project_budget", "create_purchase_request"]
}
```

---

### Tool Management API

All routes require approver auth (`employee_id` + `pin`).

#### `POST /api/tools/list`

List tools on the live MCP server.

```bash
curl -X POST http://localhost:8000/api/tools/list \
  -H "Content-Type: application/json" \
  -d '{"employee_id": 1, "pin": "1234"}'
```

**Scoped to agent:**
```bash
curl -X POST "http://localhost:8000/api/tools/list?agent_id=change_order_agent" \
  -H "Content-Type: application/json" \
  -d '{"employee_id": 1, "pin": "1234"}'
```

#### `POST /api/tools/register`

Re-register a deregistered tool. Leverages `authenticate_as_approver` side-effect (re-adds missing approver tools from the hardcoded pool).

```bash
curl -X POST http://localhost:8000/api/tools/register \
  -H "Content-Type: application/json" \
  -d '{"employee_id": 1, "pin": "1234", "tool_name": "reserve_material"}'
```

#### `POST /api/tools/deregister`

Remove a tool from the live MCP server.

```bash
curl -X POST http://localhost:8000/api/tools/deregister \
  -H "Content-Type: application/json" \
  -d '{"employee_id": 1, "pin": "1234", "tool_name": "reserve_material"}'
```

#### `POST /api/tools/scope`

Restrict which tools an agent can see. Pass empty `tool_names` to remove restrictions.

```bash
curl -X POST http://localhost:8000/api/tools/scope \
  -H "Content-Type: application/json" \
  -d '{
    "employee_id": 1, "pin": "1234",
    "agent_id": "change_order_agent",
    "tool_names": ["check_material_inventory", "view_project_budget"]
  }'
```

#### `POST /api/tools/scope/get`

Get an agent's current tool scope.

```bash
curl -X POST "http://localhost:8000/api/tools/scope/get?agent_id=change_order_agent" \
  -H "Content-Type: application/json" \
  -d '{"employee_id": 1, "pin": "1234"}'
```

---

## A6: HITL Inbox, Ticket Inbox, RAG Documents

### Files

| File | Purpose |
|------|---------|
| `ib_platform/backend/routes/hitl.py` | HITL task inbox — list, inspect, resolve, auto-resume graph |
| `ib_platform/backend/routes/tickets.py` | Ticket inbox — list, investigate, resolve with state corrections, auto-resume graph |
| `ib_platform/backend/routes/rag_docs.py` | Adapter-based RAG doc management — works with ANY RAG backend |
| `ib_platform/backend/routes/__init__.py` | Aggregate router combining all admin routes |

---

### HITL Inbox API

#### `POST /api/hitl/inbox`

List pending HITL tasks with full persisted run state.

```bash
curl -X POST http://localhost:8000/api/hitl/inbox \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Response:**
```json
[
  {
    "task_id": 42,
    "run_id": "co-run-001",
    "node_name": "await_client_signoff",
    "reason": "Change order 3 on project 'Riverside Tower': cost delta $18500.00, schedule delta 6 days. Approve, reject, or counter?",
    "payload": {
      "change_order": {"ChangeOrderID": 3, "CostDelta": 18500.0},
      "approving_employee_id": 2,
      "decision_key": "hitl_decision_co3_v1"
    },
    "created_at": "2026-08-20T14:30:00Z",
    "graph_name": "change_order",
    "current_state": {
      "run_id": "co-run-001",
      "change_order_id": 3,
      "employee_id": 1
    }
  }
]
```

#### `POST /api/hitl/resolve`

Resolve and **automatically resume** the graph.

```bash
curl -X POST http://localhost:8000/api/hitl/resolve \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": 42,
    "decision": "approved",
    "resolved_by": 2
  }'
```

**Counter with note:**
```bash
curl -X POST http://localhost:8000/api/hitl/resolve \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": 42,
    "decision": "countered",
    "resolved_by": 2,
    "counter_note": "Reduce cost by 10%"
  }'
```

#### `GET /api/hitl/stats`

Dashboard stats.

```bash
curl http://localhost:8000/api/hitl/stats
```

---

### Ticket Inbox API

#### `POST /api/tickets/inbox`

List tickets (open/investigating/resolved) with error context and persisted state.

```bash
curl -X POST http://localhost:8000/api/tickets/inbox \
  -H "Content-Type: application/json" \
  -d '{"status": "open"}'
```

**Response:**
```json
[
  {
    "ticket_id": 7,
    "run_id": "co-run-002",
    "node_name": "decompose_change_order",
    "error_message": "Missing 'request' payload in state.",
    "status": "open",
    "graph_name": "change_order",
    "current_state": {"invalid_key": true}
  }
]
```

#### `POST /api/tickets/resolve`

Resolve with optional state corrections and **resume from checkpoint** (not restart).

```bash
curl -X POST http://localhost:8000/api/tickets/resolve \
  -H "Content-Type: application/json" \
  -d '{
    "ticket_id": 7,
    "resolution": "Operator supplied the missing request payload.",
    "updated_state": {
      "request": {
        "project_id": 1, "employee_id": 1,
        "description": "Fixed after ticket", "cost_delta": 1000.0,
        "schedule_delta_days": 1
      }
    }
  }'
```

#### `POST /api/tickets/investigate/{ticket_id}?admin_id={id}`

Mark as investigating.

```bash
curl -X POST http://localhost:8000/api/tickets/investigate/7?admin_id=2
```

#### `GET /api/tickets/stats`

Dashboard stats.

```bash
curl http://localhost:8000/api/tickets/stats
```

---

### RAG Document Management API

The RAG routes use an **adapter pattern** that works with ANY RAG backend. Set `RAG_BACKEND_CLASS=module.Class` to use a custom backend.

#### `GET /api/rag-docs/list`

List indexed documents.

```bash
curl http://localhost:8000/api/rag-docs/list
```

#### `POST /api/rag-docs/add`

Upload, chunk, index, and invalidate cache.

```bash
curl -X POST http://localhost:8000/api/rag-docs/add \
  -F "file=@safety_policy.pdf" \
  -F "title=Updated Safety Policy 2024"
```

#### `POST /api/rag-docs/remove`

Remove and invalidate cache.

```bash
curl -X POST http://localhost:8000/api/rag-docs/remove \
  -H "Content-Type: application/json" \
  -d '{"doc_id": "a1b2c3d4e5f67890"}'
```

#### `GET /api/rag-docs/doc/{doc_id}`

View full content.

```bash
curl http://localhost:8000/api/rag-docs/doc/a1b2c3d4e5f67890
```

#### `GET /api/rag-docs/stats`

Dashboard stats.

```bash
curl http://localhost:8000/api/rag-docs/stats
```

---

## Key Design Decisions

### HITL vs Ticket: Distinct Code Paths

| Aspect | HITL | Ticket |
|--------|------|--------|
| **Trigger** | `HITLPause` raised intentionally | Any `Exception` caught by `_loop()` |
| **Store table** | `HITLTasks` | `Tickets` |
| **Status** | `pending` → `resolved` | `open` → `investigating` → `resolved` |
| **Admin action** | approve / reject / counter | resolution text + optional state correction |
| **Resume** | `resolve_hitl_task()` merges decision | `resolve_ticket()` flips status to `running` |
| **Graph behavior** | Same node re-executes, sees decision | Same node re-executes with corrected state |

### Graph Auto-Resume

Both `/hitl/resolve` and `/tickets/resolve` automatically resume the graph:

1. Admin posts resolution
2. Route calls `resolve_hitl_task()` or `resolve_ticket()` on checkpoint store
3. Route imports correct graph builder based on `GraphName` from `StateGraphRuns`
4. Route calls `graph.run(run_id)` — checkpoint store returns last state
5. Graph re-executes paused/failed node and continues

The admin sees the resumed state in the JSON response.

### Decision Key from Payload

HITL resolution reads `decision_key` from the task's stored payload (e.g. `hitl_decision_co3_v1`), ensuring resubmitted change orders (Version > 1) resolve correctly.

### Per-Agent Tool Scoping

Since `server.py` has a single global tool set, scoping is enforced at the bridge API boundary via `AGENT_TOOL_SCOPES`. Agents querying their tool list through the platform only see allowed tools.

### RAG Cache Invalidation

After every add/remove, `invalidate()` runs 5 strategies:

1. **Version sentinel** — `rag/corpus_version.txt` bumped
2. **Touch sentinel** — `rag/invalidation_sentinel.txt` updated for file-watchers
3. **B2's function** — `rag.index_manager.invalidate_cache()` if available
4. **Agent direct** — `agent.agent.invalidate_rag_cache()` if available
5. **Vector store reload** — `rag.vector_store.reload_index()` if available

### Transport: Why HTTP Matters

`mcp._tool_manager._tools` lives in the server process's memory. Over stdio, each `connect()` spawns a new subprocess — deregistrations are invisible to other sessions. The bridge mandates HTTP for real deployments.

---

#
