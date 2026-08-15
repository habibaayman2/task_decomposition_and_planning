# IronBridge Planning Agent — Task Decomposition & Planning Lab

> **Agent:** Delay-Response Planning Agent  
> **Company:** IronBridge Construction  
> **Problem:** Multi-step construction delay mitigation with real branching, real cost of wrong plans, and real database constraints.  
> **Sits next to:** The Memory & RAG Agent (Week 3) — does not replace or duplicate it.

---

## Problem Framing

IronBridge is a construction company managing multiple projects involving materials, contractors, equipment, and deadlines. A major operational pain point is **selecting the optimal response when a delay hits**.

A project may face delays from:
- Material shortages (e.g., rebar delivery 9 days late)
- Contractor availability
- Equipment breakdowns
- Budget constraints
- Weather conditions

**Why a single tool call is not enough:**  
When a site engineer reports "Project 4 is at risk," the assistant cannot know whether the cause is a material shortage, equipment failure, or budget overrun until it **queries the database**. A single lookup returns raw data; deciding which mitigation strategy fits the **remaining budget**, **available stock**, and **equipment status** requires a multi-step plan with branching.

**Why a wrong plan costs something:**  
Recommending a rush order when the remaining budget is $0, or proposing a supplier switch when the alternate supplier is already under maintenance, wastes procurement time and extends the delay. The planning agent must **validate every proposal against live DB constraints** before shipping it.

**Why decomposition-first vs. dynamic matters:**  
- **Decomposition-first** generates the full DAG upfront (diagnose → rank → propose → notify). It is fast and deterministic when the problem shape is known in advance.  
- **Dynamic decomposition** decides the next step only after observing the previous result. When the diagnosis reveals "Project 4 does not exist in the database," a fixed plan would still execute `rank_options` against generic strategies; dynamic decomposition routes around the irrelevant step.

**Why search (ToT / LATS) matters:**  
The "rank options" sub-task genuinely benefits from comparing multiple orderings before committing. The "propose plan" sub-task must survive a real budget check; a wrong proposal is expensive to unwind by phone. Plan-and-Solve is sufficient only for simple text synthesis (e.g., the final notification).

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
│   ├── reflexion.py               # Multi-trial with episodic memory
│   ├── environment.py             # IronBridgeEnvironment (real DB checks)
│   └── router.py                  # Routes sub-tasks to the right algorithm
├── models.py                      # Plan, Task, Thought, EnvironmentFeedback
├── cli.py                         # CLI entry point for all modes
└── model_provider.py              # Groq / deterministic fallback LLM

planning_eval/
├── decomposition_eval.py          # Decomposition-first vs. dynamic benchmark
└── (planning_eval.py — WIP)       # Full PS/ToT/LATS/Reflexion benchmark

tests/
├── test_toolkit_base.py           # Toolkit-derived tests (DAG, cycles, Reflexion, LATS)
├── test_eval.py                   # Algorithm benchmark + unit tests
└── test_decomposition.py          # IronBridge-specific decomposition tests

artifacts/
├── decomposition_comparison.json  # Traces backing the table below
├── algorithm_comparison.json      # Traces for PS/ToT/LATS
└── unit_test_results.json         # Unit test artifacts
```

---

## Comparison Tables

### Table 1: Top-Level Decomposition (3 real cases)

| Method | Task Success | Avg LLM Calls | Avg Tokens | Avg Latency | Est. Cost/Run |
|--------|:------------:|:-------------:|:----------:|:-----------:|:-------------:|
| **Decomposition-first** | 3/3 (baseline) | 1.33 | 622.7 | 5.435s | ~$0.01 |
| **Dynamic decomposition** | 3/3 (diverges on CASE_DIVERGENCE) | 7.0 | 2393.0 | 74.746s | ~$0.04 |

**Why dynamic ships as default:**  
On `CASE_DIVERGENCE`, decomposition-first committed to a 5-node plan (diagnose → gather_info → rank_options → propose_plan → notify) before any real data existed. The dynamic method observed that Project 4 was not found in the database and routed around `rank_options`, stopping after 7 adaptive steps instead of executing a stale plan. Dynamic costs more tokens but avoids blind execution when the problem shape is unknown.

**Decomposition-first is kept** for fully mechanical sub-tasks with no real branching (e.g., the final `notify` step when the plan is already validated).

---

### Table 2: Planning the Sub-Tasks (10 real request cases)

| Method | Task Success | Avg LLM Calls | Avg Latency | Notes |
|--------|:------------:|:-------------:|:-----------:|-------|
| **Plan-and-Solve** | 6/10 (60%) | 1.0 | 3.8s | Fast, fails on budget-constrained or ambiguous cases |
| **Tree-of-Thoughts** | 6/10 (60%) | 6.4 | 54.3s | Better for ranking, but batch evaluation unstable with llama-3.3-70b |
| **LATS (Grounded)** | **9/10 (90%)** | **3.7** | **45.2s** | Best success rate; environment catches over-budget proposals |

**Per-sub-task routing decision:**

| Sub-Task | Algorithm | Justification |
|----------|-----------|---------------|
| `diagnose` | Direct executor | Deterministic DB lookups; no LLM needed |
| `rank_options` | **Tree-of-Thoughts** | Needs to compare multiple strategies before committing; small extra cost is worth paying |
| `propose_plan` | **LATS (Grounded)** | Wrong plan is expensive to unwind; MCTS + real budget check justifies the cost |
| `notify` | **Plan-and-Solve** | Simple text synthesis; one call is sufficient |

**Why LATS beats PS on `propose_plan`:**  
Task 5 (rush order $999,999) and Task 3 (rush order within budget) both require a real budget check. LATS's `IronBridgeEnvironment` evaluates against `db.get_project().RemainingBudget`; proposals that exceed the budget are rejected with a grounded score, while PS blindly generates text. LATS achieves 90% success vs. 60% for PS.

**Why ToT is unstable:**  
The Groq model (llama-3.3-70b-versatile) occasionally malforms the closing XML tag in batch function calls (`<function>` instead of `</function>`). Retry logic mitigates this but increases call count. For production stability, `batch_evaluate=False` or a different model is recommended.

---

### Table 3: Self-Correction (unit tests + benchmark)

| Method | Scope | Grounded? | Use Case |
|--------|-------|-----------|----------|
| **Self-Refine** | Single draft → critique → revision | Yes (deterministic checks: length, structure, goal-term coverage) | Cheap sub-task outputs (e.g., notification text) |
| **Reflexion** | Full task retry across trials | Yes (environment feedback carried across trials) | Sub-tasks where single retry is insufficient; episodic memory prevents repeating the same mistake |

**Grounded vs. Ungrounded:**  
The toolkit's default `Environment` returns randomized scores (`random.betavariate`). `IronBridgeEnvironment` replaces this with real checks:
- Budget validation against `db.get_project(project_id).RemainingBudget`
- Stock validation against `db.find_materials()`
- Equipment validation against `db.equipment_status()`

A proposal mentioning a cost that exceeds the remaining budget is **caught by the grounded environment** and scored down; the ungrounded default would have accepted it 70% of the time.

---

## Setup

```bash
# 1. Install dependencies
pip install -r mcp_server/requirements.txt
pip install -r agent/requirements.txt
pip install -r planning/requirements.txt

# 2. Configure environment (.env in repo root)
GROQ_API_KEY=...
GROQ_MODEL=llama-3.3-70b-versatile   # or llama-3.1-8b-instant for faster runs
IRONBRIDGE_DB_PATH=./db/procurement.db
IRONBRIDGE_DB_ENGINE=sqlite

# 3. Run the decomposition benchmark
python -m planning_eval.decomposition_eval

# 4. Run the algorithm benchmark + unit tests
python -m tests.test_eval

# 5. Run the CLI
python -m planning.cli "Project 1 delay risk" --mode dag
python -m planning.cli "Project 1 delay risk" --mode dynamic
python -m planning.cli "Estimate capacity" --mode ps
python -m planning.cli "Propose launch strategy" --mode tot --depth 2 --beam-width 2
python -m planning.cli "Create security checklist" --mode reflexion --max-trials 3
python -m planning.cli "Create security checklist" --mode lats --iterations 2 --n-actions 2
```

---

## Demo Evidence

### 1. Decomposition-first vs. Dynamic Divergence

```
[decomposition-first] committed plan (generated before any real data):
    - diagnose -> gather_info -> rank_options -> propose_plan -> notify
    -> executed all 5 nodes regardless of what diagnose() returned.

[dynamic decomposition] steps actually taken:
    0. [diagnose]
    1. [Investigate why Project 4 is not found in database]
    2. [Verify the project ID and database query]
    ...
    -> took 7 step(s) vs. decomposition-first's fixed 5-node plan:
       the dynamic method reacted to the real diagnosis.
```

### 2. Sub-Task Routing

```python
# planning/router.py
if task_id == "diagnose":
    return DEFAULT_EXECUTORS["diagnose"](...)          # Direct DB call
elif task_id == "rank_options":
    return tree_of_thoughts(...)                       # Compare strategies
elif task_id == "propose_plan":
    return lats(..., environment=IronBridgeEnvironment())  # Validate budget
elif task_id == "notify":
    return plan_and_solve(...)                         # Simple synthesis
```

### 3. Self-Refine Revision

The `reflect_and_refine` function runs deterministic checks (word count, structure, goal-term coverage) and invokes an independent critic. If the draft is under 80 words or lacks structure, it is revised.

### 4. Reflexion Cross-Trial Memory

```python
# Reflexion carries a bounded episodic buffer:
recalled = "\n".join(memory[-memory_size:])  # verbal reflections from failed trials
# The next trial receives these as context, preventing repeated mistakes.
```

### 5. Grounded Environment Catching a Failure

```python
# IronBridgeEnvironment.evaluate() checks:
if mentioned_cost > project["RemainingBudget"]:
    details.append(f"FAIL: Proposed cost exceeds remaining budget.")
    score -= 0.4
```

A proposal for a $999,999 rush order when the remaining budget is $50,000 receives `success=False` and `score=0.1`. The ungrounded `Environment` would have returned `success=True` ~70% of the time.

---

## Teamwork & Issues

| Issue | Rationale | Owner | Status |
|-------|-----------|-------|--------|
| #X — Merge Salma's planning algorithms into ironbridge-planning | `ironbridge-planning` had generic toolkit code; needed PS/ToT/LATS wired to IronBridge DB | @salmawaly54 | Closed by commits 88c37e9, 86eb0c1 |
| #Y — Grounded environment for LATS/Reflexion | Toolkit's randomized default must be replaced with real DB checks for grading credit | Person 3 | In Progress |
| #Z — Full comparison table & evaluation harness | Need fixed test suite + traces for every method against every applicable case | All | In Progress |

---

## Guardrails Met

- [x] Decomposition-first and dynamic both implemented against the same real request type
- [x] Acyclicity enforced at construction time (`test_cycle_is_rejected`)
- [x] Plan-and-Solve, Tree of Thoughts, and LATS all implemented
- [x] Sub-tasks routed to the algorithm that fits their shape
- [x] Self-Refine and Reflexion both implemented
- [x] Grounded environment replaces toolkit's randomized default (budget/stock/equipment checks)
- [x] Comparison table covers all required methods with real numbers
- [x] Artifacts saved as JSON traces
- [ ] Demo transcript/video (pending final recording)
- [ ] README comparison table embedded with numbers driving per-sub-task choices

---

## Credits

Built on top of the reference toolkit:  
`github.com/AmrSheta22/task_decomposition_and_planning`

Extends the existing IronBridge MCP server and database from Weeks 2–3 (MCP Server Lab + Memory & RAG Lab).
