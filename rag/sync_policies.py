"""
rag/sync_policies.py

Copies the policy markdown files that mcp_server/ exposes as MCP resources
into rag/policies/, so the RAG pipeline has its own snapshot to chunk and
embed without duplicating mcp_server/ as a second source of truth.

mcp_server/ stays the single authoritative source. Run this script whenever
the source policies change, instead of manually copy-pasting files.

Usage (from the repo root):
    python rag/sync_policies.py
"""

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "mcp_server" / "policies"
DEST_DIR = REPO_ROOT / "rag" / "policies"

# The three policy documents currently exposed as MCP resources in
# mcp_server/server.py. Keep this list in sync with the @mcp.resource
# definitions there.
POLICY_FILES = [
    "material_handling_procedures.md",
    "warehouse_safety_regulations.md",
    "equipment_operation_safety_rules.md",
]


def sync_policies() -> None:
    DEST_DIR.mkdir(parents=True, exist_ok=True)

    copied = []
    missing = []

    for filename in POLICY_FILES:
        source_path = SOURCE_DIR / filename
        if not source_path.exists():
            missing.append(filename)
            continue

        dest_path = DEST_DIR / filename
        shutil.copy2(source_path, dest_path)
        copied.append(filename)

    print(f"Synced {len(copied)} policy file(s) to {DEST_DIR}:")
    for name in copied:
        print(f"  - {name}")

    if missing:
        print(f"\nWARNING: {len(missing)} file(s) not found in {SOURCE_DIR}:")
        for name in missing:
            print(f"  - {name}")


if __name__ == "__main__":
    sync_policies()
