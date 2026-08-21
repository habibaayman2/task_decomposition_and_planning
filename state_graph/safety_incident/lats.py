"""
LATS (Language Agent Tree Search) for safety_incident's investigate_node
(Problem 3, owned by Person C -- Day-3 / C3 wiring).

Why LATS and not a single LLM guess:
  - Root causes for a safety incident are open-ended (unlike
    equipment_recovery's fixed repair/rent/reroute set, which is why
    that graph uses ToT with static branches instead). Each round an
    LLM proposes several DISTINCT candidate root-cause / corrective-
    action paths -- a real branching step, not one call dressed up as
    "search".
  - Each candidate is scored against something real, not the model's
    own opinion of urgency: the incident's actual DB severity, plus a
    grounded regulatory-exposure check against the real policy corpus
    in rag/policies/ via rag.hybrid_search.hybrid_rag_answer -- the
    SAME Self-RAG support_check equipment_recovery's
    diagnose_from_manuals() relies on for its confidence number.
  - If the best candidate isn't well grounded in that corpus, the
    search expands: the LLM is told which candidate was weak and why,
    and asked to propose different root causes on the next round. A
    previously-explored (or previously officer-rejected, via the
    'needs_more_investigation' HITL cycle) root cause is pruned so a
    re-investigation round doesn't just repeat the same guess.

This mirrors the tree-search shape of planning/algorithms/lats.py
(root/children/visits-style search with real scoring) but is kept
self-contained and dependency-light, matching the precedent set by
equipment_recovery/tot.py: a graph node needs a fast, testable search
step, not the full MCTS+environment machinery built for the planning
lab.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# --------------------------------------------------------------------------
# Path & Module Resolution
# --------------------------------------------------------------------------
_current_dir = Path(__file__).resolve().parent
REPO_ROOT = next(
    (p for p in [_current_dir] + list(_current_dir.parents) if (p / "mcp_server").exists()),
    _current_dir.parent.parent,  # Fallback
)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError

from rag.hybrid_search import hybrid_rag_answer

MAX_ROUNDS = 2
BRANCHING_FACTOR = 3
GROUNDED_CONFIDENCE_THRESHOLD = 0.6
# First-time setup can build a local embedding index (or download a
# model) -- bound it so an unavailable/slow retrieval backend degrades
# this round to "ungrounded" instead of blocking the graph node.
GROUNDING_TIMEOUT_SECONDS = 20
_grounding_executor = ThreadPoolExecutor(max_workers=2)

# Real, DB-sourced severity -> weight. Not invented per-candidate; the
# same weight applies to every candidate for a given incident, exactly
# so severity can't be used to launder an ungrounded guess into a high
# score.
_SEVERITY_WEIGHT = {"low": 0.2, "medium": 0.5, "high": 0.8, "critical": 1.0}


@dataclass
class LATSNode:
    root_cause: str
    corrective_action: str
    rationale: str
    depth: int
    regulatory_grounded: bool = False
    regulatory_confidence: float = 0.0
    score: float = 0.0


@dataclass
class LATSResult:
    root_cause: str
    corrective_action: str
    rationale: str
    regulator_report_recommended: bool
    confidence: float
    iterations: int
    pruned_count: int
    rounds: int


def _build_candidate_prompt(
    description: str, severity: str, prior_notes: str, explored: List[str], feedback: Optional[str]
) -> str:
    explored_txt = "; ".join(explored) if explored else "(none yet)"
    feedback_txt = (
        f"\nA previous best guess was weak: {feedback}\nPropose genuinely different root causes this round."
        if feedback
        else ""
    )
    return f"""You are a safety investigator on a construction site.
Incident: {description}
Reported severity: {severity}
Already-explored root causes this run (do NOT repeat these): {explored_txt}
Prior investigation notes: {prior_notes or '(none yet)'}
{feedback_txt}

Propose {BRANCHING_FACTOR} DISTINCT candidate root causes for this
incident and, for each, one corrective action.

Respond ONLY as a JSON list, no other text, in this exact shape:
[
  {{"root_cause": "...", "corrective_action": "...", "rationale": "..."}},
  {{"root_cause": "...", "corrective_action": "...", "rationale": "..."}}
]"""


def _parse_candidates(raw: str) -> List[Dict[str, str]]:
    try:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        data = json.loads(match.group(0) if match else raw)
        candidates = [
            {
                "root_cause": str(c["root_cause"]).strip(),
                "corrective_action": str(c["corrective_action"]).strip(),
                "rationale": str(c.get("rationale", "")).strip(),
            }
            for c in data
            if "root_cause" in c and "corrective_action" in c
        ]
        if candidates:
            return candidates
    except Exception:
        pass
    return [
        {
            "root_cause": "Undetermined -- candidate generation returned unparseable output",
            "corrective_action": "Escalate to manual review",
            "rationale": "Fallback candidate produced because the LLM response could not be parsed as JSON.",
        }
    ]


def _ground_regulatory_exposure(root_cause: str, description: str) -> Dict[str, Any]:
    """Real grounding step: checks this candidate root cause against
    IronBridge's actual safety-policy corpus (rag/policies/*.md) using
    the same Self-RAG support_check equipment_recovery's
    diagnose_from_manuals() uses -- not the model's own say-so about
    whether a regulation applies."""
    query = (
        f"Safety incident root cause: {root_cause}. Incident description: {description}. "
        f"Does a safety regulation or regulator reporting requirement apply?"
    )
    try:
        future = _grounding_executor.submit(hybrid_rag_answer, query)
        result = future.result(timeout=GROUNDING_TIMEOUT_SECONDS)
        support = result.get("self_rag", {}).get("support_check")
        grounded = bool(support and support.get("passed"))
        confidence = 0.85 if grounded else 0.35
    except _FutureTimeoutError:
        # Retrieval backend didn't respond in time (e.g. cold-start
        # index build) -- treat as ungrounded rather than hang the node.
        grounded, confidence = False, 0.0
    except Exception:
        grounded, confidence = False, 0.0
    return {"grounded": grounded, "confidence": confidence}


def _score(grounding: Dict[str, Any], severity: str) -> float:
    severity_weight = _SEVERITY_WEIGHT.get(severity, 0.5)
    return round(0.6 * grounding["confidence"] + 0.4 * severity_weight, 4)


def investigate_incident(
    *,
    description: str,
    severity: str,
    prior_notes: str,
    previously_explored: Optional[List[str]] = None,
    call_llm: Callable[[str], str],
) -> LATSResult:
    """Runs the LATS search: generate candidate paths -> ground each
    against the real policy corpus -> score -> expand if the best
    candidate isn't well grounded -> select. Returns the winning path
    plus search metadata so investigate_node can log a real audit
    trail instead of a single unexplained answer."""
    explored = list(previously_explored or [])
    best: Optional[LATSNode] = None
    pruned = 0
    iterations = 0
    feedback: Optional[str] = None
    rounds = 0

    for round_no in range(1, MAX_ROUNDS + 1):
        rounds = round_no
        prompt = _build_candidate_prompt(description, severity, prior_notes, explored, feedback)
        raw = call_llm(prompt)
        candidates = _parse_candidates(raw)

        round_best: Optional[LATSNode] = None
        for c in candidates:
            if c["root_cause"] in explored:
                pruned += 1
                continue
            grounding = _ground_regulatory_exposure(c["root_cause"], description)
            node = LATSNode(
                root_cause=c["root_cause"],
                corrective_action=c["corrective_action"],
                rationale=c["rationale"],
                depth=round_no,
                regulatory_grounded=grounding["grounded"],
                regulatory_confidence=grounding["confidence"],
            )
            node.score = _score(grounding, severity)
            iterations += 1
            if round_best is None or node.score > round_best.score:
                round_best = node

        if round_best and (best is None or round_best.score > best.score):
            best = round_best

        if best and best.regulatory_grounded and best.regulatory_confidence >= GROUNDED_CONFIDENCE_THRESHOLD:
            break  # good enough -- stop expanding the tree

        if best:
            feedback = (
                f"'{best.root_cause}' was not clearly supported by the safety-policy corpus "
                f"(grounding confidence {best.regulatory_confidence:.2f})."
            )
            explored.append(best.root_cause)

    if best is None:
        best = LATSNode(
            root_cause="Undetermined",
            corrective_action="Escalate to manual review",
            rationale="No candidate could be generated or grounded within the search budget.",
            depth=rounds,
        )

    return LATSResult(
        root_cause=best.root_cause,
        corrective_action=best.corrective_action,
        rationale=best.rationale,
        regulator_report_recommended=best.regulatory_grounded,
        confidence=best.regulatory_confidence,
        iterations=iterations,
        pruned_count=pruned,
        rounds=rounds,
    )
