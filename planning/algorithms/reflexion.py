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
    self_graded: bool = False
    """True when ungrounded evaluation fell back to using the same `llm`
    that generated the attempt (no judge_llm supplied) -- flags the
    known self-grading bias so callers can report/exclude it instead of
    silently treating it as an independent score."""


def _ungrounded_self_evaluate(
    task: str, attempt: str, judge_llm: BaseChatModel
) -> EnvironmentFeedback:
    """Ungrounded self-evaluation via with_structured_output(EnvironmentFeedback)
    instead of a regex-parsed free-text reply -- same schema IronBridgeEnvironment
    .evaluate() returns, so grounded and ungrounded runs stay directly comparable,
    and there's no brittle "score[:\\s=]+(\\d+)" parsing to fall out of sync with
    however the model happens to phrase its answer.

    judge_llm must be a genuinely separate model instance from whatever
    generated `attempt` -- see the self-grading-bias note on reflexion()
    below. Groq's json_mode requires the literal word 'json' to appear
    somewhere in the messages, hence the lowercase 'json' both places
    below (a capitalized-only "JSON" was silently rejected with a 400)."""
    structured_llm = judge_llm.with_structured_output(EnvironmentFeedback, method="json_mode")
    return structured_llm.invoke([
        ("system", "You are an independent evaluator. You have no access to the real "
                   "database -- judge based only on what's written in the attempt. "
                   "Respond only with valid json."),
        ("human", f"""Task: {task}
Attempt:
{attempt}

Judge this attempt. Return success (true/false), a score between 0.0 and 1.0,
and a short list of specific issues in details (empty list if none).
Respond with json only."""),
    ])


def reflexion(
    task: str,
    llm: BaseChatModel,
    environment: Optional[IronBridgeEnvironment] = None,
    max_trials: int = 3,
    memory_size: int = 3,
    judge_llm: Optional[BaseChatModel] = None,
) -> ReflexionResult:
    """
    Reflexion loop: attempt -> evaluate -> reflect -> retry with memory.

    Args:
        task: The planning problem to solve.
        llm: The language model used to generate attempts and reflections.
        environment: If provided, grounded evaluation against the real DB.
                     If None, ungrounded self-evaluation is used instead.
        max_trials: Maximum retry attempts.
        memory_size: Max reflections to carry across trials.
        judge_llm: The model used for UNGROUNDED self-evaluation (ignored
            when `environment` is provided, since grounded mode judges
            against the real DB instead). Pass a genuinely different
            model instance than `llm` here.

            SELF-GRADING BIAS: if judge_llm is left as None, ungrounded
            mode falls back to using `llm` itself to grade its own
            attempt. That is a known bias in the Reflexion literature --
            a model tends to rate its own output more favorably than an
            independent judge would, which is exactly the failure mode
            that makes "Reflexion (Ungrounded)" scores look artificially
            close to "Reflexion (Grounded)" in a comparison table when
            they shouldn't be directly comparable. The fallback exists
            so this function stays backward compatible for any caller
            that only has one model handy, but the result is flagged
            (`self_graded=True`) so it can be reported honestly rather
            than silently blended in with genuinely independent scores.
    """
    if max_trials < 1 or memory_size < 1:
        raise ValueError("max_trials and memory_size must be positive")

    self_graded = judge_llm is None
    evaluator_llm = judge_llm if judge_llm is not None else llm

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
            # Ungrounded: judged by evaluator_llm (judge_llm if the
            # caller supplied one, else self-graded by `llm` -- see
            # the self-grading-bias note in this function's docstring)
            feedback = _ungrounded_self_evaluate(task, attempt, evaluator_llm)
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
                True, attempt, trials, memory[-memory_size:], total_llm_calls, tokens,
                self_graded=self_graded and environment is None,
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
        False, best_attempt, trials, memory[-memory_size:], total_llm_calls, tokens,
        self_graded=self_graded and environment is None,
    )