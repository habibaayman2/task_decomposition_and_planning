import os
from langchain_qdrant import QdrantVectorStore
from langchain_community.embeddings.fastembed import FastEmbedEmbeddings
from rag.chunking import get_policy_chunks
from qdrant_client import QdrantClient
from qdrant_client.http import models
import atexit

COLLECTION_NAME = "ironbridge_policies"

# Singleton cache — shared across all architectures
_vector_store_instance = None
_client_instance = None


def setup_vector_store():
    global _vector_store_instance, _client_instance

    # If already built, reuse the same client + store
    if _vector_store_instance is not None:
        return _vector_store_instance

    # 1. Embeddings
    print("Initializing FastEmbed...")
    embeddings = FastEmbedEmbeddings(model_name="BAAI/bge-small-en-v1.5")

    # 2. Storage path
    path = os.path.join(os.path.dirname(__file__), "qdrant_data")
    _client_instance = QdrantClient(path=path)

    # 3. Check if collection already exists and has data
    collection_exists = _client_instance.collection_exists(COLLECTION_NAME)
    point_count = _client_instance.count(COLLECTION_NAME).count if collection_exists else 0

    if collection_exists and point_count > 0:
        print(f"Collection already exists with {point_count} points -- reusing it.")
        _vector_store_instance = QdrantVectorStore(
            client=_client_instance,
            collection_name=COLLECTION_NAME,
            embedding=embeddings,
        )
        return _vector_store_instance

    # 4. First time: chunk, embed, index
    chunks = get_policy_chunks()
    print(f"Indexing {len(chunks)} chunks into Qdrant (first-time setup)...")

    _vector_store_instance = QdrantVectorStore.from_documents(
        documents=chunks,
        embedding=embeddings,
        path=path,
        collection_name=COLLECTION_NAME,
        force_recreate=True,
    )

    # 5. Metadata payload index for filtering
    print("Creating payload index on metadata.source...")
    _vector_store_instance.client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="metadata.source",
        field_schema="keyword",
    )

    print("✅ Qdrant Vector Store is ready, indexed, and payload-indexed!")
    return _vector_store_instance


if __name__ == "__main__":
    setup_vector_store()
  

def _cleanup_qdrant():
    global _client_instance
    if _client_instance is not None:
        try:
            _client_instance.close()
        except Exception:
            pass

atexit.register(_cleanup_qdrant)