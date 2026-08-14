import os
import sys
import atexit
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from groq import Groq

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR.parent))

from rag.vector_store import setup_vector_store
from memory.self_rag_check import SelfRAGChecker

load_dotenv()

SYSTEM_PROMPT = (
    "You are the IronBridge procurement assistant. "
    "Answer ONLY using the provided context. "
    "If the context doesn't contain the answer, "
    "say so explicitly rather than guessing."
)

GROQ_MODEL = "llama-3.1-8b-instant"  # same as rest of repo


class NaiveRetriever:
    """Vector-only similarity search retriever — baseline control group."""

    def __init__(self, vector_store: Optional[Any] = None) -> None:
        self.vector_store = vector_store or setup_vector_store()

    def search(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        results = self.vector_store.similarity_search_with_score(query, k=top_k)
        return [
            {
                "text": doc.page_content,
                "metadata": doc.metadata,
                "vector_score": float(score),
            }
            for doc, score in results
        ]

    def close(self) -> None:
        """Explicitly close vector store client to avoid interpreter teardown warnings."""
        if hasattr(self.vector_store, "client") and hasattr(self.vector_store.client, "close"):
            try:
                self.vector_store.client.close()
            except Exception:
                pass


_DEFAULT_RETRIEVER: Optional[NaiveRetriever] = None
_DEFAULT_CHECKER: Optional[SelfRAGChecker] = None
_GROQ_CLIENT: Optional[Groq] = None


def get_groq_client() -> Groq:
    global _GROQ_CLIENT
    if _GROQ_CLIENT is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY environment variable is not set.")
        _GROQ_CLIENT = Groq(api_key=api_key)
    return _GROQ_CLIENT


def get_self_rag_checker() -> SelfRAGChecker:
    global _DEFAULT_CHECKER
    if _DEFAULT_CHECKER is None:
        _DEFAULT_CHECKER = SelfRAGChecker()
    return _DEFAULT_CHECKER


def cleanup_resources() -> None:
    """Cleanup hook registered at exit."""
    global _DEFAULT_RETRIEVER
    if _DEFAULT_RETRIEVER is not None:
        _DEFAULT_RETRIEVER.close()


# Register clean shutdown to suppress Qdrant __del__ warnings
atexit.register(cleanup_resources)


def generate_answer(
    query: str,
    retrieved: List[Dict[str, Any]],
    model: str = GROQ_MODEL,
) -> str:
    if not retrieved:
        return "No relevant policy content was found for this question."

    client = get_groq_client()
    context_blocks = []
    for idx, item in enumerate(retrieved, start=1):
        source = item.get("metadata", {}).get("source", f"Chunk {idx}")
        context_blocks.append(f"[Source: {source}]\n{item['text']}")
    context_str = "\n\n".join(context_blocks)

    prompt = f"Context:\n{context_str}\n\nQuestion: {query}"
    response = client.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content or ""


def naive_rag_answer(
    query: str,
    top_k: int = 3,
    retriever: Optional[NaiveRetriever] = None,
) -> Dict[str, Any]:
    global _DEFAULT_RETRIEVER
    if retriever is None:
        if _DEFAULT_RETRIEVER is None:
            _DEFAULT_RETRIEVER = NaiveRetriever()
        retriever = _DEFAULT_RETRIEVER

    # 1. Retrieve
    retrieved = retriever.search(query, top_k=top_k)

    # 2. Self-RAG relevance check
    checker = get_self_rag_checker()
    relevant_chunks = []
    for c in retrieved:
        check = checker.relevance_check(query, c["text"])
        c["relevance_check"] = {"passed": check.passed, "reason": check.reason}
        if check.passed:
            relevant_chunks.append(c)
        else:
            print(
                f"[self-rag-check] DROPPED chunk (failed relevance): "
                f"{c['text'][:80]!r} -- {check.reason}"
            )

    if not relevant_chunks:
        return {
            "query": query,
            "answer": "No relevant policy content was found for this question.",
            "retrieved_chunks": retrieved,
            "self_rag": {"relevance_passed": False, "support_check": None},
        }

    # 3. Generate
    answer = generate_answer(query, relevant_chunks)

    # 4. Self-RAG support check
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
    test_query = "What are the rules for lifting steel above 50kg?"
    result = naive_rag_answer(test_query)
    print(f"\nQuery: {result['query']}")
    print(f"\nAnswer:\n{result['answer']}")
    print(f"\nTotal chunks used: {len(result['retrieved_chunks'])}")

    # Clean exit
    cleanup_resources()