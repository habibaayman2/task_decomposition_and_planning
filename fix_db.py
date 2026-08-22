import sqlite3

conn = sqlite3.connect('db/procurement.db')
cur = conn.cursor()

# 1. EquipmentBreakdowns table
cur.execute('''
CREATE TABLE IF NOT EXISTS EquipmentBreakdowns (
    EquipmentBreakdownID INTEGER PRIMARY KEY AUTOINCREMENT,
    ProjectID INTEGER NOT NULL,
    Description TEXT NOT NULL,
    Status TEXT NOT NULL DEFAULT 'reported',
    ReportedAt TEXT DEFAULT (datetime('now'))
)
''')

# 2. Projects table (with RemainingBudget for HITL)
cur.execute('''
CREATE TABLE IF NOT EXISTS Projects (
    ProjectID INTEGER PRIMARY KEY AUTOINCREMENT,
    ProjectName TEXT NOT NULL,
    RemainingBudget REAL NOT NULL DEFAULT 100000
)
''')

# 3. EquipmentInventory table
cur.execute('''
CREATE TABLE IF NOT EXISTS EquipmentInventory (
    EquipmentID INTEGER PRIMARY KEY AUTOINCREMENT,
    EquipmentName TEXT NOT NULL,
    ProjectID INTEGER,
    Status TEXT DEFAULT 'available',
    DailyRentalRate REAL DEFAULT 0
)
''')

# Seed test data
cur.execute("INSERT OR IGNORE INTO Projects (ProjectID, ProjectName, RemainingBudget) VALUES (1, 'Demo Project', 10000)")
cur.execute("INSERT OR IGNORE INTO EquipmentInventory (EquipmentID, EquipmentName, ProjectID, Status, DailyRentalRate) VALUES (1, 'Hydraulic Crane', 1, 'available', 500)")
cur.execute("INSERT OR IGNORE INTO EquipmentInventory (EquipmentID, EquipmentName, ProjectID, Status, DailyRentalRate) VALUES (2, 'Excavator', 1, 'available', 300)")

conn.commit()
conn.close()
print('Tables created + seeded!')