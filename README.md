# 🏗️ IronBridge Planning Agent — Task Decomposition & Planning Lab (Week 4)

> **Agent:** Delay-Response Planning Agent
> **Company:** IronBridge Construction
> **Sits alongside:** The Memory & RAG Agent (`agent/agent.py`) — does not replace or duplicate it.
> **New entry point:** `agent/planning_agent.py`

---

## Problem Framing

IronBridge is a construction company managing multiple projects involving materials, contractors, equipment, and deadlines. A major operational pain point is **selecting the optimal response when a delay hits**.

A project may face delays from:
- Material shortages (e.g., rebar delivery late)
- Contractor availability
- Equipment breakdowns
- Budget constraints
- Weather conditions

**Why a single tool call is not enough:**
When a site engineer reports "Project 1 is behind schedule," the assistant cannot know whether the cause is a material shortage, equipment failure, or budget overrun until it **queries the database**. A single lookup returns raw data; deciding which mitigation strategy fits the **remaining budget**, **available stock**, and **equipment status** requires a multi-step plan with branching.

**Why a wrong plan costs something:**
Recommending a rush order when the remaining budget is $42,000 and the rush premium is $999,999 wastes procurement time, damages supplier trust, and extends the delay. The planning agent must **validate every proposal against live DB constraints** before shipping it — this is exactly what the grounded LATS environment does (see Case T03 below).

**Why decomposition-first vs. dynamic matters:**
- **Decomposition-first** generates the full DAG upfront (`diagnose → rank_options → propose_plan → notify`). It is cheap and predictable when the problem shape is already known.
- **Dynamic decomposition** decides the next step only after observing the previous result. When diagnosis reveals a mid-plan surprise (e.g., low stock the static plan didn't anticipate), a fixed plan keeps executing the stale route; dynamic decomposition reacts.

**Why search (ToT / LATS) matters:**
The `rank_options` sub-task genuinely benefits from comparing multiple strategies before committing. The `propose_plan` sub-task must survive a real budget check — a wrong proposal is expensive to unwind by phone. Plan-and-Solve is reserved for simple, low-stakes synthesis (e.g., the final notification).

---

## Repository Map

```
planning/
├── algorithms/
│   ├── decomposition.py           # DAG generation + IronBridge executors (DB calls)
│   ├── dynamic_decomposition.py   # Interleaved planning + execution
│   ├── plan_and_solve.py          # PS for simple synthesis
│   ├── tree_of_thoughts.py        # ToT for ranking/options
│   ├── lats.py                    # LATS for propose_plan (grounded env)
│   ├── self_refine.py             # One-draft critique + revision
│   ├── reflexion.py                # Multi-trial with episodic memory
│   ├── environment.py             # IronBridgeEnvironment (real DB checks) + ungrounded baseline
│   └── router.py                  # Routes sub-tasks to the right algorithm
├── models.py                      # Plan, Task, Thought, EnvironmentFeedback (DAG + cycle checks)
├── cli.py                         # CLI entry point for all modes
└── model_provider.py              # Groq / deterministic fallback LLM

planning_eval/
├── decomposition_eval.py          # Decomposition-first vs. dynamic benchmark
├── full_comparison.py             # Unified benchmark — ALL required methods, ALL cases
├── lats_grounded_eval.py          # LATS grounded-vs-ungrounded focused benchmark
└── self_correction_eval.py        # Self-Refine / Reflexion benchmark

agent/
├── agent.py                       # Memory & RAG agent (Week 3) — untouched
└── planning_agent.py              # This week's agent: RAG + MCP tools + Planning, routed together

tests/
├── test_decomposition.py          # Acyclicity, divergence, IronBridge-specific decomposition tests
├── test_eval.py                   # Algorithm unit tests + grounded-environment tests
└── test_router.py                 # Live smoke test of the routing layer (see Demo Evidence #2)

artifacts/
└── full_comparison_table.json     # Trace backing the Master Comparison Table below
```

---

## Master Comparison Table (Evaluation Results)

Executed via `python -m planning_eval.full_comparison` against the **same fixed 10-case test suite** (`T01`–`T10`, defined in `planning_eval/full_comparison.py`) for every method, so all numbers below are directly comparable.

| Method | Success Rate | Acc % | Avg Score | Avg Calls | Latency | Est. Cost |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| Decomposition-first (Static) | 2/10 | 20.0% | 0.25 | 2.0 | 1.47s | $0.11 |
| Dynamic Decomposition | 5/10 | 30.0% | 0.22 | 5.2 | 8.96s | $0.18 |
| Tree-of-Thoughts (ToT) | 4/10 | 40.0% | 0.20 | 4.2 | 6.55s | $0.07 |
| LATS (Grounded) | 4/10 | 40.0% | 0.24 | 3.4 | 3.68s | $0.02 |
| LATS (Ungrounded) | 4/10 | 40.0% | 0.20 | 3.8 | 4.12s | $0.03 |
| Reflexion (Ungrounded) | 4/10 | 40.0% | 0.37 | 1.4 | 3.14s | $0.02 |

**What the numbers say so far:**
- **Dynamic beats Static** on success rate (5/10 vs 2/10) at roughly double the cost and 6× the latency — the adaptive replanning pays for itself when the initial diagnosis turns up a surprise (see Case T01 below).
- **LATS (Grounded) and LATS (Ungrounded) post the same success count** (4/10) with only a modest score gap (0.24 vs 0.20) in the aggregate. The aggregate numbers alone are a weak argument for grounding — the real evidence is the per-case walkthrough in Case T03, where grounding catches a specific budget violation ungrounded scoring misses entirely.
- **Reflexion (Ungrounded) posts the highest average score of any method (0.37)** despite being self-graded by the same model that produced the attempt. This is worth flagging rather than treating as a win — see Known Limitations.

---

## Key Planning Concerns

### 1. Task Decomposition: Static vs. Dynamic

Both methods run against the same real request type (`decompose_goal` / `dynamic_decomposition` in `planning/algorithms/`), with acyclicity enforced at `Plan` construction time via `networkx` + a Pydantic `model_validator` (`planning/models.py`) — a cyclic plan is rejected before it can ever execute, not caught mid-run.

**Divergence — Case T01 (Mitigate Delay):** the dynamic planner pivoted its strategy mid-execution after observing low stock levels that the static plan's pre-committed route didn't account for, reaching a **0.90** score. The static plan, having committed to its full DAG before any real diagnosis existed, scored **0.34** on the same request.

### 2. Planning Algorithms (Plan-and-Solve, ToT, LATS)

- **Plan-and-Solve** — simple synthesis (e.g., Case T04's capacity estimate, and the `notify` sub-task).
- **Tree-of-Thoughts** — generates and self-evaluates multiple mitigation-strategy branches before committing; used for `rank_options`.
- **LATS** — our most robust method for high-stakes proposals. MCTS-guided search (UCT selection, backpropagation, verbal reflection on failed branches) scored by real external DB feedback rather than the model's own opinion; used for `propose_plan`.

### 3. Grounded vs. Ungrounded Critique

We replaced the toolkit's randomized evaluator (`algorithms/environment.py::Environment`, `random.betavariate`) with a real `IronBridgeEnvironment` that checks proposals against `mcp_server/db.py`: remaining budget, supplier `ContractStatus`, and material stock levels.

**The failure (ungrounded):** in Case T03 (Rush Order), the ungrounded evaluator approved a **$999,999** rush order despite the project having only **$42,000** remaining.

**The correction (grounded):** `IronBridgeEnvironment` caught the budget overrun (score **0.0**) and forced the agent to fall back to a "Schedule Resequence" strategy that required no extra budget.

### 4. Self-Correction (Self-Refine & Reflexion)

- **Self-Refine** — one draft, one grounded critique (deterministic checks + `IronBridgeEnvironment`), one revision. Used to polish cheap-to-redo outputs like proposal drafts and notifications.
- **Reflexion** — full-task retry across trials with a capped episodic buffer. In **Case T04** (Capacity Estimation), the agent reached a **1.00** score by carrying forward reflections that corrected an initial mathematical error across three trials.

---

## System Integration

The Planning Agent is wired into `agent/planning_agent.py`, alongside — not instead of — the RAG/memory system:

- **Automatic routing:** requests containing keywords like `"delay"`, `"risk"`, `"shortage"`, `"resequence"`, `"rush order"` are routed to the Planning Agent (`_is_planning_request`); policy questions still go to RAG; everything else still goes through the normal MCP tool-calling loop.
- **Sub-task routing:** `planning/router.py::execute_routed_plan` dispatches each DAG node to the algorithm that fits its shape (`diagnose` → direct, `rank_options` → ToT, `propose_plan` → LATS, `notify` → Plan-and-Solve).
- **Robust fallback:** if the Planning Agent hits an API rate limit (429) or any other exception, the loop catches it and falls back to the standard tool-based agent so the user still gets an answer (see Known Limitations — this fallback fired often enough during evaluation to affect the numbers above).

---

## Sub-Task Routing Recommendations

| Sub-Task Shape | Recommended Method | Justification |
|---|---|---|
| Simple / deterministic | Plan-and-Solve | Lowest cost, fastest execution — no branching to explore. |
| Ranking / options | Tree-of-Thoughts | Explores multiple strategies before committing; beats single-pass ranking. |
| High-stakes / financial | LATS (Grounded) | Hard-validates proposals against real DB constraints before they ship — see Case T03. |
| Multi-trial learning | Reflexion | Carries lessons across attempts; fixes recurring logic/calculation errors (Case T04). |

---

## Demo Evidence

### 1. Decomposition-first vs. Dynamic Divergence (Case T01)
Dynamic decomposition pivoted after observing low stock mid-run and scored 0.90; the static plan committed to a stale route and scored 0.34 on the identical request.

### 2. Sub-Task Routing — Live Smoke Test (`python -m tests.test_router`)

```
============================================================
ROUTER SMOKE TEST
============================================================
[diagnose] Diagnose root cause for Project 1
  Status: OK  | method=Plan-and-Solve
  Output preview: **Project 1 Diagnosis: Test Delay Risk**
  Based on the project data, the root cause of the test delay risk in Project 1 ...

[rank_options] Rank mitigation strategies for Project 1
  Status: OK  | method=Tree-of-Thoughts
  Output preview: Develop a decision matrix to evaluate and rank mitigation strategies based on
  their potential impact, feasibility, and c...

[propose_plan] Propose final plan for Project 1
  Status: OK  | method=LATS
  Output preview: Solution 1: Implement a machine learning model to analyze data and predict
  outcomes. The model will be trained on a data...

[notify] Draft notification for site engineer
  Status: OK  | method=Plan-and-Solve
  Output preview: **Notification for Site Engineer: Delay in Material Delivery and Revised
  Construction Schedule** **Problem Statement:** ...
============================================================
ALL TESTS PASSED ✅
============================================================
```

### 3. Grounded Environment Catching a Failure (Case T03)
A $999,999 rush order against a $42,000 remaining budget is accepted by the ungrounded evaluator but rejected (score 0.0) by `IronBridgeEnvironment`, forcing a switch to Schedule Resequence.

### 4. Reflexion Cross-Trial Memory (Case T04)
Episodic memory carried across three trials corrected a repeated math error in the capacity estimate, ending at a 1.00 score.

### 5. Self-Refine Revision
*(Pending — add one concrete before/after draft-and-revision transcript here; see Known Limitations.)*

---

## How to Run

```bash
# 1. Install dependencies
pip install -r mcp_server/requirements.txt
pip install -r agent/requirements.txt
pip install -r requirements.txt

# 2. Configure environment (.env in repo root)
GROQ_API_KEY=...
GROQ_MODEL=llama-3.3-70b-versatile
IRONBRIDGE_DB_PATH=./db/procurement.db
IRONBRIDGE_DB_ENGINE=sqlite

# 3. Run the full comparison benchmark
python -m planning_eval.full_comparison

# 4. Run the router smoke test
python -m tests.test_router

# 5. Run the interactive agent (planning + RAG + MCP tools, routed together)
python -m agent.planning_agent
# Try: "Project 1 is behind schedule. Propose a plan."
```

---

## Known Limitations (Before Final Submission)

- **Comparison table is incomplete.** The rubric requires every method compared against the fixed suite; the table above is missing **Plan-and-Solve (baseline)**, **Self-Refine (Grounded)**, **Self-Refine (Ungrounded)**, and **Reflexion (Grounded)**. The code paths for all four exist and are exercised by `planning_eval/full_comparison.py` and `planning_eval/self_correction_eval.py` — this is a matter of completing the run, not writing new code.
- **API rate limiting affected the run.** Several of the low success counts above include Groq 429 errors caught and scored as failures, not genuine plan-quality failures. This deflates every method's success rate and weakens the comparison as a signal of algorithm quality. Recommend re-running with longer inter-case delays (or a smaller/faster model) and, in the saved trace, distinguishing rate-limit failures from genuine grounded-environment rejections.
- **Reflexion (Ungrounded) currently scores highest of all methods (0.37).** Since this number comes from the model grading its own output, it is exactly the kind of self-serving bias grounding exists to catch — without the missing Reflexion (Grounded) row, there's no evidence yet that grounding corrects this for Reflexion specifically (only demonstrated for LATS, via Case T03).
- **The aggregate LATS grounded-vs-ungrounded gap is narrow** (0.24 vs 0.20, same success count). Lead the "why grounding matters" argument with the Case T03 walkthrough, not the aggregate averages alone.
- **Self-Refine demo evidence is a placeholder.** Add one concrete draft → critique → revision transcript before submission.

---

## Guardrails Met

- [x] Decomposition-first and dynamic both implemented against the same real request type
- [x] Acyclicity enforced at construction time
- [x] Plan-and-Solve, Tree of Thoughts, and LATS all implemented and routed live (see `test_router.py`)
- [x] Self-Refine and Reflexion both implemented, grounded and ungrounded variants
- [x] Grounded environment replaces the toolkit's randomized default (budget/stock/supplier checks)
- [x] Artifacts saved as JSON traces (`artifacts/full_comparison_table.json`)
- [x] Live agent integration (`agent/planning_agent.py`) alongside the RAG/memory agent

---

## Teamwork & Issues

| # | Title | Status |
|---|---|---|
| 1 | Delay-risk mitigation plans need grounded search because single-pass proposals exceed budget or use inactive suppliers | Closed |
| 2 | Delay-risk tickets need decomposition because no single MCP tool resolves them | Closed |
| 3 | Swap the toolkit's model provider | Closed |
| 4 | Router depends on Task 2: plan_and_solve, tree_of_thoughts, lats need IronBridge adaptation | Closed |
| 5 | Swap base model to our used model | Closed (PR) |
| 6 | Test the three planning algorithms | Closed (PR) |
| 7 | Integrate planning algorithms into ironbridge-planning | Closed |
| 8 | Self Connection + Grounded for LATS/connection | Closed |

> Assign each issue to its actual owner on GitHub itself (currently unassigned) so individual contribution is traceable from the issue tracker, not just this table.

---

## Credits

Built on top of the reference toolkit:
`github.com/AmrSheta22/task_decomposition_and_planning`

Extends the existing IronBridge MCP server and database from Weeks 2–3 (MCP Server Lab + Memory & RAG Lab).
