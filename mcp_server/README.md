
# mcp_server/

The MCP server itself, built on the `mcp` Python SDK's FastMCP API.

- **`server.py`** — tool, resource, and prompt definitions; every protocol concern (capability negotiation, notifications, elicitation, resources, prompts, transport, progress tracking, sampling, defensive tool design) is tagged with a `# === CONCERN: ... ===` comment
- **`db.py`** — SQLite access layer; all database reads/writes go through here
- **`validation.py`** — server-side business-rule validation and role-based authorization, independent of the tool schemas
- **`http_app.py`** — Streamable HTTP transport wiring for production deployment
- **`policies/`** — three safety-policy documents (Material Handling Procedures, Warehouse Safety Regulations, Equipment Operation Safety Rules) exposed to the model as resources, not tools

## Run

```bash
pip install -r requirements.txt

# stdio (local dev, default)
python server.py

# Streamable HTTP (production)
TRANSPORT=http IRONBRIDGE_API_TOKEN=devtoken python server.py
```

Requires a database built from `../db/schema.sql` + `../db/seed.sql` first (see `db/README.md`).
