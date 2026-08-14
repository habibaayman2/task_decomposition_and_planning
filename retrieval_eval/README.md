# Retrieval Evaluation

Retrieval architecture evaluation for the **IronBridge Procurement Assistant**.

## What This Solves

IronBridge staff ask the assistant questions that span multiple policy documents and require both:

- Exact identifier retrieval, such as **"Policy #2"**
- Multi-part reasoning, such as **"handling requirements AND approval workflow"**

This evaluation measures which retrieval architecture answers these questions correctly and at what cost, so the choice of the shipped architecture is justified by measurable results rather than intuition.

---

## Structure

```text
retrieval_eval/
├── test_questions.py   # Fixed question set (6 questions, ≥1 per architecture)
├── evaluate.py         # Runs all 3 architectures and prints comparison table
├── results.json        # Generated artifact (not committed)
└── README.md           # This file
```

---

## The Test Questions

The evaluation uses a **fixed set of 6 questions**. The questions are designed to favor different retrieval architectures based on the type of information they require.

| ID | Favored Architecture | Why |
|---|---|---|
| `naive_001` / `naive_002` | Naive RAG | General semantic questions where embeddings alone should be sufficient. |
| `hybrid_001` / `hybrid_002` | Hybrid RAG | Exact identifiers such as `"Policy #2"` and `"50kg"` that BM25 can match more reliably than pure vector search. |
| `agentic_001` / `agentic_002` | Agentic RAG | Multi-part questions spanning two or more policy documents and requiring an additional retrieval hop. |

> **Important:** The question set is fixed. Changing the questions between runs invalidates the comparison because the architectures would no longer be evaluated on the same workload.

---

## Running the Evaluation

Run the evaluation from the repository root:

```bash
python -m retrieval_eval.evaluate
```

### Requirements

Before running the evaluation:

1. `GROQ_API_KEY` must be exported.
2. The vector store must already be built.
3. The existing Qdrant collection must be available.

The evaluation reuses the existing vector store through:

```text
rag/vector_store.py
```

All three architectures use **Groq** for generation, ensuring that the comparison focuses on retrieval architecture rather than different generation models.

---

## Output

The evaluation produces:

- A comparison table printed to the terminal.
- A `results.json` file containing the per-question evaluation results.

`results.json` is a generated artifact and should **not be committed** to the repository.

---

## Evaluation Results

The fixed 6-question evaluation produced the following results:

| Architecture | Accuracy | Avg In Tokens | Avg Out Tokens | Avg Latency |
|---|---:|---:|---:|---:|
| Naive RAG | 1/6 (17%) | 261 | 90 | 0.37s |
| Hybrid RAG | 6/6 (100%) | 511 | 56 | 1.45s |
| Agentic RAG | 6/6 (100%) | 466 | 33 | 3.68s |

---

## Selected Architecture

### Hybrid Search as the Default

**Hybrid RAG is the default retrieval architecture.**

The evaluation shows that Hybrid RAG achieved:

- **6/6 correct answers (100% accuracy)**
- **1.45s average latency**
- Reliable retrieval of exact identifiers
- Better performance than Naive RAG on identifier-heavy questions

Hybrid RAG combines semantic vector search with **BM25 keyword search**. This is particularly important for exact identifiers such as:

```text
Policy #2
50kg
```

Dense embeddings are optimized for semantic similarity and can fail to distinguish exact token-level matches. BM25 complements vector search by explicitly rewarding exact keyword matches.

As a result, Hybrid RAG achieved the same accuracy as Agentic RAG while requiring substantially less latency.

---

## Why Agentic RAG Exists

Although Agentic RAG is not the default, it remains available as a **routed fallback** for complex questions.

Agentic RAG is intended for questions that are explicitly multi-part or decomposition-shaped, especially when the first retrieval hop does not provide enough context for all required sub-topics.

For example, a question may require information about both:

```text
Handling requirements
        +
Approval workflow
```

from different policy documents.

In such cases, the agent can decompose the question, perform additional retrieval steps, and combine the retrieved context before generating the final answer.

However, the evaluation showed that on the fixed test set:

- Hybrid RAG already retrieved sufficient context for the multi-part questions.
- Agentic RAG achieved the same **6/6 (100%) accuracy**.
- Agentic RAG had a significantly higher average latency: **3.68s vs 1.45s**.

Therefore, using Agentic RAG for every request would add unnecessary latency without improving accuracy on the tested workload.

---

## Final Decision

Based on the evaluation results:

```text
                    ┌─────────────────────┐
                    │     User Query      │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │    Hybrid RAG       │
                    │      Default        │
                    └──────────┬──────────┘
                               │
                    ┌──────────┴──────────┐
                    │                     │
              Sufficient             Complex /
               context?            multi-part query
                    │                     │
                   Yes                    ▼
                    │             ┌─────────────────┐
                    │             │   Agentic RAG   │
                    │             │    Fallback     │
                    │             └─────────────────┘
                    │
                    ▼
                 Answer
```

### Shipping Strategy

**Hybrid Search is shipped as the default retrieval strategy, with Agentic RAG available as a routed fallback for explicitly complex multi-part questions.**

This decision is supported by the measured results rather than intuition:

| Decision | Evidence |
|---|---|
| Use Hybrid as default | 100% accuracy with 1.45s average latency |
| Keep Agentic RAG | Useful fallback for decomposition-shaped queries |
| Do not use Agentic for every query | 3.68s average latency with no accuracy improvement on this test set |
| Do not use Naive RAG as default | Only 17% accuracy on the fixed evaluation set |

The evaluation therefore supports a **hybrid-by-default, agentic-when-needed** retrieval strategy for the IronBridge Procurement Assistant.
### Graph RAG Bonus -- Retrieval Architecture Comparison

| Architecture | Accuracy | Avg Tokens/Query | Avg Latency/Query |
|:---|:---:|:---:|:---:|
| Naive RAG | 2/6 (33%) | 22 | 0.040500s |
| Hybrid Search (Vector + BM25) | 4/6 (66%) | 27 | 0.080855s |
| Agentic RAG (Multi-hop) | 5/6 (83%) | 59 | 0.310673s |
| Graph RAG (Bonus) | 6/6 (100%) | 124 | 0.000194s |
