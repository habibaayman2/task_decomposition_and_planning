"""
rag/hybrid_search.py

Hybrid retrieval: combines Qdrant vector similarity search with BM25
keyword search over the same policy chunks, then merges both result
sets before generation.

Vector search alone misses exact identifiers (policy numbers, specific
terms) because they don't carry distinctive semantic meaning. BM25
catches exact token overlap that embeddings miss. Running both and
merging catches what either one alone would miss.
"""

import os
from rank_bm25 import BM25Okapi
from groq import Groq
from dotenv import load_dotenv 
from memory.self_rag_check import SelfRAGChecker

from rag.chunking import get_policy_chunks
from rag.vector_store import setup_vector_store
load_dotenv()
GROQ_MODEL = "openai/gpt-oss-20b"


def _tokenize(text: str) -> list[str]:
    return text.lower().split()


class HybridRetriever:
    def __init__(self):
        # Same chunks feed both indexes -- one source of truth, two
        # different ways of searching it.
        self.chunks = get_policy_chunks()
        self.corpus_texts = [c.page_content for c in self.chunks]

        print("Building BM25 index...")
        tokenized_corpus = [_tokenize(t) for t in self.corpus_texts]
        self.bm25 = BM25Okapi(tokenized_corpus)

        print("Loading Qdrant vector store...")
        self.vector_store = setup_vector_store()

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Returns up to top_k chunks ranked by a merged score:
        normalized vector similarity + normalized BM25 score, summed.
        A chunk retrieved by both methods naturally scores higher than
        one retrieved by only one.
        """
        # --- Vector search ---
        vector_hits = self.vector_store.similarity_search_with_score(query, k=len(self.chunks))
        vector_scores = {}
        for doc, score in vector_hits:
            vector_scores[doc.page_content] = score

        # --- BM25 search ---
        tokenized_query = _tokenize(query)
        bm25_scores_raw = self.bm25.get_scores(tokenized_query)
        bm25_scores = {
            self.corpus_texts[i]: bm25_scores_raw[i]
            for i in range(len(self.corpus_texts))
        }

        # --- Normalize each score set to 0-1 so they're comparable ---
        def normalize(score_dict: dict) -> dict:
            values = list(score_dict.values())
            if not values:
                return score_dict
            lo, hi = min(values), max(values)
            if hi == lo:
                return {k: 0.0 for k in score_dict}
            return {k: (v - lo) / (hi - lo) for k, v in score_dict.items()}

        norm_vector = normalize(vector_scores)
        norm_bm25 = normalize(bm25_scores)

        # --- Merge: sum normalized scores across all chunks that appear
        # in either result set ---
        all_texts = set(norm_vector) | set(
            t for t in self.corpus_texts if bm25_scores.get(t, 0) > 0
        )
        merged = []
        for text in all_texts:
            v_score = norm_vector.get(text, 0.0)
            b_score = norm_bm25.get(text, 0.0)
            merged.append({
                "text": text,
                "vector_score": v_score,
                "bm25_score": b_score,
                "combined_score": v_score + b_score,
            })

        merged.sort(key=lambda x: x["combined_score"], reverse=True)
        return merged[:top_k]


def generate_answer(query: str, retrieved_chunks: list[dict]) -> str:
    """Sends retrieved chunks + query to Groq, grounded generation only."""
    context = "\n\n---\n\n".join(c["text"] for c in retrieved_chunks)
    prompt = (
        "Answer the question using ONLY the context below. "
        "If the context doesn't contain the answer, say so explicitly.\n\n"
        f"Context:\n{context}\n\n"
        f"Question: {query}"
    )

    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


def hybrid_rag_answer(query: str, top_k: int = 5) -> dict:
    """Full pipeline: hybrid retrieve, then generate, then verify with
    Person 1's Self-RAG checker before the answer is considered final.
    Both checks are logged with a visible pass/fail, not silently
    trusted."""
    retriever = HybridRetriever()
    retrieved = retriever.search(query, top_k=top_k)

    checker = SelfRAGChecker()

    # 1. هل الـ chunks اللي جبناها فعلاً relevant للسؤال؟
    relevant_chunks = []
    for c in retrieved:
        check = checker.relevance_check(query, c["text"])
        c["relevance_check"] = {"passed": check.passed, "reason": check.reason}
        if check.passed:
            relevant_chunks.append(c)
        else:
            print(f"[self-rag-check] DROPPED chunk (failed relevance): "
                  f"{c['text'][:80]!r} -- {check.reason}")

    # لو كل الـ chunks فشلوا، منقدرش نجاوب من غير مصدر حقيقي
    if not relevant_chunks:
        return {
            "query": query,
            "answer": "No relevant policy content was found for this question.",
            "retrieved_chunks": retrieved,
            "self_rag": {"relevance_passed": False, "support_check": None},
        }

    answer = generate_answer(query, relevant_chunks)

    # 2. هل الإجابة فعلاً مبنية على الـ chunks دي، ولا الموديل اخترع حاجة؟
    combined_content = "\n\n".join(c["text"] for c in relevant_chunks)
    support = checker.support_check(answer, combined_content)
    print(f"[self-rag-check] support_check: passed={support.passed} -- {support.reason}")

    return {
        "query": query,
        "answer": answer,
        "retrieved_chunks": relevant_chunks,
        "self_rag": {
            "relevance_passed": True,
            "support_check": {"passed": support.passed, "reason": support.reason},
        },
    }


if __name__ == "__main__":
    test_query = (
        "For a reservation that would breach minimum stock on Reinforcement "
        "Steel, what handling requirements apply and what does the approval "
        "workflow require?"
    )

    print("=" * 70)
    print("Diagnostic: does a single hybrid search call answer both parts")
    print("of a multi-part question, or does it need a narrower top_k to")
    print("expose the limit? (This motivates Agentic RAG's multi-hop loop.)")
    print("=" * 70)

    for k in (5, 3):
        print(f"\n--- top_k={k} ---")
        result = hybrid_rag_answer(test_query, top_k=k)
        print(f"Answer:\n{result['answer']}")
        print(f"\nGrounded in {len(result['retrieved_chunks'])} chunks:")
        for c in result["retrieved_chunks"]:
            print(f"  - combined={c['combined_score']:.3f} (vector={c['vector_score']:.3f}, bm25={c['bm25_score']:.3f})")
            print(f"    {c['text'][:80]}...")