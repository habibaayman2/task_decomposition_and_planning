

from __future__ import annotations

from pathlib import Path
import sys
import json
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------
_current_file = Path(__file__).resolve()
BACKEND_DIR = _current_file.parent.parent
REPO_ROOT = next(
    (p for p in [_current_file] + list(_current_file.parents) if (p / "mcp_server").exists()),
    BACKEND_DIR.parent
)

for path_entry in (str(REPO_ROOT), str(BACKEND_DIR)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from state_graph.core.checkpoint_store import default_store

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class TicketOut(BaseModel):
    ticket_id: int
    run_id: str
    node_name: str
    error_message: str
    status: str
    resolution: Optional[str] = None
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None
    graph_name: Optional[str] = None
    current_state: Optional[Dict[str, Any]] = None


class TicketResolution(BaseModel):
    ticket_id: int = Field(ge=1)
    resolution: str = Field(min_length=5, max_length=1000, description="Human explanation of the fix")
    updated_state: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional state corrections to apply before resuming (e.g. fix a missing key)"
    )


class TicketFilter(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(open|investigating|resolved)$")
    graph_name: Optional[str] = None
    run_id: Optional[str] = None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.post("/inbox", response_model=List[TicketOut])
def list_tickets(filters: Optional[TicketFilter] = None):
    """List tickets. Defaults to open + investigating. Includes full run
    state at the point of failure so the admin can diagnose."""
    filters = filters or TicketFilter()

    # Determine which statuses to fetch
    if filters.status:
        status_filter = filters.status
    else:
        status_filter = None  # fetch all, then filter

    # Query from store
    try:
        with default_store._get_conn() as conn:
            if status_filter:
                rows = conn.execute(
                    "SELECT * FROM Tickets WHERE Status = ? ORDER BY CreatedAt DESC",
                    (status_filter,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM Tickets ORDER BY CreatedAt DESC"
                ).fetchall()
    except Exception as e:
        # Fallback to default_store's method
        all_open = default_store.list_open_tickets()
        rows = all_open
        if status_filter == "resolved":
            # list_open_tickets only returns non-resolved; we'd need a separate query
            pass

    results: List[TicketOut] = []
    for row in rows:
        # Apply run_id filter
        if filters.run_id and row.get("RunID") != filters.run_id:
            continue

        # Enrich with run state and graph name
        run_state = None
        graph_name = None
        checkpoint = default_store.load(row["RunID"])
        if checkpoint:
            state, node, status = checkpoint
            run_state = state
            try:
                with default_store._get_conn() as conn:
                    g_row = conn.execute(
                        "SELECT GraphName FROM StateGraphRuns WHERE RunID = ?",
                        (row["RunID"],)
                    ).fetchone()
                    if g_row:
                        graph_name = g_row["GraphName"]
            except Exception:
                pass

        # Apply graph_name filter
        if filters.graph_name and graph_name != filters.graph_name:
            continue

        results.append(TicketOut(
            ticket_id=row["TicketID"],
            run_id=row["RunID"],
            node_name=row["NodeName"],
            error_message=row["ErrorMessage"],
            status=row.get("Status", "open"),
            resolution=row.get("Resolution"),
            created_at=row.get("CreatedAt"),
            resolved_at=row.get("ResolvedAt"),
            graph_name=graph_name,
            current_state=run_state,
        ))

    return results


@router.get("/ticket/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: int):
    """Get a single ticket by ID with full persisted failure state."""
    try:
        with default_store._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM Tickets WHERE TicketID = ?",
                (ticket_id,)
            ).fetchone()
    except Exception:
        row = None

    if not row:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")

    run_state = None
    graph_name = None
    checkpoint = default_store.load(row["RunID"])
    if checkpoint:
        state, node, status = checkpoint
        run_state = state
        try:
            with default_store._get_conn() as conn:
                g_row = conn.execute(
                    "SELECT GraphName FROM StateGraphRuns WHERE RunID = ?",
                    (row["RunID"],)
                ).fetchone()
                if g_row:
                    graph_name = g_row["GraphName"]
        except Exception:
            pass

    return TicketOut(
        ticket_id=row["TicketID"],
        run_id=row["RunID"],
        node_name=row["NodeName"],
        error_message=row["ErrorMessage"],
        status=row.get("Status", "open"),
        resolution=row.get("Resolution"),
        created_at=row.get("CreatedAt"),
        resolved_at=row.get("ResolvedAt"),
        graph_name=graph_name,
        current_state=run_state,
    )


@router.post("/resolve")
def resolve_ticket(resolution: TicketResolution):
    """Resolve an open ticket and resume the underlying graph run from its
    last checkpoint -- NOT from the beginning.

    The admin may supply corrected state (e.g. a missing 'request' payload)
    which is merged into the checkpoint before resuming. The failed node
    re-executes with the corrected state.
    """
    # Verify ticket exists and is not already resolved
    try:
        with default_store._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM Tickets WHERE TicketID = ?",
                (resolution.ticket_id,)
            ).fetchone()
    except Exception:
        row = None

    if not row:
        raise HTTPException(status_code=404, detail=f"Ticket {resolution.ticket_id} not found")

    if row.get("Status") == "resolved":
        raise HTTPException(status_code=400, detail=f"Ticket {resolution.ticket_id} is already resolved")

    # Resolve via checkpoint store
    try:
        run_id = default_store.resolve_ticket(
            ticket_id=resolution.ticket_id,
            resolution=resolution.resolution,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Apply state corrections if provided
    if resolution.updated_state:
        loaded = default_store.load(run_id)
        if loaded:
            state, current_node, status = loaded
            state.update(resolution.updated_state)
            default_store.save_checkpoint(run_id, state, current_node, status="running")

    # Resume the graph
    graph_name = None
    try:
        with default_store._get_conn() as conn:
            g_row = conn.execute(
                "SELECT GraphName FROM StateGraphRuns WHERE RunID = ?",
                (run_id,)
            ).fetchone()
            if g_row:
                graph_name = g_row["GraphName"]
    except Exception:
        pass

    resumed_state = _resume_graph(run_id, graph_name)

    return {
        "ticket_id": resolution.ticket_id,
        "run_id": run_id,
        "status": "resolved_and_resumed",
        "resumed_state": resumed_state,
    }


def _resume_graph(run_id: str, graph_name: Optional[str]) -> Dict[str, Any]:
    """Resume a graph run after ticket resolution."""
    if graph_name == "change_order":
        from state_graph.change_order.graph import build_change_order_graph
        graph = build_change_order_graph()
    elif graph_name == "equipment_recovery":
        try:
            from state_graph.equipment_recovery.graph import build_equipment_recovery_graph
            graph = build_equipment_recovery_graph()
        except ImportError:
            raise HTTPException(status_code=500, detail=f"Graph '{graph_name}' not available")
    elif graph_name == "safety_incident":
        try:
            from state_graph.safety_incident.graph import build_safety_incident_graph
            graph = build_safety_incident_graph()
        except ImportError:
            raise HTTPException(status_code=500, detail=f"Graph '{graph_name}' not available")
    else:
        raise HTTPException(status_code=500, detail=f"Unknown graph name: {graph_name}")

    return graph.run(run_id)


@router.post("/investigate/{ticket_id}")
def mark_investigating(ticket_id: int, admin_id: int):
    """Mark a ticket as 'investigating' so other admins know someone is
    looking at it."""
    try:
        with default_store._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM Tickets WHERE TicketID = ?",
                (ticket_id,)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
            if row.get("Status") == "resolved":
                raise HTTPException(status_code=400, detail="Cannot investigate a resolved ticket")

            conn.execute(
                "UPDATE Tickets SET Status = 'investigating' WHERE TicketID = ?",
                (ticket_id,)
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"ticket_id": ticket_id, "status": "investigating", "admin_id": admin_id}


@router.get("/stats")
def ticket_stats():
    """Dashboard stats: open/investigating/resolved counts by graph."""
    try:
        with default_store._get_conn() as conn:
            rows = conn.execute(
                "SELECT Status, COUNT(*) as cnt FROM Tickets GROUP BY Status"
            ).fetchall()
            status_counts = {r["Status"]: r["cnt"] for r in rows}
    except Exception:
        status_counts = {}

    # By graph
    by_graph: Dict[str, Dict[str, int]] = {}
    try:
        with default_store._get_conn() as conn:
            rows = conn.execute(
                "SELECT t.Status, r.GraphName, COUNT(*) as cnt "
                "FROM Tickets t JOIN StateGraphRuns r ON t.RunID = r.RunID "
                "GROUP BY t.Status, r.GraphName"
            ).fetchall()
            for r in rows:
                g = r["GraphName"] or "unknown"
                s = r["Status"]
                by_graph.setdefault(g, {})[s] = by_graph[g].get(s, 0) + r["cnt"]
    except Exception:
        pass

    return {
        "total_by_status": status_counts,
        "by_graph": by_graph,
        "currently_open": status_counts.get("open", 0) + status_counts.get("investigating", 0),
    }