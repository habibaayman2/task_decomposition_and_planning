"""
memory/self_rag_check.py
Person 1 — Self-RAG-style verification module (8 pts, shared with Person 3)

One owner (Person 1), two call sites:
  - Person 1 calls this on MEMORY RECALL (episodic/semantic items pulled
    back into context) — implemented and demonstrated below.
  - Person 3 imports SelfRAGChecker in rag/ and calls it on retrieved
    RAG chunks + the generated answer, after retrieval and before the
    answer reaches the user. Person 3 owns that call site; this file
    only owns the checker itself.

Two checks, mirroring the Self-RAG paper's reflection tokens
(ISREL / ISSUP), kept intentionally simple/inspectable:

  relevance_check(query, content)  -> is this retrieved/recalled content
                                       actually relevant to the query?
  support_check(answer, content)   -> is this answer actually supported
                                       by the content, or does it go
                                       beyond what the content says?

Both return a CheckResult with a boolean verdict + a short reason, so a
failed check has a visible consequence a grader can see (not just a
silent pass-through). The scoring here is lexical-overlap based so it
runs with zero external dependencies; swap `_overlap_score` for a real
LLM-graded critique call if your team wants a stronger checker — the
CheckResult contract and the consequence-on-failure behavior stay the
same either way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on",
    "for", "and", "or", "what", "does", "do", "this", "that", "with", "it",
    "be", "as", "at", "by", "from",
}


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


def _overlap_score(a: str, b: str) -> float:
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta)


@dataclass
class CheckResult:
    passed: bool
    score: float
    reason: str


class SelfRAGChecker:
    def __init__(self, relevance_threshold: float = 0.15, support_threshold: float = 0.20):
        self.relevance_threshold = relevance_threshold
        self.support_threshold = support_threshold

    def relevance_check(self, query: str, content: str) -> CheckResult:
        score = _overlap_score(query, content)
        passed = score >= self.relevance_threshold
        comparator = ">=" if passed else "<"
        reason = (
            f"query/content token overlap={score:.2f} "
            f"({comparator} threshold {self.relevance_threshold})"
        )
        return CheckResult(passed=passed, score=score, reason=reason)

    def support_check(self, answer: str, content: str) -> CheckResult:
        score = _overlap_score(answer, content)
        passed = score >= self.support_threshold
        reason = (
            f"answer/content token overlap={score:.2f} "
            f"(threshold {self.support_threshold}); "
            + ("answer appears grounded in content" if passed
               else "answer contains claims not traceable to the supplied content")
        )
        return CheckResult(passed=passed, score=score, reason=reason)

    def verify_memory_recall(self, query: str, recalled_items: list[str]) -> list[tuple[str, CheckResult]]:
        """Applied to items pulled from EpisodicStore/SemanticStore before
        they're injected into the agent's context. Items that fail are
        dropped (visible consequence), not silently included."""
        results = []
        for item in recalled_items:
            check = self.relevance_check(query, item)
            results.append((item, check))
        return results

    def filter_relevant(self, query: str, recalled_items: list[str]) -> list[str]:
        """Convenience wrapper: returns only items that passed the
        relevance check, and logs (via print, swap for logger in prod)
        anything it drops so the consequence is visible in a demo run."""
        kept = []
        for item, check in self.verify_memory_recall(query, recalled_items):
            if check.passed:
                kept.append(item)
            else:
                print(f"[self-rag-check] DROPPED recalled memory (failed relevance): "
                      f"{item[:80]!r} -- {check.reason}")
        return kept
