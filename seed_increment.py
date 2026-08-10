"""
seed_increment.py  –  Backfill Salary Increment proposals for Jan 2026.
Run AFTER init_db.py and seed_performance.py.

Usage: python seed_increment.py
"""
import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join('instance', 'smarthr.db')

GRADE_RANGES = {
    'A': (8, 10, 9),
    'B': (5, 7, 6),
    'C': (3, 4, 3.5),
    'D': (0, 2, 1),
}


def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()

    # Find admin user to set as proposer/reviewer
    admin = cur.execute(
        "SELECT employee_id FROM Employee WHERE role_id=(SELECT role_id FROM Role WHERE role_name='Admin') LIMIT 1"
    ).fetchone()
    if not admin:
        print("[ERROR] No admin found. Run init_db.py first.")
        return
    admin_id = admin['employee_id']

    year = 2026
    employees = cur.execute("""
        SELECT e.employee_id, e.full_name, e.base_salary,
               ps.composite_score, ps.grade
        FROM Employee e
        JOIN Performance_Score ps ON e.employee_id=ps.employee_id
        WHERE ps.period_year=? AND e.is_active=1
          AND e.employee_id NOT IN (
              SELECT employee_id FROM Salary_Increment WHERE period_year=?
          )
        GROUP BY e.employee_id
    """, (year, year)).fetchall()

    if not employees:
        print("[SKIP] All eligible employees already have increment records.")
        return

    count = 0
    for emp in employees:
        grade = emp['grade']
        lo, hi, default = GRADE_RANGES.get(grade, (0, 0, 0))
        if default == 0:
            continue

        old_salary = emp['base_salary']
        new_salary = round(old_salary * (1 + default / 100))

        cur.execute("""
            INSERT OR IGNORE INTO Salary_Increment
            (employee_id, period_year, old_salary, new_salary, increment_pct,
             performance_score, performance_grade, proposed_by, proposed_at,
             status, reviewed_by, reviewed_at, notified_at)
            VALUES(?,?,?,?,?,?,?,?,datetime('now','-5 months'),
                   'Approved',?,datetime('now','-5 months'),datetime('now','-5 months'))
        """, (emp['employee_id'], year, old_salary, new_salary, default,
              emp['composite_score'], grade, admin_id, admin_id))

        cur.execute("UPDATE Employee SET base_salary=? WHERE employee_id=?",
                    (new_salary, emp['employee_id']))
        count += 1

    con.commit()
    con.close()
    print(f"[OK] Created {count} approved Salary Increment records for {year}.")
    print(f"[OK] Employee base salaries updated accordingly.")


if __name__ == '__main__':
    main()
