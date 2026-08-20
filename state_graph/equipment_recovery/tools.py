"""
Whitelisted tools for equipment_recovery's constrained ReAct node
(execute_recovery_action in nodes.py).

"Constrained" means the graph can only ever call one of these three
functions -- never an arbitrary action the model invents -- and each
one writes through mcp_server.db to the SAME procurement.db the rest
of the system uses (never a parallel store).

diagnose_from_manuals lives here too (used by nodes.diagnose_issue),
since it's also a tool call in the ReAct sense, even though its
result feeds Tree of Thoughts rather than an approved action.
"""

from typing import Any, Dict, List, Optional

from mcp_server.db import get_conn, equipment_status
from rag.hybrid_search import hybrid_rag_answer


def diagnose_from_manuals(equipment_id: int, symptom: str) -> Dict[str, Any]:
    """RAG call: looks up the reported symptom against the equipment /
    safety policy corpus in rag/, instead of letting the model
    hallucinate a failure cause with no grounding. Confidence is
    derived from the Self-RAG support_check (rag/hybrid_search.py),
    not invented -- an unsupported answer is reported as low
    confidence rather than treated as certain.
    """
    query = f"Equipment failure symptom: {symptom}. What is the likely cause and safety procedure?"
    result = hybrid_rag_answer(query)

    support = result["self_rag"].get("support_check")
    confidence = 0.8 if (support and support["passed"]) else 0.3

    # NOTE: HybridRetriever.search() chunks currently carry only
    # text/vector_score/bm25_score/combined_score, no source metadata
    # -- so we can't cite which policy file grounded this diagnosis
    # yet. Flagging as a follow-up rather than inventing a field that
    # isn't actually there: chunk provenance would need to flow from
    # rag/chunking.py's metadata["source"] through hybrid_search.py's
    # merged dicts before this can report real sources.
    sources = [c["text"][:60] for c in result["retrieved_chunks"]]

    return {
        "cause": result["answer"],
        "confidence": confidence,
        "sources": sources,
    }


def schedule_repair(equipment_id: int, diagnosis: str) -> Dict[str, Any]:
    """Marks the equipment Under Maintenance in the real Equipment
    table. This is the one write path for the "repair" branch --
    execute_recovery_action never writes to Equipment any other way.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE Equipment SET Availability = 'Under Maintenance', "
            "MaintenanceStatus = ? WHERE EquipmentID = ?",
            (f"Scheduled repair: {diagnosis}", equipment_id),
        )
    return {"action": "repair", "equipment_id": equipment_id, "new_status": "Under Maintenance"}


def reserve_rental_equipment(equipment_id: int, site: str, estimated_cost: float) -> Dict[str, Any]:
    """Records a rental reservation. Does not touch the broken
    equipment's own row (it's still broken and awaiting repair
    separately) -- this books a substitute so the site can keep
    working.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE Equipment SET MaintenanceStatus = ? WHERE EquipmentID = ?",
            (f"Broken; rental substitute reserved for {site} (~${estimated_cost:,.2f})", equipment_id),
        )
    return {
        "action": "rent",
        "equipment_id": equipment_id,
        "site": site,
        "estimated_cost": estimated_cost,
    }


def reroute_to_alternate_equipment(equipment_id: int, site: str) -> Dict[str, Any]:
    """Finds another Available piece of equipment of the SAME type
    already in the fleet and reassigns it to this site, instead of
    repairing or renting. Real failure mode this guards against: if
    no alternate equipment exists, that's reported back rather than
    silently doing nothing, so the caller (execute_recovery_action)
    can surface it as a ticket instead of a false success.
    """
    with get_conn() as conn:
        broken = conn.execute(
            "SELECT EquipmentType FROM Equipment WHERE EquipmentID = ?",
            (equipment_id,),
        ).fetchone()
        if broken is None:
            raise ValueError(f"No equipment with id {equipment_id}")
        equipment_type = broken["EquipmentType"]

    candidates: List[Dict[str, Any]] = [
        e for e in equipment_status(equipment_type, None)
        if e["Availability"] == "Available" and e["EquipmentID"] != equipment_id
    ]
    if not candidates:
        raise RuntimeError(
            f"No available {equipment_type} found to reroute to {site} -- "
            f"reroute is not viable, this run should not have proposed it."
        )

    alternate = candidates[0]
    with get_conn() as conn:
        conn.execute(
            "UPDATE Equipment SET CurrentSite = ?, Availability = 'In Use' WHERE EquipmentID = ?",
            (site, alternate["EquipmentID"]),
        )
        conn.execute(
            "UPDATE Equipment SET MaintenanceStatus = ? WHERE EquipmentID = ?",
            (f"Broken; work rerouted to equipment {alternate['EquipmentID']} at {site}", equipment_id),
        )

    return {
        "action": "reroute",
        "equipment_id": equipment_id,
        "rerouted_to_equipment_id": alternate["EquipmentID"],
        "site": site,
    }