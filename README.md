# 🏗️ IronBridge Construction — State Graphs, HITL, and the Platform

> **Final Project — 4-Day Sprint.** Three real-world IronBridge workflows, each built as a
> cyclic, checkpointed state graph on a shared core, each pausing for a human at a genuine
> decision point, each recovering cleanly from a crash. This README replaces the earlier
> Week-4-only Planning Lab README below with the consolidated picture of the whole sprint —
> the Planning Lab material is folded into [Lab Corrections](#lab-corrections-consolidated)
> and [Master Comparison Table](#master-comparison-table-planning-lab) rather than removed.


| Person | State graph owned | LLM additions | System slice owned |
|---|---|---|---|
| **A** | `state_graph/change_order/` — change order approval & appeal | Task decomposition + constrained ReAct (form filing) | `mcp_server/` corrections + Admin platform |
| **B** | `state_graph/equipment_recovery/` — equipment breakdown recovery | RAG (manuals/catalog) + Tree of Thoughts | Memory/RAG corrections + User platform |
| **C** | `state_graph/safety_incident/` — safety incident & regulator reporting | LATS (investigation search) + constrained ReAct (regulator filing) | Shared `state_graph/core/` + Planning Lab corrections |

---

## Architecture

```
state_graph/
├── core/                  # C1 — shared, graph-agnostic engine
│   ├── models.py
│   ├── checkpoint_store.py   # every transition persisted to db/procurement.db BEFORE advancing
│   ├── graph_base.py         # StateGraph: cyclic, .run() starts-or-resumes, same call either way
│   ├── hitl.py                # require_hitl() / HITLPause
│   └── tickets.py             # TicketableError — unplanned failures, not HITL pauses
├── change_order/           # A — Problem 1
├── equipment_recovery/     # B — Problem 2
├── safety_incident/        # C — Problem 3
├── demo_crash_resume.py    # C1's toy proof-of-concept (Day 1)
└── cli.py                  # C4 — wraps all three REAL graphs the same way, for the final proof

ib_platform/                # Admin (A) + User (B) platform, HTTP API + frontend
mcp_server/                 # MCP tool server + policy docs (A corrections)
rag/                        # Hybrid RAG (BM25 + vector) + Self-RAG grounding check (B corrections)
memory/                     # Short/long-term memory (B corrections)
planning/, planning_eval/   # Planning Lab (C2b corrections) — Plan-and-Solve, ToT, LATS, Self-Refine, Reflexion
db/                         # Single shared SQLite DB (procurement.db) for everything above
```

**One database, one core, three graphs.** All three graphs and the platform read/write the
same `db/procurement.db` via `mcp_server/db.py`; all three graphs run on the same
`state_graph/core/` engine. No graph stands up its own parallel state store.

One thing worth knowing before you read the three graphs' code: **their `initial_state`
contracts are not identical.** `change_order` and `safety_incident` wrap the caller's payload
as `{"run_id": ..., "request": {...}}` (their entry nodes do task decomposition on `request`,
which may be a free-text string); `equipment_recovery` takes a flat dict with no wrapper and no
`run_id` key. This isn't a bug — each graph's own `start_new_*()` / entry node defines its own
contract — but it's easy to trip over, so `state_graph/cli.py`'s built-in demo payloads match
each graph's real contract exactly. See [Cross-Review](#cross-review-ab-core-usage) for more on this.

---

## Setup

```bash
git clone <repo> && cd task_decomposition_and_planning
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r rag/requirements.txt
pip install -r mcp_server/requirements.txt

# one-time DB migration — adds StateGraphRuns/StateGraphCheckpoints/HITLTasks/Tickets
python -m db.migrate_state_graph

# optional — without this, every LLM call falls back to a deterministic
# stub so the graphs, HITL, and tickets still run end to end
echo "GROQ_API_KEY=..." >> .env
```

---

## Crash & Resume Proof (`state_graph/cli.py`)

New for C4: one CLI that wraps all **three real graphs**, not just the Day-1 toy graph
(`demo_crash_resume.py`). Starting and resuming are the *same command* — `StateGraph.run()`
checks the checkpoint store itself and does the right thing either way.

```bash
# start (or resume) a run
python -m state_graph.cli run safety_incident demo-1

# while it's mid an LLM call (decompose / investigate / diagnose — no artificial
# sleep needed, a real call takes a couple of seconds), Ctrl+C it. Then:
python -m state_graph.cli run safety_incident demo-1        # resumes, does not restart

# inspect without advancing anything
python -m state_graph.cli status demo-1
python -m state_graph.cli history demo-1                    # the actual proof: full checkpoint trail
python -m state_graph.cli list-runs
python -m state_graph.cli pending-hitl
python -m state_graph.cli open-tickets
```

`history` prints every row of the append-only `StateGraphCheckpoints` table for that run, in
order. Verified end to end for this README: a `safety_incident` run was killed mid-`investigate`,
and the checkpoint trail after resuming showed `report_incident` appearing **exactly once** (it
had already completed before the kill) while `investigate` re-ran from scratch (it was the node
in flight when the process died) — proving completed work survives a crash and only the
in-flight node repeats.

```
#    Node                         Status         Timestamp
----------------------------------------------------------------------
176  report_incident              running        2026-08-22 21:10:06   <- completed before kill
177  investigate                  running        2026-08-22 21:10:06   <- crashed here
178  safety_officer_signoff       running        2026-08-22 21:13:02   <- investigate re-ran, this time completing
179  safety_officer_signoff       paused_hitl    2026-08-22 21:13:02   <- HITL pause, waiting on a human
```

Same works for `change_order` and `equipment_recovery` — swap the graph name.

---

## HITL and Tickets, in one sentence each

- **HITL** (`state_graph/core/hitl.py`): a node calls `require_hitl(state, reason, payload)`;
  first pass raises `HITLPause`, the runner checkpoints and opens a `HITLTasks` row instead of
  guessing; the platform's admin inbox resolves it, the SAME node re-executes and now finds the
  decision in state.
- **Tickets** (`state_graph/core/tickets.py`): any *other* exception a node raises is an
  unplanned failure, not a decision the graph is entitled to make — checkpointed the same way,
  but into a separate `Tickets` table, so it's distinguishable from a HITL pause at the schema
  level, not just in application code.

Each graph pauses for a human at a genuine, grounded decision point, not an arbitrary one:

| Graph | HITL trigger |
|---|---|
| `change_order` | Client sign-off on cost/schedule delta |
| `equipment_recovery` | Proposed action cost exceeds the *project's real, current* `RemainingBudget` |
| `safety_incident` | Safety officer sign-off, informed by the LATS investigation's regulatory-exposure recommendation (advisory — the human still decides) |

---

## Master Comparison Table (Planning Lab)

Executed via `python -m planning_eval.full_comparison` against the fixed 10-case suite
(`T01`–`T10`), all methods on the same cases. **Re-run on 2026-08-23 11:06 UTC** — 100/100
cells complete, zero rate-limited/JSON-truncated/tool-leak/reasoning-effort-rejected exclusions,
and every row's `self_graded` is `false` (an independent `JUDGE_GROQ_MODEL` was set, so ungrounded
Reflexion is judged by a different model than the one that produced the attempt — not the
self-grading-bias fallback described below).

| Method | Success Rate | Acc % | Avg Score | Avg Calls | Latency | Est. Cost |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| Decomposition-first | 10/10 | 100.0% | 0.98 | 3.0 | 14.53s | $0.239 |
| Dynamic | 7/10 | 70.0% | 0.66 | 4.8 | 27.18s | $0.342 |
| Plan-and-Solve | 9/10 | 90.0% | 0.73 | 1.0 | 8.15s | $0.256 |
| Tree-of-Thoughts | 10/10 | 100.0% | 0.67 | 8.9 | 79.20s | $0.252 |
| LATS (Grounded) | 7/10 | 70.0% | 0.60 | 7.9 | 58.14s | $0.650 |
| LATS (Ungrounded) | 5/10 | 50.0% | 0.30 | 8.5 | 58.20s | $0.623 |
| Self-Refine (Grounded) | 9/10 | 90.0% | 0.83 | 2.9 | 49.65s | $0.390 |
| Self-Refine (Ungrounded) | 9/10 | 90.0% | 0.867 | 3.9 | 56.22s | $0.385 |
| Reflexion (Grounded) | 10/10 | 100.0% | 0.76 | 1.4 | 19.95s | $0.178 |
| Reflexion (Ungrounded) | 9/10 | 90.0% | 0.867 | 2.7 | 19.45s | $0.159 |

All 10 required rows are present (see [Lab Corrections](#lab-corrections-consolidated) — this
table used to be missing Plan-and-Solve, both Self-Refine variants, and Reflexion (Grounded)
entirely). Two C2b-era bugs that were previously flattening Tree-of-Thoughts and Self-Refine
(Ungrounded) to a hard 0/10 regardless of actual plan quality are fixed as of this run (a
grounding-check target mismatch in `run_tot()`, and a dead ungrounded-evaluator call in
`self_refine.py`) — both now score in line with the other methods instead of a uniform zero.

**LATS still shows a clean grounded-vs-ungrounded gap** (0.60 vs. 0.30) — grounding matters
most for a method that's already exploring/committing to actions via search. **Self-Refine and
Reflexion's ungrounded scores now sit at or slightly above their grounded counterparts**
(0.867 vs. 0.83 and 0.76) under an independent judge — worth treating as a real result to discuss
in the writeup (a genuinely separate judge model can be a reasonably calibrated critic on this
task), not assuming grounding "must" win the way it did in the earlier, bugged run.

**Sub-task routing recommendations** (unchanged from the Planning Lab): Plan-and-Solve for
simple/deterministic synthesis, Tree-of-Thoughts for ranking/options, LATS (Grounded) for
high-stakes/financial proposals, Reflexion for multi-trial learning tasks with a real grader
available.

---

## Lab Corrections (Consolidated)

Corrections filed and closed against each of the three labs this sprint built on top of.

### MCP Server Lab (Person A — `mcp_server/`)
Tracked as GitHub issues on the repo (not a local file) per the A1 task. See the repo's Issues
tab for the individual items closed against `mcp_server/`.

### Memory & RAG Lab (Person B — `memory/`, `rag/`)
Full detail in [`memory/ISSUES.md`](memory/ISSUES.md). Closed items include:
- Policy resources 404 — `mcp_server/policies/` didn't exist; the three policy docs were moved
  into it so `resources/read` resolves instead of throwing `FileNotFoundError`.
- Doc undercount — `mcp_server/README.md` and the root README both said "two" safety-policy
  documents when there are three (`equipment_operation_safety_rules.md` was omitted).
- `rag/sync_policies.py`'s `SOURCE_DIR` cross-team dependency on the policy-dir fix above.
- Short-term memory buffer had no scratchpad, risking in-progress task state being pruned.
- No decision layer for what survives short-term memory overflow.
- Semantic memory had no consolidation layer (no versioning, no conflict handling).
- Self-RAG-style grounding checker was missing for memory recall — this is the SAME
  `support_check` mechanism `safety_incident`'s LATS module (`state_graph/safety_incident/lats.py`)
  now reuses via `rag/hybrid_search.py` to ground its regulatory-exposure scoring, so this fix
  ended up load-bearing for C3 as well as B5.

### Planning Lab (Person C — `planning_eval/`, `planning/algorithms/reflexion.py`) — C2b
- **Missing eval rows** — the comparison table used to be missing Plan-and-Solve, Self-Refine
  (both variants), and Reflexion (Grounded). All four now run and report in
  `planning_eval/full_comparison.py`; see the table above.
- **Rate-limit vs. grounded-failure split** — `full_comparison.py` previously counted a
  transient Groq 429 the same as a genuine plan-quality failure, deflating every method's
  success rate. It now classifies errors (`_RATE_LIMIT_MARKERS`), retries rate-limited calls
  with backoff before giving up, and reports `rate_limited_excluded` separately from real
  failures.
- **Reflexion self-grading bias** — Reflexion (Ungrounded) used to post the *highest* average
  score of any method (0.37) because it was graded by the same model that produced the attempt.
  `reflexion.py` now tracks `self_graded=True` explicitly whenever no independent judge/environment
  is supplied, and `full_comparison.py` flags self-graded rows in its console output rather than
  presenting them as directly comparable to grounded scores.

---

## Cross-Review: A/B Core Usage

Done as part of C4. What was checked, and what came out of it:

**All three graphs genuinely build on `state_graph/core/`, not a reimplementation.**
`change_order/graph.py` and `safety_incident/graph.py` both import `checkpoint_store.default_store`
directly (used for `resume_after_signoff`/`resume_after_ticket`-style helper functions that need
to load a run's current state before deciding how to resume it). `equipment_recovery/graph.py`
doesn't import `checkpoint_store` at all — it only imports `StateGraph`/`END` and calls
`graph.run(run_id, initial_state=..., store=store)` directly (see `test_equipment_recovery.py`),
relying entirely on `StateGraph.run()`'s own internal `store.load()`/`store.save_checkpoint()`
calls. Both patterns are valid uses of the same core — `equipment_recovery` simply doesn't (yet)
have a helper function that needs to inspect a run's state *before* calling `.run()` the way
`resume_after_signoff` does for the other two — but it's worth knowing which pattern you're
looking at before assuming a missing import is a gap.

**Known remaining bug (documented, not yet fixed):** `approval_gate` in
`equipment_recovery/nodes.py` can re-open a new HITL task for an **already-approved** run if
`graph.run()` is invoked more than once for the same `run_id` — the consumed `hitl_decision`
key doesn't get cleared from state after use, since `graph_base.py`'s `_loop()` only ever
*merges* a node's return dict into state, never removes a key. Filed as a follow-up issue per
the Day-4 commit that found it (`66e3196`); does not affect the crash/resume proof above (that
exercises a single in-flight run, not a completed one being re-invoked), but worth fixing before
relying on `equipment_recovery` HITL resolution being called defensively/idempotently from the
platform.

**Initial-state contract inconsistency** — see [Architecture](#architecture) above:
`change_order`/`safety_incident` wrap the payload in `{"run_id", "request"}`;
`equipment_recovery` doesn't. Not a bug (each graph's entry contract is internally consistent
and exercised by its own tests), but worth normalizing if a fourth graph is ever added, so a
platform caller doesn't need per-graph special-casing.

---


## Presentation Split (10 min, all three present)

| Person | ~3 min covering |
|---|---|
| A | Change-order graph walkthrough + live admin demo: toggle a tool off via `platform/frontend/admin/`, show `tool_registry.py` blocks the call; resolve a pending HITL task. |
| B | Equipment-recovery graph walkthrough + live user demo: switch between `agent.py`, `planning_agent.py`, and the new state-graph agents; add a RAG doc and show it change a retrieval answer live. |
| C | Safety-incident graph walkthrough + `python -m state_graph.cli run safety_incident <run-id>`, kill it mid-run on camera, restart, `python -m state_graph.cli history <run-id>` to show resume with no re-execution. |

---

## Credits

Built on top of the reference toolkit: `github.com/AmrSheta22/task_decomposition_and_planning`.
Extends the IronBridge MCP server, database, and Memory/RAG agent from Weeks 2–3.
