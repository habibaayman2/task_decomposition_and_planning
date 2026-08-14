# Issues to open (Person 1)

Verified against the real `ironbridge-memory-rag-main.zip`, not
assumed. Copy each `###` block into one GitHub Issue (title = the `###`
line). Each has a real constraint plus a concrete fix to close against.

---

### Policy resources 404 — `mcp_server/policies/` doesn't exist (bug #1)

**Description:** Confirmed in `mcp_server/server.py`'s
`_find_policy_dir()` (lines 26–48): it already looks for
`mcp_server/policies/` first, but the three actual files —
`material_handling_procedures.md`, `warehouse_safety_regulations.md`,
`equipment_operation_safety_rules.md` — sit directly in `mcp_server/`.
All three candidate paths miss, `POLICY_DIR` falls back to
`cwd/policies` (also nonexistent), so any `resources/read` call for any
of the three policy resources throws `FileNotFoundError`.

**Fix:**
```bash
mkdir -p mcp_server/policies
git mv mcp_server/material_handling_procedures.md mcp_server/policies/
git mv mcp_server/warehouse_safety_regulations.md mcp_server/policies/
git mv mcp_server/equipment_operation_safety_rules.md mcp_server/policies/
```
No code change needed — `_find_policy_dir()` already checks
`this_dir / "policies"` first. Verified locally: all three resource
functions (`material_handling_policy`, `warehouse_safety_policy`,
`equipment_operation_policy`) resolve and read correctly once moved.
**Verify:** `resources/read` succeeds for `policy://material-handling`,
`policy://warehouse-safety`, and `policy://equipment-operation` with no
traceback.

---

### `mcp_server/README.md` and root `README.md` undercount the policy resources (bug #2)

**Description:** Two separate doc bugs, same root cause as #1:
1. `mcp_server/README.md` line 10 references a `policies/` folder that
   doesn't exist yet (fixed by the move above), *and* says **"two"**
   safety-policy documents when there are **three** —
   `equipment_operation_safety_rules.md` is a real third resource
   (`equipment_operation_policy()` in `server.py`) that the doc simply
   omits.
2. Root `README.md`'s protocol-concern table (Resources row) lists only
   "Material Handling Procedures and Warehouse Safety Regulations,"
   same omission.

**Fix:** Applied directly:
```diff
# mcp_server/README.md
- **`policies/`** — two safety-policy documents exposed to the model as resources, not tools
+ **`policies/`** — three safety-policy documents (Material Handling Procedures, Warehouse Safety Regulations, Equipment Operation Safety Rules) exposed to the model as resources, not tools
```
```diff
# README.md (root), Resources row
- Material Handling Procedures and Warehouse Safety Regulations are read once via `resources/read`, not re-fetched per question
+ Material Handling Procedures, Warehouse Safety Regulations, and Equipment Operation Safety Rules are read once via `resources/read`, not re-fetched per question
```
**Verify:** grep the repo for `"two safety-policy"` and for the old
flat `mcp_server/<filename>.md` path — no hits.

---

### `rag/sync_policies.py`'s `SOURCE_DIR` will break once bug #1 is fixed — cross-team, needs Person 3

**Description:** Found while fixing bug #1. `rag/sync_policies.py`
(Person 3's file) hardcodes `SOURCE_DIR = REPO_ROOT / "mcp_server"` —
it reads the three policy files flat out of `mcp_server/`, not
`mcp_server/policies/`. Moving the files to fix bug #1 (see above)
means `sync_policies.py` will now report all three files "missing" and
silently stop refreshing `rag/policies/`'s corpus snapshot, since it
fails soft (prints a warning, doesn't raise). This is exactly the kind
of thing that looks fine in a quick manual test and quietly breaks the
RAG corpus sync later.

**I did not fix this myself** — `rag/` is Person 3's, not mine to
edit. Flagging here so it's tracked instead of discovered later when
`rag/policies/` silently goes stale.

**Fix (for Person 3):** in `rag/sync_policies.py`, change:
```diff
- SOURCE_DIR = REPO_ROOT / "mcp_server"
+ SOURCE_DIR = REPO_ROOT / "mcp_server" / "policies"
```
**Verify:** run `python rag/sync_policies.py` after bug #1 lands —
output should say "Synced 3 policy file(s)," zero "missing."

---

### Short-term buffer has no scratchpad — pruning risks wiping in-progress task state

**Description:** `agent/agent.py`'s `run_agent()` holds the whole
transcript in one plain list, `conversation: list[dict]` (line 125),
appended to at three points (user input, assistant replies, tool
results). There's nowhere to hold the agent's *current* plan/sub-goal
separate from that list, so whichever pruning strategy Person 2 ships
against `context_eval/` risks discarding in-progress task state along
with old dialogue — e.g. mid-way through reserving Reinforcement Steel
12mm for Project 1, losing track of "already checked budget, still
need to check MinimumStockLevel."

**Fix:** `memory/short_term.py` adds `ShortTermBuffer` (evictable) and
`Scratchpad` (a separate dataclass) as two distinct objects, wired
alongside `conversation` per `memory/INTEGRATION.md`. **Verify:** run
`python -m memory.demo` — buffer evicts turns while
`Scratchpad.plan`/`sub_goal`/`working_state` print unchanged
before/after.

---

### No decision layer for what survives short-term memory overflow

**Description:** Evicted turns from the buffer above are currently
just discarded, including durable facts (a supplier's delay pattern, a
Project Manager's standing escalation preference) that staff would
otherwise have to re-explain next session.

**Fix:** `memory/router.py` adds `PromoteOrDropRouter.route()`, which
returns a logged `forget`/`episodic` decision with a reason per item,
and only ever writes to `EpisodicStore` — verifiable by grep, no
`SemanticStore` import anywhere in `router.py`. **Verify:**
`router.audit_log()` shows a reason string for every routed item; `python
-m memory.demo` §2 shows both outcomes firing on real turns.

---

### Semantic memory has no consolidation layer — no versioning, no conflict handling

**Description:** Without a separate consolidation pass, semantic facts
either never get written or get silently overwritten, losing history —
e.g. Ironbridge Steel Yard's lead time changing after a fleet change,
with no record of the prior figure, or a fact nobody's confirmed in
months still being treated as current.

**Fix:** `memory/consolidation.py` adds `ConsolidationJob.run()`,
called on a schedule/at startup, never from `router.py`. Running it
against two episodes implying different lead-time values for the same
supplier writes a new version, marks the old one `superseded` (not
deleted — `conflict_note` records both values), and a separate
`expire_stale()` sweep marks TTL-exceeded facts `expired`. **Verify:**
`python -m memory.demo` §3 shows the Ironbridge Steel Yard conflict
(14→21 days) resolved with both versions visible in
`semantic_store.history(...)`.

---

### Self-RAG-style checker missing for memory recall (shared call site Person 3 needs)

**Description:** Recalled episodic/semantic items currently go
straight into context with no relevance check, risking an unrelated
memory polluting an answer the same way an ungrounded RAG chunk would.
Person 3 needs the same module to wire into `rag/`'s retrieval
pipeline post-generation.

**Fix:** `memory/self_rag_check.py` adds `SelfRAGChecker` with
`relevance_check()`, `support_check()`, and `filter_relevant()`, which
drops and logs anything below threshold rather than passing it through
silently. **Verify:** `python -m memory.demo` §4 shows a planted
irrelevant recalled item printed as dropped, with the kept-count
excluding it. Person 3 can `from memory.self_rag_check import
SelfRAGChecker` unmodified for the RAG call site.
