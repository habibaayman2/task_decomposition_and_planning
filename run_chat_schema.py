import sqlite3

DB_PATH = "db/procurement.db"
SCHEMA_PATH = "db/chat_schema.sql"

with open(SCHEMA_PATH, "r") as f:
    schema = f.read()

conn = sqlite3.connect(DB_PATH)
conn.executescript(schema)
conn.commit()
conn.close()

print("✅ Chat schema applied successfully!")