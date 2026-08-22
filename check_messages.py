import sqlite3
conn = sqlite3.connect('db/procurement.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

session_id = 40  # غيريها لآخر session_id شفتيها في اللوج

rows = cur.execute(
    "SELECT MessageID, Sender, Content, MessageType, CreatedAt FROM ChatMessages "
    "WHERE SessionID = ? ORDER BY CreatedAt", (session_id,)
).fetchall()

print(f"Total messages in session {session_id}: {len(rows)}")
for r in rows:
    print(f"  [{r['MessageID']}] {r['Sender']} ({r['MessageType']}): {r['Content'][:80]}")

status = cur.execute("SELECT Status FROM ChatSessions WHERE SessionID = ?", (session_id,)).fetchone()
print(f"\nSession status: {status['Status'] if status else 'not found'}")

conn.close()