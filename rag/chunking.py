import os
from pathlib import Path
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

def get_policy_chunks():
    # 1. تحديد المسارات
    base_dir = Path(__file__).resolve().parent
    policies_dir = base_dir / "policies"
    
    # 2. تعريف مستويات العناوين للتقسيم
    # هنقسم بناءً على ## (Sections)
    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
    ]
    
    markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on)
    
    # لضمان إن لو فيه قسم طويل بشكل استثنائي يتم تقسيمه برضه
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    
    all_final_chunks = []
    
    # 3. معالجة كل ملف في مجلد policies
    for md_file in policies_dir.glob("*.md"):
        print(f"Processing: {md_file.name}")
        with open(md_file, "r", encoding="utf-8") as f:
            text = f.read()
        
        # التقسيم الأول (بناءً على العناوين)
        header_splits = markdown_splitter.split_text(text)
        
        # إضافة metadata إضافية لكل قطعة (اسم الملف)
        for doc in header_splits:
            doc.metadata["source"] = md_file.name

            # نلزّق عناوين الأقسام في بداية النص نفسه، مش بس في الـ
            # metadata -- عشان BM25 والـ vector search يقدروا يشوفوا
            # حاجات زي "Policy #1" اللي بتكون موجودة في العنوان بس
            header_prefix_parts = []
            if "Header 1" in doc.metadata:
                header_prefix_parts.append(doc.metadata["Header 1"])
            if "Header 2" in doc.metadata:
                header_prefix_parts.append(doc.metadata["Header 2"])
            if header_prefix_parts:
                doc.page_content = f"{' - '.join(header_prefix_parts)}\n\n{doc.page_content}"
            
            final_docs = text_splitter.split_documents([doc])

            # نستبعد الـ chunks القصيرة جدًا (زي سطور metadata بحتة من
            # غير محتوى فعلي) -- بتديها embeddings مش موثوقة وبتشوّه
            # الترتيب (لاحظنا chunk من سطرين بس بياخد أعلى vector score)
            MIN_CHUNK_LENGTH = 40
            final_docs = [d for d in final_docs if len(d.page_content.strip()) >= MIN_CHUNK_LENGTH]

            all_final_chunks.extend(final_docs)
    return all_final_chunks

if __name__ == "__main__":
    chunks = get_policy_chunks()
    print(f"\n✅ Created {len(chunks)} chunks in total.")
    
    # عرض عينة للتأكد
    if chunks:
        print("\n--- Sample Chunk ---")
        print(f"Metadata: {chunks[0].metadata}")
        print(f"Content Sample: {chunks[0].page_content[:100]}...")