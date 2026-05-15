import sqlite3
import os
db_path = os.path.join(os.path.dirname(__file__), 'users.db')
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, username, is_mock FROM users")
    rows = cur.fetchall()
    for row in rows:
        print(f"ID: {row[0]}, User: {row[1]}, IsMock: {row[2]}")
    conn.close()
else:
    print(f"File not found: {db_path}")
