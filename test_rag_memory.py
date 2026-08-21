from qdrant_client import QdrantClient
from qdrant_client.http import models
from langchain_qdrant import QdrantVectorStore
from langchain_community.embeddings.fastembed import FastEmbedEmbeddings
from rag.chunking import get_policy_chunks, get_chunks_for_file

COLLECTION_NAME = "test_ironbridge_policies"

print("Initializing FastEmbed...")
embeddings = FastEmbedEmbeddings(model_name="BAAI/bge-small-en-v1.5")

# Qdrant في الذاكرة بس -- مفيش ملفات على القرص خالص
client = QdrantClient(":memory:")

chunks = get_policy_chunks()
print(f"Indexing {len(chunks)} chunks...")

# 1. Create the collection manually first
sample_vector = embeddings.embed_query("test")
client.create_collection(
    collection_name=COLLECTION_NAME,
    vectors_config=models.VectorParams(size=len(sample_vector), distance=models.Distance.COSINE),
)

# 2. Build the store around the existing client + collection
store = QdrantVectorStore(
    client=client,
    collection_name=COLLECTION_NAME,
    embedding=embeddings,
)

# 3. Add the initial chunks
store.add_documents(chunks)

before = client.count(COLLECTION_NAME).count
print(f"\nTotal points BEFORE: {before}")

TEST_FILE = "equipment_operation_safety_rules.md"

# --- REMOVE ---
client.delete(
    collection_name=COLLECTION_NAME,
    points_selector=models.FilterSelector(
        filter=models.Filter(must=[
            models.FieldCondition(key="metadata.source", match=models.MatchValue(value=TEST_FILE))
        ])
    ),
)
after_remove = client.count(COLLECTION_NAME).count
print(f"Total points AFTER remove: {after_remove}")
assert after_remove < before, "FAILED: remove didn't reduce count"
print("✅ remove_document logic works")

# --- ADD ---
new_chunks = get_chunks_for_file(TEST_FILE)
store.add_documents(new_chunks)
after_add = client.count(COLLECTION_NAME).count
print(f"Total points AFTER re-add: {after_add}")
assert after_add >= before, "FAILED: add didn't restore count"
print("✅ add_document logic works")

print("\n🎉 Core add/remove logic verified (in-memory).")