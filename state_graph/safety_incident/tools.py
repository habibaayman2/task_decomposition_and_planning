from __future__ import annotations
from pathlib import Path
import sys
import time
from typing import Optional

# --------------------------------------------------------------------------
# Path & Module Resolution
# --------------------------------------------------------------------------
_current_dir = Path(__file__).resolve().parent
REPO_ROOT = next(
    (p for p in [_current_dir] + list(_current_dir.parents) if (p / "mcp_server").exists()),
    _current_dir.parent.parent  # Fallback
)

for path_entry in (str(REPO_ROOT), str(REPO_ROOT / "mcp_server")):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

import db as mcp_db  # get_conn(), log_action() -- same ones server.py's tools use

_SCHEMA = """
CREATE TABLE IF NOT EXISTS SafetyIncidents (
    IncidentID          INTEGER PRIMARY KEY,
    RunID                TEXT NOT NULL,
    ProjectID            INTEGER NOT NULL,
    Description          TEXT NOT NULL,
    Severity             TEXT NOT NULL CHECK (Severity IN ('low', 'medium', 'high', 'critical')),
    Status                TEXT NOT NULL CHECK (
        Status IN (
            'Reported', 'UnderInvestigation', 'PendingOfficerReview',
            'RegulatorReportRequired', 'RegulatorReportFiled', 'Closed'
        )
    ),
    InvestigationRound   INTEGER NOT NULL DEFAULT 1,
    InvestigationNotes   TEXT,
    RegulatorCaseNumber  TEXT,
    ReportedAt           REAL,
    ClosedAt             REAL,
    FOREIGN KEY (ProjectID) REFERENCES Projects(ProjectID)
);
"""


def _ensure_schema(conn) -> None:
    conn.executescript(_SCHEMA)


def create_incident(
    run_id: str,
    project_id: int,
    description: str,
    severity: str,
    actor_id: int,
) -> int:
    with mcp_db.get_conn() as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "INSERT INTO SafetyIncidents "
            "(RunID, ProjectID, Description, Severity, Status, InvestigationRound, ReportedAt) "
            "VALUES (?, ?, ?, ?, 'Reported', 1, ?)",
            (run_id, project_id, description, severity, time.time()),
        )
        incident_id = cur.lastrowid
        mcp_db.log_action(
            conn, actor_id, "report_safety_incident", incident_id,
            f"severity={severity}",
        )
        return incident_id


def record_investigation_notes(incident_id: int, notes: str, actor_id: int) -> None:
    """Appends findings for the CURRENT investigation round and moves the
    incident to PendingOfficerReview -- called after the (Day-3) LATS
    search over candidate root causes finishes for this round."""
    with mcp_db.get_conn() as conn:
        row = conn.execute(
            "SELECT InvestigationNotes, InvestigationRound FROM SafetyIncidents WHERE IncidentID = ?",
            (incident_id,),
        ).fetchone()
        prior = row["InvestigationNotes"] or ""
        round_no = row["InvestigationRound"]
        merged = f"{prior}\n[Round {round_no}] {notes}".strip()
        conn.execute(
            "UPDATE SafetyIncidents SET InvestigationNotes = ?, Status = 'PendingOfficerReview' "
            "WHERE IncidentID = ?",
            (merged, incident_id),
        )
        mcp_db.log_action(conn, actor_id, "record_investigation_notes", incident_id, f"round={round_no}")


def bump_investigation_round(incident_id: int, actor_id: int) -> None:
    """Called when the safety officer sends the incident back for another
    investigation pass -- the genuine cycle in this graph: a human decided
    the current findings aren't enough, not something a retry can fix."""
    with mcp_db.get_conn() as conn:
        conn.execute(
            "UPDATE SafetyIncidents SET Status = 'UnderInvestigation', "
            "InvestigationRound = InvestigationRound + 1 WHERE IncidentID = ?",
            (incident_id,),
        )
        mcp_db.log_action(conn, actor_id, "reopen_investigation_round", incident_id, "")


def mark_regulator_report_required(incident_id: int, actor_id: int) -> None:
    with mcp_db.get_conn() as conn:
        conn.execute(
            "UPDATE SafetyIncidents SET Status = 'RegulatorReportRequired' WHERE IncidentID = ?",
            (incident_id,),
        )
        mcp_db.log_action(conn, actor_id, "require_regulator_report", incident_id, "")


def submit_regulator_report(incident_id: int, case_number: str, actor_id: int) -> None:
    """The ONE whitelisted filing action a constrained-ReAct node may
    take -- nothing else is exposed to the LLM from this module."""
    with mcp_db.get_conn() as conn:
        conn.execute(
            "UPDATE SafetyIncidents SET Status = 'RegulatorReportFiled', RegulatorCaseNumber = ? "
            "WHERE IncidentID = ?",
            (case_number, incident_id),
        )
        mcp_db.log_action(conn, actor_id, "submit_regulator_report", incident_id, case_number)


def close(incident_id: int, actor_id: int) -> None:
    with mcp_db.get_conn() as conn:
        conn.execute(
            "UPDATE SafetyIncidents SET Status = 'Closed', ClosedAt = ? WHERE IncidentID = ?",
            (time.time(), incident_id),
        )
        mcp_db.log_action(conn, actor_id, "close_safety_incident", incident_id, "")


def get(incident_id: int) -> Optional[dict]:
    with mcp_db.get_conn() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM SafetyIncidents WHERE IncidentID = ?", (incident_id,)
        ).fetchone()
    return dict(row) if row else None
