import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from transcript import Turn, approx_tokens

def apply(turns: list[Turn], keep_last_n_tool_outputs: int = 3) -> list[Turn]:
    """
    Strategy 2: Observation/Tool-Output Masking.
    Truncates/omits large tool outputs while leaving non-tool turns intact.
    Preserves the last `keep_last_n_tool_outputs` tool responses verbatim.
    """
    # Single-pass to count total tool outputs (avoids extra list allocation)
    total_tool_count = sum(1 for t in turns if t.is_tool_output)
    
    masked: list[Turn] = []
    seen_tool_count = 0

    for t in turns:
        if t.is_tool_output:
            seen_tool_count += 1
            # If this tool output is older than the last N tool outputs, mask it
            if seen_tool_count <= (total_tool_count - keep_last_n_tool_outputs):
                masked.append(
                    Turn(
                        role=t.role,
                        content=f"[tool output omitted, ~{approx_tokens(t.content)} tokens]",
                        is_tool_output=True,
                        critical=t.critical,
                        turn_index=t.turn_index,
                    )
                )
                continue

        # Keep non-tool turns and the last N tool outputs intact
        masked.append(t)

    return masked
