"""
C4: a single CLI that wraps all three real graphs (change_order,
equipment_recovery, safety_incident) the same way
state_graph/demo_crash_resume.py wrapped its toy graph for C1 -- this
is the script the plan names as state_graph/cli.py, and the one to
kill mid-run on camera for the crash/resume proof.

Why this proves crash/resume for real (not just the toy graph):
  graph.run(run_id, ...) is the SAME call whether run_id is brand new,
  mid-flight after a HITL pause, or was just killed by Ctrl+C -- see
  core/graph_base.py's own docstring on StateGraph.run(). Every node
  transition is checkpointed to StateGraphCheckpoints/StateGraphRuns
  BEFORE the runner advances (core/checkpoint_store.py), so killing
  the process loses at most the ONE node that was in flight; every
  node that already completed is never re-run.

Usage (from repo root, venv active):

    # Start (or resume) a run. Same command either way.
    python -m state_graph.cli run safety_incident demo-1

    # Same but with your own initial state instead of the built-in demo one
    python -m state_graph.cli run change_order demo-2 --initial-state '{"run_id": "demo-2", "request": {"project_id": 1, "employee_id": 1, "description": "Add a ramp", "cost_delta": 4200, "schedule_delta_days": 3}}'

    # While a run is in flight (e.g. mid an LLM call), Ctrl+C it, then:
    python -m state_graph.cli run safety_incident demo-1     # <-- resumes, does not restart

    # Inspect a run without advancing it
    python -m state_graph.cli status demo-1
    python -m state_graph.cli history demo-1                 # full checkpoint trail -- the actual proof
    python -m state_graph.cli list-runs
    python -m state_graph.cli list-runs --graph safety_incident

    # See what's waiting on a human / stuck as a ticket
    python -m state_graph.cli pending-hitl
    python -m state_graph.cli open-tickets

Recording tip: `run`, kill it with Ctrl+C while it's printing an LLM
call (decompose / investigate / diagnose -- any real node takes a
couple of seconds, no artificial sleep needed), then run `history
<run_id>` to show the checkpoint trail BEFORE resuming, run `run`
again to resume, then `history <run_id>` again side by side -- the
node that was interrupted appears exactly once more; nothing earlier
duplicates.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict

# --------------------------------------------------------------------------
# Path & Module Resolution (same pattern every state_graph module uses)
# --------------------------------------------------------------------------
_current_dir = Path(__file__).resolve().parent
REPO_ROOT = next(
    (p for p in [_current_dir] + list(_current_dir.parents) if (p / "mcp_server").exists()),
    _current_dir.parent,
)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from state_graph.core.checkpoint_store import default_store
from state_graph.change_order.graph import build_change_order_graph
from state_graph.equipment_recovery.graph import build_equipment_recovery_graph
from state_graph.safety_incident.graph import build_safety_incident_graph


# --------------------------------------------------------------------------
# Graph registry
# --------------------------------------------------------------------------
# NOTE for the cross-review: the three graphs do NOT all take the same
# initial_state shape. change_order and safety_incident wrap the
# caller's payload as {"run_id": ..., "request": {...}} (their nodes
# do task decomposition on `request`, which may even be a free-text
# string); equipment_recovery takes a flat dict with no wrapper and no
# run_id key at all (see report_breakdown/nodes.py). That is not a bug
# -- each graph's own start_new_*()/report node defines its own
# contract -- but it is easy to trip over, so the default templates
# below match each graph's ACTUAL contract exactly rather than forcing
# one shape onto all three.

def _default_change_order_state(run_id: str) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "request": {
            "project_id": 1,
            "employee_id": 1,
            "description": "Client requested an additional accessibility ramp at the east entrance.",
            "cost_delta": 4200.0,
            "schedule_delta_days": 3,
        },
    }


def _default_equipment_recovery_state(run_id: str) -> Dict[str, Any]:
    return {
        "equipment_id": 1,
        "project_id": 1,
        "site": "Riverside Tower",
        "reported_symptom": "Loader making a grinding noise on startup",
    }


def _default_safety_incident_state(run_id: str) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "request": {
            "project_id": 1,
            "employee_id": 1,
            "description": "Worker reported near-miss with unsecured scaffolding on level 4",
            "severity": "high",
        },
    }


GRAPHS: Dict[str, Dict[str, Any]] = {
    "change_order": {
        "build": build_change_order_graph,
        "default_initial_state": _default_change_order_state,
    },
    "equipment_recovery": {
        "build": build_equipment_recovery_graph,
        "default_initial_state": _default_equipment_recovery_state,
    },
    "safety_incident": {
        "build": build_safety_incident_graph,
        "default_initial_state": _default_safety_incident_state,
    },
}


def _resolve_graph(name: str) -> Dict[str, Any]:
    if name not in GRAPHS:
        raise SystemExit(f"Unknown graph '{name}'. Choices: {', '.join(GRAPHS)}")
    return GRAPHS[name]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> None:
    spec = _resolve_graph(args.graph)
    graph = spec["build"]()

    existing = default_store.load(args.run_id)
    if existing is None:
        if args.initial_state:
            initial_state = json.loads(args.initial_state)
        else:
            initial_state = spec["default_initial_state"](args.run_id)
        print(f"[cli] no checkpoint for '{args.run_id}' -- starting a NEW {args.graph} run")
        print(f"[cli] initial_state = {json.dumps(initial_state, indent=2)}")
    else:
        _, current_node, status = existing
        if args.initial_state:
            print(f"[cli] WARNING: --initial-state ignored -- '{args.run_id}' already exists "
                  f"(status={status}, next node={current_node}); resuming instead.")
        print(f"[cli] found checkpoint for '{args.run_id}' -- RESUMING at node "
              f"'{current_node}' (status was '{status}')")
        initial_state = None  # unused when resuming; graph.run() reads the checkpoint itself

    final_state = graph.run(args.run_id, initial_state=initial_state)
    _, current_node, status = default_store.load(args.run_id)

    print()
    print(f"[cli] run '{args.run_id}' is now: {status}"
          + (f" (paused/stuck at '{current_node}')" if status != "completed" else ""))
    if status == "paused_hitl":
        print("[cli] a human decision is required -- see: python -m state_graph.cli pending-hitl")
    elif status == "ticket_open":
        print("[cli] an unplanned failure was ticketed -- see: python -m state_graph.cli open-tickets")
    print(f"[cli] state keys: {sorted(final_state.keys())}")


def cmd_status(args: argparse.Namespace) -> None:
    loaded = default_store.load(args.run_id)
    if loaded is None:
        print(f"No run found for run_id='{args.run_id}'.")
        return
    state, current_node, status = loaded
    print(f"RunID:        {args.run_id}")
    print(f"Status:       {status}")
    print(f"Current/next node: {current_node}")
    print(f"State:")
    print(json.dumps(state, indent=2))


def cmd_history(args: argparse.Namespace) -> None:
    from mcp_server.db import get_conn

    with get_conn() as conn:
        run = conn.execute(
            "SELECT GraphName, Status, CurrentNode, CreatedAt, UpdatedAt "
            "FROM StateGraphRuns WHERE RunID = ?",
            (args.run_id,),
        ).fetchone()
        if run is None:
            print(f"No run found for run_id='{args.run_id}'.")
            return

        rows = conn.execute(
            "SELECT CheckpointID, NodeName, Status, CreatedAt "
            "FROM StateGraphCheckpoints WHERE RunID = ? ORDER BY CheckpointID ASC",
            (args.run_id,),
        ).fetchall()

    print(f"Graph: {run['GraphName']}   Current status: {run['Status']}   "
          f"Started: {run['CreatedAt']}   Last update: {run['UpdatedAt']}")
    print()
    print(f"{'#':<4} {'Node':<28} {'Status':<14} {'Timestamp'}")
    print("-" * 70)
    for r in rows:
        print(f"{r['CheckpointID']:<4} {r['NodeName']:<28} {r['Status']:<14} {r['CreatedAt']}")
    print()
    print(f"({len(rows)} checkpoint row(s) total -- this is the append-only trail. "
          f"A node that completed once and was never re-entered appears exactly once.)")


def cmd_list_runs(args: argparse.Namespace) -> None:
    from mcp_server.db import get_conn

    query = "SELECT RunID, GraphName, Status, CurrentNode, UpdatedAt FROM StateGraphRuns"
    params: tuple = ()
    if args.graph:
        query += " WHERE GraphName = ?"
        params = (args.graph,)
    query += " ORDER BY UpdatedAt DESC LIMIT ?"
    params = params + (args.limit,)

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    if not rows:
        print("No runs found.")
        return

    print(f"{'RunID':<28} {'Graph':<20} {'Status':<14} {'Node':<26} {'Updated'}")
    print("-" * 100)
    for r in rows:
        print(f"{r['RunID']:<28} {r['GraphName']:<20} {r['Status']:<14} "
              f"{r['CurrentNode']:<26} {r['UpdatedAt']}")


def cmd_pending_hitl(args: argparse.Namespace) -> None:
    tasks = default_store.list_pending_hitl_tasks()
    if not tasks:
        print("No pending HITL tasks.")
        return
    for t in tasks:
        print(f"TaskID={t['TaskID']}  RunID={t['RunID']}  Node={t['NodeName']}")
        print(f"  Reason: {t['Reason']}")
        print()


def cmd_open_tickets(args: argparse.Namespace) -> None:
    tickets = default_store.list_open_tickets()
    if not tickets:
        print("No open tickets.")
        return
    for tk in tickets:
        print(f"TicketID={tk['TicketID']}  RunID={tk['RunID']}  Node={tk['NodeName']}")
        print(f"  Error: {tk['ErrorMessage']}")
        print()


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m state_graph.cli",
        description="Start, resume, and inspect state_graph runs (change_order / "
                     "equipment_recovery / safety_incident) -- the crash/resume proof CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Start a new run, or resume an existing one (same command).")
    p_run.add_argument("graph", choices=sorted(GRAPHS.keys()))
    p_run.add_argument("run_id")
    p_run.add_argument(
        "--initial-state",
        help="JSON initial state, only used when starting a NEW run. "
             "If omitted, a built-in demo payload is used. Ignored when resuming.",
    )
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="Show a run's current node, status, and full state.")
    p_status.add_argument("run_id")
    p_status.set_defaults(func=cmd_status)

    p_history = sub.add_parser("history", help="Show the full checkpoint trail for a run (the crash-proof evidence).")
    p_history.add_argument("run_id")
    p_history.set_defaults(func=cmd_history)

    p_list = sub.add_parser("list-runs", help="List recent runs across all graphs.")
    p_list.add_argument("--graph", choices=sorted(GRAPHS.keys()), default=None)
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list_runs)

    p_pending = sub.add_parser("pending-hitl", help="List all pending HITL tasks across all runs.")
    p_pending.set_defaults(func=cmd_pending_hitl)

    p_tickets = sub.add_parser("open-tickets", help="List all open tickets across all runs.")
    p_tickets.set_defaults(func=cmd_open_tickets)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
