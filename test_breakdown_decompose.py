"""Quick standalone test for report_breakdown's new free-text decomposition."""
from state_graph.equipment_recovery.nodes import report_breakdown

# Test 1: structured input (old behavior, must still work unchanged)
structured_state = {
    "equipment_id": 1,
    "project_id": 1,
    "site": "Riverside Tower",
    "reported_symptom": "Grinding noise on startup",
}
result1 = report_breakdown(structured_state)
print("TEST 1 (structured):", result1)
assert result1["equipment_id"] == 1
print("✅ Test 1 passed\n")

# Test 2: free text input (new LLM decomposition path)
free_text_state = {
    "request": "The excavator at Riverside Tower is making a loud grinding "
               "noise when I start it up. It's equipment number 1, project 1.",
}
result2 = report_breakdown(free_text_state)
print("TEST 2 (free text):", result2)
assert result2["equipment_id"] == 1, f"Expected equipment_id=1, got {result2.get('equipment_id')}"
assert result2["project_id"] == 1, f"Expected project_id=1, got {result2.get('project_id')}"
print("✅ Test 2 passed\n")

print("🎉 ALL DECOMPOSITION TESTS PASSED.")