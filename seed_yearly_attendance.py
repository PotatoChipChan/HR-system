"""
seed_yearly_attendance.py – Seed a full year of attendance so the yearly
performance review produces a varied A/B/C/D grade distribution.

Generates 2026 (Jan–Dec) weekday attendance for every active employee of
company 1, using per-employee attendance profiles (attendance rate,
punctuality, overtime, full-day reliability), then recomputes the 2026
Performance_Review rows with the app's own calculate_yearly_review so the
grades match what the Performance and Year-End modules will show. Pending
2026 increments/bonuses are refreshed with the new grades afterwards.

Idempotent: days that already have an Attendance row for an employee are
skipped (e.g. the May 2026 records from seed_increment_test_data.py).

Run: .venv/Scripts/python.exe seed_yearly_attendance.py
"""
import os
import random
import sqlite3
from datetime import date, datetime, timedelta

DB_PATH = os.path.join('instance', 'smarthr.db')
YEAR = 2026

random.seed(2026)

# Profile per target grade:
# (att_prob, on_time_prob, ot_prob, ot_mean_hours, min_duration_h, duration_spread_h)
PROFILES = {
    'A': (0.98, 0.96, 0.45, 1.0, 8.0, 1.5),
    'B': (0.93, 0.86, 0.20, 1.0, 8.0, 1.0),
    'C': (0.80, 0.66, 0.08, 0.75, 7.0, 1.0),
    'D': (0.60, 0.52, 0.02, 0.5, 4.5, 4.0),
}

# Target yearly grade per employee; unlisted employees use FALLBACK.
GRADE_TARGETS = {
    1: 'A', 2: 'B', 3: 'B', 4: 'B', 5: 'B', 6: 'C', 7: 'B', 8: 'B',
    9: 'B', 10: 'B',
    11: 'A', 12: 'B', 13: 'C', 14: 'B', 15: 'A', 16: 'A', 17: 'B',
    18: 'B', 19: 'A', 20: 'B', 21: 'B', 22: 'B', 23: 'A', 24: 'D',
    25: 'B', 26: 'B', 27: 'C', 28: 'A', 29: 'C', 30: 'D', 31: 'A',
    32: 'B', 33: 'C', 34: 'B', 35: 'C', 36: 'B', 37: 'C', 38: 'B',
    39: 'C', 44: 'B', 45: 'B', 46: 'B', 66: 'A', 67: 'C',
}
FALLBACK_GRADES = ('A', 'B', 'B', 'C', 'C', 'D')


def _weekdays(start, end):
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _to_minutes(t):
    h, m = t.split(':')
    return int(h) * 60 + int(m)


def _time_str(total_minutes):
    total_minutes = min(total_minutes, 23 * 60 + 59)
    h, m = divmod(total_minutes, 60)
    return f'{h:02d}:{m:02d}:00'


def generate_yearly_attendance(cur):
    """Insert 2026 weekday attendance for active company-1 employees."""
    employees = cur.execute("""
        SELECT employee_id, branch_id, hire_date, COALESCE(work_start_time, '09:00') AS ws
        FROM Employee
        WHERE company_id = 1 AND is_active = 1
        ORDER BY employee_id
    """).fetchall()

    inserted = 0
    for emp in employees:
        eid = emp['employee_id']
        grade = GRADE_TARGETS.get(eid) or FALLBACK_GRADES[eid % len(FALLBACK_GRADES)]
        att_prob, on_time_prob, ot_prob, ot_mean, min_dur, spread = PROFILES[grade]
        ws_min = _to_minutes(emp['ws'])

        hire = emp['hire_date'] or f'{YEAR}-01-01'
        start = max(date(YEAR, 1, 1), datetime.strptime(hire, '%Y-%m-%d').date())
        end = date(YEAR, 12, 31)
        if start > end:
            print(f"  [SKIP] Employee #{eid} hired after {YEAR}.")
            continue

        # Per-employee RNG: re-runs generate the identical sequence of records,
        # so existing days are skipped consistently (true idempotency).
        rng = random.Random(f'seed_yearly_attendance:{eid}')

        existing = {r[0] for r in cur.execute(
            "SELECT DISTINCT substr(check_in,1,10) FROM Attendance WHERE employee_id=?",
            (eid,))}

        recs = []
        for day in _weekdays(start, end):
            ds = day.isoformat()
            # Always roll the full record first so the RNG stream stays fixed
            # across runs; only the INSERT depends on whether the day exists.
            if rng.random() > att_prob:
                continue

            if rng.random() <= on_time_prob:
                ci_min = ws_min - rng.randint(0, 20)
            else:
                max_late = 150 if grade == 'D' else 45
                ci_min = ws_min + rng.randint(5, max_late)

            duration = min_dur + rng.random() * spread
            if rng.random() <= ot_prob:
                duration += rng.random() * ot_mean * 2
            co_min = ci_min + int(round(duration * 60))

            hrs = round(min(duration, (23 * 60 + 59 - ci_min) / 60.0), 2)
            ot = round(max(0.0, hrs - 9.0), 2)
            recs.append((eid, emp['branch_id'],
                         f'{ds} {_time_str(ci_min)}', f'{ds} {_time_str(co_min)}',
                         hrs, ot, None, 'Approved', 0))

        new_recs = [r for r in recs if r[2][:10] not in existing]
        if new_recs:
            cur.executemany("""
                INSERT INTO Attendance
                (employee_id, branch_id, check_in, check_out, hours_worked,
                 overtime_hours, confidence_score, status, is_manual_entry)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, new_recs)
            inserted += len(new_recs)
            print(f"  [ATT] Employee #{eid} (target {grade}): {len(new_recs)} new day(s)")

    return inserted


def regenerate_yearly_reviews():
    """Recompute 2026 Performance_Review rows with the app's calculator."""
    from dotenv import load_dotenv
    load_dotenv()
    from app import create_app
    from app.performance.calculator import calculate_yearly_review
    from app.database import query, execute

    app = create_app()
    with app.app_context():
        employees = query(
            "SELECT employee_id FROM Employee WHERE company_id=1 AND is_active=1",
            one=False)
        count = 0
        for emp in employees:
            eid = emp['employee_id']
            result = calculate_yearly_review(eid, YEAR)
            if not result:
                continue
            execute("""
                INSERT OR REPLACE INTO Performance_Review
                (employee_id, period_year, attendance_rate, punctuality,
                 overtime_score, reliability, composite_score, grade)
                VALUES(?,?,?,?,?,?,?,?)
            """, (eid, YEAR, result['attendance_rate'], result['punctuality'],
                  result['overtime_score'], result['reliability'],
                  result['composite_score'], result['grade']))
            count += 1
        distribution = query(
            "SELECT grade, COUNT(*) AS c FROM Performance_Review WHERE period_year=? GROUP BY grade",
            (YEAR,))
        print(f"\n[OK] Regenerated {count} yearly performance review(s) for {YEAR}.")
        print(f"     Grade distribution for {YEAR}: " +
              ", ".join(f"{r['grade']}={r['c']}" for r in distribution))


def refresh_pending_year_end():
    """Refresh pending year-end increments/bonuses with the new review grades.

    Year-end proposals already generated for the year are not re-created by the
    app (it only auto-generates missing rows), so pending rows still carry the
    old grades. Update pending rows in place; approved/rejected decisions are
    left untouched.
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    policy = cur.execute(
        "SELECT * FROM Bonus_Policy WHERE company_id=1 AND year=?", (YEAR,)).fetchone()
    if policy:
        policy = dict(policy)
    else:
        policy = {'grade_A_months': 3.0, 'grade_B_months': 2.0,
                  'grade_C_months': 1.0, 'grade_D_months': 0.5}

    inc_updated = 0
    pending_inc = cur.execute("""
        SELECT si.increment_id, pr.composite_score, pr.grade
        FROM Salary_Increment si
        JOIN Performance_Review pr
          ON pr.employee_id = si.employee_id AND pr.period_year = ?
        WHERE si.period_year = ? AND si.status = 'Pending'
    """, (YEAR, YEAR)).fetchall()
    for r in pending_inc:
        cur.execute("""
            UPDATE Salary_Increment
            SET performance_score = ?, performance_grade = ?
            WHERE increment_id = ?
        """, (r['composite_score'], r['grade'], r['increment_id']))
        inc_updated += 1

    grade_months = {g: float(policy.get(f'grade_{g}_months', 0) or 0)
                    for g in ('A', 'B', 'C', 'D')}
    bonus_updated = 0
    pending_bonus = cur.execute("""
        SELECT bp.proposal_id, bp.months_worked, pr.composite_score, pr.grade,
               e.base_salary
        FROM Bonus_Proposal bp
        JOIN Performance_Review pr
          ON pr.employee_id = bp.employee_id AND pr.period_year = ?
        JOIN Employee e ON e.employee_id = bp.employee_id
        WHERE bp.period_year = ? AND bp.status = 'Pending'
    """, (YEAR, YEAR)).fetchall()
    for r in pending_bonus:
        months = grade_months.get(r['grade'], 0.5)
        mw = r['months_worked'] or 12
        full = round(r['base_salary'] * months, 2)
        prorated = round(full * (mw / 12.0), 2)
        cur.execute("""
            UPDATE Bonus_Proposal
            SET composite_score = ?, grade = ?, full_bonus_amount = ?, bonus_amount = ?
            WHERE proposal_id = ?
        """, (r['composite_score'], r['grade'], full, prorated, r['proposal_id']))
        bonus_updated += 1

    con.commit()
    con.close()
    print(f"[OK] Refreshed {inc_updated} pending increment(s) and "
          f"{bonus_updated} pending bonus(es) for {YEAR} with new grades.")


def main():
    print(f"Seeding full-year ({YEAR}) attendance for yearly performance grades...\n")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()

    inserted = generate_yearly_attendance(cur)
    con.commit()
    con.close()
    print(f"\n[OK] Inserted {inserted} attendance records for {YEAR}.")

    regenerate_yearly_reviews()
    refresh_pending_year_end()
    print("\n[DONE] Yearly performance grades for {0} now vary across employees.".format(YEAR))


if __name__ == '__main__':
    main()
