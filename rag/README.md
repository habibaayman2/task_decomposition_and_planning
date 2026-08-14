# rag/ — Retrieval Architecture (Person 3)

## What's in here

| File | Purpose |
|------|---------|
| `sync_policies.py` | Copies the policy markdown files from `mcp_server/` (the authoritative source, also exposed as MCP resources) into `rag/policies/`. Run this whenever a source policy changes instead of copying files manually. |
| `policies/` | Snapshot of the RAG corpus containing the IronBridge policy documents. Files are split by `##` section headers using LangChain's `MarkdownHeaderTextSplitter`, with `RecursiveCharacterTextSplitter` as a fallback for oversized sections. |
| `chunking.py` | `get_policy_chunks()` reads every policy file, splits it into section-based chunks, and attaches metadata such as source filename and section title. |
| `vector_store.py` | `setup_vector_store()` — embeds every chunk with FastEmbed (`BAAI/bge-small-en-v1.5`, local, no API key required), stores them in a local Qdrant collection (`ironbridge_policies`), and attempts an explicit payload index on `metadata.source`. **Known limitation:** Qdrant's local/embedded mode does not actually build payload indexes (`create_payload_index` is a documented no-op there, server mode only) -- confirmed via `test_search.py`, which shows filtering *during* search still works correctly (all 5 results match the filter) even without index acceleration. Given the current corpus size (11 chunks), the missing index has no measurable performance cost; a production deployment would need Qdrant server mode for the index to actually accelerate filtering. |
| `test_search.py` | Demonstrates filtered similarity search using Qdrant metadata filters to restrict retrieval to selected source documents. |

---

## Design Decisions

### FastEmbed instead of OpenAI Embeddings
- Runs completely locally.
- No API key required.
- No per-query cost.
- More than sufficient for the current small policy corpus.

### Section-based chunking instead of fixed-size chunking
- IronBridge policy documents are short.
- Splitting by Markdown headers (`##`) preserves logical sections.
- Each chunk receives meaningful metadata (section title + source file).

### `rag/policies/` is a generated snapshot
- `mcp_server/*.md` remains the single source of truth.
- `sync_policies.py` copies the files automatically so the RAG corpus always matches the MCP resources.

### Local Qdrant storage
- The local database (`rag/local_qdrant/`) is regenerated when needed.
- It is excluded from Git using `.gitignore`.

- - **Payload index limitation (local Qdrant mode).** `create_payload_index()`
  is called and completes without error, but Qdrant's local/embedded
  storage mode does not support real payload indexes -- this is
  documented upstream behavior, not a bug in this code. Metadata
  filtering during search (`filter=...`) still works correctly and is
  verified in `test_search.py`; what's missing is index-accelerated
  filtering, which only matters at a scale far beyond the current
  11-chunk corpus.

---

## Setup

```bash
pip install -r rag/requirements.txt

python rag/sync_policies.py
python -m rag.vector_store
python rag/test_search.py
```

---

## Status / Next Steps

- [x] Bug fixes (#5 and #6)
- [x] Section-aware chunking
- [x] Local Qdrant vector database
- [x] Metadata payload indexing
- [x] Hybrid Search (Vector + BM25)
- [x] Agentic RAG
- [ ] Self-RAG verification (integration with Person 1)
- [ ] `retrieval_eval/` benchmark and comparison table
# Naive RAG (Retrieval-Augmented Generation)

A clean, minimalist implementation of Naive RAG designed for quick context retrieval and generation using FastEmbed for local embeddings and vector indexing.

---

## Architecture / Pipeline Flow
[ Documents ] ──> [ Chunking ] ──> [ Embedding ] ──> [ Vector Store ]
│
[ User Query ] ──> [ Embedding ] ──> [ Similarity Search ] ┘
│
[ Context + Query ]
│
[ LLM ] ──> [ Final Answer ]


---

## Features

* **FastEmbed Acceleration:** Local, high-performance ONNX embeddings with automatic model caching.
* **Vector Index Persistence:** Reuses existing collection indices to skip redundant re-embedding.
* **Self-RAG Grounding Verification:** Includes automated token overlap checks to verify answer accuracy against retrieved context.
* **Strict Context Grounding:** Formats prompts to ensure responses strictly adhere to retrieved facts.

---

## Benchmark Results

Below are the execution logs and Self-RAG verification results from a sample query run:

### Execution Output

```text
Initializing FastEmbed...
Collection already exists with 11 points -- reusing it, skipping re-embedding.
[self-rag-check] support_check: passed=True -- answer/content token overlap=0.81 (threshold 0.2); answer appears grounded in content

Query: What are the rules for lifting steel above 50kg?

Answer:
According to the context, materials in the Steel category require mechanical lifting equipment above 50kg per unit — never manual lift.

Total chunks used: 3
