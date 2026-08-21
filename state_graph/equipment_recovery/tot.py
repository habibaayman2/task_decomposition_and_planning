"""
Tree of Thoughts for equipment_recovery's evaluate_options node.

Generates the three candidate branches (repair / rent / reroute),
scores each on cost + downtime + diagnosis confidence, and returns the
best-scoring one that hasn't already been rejected this run. This is
a real ToT, not a single LLM call dressed up: each branch is generated
and scored independently before one is picked, and a prior rejection
prunes the tree on the next pass instead of re-proposing the same
option.
"""

from typing import Any, Dict, List, Optional

from mcp_server.db import equipment_status

# Rough per-branch cost model. Real dollar figures would come from a
# rental-rate table / supplier quote tool in a fuller build; kept
# simple here since the point of this node is the ToT comparison
# logic, not procurement pricing.
_REPAIR_BASE_COST = 800.0
_RENTAL_DAILY_RATE = 450.0
_ASSUMED_RENTAL_DAYS = 14
_REROUTE_COST = 0.0  # reassigning existing fleet equipment has no direct spend


def _generate_branches(equipment_id: int, site: str, diagnosis_confidence: float) -> List[Dict[str, Any]]:
    """One branch per candidate action, each scored independently."""
    branches = []

    # Branch 1: repair
    # Cheap in isolation, but risky if the diagnosis isn't confident --
    # a low-confidence diagnosis means the "repair" might not actually
    # fix the reported symptom.
    repair_downtime_days = 3
    branches.append({
        "action": "repair",
        "estimated_cost": _REPAIR_BASE_COST,
        "downtime_days": repair_downtime_days,
        "risk": 1.0 - diagnosis_confidence,  # low confidence -> high risk
    })

    # Branch 2: rent
    # Always viable in principle (a rental market exists outside the
    # fleet), fastest to get the site moving again, but costs the most
    # over the assumed rental window.
    branches.append({
        "action": "rent",
        "estimated_cost": _RENTAL_DAILY_RATE * _ASSUMED_RENTAL_DAYS,
        "downtime_days": 1,
        "risk": 0.1,
    })

    # Branch 3: reroute -- only a real branch if the fleet actually has
    # a spare unit of the same type sitting Available somewhere else.
    current = equipment_status(None, None)
    equipment_type = next((e["EquipmentType"] for e in current if e["EquipmentID"] == equipment_id), None)
    same_type_available = [
        e for e in current
        if e["EquipmentID"] != equipment_id
        and e["Availability"] == "Available"
        and e["EquipmentType"] == equipment_type
    ]
    if same_type_available:
        branches.append({
            "action": "reroute",
            "estimated_cost": _REROUTE_COST,
            "downtime_days": 1,
            "risk": 0.2,  # relocation logistics risk
        })

    return branches


def _score(branch: Dict[str, Any]) -> float:
    """Lower is better. Normalizes cost/downtime/risk onto comparable
    scales so no single factor dominates just because of its raw
    units (dollars vs days vs a 0-1 risk score)."""
    cost_score = branch["estimated_cost"] / 1000.0     # ~0-7 range here
    downtime_score = branch["downtime_days"] * 1.0      # ~0-3 range
    risk_score = branch["risk"] * 5.0                   # ~0-5 range
    return cost_score + downtime_score + risk_score


def evaluate_recovery_options(
    equipment_id: int,
    diagnosis: str,
    site: str,
    previously_rejected: Optional[List[str]] = None,
    rejection_reason: Optional[str] = None,
    diagnosis_confidence: float = 0.7,
) -> Dict[str, Any]:
    previously_rejected = previously_rejected or []

    branches = _generate_branches(equipment_id, site, diagnosis_confidence)
    viable = [b for b in branches if b["action"] not in previously_rejected]

    if not viable:
        # Every option has been rejected -- this graph can't resolve
        # itself further without a person deciding something outside
        # the three whitelisted actions. Not a HITLPause (that's a
        # request for a decision within known options): this is a
        # genuine dead end the model can't act on, so it becomes a
        # ticket per tickets.py's contract.
        raise RuntimeError(
            f"All recovery options ({[b['action'] for b in branches]}) have "
            f"been rejected for equipment {equipment_id}; no viable option remains."
        )

    scored = [(b, _score(b)) for b in viable]
    scored.sort(key=lambda pair: pair[1])
    best, best_score = scored[0]

    other_options = ", ".join(f"{b['action']}({s:.2f})" for b, s in scored[1:])
    rationale = (
        f"Chose '{best['action']}' (score={best_score:.2f}) over "
        f"[{other_options}]. Diagnosis: {diagnosis}."
    )
    if rejection_reason:
        rationale += f" Previous proposal was rejected: {rejection_reason}."

    return {
        "action": best["action"],
        "estimated_cost": best["estimated_cost"],
        "rationale": rationale,
    }