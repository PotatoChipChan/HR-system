"""
seed_increment_test_data.py – Add 20+ employees with attendance & performance
scores so the salary increment propose page has candidates for testing.

Run: python seed_increment_test_data.py
"""
import sqlite3
import os
import random
from datetime import date, timedelta
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join('instance', 'smarthr.db')

random.seed(2026)


def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()

    # ── Add a third branch if it doesn't exist ────────────────────────────────
    cur.execute("SELECT branch_id FROM Branch WHERE branch_id=3")
    if not cur.fetchone():
        cur.execute("""
            INSERT INTO Branch(branch_id,company_id,name,address,contact_no)
            VALUES(3,1,'Johor Bahru Branch',
                   'No. 28, Jalan Tun Abdul Razak, 80000 Johor Bahru, Johor',
                   '+607-234 5678')
        """)
        print("[OK] Added Johor Bahru Branch (branch_id=3)")

    # ── Add departments for new branch ────────────────────────────────────────
    new_depts = [
        (8,  3, 'Engineering'),
        (9,  3, 'Operations'),
        (10, 3, 'Finance'),
    ]
    for dept_id, br_id, name in new_depts:
        cur.execute("SELECT department_id FROM Department WHERE department_id=?", (dept_id,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO Department(department_id,branch_id,department_name) VALUES(?,?,?)",
                (dept_id, br_id, name)
            )

    # ── 20 new employees (IDs 11–30) ──────────────────────────────────────────
    employees = [
        # (id, company, branch, dept, name, ic, contact, address, dob, gender, ec_name, ec_no,
        #  position, emp_type, emp_status, hire_date, salary, role_id, email, password_hash)

        # KL Branch – Engineering
        (11, 1, 1, 1, 'Vincent Ong Kim Huat',      '850415105011', '+60127890111',
         'No 12, Jalan Ampang, 50450 KL', '1985-04-15', 'Male',
         'Ong Ah Hock', '+60127890112', 'Senior Software Engineer',
         'Full-Time', 'Active', '2019-06-01', 11000.00, 3,
         'vincent@smarthr.my', generate_password_hash('Manager@123')),

        (12, 1, 1, 1, 'Chang Mei Yee',              '900808085012', '+60127890113',
         'No 33, Jalan Tun Razak, 50400 KL', '1990-08-08', 'Female',
         'Chang Ah Moi', '+60127890114', 'Software Engineer',
         'Full-Time', 'Active', '2021-04-01', 6500.00, 4,
         'meiyee@smarthr.my', generate_password_hash('Employee@123')),

        (13, 1, 1, 1, 'Lim Kok Weng',              '940215105013', '+60127890115',
         'No 56, Jalan Puchong, 47100 Puchong', '1994-02-15', 'Male',
         'Lim Ah Seng', '+60127890116', 'Junior Developer',
         'Full-Time', 'Active', '2024-01-15', 3800.00, 4,
         'kokweng@smarthr.my', generate_password_hash('Employee@123')),

        # KL Branch – Operations
        (14, 1, 1, 3, 'Nur Aisyah binti Zulkifli', '960701086014', '+60127890117',
         'No 7, Jalan Gombak, 53000 KL', '1996-07-01', 'Female',
         'Zulkifli bin Ismail', '+60127890118', 'Operations Analyst',
         'Full-Time', 'Active', '2022-03-15', 4500.00, 4,
         'aisyah@smarthr.my', generate_password_hash('Employee@123')),

        (15, 1, 1, 3, 'Tan Wei Liang',             '880901105015', '+60127890119',
         'No 21, Jalan Ipoh, 51200 KL', '1988-09-01', 'Male',
         'Tan Khoon Hock', '+60127890120', 'Operations Manager',
         'Full-Time', 'Active', '2018-01-01', 9200.00, 3,
         'weiliang@smarthr.my', generate_password_hash('Manager@123')),

        # KL Branch – Finance
        (16, 1, 1, 4, 'Shanthi a/p Krishnan',      '870512086016', '+60127890121',
         'No 4, Jalan Bangsar, 59100 KL', '1987-05-12', 'Female',
         'Krishnan M', '+60127890122', 'Finance Manager',
         'Full-Time', 'Active', '2020-01-01', 10500.00, 3,
         'shanthi@smarthr.my', generate_password_hash('Manager@123')),

        (17, 1, 1, 4, 'Mohd Faiz bin Razak',       '920903105017', '+60127890123',
         'No 88, Jalan Cheras, 56100 KL', '1992-09-03', 'Male',
         'Razak bin Amin', '+60127890124', 'Accountant',
         'Full-Time', 'Active', '2023-06-01', 5200.00, 4,
         'faiz@smarthr.my', generate_password_hash('Employee@123')),

        # KL Branch – HR
        (18, 1, 1, 2, 'Goh Sook Ting',             '950314086018', '+60127890125',
         'No 15, Jalan Kuchai Lama, 58200 KL', '1995-03-14', 'Female',
         'Goh Kim Swee', '+60127890126', 'HR Executive',
         'Full-Time', 'Active', '2021-09-01', 4200.00, 2,
         'sookting@smarthr.my', generate_password_hash('Employee@123')),

        # Penang Branch – Engineering
        (19, 1, 2, 6, 'Ng Chee Seng',              '830620105019', '+60127890127',
         'No 9, Jalan Nagor, 10050 Penang', '1983-06-20', 'Male',
         'Ng Ah Hock', '+60127890128', 'Engineering Manager',
         'Full-Time', 'Active', '2017-04-01', 13000.00, 3,
         'cheeseng@smarthr.my', generate_password_hash('Manager@123')),

        (20, 1, 2, 6, 'Lee Ee Lyn',                '910507086020', '+60127890129',
         'No 66, Jalan Burma, 10350 Penang', '1991-05-07', 'Female',
         'Lee Kim Soon', '+60127890130', 'Embedded Engineer',
         'Full-Time', 'Active', '2020-10-01', 6000.00, 4,
         'eelyn@smarthr.my', generate_password_hash('Employee@123')),

        # Penang Branch – Operations
        (21, 1, 2, 7, 'Wong Jin Ming',             '970812105021', '+60127890131',
         'No 3, Jalan Perak, 11600 Penang', '1997-08-12', 'Male',
         'Wong Fook Yew', '+60127890132', 'Logistics Coordinator',
         'Full-Time', 'Active', '2023-11-01', 3600.00, 4,
         'jinming@smarthr.my', generate_password_hash('Employee@123')),

        (22, 1, 2, 7, 'Siti Rahmah binti Hamid',   '940226086022', '+60127890133',
         'No 18, Jalan Sultan, 10050 Penang', '1994-02-26', 'Female',
         'Hamid bin Osman', '+60127890134', 'Customer Service Lead',
         'Full-Time', 'Active', '2019-07-15', 4900.00, 4,
         'siti_rahmah@smarthr.my', generate_password_hash('Employee@123')),

        # Johor Branch – Engineering
        (23, 1, 3, 8, 'Tan Sri Dato\' Kevin Loh',  '750101015023', '+60127890135',
         'No 1, Jalan Skudai, 80100 Johor Bahru', '1975-01-01', 'Male',
         'Loh Fook Hin', '+60127890136', 'Director of Engineering',
         'Full-Time', 'Active', '2015-01-01', 15000.00, 3,
         'kevin_loh@smarthr.my', generate_password_hash('Manager@123')),

        (24, 1, 3, 8, 'Fatin Amira binti Azman',   '980413086024', '+60127890137',
         'No 22, Jalan Tebrau, 80250 Johor Bahru', '1998-04-13', 'Female',
         'Azman bin Saad', '+60127890138', 'Junior IT Executive',
         'Contract', 'Active', '2025-01-01', 3000.00, 4,
         'fatin@smarthr.my', generate_password_hash('Employee@123')),

        # Johor Branch – Operations
        (25, 1, 3, 9, 'Mohan a/l Ravi',            '930610105025', '+60127890139',
         'No 45, Jalan Melodies, 80250 Johor Bahru', '1993-06-10', 'Male',
         'Ravi a/l Samy', '+60127890140', 'Operations Executive',
         'Full-Time', 'Active', '2022-04-01', 4000.00, 4,
         'mohan@smarthr.my', generate_password_hash('Employee@123')),

        # Johor Branch – Finance
        (26, 1, 3, 10, 'Khoo Beng Teik',            '860115105026', '+60127890141',
         'No 77, Jalan Johor, 80300 Johor Bahru', '1986-01-15', 'Male',
         'Khoo Cheng Hean', '+60127890142', 'Finance Executive',
         'Full-Time', 'Active', '2020-06-01', 5800.00, 4,
         'bengteik@smarthr.my', generate_password_hash('Employee@123')),

        (27, 1, 3, 10, 'Norhayati binti Ismail',    '950529086027', '+60127890143',
         'No 11, Jalan Stulang, 80300 Johor Bahru', '1995-05-29', 'Female',
         'Ismail bin Jusoh', '+60127890144', 'Accounts Assistant',
         'Full-Time', 'Active', '2024-03-01', 3500.00, 4,
         'norhayati@smarthr.my', generate_password_hash('Employee@123')),

        # Additional spread
        (28, 1, 1, 1, 'Raja Iskandar bin Raja Ahmad', '890710105028', '+60127890145',
         'No 10, Jalan Setiawangsa, 54200 KL', '1989-07-10', 'Male',
         'Raja Ahmad bin Raja Ali', '+60127890146', 'DevOps Engineer',
         'Full-Time', 'Active', '2021-01-01', 7500.00, 4,
         'iskandar@smarthr.my', generate_password_hash('Employee@123')),

        (29, 1, 2, 7, 'Teoh Pey Ching',             '960822086029', '+60127890147',
         'No 5, Jalan Bagan Jermal, 10250 Penang', '1996-08-22', 'Female',
         'Teoh Leng Choo', '+60127890148', 'Administrative Assistant',
         'Part-Time', 'Active', '2023-05-01', 2600.00, 4,
         'peyching@smarthr.my', generate_password_hash('Employee@123')),

        # Extra employees
        (30, 1, 1, 4, 'Nurhidayah binti Sulaiman',   '970216086030', '+60127890149',
         'No 29, Jalan Duta, 50480 KL', '1997-02-16', 'Female',
         'Sulaiman bin Hashim', '+60127890150', 'Finance Intern',
         'Contract', 'Active', '2025-01-15', 2500.00, 4,
         'hidayah@smarthr.my', generate_password_hash('Employee@123')),

        (31, 1, 3, 9, 'Ahmad Fauzi bin Mohd Noor',   '880715105031', '+60127890151',
         'No 14, Jalan Larkin, 80350 Johor Bahru', '1988-07-15', 'Male',
         'Mohd Noor bin Yusof', '+60127890152', 'Operations Manager',
         'Full-Time', 'Active', '2019-08-01', 8200.00, 3,
         'fauzi@smarthr.my', generate_password_hash('Manager@123')),

        (32, 1, 3, 8, 'Lim Siew Ling',               '920411086032', '+60127890153',
         'No 8, Jalan Permas, 81750 Masai', '1992-04-11', 'Female',
         'Lim Weng Fatt', '+60127890154', 'QA Engineer',
         'Full-Time', 'Active', '2021-11-01', 4800.00, 4,
         'siewling@smarthr.my', generate_password_hash('Employee@123')),

        (33, 1, 1, 3, 'Ravi a/l Muthusamy',          '930812105033', '+60127890155',
         'No 6, Jalan Sentul, 51000 KL', '1993-08-12', 'Male',
         'Muthusamy a/l Perumal', '+60127890156', 'Customer Service Executive',
         'Full-Time', 'Active', '2022-06-01', 3700.00, 4,
         'ravi@smarthr.my', generate_password_hash('Employee@123')),

        (34, 1, 2, 6, 'Chong Li Ken',                 '950106105034', '+60127890157',
         'No 12, Jalan Macalister, 10400 Penang', '1995-01-06', 'Male',
         'Chong Weng Keong', '+60127890158', 'Hardware Engineer',
         'Full-Time', 'Active', '2020-02-01', 5500.00, 4,
         'liken@smarthr.my', generate_password_hash('Employee@123')),

        (35, 1, 1, 2, 'Sharifah Nadira binti Syed Ariffin', '940801086035', '+60127890159',
         'No 19, Jalan Wangsa Maju, 53300 KL', '1994-08-01', 'Female',
         'Syed Ariffin bin Syed Ahmad', '+60127890160', 'Training Coordinator',
         'Full-Time', 'Active', '2022-08-01', 3900.00, 4,
         'nadira@smarthr.my', generate_password_hash('Employee@123')),

        (36, 1, 1, 5, 'Mohd Hafizuddin bin Azman',   '900516105036', '+60127890161',
         'No 2, Jalan Kuching, 51200 KL', '1990-05-16', 'Male',
         'Azman bin Ibrahim', '+60127890162', 'Admin Executive',
         'Full-Time', 'Active', '2021-03-01', 4200.00, 4,
         'hafizuddin@smarthr.my', generate_password_hash('Employee@123')),
    ]

    inserted = 0
    for emp in employees:
        eid = emp[0]
        cur.execute("SELECT employee_id FROM Employee WHERE employee_id=?", (eid,))
        if cur.fetchone():
            continue
        cur.execute("""
            INSERT INTO Employee
            (employee_id,company_id,branch_id,department_id,full_name,ic_number,contact_no,
             address,date_of_birth,gender,emergency_contact_name,emergency_contact_no,
             position,employment_type,employment_status,hire_date,base_salary,
             role_id,email,password_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, emp)
        # Grant role permissions for new employee
        cur.execute("SELECT permission_id FROM Role_Permission WHERE role_id=?", (emp[17],))
        for perm in cur.fetchall():
            cur.execute("INSERT OR IGNORE INTO Employee_Permission(employee_id,permission_id,is_active,reason) VALUES(?,?,1,'seed_assignment')",
                       (eid, perm[0]))
        # Create leave balances for the new employee
        for lt_id in (1, 2, 3):
            entitled = 14
            if lt_id == 3: entitled = 3
            cur.execute("INSERT OR IGNORE INTO Leave_Balance(employee_id,leave_type_id,year,entitled_days,used_days,pending_days) VALUES(?,?,2026,?,0,0)",
                       (eid, lt_id, entitled))
        inserted += 1
        print(f"  [EMP] Added employee #{eid} – {emp[4]}")

    print(f"[OK] Inserted {inserted} new employees.")

    con.commit()

    # ── Generate May 2026 attendance for new employees ─────────────────────
    may_att = generate_may_attendance(cur)
    con.commit()
    print(f"[OK] Generated {len(may_att)} attendance records for May 2026.")

    # ── Compute performance scores from attendance ─────────────────────────
    perf_count = generate_performance_scores(cur)
    con.commit()
    print(f"[OK] Generated {perf_count} performance score records.")

    # ── Create a few sample Salary_Increment proposals for testing ─────────
    inc_count = create_sample_increments(cur)
    con.commit()
    print(f"[OK] Created {inc_count} salary increment proposals (mix of statuses).")

    # ── Grant HR Manager all permissions (like Admin) ──────────────────────
    migrate_hr_manager_permissions(cur)
    con.commit()
    print("[OK] HR Manager role now has all Admin-level permissions.")

    con.close()
    print("\n[DONE] Test data ready for salary increment module.")
    print("  -> Login as admin@smarthr.my (Admin@123) or hr@smarthr.my (Hr@123)")
    print("  -> Navigate to Salary Increment -> Propose to see 15+ eligible employees.")
    print("  -> The List page also has some pending/approved/rejected samples to test.")


def generate_may_attendance(cur):
    """Create May 2026 attendance for employees 11–30 with varied patterns."""
    from datetime import date, timedelta
    import random as rnd
    rnd.seed(2026)

    # Get active new employees (ID 11–30)
    employees = cur.execute(
        "SELECT employee_id, branch_id FROM Employee WHERE employee_id BETWEEN 11 AND 36 AND is_active=1"
    ).fetchall()

    # Compute weekdays of May 2026 (skip May-Day holiday on May 1)
    weekdays = []
    d = date(2026, 5, 1)
    while d.month == 5:
        if d.weekday() < 5 and d.day != 1:
            weekdays.append(d)
        d += timedelta(days=1)

    att_records = []
    # Assign a "dependability" factor per employee (affects attendance rate)
    emp_factors = {e['employee_id']: rnd.uniform(0.75, 1.0) for e in employees}

    for emp in employees:
        eid = emp['employee_id']
        bid = emp['branch_id']
        factor = emp_factors[eid]

        for day in weekdays:
            # Weighted attendance: more dependable employees attend ~95%, less ~80%
            if rnd.random() > (0.85 * factor):
                continue

            # Realistic check-in times
            late_minutes = rnd.choices(
                [0, 0, 0, 0, 5, 5, 10, 15, 20, 30],
                weights=[30, 15, 10, 10, 10, 8, 7, 5, 3, 2]
            )[0]
            ci_h, ci_m = 9, late_minutes
            ci_str = f"{day.isoformat()} {ci_h:02d}:{ci_m:02d}:00"

            # Check-out: generally ~8h work, some OT
            ot = rnd.choices(
                [0, 0, 0, 0.25, 0.5, 0.5, 1.0, 1.5, 2.0],
                weights=[30, 20, 15, 10, 8, 7, 5, 3, 2]
            )[0]
            co_h, co_m = 18 + int(ot), int((ot % 1) * 60)
            # Handle if OT pushes to next day (cap at 23:59)
            if co_h >= 24:
                co_h, co_m = 23, 59
            co_str = f"{day.isoformat()} {co_h:02d}:{co_m:02d}:00"

            hrs = round((co_h + co_m / 60) - (ci_h + ci_m / 60), 2)
            ot_hrs = round(max(0, hrs - 9), 2)
            att_records.append((eid, bid, ci_str, co_str, hrs, ot_hrs, None, 'Approved', 0))

    if att_records:
        cur.executemany("""
            INSERT INTO Attendance
            (employee_id, branch_id, check_in, check_out, hours_worked, overtime_hours,
             confidence_score, status, is_manual_entry)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, att_records)

    return att_records


def generate_performance_scores(cur):
    """Calculate & insert performance scores for employees 11–30 from May 2026 attendance."""
    from datetime import date
    import calendar

    month, year = 5, 2026

    def working_days(m, y):
        total = 0
        num = calendar.monthrange(y, m)[1]
        for d in range(1, num + 1):
            dt = date(y, m, d)
            if dt.weekday() < 5 and dt.day != 1:  # skip May 1 holiday
                total += 1
        return total

    total_wd = working_days(month, year)
    if total_wd == 0:
        return 0

    employees = cur.execute(
        "SELECT DISTINCT employee_id FROM Attendance WHERE employee_id BETWEEN 11 AND 36 AND status='Approved'"
    ).fetchall()

    count = 0
    for emp in employees:
        eid = emp['employee_id']
        cur.execute("""
            SELECT * FROM Attendance
            WHERE employee_id=? AND strftime('%m', check_in)=? AND strftime('%Y', check_in)=?
              AND status='Approved'
            ORDER BY check_in
        """, (eid, f"{month:02d}", str(year)))
        recs = cur.fetchall()
        if not recs:
            continue

        work_start = '09:00'
        cur.execute("SELECT work_start_time FROM Employee WHERE employee_id=?", (eid,))
        row = cur.fetchone()
        if row and row['work_start_time']:
            work_start = row['work_start_time']

        days_present = set()
        on_time_count = 0
        total_ot = 0.0
        manual_count = 0
        total = len(recs)

        for r in recs:
            day = r['check_in'][:10]
            days_present.add(day)
            if r['check_in'][11:16] <= work_start:
                on_time_count += 1
            total_ot += r['overtime_hours'] or 0
            if r['is_manual_entry']:
                manual_count += 1

        att_rate = min(100.0, (len(days_present) / total_wd) * 100)
        punct = (on_time_count / len(days_present) * 100) if days_present else 0
        ot_score = min(100.0, (total_ot / 40.0) * 100)
        reliability = (1 - manual_count / total) * 100 if total > 0 else 100
        composite = att_rate * 0.40 + punct * 0.30 + ot_score * 0.15 + reliability * 0.15

        if composite >= 85:
            grade = 'A'
        elif composite >= 70:
            grade = 'B'
        elif composite >= 55:
            grade = 'C'
        else:
            grade = 'D'

        cur.execute("""
            INSERT OR REPLACE INTO Performance_Score
            (employee_id, period_month, period_year, attendance_rate, punctuality,
             overtime_score, reliability, composite_score, grade)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (eid, month, year, round(att_rate, 2), round(punct, 2),
              round(ot_score, 2), round(reliability, 2), round(composite, 2), grade))
        count += 1

    return count


def create_sample_increments(cur):
    """Add a few Salary_Increment records in various statuses for testing."""
    uid = 1  # admin user
    year = 2026
    now = "datetime('now')"

    # Find admin user
    admin = cur.execute(
        "SELECT employee_id FROM Employee WHERE role_id=1 LIMIT 1"
    ).fetchone()
    if admin:
        uid = admin['employee_id']

    # Create pending increments for employees across branches
    pending_targets = [11, 15, 19, 23, 28, 31, 33]
    pending_count = 0
    for eid in pending_targets:
        emp = cur.execute("SELECT employee_id, base_salary FROM Employee WHERE employee_id=?", (eid,)).fetchone()
        if not emp:
            continue
        old = emp['base_salary']
        pct = 6.0
        new_sal = round(old * (1 + pct / 100))

        # Check for duplicate
        existing = cur.execute(
            "SELECT increment_id FROM Salary_Increment WHERE employee_id=? AND period_year=?",
            (eid, year)
        ).fetchone()
        if existing:
            continue

        # Get performance score
        perf = cur.execute("""
            SELECT composite_score, grade FROM Performance_Score
            WHERE employee_id=? AND period_year=? ORDER BY period_month DESC LIMIT 1
        """, (eid, year)).fetchone()

        cur.execute("""
            INSERT INTO Salary_Increment
            (employee_id, period_year, old_salary, new_salary, increment_pct,
             performance_score, performance_grade, proposed_by, proposed_at, status)
            VALUES(?,?,?,?,?,?,?,?,datetime('now'),'Pending')
        """, (eid, year, old, new_sal, pct,
              perf['composite_score'] if perf else None,
              perf['grade'] if perf else None,
              uid))
        pending_count += 1
        print(f"  [INC] Pending increment for employee #{eid}: RM {old:,.2f} -> RM {new_sal:,.2f}")

    # Create approved increments for employees
    approved_targets = [13, 20, 25, 32, 35]
    approved_count = 0
    for eid in approved_targets:
        emp = cur.execute("SELECT employee_id, base_salary FROM Employee WHERE employee_id=?", (eid,)).fetchone()
        if not emp:
            continue
        old = emp['base_salary']
        pct = 5.0
        new_sal = round(old * (1 + pct / 100))

        existing = cur.execute(
            "SELECT increment_id FROM Salary_Increment WHERE employee_id=? AND period_year=?",
            (eid, year)
        ).fetchone()
        if existing:
            continue

        perf = cur.execute("""
            SELECT composite_score, grade FROM Performance_Score
            WHERE employee_id=? AND period_year=? ORDER BY period_month DESC LIMIT 1
        """, (eid, year)).fetchone()

        cur.execute("""
            INSERT INTO Salary_Increment
            (employee_id, period_year, old_salary, new_salary, increment_pct,
             performance_score, performance_grade, proposed_by, proposed_at,
             status, reviewed_by, reviewed_at, notified_at)
            VALUES(?,?,?,?,?,?,?,?,datetime('now'),
                   'Approved',?,datetime('now'),datetime('now'))
        """, (eid, year, old, new_sal, pct,
              perf['composite_score'] if perf else None,
              perf['grade'] if perf else None,
              uid, uid))
        # Also update base_salary
        cur.execute("UPDATE Employee SET base_salary=? WHERE employee_id=?", (new_sal, eid))
        approved_count += 1
        print(f"  [INC] Approved increment for employee #{eid}: RM {old:,.2f} -> RM {new_sal:,.2f}")

    # Create rejected increment for employee 30
    rejected_count = 0
    eid = 30
    emp = cur.execute("SELECT employee_id, base_salary FROM Employee WHERE employee_id=?", (eid,)).fetchone()
    if emp:
        old = emp['base_salary']
        pct = 4.0
        new_sal = round(old * (1 + pct / 100))

        existing = cur.execute(
            "SELECT increment_id FROM Salary_Increment WHERE employee_id=? AND period_year=?",
            (eid, year)
        ).fetchone()
        if not existing:
            perf = cur.execute("""
                SELECT composite_score, grade FROM Performance_Score
                WHERE employee_id=? AND period_year=? ORDER BY period_month DESC LIMIT 1
            """, (eid, year)).fetchone()

            cur.execute("""
                INSERT INTO Salary_Increment
                (employee_id, period_year, old_salary, new_salary, increment_pct,
                 performance_score, performance_grade, proposed_by, proposed_at,
                 status, reviewed_by, reviewed_at, rejection_reason)
                VALUES(?,?,?,?,?,?,?,?,datetime('now'),
                       'Rejected',?,datetime('now'),'Performance below expectations for this cycle')
            """, (eid, year, old, new_sal, pct,
                  perf['composite_score'] if perf else None,
                  perf['grade'] if perf else None,
                  uid, uid))
            rejected_count += 1
            print(f"  [INC] Rejected increment for employee #{eid}: RM {old:,.2f} -> RM {new_sal:,.2f} (rejected)")

    return pending_count + approved_count + rejected_count


def migrate_hr_manager_permissions(cur):
    """Grant HR Manager role all Admin-level permissions (in-place migration)."""
    # 1. Find HR Manager role_id
    cur.execute("SELECT role_id FROM Role WHERE role_name='HR Manager'")
    row = cur.fetchone()
    if not row:
        print("[SKIP] HR Manager role not found.")
        return
    hr_mgr_role_id = row['role_id']

    # 2. Get all permission IDs
    cur.execute("SELECT permission_id FROM Permission")
    all_perms = [p['permission_id'] for p in cur.fetchall()]

    # 3. Grant each to HR Manager role (ignore duplicates)
    for pid in all_perms:
        cur.execute("INSERT OR IGNORE INTO Role_Permission(role_id, permission_id) VALUES(?,?)",
                   (hr_mgr_role_id, pid))

    # 4. Update existing HR Manager employees: make sure they have all permissions active
    cur.execute("SELECT employee_id FROM Employee WHERE role_id=?", (hr_mgr_role_id,))
    hr_mgr_emps = cur.fetchall()
    for emp in hr_mgr_emps:
        eid = emp['employee_id']
        for pid in all_perms:
            # Try to activate if already exists
            cur.execute("""
                UPDATE Employee_Permission SET is_active=1, revoked_at=NULL, reason='migration_hr_mgr_full'
                WHERE employee_id=? AND permission_id=?
            """, (eid, pid))
            if cur.rowcount == 0:
                # Insert new record
                cur.execute("""
                    INSERT INTO Employee_Permission(employee_id, permission_id, is_active, reason)
                    VALUES(?,?,1,'migration_hr_mgr_full')
                """, (eid, pid))
    print(f"[OK] Updated {len(hr_mgr_emps)} HR Manager employee(s) with Admin-level permissions.")


if __name__ == '__main__':
    print("Seeding test data for salary increment module...\n")
    main()
