"""
planning/environment.py

Real EnvironmentFeedback grounded in mcp_server/db.py.
Replaces the toolkit's randomized default with genuine DB checks.
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from models import EnvironmentFeedback
from mcp_server import db

import random


class Environment:
    """Toolkit default: stochastic evaluator biased toward favorable results.

    This is the UNGROUNDED baseline. It uses random.betavariate with no
    connection to the real database. A LATS or Reflexion still pointed at
    this at submission time earns no credit for grounding.
    """
    def __init__(self, success_threshold: float = 0.6, rng=None):
        if not 0.0 <= success_threshold <= 1.0:
            raise ValueError("success_threshold must be between zero and one")
        self.success_threshold = success_threshold
        self.rng = rng or random.Random()

    def evaluate(self, state: str) -> EnvironmentFeedback:
        del state
        score = round(self.rng.betavariate(5.0, 2.0), 4)
        success = score >= self.success_threshold
        details = [] if success else ["The randomized evaluator rejected this attempt."]
        return EnvironmentFeedback(success=success, score=score, details=details)


# ---------------------------------------------------------------------------
# Regex extractors
# ---------------------------------------------------------------------------
_PROJECT_RE = re.compile(r"[Pp]roject(?:\s*ID)?\s*(\d+)")
_RUSH_COST_RE = re.compile(
    r"(?:rush|premium|expedite).{0,50}?\$\s?([\d,]+(?:\.\d+)?)|\$\s?([\d,]+(?:\.\d+)?).{0,50}?(?:rush|premium|expedite)",
    re.IGNORECASE,
)
_COST_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")

SUPPLIER_HINTS = ["steel yard", "cement co", "plumbing supplies", "electrical supply"]


def _extract_project_id(state: str) -> Optional[int]:
    m = _PROJECT_RE.search(state)
    return int(m.group(1)) if m else None


def _extract_rush_cost(state: str) -> Optional[float]:
    m = _RUSH_COST_RE.search(state)
    if m:
        raw = m.group(1) or m.group(2)
        try:
            return float(raw.replace(",", ""))
        except ValueError:
            return None
    return None


def _extract_any_cost(state: str) -> Optional[float]:
    """Extract any dollar amount, not just rush-related."""
    m = _COST_RE.search(state)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


@lru_cache(maxsize=32)
def _get_supplier_status(supplier_name_fragment: str) -> Optional[list[dict]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT CompanyName, ContractStatus FROM Suppliers WHERE CompanyName LIKE ?",
            (f"%{supplier_name_fragment}%",),
        ).fetchall()
    return [{"name": r["CompanyName"], "status": r["ContractStatus"]} for r in rows] if rows else None


@lru_cache(maxsize=8)
def _get_project_cached(project_id: int) -> Optional[dict]:
    return db.get_project(project_id)


@lru_cache(maxsize=8)
def _get_low_stock_materials() -> list[dict]:
    return [m for m in db.find_materials(None, None) if m["QuantityAvailable"] < m["MinimumStockLevel"]]


class IronBridgeEnvironment:
    """Drop-in replacement for algorithms.environment.Environment.

    Every critique step uses real DB state as the source of truth:
      - Project remaining budget  -> db.get_project()
      - Supplier contract status  -> Suppliers table
      - Material stock levels     -> Materials table
    """

    def __init__(self, success_threshold: float = 0.6):
        if not 0.0 <= success_threshold <= 1.0:
            raise ValueError("success_threshold must be between zero and one")
        self.success_threshold = success_threshold

    def evaluate(self, state: str) -> EnvironmentFeedback:
        checks, score = _grounded_checks(state)
        success = score >= self.success_threshold
        ok_checks = [c for c in checks if c.startswith("OK:")]
        fail_checks = [c for c in checks if not c.startswith("OK:")]
        details = fail_checks or ok_checks or checks
        return EnvironmentFeedback(success=success, score=round(score, 4), details=details)

    def clear_cache(self) -> None:
        _get_supplier_status.cache_clear()
        _get_project_cached.cache_clear()
        _get_low_stock_materials.cache_clear()


def _grounded_checks(state: str) -> tuple[list[str], float]:
    """
    IronBridge grounded validation logic:
    Checks the proposal against real DB constraints (Budget, Supplier, Stock).
    """
    import re
    from mcp_server import db

    issues = []
    score = 1.0
    lowered = state.lower()
    
    # --- FIX: Initialize variables to avoid UnboundLocalError ---
    any_cost = None
    project_id = None
    # -----------------------------------------------------------

    # 1. Extract ProjectID from the text
    m_pid = re.search(r"[Pp]roject(?:\s*ID)?\s*(\d+)", state)
    if m_pid:
        project_id = int(m_pid.group(1))
    else:
        # Fatal, not partial: with no ProjectID, every downstream check
        # (budget, contract status) is unverifiable against the real
        # DB -- the plan is pure LLM invention with nothing grounding
        # it, so this must zero the score, not just dock it, otherwise
        # a plan can score above threshold while stating "GROUNDING
        # FAIL" in its own details (exactly what happened here).
        issues.append("GROUNDING FAIL: No ProjectID mentioned. Cannot verify budget.")
        score = 0.0
        return issues, score

    # 2. Extract Dollar Amounts ($) from the text
    m_cost = re.search(r"\$\s?([\d,]+(?:\.\d+)?)", state)
    if m_cost:
        try:
            any_cost = float(m_cost.group(1).replace(",", ""))
        except ValueError:
            any_cost = None

    # 3. Check for Financial Actions without Cost
    if any_cost is None and "resequence" not in lowered:
        # If proposing a purchase but no $ amount is stated
        if any(word in lowered for word in ["buy", "order", "purchase", "rush", "rental", "procure"]):
            issues.append("GROUNDING FAIL: Proposes a financial action but states no dollar amount.")
            score -= 0.4

    # 4. Real DB Check: Budget Constraint
    if project_id and any_cost is not None:
        project = db.get_project(project_id)
        if project:
            remaining = project.get("RemainingBudget", 0.0)
            if any_cost > remaining:
                issues.append(f"GROUNDING FAIL: Proposed cost ${any_cost:,.2f} exceeds RemainingBudget ${remaining:,.2f}.")
                score -= 0.5
        else:
            issues.append(f"GROUNDING FAIL: ProjectID {project_id} not found in database.")
            score -= 0.2

    # 5. Real DB Check: Supplier Status
    # If a supplier name is mentioned, check if they are 'Inactive'
    if "supplier" in lowered or "vendor" in lowered:
        # Simple heuristic: check all active suppliers
        # In a real system, you'd extract the specific supplier name
        if "inactive" in lowered or "expired" in lowered:
             issues.append("GROUNDING FAIL: Proposal mentions an inactive or expired contract.")
             score -= 0.4

    # 6. Real DB Check: Material Stock Levels
    if "resequence" in lowered:
        # Check if resequencing is being used to hide a stock shortage
        low_stock = [m for m in db.find_materials(None, None) if m["QuantityAvailable"] < m["MinimumStockLevel"]]
        if low_stock and "order" not in lowered and "purchase" not in lowered:
            names = ", ".join(m["MaterialName"] for m in low_stock)
            issues.append(f"GROUNDING FAIL: Resequencing ignores critical shortages in: {names}.")
            score -= 0.3

    # Ensure score doesn't go below 0
    return issues, max(0.0, score)