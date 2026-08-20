"""
Trivial proof-of-concept for C1: a cyclic graph you can kill mid-run
and resume with no re-execution of completed steps and no state lost.
This is the script the Day-4 crash/resume recording is built on
(state_graph/cli.py, per the team plan, can wrap real graphs the same
way once they exist).

The graph: step_one -> step_two -> (loop back to step_one twice, then
finish). step_two sleeps for a few seconds specifically so you have a
window to Ctrl+C the process while it's "mid-node".

Usage (from repo root, venv active, after running the migration once):

    python -m db.migrate_state_graph          # one-time, if not done yet
    python -m state_graph.demo_crash_resume run demo-1

Then, while it prints "[step_two] ... kill me now", hit Ctrl+C. Run the
SAME command again:

    python -m state_graph.demo_crash_resume run demo-1

You'll see it resume from the checkpointed node -- step_one for that
visit is NOT re-printed/re-executed, and the visit counter continues
from where it stopped, proving state survived the crash.
"""

import sys
import time

from state_graph.core.graph_base import StateGraph, END
from state_graph.core.checkpoint_store import CheckpointStore

TOTAL_VISITS = 3


def step_one(state):
    visits = state.get("visits", 0) + 1
    print(f"[step_one] visit #{visits}")
    return {"visits": visits}


def step_two(state):
    print(f"[step_two] processing visit #{state['visits']} of {TOTAL_VISITS} "
          f"... sleeping 6s, kill me now (Ctrl+C) to test crash/resume")
    time.sleep(6)
    print(f"[step_two] visit #{state['visits']} done")
    return {}


def route_after_step_two(state):
    if state.get("visits", 0) < TOTAL_VISITS:
        return "step_one"  # cycle back -- this is why it's a graph, not a DAG
    return END


def build_demo_graph() -> StateGraph:
    g = StateGraph("crash_resume_demo")
    g.add_node("step_one", step_one)
    g.add_node("step_two", step_two)
    g.set_entry("step_one")
    g.add_edge("step_one", "step_two")
    g.add_conditional_edge("step_two", route_after_step_two)
    return g


def main():
    if len(sys.argv) != 3 or sys.argv[1] != "run":
        print(__doc__)
        sys.exit(1)

    run_id = sys.argv[2]
    graph = build_demo_graph()
    store = CheckpointStore()

    existing = store.load(run_id)
    if existing is None:
        print(f"Starting new run '{run_id}'")
    else:
        _, current_node, status = existing
        print(f"Resuming run '{run_id}' from checkpoint: "
              f"node='{current_node}', status='{status}'")

    final_state = graph.run(run_id, initial_state={}, store=store)
    print(f"Run stopped/finished. Final state: {final_state}")


if __name__ == "__main__":
    main()
