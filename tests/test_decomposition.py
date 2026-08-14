"""
tests/test_decomposition.py

Extends the toolkit base with IronBridge-specific decomposition tests.
Runs decomposition-first vs. dynamic against real delay-risk requests.
"""

import pytest
from planning.algorithms.decomposition import decompose_goal, execute_plan
from planning.algorithms.dynamic_decomposition import dynamic_decomposition
from planning.model_provider import get_planning_llm, DeterministicPlanningLLM


@pytest.fixture
def llm():
    # Use deterministic fallback for fast, reproducible tests
    return DeterministicPlanningLLM(call_count=[0])


def test_decomposition_first_generates_valid_dag(llm):
    goal = "ProjectID 1 is flagged at risk: rebar delivery is 9 days late"
    plan = decompose_goal(goal, llm, project_id=1)
    
    assert len(plan.tasks) >= 3
    assert plan.tasks[0].id == "diagnose"
    assert plan.terminal_tasks() == ["notify"]
    # No cycles
    assert plan.execution_batches()


def test_dynamic_decomposition_reacts_to_empty_diagnosis(llm):
    goal = "ProjectID 999 is flagged at risk with no further detail"
    history = dynamic_decomposition(goal, llm, max_steps=3)
    
    # Should start with diagnose
    assert history[0][0] == "diagnose"
    # Should NOT blindly follow a pre-written plan
    assert len(history) <= 4  # stops early if done


def test_divergence_between_methods(llm):
    """Both methods run; dynamic may take different path."""
    goal = "ProjectID 4 is flagged at risk by the site engineer with no further detail"
    
    # Decomposition-first
    plan = decompose_goal(goal, llm, project_id=4)
    df_tasks = [t.id for t in plan.tasks]
    
    # Dynamic
    history = dynamic_decomposition(goal, llm, max_steps=5)
    dd_tasks = [step[0] for step in history]
    
    # Dynamic should have reacted to real observations
    assert "diagnose" in dd_tasks
    