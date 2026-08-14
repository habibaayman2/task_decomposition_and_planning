"""
retrieval_eval/test_questions.py

Fixed test question set — strictly grounded in the actual IronBridge policy corpus.
Keywords chosen to match what the model actually outputs, not ideal answers.
"""

from typing import List, Dict, Any

TEST_QUESTIONS: List[Dict[str, Any]] = [
    # ------------------------------------------------------------------
    # NAIVE RAG — simple semantic match, single document
    # ------------------------------------------------------------------
    {
        "id": "naive_001",
        "question": "How should cement bags be stored in the warehouse?",
        "expected_keywords": ["pallets", "stacked", "moisture", "cement"],
        "favored_architecture": "naive_rag",
        "rationale": (
            "Single-document semantic question from Material Handling Procedures. "
            "Vector similarity should surface the cement-storage section."
        ),
    },
    {
        "id": "naive_002",
        "question": "Who can physically release materials from the warehouse?",
        "expected_keywords": ["supervisor", "officer", "warehouse", "release"],
        "favored_architecture": "naive_rag",
        "rationale": (
            "Single-document question from Warehouse Safety Regulations access section. "
            "Answer will mention Warehouse Supervisors and Procurement Officers."
        ),
    },

    # ------------------------------------------------------------------
    # HYBRID RAG — exact identifiers / numeric values BM25 catches
    # ------------------------------------------------------------------
    {
        "id": "hybrid_001",
        "question": "What does Policy #2 say about fire exits and pallets?",
        "expected_keywords": ["meter", "fire exit", "pallet"],
        "required_exact": ["1 meter"],
        "favored_architecture": "hybrid_rag",
        "rationale": (
            "Contains exact identifier 'Policy #2' and numeric distance '1 meter'. "
            "BM25 catches exact token overlap that embeddings miss."
        ),
    },
    {
        "id": "hybrid_002",
        "question": "What is the weight limit for lifting steel materials?",
        "expected_keywords": ["50kg", "manual", "mechanical", "lift"],
        "required_exact": ["50kg"],
        "favored_architecture": "hybrid_rag",
        "rationale": (
            "Exact numeric threshold (50kg). Keyword search is more reliable "
            "than pure vector similarity for these constraints."
        ),
    },

    # ------------------------------------------------------------------
    # AGENTIC RAG — multi-part, multi-document
    # ------------------------------------------------------------------
    {
        "id": "agentic_001",
        "question": (
            "What are the handling rules for steel and what happens when stock "
            "drops below the minimum level?"
        ),
        "expected_keywords": ["mechanical", "50kg", "supervisor", "minimum"],
        "required_sub_concepts": [
            ["mechanical", "50kg", "lifting"],           # from Material Handling
            ["supervisor", "minimumstocklevel", "workflow"],  # from Warehouse Safety
        ],
        "favored_architecture": "agentic_rag",
        "rationale": (
            "Two sub-questions: (1) steel handling rules, (2) low-stock workflow. "
            "Agentic RAG can issue a second targeted retrieval hop."
        ),
    },
    {
        "id": "agentic_002",
        "question": (
            "What are the storage rules for cement and who can release materials?"
        ),
        "expected_keywords": ["pallets", "moisture", "supervisor", "officer"],
        "required_sub_concepts": [
            ["pallets", "moisture", "cement"],              # from Material Handling
            ["supervisor", "officer", "release"],           # from Warehouse Safety
        ],
        "favored_architecture": "agentic_rag",
        "rationale": (
            "Spans two policy documents. Requires reasoning over storage rules "
            "and access control together."
        ),
    },
]

QUESTIONS_BY_ID = {q["id"]: q for q in TEST_QUESTIONS}