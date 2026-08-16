"""planning/algorithms/reflexion.py

Reflexion: retry the entire task across multiple trials, carrying a capped
episodic buffer of verbal reflections from prior failed trials into the next
attempt.

Grounded mode uses IronBridgeEnvironment.evaluate() — source of truth:
mcp_server/db.py (RemainingBudget, Supplier.ContractStatus, Material stock).

Ungrounded mode asks the LLM to score itself — source of truth: the model's
own opinion, with no external validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel

from ..models import EnvironmentFeedback
from .environment import IronBridgeEnvironment


def _approx_tokens(*texts: str) -> int:
    """Rough token estimate: 1 token ~= 4 characters."""
    return sum(len(t) for t in texts) // 4


@dataclass
class ReflexionTrial:
    number: int
    attempt: str
    feedback: EnvironmentFeedback
    reflection: str | None = None
    llm_calls: int = 0


@dataclass
class ReflexionResult:
    success: bool
    output: str
    trials: list[ReflexionTrial]
    memory: list[str]
    total_llm_calls: int = 0
    approx_tokens: int = 0


def _ungrounded_self_evaluate(task: str, attempt: str, llm: BaseChatModel) -> EnvironmentFeedback:
    """Ungrounded self-evaluation via with_structured_output(EnvironmentFeedback)
    instead of a regex-parsed free-text reply -- same schema IronBridgeEnvironment
    .evaluate() returns, so grounded and ungrounded runs stay directly comparable,
    and there's no brittle "score[:\\s=]+(\\d+)" parsing to fall out of sync with
    however the model happens to phrase its answer."""
    structured_llm = llm.with_structured_output(EnvironmentFeedback)
    return structured_llm.invoke([
        ("system", "You are an independent evaluator. You have no access to the real "
                   "database -- judge based only on what's written in the attempt."),
        ("human", f"""Task: {task}
Attempt:
{attempt}

Judge this attempt. Return success (true/false), a score between 0.0 and 1.0,
and a short list of specific issues in details (empty list if none)."""),
    ])


def reflexion(
    task: str,
    llm: BaseChatModel,
    environment: Optional[IronBridgeEnvironment] = None,
    max_trials: int = 3,
    memory_size: int = 3,
) -> ReflexionResult:
    """
    Reflexion loop: attempt -> evaluate -> reflect -> retry with memory.

    Args:
        task: The planning problem to solve.
        llm: The language model.
        environment: If provided, grounded evaluation against the real DB.
                     If None, ungrounded self-evaluation by the LLM.
        max_trials: Maximum retry attempts.
        memory_size: Max reflections to carry across trials.
    """
    if max_trials < 1 or memory_size < 1:
        raise ValueError("max_trials and memory_size must be positive")

    memory: list[str] = []
    trials: list[ReflexionTrial] = []
    best_attempt = ""
    best_score = -1.0
    total_llm_calls = 0

    for number in range(1, max_trials + 1):
        recalled = "\n".join(f"- {item}" for item in memory[-memory_size:]) or "- No prior trials."

        # ---- Attempt ----
        response = llm.invoke([
            ("system", "You are the acting agent in a Reflexion loop. Attempt the entire task again."),
            ("human", f"""Task: {task}
Episodic memory from previous failed trials:
{recalled}

Produce the complete deliverable. Apply remembered lessons without discussing them."""),
        ], temperature=0.2)
        attempt = response.content
        if not isinstance(attempt, str) or not attempt.strip():
            raise RuntimeError("The chat model returned an empty or unsupported response")
        attempt = attempt.strip()
        total_llm_calls += 1

        # ---- Evaluate ----
        if environment is not None:
            # Grounded: real DB check
            feedback = environment.evaluate(attempt)
        else:
            # Ungrounded: LLM judges itself
            feedback = _ungrounded_self_evaluate(task, attempt, llm)
            total_llm_calls += 1

        trial = ReflexionTrial(
            number=number,
            attempt=attempt,
            feedback=feedback,
            llm_calls=total_llm_calls,
        )
        if feedback.score > best_score:
            best_attempt, best_score = attempt, feedback.score

        if feedback.success:
            trials.append(trial)
            tokens = sum(_approx_tokens(t.attempt, t.reflection or "") for t in trials)
            return ReflexionResult(
                True, attempt, trials, memory[-memory_size:], total_llm_calls, tokens
            )

        # ---- Reflect ----
        response = llm.invoke([
            ("system", "Generate a concise first-person Reflexion memory, not a revised answer."),
            ("human", f"""Task: {task}
Failed attempt:
{attempt}

External environment feedback (score {feedback.score}):
{chr(10).join('- ' + item for item in feedback.details)}

State what I did wrong and the specific strategy I should use next trial. Start with 'I'."""),
        ], temperature=0.2)
        reflection = response.content
        if not isinstance(reflection, str) or not reflection.strip():
            raise RuntimeError("The chat model returned an empty or unsupported response")
        reflection = reflection.strip()
        trial.reflection = reflection
        trials.append(trial)
        memory.append(reflection)
        total_llm_calls += 1

    tokens = sum(_approx_tokens(t.attempt, t.reflection or "") for t in trials)
    return ReflexionResult(
        False, best_attempt, trials, memory[-memory_size:], total_llm_calls, tokens
    )