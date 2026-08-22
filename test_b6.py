from ib_platform.backend.routes.rag_docs import FileBackend

b = FileBackend()
result = b.add_doc('test-doc-123', 'test_policy.md', b'This is a test equipment manual about hydraulic failure.')

docs_file = b.docs_dir / result["filename"]
policies_file = b.policies_dir / result["filename"]

print("File in docs:", docs_file.exists())
print("File in policies:", policies_file.exists())

# Clean up test files
if docs_file.exists():
    docs_file.unlink()
if policies_file.exists():
    policies_file.unlink()
print("Cleaned up test files")