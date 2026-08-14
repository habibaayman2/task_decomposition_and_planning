"""
Strategy 4: Zone-Based Pruning.

Splits the transcript into three zones instead of treating all history
equally:

  - Anchor zone (oldest `anchor_turns`): kept verbatim. Early critical
    decisions (e.g. a budget cap stated in turn 2) live here and must
    survive.
  - Recent zone (newest `recent_turns`): kept verbatim, since it's what
    the model needs for immediate continuation.
  - Middle zone (everything between): compressed, not dropped --
    non-tool turns are kept, tool outputs are truncated to a short
    marker, similar to observation masking but only applied to the
    middle zone specifically.

Information degrades gradually (anchor -> compressed middle -> recent)
instead of disappearing all at once the way a plain sliding window does.
"""

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from transcript import Turn, approx_tokens


def apply(
    turns: list[Turn],
    anchor_turns: int = 4,
    recent_turns: int = 8,
) -> list[Turn]:
    """
    Applies zone-based pruning.

    Args:
        turns: The full transcript as a list of Turn objects.
        anchor_turns: Number of oldest turns kept verbatim (anchor zone).
        recent_turns: Number of newest turns kept verbatim (recent zone).
    """
    total = len(turns)

    # Transcript too short to have a meaningful middle zone -- keep as is.
    if total <= anchor_turns + recent_turns:
        return list(turns)

    anchor_zone = turns[:anchor_turns]
    middle_zone = turns[anchor_turns: total - recent_turns]
    recent_zone = turns[total - recent_turns:]

    compressed_middle: list[Turn] = []
    for t in middle_zone:
        if t.is_tool_output:
            compressed_middle.append(
                Turn(
                    role=t.role,
                    content=f"[zone-compressed tool output, ~{approx_tokens(t.content)} tokens]",
                    is_tool_output=True,
                    critical=t.critical,
                    turn_index=t.turn_index,
                )
            )
        else:
            compressed_middle.append(t)

    return list(anchor_zone) + compressed_middle + list(recent_zone)