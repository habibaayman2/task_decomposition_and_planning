from __future__ import annotations

import sys
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


class ThoughtEvaluations(BaseModel):
    """Batch evaluation schema — evaluates multiple candidates in ONE LLM call."""
    model_config = ConfigDict(extra="forbid")
    evaluations: list[ThoughtEvaluationItem] = Field(min_length=1)


class ThoughtEvaluationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_index: int = Field(ge=0)
    score: float = Field(ge=0.0, le=1.0)
    rationale: str


default_root_state = "Initial project status review"


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
            generated = llm.with_structured_output(
                ThoughtCandidates,
                method="function_calling",
            ).invoke([
                ("system", (
                    "Generate distinct, fully formed candidate solution plans. "
                    "Each proposal must contain specific actions and explicit budget/schedule verification."
                )),
                ("human", f"""Problem: {problem}
Previous step: {parent.state}

Propose two distinct, complete, actionable continuations."""),
            ], temperature=0.4)

            raw_candidates = generated.candidates[:2]
            if not raw_candidates:
                continue

            if batch_evaluate and len(raw_candidates) > 1:
                eval_prompt = "\n\n".join(
                    f"[{i}] {state}" for i, state in enumerate(raw_candidates)
                )
                batch_result = llm.with_structured_output(
                    ThoughtEvaluations,
                    method="function_calling",
                ).invoke([
                    ("system", (
                        "Evaluate multiple construction planning strategies independently. "
                        "For each candidate, assign a score (0.0-1.0) and brief rationale."
                    )),
                    ("human", f"""Problem: {problem}

Candidates to evaluate:
{eval_prompt}

Return one evaluation per candidate, indexed 0..N-1."""),
                ], temperature=0.1)

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
                    judged = llm.with_structured_output(
                        ThoughtEvaluationItem,
                        method="function_calling",
                    ).invoke([
                        ("system", "Independently evaluate a construction planning strategy."),
                        ("human", f"""Problem: {problem}
Candidate strategy: {state}
Score feasibility, actionable detail, and budget adherence (0.0 to 1.0)."""),
                    ], temperature=0.1)
                    candidates.append(
                        Thought(state=state, score=judged.score, rationale=judged.rationale)
                    )
                    if judged.score >= 1.0:
                        return [candidates[-1]]

        frontier = sorted(candidates, key=lambda item: item.score, reverse=True)[:beam_width]
        if not frontier:
            break

    return frontier