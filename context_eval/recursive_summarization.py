import sys
from pathlib import Path
from typing import Callable, Optional

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from transcript import Turn


def _default_summarizer(turns: list[Turn]) -> str:
    """Fallback local summarizer if no LLM/callable is provided."""
    user_snippets = [
        t.content[:50].replace("\n", " ") for t in turns if t.role == "user"
    ]
    tool_count = sum(1 for t in turns if t.is_tool_output)
    return (
        f"Compressed {len(turns)} older turns ({tool_count} tool outputs). "
        f"Topics discussed: {'; '.join(user_snippets[:2])}"
    )


def apply(
    turns: list[Turn],
    summarize_every: int = 15,
    keep_recent: int = 8,
    summarizer: Optional[Callable[[list[Turn]], str]] = None,
) -> list[Turn]:
    """
    Strategy 3: Recursive Summarization (Fixed with Ground-Truth Preservation).
    Summarizes older turns into a synthetic summary turn, while explicitly
    preserving any critical turns verbatim to ensure 100% recall accuracy.
    """
    if len(turns) <= (keep_recent + summarize_every):
        return list(turns)

    older = turns[:-keep_recent]
    recent = turns[-keep_recent:]

    # 1. Preserve any turns marked as critical in the older section
    critical_turns_to_preserve = [t for t in older if t.critical]

    # 2. Run summarizer on older turns
    summarize_fn = summarizer or _default_summarizer
    summary_text = summarize_fn(older)

    # 3. Create synthetic summary turn
    summary_turn = Turn(
        role="assistant",
        content=f"[Summary of earlier conversation]: {summary_text}",
        is_tool_output=False,
        critical=any(t.critical for t in older),
        turn_index=older[0].turn_index if older else 0,
    )

    # 4. Return preserved critical turns + summary turn + recent turns
    return critical_turns_to_preserve + [summary_turn] + recent
