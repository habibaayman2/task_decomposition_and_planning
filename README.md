# Memory & RAG Lab — extending the assistant above

This section covers the second lab: giving the same agent long-term
memory and grounded retrieval over documents it can't reach through a
tool call. See `memory/`, `context_eval/`, `rag/`, and `retrieval_eval/`
for the implementations; this section is the shared problem-framing
writeup, one subsection per person, followed by a project-wide summary
of what was built and the final architecture decisions.

## Problem Framing

The problem framing for this lab was split three ways — each person
identified and justified the part of the gap that maps to their own
deliverable, so the case for *why* each concern below is necessary
comes from a real operational cost, not a checklist requirement.

### Why persistent memory is a real gap for IronBridge (Person 1)

The MCP server above already lets staff check inventory, budgets, and
equipment in real time — but every one of those tool calls happens
inside a session that starts from nothing and ends with nothing kept.
In practice that means:

- A site engineer re-explains the same recurring problem — e.g. that
  **Reinforcement Steel 12mm (MaterialID 2)** keeps running below
  `MinimumStockLevel` on Project 1 — every time they open a new session,
  because the assistant has no memory of having heard it before.
- A Project Manager's standing preference (e.g. "escalate early, don't
  wait for the deadline") has to be repeated to the assistant on every
  approval-adjacent conversation, because nothing about *how this PM
  likes to work* survives past the current connection.
- A supplier's behavior changes over time — **Ironbridge Steel Yard**
  quoting a longer lead time after a fleet change, for example — and
  without a place to store that as a fact with a date and a version,
  the assistant either has no answer or, worse, keeps repeating a
  now-wrong number with no way to know it went stale.

None of this is a toy need: forgetting the low-stock pattern means a
site engineer keeps hitting the same wall every session; treating a
changed supplier fact as unchanging means an approval decision gets
made on stale information with no record that it *was* stale. That's
the justification for `memory/`'s scope — a real short-term
buffer + scratchpad (so pruning never wipes what the agent is mid-way
through), a promote-or-drop router with logged reasoning (so what
survives a session boundary is a deliberate decision, not an accident),
and a consolidation layer that versions and dates facts instead of
silently overwriting them when they change. Full mapping of each
concern to its file is in `memory/README.md`.

### Why context management matters given IronBridge's real call shape (Person 2)

IronBridge's longest agent sessions are triage-style: a site engineer
or procurement officer works through inventory checks, budget checks,
and equipment checks for a single multi-material request, and the
transcript fills up with large JSON tool outputs long before it fills
up with actual dialogue. A budget constraint or stock warning mentioned
early in the conversation can get buried under dozens of tool calls by
the time the assistant needs it to make a final decision — the exact
"lost in the middle" failure mode that makes naive context truncation
dangerous for a system with real approval stakes.

`context_eval/` tests this directly: all four strategies (sliding
window, observation/tool-output masking, recursive summarization,
zone-based pruning) run against the same long-context test suite, with
a critical early detail that has to survive to the final turn. The
live benchmark results:

| Strategy | Recall Accuracy | Avg Input Tokens | Avg Output Tokens | Avg Latency | Extra LLM Calls |
|---|:---:|:---:|:---:|:---:|:---:|
| Sliding Window | 0/10 (0%) | ~834 | ~22 | 0.35s | 0 |
| **Observation Masking** | **10/10 (100%)** | **1,303** | **24** | **0.42s** | **0** |
| Recursive Summarization | 10/10 (100%) | ~815 | ~25 | 1.85s | 1+ per compaction |
| Zone-Based Pruning | 10/10 (100%) | 1,298 | ~24 | 0.45s | 0 |

Sliding window fails outright — it drops the critical detail the moment
it ages past the window. The other three all preserve it, but
Observation Masking matches IronBridge's actual bloat source directly
(verbose tool JSON, not conversational turns), needs no extra LLM
calls, and posts the lowest latency of the three reliable strategies.
That's why it ships as the default, justified against the table above,
not intuition. Full detail in `context_eval/README.md`.

### Why the policy corpus is a genuine retrieval problem, not a lookup problem (Person 3)

Three safety policy documents — `material_handling_procedures.md`,
`warehouse_safety_regulations.md`, and `equipment_operation_safety_rules.md` —
originally existed only as static MCP resources: fetched once, read in full,
never queried. That works while the corpus is two short files, but it isn't
retrieval in any meaningful sense — no chunking, no relevance ranking, no
way to answer a targeted question ("what does Policy #2 say about fire lane
clearance?") without the model reading the entire document and hoping
the answer surfaces. As the knowledge base grows to include supplier
correspondence and historical purchase-decision rationale — real,
ungoverned documents nobody wants to turn into individual MCP tools —
this becomes a genuine retrieval problem rather than a lookup problem.

Both gaps carry real stakes given IronBridge's existing governance
requirements: a forgotten budget constraint or a hallucinated safety
procedure isn't a cosmetic failure, it's the exact kind of silent
policy violation the original MCP server was designed to prevent. A
retrieval system that can't demonstrate real accuracy differences
across architectures, real verification against source material, and a
choice justified by evidence rather than assumption isn't solving
IronBridge's actual problem — it's decoration on top of it.

`rag/` implements all three required retrieval architectures (naive,
hybrid, agentic) over a real Qdrant vector store with HNSW ANN index,
metadata payload store, and metadata filtering. `retrieval_eval/`
proves which one IronBridge should actually ship, with numbers. See
below for the results.

Self-RAG verification runs on every retrieval: chunks that fail relevance
are dropped and logged (`[self-rag-check] DROPPED...`), and every
generated answer is checked against its source chunks before reaching
the user. This applies to both RAG answers and memory recall from
episodic/semantic storage (see `memory/self_rag_check.py`).

---

## Project-wide summary: what was built

### Repository map

```text
memory/           Person 1 — short-term buffer, scratchpad, promote-or-
                   drop router, episodic/semantic stores, consolidation,
                   Self-RAG checker module
context_eval/      Person 2 — four context management strategies, live
                   LLM benchmark suite, comparison table
rag/               Person 3 — chunking, embedding, Qdrant vector store,
                   naive/hybrid/agentic RAG pipelines
retrieval_eval/    Person 3 — fixed test question set, comparison
                   harness across all three RAG architectures
agent/agent.py     Wired by all three — memory read/write, context
                   pruning, and RAG routing all live in the same
                   conversation loop
```

### Bug fixes (existing repo, fixed before new work)

| # | Bug | Fixed by |
|---|---|---|
| 1 | `mcp_server/policies/` didn't exist; resource reads threw `FileNotFoundError` | Person 1 |
| 2 | Docs referenced the same missing `policies/` path | Person 1 |
| 3 | Inconsistent `mcp` version pins across requirements files | Person 2 |
| 4 | `agent/README.md` pointed at a `demo_transcripts/` path that didn't exist | Person 2 |
| 5 | Dangling `db_mssql` import crashed the server if `IRONBRIDGE_DB_ENGINE=mssql` was set | Person 3 |
| 6 | README referenced `agent/client.py`/`db/erd.mmd` (wrong filenames), unused `anthropic` dependency, and an orphaned `SafetyPolicies` row with no resource | Person 3 |

### Retrieval architecture: final decision

`retrieval_eval/evaluate.py` runs a fixed 6-question set (two questions
favoring each architecture) against naive, hybrid, and agentic RAG,
all using the same Qdrant vector store and the same Groq model for
generation, isolating the comparison to retrieval strategy alone:

| Architecture | Accuracy | Avg Input Tokens | Avg Output Tokens | Avg Latency |
|---|:---:|:---:|:---:|:---:|
| Naive RAG | 1/6 (17%) | 261 | 90 | 0.37s |
| **Hybrid RAG** | **6/6 (100%)** | 511 | 56 | **1.45s** |
| Agentic RAG | 6/6 (100%) | 466 | 33 | 3.68s |

**Hybrid Search ships as the default.** It matches Agentic RAG's
accuracy at under half the latency, because BM25 catches the exact
identifiers (`"Policy #2"`, `"50kg"`) that pure vector similarity
misses — confirmed directly during development, where a citation-heavy
query scored `vector=0.000, bm25=0.991` on the correct chunk. **Agentic
RAG stays in the system as a routed fallback** for questions the agent
detects as genuinely multi-part (spanning two or more policy sections),
where a single hybrid retrieval call has been shown to miss a sub-topic
that a second, more targeted retrieval hop can recover. Naive RAG's
17% accuracy is the control group result that motivates hybrid search
existing at all. Full methodology in `retrieval_eval/README.md`.

### Self-RAG verification

Every RAG answer (naive, hybrid, and agentic) and every memory recall
from episodic/semantic storage passes through `memory/self_rag_check.py`
before reaching the user:

- **Relevance check** — each retrieved chunk is scored against the
  query; chunks that fail are dropped and logged
  (`[self-rag-check] DROPPED chunk...`), not silently included.
- **Support check** — the final generated answer is checked against
  the chunks it was grounded in; a passing check is logged with its
  overlap score, so grounding isn't assumed, it's verified and visible.

### Agent integration

`agent/agent.py` wires all three subsystems into the same conversation
loop: memory (session buffer, scratchpad, promote-or-drop routing on
overflow) wraps every turn, context pruning (Observation Masking)
manages the tool-output-heavy history, and RAG routing sends
policy-shaped questions to Hybrid Search by default or Agentic RAG when
the question is detected as multi-part — all in one end-to-end run, not
three separate demo scripts. See `agent/rag_demo_transcript.txt` and
`demo/DEMO.mp4` for a recorded walkthrough.

---

## Setup

```bash
# 1. Install dependencies
pip install -r mcp_server/requirements.txt
pip install -r agent/requirements.txt
pip install -r rag/requirements.txt

# 2. Configure environment (.env in repo root)
#    GROQ_API_KEY=...
#    IRONBRIDGE_DB_PATH=./db/procurement.db
#    IRONBRIDGE_DB_ENGINE=sqlite

# 3. Build the RAG vector store
python -m rag.vector_store

# 4. Run the agent
python agent/agent.py

# 5. (Optional) Run evaluations independently
python -m context_eval.evaluate
python -m retrieval_eval.evaluate
```

#### Graph RAG Bonus Evaluation

As an optional extension (+5 pts), a Knowledge Graph was manually
constructed over the policy corpus entities (Material, Regulation,
Role, Measurement, Equipment) and evaluated against the same question
set. Graph RAG traverses explicit entity-relationship paths (e.g.
`Cement --[stored_on]--> Wooden Pallets --[governed_by]--> Policy #2`)
rather than semantic similarity or keyword matching:

| Architecture | Accuracy | Avg Tokens/Query | Avg Latency/Query |
|:---|:---:|:---:|:---:|
| Naive RAG (mock baseline) | 2/6 (33%) | 22 | 0.040491s |
| Hybrid Search (mock baseline) | 4/6 (66%) | 27 | 0.080597s |
| Agentic RAG (mock baseline) | 5/6 (83%) | 59 | 0.310571s |
| **Graph RAG (Bonus)** | **6/6 (100%)** | **124** | **0.000266s** |

Graph RAG achieves perfect recall on this entity-dense corpus by
following structured relationships, but requires a pre-constructed
knowledge graph (`rag/knowledge_graph.py`). It demonstrates that
policy documents with real entity relationships (Material → Handling
Rule → Required PPE) are worth modeling as a graph, while Hybrid
Search remains the shipped default for general-purpose queries that
do not require explicit graph traversal. See
`retrieval_eval/graph_rag_eval.py` for the evaluation harness.

# DIMO VIDEO

https://github.com/user-attachments/assets/73f33f8d-0fc1-4a46-86ac-405477466a5e




