import sqlite3
conn = sqlite3.connect('db/procurement.db')
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM Tickets WHERE Status = 'open'")
print('Open tickets:', cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM Tickets WHERE Status = 'resolved'")
print('Resolved tickets:', cur.fetchone()[0])
conn.close()