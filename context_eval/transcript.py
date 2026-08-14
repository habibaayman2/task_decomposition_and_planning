from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Turn:
    role: str  # "user" | "assistant" | "tool"
    content: str
    is_tool_output: bool = False
    critical: bool = False  # Marks the turn holding detail needed for recall evaluation
    turn_index: int = 0
    # Add optional tool metadata if observation masking needs to preserve tool names
    tool_name: Optional[str] = None 


def approx_tokens(text: str) -> int:
    # Handles empty strings safely without returning 1 token unnecessarily
    if not text:
        return 0
    return max(len(text) // 4, 1)


def transcript_tokens(turns: list[Turn]) -> int:
    return sum(approx_tokens(t.content) for t in turns)
