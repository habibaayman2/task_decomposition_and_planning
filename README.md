# IronBridge AI Operations Platform

&gt; **Company:** IronBridge Construction  
&gt; **System:** Multi-Agent AI Platform with Persistent State, Human Oversight, and Live Operations Management

---

## Overview

IronBridge Construction operates across multiple active job sites, each with its own materials pipeline, contractor schedules, equipment fleet, and safety profile. Coordinating these moving parts in real time is not a task that can be solved with a single prompt or a linear script. A delay on one site ripples into procurement decisions on another. A safety incident triggers a chain of notifications, inspections, and work stoppages that may span days. A change order from a client must be priced, approved, and executed without breaking the budget of the original project.

This repository houses the complete AI operations platform built for IronBridge. It is not a collection of disconnected demos. It is a single, integrated system where every agent shares the same MCP server, the same procurement database, the same document store, and the same operational platform that real users and administrators interact with every day.

The system is organized into four layers:

| Layer | Purpose | Key Components |
|:---|:---|:---|
| **Platform** (`ib_platform/`) | Where people meet the agents | Admin dashboard, user chat interface, HITL inbox, ticket board |
| **State Graphs** (`state_graph/`) | Agents that hold state across time | Change-order negotiation, equipment recovery, safety-incident response |
| **Planning & Memory** (`planning/`, `agent/`, `memory/`, `rag/`) | Agents that reason and remember | Delay-response planning, policy Q&A, document retrieval |
| **Infrastructure** (`mcp_server/`, `db/`) | Shared tools and data | Runtime tool registry, SQLite database, RAG vector store |

---

## The Three Stateful Problems

Construction work is inherently asynchronous. A machine breaks on Friday evening. A safety inspector's report arrives Monday morning. A client's change-order request sits in email for three days. Each of these scenarios requires an agent that can start work, pause, wait for something outside its control, and resume exactly where it left off — without losing context, without re-executing completed steps, and without making irreversible decisions without human approval.

### 1. Change-Order Negotiation Agent

**The problem:** A client requests a scope change — an extra floor, a materials upgrade, a timeline shift. The project manager must price the change, check it against the remaining budget, obtain client approval, and then re-sequence the remaining work. This process can stretch across multiple days and multiple rounds of back-and-forth.

**Why it needs a state graph:** The agent cannot price the change until it queries current stock and contractor availability. It cannot execute the change until a human approves the cost. It cannot re-sequence the schedule until the client confirms. Each of these is a genuine wait or a genuine branch.

**Techniques used:**
- **Task Decomposition** — The agent breaks the change-order response into a sequence of verifiable sub-tasks: impact assessment, cost estimation, approval gating, and schedule resequencing.
- **Constrained ReAct** — When the agent proposes a revised schedule, it operates within a whitelist of permissible actions and validates every proposal against live database constraints (budget, stock, contractor status).

### 2. Equipment Recovery Agent

**The problem:** A critical piece of equipment fails on site. The site engineer needs a replacement fast, but the optimal path depends on whether a rental is available nearby, whether the budget can absorb the cost, and whether a repair might be faster. If the rental exceeds a threshold, a project manager must sign off.

**Why it needs a state graph:** The agent must diagnose the failure, search for alternatives, price them, and then stop for human approval before committing spend. If a rental vendor's API is down, the run must fail cleanly, open a ticket, and resume once the vendor is reachable again — not start over from diagnosis.

**Techniques used:**
- **Tree of Thoughts** — The agent explores multiple recovery strategies (rental, repair, subcontract, schedule shift) in parallel, scoring each against time-to-recovery and cost before committing.
- **Constrained ReAct** — The execution node that books a rental or calls a repair service is constrained by a whitelist and a budget ceiling. Any action that would exceed the threshold triggers a human-in-the-loop pause.

### 3. Safety Incident Response Agent

**The problem:** A safety incident is reported on site. The agent must triage severity, notify the relevant parties, schedule an inspection, and recommend corrective actions. Some actions — like ordering an immediate work stoppage or evacuating a section — have real operational cost and must not be taken autonomously.

**Why it needs a state graph:** Severity classification may need to wait for a photo upload or a witness statement. The inspection may be scheduled for the next business day. A work-stoppage order must be approved by the safety officer. The agent must hold its state across these gaps.

**Techniques used:**
- **LATS (Language Agent Tree Search)** — The agent searches over candidate response orderings, scoring each path against a real severity rubric and regulatory checklist rather than its own intuition.
- **Constrained ReAct** — Only whitelisted low-severity actions (notifications, documentation) execute automatically. Any action that would stop work or evacuate triggers a human-in-the-loop pause routed to the safety officer through the platform.

---

## Shared Infrastructure

### MCP Server

The `mcp_server/` directory contains the Model Context Protocol server that exposes IronBridge's operational data as tools. Every agent — whether stateful or single-pass — calls the same server. The server supports runtime registration and de-registration of tools, managed through the admin dashboard. A tool added from the platform is live for the next agent call; a tool removed is immediately unavailable.

Key tool categories:
- **Project & Budget:** `get_project`, `update_project_budget`, `list_projects`
- **Materials & Suppliers:** `get_material_stock`, `get_supplier_status`, `list_materials`
- **Contractors:** `get_contractor`, `list_contractors`
- **Equipment:** `get_equipment`, `update_equipment_status`, `list_equipment`
- **Safety & Compliance:** `log_safety_incident`, `get_safety_policy`

### Database

All agents read from and write to `db/procurement.db`, a single SQLite database. The schema covers projects, materials, suppliers, contractors, equipment, safety incidents, and — for the state-graph layer — runs, checkpoints, HITL tasks, and tickets. There is no parallel database for the new work; everything extends the existing schema.

### RAG Document Store

The Memory & RAG agent maintains a vector store of construction policies, safety manuals, and material specifications. Administrators add and remove documents through the platform's admin panel, and the retrieval agent's answers reflect the current corpus on its next query.

---

## Platform

The `ib_platform/` directory contains the full-stack web application that serves as the product surface for the entire system.

### For Administrators

The admin panel (`ib_platform/frontend/admin/`) provides:
- **Agent Registry:** View every agent connected to the MCP server. Add or remove tools from each agent's available toolkit. Changes propagate to the live server immediately.
- **Document Management:** Upload new documents to the RAG corpus or remove outdated ones. The retrieval agent sees the updated corpus on its next query.
- **HITL Inbox:** Review pending human-in-the-loop tasks opened by any state-graph agent. Inspect the full persisted state at the point of pause. Approve or reject with a comment, and watch the underlying run resume.
- **Ticket Board:** Review open failure tickets from any state-graph run. See the checkpointed state at the moment of failure, the exception that caused it, and the node where it occurred. Resolve the ticket after fixing the root cause, and the run resumes from the same checkpoint.

### For End Users

The user chat interface (`ib_platform/frontend/user/`) provides:
- **Agent Switching:** A sidebar or tab interface lets the user choose which agent to speak with — the Memory & RAG agent for policy questions, the Planning Agent for delay-response scenarios, or any of the three state-graph agents for long-running operational workflows.
- **Persistent Threads:** Conversations with state-graph agents survive page refreshes, browser closures, and even server restarts. The user can close their laptop, reopen it the next morning, and pick up the same conversation exactly where it left off.

---

## State Graph Architecture

### Checkpointing

Every state graph writes its full state to durable storage after every meaningful transition. This is not a log file written after the fact; it is a first-class checkpoint that makes crash recovery possible. If the process is killed mid-run — demonstrated in `state_graph/demo_crash_resume.py` — the run resumes from its last checkpoint with no re-execution of completed steps and no loss of collected state.

The checkpoint store lives in `state_graph/core/checkpoint_store.py`. It persists to the same SQLite database the rest of the system uses.

### Human-in-the-Loop

An explicit `HITL` node type is implemented in `state_graph/core/hitl.py`. When a node encounters a condition that requires human judgment — a cost above a threshold, an action that contradicts policy, a confidence score below a bar — the graph pauses, persists its full state, and opens a task on the platform. The graph resumes only after an administrator acts through the platform's UI, and the resumed run picks up the administrator's decision as part of its state.

HITL pauses are expected. They are part of the normal flow. They are distinct from failures.

### Tickets and Failure Recovery

When a node fails unexpectedly — a tool call errors, a schema validation fails, the model returns something the graph cannot act on — the runner catches the exception, checkpoints the state at the moment of failure, and opens a ticket with status `open`. The ticket is inspectable on the platform, and once the underlying issue is resolved, the ticket is marked resolved and the run resumes from the same checkpoint — not restarted from the beginning.

Tickets and HITL tasks follow different code paths, have different database tables, and surface in different sections of the admin panel.

---

## Repository Map
