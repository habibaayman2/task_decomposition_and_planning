"""
rag/test_search.py

Sanity check for the vector store: runs a filtered similarity search,
restricting results to a single source document via a Qdrant metadata
filter. Reuses setup_vector_store() instead of building its own
QdrantClient/path, so there's a single source of truth for how the
store is configured -- no risk of this file and vector_store.py
drifting to different paths again.
"""

from qdrant_client.http import models
from rag.vector_store import setup_vector_store


def test_search():
    vector_store = setup_vector_store()

    query = "What handling requirements apply to Steel materials?"
    target_source = "material_handling_procedures.md"

    print(f"\nQuery: {query}")
    print(f"Filter: metadata.source == {target_source}")

    results = vector_store.similarity_search(
        query,
        k=5,
        filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="metadata.source",
                    match=models.MatchValue(value=target_source),
                )
            ]
        ),
    )

    print(f"\n{len(results)} result(s) returned:")
    all_correct_source = True
    for doc in results:
        actual_source = doc.metadata.get("source")
        matches = actual_source == target_source
        all_correct_source = all_correct_source and matches
        print(f"  source={actual_source} (matches filter: {matches})")
        print(f"    {doc.page_content[:80]}...")

    print(
        f"\n{'PASS' if all_correct_source and results else 'FAIL'}: "
        f"all results came from '{target_source}' only"
    )


if __name__ == "__main__":
    test_search()