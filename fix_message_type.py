import sqlite3

conn = sqlite3.connect('db/procurement.db')
cur = conn.cursor()

# Preserve existing data
cur.execute("SELECT * FROM ChatMessages")
old_rows = cur.fetchall()
cur.execute("PRAGMA table_info(ChatMessages)")
cols = [c[1] for c in cur.fetchall()]

cur.execute("DROP TABLE ChatMessages")
cur.execute('''
CREATE TABLE ChatMessages (
    MessageID INTEGER PRIMARY KEY AUTOINCREMENT,
    SessionID INTEGER NOT NULL,
    Sender TEXT NOT NULL CHECK (Sender IN ('user', 'agent')),
    Content TEXT NOT NULL,
    NodeName TEXT,
    MessageType TEXT NOT NULL DEFAULT 'text'
        CHECK (MessageType IN (
            'text', 'status_hitl', 'status_paused_hitl',
            'status_ticket', 'status_ticket_open',
            'status_completed', 'status_error', 'status_active'
        )),
    CreatedAt TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (SessionID) REFERENCES ChatSessions(SessionID)
)
''')
cur.execute("CREATE INDEX idx_chat_messages_session ON ChatMessages(SessionID, CreatedAt)")

placeholders = ",".join("?" * len(cols))
cur.executemany(f"INSERT INTO ChatMessages ({','.join(cols)}) VALUES ({placeholders})", old_rows)

conn.commit()
conn.close()
print(f"Fixed MessageType constraint, preserved {len(old_rows)} existing messages")