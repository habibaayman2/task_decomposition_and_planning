from state_graph.equipment_recovery.nodes import report_breakdown

state = {
    "request": "The crane has a hydraulic failure.",
    "project_id": 1,
    "equipment_name": "Hydraulic Crane",
    "site": "Site A",
    "session_id": "test-123"
}

try:
    result = report_breakdown(state)
    print("SUCCESS:", result)
except Exception as e:
    import traceback
    traceback.print_exc()