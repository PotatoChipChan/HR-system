import sqlite3

conn = sqlite3.connect('instance/smarthr.db')
conn.execute("PRAGMA foreign_keys = ON")

eid = 41

# Show what will be deleted
emp = conn.execute("SELECT employee_id, full_name, email FROM Employee WHERE employee_id=?", (eid,)).fetchone()
if emp:
    print(f"Deleting: ID={emp[0]}, Name={emp[1]}, Email={emp[2]}")
else:
    print(f"Employee #{eid} not found")
    conn.close()
    exit()

# Delete related records first (FK constraints)
tables = [
    ("Notification", "employee_id"),
    ("IC_Access_Request", "requester_id"),
    ("IC_Access_Request", "target_employee_id"),
    ("Leave_Application", "employee_id"),
    ("Invoice", "employee_id"),
    ("Attendance", "employee_id"),
    ("AuditLog", "employee_id"),
    ("Employee_Permission", "employee_id"),
    ("Face_Registration", "employee_id"),
    ("Salary_Increment", "employee_id"),
    ("Bonus_Proposal", "employee_id"),
    ("Performance", "employee_id"),
    ("Contract", "employee_id"),
    ("Payroll", "employee_id"),
]

for table, col in tables:
    try:
        cur = conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (eid,))
        if cur.rowcount > 0:
            print(f"  Deleted {cur.rowcount} row(s) from {table}")
    except Exception as e:
        print(f"  Skipped {table}: {e}")

# Delete the employee
conn.execute("DELETE FROM Employee WHERE employee_id = ?", (eid,))
conn.commit()
print("Employee deleted successfully!")
conn.close()
