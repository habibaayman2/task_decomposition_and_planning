import os
from pathlib import Path
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

MIN_CHUNK_LENGTH = 40


_HEADERS_TO_SPLIT_ON = [
    ("#", "Header 1"),
    ("##", "Header 2"),
]


def _chunk_single_file(md_file: Path):
    """يقسّم ملف .md واحد لقطع (chunks) جاهزة للفهرسة، بنفس منطق
    get_policy_chunks() بالظبط. مستخدمة من جوه get_policy_chunks()
    (لكل الملفات) وبرضو من rag/vector_store.py's add_document()
    (لملف واحد بس، لتحديث جزئي بدل إعادة بناء الفهرس كله)."""
    markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=_HEADERS_TO_SPLIT_ON)
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)

    with open(md_file, "r", encoding="utf-8") as f:
        text = f.read()

    header_splits = markdown_splitter.split_text(text)

    file_chunks = []
    for doc in header_splits:
        doc.metadata["source"] = md_file.name

        header_prefix_parts = []
        if "Header 1" in doc.metadata:
            header_prefix_parts.append(doc.metadata["Header 1"])
        if "Header 2" in doc.metadata:
            header_prefix_parts.append(doc.metadata["Header 2"])
        if header_prefix_parts:
            doc.page_content = f"{' - '.join(header_prefix_parts)}\n\n{doc.page_content}"

        final_docs = text_splitter.split_documents([doc])
        final_docs = [d for d in final_docs if len(d.page_content.strip()) >= MIN_CHUNK_LENGTH]
        file_chunks.extend(final_docs)

    return file_chunks


def get_policy_chunks():
   
    base_dir = Path(__file__).resolve().parent
    policies_dir = base_dir / "policies"

    all_final_chunks = []

    for md_file in policies_dir.glob("*.md"):
        print(f"Processing: {md_file.name}")
        all_final_chunks.extend(_chunk_single_file(md_file))

    return all_final_chunks


def get_chunks_for_file(filename: str):
    
    base_dir = Path(__file__).resolve().parent
    md_file = base_dir / "policies" / filename

    if not md_file.exists():
        raise FileNotFoundError(f"{filename} not found in {md_file.parent}")

    return _chunk_single_file(md_file)


if __name__ == "__main__":
    chunks = get_policy_chunks()
    print(f"\n✅ Created {len(chunks)} chunks in total.")
    
    
    if chunks:
        print("\n--- Sample Chunk ---")
        print(f"Metadata: {chunks[0].metadata}")
        print(f"Content Sample: {chunks[0].page_content[:100]}...")