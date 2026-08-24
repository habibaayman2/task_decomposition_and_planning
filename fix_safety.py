import sqlite3
conn = sqlite3.connect('db/procurement.db')
conn.execute("CREATE TABLE IF NOT EXISTS SafetyIncidents (IncidentID INTEGER PRIMARY KEY AUTOINCREMENT, RunID TEXT, ProjectID INTEGER, Description TEXT, Severity TEXT, Status TEXT DEFAULT 'Reported', InvestigationRound INTEGER DEFAULT 1, InvestigationNotes TEXT, ReportedAt REAL)")
conn.commit()
print('Done')