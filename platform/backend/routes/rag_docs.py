

from __future__ import annotations

from pathlib import Path
import sys
import json
import hashlib
import shutil
from typing import Any, Dict, List, Optional, Protocol
from datetime import datetime
from abc import ABC, abstractmethod

# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------
_current_file = Path(__file__).resolve()
BACKEND_DIR = _current_file.parent.parent
REPO_ROOT = next(
    (p for p in [_current_file] + list(_current_file.parents) if (p / "mcp_server").exists()),
    BACKEND_DIR.parent
)

for path_entry in (str(REPO_ROOT), str(BACKEND_DIR)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/rag-docs", tags=["rag-docs"])


# ==========================================================================
# RAG Backend Interface (Adapter Pattern)
# ==========================================================================

class RagBackend(ABC):
    """Abstract interface for any RAG document store.

    Implementations:
    - FileBackend: documents in rag/documents/, indexed in rag/index.json
    - VectorBackend: Chroma/FAISS vector store (would need chromadb/faiss)
    - McpResourceBackend: documents as MCP resources (policy:// URIs)
    """

    @abstractmethod
    def list_docs(self) -> List[Dict[str, Any]]:
        """Return metadata for all documents in the corpus."""
        ...

    @abstractmethod
    def add_doc(self, doc_id: str, filename: str, content: bytes, title: Optional[str] = None) -> Dict[str, Any]:
        """Add a document to the corpus. Returns {doc_id, chunks_indexed, status}."""
        ...

    @abstractmethod
    def remove_doc(self, doc_id: str) -> Dict[str, Any]:
        """Remove a document from the corpus. Returns {doc_id, status}."""
        ...

    @abstractmethod
    def get_doc(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get full content and metadata for a single document."""
        ...

    @abstractmethod
    def invalidate(self) -> None:
        """Signal to the RAG agent that the corpus has changed.
        Must be called after EVERY add or remove."""
        ...

    @abstractmethod
    def stats(self) -> Dict[str, Any]:
        """Return corpus statistics."""
        ...


# ==========================================================================
# Default Implementation: FileBackend
# ==========================================================================

class FileBackend(RagBackend):
    """Default file-based RAG backend.

    Documents are stored in rag/documents/.
    Chunked index is stored in rag/index.json.
    Metadata is stored in rag/metadata.json.
    Cache invalidation via rag/corpus_version.txt sentinel.
    """

    def __init__(self, root_dir: Optional[Path] = None):
        self.root = root_dir or (REPO_ROOT / "rag")
        self.docs_dir = self.root / "documents"
        self.index_file = self.root / "index.json"
        self.metadata_file = self.root / "metadata.json"
        self.version_file = self.root / "corpus_version.txt"
        self.sentinel_file = self.root / "invalidation_sentinel.txt"

        self.docs_dir.mkdir(parents=True, exist_ok=True)

    # -- helpers ----------------------------------------------------------

    def _load_index(self) -> Dict[str, Any]:
        if self.index_file.exists():
            try:
                with open(self.index_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {"documents": {}, "version": 1, "last_updated": None}

    def _save_index(self, index: Dict[str, Any]) -> None:
        index["version"] = index.get("version", 1) + 1
        index["last_updated"] = datetime.utcnow().isoformat() + "Z"
        with open(self.index_file, 'w') as f:
            json.dump(index, f, indent=2)

    def _load_metadata(self) -> Dict[str, Any]:
        if self.metadata_file.exists():
            try:
                with open(self.metadata_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {}

    def _save_metadata(self, metadata: Dict[str, Any]) -> None:
        with open(self.metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)

    def _chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
        """Sliding-window chunking with sentence-boundary awareness."""
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            if end < len(text):
                for sep in ["\n\n", ". ", "\n", " "]:
                    pos = text.rfind(sep, start, end)
                    if pos > start + chunk_size // 2:
                        end = pos + len(sep)
                        break
            chunks.append(text[start:end].strip())
            start = end - overlap if end < len(text) else end
        return chunks

    def _compute_doc_id(self, content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()[:16]

    def _safe_filename(self, name: str) -> str:
        return Path(name).name

    # -- RagBackend implementation ----------------------------------------

    def list_docs(self) -> List[Dict[str, Any]]:
        metadata = self._load_metadata()
        index = self._load_index()
        results = []
        for doc_id, meta in metadata.items():
            doc_info = index.get("documents", {}).get(doc_id, {})
            filepath = self.docs_dir / meta.get("filename", "")
            content_preview = ""
            size_bytes = 0
            if filepath.exists():
                size_bytes = filepath.stat().st_size
                try:
                    text = filepath.read_text(encoding='utf-8')
                    content_preview = text[:200].replace("\n", " ") + "..." if len(text) > 200 else text
                except Exception:
                    content_preview = "(binary or unreadable)"
            results.append({
                "doc_id": doc_id,
                "filename": meta.get("filename", "unknown"),
                "title": meta.get("title"),
                "content_preview": content_preview,
                "size_bytes": size_bytes,
                "indexed_at": doc_info.get("indexed_at"),
                "chunk_count": doc_info.get("chunk_count", 0),
            })
        return sorted(results, key=lambda x: x.get("indexed_at") or "", reverse=True)

    def add_doc(self, doc_id: str, filename: str, content: bytes, title: Optional[str] = None) -> Dict[str, Any]:
        # Write to storage
        safe_name = self._safe_filename(filename)
        filepath = self.docs_dir / safe_name
        counter = 1
        original_stem = filepath.stem
        while filepath.exists():
            filepath = self.docs_dir / f"{original_stem}_{counter}{filepath.suffix}"
            counter += 1

        with open(filepath, 'wb') as f:
            f.write(content)

        # Chunk and index
        try:
            text = filepath.read_text(encoding='utf-8')
            chunks = self._chunk_text(text)
        except UnicodeDecodeError:
            chunks = []

        index = self._load_index()
        index["documents"][doc_id] = {
            "filename": filepath.name,
            "title": title or filepath.stem,
            "chunks": chunks,
            "chunk_count": len(chunks),
            "indexed_at": datetime.utcnow().isoformat() + "Z",
            "file_size": filepath.stat().st_size,
        }
        self._save_index(index)

        metadata = self._load_metadata()
        metadata[doc_id] = {
            "filename": filepath.name,
            "title": title or filepath.stem,
            "indexed_at": datetime.utcnow().isoformat() + "Z",
        }
        self._save_metadata(metadata)

        return {
            "doc_id": doc_id,
            "filename": filepath.name,
            "chunks_indexed": len(chunks),
            "status": "indexed",
        }

    def remove_doc(self, doc_id: str) -> Dict[str, Any]:
        metadata = self._load_metadata()
        if doc_id not in metadata:
            raise KeyError(f"Document {doc_id} not found")

        meta = metadata[doc_id]
        filename = meta.get("filename")
        if filename:
            filepath = self.docs_dir / filename
            if filepath.exists():
                filepath.unlink()

        index = self._load_index()
        if doc_id in index.get("documents", {}):
            del index["documents"][doc_id]
            self._save_index(index)

        del metadata[doc_id]
        self._save_metadata(metadata)

        return {"doc_id": doc_id, "filename": filename, "status": "removed"}

    def get_doc(self, doc_id: str) -> Optional[Dict[str, Any]]:
        metadata = self._load_metadata()
        if doc_id not in metadata:
            return None

        meta = metadata[doc_id]
        filepath = self.docs_dir / meta.get("filename", "")
        if not filepath.exists():
            return None

        try:
            content = filepath.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            return None

        index = self._load_index()
        doc_info = index.get("documents", {}).get(doc_id, {})

        return {
            "doc_id": doc_id,
            "filename": meta.get("filename"),
            "title": meta.get("title"),
            "content": content,
            "size_bytes": filepath.stat().st_size,
            "indexed_at": doc_info.get("indexed_at"),
            "chunk_count": doc_info.get("chunk_count", 0),
            "chunks": doc_info.get("chunks", []),
        }

    def invalidate(self) -> None:
        """Bump version sentinel + call any registered invalidation hooks."""
        # 1. Version file (simplest, works with any agent that checks a version)
        version = 1
        if self.version_file.exists():
            try:
                version = int(self.version_file.read_text().strip()) + 1
            except ValueError:
                version = 1
        self.version_file.write_text(str(version))

        # 2. Touch sentinel for file-watchers
        self.sentinel_file.write_text(datetime.utcnow().isoformat() + "Z")

        # 3. Try B2's invalidation function if available
        try:
            from rag import index_manager
            if hasattr(index_manager, "invalidate_cache"):
                index_manager.invalidate_cache()
        except ImportError:
            pass

        # 4. Try agent direct invalidation
        try:
            from agent.agent import invalidate_rag_cache as agent_invalidate
            agent_invalidate()
        except (ImportError, AttributeError):
            pass

        # 5. Try to reload any in-memory vector store
        try:
            from rag.vector_store import reload_index
            reload_index()
        except (ImportError, AttributeError):
            pass

    def stats(self) -> Dict[str, Any]:
        metadata = self._load_metadata()
        index = self._load_index()
        total_chunks = sum(d.get("chunk_count", 0) for d in index.get("documents", {}).values())

        version = 1
        if self.version_file.exists():
            try:
                version = int(self.version_file.read_text().strip())
            except ValueError:
                pass

        return {
            "total_documents": len(metadata),
            "total_chunks": total_chunks,
            "corpus_version": version,
            "last_updated": index.get("last_updated"),
            "index_version": index.get("version", 1),
        }


# ==========================================================================
# Backend Factory
# ==========================================================================

_backends: Dict[str, RagBackend] = {}


def get_backend() -> RagBackend:
    """Returns the configured RAG backend singleton.

    To use a different backend, set the environment variable:
        RAG_BACKEND_CLASS=module.path.CustomBackend

    The custom class must inherit from RagBackend and accept
    root_dir as an optional __init__ argument.
    """
    import os
    backend_key = os.environ.get("RAG_BACKEND_CLASS", "file")

    if backend_key not in _backends:
        if backend_key == "file":
            _backends[backend_key] = FileBackend()
        else:
            # Dynamic import of custom backend
            module_path, class_name = backend_key.rsplit(".", 1)
            module = __import__(module_path, fromlist=[class_name])
            cls = getattr(module, class_name)
            _backends[backend_key] = cls(root_dir=REPO_ROOT / "rag")

    return _backends[backend_key]


# ==========================================================================
# Pydantic Schemas
# ==========================================================================

class RagDocOut(BaseModel):
    doc_id: str
    filename: str
    title: Optional[str] = None
    content_preview: str
    size_bytes: int
    indexed_at: Optional[str] = None
    chunk_count: int = 0


class RagDocAddResponse(BaseModel):
    doc_id: str
    filename: str
    chunks_indexed: int
    status: str


class RagDocRemoveRequest(BaseModel):
    doc_id: str


# ==========================================================================
# Routes
# ==========================================================================

@router.get("/list", response_model=List[RagDocOut])
def list_rag_docs():
    """List all documents currently in the RAG corpus."""
    backend = get_backend()
    docs = backend.list_docs()
    return docs


@router.post("/add")
async def add_rag_doc(
    file: UploadFile = File(..., description="Document to add to RAG corpus"),
    title: Optional[str] = Form(None, description="Optional display title"),
):
    """Upload a document to the RAG corpus, chunk it, index it, and invalidate
    the retrieval cache so the next agent query sees the new document.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    doc_id = hashlib.sha256(content).hexdigest()[:16]
    backend = get_backend()

    # Check duplicate
    existing = backend.get_doc(doc_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Document '{file.filename}' already exists (doc_id={doc_id}). Remove it first."
        )

    try:
        result = backend.add_doc(doc_id, file.filename, content, title or file.filename)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Indexing failed: {e}")

    # CRITICAL: invalidate cache so next query picks up the change
    backend.invalidate()

    return RagDocAddResponse(**result)


@router.post("/remove")
def remove_rag_doc(request: RagDocRemoveRequest):
    """Remove a document from the RAG corpus and invalidate the retrieval cache.

    The next agent query will NOT retrieve from this document.
    """
    backend = get_backend()
    try:
        result = backend.remove_doc(request.doc_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # CRITICAL: invalidate cache
    backend.invalidate()

    return result


@router.get("/doc/{doc_id}")
def get_rag_doc(doc_id: str):
    """Get full content and metadata of a single RAG document."""
    backend = get_backend()
    doc = backend.get_doc(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
    return doc


@router.get("/stats")
def rag_stats():
    """Dashboard stats: total docs, total chunks, corpus version."""
    backend = get_backend()
    return backend.stats()