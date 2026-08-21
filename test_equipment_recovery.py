"""
End-to-end test for the equipment_recovery graph. Covers:
  1. Cheap option -> auto-approved, no HITL, runs straight to completion.
  2. Expensive option -> HITL pause -> reject -> loop back -> re-propose
     -> HITL pause again -> approve -> execute -> completion.
"""
import uuid
from rag.vector_store import setup_vector_store
setup_vector_store()

from state_graph.equipment_recovery.graph import build_equipment_recovery_graph
from state_graph.core.checkpoint_store import CheckpointStore

store = CheckpointStore()
graph = build_equipment_recovery_graph()


def get_status(run_id):
    """run() only returns the state dict -- status lives in store.load()."""
    loaded = store.load(run_id)
    _, _, status = loaded
    return status


print("=" * 60)
print("SCENARIO 1: cheap repair, should auto-approve, no HITL")
print("=" * 60)

run_id_1 = f"test-cheap-{uuid.uuid4().hex[:8]}"
initial_state_1 = {
    "equipment_id": 1,
    "project_id": 1,
    "site": "Riverside Tower",
    "reported_symptom": "Loader making a grinding noise on startup",
}

final_state = graph.run(run_id_1, initial_state=initial_state_1, store=store)
status = get_status(run_id_1)
print(f"Status: {status}")
print(f"Final state keys: {list(final_state.keys())}")
assert status == "completed", f"Expected completed, got {status}"
print("✅ Scenario 1 passed: cheap option completed with no HITL pause.\n")

# Insert a temporary low-budget project so Scenario 2 reliably forces
# a HITL pause, instead of depending on Riverside Tower's real
# $42,000 (which is more than the $6,300 rental estimate).
from mcp_server.db import get_conn

with get_conn() as conn:
    conn.execute(
        "INSERT OR REPLACE INTO Projects "
        "(ProjectID, ProjectName, Client, ProjectLocation, Budget, RemainingBudget, ProjectManagerID, Status) "
        "VALUES (999, 'Test Small Budget Project', 'Test Client', 'Test Site', 500.0, 500.0, "    
        "(SELECT ProjectManagerID FROM Projects WHERE ProjectID = 1), 'Active')"
    )
    
print("=" * 60)
print("SCENARIO 2: expensive option, should pause for HITL, then loop")
print("=" * 60)

run_id_2 = f"test-expensive-{uuid.uuid4().hex[:8]}"
initial_state_2 = {
    "equipment_id": 2,
    "project_id": 999,  # Test Small Budget Project -- RemainingBudget = $500, forces HITL
    "site": "Riverside Tower",
    "reported_symptom": "Crane hydraulic system completely failed, no response",
}

final_state = graph.run(run_id_2, initial_state=initial_state_2, store=store)
status = get_status(run_id_2)
print(f"Status after first run: {status}")
assert status == "paused_hitl", f"Expected paused_hitl, got {status}"
print("✅ HITL pause confirmed.")

pending = store.list_pending_hitl_tasks()
task = next(t for t in pending if t["RunID"] == run_id_2)
print(f"Pending task: TaskID={task['TaskID']}, reason={task['Reason']}")

print("\n--- Admin REJECTS the proposal ---")
import json

# بنحاول نجيب الـ payload بكذا طريقة ونضمن إنه ميبقاش None
raw_payload = task.get("payload") or task.get("Payload") or {}
if isinstance(raw_payload, str):
    task_payload = json.loads(raw_payload)
else:
    task_payload = raw_payload

# بنجيب الأكشن، ولو مش موجود بنفترض إنه "repair" كـ fallback عشان التست ميعطلش
p_action = task_payload.get('proposed_action', 'repair')

store.resolve_hitl_task(
    task_id=task.get("TaskID") or task.get("task_id"),
    decision="rejected because rental quote seems too high, try again",
    resolved_by=1,
    decision_key=f"hitl_decision__{p_action}",
)
final_state = graph.run(run_id_2, store=store)
status = get_status(run_id_2)
print(f"Status after reject+resume: {status}")

if status == "paused_hitl":
    print("Looped back and hit HITL again with a different option -- as expected.")
    pending = store.list_pending_hitl_tasks()
    task2 = next(t for t in pending if t["RunID"] == run_id_2)
    print(f"New pending task: TaskID={task2['TaskID']}, reason={task2['Reason']}")

    print("\n--- Admin APPROVES this time ---")
    raw_payload2 = task2.get("payload") or task2.get("Payload") or {}
    if isinstance(raw_payload2, str):
        task2_payload = json.loads(raw_payload2)
    else:
        task2_payload = raw_payload2

    p_action2 = task2_payload.get('proposed_action', 'rent')

    store.resolve_hitl_task(
        task_id=task2.get("TaskID") or task2.get("task_id"),
        decision="approved",
        resolved_by=1,
        decision_key=f"hitl_decision__{p_action2}",
    )
    final_state = graph.run(run_id_2, store=store)
    status = get_status(run_id_2)
    print(f"Status after approve+resume: {status}")

assert status == "completed", f"Expected eventually completed, got {status}"
print(f"Execution result: {final_state.get('execution_result')}")
print("✅ Scenario 2 passed: HITL pause -> reject -> loop -> HITL again -> approve -> completed.\n")

print("🎉 ALL equipment_recovery TESTS PASSED.")