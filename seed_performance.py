"""
seed_performance.py – One-off script to back-calculate performance scores
for ALL existing attendance data.

Usage: python seed_performance.py
"""
import sqlite3
import os
from datetime import date
import calendar

DB_PATH = os.path.join('instance', 'smarthr.db')


def get_connection():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _working_days_in_month(month, year):
    total = 0
    num_days = calendar.monthrange(year, month)[1]
    for d in range(1, num_days + 1):
        dt = date(year, month, d)
        if dt.weekday() < 5:
            total += 1
    return total


def _get_employee_work_start(cur, employee_id):
    cur.execute("SELECT work_start_time FROM Employee WHERE employee_id=?", (employee_id,))
    emp = cur.fetchone()
    if emp and emp['work_start_time']:
        return emp['work_start_time']
    return '09:00'


def calculate_monthly_score(cur, employee_id, month, year):
    cur.execute("""
        SELECT * FROM Attendance
        WHERE employee_id = ?
          AND strftime('%m', check_in) = ?
          AND strftime('%Y', check_in) = ?
          AND status = 'Approved'
        ORDER BY check_in
    """, (employee_id, f"{month:02d}", str(year)))
    att_records = cur.fetchall()

    if not att_records:
        return None

    total_working_days = _working_days_in_month(month, year)
    if total_working_days == 0:
        return None

    work_start = _get_employee_work_start(cur, employee_id)

    days_present = set()
    on_time_count = 0
    total_ot = 0.0
    manual_count = 0
    total_entries = len(att_records)

    for rec in att_records:
        ci = rec['check_in']
        day = ci[:10]
        days_present.add(day)
        ci_time = ci[11:16]
        if ci_time <= work_start:
            on_time_count += 1
        ot = rec['overtime_hours'] or 0.0
        total_ot += ot
        if rec['is_manual_entry']:
            manual_count += 1

    attendance_rate = min(100.0, (len(days_present) / total_working_days) * 100)
    if len(days_present) > 0:
        punctuality = (on_time_count / len(days_present)) * 100
    else:
        punctuality = 0.0
    ot_cap = 40.0
    overtime_score = min(100.0, (total_ot / ot_cap) * 100)
    if total_entries > 0:
        reliability = (1 - (manual_count / total_entries)) * 100
    else:
        reliability = 100.0

    composite = (
        attendance_rate * 0.40 +
        punctuality * 0.30 +
        overtime_score * 0.15 +
        reliability * 0.15
    )

    if composite >= 85:
        grade = 'A'
    elif composite >= 70:
        grade = 'B'
    elif composite >= 55:
        grade = 'C'
    else:
        grade = 'D'

    return {
        'employee_id': employee_id,
        'period_month': month,
        'period_year': year,
        'attendance_rate': round(attendance_rate, 2),
        'punctuality': round(punctuality, 2),
        'overtime_score': round(overtime_score, 2),
        'reliability': round(reliability, 2),
        'composite_score': round(composite, 2),
        'grade': grade,
    }


def seed_performance():
    con = get_connection()
    cur = con.cursor()

    cur.execute("""
        SELECT DISTINCT employee_id,
               CAST(strftime('%m', check_in) AS INTEGER) as month,
               CAST(strftime('%Y', check_in) AS INTEGER) as year
        FROM Attendance
        WHERE status = 'Approved'
        ORDER BY employee_id, year, month
    """)
    combos = cur.fetchall()

    count = 0
    for row in combos:
        eid = row['employee_id']
        m = row['month']
        y = row['year']

        result = calculate_monthly_score(cur, eid, m, y)
        if result:
            cur.execute("""
                INSERT OR REPLACE INTO Performance_Score
                (employee_id, period_month, period_year, attendance_rate,
                 punctuality, overtime_score, reliability, composite_score, grade)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (result['employee_id'], result['period_month'], result['period_year'],
                  result['attendance_rate'], result['punctuality'],
                  result['overtime_score'], result['reliability'],
                  result['composite_score'], result['grade']))
            count += 1
            print(f"  [{count}] Employee {eid} – {m}/{y}: Score={result['composite_score']} Grade={result['grade']}")

    con.commit()
    con.close()
    print(f"\nDone! Generated {count} performance scores.")


if __name__ == '__main__':
    print("Seeding performance scores from existing attendance data...")
    seed_performance()
