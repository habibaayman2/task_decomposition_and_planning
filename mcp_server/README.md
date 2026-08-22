# IronBridge MCP Server

> **Purpose:** Unified tool registry and execution layer for all IronBridge AI agents.

The Model Context Protocol (MCP) server is the shared infrastructure that exposes IronBridge's operational data as callable tools. Every agent in the system — the Memory & RAG agent, the Planning agent, and all three state-graph agents — connects to this same server. There are no parallel tool layers, no duplicated database connections, and no agent-specific hacks.

---

## Responsibilities

1. **Tool Definition:** Each tool is defined with a clear schema, description, and validation rules. Agents receive these definitions and use them to construct valid calls.
2. **Tool Execution:** When an agent calls a tool, the server executes the corresponding function against the live database and returns the result.
3. **Runtime Registration:** Tools can be registered and de-registered at runtime through the platform's admin panel. A tool added from the UI is live immediately; a tool removed is immediately unavailable to all agents.
4. **Validation:** All tool inputs are validated before execution. Invalid inputs are rejected with a descriptive error that the agent can use to correct its call.

---

## Tool Categories

### Project & Budget

| Tool | Description |
|:---|:---|
| `get_project` | Retrieve project details by ID |
| `list_projects` | List all active projects |
| `update_project_budget` | Adjust the remaining budget for a project |

### Materials & Suppliers

| Tool | Description |
|:---|:---|
| `get_material_stock` | Check current stock level for a material |
| `list_materials` | List all materials in the system |
| `get_supplier_status` | Check whether a supplier is active and their lead time |

### Contractors

| Tool | Description |
|:---|:---|
| `get_contractor` | Retrieve contractor details and availability |
| `list_contractors` | List all contractors |

### Equipment

| Tool | Description |
|:---|:---|
| `get_equipment` | Retrieve equipment details and maintenance history |
| `list_equipment` | List all equipment |
| `update_equipment_status` | Update the operational status of a piece of equipment |

### Safety & Compliance

| Tool | Description |
|:---|:---|
| `log_safety_incident` | Record a new safety incident |
| `get_safety_policy` | Retrieve a safety policy document by topic |

---

## Files

| File | Purpose |
|:---|:---|
| `server.py` | MCP protocol implementation, tool registry, and execution dispatcher |
| `db.py` | Database access layer — all SQL queries live here |
| `http_app.py` | HTTP bridge that exposes the MCP server to the platform backend |
| `validation.py` | Input validation schemas for all tool arguments |
| `policies/` | Policy documents and templates for safety and compliance tools |

---

## Running the Server

```bash
python -m mcp_server.server
```

The server starts on the configured port and waits for MCP client connections from the agents and the platform backend.

---

## Runtime Tool Management

The platform backend communicates with the MCP server through `http_app.py` to add or remove tools dynamically:

- **Register:** `POST /tools/register` — adds a new tool to the live registry
- **Deregister:** `POST /tools/deregister` — removes a tool from the live registry
- **List:** `GET /tools` — returns all currently registered tools

These endpoints are called by the admin panel when an administrator toggles a tool for an agent.
