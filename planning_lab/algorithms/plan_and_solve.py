from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel

# Resolve project root for cross-module imports
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class PlanAndSolveError(Exception):
    """Raised when plan-and-solve fails after all retries."""
    pass


def plan_and_solve(
    question: str,
    llm: BaseChatModel,
    temperature: float = 0.2,
    max_retries: int = 2,
    retry_delay_sec: float = 1.0,
) -> str:
    """Execute Plan-and-Solve prompting with automatic retry on empty responses.

    Args:
        question: The planning task to solve.
        llm: The language model to use.
        temperature: Sampling temperature for generation.
        max_retries: Number of retry attempts on empty/invalid responses.
        retry_delay_sec: Delay between retries (for rate limiting).

    Returns:
        The generated solution text.

    Raises:
        PlanAndSolveError: If all retry attempts fail.
    """
    system_prompt = (
        "You are an expert construction project planner. "
        "Execute Plan-and-Solve prompting. First outline a plan, then immediately provide "
        "the full execution details including actionable mitigation steps, explicit cost/budget checks, "
        "and direct trade/supplier notifications."
    )
    user_prompt = f"""{question}

Formulate the complete solution directly addressing the problem with actionable steps."""

    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            response = llm.invoke([
                ("system", system_prompt),
                ("human", user_prompt),
            ], temperature=temperature)

            if not isinstance(response.content, str) or not response.content.strip():
                raise RuntimeError("The chat model returned an empty or unsupported response")

            return response.content.strip()

        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                time.sleep(retry_delay_sec)
            continue

    raise PlanAndSolveError(
        f"Failed after {max_retries} attempt(s). Last error: {type(last_error).__name__}: {last_error}"
    ) from last_error
