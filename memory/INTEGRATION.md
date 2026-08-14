# Wiring memory/ into the existing agent loop

Verified directly against `ironbridge-memory-rag-main.zip` — every name
below is copied from the real file, not assumed.

## 1. `agent/agent.py` — the real conversation loop

The loop lives in `run_agent()`. The transcript is the plain list
`conversation: list[dict]` (line 125), appended to at three points:
- line 132: `conversation.append({"role": "user", "content": user_text})`
- line 164: `conversation.append(assistant_msg)`
- line 177-183: `conversation.append({"role": "tool", "tool_call_id": tc.id, "content": text})`

There is currently **no `session_id` and no `project_id` tracked
anywhere** in `agent.py` — every tool call just passes whatever
arguments the model chose (e.g. `project_id` shows up only as a
per-call argument to tools like `view_project_budget`). So wiring
memory in requires adding a session id once per `run_agent()` call, and
best-effort extracting `project_id` from tool-call args when present.

### Exact patch

At the top of `run_agent()`, right after `groq_client = Groq(api_key=api_key)`
(line 81):

```python
import uuid
from memory.short_term import SessionMemory
from memory.router import PromoteOrDropRouter
from memory.stores import EpisodicStore, SemanticStore
from memory.consolidation import ConsolidationJob
from memory.self_rag_check import SelfRAGChecker

session_id = str(uuid.uuid4())
episodic_store = EpisodicStore()
semantic_store = SemanticStore()
router = PromoteOrDropRouter(episodic_store)
checker = SelfRAGChecker()
session_mem = SessionMemory(session_id, max_turns=20)

# Separate periodic pass -- NOT triggered by the router. Startup is fine
# for a single-process demo; swap for a real scheduler in production.
ConsolidationJob(episodic_store, semantic_store).run()
```

Replace line 132:
```python
conversation.append({"role": "user", "content": user_text})
```
with:
```python
conversation.append({"role": "user", "content": user_text})
evicted = session_mem.add_turn("user", user_text)
if evicted:
    router.route(evicted)
```

Right after line 164 (`conversation.append(assistant_msg)`), add:
```python
if message.content:
    evicted = session_mem.add_turn("assistant", message.content)
    if evicted:
        router.route(evicted)
```

Inside the `for tc in message.tool_calls:` loop, right after line 183
(`conversation.append({"role": "tool", ...})`), add:
```python
tool_project_id = args.get("project_id")  # best-effort; None if this
                                           # tool call didn't include one
evicted = session_mem.add_turn("tool", text, project_id=tool_project_id)
if evicted:
    router.route(evicted)
```

For the scratchpad and Self-RAG-checked memory recall, the natural
place is right before the `groq_client.chat.completions.create(...)`
call (line 136) — prepend the scratchpad and any relevant recalled
memory as an extra system-ish message in `conversation`, e.g.:

```python
recalled = [e.content for e in episodic_store.recall(session_id=session_id)]
recalled = checker.filter_relevant(user_text, recalled)
if recalled or session_mem.scratchpad.plan:
    memory_block = session_mem.scratchpad.as_context_block()
    if recalled:
        memory_block += "\n[RELEVANT MEMORY]\n" + "\n".join(f"- {r}" for r in recalled)
    conversation.append({"role": "system", "content": memory_block})
```

(Adjust placement once Person 2's chosen `context_eval/` pruning
strategy is wired in too — the scratchpad injection should happen
*after* whatever pruning strategy trims `conversation`, so it survives
regardless of which strategy is shipped.)

## 2. `mcp_server/server.py` / `validation.py` / `db.py` — no changes needed

Confirmed from the real files: `db.py` already has
`list_safety_policies()` reading the real `SafetyPolicies` table, and
`server.py`'s three resources (`material_handling_policy`,
`warehouse_safety_policy`, `equipment_operation_policy`) read flat
files via `POLICY_DIR`. Memory doesn't call into any of this directly
— `agent.py` passing real tool results into `session_mem.add_turn()`
is what gives the router/consolidation real system context. No edits
needed here beyond the bug fix in §3.

## 3. Bug fixes #1–2 (Person 1, do these first) — confirmed against real files

**Root cause, confirmed:** `mcp_server/server.py`'s `_find_policy_dir()`
(lines 26-48) already looks for `mcp_server/policies/` first — the code
anticipates the fix. But the three actual files —
`material_handling_procedures.md`, `warehouse_safety_regulations.md`,
`equipment_operation_safety_rules.md` — sit directly in `mcp_server/`,
not in a `policies/` subfolder. So `_find_policy_dir()` falls through
all three candidates and defaults to `cwd/policies`, which doesn't
exist either → any `resources/read` call throws `FileNotFoundError`.

Note also: `rag/policies/` already has its own copies of all three
files (for the RAG corpus) — separate from this fix, don't touch those,
they're Person 3's.

**Fix:**
```bash
mkdir -p mcp_server/policies
git mv mcp_server/material_handling_procedures.md mcp_server/policies/
git mv mcp_server/warehouse_safety_regulations.md mcp_server/policies/
git mv mcp_server/equipment_operation_safety_rules.md mcp_server/policies/
```
No code change needed in `server.py` itself — `_find_policy_dir()`
already checks `this_dir / "policies"` first (line 34), so moving the
files is the entire fix.

**Bug #2, confirmed:** `mcp_server/README.md` line 10 says:
> `policies/` — two safety-policy documents exposed to the model as resources, not tools

Two problems in that one line: (a) the path doesn't exist yet (fixed by
the `git mv` above), and (b) it says **"two"** documents but there are
**three** (`equipment_operation_safety_rules.md` is a real third
resource registered in `server.py`, `equipment_operation_policy()`).

**Fix:** in `mcp_server/README.md`, change:
```diff
- **`policies/`** — two safety-policy documents exposed to the model as resources, not tools
+ **`policies/`** — three safety-policy documents exposed to the model as resources, not tools
```

**Acceptance criteria:**
> `resources/read` succeeds for all three policy resources
> (`policy://material-handling`, `policy://warehouse-safety`,
> `policy://equipment-operation`) with no `FileNotFoundError`, and
> `mcp_server/README.md` says "three," not "two."
