"""
Seed sample Bonus_Proposal records for testing the approval flow.
Uses yearly Performance_Review grades and default bonus months.
Usage: python seed_bonus_test.py
"""
import sqlite3
import os

DB_PATH = os.path.join('instance', 'smarthr.db')

DEFAULT_GRADE_MONTHS = {'A': 3.0, 'B': 2.0, 'C': 1.0, 'D': 0.5}


def _months_worked(hire_date, year):
    from datetime import date, datetime
    if not hire_date:
        return 12
    hire = datetime.strptime(hire_date, '%Y-%m-%d').date()
    start = max(hire, date(year, 1, 1))
    end = date(year, 12, 31)
    if start > end:
        return 0
    return min(12, end.month - start.month + 1)


def seed():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Get active employees with yearly performance reviews
    employees = cur.execute("""
        SELECT e.employee_id, e.full_name, e.base_salary, e.hire_date,
               pr.composite_score, pr.grade, pr.period_year
        FROM Employee e
        JOIN Performance_Review pr ON e.employee_id = pr.employee_id
        WHERE e.is_active = 1
        ORDER BY e.employee_id, pr.period_year DESC
    """).fetchall()

    # Keep latest review per employee
    emp_reviews = {}
    for e in employees:
        eid = e['employee_id']
        if eid not in emp_reviews:
            emp_reviews[eid] = e

    proposals = []
    for eid, emp in emp_reviews.items():
        grade = emp['grade'] or 'D'
        months = DEFAULT_GRADE_MONTHS.get(grade, 0.5)
        full_bonus = round(emp['base_salary'] * months, 2)
        months_worked = _months_worked(emp['hire_date'], emp['period_year'])
        prorated = round(full_bonus * (months_worked / 12.0), 2)

        proposals.append((
            eid, emp['period_year'], emp['composite_score'], grade,
            full_bonus, prorated, months_worked, 1, 'Pending'
        ))

    # Clear existing sample data (proposed by admin)
    cur.execute("DELETE FROM Bonus_Proposal WHERE proposed_by=1")

    cur.executemany("""
        INSERT INTO Bonus_Proposal
        (employee_id, period_year, composite_score, grade,
         full_bonus_amount, bonus_amount, months_worked, proposed_by, status)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, proposals)

    con.commit()
    print(f"Inserted {len(proposals)} sample bonus proposals:")
    for eid, py, score, grade, full, amt, mw, _, _ in proposals:
        emp_name = next((e['full_name'] for e in employees if e['employee_id'] == eid), f'#{eid}')
        print(f"  {emp_name:25s} | {py} | Score: {score or 0:5.1f} | Grade: {grade:3s} | Full: RM {full:>8.2f} | Prorated: RM {amt:>8.2f} | Months: {mw}")

    con.close()
    print("\nRun the app, log in as Admin, go to Performance > Bonus Proposal to test.")


if __name__ == '__main__':
    seed()
