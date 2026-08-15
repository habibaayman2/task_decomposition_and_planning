"""
tests/conftest.py
Shared pytest fixtures.
"""

import pytest
from planning.model_provider import DeterministicPlanningLLM


@pytest.fixture(scope="session")
def deterministic_llm():
    return DeterministicPlanningLLM(call_count=[0])