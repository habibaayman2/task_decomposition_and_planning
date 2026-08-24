import sqlite3

conn = sqlite3.connect('db/procurement.db')

# Drop old table (data will be lost — that's OK for demo)
conn.execute("DROP TABLE IF EXISTS SafetyIncidents")

# Create with FULL schema from _SCHEMA
conn.execute("""
CREATE TABLE SafetyIncidents (
    IncidentID          INTEGER PRIMARY KEY,
    RunID               TEXT NOT NULL,
    ProjectID           INTEGER NOT NULL,
    Description         TEXT NOT NULL,
    Severity            TEXT NOT NULL CHECK (Severity IN ('low', 'medium', 'high', 'critical')),
    Status              TEXT NOT NULL CHECK (Status IN (
        'Reported', 'UnderInvestigation', 'PendingOfficerReview',
        'RegulatorReportRequired', 'RegulatorReportFiled', 'Closed'
    )),
    InvestigationRound  INTEGER NOT NULL DEFAULT 1,
    InvestigationNotes  TEXT,
    RegulatorCaseNumber TEXT,
    ReportedAt          REAL,
    ClosedAt            REAL,
    FOREIGN KEY (ProjectID) REFERENCES Projects(ProjectID)
)
""")

conn.commit()
print("SafetyIncidents table recreated with full schema!")