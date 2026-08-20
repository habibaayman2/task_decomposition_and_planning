from rag.vector_store import setup_vector_store, add_document, remove_document, update_document
import rag.vector_store as vs

TEST_FILE = "equipment_operation_safety_rules.md"

print("=== Step 1: Build/load the index ===")
setup_vector_store()
before = vs._client_instance.count(vs.COLLECTION_NAME).count
print(f"Total points BEFORE: {before}")

print(f"\n=== Step 2: Remove {TEST_FILE} ===")
remove_document(TEST_FILE)
after_remove = vs._client_instance.count(vs.COLLECTION_NAME).count
print(f"Total points AFTER remove: {after_remove}")
assert after_remove < before, "❌ FAILED: point count didn't decrease after remove!"
print("✅ Removal confirmed: point count decreased.")

print(f"\n=== Step 3: Re-add {TEST_FILE} ===")
add_document(TEST_FILE)
after_add = vs._client_instance.count(vs.COLLECTION_NAME).count
print(f"Total points AFTER re-add: {after_add}")
assert after_add >= before, "❌ FAILED: point count didn't recover after re-add!"
print("✅ Re-add confirmed: point count recovered.")

print(f"\n=== Step 4: update_document() convenience wrapper ===")
update_document(TEST_FILE)
after_update = vs._client_instance.count(vs.COLLECTION_NAME).count
print(f"Total points AFTER update_document: {after_update}")
assert after_update == after_add, "❌ FAILED: update_document changed total count unexpectedly!"
print("✅ update_document confirmed: remove+add cycle works as one call.")

print("\n🎉 ALL CHECKS PASSED — index invalidation bug (Issue B2) is fixed.")