"""
memory/demo.py
Person 1 — demo transcript proving every concern actually fires.

Grounded in the REAL seed data in db/seed.sql, not invented names, so
this reads as the genuine IronBridge scenario rather than a synthetic
stand-in:
  - Supplier: "Ironbridge Steel Yard" (Suppliers.CompanyName, SupplierID 2,
    MaterialCategory='Steel').
  - Material: "Reinforcement Steel 12mm" (MaterialID 2) — seed.sql's own
    comment flags it as "already below MinimumStockLevel (edge case)",
    which is exactly the recurring-low-stock scenario the router's
    persist-signal rules are tuned to catch.
  - Project Manager: "Rania Adel" (EmployeeID 2), scoped to Project 1
    ("Riverside Tower").

Steps:
  1. Short-term buffer overflow -> scratchpad survives untouched.
  2. Promote-or-drop routing, with logged reasoning, forget AND episodic
     both firing.
  3. Consolidation as a separate periodic pass (called explicitly here,
     NOT from inside the router) that:
       - writes a brand new fact (v1)
       - writes a second version that RESOLVES A REAL CONFLICT (supplier
         lead time before vs. after a supplier change) and preserves the
         old value
       - expires a stale fact
  4. Self-RAG-style check on memory recall, catching an irrelevant item.

Run: python -m memory.demo   (from the repo root, alongside the
existing agent/, mcp_server/, db/, rag/, context_eval/ — this demo only
touches memory/, it doesn't import from rag/ or context_eval/).
"""

from __future__ import annotations

import os
import time

from memory.short_term import SessionMemory
from memory.stores import EpisodicStore, SemanticStore, DB_PATH
from memory.router import PromoteOrDropRouter
from memory.consolidation import ConsolidationJob
from memory.self_rag_check import SelfRAGChecker


def reset_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)


def main():
    reset_db()
    session_id = "demo-session-1"

    print("\n=== 1. Short-term buffer + scratchpad ===")
    mem = SessionMemory(session_id, max_turns=2)
    mem.scratchpad.update(
        plan="process a Reinforcement Steel 12mm reservation for Project 1 (Riverside Tower)",
        sub_goal="check stock against MinimumStockLevel before reserving",
        project_id="1",
        budget_checked=True,
    )
    print(mem.scratchpad.as_context_block())

    turns_text = [
        ("user", "Hi there"),
        ("tool", "check_material_inventory result: " + ("MaterialID=2, MaterialName=Reinforcement Steel 12mm, QuantityAvailable=18 " * 40)),
        ("user", "Supplier: Ironbridge Steel Yard has lead time of 14 days for this order."),
        ("assistant", "Noted, checking budget next."),
        ("user", "Thanks"),
        ("user", "Project Manager Rania Adel always says escalate early on Project 1."),
    ]
    evicted_all = []
    for role, text in turns_text:
        evicted = mem.add_turn(role, text)
        evicted_all.extend(evicted)

    print(f"buffer size now: {len(mem.buffer)} (max {mem.buffer.max_turns})")
    print("scratchpad UNCHANGED by eviction:")
    print(mem.scratchpad.as_context_block())
    print(f"{len(evicted_all)} turn(s) evicted, handing to router...")

    print("\n=== 2. Promote-or-drop routing ===")
    episodic = EpisodicStore()
    router = PromoteOrDropRouter(episodic)
    decisions = router.route(evicted_all)
    for d in decisions:
        print(f"  {d.decision.upper():9s} | {d.reason}")

    # A second episode implying a CHANGED lead time for the same
    # supplier, simulating a later session after Ironbridge Steel Yard
    # changed terms.
    episodic.add(
        session_id=session_id,
        content="Supplier: Ironbridge Steel Yard has lead time of 21 days for this order after their fleet change.",
        source_role="user",
        reason="matched persist-signal pattern: supplier lead time change",
        project_id="1",
    )

    print("\n=== 3. Consolidation (separate periodic pass) ===")
    semantic = SemanticStore()
    job = ConsolidationJob(episodic, semantic, expiration_ttl_seconds=1)  # short TTL for demo
    result = job.run()
    print(f"episodes processed: {result.episodes_processed}")
    print(f"facts written: {len(result.facts_written)}")
    for f in result.facts_written:
        print(f"  {f.subject}.{f.predicate} = {f.object} (v{f.version}, status={f.status})")
    print(f"conflicts resolved: {len(result.conflicts_resolved)}")
    for c in result.conflicts_resolved:
        print("  CONFLICT:", c["note"])

    print("\nfull version history for Ironbridge Steel Yard lead time (old value preserved, not deleted):")
    for f in semantic.history("Ironbridge Steel Yard", "typical_lead_time_days"):
        print(f"  v{f.version} = {f.object} | status={f.status} | conflict_note={f.conflict_note}")

    time.sleep(1.2)
    expired_run = job.run()  # second pass sweeps expiration (nothing new to consolidate)
    print(f"\nsecond pass (TTL=1s) expired {len(expired_run.facts_expired)} fact(s):")
    for f in expired_run.facts_expired:
        print(f"  {f.subject}.{f.predicate} v{f.version} -> status={f.status}")

    print("\n=== 4. Self-RAG-style check on memory recall ===")
    checker = SelfRAGChecker()
    recalled = [e.content for e in episodic.recall(session_id=session_id)]
    recalled.append("Unrelated small talk about the weather today.")  # plant an irrelevant one
    query = "What is Ironbridge Steel Yard's current lead time for Project 1?"
    kept = checker.filter_relevant(query, recalled)
    print(f"\n{len(kept)}/{len(recalled)} recalled items passed the relevance check and would reach the agent's context.")


if __name__ == "__main__":
    main()
