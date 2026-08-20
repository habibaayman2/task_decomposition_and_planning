
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

router = APIRouter(prefix="/api/hitl", tags=["hitl"])


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class HITLTaskOut(BaseModel):
    task_id: int
    run_id: str
    node_name: str
    reason: str
    payload: Dict[str, Any]
    created_at: Optional[str] = None
    graph_name: Optional[str] = None
    current_state: Optional[Dict[str, Any]] = None


class HITLResolution(BaseModel):
    task_id: int = Field(ge=1)
    decision: str = Field(pattern=r"^(approved|rejected|countered)$")
    resolved_by: int = Field(ge=1, description="Admin employee ID")
    counter_note: Optional[str] = Field(None, max_length=500)


class HITLFilter(BaseModel):
    graph_name: Optional[str] = None
    run_id: Optional[str] = None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.post("/inbox", response_model=List[HITLTaskOut])
def list_hitl_inbox(filters: Optional[HITLFilter] = None):
    """List all pending HITL tasks across all state_graph agents.

    Returns full task details including the persisted run state at the
    pause point, so the admin can inspect context before deciding.
    """
    filters = filters or HITLFilter()
    pending = default_store.list_pending_hitl_tasks()

    results: List[HITLTaskOut] = []
    for task in pending:
        # Apply filters
        if filters.run_id and task.get("RunID") != filters.run_id:
            continue

        # Enrich with run state and graph name
        run_state = None
        graph_name = None
        checkpoint = default_store.load(task["RunID"])
        if checkpoint:
            state, node, status = checkpoint
            run_state = state
            # Graph name is not directly in checkpoint; infer from state or store
            # The StateGraphRuns table has GraphName
            try:
                with default_store._get_conn() as conn:
                    row = conn.execute(
                        "SELECT GraphName FROM StateGraphRuns WHERE RunID = ?",
                        (task["RunID"],)
                    ).fetchone()
                    if row:
                        graph_name = row["GraphName"]
            except Exception:
                pass

        payload = {}
        if task.get("PayloadJSON"):
            try:
                payload = json.loads(task["PayloadJSON"])
            except json.JSONDecodeError:
                pass

        results.append(HITLTaskOut(
            task_id=task["TaskID"],
            run_id=task["RunID"],
            node_name=task["NodeName"],
            reason=task["Reason"],
            payload=payload,
            created_at=task.get("CreatedAt"),
            graph_name=graph_name,
            current_state=run_state,
        ))

    # Filter by graph_name if requested
    if filters.graph_name:
        results = [r for r in results if r.graph_name == filters.graph_name]

    return results


@router.get("/task/{task_id}", response_model=HITLTaskOut)
def get_hitl_task(task_id: int):
    """Get a single HITL task by ID, including full persisted state."""
    pending = default_store.list_pending_hitl_tasks()
    task = next((t for t in pending if t["TaskID"] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail=f"HITL task {task_id} not found or already resolved")

    run_state = None
    graph_name = None
    checkpoint = default_store.load(task["RunID"])
    if checkpoint:
        state, node, status = checkpoint
        run_state = state
        try:
            with default_store._get_conn() as conn:
                row = conn.execute(
                    "SELECT GraphName FROM StateGraphRuns WHERE RunID = ?",
                    (task["RunID"],)
                ).fetchone()
                if row:
                    graph_name = row["GraphName"]
        except Exception:
            pass

    payload = {}
    if task.get("PayloadJSON"):
        try:
            payload = json.loads(task["PayloadJSON"])
        except json.JSONDecodeError:
            pass

    return HITLTaskOut(
        task_id=task["TaskID"],
        run_id=task["RunID"],
        node_name=task["NodeName"],
        reason=task["Reason"],
        payload=payload,
        created_at=task.get("CreatedAt"),
        graph_name=graph_name,
        current_state=run_state,
    )


@router.post("/resolve")
def resolve_hitl_task(resolution: HITLResolution):
    """Resolve a pending HITL task and resume the underlying graph run.

    This is the ONLY way a graph resumes after a HITL pause -- the admin
    must act through this platform UI. The decision is persisted into the
    checkpoint store under the correct decision_key (read from the task's
    payload), and the run status is flipped to 'running' so the next
    graph.run() call picks it up.
    """
    # Verify task exists and is pending
    pending = default_store.list_pending_hitl_tasks()
    task = next((t for t in pending if t["TaskID"] == resolution.task_id), None)
    if not task:
        raise HTTPException(
            status_code=404,
            detail=f"HITL task {resolution.task_id} not found or already resolved"
        )

    # Extract decision_key from task payload (cycle-safe key from nodes.py)
    decision_key = "hitl_decision"  # fallback
    if task.get("PayloadJSON"):
        try:
            payload = json.loads(task["PayloadJSON"])
            decision_key = payload.get("decision_key", decision_key)
        except json.JSONDecodeError:
            pass

    # Resolve via checkpoint store
    try:
        run_id = default_store.resolve_hitl_task(
            task_id=resolution.task_id,
            decision=resolution.decision,
            resolved_by=resolution.resolved_by,
            decision_key=decision_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # If countered, attach counter_note to state
    if resolution.decision == "countered" and resolution.counter_note:
        fresh_state, fresh_node, fresh_status = default_store.load(run_id)
        fresh_state["counter_note"] = resolution.counter_note
        default_store.save_checkpoint(run_id, fresh_state, fresh_node, status=fresh_status)

    # Trigger graph resume
    from state_graph.core.graph_base import StateGraph
    # Import the correct graph builder based on graph name
    graph_name = None
    try:
        with default_store._get_conn() as conn:
            row = conn.execute(
                "SELECT GraphName FROM StateGraphRuns WHERE RunID = ?",
                (run_id,)
            ).fetchone()
            if row:
                graph_name = row["GraphName"]
    except Exception:
        pass

    # Resume the graph
    resumed_state = _resume_graph(run_id, graph_name)

    return {
        "task_id": resolution.task_id,
        "run_id": run_id,
        "decision": resolution.decision,
        "status": "resolved_and_resumed",
        "resumed_state": resumed_state,
    }


def _resume_graph(run_id: str, graph_name: Optional[str]) -> Dict[str, Any]:
    """Resume a graph run after HITL resolution. Dispatches to the correct
    graph builder based on graph_name."""
    if graph_name == "change_order":
        from state_graph.change_order.graph import build_change_order_graph
        graph = build_change_order_graph()
    elif graph_name == "equipment_recovery":
        # Placeholder -- import when implemented
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


@router.get("/stats")
def hitl_stats():
    """Dashboard stats: pending count by graph, oldest pending task."""
    pending = default_store.list_pending_hitl_tasks()
    by_graph: Dict[str, int] = {}
    oldest = None

    for task in pending:
        graph = "unknown"
        try:
            with default_store._get_conn() as conn:
                row = conn.execute(
                    "SELECT GraphName FROM StateGraphRuns WHERE RunID = ?",
                    (task["RunID"],)
                ).fetchone()
                if row:
                    graph = row["GraphName"]
        except Exception:
            pass

        by_graph[graph] = by_graph.get(graph, 0) + 1

        if oldest is None or task.get("CreatedAt", "") < oldest.get("CreatedAt", ""):
            oldest = task

    return {
        "total_pending": len(pending),
        "by_graph": by_graph,
        "oldest_task_id": oldest["TaskID"] if oldest else None,
        "oldest_task_age_hours": _hours_since(oldest.get("CreatedAt")) if oldest else None,
    }


def _hours_since(timestamp_str: Optional[str]) -> Optional[float]:
    if not timestamp_str:
        return None
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        return (datetime.utcnow() - dt.replace(tzinfo=None)).total_seconds() / 3600
    except Exception:
        return None