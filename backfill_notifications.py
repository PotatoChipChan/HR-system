"""Backfill missing Notification records for existing IC access requests."""
import sqlite3
import os
from datetime import datetime

db_path = os.path.join(os.path.dirname(__file__), 'instance', 'smarthr.db')
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT r.*, requester.full_name as requester_name
    FROM IC_Access_Request r
    JOIN Employee requester ON r.requester_id = requester.employee_id
    WHERE r.status = 'Pending'
""").fetchall()

print(f"Found {len(rows)} pending IC access requests")

for r in rows:
    msg = f"{r['requester_name']} has requested access to your IC document. Please review the request."

    existing = conn.execute("""
        SELECT notification_id FROM Notification
        WHERE employee_id = ? AND title = 'IC Access Request' AND is_read = 0
    """, (r['target_employee_id'],)).fetchone()

    if existing:
        print(f"  Request #{r['request_id']}: Notification #{existing['notification_id']} already exists - updating related_url")
        conn.execute("""
            UPDATE Notification SET related_url = '/settings/?tab=ic-requests'
            WHERE notification_id = ? AND (related_url IS NULL OR related_url = '')
        """, (existing['notification_id'],))
    else:
        conn.execute("""
            INSERT INTO Notification (employee_id, title, message, type, is_read, related_url, created_at)
            VALUES (?, ?, ?, 'Info', 0, '/settings/?tab=ic-requests', ?)
        """, (r['target_employee_id'], 'IC Access Request', msg, r['requested_at'] or datetime.now().isoformat()))
        print(f"  Request #{r['request_id']}: Notification created for Employee #{r['target_employee_id']}")

conn.commit()
conn.close()
print("Done!")