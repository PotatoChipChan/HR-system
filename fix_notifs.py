import sqlite3
conn = sqlite3.connect('instance/smarthr.db')

# Fix notification #53 - wrong related_url
conn.execute("UPDATE Notification SET related_url = '/settings/?tab=ic-requests' WHERE title LIKE '%IC%' AND related_url != '/settings/?tab=ic-requests'")
conn.commit()

# Verify
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT notification_id, employee_id, title, related_url, type, is_read FROM Notification WHERE title LIKE '%IC%'").fetchall()
for r in rows:
    print(dict(r))
conn.close()
print("Fixed!")
