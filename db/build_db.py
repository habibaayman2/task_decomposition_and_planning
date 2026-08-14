"""
Build (or rebuild) db/procurement.db from db/schema.sql + db/seed.sql.

Why this exists?: db/*.db is gitignored on purpose (a binary SQLite file
doesn't diff meaningfully in git), so a fresh clone needs a
deterministic way to get a working database. 
Usage:
    python3 db/build_db.py            # build db/procurement.db next to this script
    IRONBRIDGE_DB_PATH=/tmp/x.db python3 db/build_db.py   # build somewhere else

Safe to re-run: always drops and recreates from schema.sql + seed.sql,
so it's also the fix for missing up the local data during testing."
"""

import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "schema.sql"
SEED_PATH = HERE / "seed.sql"


def build(db_path: Path) -> None:
    if db_path.exists():
        print(f"Removing existing {db_path}")
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.executescript(SEED_PATH.read_text())
        conn.commit()
    finally:
        conn.close()

    print(f"Built {db_path} from {SCHEMA_PATH.name} + {SEED_PATH.name}")


if __name__ == "__main__":
    target = os.environ.get("IRONBRIDGE_DB_PATH", str(HERE / "procurement.db"))
    build(Path(target))
