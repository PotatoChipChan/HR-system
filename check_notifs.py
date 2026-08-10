import sqlite3
conn = sqlite3.connect('instance/smarthr.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT notification_id, employee_id, title, related_url, type, is_read FROM Notification WHERE title LIKE '%IC%'").fetchall()
for r in rows:
    print(dict(r))
conn.close()
