# IronBridge Database

> **Purpose:** Single source of truth for all operational data, agent state, and platform records.

The IronBridge database is a single SQLite database (`procurement.db`) shared by every component of the system. There are no separate databases for agents, no parallel stores for state-graph checkpoints, and no external caches that can drift out of sync.

---

## Schema Files

| File | Tables | Purpose |
|:---|:---|:---|
| `schema.sql` | Projects, Materials, Suppliers, Contractors, Equipment | Core operational schema |
| `state_graph_schema.sql` | StateGraphRuns, StateGraphCheckpoints, HITLTasks, Tickets | Stateful agent persistence |
| `chat_schema.sql` | ChatThreads, ChatMessages | Platform conversation history |

---

## Core Tables

### Operational Data

- **Projects:** Project ID, name, location, start date, end date, total budget, remaining budget, status
- **Materials:** Material ID, name, unit cost, current stock, reorder threshold, supplier ID
- **Suppliers:** Supplier ID, name, contract status, lead time days, contact info
- **Contractors:** Contractor ID, name, specialty, availability, daily rate
- **Equipment:** Equipment ID, name, type, site assignment, status, last maintenance date

### State Graph Persistence

- **StateGraphRuns:** Run ID, graph name, current status (running / paused / failed / completed), created timestamp, updated timestamp
- **StateGraphCheckpoints:** Checkpoint ID, run ID, node name, full state JSON, timestamp
- **HITLTasks:** Task ID, run ID, node name, reason, payload JSON, status (pending / resolved), decision, resolved by, resolved at
- **Tickets:** Ticket ID, run ID, node name, exception message, state JSON, status (open / investigating / resolved), resolution, resolved at

### Chat History

- **ChatThreads:** Thread ID, user ID, agent name, created timestamp
- **ChatMessages:** Message ID, thread ID, role (user / agent), content, timestamp

---

## Initialization

```bash
# Build the core operational database
python -m db.build_db

# Add state-graph tables
python -m db.migrate_state_graph

# Seed with demo data
sqlite3 db/procurement.db < db/seed.sql
```

Safe to re-run. `migrate_state_graph.py` uses `IF NOT EXISTS` for all table creation.

---

## Entity Relationship Diagram

See `db/ERD.png` for a visual overview of the core operational schema.
