# IronBridge Operations Platform

> **Purpose:** The product surface where administrators manage the AI system and end users interact with it.

The IronBridge Operations Platform is the web application that sits between the company's people and its agents. It is not a static mockup or a collection of screenshots. It is a live, working application wired end-to-end against the MCP server, the database, and every agent in the system.

---

## Architecture

The platform follows a clean separation between backend and frontend:

- **Backend** (`backend/`): A Python web application that exposes REST endpoints for agent execution, tool management, document management, HITL resolution, and ticket resolution. It bridges the frontend to the live MCP server and database.
- **Frontend** (`frontend/`): HTML, CSS, and JavaScript interfaces. No build step is required; the pages are served directly or via any static file server.

All communication between frontend and backend happens through the REST API. There are no mock responses, no hardcoded data, and no simulated agents.

---

## Backend

### Entry Point

`backend/app.py` initializes the web application and registers all route modules. It uses the same database connection pool and MCP client configuration as the rest of the system.

### Route Modules

| Module | Endpoint Group | Purpose |
|:---|:---|:---|
| `routes/agents.py` | `/api/agents` | List connected agents, view their configured tools, update tool assignments |
| `routes/chat.py` | `/api/chat` | Send messages to any agent, receive responses, manage conversation threads |
| `routes/tools.py` | `/api/tools` | List all tools registered on the MCP server, add or remove tools per agent |
| `routes/rag_docs.py` | `/api/rag/docs` | Upload documents to the RAG corpus, list current documents, remove documents |
| `routes/hitl.py` | `/api/hitl` | List pending HITL tasks, inspect checkpointed state, resolve tasks with a decision |
| `routes/tickets.py` | `/api/tickets` | List open tickets, inspect failure context, resolve tickets to resume runs |

### MCP Bridge

`backend/mcp_bridge.py` provides the runtime connection to the live MCP server. When an administrator adds or removes a tool from an agent through the admin panel, the bridge sends the appropriate registration or de-registration command to the MCP server. The change is effective immediately for the next agent call.

### Agent Runner

`backend/services/agent_runner.py` is the execution service that dispatches chat messages to the correct agent. It handles:
- Routing the message to the Memory & RAG agent, the Planning agent, or one of the three state-graph agents based on the user's selection
- Managing conversation persistence in the database
- Handling state-graph runs: starting new runs, resuming paused runs, and surfacing HITL or ticket status to the frontend

---

## Frontend

### User Chat Interface

`frontend/user/index.html` is the end-user-facing chat application.

**Features:**
- **Agent Switcher:** A sidebar or tab interface lets the user choose which agent to speak with. Available agents include:
  - **Policy Assistant** — the Memory & RAG agent for construction policy, safety manual, and material specification questions
  - **Planning Assistant** — the delay-response planning agent for schedule risk and mitigation strategy questions
  - **Change-Order Agent** — the stateful change-order negotiation workflow
  - **Equipment Recovery Agent** — the stateful equipment breakdown recovery workflow
  - **Safety Incident Agent** — the stateful safety incident response workflow
- **Persistent Conversations:** Chat threads are stored in the database. A user can refresh the page, close their browser, or switch devices and resume the same conversation.
- **Status Indicators:** When a state-graph agent is waiting for a human decision or has encountered a failure, the chat interface displays the current status (e.g., "Waiting for manager approval" or "Paused — ticket opened") so the user knows what is happening.

### Admin Dashboard

`frontend/admin/admin_panel.html` is the administrator-facing control center.

**Features:**

#### Agent & Tool Management
- View every agent currently connected to the MCP server
- See which tools are available to each agent
- Toggle tools on or off per agent. Changes are sent to the live MCP server immediately.
- Add new tools to the global registry or remove obsolete ones

#### RAG Document Management
- View the current set of documents in the RAG corpus
- Upload new documents (PDF, text, markdown). The document is indexed and becomes available for retrieval on the next query.
- Remove outdated documents. The removal invalidates the vector index entry immediately.

#### HITL Inbox
- View all pending human-in-the-loop tasks opened by any state-graph agent
- Each task displays:
  - The agent that opened it
  - The reason for the pause (e.g., "Cost exceeds $5,000 threshold")
  - The full checkpointed state at the point of pause, including all context the agent had gathered
  - The payload specific to the decision (e.g., the estimated cost, the equipment ID)
- The administrator can **Approve** or **Reject** with an optional comment
- Upon resolution, the underlying state-graph run resumes automatically, incorporating the administrator's decision

#### Ticket Board
- View all open failure tickets from any state-graph run
- Each ticket displays:
  - The run ID and agent name
  - The node that failed
  - The exception message and traceback
  - The full checkpointed state at the moment of failure
  - The timestamp of the failure
- The administrator can inspect the state, diagnose the root cause (e.g., a vendor API outage), fix the external condition, and click **Resolve**
- Upon resolution, the run resumes from the same checkpoint, re-executing the failed node

---

## Running the Platform

### Prerequisites

The MCP server and database must be running before the platform backend starts.

```bash
# 1. Start the MCP server (in one terminal)
python -m mcp_server.server

# 2. Ensure the database is initialized
python -m db.build_db
python -m db.migrate_state_graph
```

### Start the Backend

```bash
cd ib_platform/backend
pip install -r requirements.txt
python app.py
```

The backend will start on `http://localhost:5000` (or the port configured in your environment).

### Serve the Frontend

The frontend consists of static HTML files. You can open them directly in a browser, or serve them via any static file server:

```bash
cd ib_platform/frontend
python -m http.server 8080
```

Then navigate to:
- **User Interface:** `http://localhost:8080/user/index.html`
- **Admin Panel:** `http://localhost:8080/admin/admin_panel.html`

### API Base URL

The frontend expects the backend API at `http://localhost:5000/api/`. If you run the backend on a different host or port, update `frontend/shared_config.php` accordingly.

---

## End-to-End Workflows

### Adding a Tool to an Agent

1. Administrator opens the Admin Panel → Agent Management
2. Selects the agent (e.g., "Equipment Recovery Agent")
3. Toggles a new tool (e.g., `search_rental_vendors`) from "Available" to "Enabled"
4. The frontend sends a POST to `/api/tools/assign`
5. The backend calls `mcp_bridge.register_tool_for_agent()`
6. The MCP server adds the tool to the agent's runtime toolkit
7. The next message to the Equipment Recovery Agent can now call `search_rental_vendors`

### Uploading a RAG Document

1. Administrator opens the Admin Panel → Document Management
2. Clicks "Upload Document" and selects a PDF
3. The frontend sends a POST to `/api/rag/docs/upload`
4. The backend extracts text, chunks it, embeds it, and adds it to the vector store
5. A user asks the Policy Assistant a question related to the new document
6. The retrieval agent finds the new chunk and includes it in its answer

### Resolving a HITL Task

1. A Change-Order Agent run hits the HITL node because the estimated cost is $12,000 against a remaining budget of $8,000
2. The graph pauses, checkpoints, and a task appears in the HITL Inbox
3. The project manager opens the Admin Panel → HITL Inbox
4. They see the task: "Cost estimate ($12,000) exceeds remaining budget ($8,000). Requires approval to proceed."
5. They click **Reject** with the comment: "Client must absorb the overrun or reduce scope."
6. The backend calls `store.resolve_hitl_task(task_id, "rejected", "project_manager")`
7. The run resumes, the `approval` node returns `"rejected"`, and the graph routes to the `negotiate_scope_reduction` node

### Resolving a Ticket

1. An Equipment Recovery Agent run fails during the `search_rentals` node because the vendor API returns a 503
2. The node throws an exception, the runner checkpoints, and a ticket appears on the Ticket Board
3. The operations manager sees the ticket: "Vendor API unreachable — 503 Service Unavailable"
4. They verify the vendor status page, confirm the outage, and wait
5. Once the vendor is back online, they click **Resolve** with the comment: "Vendor API restored."
6. The backend calls `store.resolve_ticket(ticket_id, "Vendor API restored.")`
7. The run resumes from the `search_rentals` node, re-executes the vendor call, and continues

---

## Directory Map

```
ib_platform/
├── backend/
│   ├── app.py                  # Application entry point
│   ├── mcp_bridge.py           # Live MCP server bridge
│   ├── requirements.txt        # Python dependencies
│   ├── routes/
│   │   ├── agents.py           # Agent listing and tool assignment
│   │   ├── chat.py             # User chat endpoints
│   │   ├── tools.py            # Tool registry endpoints
│   │   ├── rag_docs.py         # Document management endpoints
│   │   ├── hitl.py             # HITL task resolution endpoints
│   │   └── tickets.py          # Ticket resolution endpoints
│   └── services/
│       └── agent_runner.py     # Agent execution dispatcher
└── frontend/
    ├── index.html              # Landing page
    ├── shared_config.php       # API base URL configuration
    ├── admin/
    │   └── admin_panel.html    # Admin dashboard (tools, docs, HITL, tickets)
    └── user/
        └── index.html          # User chat interface
```

---

## Security Notes

- No API keys or database credentials are committed to the repository. All secrets are read from environment variables or a `.env` file that is listed in `.gitignore`.
- The admin panel and user interface are served as separate pages. In a production deployment, the admin panel should be protected by authentication.
- File uploads are validated for type and size before being processed by the RAG pipeline.

