from mcp_server.db import DB_PATH
import os

print("DB_PATH used by server:", DB_PATH)
print("File exists:", os.path.exists(DB_PATH))

if os.path.exists(DB_PATH):
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print("Tables found:", [t[0] for t in tables])
    conn.close()