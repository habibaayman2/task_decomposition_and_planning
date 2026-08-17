"""planning/algorithms/self_refine.py

Self-Refine: one draft, one critique against an explicit rubric, one revision.

Grounded mode uses:
  1. Deterministic checks (regex / structural) — source of truth: the text itself.
  2. IronBridgeEnvironment.evaluate() — source of truth: mcp_server/db.py
     (RemainingBudget, Supplier.ContractStatus, Material stock levels).

Ungrounded mode uses only the LLM's own rubric review — source of truth:
the model's internal opinion, with no external validation.

Episodic memory integration: self_refine() is single-shot by design (one
draft, one critique, one revision) and has no memory of its own across
calls, unlike reflexion()'s in-loop `memory: list[str]`. Person 1's
memory/stores.py::EpisodicStore is what closes that gap here — a real
grounded problem this call catches (a budget overrun, an inactive
supplier, an under-stocked material) gets written as an episode, and the
next self_refine() call on the same project/session recalls it before
drafting, so the same mistake doesn't have to be rediscovered from a cold
start every single call. If memory/ isn't importable (e.g. self_refine.py
used standalone outside this repo) this degrades to the original
stateless behavior rather than failing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel

from ..models import EnvironmentFeedback
from .environment import IronBridgeEnvironment

try:
    from memory.stores import EpisodicStore
except ImportError:  # pragma: no cover - keeps self_refine usable standalone
    EpisodicStore = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Explicit rubric for construction-delay mitigation proposals
# ---------------------------------------------------------------------------

SELF_REFINE_RUBRIC = """
RUBRIC (score each item 0-1):
1. PROJECT_IDENTITY: Does the proposal name a specific ProjectID and cite real
   project data (budget, status) from the database?
2. BUDGET_GROUNDING: Does it explicitly compare any proposed cost against the
   project's RemainingBudget?
3. SUPPLIER_GROUNDING: Does it name a supplier and verify that supplier's
   ContractStatus is Active in the database?
4. STOCK_GROUNDING: If materials are mentioned, does it check QuantityAvailable
   against MinimumStockLevel?
5. ACTIONABILITY: Are the steps concrete enough for a site manager to execute
   today?
6. CONSISTENCY: Are there internal contradictions (e.g., propose rush order
   then say "no extra cost")?
"""

_PROJECT_ID_RE = re.compile(r"[Pp]roject(?:\s*ID)?\s*(\d+)")


def _approx_tokens(*texts: str) -> int:
    """Rough token estimate: 1 token ~= 4 characters."""
    return sum(len(t) for t in texts) // 4


def _extract_project_id(text: str) -> Optional[str]:
    m = _PROJECT_ID_RE.search(text)
    return m.group(1) if m else None


@dataclass
class SelfRefineResult:
    draft: str
    critique: str
    revised: str
    grounded_issues: list[str]
    environment_feedback: Optional[EnvironmentFeedback] = None
    llm_calls: int = 0
    approx_tokens: int = 0
    episode_id: Optional[str] = None
    recalled_lessons: list[str] = field(default_factory=list)


def deterministic_checks(goal: str, draft: str) -> list[str]:
    """IronBridge-specific deterministic / grounded checks.

    Source of truth: the draft text itself (regex + heuristics).
    These are cheap, reproducible, and require no LLM call.
    """
    issues: list[str] = []

    # 1. Must mention a ProjectID
    if not re.search(r"[Pp]roject(?:\s*ID)?\s*\d+", draft):
        issues.append("FAIL: No ProjectID found — cannot verify budget or scope.")

    # 2. Must mention a concrete dollar amount if proposing a purchase/rush
    if re.search(r"(?:rush|expedite|order|purchase|buy)", draft, re.I):
        if not re.search(r"\$\s?[\d,]+(?:\.\d+)?", draft):
            issues.append("FAIL: Proposes a financial action but states no dollar amount.")

    # 3. Must have structure
    if not re.search(r"(^|\n)(#{1,3}\s+|\d+[.)]\s+|[-*]\s+)", draft):
        issues.append("FAIL: Deliverable has no visible structure (headings or list items).")

    # 4. Length check
    if len(draft.split()) < 50:
        issues.append("FAIL: Deliverable is under 50 words and probably incomplete.")

    # 5. Goal-term coverage
    goal_terms = {
        word.lower()
        for word in re.findall(r"[A-Za-z]{5,}", goal)
        if word.lower() not in {"create", "design", "write", "build", "about", "using", "please", "recommend", "propose"}
    }
    represented = [term for term in goal_terms if term in draft.lower()]
    if goal_terms and not represented:
        issues.append("FAIL: Output contains none of the goal's significant terms.")

    return issues


# ---------------------------------------------------------------------------
# Episodic memory: recall past lessons before drafting, remember new ones
# after critique. Only memory/stores.py::EpisodicStore is touched here --
# never SemanticStore, matching the "router never writes semantic directly"
# invariant memory/stores.py documents (self_refine isn't the router, but
# the same tier discipline applies: this is episodic-only, consolidation.py
# is the sole path to semantic memory).
# ---------------------------------------------------------------------------

def _recall_lessons(episodic_store, session_id: str, project_id: Optional[str]) -> list[str]:
    """Pull prior self_refine lessons for this session, preferring ones
    tagged with the same project_id if we can extract one from the goal."""
    episodes = episodic_store.recall(session_id=session_id, limit=20)
    self_refine_episodes = [e for e in episodes if e.source_role == "self_refine"]
    if project_id is not None:
        same_project = [e for e in self_refine_episodes if e.project_id == project_id]
        if same_project:
            self_refine_episodes = same_project
    return [e.content for e in self_refine_episodes[:5]]


def _remember_lesson(
    episodic_store,
    session_id: str,
    project_id: Optional[str],
    goal: str,
    grounded_issues: list[str],
    env_feedback: Optional[EnvironmentFeedback],
    critique: str,
) -> Optional[str]:
    """Persist what this call's grounded checks caught, so a later
    self_refine() call (this session, or a future one against the same
    EpisodicStore) can recall it instead of rediscovering it cold. Returns
    None (writes nothing) when the draft was clean -- an episode is only
    worth keeping if there was a real problem to remember."""
    del critique  # not stored verbatim; grounded_issues/env details are the durable signal
    problems = list(grounded_issues)
    if env_feedback:
        problems.extend(env_feedback.details)
    if not problems:
        return None
    content = (
        f"Self-Refine on goal '{goal[:120]}': caught {len(problems)} issue(s) -- "
        + "; ".join(problems[:3])
    )
    episode = episodic_store.add(
        session_id=session_id,
        content=content,
        source_role="self_refine",
        reason="grounded/deterministic checks caught a real problem worth remembering across calls",
        project_id=project_id,
    )
    return episode.episode_id


def _ungrounded_critique(goal: str, draft: str, llm: BaseChatModel) -> str:
    """Pure LLM self-critique with no external validation.

    Source of truth: the LLM's own judgment against the rubric.
    """
    response = llm.invoke([
        ("system", "You are an independent critic. Judge the draft against the rubric only."),
        ("human", f"""Goal: {goal}
Rubric:
{SELF_REFINE_RUBRIC}

Draft:
{draft}

List concrete issues. If there are none, respond exactly PASS."""),
    ], temperature=0.2)
    critique = response.content
    if not isinstance(critique, str) or not critique.strip():
        raise RuntimeError("The chat model returned an empty response")
    return critique.strip()


def _grounded_critique(
    goal: str,
    draft: str,
    llm: BaseChatModel,
    environment: IronBridgeEnvironment,
) -> tuple[str, EnvironmentFeedback]:
    """Critique backed by real environment evaluation + LLM rubric review.

    Sources of truth:
      1. deterministic_checks() — structural validation of the text.
      2. environment.evaluate() — DB-backed validation (budget, suppliers, stock).
      3. LLM — synthesizes the above into a coherent critique.
    """
    # 1. Run deterministic checks
    grounded = deterministic_checks(goal, draft)
    grounded_report = "\n".join(f"- {issue}" for issue in grounded) or "- Deterministic checks passed."

    # 2. Run real environment evaluation
    env_feedback = environment.evaluate(draft)

    # 3. Ask LLM to critique with BOTH rubric and real external data
    response = llm.invoke([
        ("system", "You are an independent critic. Judge against the rubric AND the external validation results."),
        ("human", f"""Goal: {goal}
Rubric:
{SELF_REFINE_RUBRIC}

External deterministic checks:
{grounded_report}

External environment evaluation (score={env_feedback.score}):
{chr(10).join('- ' + d for d in env_feedback.details)}

Draft:
{draft}

List concrete issues, citing which external check caught each problem.
If there are no issues, respond exactly PASS."""),
    ], temperature=0.2)
    critique = response.content
    if not isinstance(critique, str) or not critique.strip():
        raise RuntimeError("The chat model returned an empty response")
    return critique.strip(), env_feedback


def _ungrounded_self_evaluate(goal: str, draft: str, llm: BaseChatModel) -> EnvironmentFeedback:
    """Ungrounded evaluation: LLM judges its own output via structured feedback."""
    structured_llm = llm.with_structured_output(EnvironmentFeedback)
    return structured_llm.invoke([
        ("system", "You are an independent evaluator. Judge the draft against the goal with no access to external databases."),
        ("human", f"""Goal: {goal}
Draft:
{draft}

Evaluate this draft. Return:
- success: true/false
- score: 0.0 to 1.0
- details: list of specific issues (empty if none)"""),
    ])


def self_refine(
    goal: str,
    llm: BaseChatModel,
    environment: Optional[IronBridgeEnvironment] = None,
    max_iterations: int = 1,
    episodic_store: Optional["EpisodicStore"] = None,
    session_id: str = "self_refine",
    use_episodic_memory: bool = True,
) -> SelfRefineResult:
    """
    Self-Refine loop: recall -> draft -> critique -> revise -> remember.

    If *environment* is provided, the critique is grounded (deterministic checks
    + real DB validation). If *environment* is None, the critique is ungrounded
    (LLM-only rubric review).

    Episodic memory (memory/stores.py::EpisodicStore) is used two ways:
      - Before drafting, prior self_refine lessons for this session/project
        are recalled and folded into the draft prompt.
      - After critique, if a real grounded problem was caught, it's written
        back to episodic memory for the next call to recall.
    Pass episodic_store=None or use_episodic_memory=False to opt out (e.g.
    tests that shouldn't touch memory/memory_store.db); otherwise a default
    EpisodicStore() is created automatically if the memory/ package is
    importable.
    """
    if episodic_store is None and use_episodic_memory and EpisodicStore is not None:
        episodic_store = EpisodicStore()

    project_id = _extract_project_id(goal)
    recalled_lessons: list[str] = []
    if episodic_store is not None:
        recalled_lessons = _recall_lessons(episodic_store, session_id, project_id)
    lessons_block = (
        "\n\nLessons from past self-corrections (episodic memory -- avoid repeating these):\n"
        + "\n".join(f"- {lesson}" for lesson in recalled_lessons)
    ) if recalled_lessons else ""

    # ---- Draft ----
    draft_response = llm.invoke([
        ("system", "You are IronBridge's planning assistant. Produce a concrete mitigation proposal."),
        ("human", goal + lessons_block),
    ], temperature=0.3)
    draft = draft_response.content
    if not isinstance(draft, str) or not draft.strip():
        raise RuntimeError("The chat model returned an empty draft")
    draft = draft.strip()
    llm_calls = 1

    # ---- Critique ----
    if environment is not None:
        critique, env_feedback = _grounded_critique(goal, draft, llm, environment)
    else:
        critique = _ungrounded_critique(goal, draft, llm)
        env_feedback = None
    llm_calls += 1

    # ---- Revise (only if critique found issues) ----
    # Grounded checks are already folded into the critique prompt (see
    # _grounded_critique's "External deterministic checks" / "External
    # environment evaluation" sections), so the critic's own PASS/FAIL
    # verdict already reflects them -- no need to re-check deterministic_checks
    # here too.
    needs_revision = critique.strip().upper() != "PASS"

    if not needs_revision:
        revised = draft
    else:
        grounded_report = "\n".join(
            f"- {issue}" for issue in deterministic_checks(goal, draft)
        ) or "- Deterministic checks passed."
        env_section = ""
        if env_feedback:
            env_section = (
                f"\nExternal environment evaluation (score={env_feedback.score}):\n"
                + "\n".join(f"- {d}" for d in env_feedback.details)
            )
        response = llm.invoke([
            ("system", "Revise the deliverable using the critique and external checks."),
            ("human", f"""Goal: {goal}

Draft:
{draft}

Grounded checks:
{grounded_report}{env_section}

Critique:
{critique}

Return only the improved deliverable. Be specific, cite ProjectIDs, dollar amounts, and concrete actions."""),
        ], temperature=0.2)
        revised = response.content
        if not isinstance(revised, str) or not revised.strip():
            raise RuntimeError("The chat model returned an empty revision")
        revised = revised.strip()
        llm_calls += 1

    grounded_issues = deterministic_checks(goal, draft)
    episode_id = None
    if episodic_store is not None:
        episode_id = _remember_lesson(
            episodic_store, session_id, project_id, goal, grounded_issues, env_feedback, critique,
        )

    return SelfRefineResult(
        draft=draft,
        critique=critique,
        revised=revised,
        grounded_issues=grounded_issues,
        environment_feedback=env_feedback,
        llm_calls=llm_calls,
        approx_tokens=_approx_tokens(goal, draft, critique, revised),
        episode_id=episode_id,
        recalled_lessons=recalled_lessons,
    )


# Keep the old name for backward compatibility within the toolkit
reflect_and_refine = self_refine