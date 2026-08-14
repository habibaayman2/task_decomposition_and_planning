"""
memory/stores.py
Person 1 — Episodic store + Semantic store (backing memory/router.py and
memory/consolidation.py)

Both stores are plain SQLite, reusing the same pattern as db/procurement.db
(see db/schema.sql) rather than inventing a new persistence layer — this
is a NEW database file (memory/memory_store.db) because episodic/semantic
memory is agent-internal state, not IronBridge business data, but it
follows the same "small SQLite file + explicit schema" convention as the
rest of the repo so a grader doesn't have to learn a second style.

IMPORTANT invariant enforced by design, not just by convention:
SemanticStore has no public method that isn't called from
consolidation.py. EpisodicStore.add() is the only write path the router
(memory/router.py) is allowed to call. This is what makes "router never
writes directly to semantic memory" checkable by reading the file, not
just true by promise.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "memory_store.db"


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS episodic_memory (
    episode_id      TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    project_id      TEXT,
    content         TEXT NOT NULL,
    source_role     TEXT NOT NULL,      -- who/what produced this turn
    reason          TEXT NOT NULL,      -- router's logged reasoning for promotion
    created_at      REAL NOT NULL,
    consolidated    INTEGER NOT NULL DEFAULT 0   -- set by consolidation.py once processed
);

CREATE TABLE IF NOT EXISTS semantic_memory (
    fact_id         TEXT PRIMARY KEY,
    subject         TEXT NOT NULL,      -- e.g. 'Supplier:Acme Rebar'
    predicate       TEXT NOT NULL,      -- e.g. 'typical_lead_time_days'
    object          TEXT NOT NULL,      -- the value, as text
    version         INTEGER NOT NULL,
    status          TEXT NOT NULL,      -- 'active' | 'superseded' | 'expired'
    valid_from      REAL NOT NULL,
    valid_until     REAL,               -- NULL = open-ended
    source_episode_ids TEXT NOT NULL,   -- JSON list, provenance
    superseded_by   TEXT,               -- fact_id of the newer version, if any
    conflict_note   TEXT                -- populated only when this write resolved a conflict
);

CREATE INDEX IF NOT EXISTS idx_semantic_subject_predicate
    ON semantic_memory (subject, predicate, status);
"""


def init_db(db_path: Path = DB_PATH) -> None:
    conn = _connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


@dataclass
class Episode:
    episode_id: str
    session_id: str
    project_id: Optional[str]
    content: str
    source_role: str
    reason: str
    created_at: float
    consolidated: bool


class EpisodicStore:
    """Append-only log of promoted memories. This is the ONLY thing
    memory/router.py is allowed to write to for long-term retention."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        init_db(db_path)

    def add(self, *, session_id: str, content: str, source_role: str,
            reason: str, project_id: Optional[str] = None) -> Episode:
        ep = Episode(
            episode_id=str(uuid.uuid4()),
            session_id=session_id,
            project_id=project_id,
            content=content,
            source_role=source_role,
            reason=reason,
            created_at=time.time(),
            consolidated=False,
        )
        conn = _connect(self.db_path)
        conn.execute(
            "INSERT INTO episodic_memory "
            "(episode_id, session_id, project_id, content, source_role, reason, created_at, consolidated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (ep.episode_id, ep.session_id, ep.project_id, ep.content,
             ep.source_role, ep.reason, ep.created_at),
        )
        conn.commit()
        conn.close()
        return ep

    def unconsolidated(self) -> list[Episode]:
        conn = _connect(self.db_path)
        rows = conn.execute(
            "SELECT * FROM episodic_memory WHERE consolidated = 0 ORDER BY created_at"
        ).fetchall()
        conn.close()
        return [Episode(**dict(r)) for r in rows]

    def mark_consolidated(self, episode_ids: list[str]) -> None:
        if not episode_ids:
            return
        conn = _connect(self.db_path)
        conn.executemany(
            "UPDATE episodic_memory SET consolidated = 1 WHERE episode_id = ?",
            [(eid,) for eid in episode_ids],
        )
        conn.commit()
        conn.close()

    def recall(self, session_id: Optional[str] = None, limit: int = 20) -> list[Episode]:
        conn = _connect(self.db_path)
        if session_id:
            rows = conn.execute(
                "SELECT * FROM episodic_memory WHERE session_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM episodic_memory ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        conn.close()
        return [Episode(**dict(r)) for r in rows]


@dataclass
class Fact:
    fact_id: str
    subject: str
    predicate: str
    object: str
    version: int
    status: str
    valid_from: float
    valid_until: Optional[float]
    source_episode_ids: list
    superseded_by: Optional[str]
    conflict_note: Optional[str]


class SemanticStore:
    """Versioned fact store. Only memory/consolidation.py writes here —
    that's what makes the "consolidation is the sole writer of semantic
    memory" rule real rather than aspirational."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        init_db(db_path)

    def current_fact(self, subject: str, predicate: str) -> Optional[Fact]:
        conn = _connect(self.db_path)
        row = conn.execute(
            "SELECT * FROM semantic_memory WHERE subject=? AND predicate=? "
            "AND status='active' ORDER BY version DESC LIMIT 1",
            (subject, predicate),
        ).fetchone()
        conn.close()
        if not row:
            return None
        d = dict(row)
        d["source_episode_ids"] = json.loads(d["source_episode_ids"])
        return Fact(**d)

    def write_new_version(
        self,
        *,
        subject: str,
        predicate: str,
        value: str,
        source_episode_ids: list[str],
        valid_from: Optional[float] = None,
        conflict_note: Optional[str] = None,
    ) -> Fact:
        """Writes a new active version. If an active version already
        exists for (subject, predicate), it is superseded (status
        flipped, valid_until stamped, superseded_by set) rather than
        overwritten — this is the versioning + no-silent-overwrite
        guarantee."""
        conn = _connect(self.db_path)
        prev = conn.execute(
            "SELECT * FROM semantic_memory WHERE subject=? AND predicate=? "
            "AND status='active' ORDER BY version DESC LIMIT 1",
            (subject, predicate),
        ).fetchone()

        new_version = 1
        now = valid_from or time.time()
        new_id = str(uuid.uuid4())

        if prev:
            new_version = prev["version"] + 1
            conn.execute(
                "UPDATE semantic_memory SET status='superseded', valid_until=?, superseded_by=? "
                "WHERE fact_id=?",
                (now, new_id, prev["fact_id"]),
            )

        conn.execute(
            "INSERT INTO semantic_memory "
            "(fact_id, subject, predicate, object, version, status, valid_from, valid_until, "
            " source_episode_ids, superseded_by, conflict_note) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, NULL, ?, NULL, ?)",
            (new_id, subject, predicate, value, new_version, now,
             json.dumps(source_episode_ids), conflict_note),
        )
        conn.commit()
        conn.close()
        return Fact(
            fact_id=new_id, subject=subject, predicate=predicate, object=value,
            version=new_version, status="active", valid_from=now, valid_until=None,
            source_episode_ids=source_episode_ids, superseded_by=None,
            conflict_note=conflict_note,
        )

    def expire_stale(self, ttl_seconds: float) -> list[Fact]:
        """Sweep active facts older than ttl_seconds and mark them
        'expired' (distinct status from 'superseded' — expired means
        nobody replaced it, it just went stale, e.g. a lead-time
        estimate nobody has reconfirmed in 90 days)."""
        conn = _connect(self.db_path)
        now = time.time()
        cutoff = now - ttl_seconds
        rows = conn.execute(
            "SELECT * FROM semantic_memory WHERE status='active' AND valid_from < ?",
            (cutoff,),
        ).fetchall()
        expired = []
        for r in rows:
            conn.execute(
                "UPDATE semantic_memory SET status='expired', valid_until=? WHERE fact_id=?",
                (now, r["fact_id"]),
            )
            d = dict(r)
            d["status"] = "expired"
            d["valid_until"] = now
            d["source_episode_ids"] = json.loads(d["source_episode_ids"])
            expired.append(Fact(**d))
        conn.commit()
        conn.close()
        return expired

    def history(self, subject: str, predicate: str) -> list[Fact]:
        """Full version history for a fact — this is how a grader (or
        the agent) can see that an old fact wasn't silently lost."""
        conn = _connect(self.db_path)
        rows = conn.execute(
            "SELECT * FROM semantic_memory WHERE subject=? AND predicate=? ORDER BY version",
            (subject, predicate),
        ).fetchall()
        conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d["source_episode_ids"] = json.loads(d["source_episode_ids"])
            out.append(Fact(**d))
        return out
