"""
planning/environment.py

Real EnvironmentFeedback grounded in mcp_server/db.py.
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

# Resolve project root for cross-module imports
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ..models import EnvironmentFeedback
from mcp_server import db


_PROJECT_RE = re.compile(r"[Pp]roject(?:\s*ID)?\s*(\d+)")
_MATERIAL_RE = re.compile(r"[Mm]aterial(?:\s*ID)?\s*(\d+)")
_RUSH_COST_RE = re.compile(
    r"(?:rush|premium|expedite).{0,50}?\$\s?([\d,]+(?:\.\d+)?)|\$\s?([\d,]+(?:\.\d+)?).{0,50}?(?:rush|premium|expedite)",
    re.IGNORECASE,
)

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
    """Drop-in for algorithms.environment.Environment."""

    def __init__(self, success_threshold: float = 0.6):
        if not 0.0 <= success_threshold <= 1.0:
            raise ValueError("success_threshold must be between zero and one")
        self.success_threshold = success_threshold

    def evaluate(self, state: str) -> EnvironmentFeedback:
        checks, score = _grounded_checks(state)
        success = score >= self.success_threshold
        details = [c for c in checks if not c.startswith("OK:")] or [c for c in checks]
        return EnvironmentFeedback(success=success, score=round(score, 4), details=details)

    def clear_cache(self) -> None:
        """Clear internal LRU caches between benchmark runs."""
        _get_supplier_status.cache_clear()
        _get_project_cached.cache_clear()
        _get_low_stock_materials.cache_clear()


def _grounded_checks(state: str) -> tuple[list[str], float]:
    checks: list[str] = []
    scores: list[float] = []
    weights: list[float] = []

    project_id = _extract_project_id(state)
    lowered = state.lower()

    # --- Check 1: rush cost vs. real remaining budget (weight: 0.4) ---
    if "rush" in lowered or "premium" in lowered or "expedite" in lowered:
        rush_cost = _extract_rush_cost(state)
        project = _get_project_cached(project_id) if project_id else None
        if project is None:
            checks.append("FAIL: proposal mentions rush order but no valid project_id found.")
            scores.append(0.1)
            weights.append(0.4)
        elif rush_cost is None:
            checks.append("FAIL: proposal mentions rush order but states no cost figure.")
            scores.append(0.2)
            weights.append(0.4)
        elif rush_cost > project["RemainingBudget"]:
            checks.append(
                f"FAIL: proposed rush cost ${rush_cost:,.2f} exceeds Project {project_id}'s "
                f"remaining budget of ${project['RemainingBudget']:,.2f}."
            )
            scores.append(0.0)
            weights.append(0.4)
        else:
            checks.append(
                f"OK: proposed rush cost ${rush_cost:,.2f} fits within Project {project_id}'s "
                f"remaining budget (${project['RemainingBudget']:,.2f})."
            )
            scores.append(0.9)
            weights.append(0.4)

    # --- Check 2: proposed supplier's real contract status (weight: 0.35) ---
    matched_any = False
    for supplier_hint in SUPPLIER_HINTS:
        if supplier_hint in lowered:
            matched_any = True
            suppliers = _get_supplier_status(supplier_hint)
            if suppliers is None:
                checks.append(f"FAIL: supplier matching '{supplier_hint}' not found in DB.")
                scores.append(0.2)
                weights.append(0.35)
            else:
                inactive = [s for s in suppliers if s["status"] != "Active"]
                if inactive:
                    names = ", ".join(s["name"] for s in inactive)
                    checks.append(
                        f"FAIL: proposed supplier(s) ({names}) have ContractStatus != Active."
                    )
                    scores.append(0.0)
                    weights.append(0.35)
                else:
                    names = ", ".join(s["name"] for s in suppliers)
                    checks.append(f"OK: proposed supplier(s) ({names}) are Active.")
                    scores.append(0.9)
                    weights.append(0.35)
            break

    if not matched_any and any(word in lowered for word in ["supplier", "vendor", "source"]):
        checks.append("WARN: proposal mentions suppliers but no specific name found to verify.")
        scores.append(0.5)
        weights.append(0.35)

    # --- Check 3: resequencing without addressing stock shortfall (weight: 0.25) ---
    if "resequence" in lowered and "order" not in lowered and "reserve" not in lowered:
        low_stock = _get_low_stock_materials()
        if low_stock:
            names = ", ".join(m["MaterialName"] for m in low_stock)
            checks.append(f"FAIL: proposal resequences without addressing low stock: {names}.")
            scores.append(0.3)
            weights.append(0.25)
        else:
            checks.append("OK: no material below minimum stock; resequencing is acceptable.")
            scores.append(0.8)
            weights.append(0.25)

    if not checks:
        checks.append("No specific groundable claim found to check.")
        scores.append(0.5)
        weights.append(1.0)

    total_weight = sum(weights)
    weighted_score = sum(s * w for s, w in zip(scores, weights)) / total_weight if total_weight > 0 else 0.5
    return checks, weighted_score
