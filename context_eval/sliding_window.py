"""
Strategy 1: Sliding Window.
Keeps only the last N turns verbatim, dropping everything prior unconditionally.
"""

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from transcript import Turn

def apply(turns: list[Turn], window_turns: int = 10) -> list[Turn]:
    """
    Applies sliding window pruning.
    
    Args:
        turns: The full transcript as a list of Turn objects.
        window_turns: Number of recent turns to preserve (default: 10).
    """
    if len(turns) <= window_turns:
        return list(turns)
    
    return list(turns[-window_turns:])