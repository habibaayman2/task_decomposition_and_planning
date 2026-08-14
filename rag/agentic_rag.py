"""
rag/agentic_rag.py

Agentic RAG: instead of one fixed retrieval call, the model itself
decides -- after seeing what's been retrieved so far -- whether it has
enough to answer, or whether it needs another retrieval round with a
different, more targeted query.

Motivation (see the diagnostic run at the bottom of hybrid_search.py):
a single hybrid search call on a multi-part question (handling
requirements AND approval workflow) either buries one part under the
other, or -- at a narrower top_k -- the model explicitly says it wasn't
given enough context on the second part. This loop lets the model ask
a second, more specific question instead of guessing or giving up.
"""

import os
from dotenv import load_dotenv
from groq import Groq
from memory.self_rag_check import SelfRAGChecker

from rag.hybrid_search import HybridRetriever

load_dotenv()

GROQ_MODEL = "llama-3.1-8b-instant"
MAX_HOPS = 3  # hard cap so the model can't loop forever


DECISION_PROMPT = """You are answering a question using retrieved policy
documents. Decide whether you have enough information to answer fully,
or whether you need to retrieve more.

Question: {question}

Retrieved so far:
{retrieved_so_far}

Respond in EXACTLY this format, nothing else:
DECISION: <ANSWER or RETRIEVE>
QUERY: <if RETRIEVE, a specific search query for the missing piece. if ANSWER, leave blank>
"""

FINAL_ANSWER_PROMPT = """Answer the question using ONLY the context below.
If parts of the question aren't covered by the context, say so explicitly.

Context:
{context}

Question: {question}
"""


def _format_retrieved(chunks: list[dict]) -> str:
    if not chunks:
        return "(nothing retrieved yet)"
    return "\n\n".join(f"- {c['text']}" for c in chunks)


def _parse_decision(raw: str) -> dict:
    decision = "ANSWER"
    query = ""
    for line in raw.strip().splitlines():
        if line.upper().startswith("DECISION:"):
            decision = line.split(":", 1)[1].strip().upper()
        elif line.upper().startswith("QUERY:"):
            query = line.split(":", 1)[1].strip()
    return {"decision": decision, "query": query}


def agentic_rag_answer(query: str, top_k_per_hop: int = 4) -> dict:
    """
    Runs the retrieve-observe-decide loop. Returns the final answer plus
    a full trace of every hop, so a grader (or the Self-RAG checker) can
    see exactly what was retrieved and why at each step.
    """
    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    retriever = HybridRetriever()

    all_retrieved = []
    trace = []

    for hop in range(1, MAX_HOPS + 1):
        decision_prompt = DECISION_PROMPT.format(
            question=query,
            retrieved_so_far=_format_retrieved(all_retrieved),
        )
        decision_response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": decision_prompt}],
        )
        decision_raw = decision_response.choices[0].message.content
        parsed = _parse_decision(decision_raw)

        trace.append({
            "hop": hop,
            "decision": parsed["decision"],
            "query": parsed["query"],
        })

        if parsed["decision"] == "ANSWER":
            break

        previous_queries = [step["query"] for step in trace[:-1]]
        if parsed["query"] in previous_queries:
            break

        hop_results = retriever.search(parsed["query"], top_k=top_k_per_hop)
        # avoid re-adding chunks already retrieved in an earlier hop
        seen_texts = {c["text"] for c in all_retrieved}
        new_results = [c for c in hop_results if c["text"] not in seen_texts]
        all_retrieved.extend(new_results)

    checker = SelfRAGChecker()

    # نفلتر كل الـ chunks اللي اتجمعوا عبر الـ hops كلها بنفس الـ
    # relevance check، قبل ما ندّيهم للموديل يجاوب بيهم
    relevant_chunks = []
    for c in all_retrieved:
        check = checker.relevance_check(query, c["text"])
        c["relevance_check"] = {"passed": check.passed, "reason": check.reason}
        if check.passed:
            relevant_chunks.append(c)
        else:
            print(f"[self-rag-check] DROPPED chunk (failed relevance): "
                  f"{c['text'][:80]!r} -- {check.reason}")

    final_prompt = FINAL_ANSWER_PROMPT.format(
        context=_format_retrieved(relevant_chunks),
        question=query,
    )
    final_response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": final_prompt}],
    )
    answer = final_response.choices[0].message.content

    # هل الإجابة النهائية فعلاً متجذرة في اللي جمعناه عبر كل الـ hops؟
    combined_content = "\n\n".join(c["text"] for c in relevant_chunks)
    support = checker.support_check(answer, combined_content) if relevant_chunks else None
    if support:
        print(f"[self-rag-check] support_check: passed={support.passed} -- {support.reason}")

    return {
        "query": query,
        "answer": answer,
        "retrieved_chunks": relevant_chunks,
        "trace": trace,
        "hops_used": len(trace),
        "self_rag": {
            "support_check": {"passed": support.passed, "reason": support.reason} if support else None,
        },
    }


if __name__ == "__main__":
    test_query = (
        "For a reservation that would breach minimum stock on Reinforcement "
        "Steel, what handling requirements apply and what does the approval "
        "workflow require?"
    )

    result = agentic_rag_answer(test_query)

    print(f"Query: {result['query']}")
    print(f"\nHops used: {result['hops_used']}")
    print("\n--- Trace ---")
    for step in result["trace"]:
        print(f"  Hop {step['hop']}: {step['decision']}" + (f" -> \"{step['query']}\"" if step["query"] else ""))

    print(f"\n--- Final answer ---\n{result['answer']}")
    print(f"\nTotal chunks retrieved across all hops: {len(result['retrieved_chunks'])}")