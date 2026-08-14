"""
memory/short_term.py
Person 1 — Short-term memory + scratchpad (5 pts)

Two SEPARATE structures, on purpose:

  ShortTermBuffer  - a rolling window of raw conversation turns (what was
                      said / what a tool returned). This is what gets
                      pruned when it grows too large.

  Scratchpad        - the agent's *current* plan, sub-goal, and working
                      state (e.g. "site engineer is mid-way through a
                      multi-material request for Project 4, budget check
                      already done, waiting on inventory check"). This is
                      never touched by transcript pruning. If the buffer
                      pruning logic could ever wipe the scratchpad, the
                      agent would forget what it's *currently doing*
                      mid-task, which is a different failure than
                      forgetting what was *said*.

Wiring: agent/agent.py's conversation loop should hold one
ShortTermBuffer + one Scratchpad per active session, call
buffer.add_turn(...) after every user/assistant/tool turn, and call
buffer.overflow_items() to hand aging items to memory/router.py.
See memory/INTEGRATION.md for the exact hook points.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Iterable, Optional


@dataclass
class Turn:
    """One item in the rolling buffer: a user message, assistant message,
    or a tool call/result."""
    turn_id: str
    role: str  # "user" | "assistant" | "tool"
    content: str
    session_id: str
    project_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


class ShortTermBuffer:
    """Rolling message buffer with a fixed max size. When it overflows,
    the oldest turn(s) are evicted and returned to the caller so the
    promote-or-drop router (memory/router.py) can decide their fate.
    The buffer NEVER decides forget-vs-episodic itself — that's the
    router's job, kept separate so the decision logic is auditable in
    one place (rubric: "no direct writes to semantic memory" applies to
    the router; this class doesn't write to long-term memory at all).
    """

    def __init__(self, max_turns: int = 20):
        self.max_turns = max_turns
        self._buffer: Deque[Turn] = deque()

    def add_turn(
        self,
        role: str,
        content: str,
        session_id: str,
        project_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> list[Turn]:
        """Add a turn. Returns any turns evicted by this add (empty list
        if the buffer wasn't full)."""
        turn = Turn(
            turn_id=str(uuid.uuid4()),
            role=role,
            content=content,
            session_id=session_id,
            project_id=project_id,
            metadata=metadata or {},
        )
        self._buffer.append(turn)
        return self._evict_overflow()

    def _evict_overflow(self) -> list[Turn]:
        evicted = []
        while len(self._buffer) > self.max_turns:
            evicted.append(self._buffer.popleft())
        return evicted

    def snapshot(self) -> list[Turn]:
        """Read-only view of what's currently in the buffer (for prompt
        construction). Does not evict anything."""
        return list(self._buffer)

    def __len__(self) -> int:
        return len(self._buffer)


@dataclass
class Scratchpad:
    """The agent's current working state for one session. Distinct
    struct from the transcript on purpose: this must survive buffer
    pruning intact.

    Example (IronBridge triage-style call):
        plan = "process multi-material request for Project 4"
        sub_goal = "run budget check before inventory check"
        working_state = {
            "project_id": "4",
            "materials_requested": ["Reinforcement Steel", "Cement Type II"],
            "budget_checked": True,
            "inventory_checked": False,
            "escalation_needed": None,
        }
    """
    session_id: str
    plan: str = ""
    sub_goal: str = ""
    working_state: dict = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def update(self, *, plan: str = None, sub_goal: str = None, **working_state_kv):
        if plan is not None:
            self.plan = plan
        if sub_goal is not None:
            self.sub_goal = sub_goal
        if working_state_kv:
            self.working_state.update(working_state_kv)
        self.updated_at = time.time()

    def as_context_block(self) -> str:
        """Rendered form injected into the prompt alongside the (pruned)
        buffer, so the agent always knows what it's mid-way through even
        if the raw transcript that led here has been trimmed or
        summarized away by context_eval/'s strategies."""
        lines = [f"[SCRATCHPAD] plan={self.plan!r} sub_goal={self.sub_goal!r}"]
        for k, v in self.working_state.items():
            lines.append(f"  - {k}: {v}")
        return "\n".join(lines)


class SessionMemory:
    """Convenience wrapper pairing one buffer + one scratchpad per
    session_id, and demonstrating the "pruning never destroys the
    scratchpad" guarantee: evicting from the buffer literally cannot
    reach the scratchpad object, they're unrelated attributes.
    """

    def __init__(self, session_id: str, max_turns: int = 20):
        self.session_id = session_id
        self.buffer = ShortTermBuffer(max_turns=max_turns)
        self.scratchpad = Scratchpad(session_id=session_id)

    def add_turn(self, role: str, content: str, **kw) -> list[Turn]:
        return self.buffer.add_turn(role, content, self.session_id, **kw)
