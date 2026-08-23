from state_graph.equipment_recovery.nodes import approval_gate

# Simulate state after evaluate_options
state = {
    "equipment_id": 1,
    "project_id": 1,
    "site": "Site A",
    "reported_symptom": "hydraulic failure",
    "diagnosis": "Leaking seal in main hydraulic cylinder",
    "diagnosis_confidence": 0.85,
    "proposed_action": "repair",
    "proposed_cost": 15000,  # More than budget (10000) to trigger HITL
    "proposal_rationale": "Replace seal, 2-day downtime",
    "rejected_options": [],
}

try:
    result = approval_gate(state)
    print("SUCCESS:", result)
except Exception as e:
    import traceback
    traceback.print_exc()