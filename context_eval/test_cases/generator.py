import json
import random
import sys
from pathlib import Path
from typing import Any, List

_TEST_CASES_DIR = Path(__file__).resolve().parent      # context_eval/test_cases
_CONTEXT_EVAL_DIR = _TEST_CASES_DIR.parent               # context_eval/
_ROOT_DIR = _CONTEXT_EVAL_DIR.parent                      # project root
for _p in (_CONTEXT_EVAL_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from transcript import Turn
from mcp_server import db
random.seed(42)

CRITICAL_DETAIL = (
    "Note for the record: Central Cement Co.'s current contract does NOT have a "
    "price-lock clause, so any price increase they announce applies immediately "
    "to all outstanding and future purchase requests -- there's no grace period."
)

FINAL_QUESTION = (
    "Before I approve this new cement purchase request, remind me: does our "
    "contract with Central Cement Co. protect us from a mid-contract price increase?"
)

EXPECTED_ANSWER_KEYWORDS = [
    "no price-lock",
    "no price lock",
    "does not have a price-lock",
    "does not have a price lock",
    "immediately",
    "no grace period",
]


# ---------------------------------------------------------------------------
# Pre-cache DB payloads to eliminate redundant DB reads during generator runs
# ---------------------------------------------------------------------------
def _load_db_payload_cache() -> List[str]:
    """Fetches real database records once and serializes them into cached JSON strings."""
    raw_payloads: List[Any] = [
        db.find_materials(None, None),
        [db.get_project(1), db.get_project(2)],
        db.equipment_status(None, None),
        db.list_safety_policies(),
    ]
    return [json.dumps(p, default=str) for p in raw_payloads]


# Cached JSON strings representing real tool noise outputs
_CACHED_TOOL_PAYLOADS = _load_db_payload_cache()


def _get_tool_output_turn(index: int) -> Turn:
    """Returns a Turn containing real tool JSON noise from the cached database payloads."""
    payload_text = _CACHED_TOOL_PAYLOADS[index % len(_CACHED_TOOL_PAYLOADS)]
    return Turn(
        role="tool",
        content=payload_text,
        is_tool_output=True,
        critical=False,
        turn_index=index,
    )


def generate_transcript(n_tool_turns: int = 32) -> list[Turn]:
    """
    Generates a single synthetic long-context transcript:
      - Turn 0-1: Initial prompt setup
      - Turn 2: Critical detail (critical=True)
      - Turns 3..N: Real tool-output noise
      - Final Turn: Evaluation query
    """
    turns: list[Turn] = []

    # Turn 0: User setup
    turns.append(
        Turn(
            role="user",
            content="Can you help me review procurement status for Riverside Tower?",
            turn_index=0,
        )
    )
    # Turn 1: Assistant acknowledgement
    turns.append(
        Turn(
            role="assistant",
            content="Sure -- let me pull the current status.",
            turn_index=1,
        )
    )
    # Turn 2: Ground-truth critical detail
    turns.append(
        Turn(
            role="user",
            content=CRITICAL_DETAIL,
            is_tool_output=False,
            critical=True,
            turn_index=2,
        )
    )

    idx = 3
    for i in range(n_tool_turns):
        # Add real tool output noise turn
        turns.append(_get_tool_output_turn(idx))
        idx += 1

        # Add assistant intermediate reasoning turn
        turns.append(
            Turn(
                role="assistant",
                content=f"Noted -- checked that, moving on to the next item ({i + 1}/{n_tool_turns}).",
                is_tool_output=False,
                critical=False,
                turn_index=idx,
            )
        )
        idx += 1

    # Final Turn: Target evaluation question
    turns.append(Turn(role="user", content=FINAL_QUESTION, turn_index=idx))

    return turns


def generate_test_suite(
    n_variations: int = 10, n_tool_turns: int = 32
) -> list[list[Turn]]:
    """
    Generates variations of long-context transcripts per the lab spec[cite: 1].
    Varies transcript lengths (32 to 40 tool turns) to prevent strategy tuning.
    """
    suite: list[list[Turn]] = []
    for v in range(n_variations):
        # Vary tool noise turns between 32 and 40
        varied_tool_turns = n_tool_turns + (v % 5) * 2
        suite.append(generate_transcript(n_tool_turns=varied_tool_turns))
    return suite


if __name__ == "__main__":
    from context_eval.transcript import transcript_tokens

    t = generate_transcript()
    crit_indices = [i for i, x in enumerate(t) if x.critical]
    print(f"Generated transcript: {len(t)} turns, ~{transcript_tokens(t)} tokens")
    print(f"Critical turn located at index: {crit_indices}")
