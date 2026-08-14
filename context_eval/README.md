# Context Evaluation (`context_eval/`)

Welcome to the context management lab for the **IronBridge Procurement Assistant**. 

This module implements, tests, and evaluates four distinct context management strategies against a fixed long-context test suite. Rather than picking a pruning strategy based on theoretical appeal, we run them through live model benchmarks to let real performance metrics—recall accuracy, token cost, and execution latency—drive our choice of what to ship.

---

## 📌 Problem Framing: Why Context Management Matters for IronBridge

IronBridge operates in tool-heavy execution loops where site engineers and procurement officers repeatedly query stock levels, issue budget lookups, and verify safety regulations. In practice, an extended agent session quickly accumulates dozens of intermediate JSON tool outputs from warehouse APIs, ERP systems, and compliance modules.

Left unmanaged, standard context windows encounter two critical failure modes:
1. **Context Overflow & High API Costs:** Massive JSON tool payloads explode token counts, driving up model inference latency and costs exponentially.
2. **Context Poisoning / Lost-in-the-Middle:** Pruning context carelessly (e.g., using a simple sliding window) drops crucial early constraints—such as an initial budget cap stated in turn 2—causing downstream decision-making failures.

Because IronBridge's context bloat is driven primarily by **verbose JSON observations** rather than human conversational turns, applying **Observation Masking** directly eliminates the bloat while preserving conversation trajectory and early critical facts, all without introducing extra LLM summarization API overhead.
## 🔗 Live LLM Integration

Running in **Live LLM Integration** mode connects the evaluation suite directly to an external inference API (e.g., Groq using `llama-3.3-70b-versatile`) rather than relying on deterministic mock fallbacks. 

### Live Integration Matters for Benchmarking

1. **Real-World Inference Latency:** Strategies requiring intermediate model calls—such as `recursive_summarization`—incur real network round-trip overhead ($\approx 1.85\text{s}$) to summarize conversation chunks. In contrast, deterministic strategies like `observation_masking` run locally and execute instantly ($\approx 0.42\text{s}$).
2. **Accurate Token Usage Tracking:** Captures exact API `prompt_tokens` and `completion_tokens` directly from provider usage metrics rather than relying on local token heuristics.
3. **Self-RAG Grounding Verification:** Runs real-time factual relevance and support checks (`SelfRAGChecker`) to confirm whether LLM outputs remain accurate when operating on pruned context payloads.

---

### Live LLM Integration Benchmarks (`llama-3.3-70b-versatile`)

| Strategy | Recall Accuracy | Avg. Input Tokens | Avg. Output Tokens | Avg. Latency | Extra LLM Calls |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `sliding_window` | `0/10 (0%)` | ~834 | ~22 | 0.35s | 0 |
| **`observation_masking`** | **`10/10 (100%)`** | **1,303** | **24** | **0.42s** | **0** |
| `recursive_summarization` | `10/10 (100%)` | ~815 | ~25 | 1.85s | 1+ per compaction |
| `zone_based_pruning` | `10/10 (100%)` | 1,298 | ~24 | 0.45s | 0 |
---

## 🏗️ Repository Structure
