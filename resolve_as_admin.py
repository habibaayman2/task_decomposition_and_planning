"""Simulates an admin resolving the pending HITL task, using
Person A's REAL /api/hitl endpoints -- not a Python shortcut."""
import requests

BASE = "http://localhost:8000"

# 1. See what's pending in the admin's inbox
resp = requests.post(f"{BASE}/api/hitl/inbox", json={})
resp.raise_for_status()
tasks = resp.json()

print(f"Pending HITL tasks: {len(tasks)}")
for t in tasks:
    print(f"  task_id={t['task_id']} run_id={t['run_id']} reason={t['reason']}")

if not tasks:
    print("No pending tasks -- nothing to resolve.")
    exit()

# 2. Resolve the most recent one (approve it)
latest = tasks[-1]
resp = requests.post(f"{BASE}/api/hitl/resolve", json={
    "task_id": latest["task_id"],
    "decision": "approved",
    "resolved_by": 1,
})
print(f"\nResolve response ({resp.status_code}):")
print(resp.json())