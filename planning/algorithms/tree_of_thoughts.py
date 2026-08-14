from __future__ import annotations

import sys
import time
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field

# Resolve project root for cross-module imports
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from models import Thought


class ThoughtCandidates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[str] = Field(min_length=1, max_length=3)


# NOTE (fix #1, kept from previous patch): ThoughtEvaluationItem MUST be
# defined before ThoughtEvaluations, which references it in
# `list[ThoughtEvaluationItem]`. With `from __future__ import annotations`
# active, that annotation is a string (forward reference) resolved by
# looking up the name in this module's namespace at class-creation time.
# Defining it below caused `.annotation.__args__` to come back empty --
# the "IndexError: tuple index out of range" seen in model_provider.py.
class ThoughtEvaluationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_index: int = Field(ge=0)
    score: float = Field(ge=0.0, le=1.0)
    rationale: str


class ThoughtEvaluations(BaseModel):
    """Batch evaluation schema — evaluates multiple candidates in ONE LLM call."""
    model_config = ConfigDict(extra="forbid")
    evaluations: list[ThoughtEvaluationItem] = Field(min_length=1)


default_root_state = "Initial project status review"


# ---------------------------------------------------------------------------
# NOTE (fix #2): llama-3.3-70b-versatile on Groq intermittently returns a
# malformed tool call for ThoughtEvaluations -- either a truncated/incorrect
# closing tag or a field-order hiccup -- which raises groq.BadRequestError
# ("Failed to call a function") before it ever reaches our pydantic
# validation. This is a known rough edge of Groq's function-calling mode
# with multi-item batch schemas, not a bug in our schema itself. A short
# bounded retry with a stricter follow-up instruction resolves it in
# practice without masking a real, persistent failure (it still raises
# after `retries` attempts).
# ---------------------------------------------------------------------------

def _invoke_structured_with_retry(
    llm: BaseChatModel,
    schema: type[BaseModel],
    messages: list[tuple[str, str]],
    temperature: float,
    retries: int = 2,
):
    structured = llm.with_structured_output(schema, method="function_calling")
    last_err: Exception | None = None
    attempt_messages = list(messages)
    for attempt in range(retries + 1):
        try:
            return structured.invoke(attempt_messages, temperature=temperature)
        except Exception as e:
            last_err = e
            attempt_messages = list(messages) + [
                (
                    "human",
                    "Your previous response was not a valid call for this schema "
                    "(wrong fields, wrong order, or malformed). Call the function "
                    "again with EXACTLY the fields the schema defines, in a single "
                    "well-formed call, and nothing else.",
                )
            ]
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise last_err


def tree_of_thoughts(
    problem: str,
    llm: BaseChatModel,
    depth: int = 2,
    beam_width: int = 2,
    root_state: str | None = None,
    batch_evaluate: bool = True,
) -> list[Thought]:
    """Beam search over thought candidates with optional batch evaluation.

    Args:
        problem: The task to solve.
        llm: The language model.
        depth: Number of expansion rounds.
        beam_width: Top candidates to keep per round.
        root_state: Optional custom root state.
        batch_evaluate: If True, evaluate all candidates in a single LLM call
            instead of one per candidate (saves ~50% of evaluation LLM calls).
    """
    if depth < 1 or beam_width < 1:
        raise ValueError("depth and beam_width must be positive")

    frontier = [Thought(state=root_state or default_root_state, score=0.5, rationale="root")]

    for _ in range(depth):
        candidates: list[Thought] = []

        for parent in frontier:
            generated = _invoke_structured_with_retry(
                llm,
                ThoughtCandidates,
                [
                    ("system", (
                        "Generate distinct, fully formed candidate solution plans. "
                        "Each proposal must contain specific actions and explicit budget/schedule verification."
                    )),
                    ("human", f"""Problem: {problem}
Previous step: {parent.state}

Propose two distinct, complete, actionable continuations."""),
                ],
                temperature=0.4,
            )

            raw_candidates = generated.candidates[:2]
            if not raw_candidates:
                continue

            if batch_evaluate and len(raw_candidates) > 1:
                eval_prompt = "\n\n".join(
                    f"[{i}] {state}" for i, state in enumerate(raw_candidates)
                )
                batch_result = _invoke_structured_with_retry(
                    llm,
                    ThoughtEvaluations,
                    [
                        ("system", (
                            "Evaluate multiple construction planning strategies independently. "
                            "For each candidate, assign a score (0.0-1.0) and a brief rationale."
                        )),
                        ("human", f"""Problem: {problem}

Candidates to evaluate:
{eval_prompt}

Call the function ONCE with an "evaluations" list containing exactly one
{{candidate_index, score, rationale}} object per candidate above, indexed 0..N-1.
Do not include any other fields."""),
                    ],
                    temperature=0.1,
                )

                eval_map = {e.candidate_index: e for e in batch_result.evaluations}
                for i, state in enumerate(raw_candidates):
                    ev = eval_map.get(i)
                    if ev is None:
                        score = 0.5
                        rationale = "Batch evaluation missing; assigned neutral score."
                    else:
                        score = ev.score
                        rationale = ev.rationale
                    candidates.append(Thought(state=state, score=score, rationale=rationale))
                    if score >= 1.0:
                        return [candidates[-1]]
            else:
                for state in raw_candidates:
                    judged = _invoke_structured_with_retry(
                        llm,
                        ThoughtEvaluationItem,
                        [
                            ("system", "Independently evaluate a construction planning strategy."),
                            ("human", f"""Problem: {problem}
Candidate strategy: {state}
Score feasibility, actionable detail, and budget adherence (0.0 to 1.0)."""),
                        ],
                        temperature=0.1,
                    )
                    candidates.append(
                        Thought(state=state, score=judged.score, rationale=judged.rationale)
                    )
                    if judged.score >= 1.0:
                        return [candidates[-1]]

        frontier = sorted(candidates, key=lambda item: item.score, reverse=True)[:beam_width]
        if not frontier:
            break

    return frontier
