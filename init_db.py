"""
init_db.py  –  Create or update the SQLite database.
Usage: python init_db.py
Safe to re-run on existing databases - uses IF NOT EXISTS / INSERT OR IGNORE.
"""
import sqlite3
import os
from werkzeug.security import generate_password_hash
from datetime import datetime, date

DB_PATH = os.path.join('instance', 'smarthr.db')
SCHEMA_PATH = 'schema.sql'

def get_connection():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con

def migrate_add_branch_address_fields():
    """Add new address fields to existing Branch table if they don't exist."""
    con = get_connection()
    cur = con.cursor()
    
    # Check if columns exist
    cur.execute("PRAGMA table_info(Branch)")
    columns = [row[1] for row in cur.fetchall()]
    
    # Add missing columns
    if 'address_line1' not in columns:
        print("[MIGRATION] Adding address_line1 column to Branch...")
        cur.execute("ALTER TABLE Branch ADD COLUMN address_line1 TEXT")
    if 'address_line2' not in columns:
        print("[MIGRATION] Adding address_line2 column to Branch...")
        cur.execute("ALTER TABLE Branch ADD COLUMN address_line2 TEXT")
    if 'city' not in columns:
        print("[MIGRATION] Adding city column to Branch...")
        cur.execute("ALTER TABLE Branch ADD COLUMN city TEXT")
    if 'state' not in columns:
        print("[MIGRATION] Adding state column to Branch...")
        cur.execute("ALTER TABLE Branch ADD COLUMN state TEXT")
    if 'postal_code' not in columns:
        print("[MIGRATION] Adding postal_code column to Branch...")
        cur.execute("ALTER TABLE Branch ADD COLUMN postal_code TEXT")
    
    con.commit()
    con.close()
    print("[OK] Branch table migration completed.")


def migrate_increment_policy_table():
    """Create Increment_Policy table if it doesn't exist (for existing databases)."""
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS Increment_Policy (
            policy_id              INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id             INTEGER NOT NULL,
            increment_pct          REAL NOT NULL DEFAULT 5.0,
            tenure_threshold_years INTEGER NOT NULL DEFAULT 1,
            effective_month        INTEGER NOT NULL DEFAULT 1,
            effective_year         INTEGER,
            auto_propose           INTEGER NOT NULL DEFAULT 1,
            created_at             TEXT DEFAULT (datetime('now')),
            updated_at             TEXT DEFAULT (datetime('now')),
            UNIQUE (company_id),
            FOREIGN KEY (company_id) REFERENCES Company(company_id)
        )
    """)
    con.commit()
    con.close()
    print("[OK] Increment_Policy table migration completed.")


def migrate_department_manager_id():
    """Add department_manager_id column to Department table if it doesn't exist."""
    con = get_connection()
    cur = con.cursor()
    cur.execute("PRAGMA table_info(Department)")
    columns = [row[1] for row in cur.fetchall()]
    if 'department_manager_id' not in columns:
        print("[MIGRATION] Adding department_manager_id column to Department...")
        cur.execute("ALTER TABLE Department ADD COLUMN department_manager_id INTEGER REFERENCES Employee(employee_id)")
    con.commit()
    con.close()
    print("[OK] Department manager migration completed.")


def backfill_position_links(con=None):
    """Populate the Position catalog from existing free-text titles and link
    Employee / Job_Posting rows to catalog entries.

    - Titles are normalized (trim + collapse inner whitespace); case-insensitive
      duplicates within the same department are merged (deterministic first-seen
      casing: the earliest-created row wins, ties broken by source then id).
    - Titles with NULL/unknown department are NOT auto-created -- they stay
      unlinked so HR can review them manually.
    - Posting titles that originated from CUSTOM-approved vacancy requests
      (is_custom=1) are EXCLUDED from the source scan: a re-run must never
      promote custom titles into the catalog (HR decides that at approval
      time). Legacy postings with no origin request are still backfilled.
    - Idempotent: safe to re-run at any time (INSERT OR IGNORE).
    """
    own = con is None
    if own:
        con = get_connection()
    cur = con.cursor()

    rows = cur.execute("""
        SELECT name, department_id FROM (
            SELECT position AS name, department_id, created_at, employee_id AS id, 0 AS src
            FROM Employee
            WHERE position IS NOT NULL AND TRIM(position) <> '' AND department_id IS NOT NULL
            UNION ALL
            SELECT Job_Posting.title AS name, Job_Posting.department_id,
                   Job_Posting.created_at, Job_Posting.posting_id AS id, 1 AS src
            FROM Job_Posting
            LEFT JOIN Vacancy_Request vr ON vr.posting_id = Job_Posting.posting_id
            WHERE Job_Posting.title IS NOT NULL AND TRIM(Job_Posting.title) <> ''
              AND Job_Posting.department_id IS NOT NULL
              AND (vr.posting_id IS NULL OR vr.is_custom = 0)
        )
        ORDER BY (created_at IS NULL), created_at, src, id
    """).fetchall()

    seen = {}
    for r in rows:
        norm = ' '.join(r['name'].split())
        key = (r['department_id'], norm.lower())
        if key not in seen:
            seen[key] = norm

    created = 0
    for (dept_id, _lname), name in seen.items():
        cur.execute("INSERT OR IGNORE INTO Position(position_name, department_id) VALUES(?,?)",
                    (name, dept_id))
        if cur.rowcount:
            created += 1

    # Link employees to the catalog (case-insensitive name match within the same department)
    cur.execute("""
        UPDATE Employee
        SET position_id = (
            SELECT p.position_id FROM Position p
            WHERE p.department_id = Employee.department_id
              AND LOWER(p.position_name) = LOWER(TRIM(Employee.position))
            LIMIT 1
        )
        WHERE Employee.position IS NOT NULL AND TRIM(Employee.position) <> ''
    """)

    # Link job postings to the catalog
    cur.execute("""
        UPDATE Job_Posting
        SET position_id = (
            SELECT p.position_id FROM Position p
            WHERE p.department_id = Job_Posting.department_id
              AND LOWER(p.position_name) = LOWER(TRIM(Job_Posting.title))
            LIMIT 1
        )
        WHERE Job_Posting.title IS NOT NULL AND TRIM(Job_Posting.title) <> ''
    """)

    con.commit()
    if own:
        con.close()
    if created:
        print(f"[MIGRATION] Position catalog backfilled: {created} new rows created from existing titles.")


POSITION_DDL = """
    CREATE TABLE IF NOT EXISTS Position (
        position_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        position_name TEXT NOT NULL CHECK(TRIM(position_name) = position_name
                                          AND position_name NOT LIKE '%  %'
                                          AND length(TRIM(position_name)) > 0),
        department_id INTEGER NOT NULL,
        is_active     INTEGER DEFAULT 1,
        created_at    TEXT DEFAULT (datetime('now')),
        UNIQUE (department_id, position_name COLLATE NOCASE),
        FOREIGN KEY (department_id) REFERENCES Department(department_id)
    )
"""


def _rebuild_position_table(con, cur):
    """Upgrade an old Position table (plain UNIQUE, no CHECK) to the hardened
    schema. SQLite cannot ALTER constraints, so the table is rebuilt:

    - position_id values are preserved 1:1, so existing Employee /
      Job_Posting / Vacancy_Request links stay valid.
    - Rows are normalized (trim + collapse inner whitespace) in Python first;
      rows that normalize to the same (department, name) are merged into the
      earliest-created survivor and every link is re-pointed to it.
    - FK enforcement is suspended only inside this rebuild and re-enabled after.
    - Idempotent: skips when the table already has COLLATE NOCASE.
    """
    row = cur.execute("""SELECT sql FROM sqlite_master
                         WHERE type='table' AND name='Position'""").fetchone()
    if row and 'COLLATE NOCASE' in (row['sql'] or '') and 'NOT LIKE' in (row['sql'] or ''):
        return

    rows = cur.execute("""
        SELECT position_id, position_name, department_id, is_active, created_at
        FROM Position ORDER BY position_id
    """).fetchall()

    survivors = {}
    for r in rows:
        name = ' '.join((r['position_name'] or '').split())
        if not name:
            raise RuntimeError(
                f"[MIGRATION] Position id {r['position_id']} has an empty title; "
                "fix the data manually before re-running init_db.py")
        key = (r['department_id'], name.lower())
        survivors.setdefault(key, []).append((r, name))

    merged = 0
    insert_rows = []
    for entries in survivors.values():
        if len(entries) > 1:
            entries.sort(key=lambda e: (e[0]['created_at'] or '', e[0]['position_id']))
            keep, rest = entries[0], entries[1:]
            for r, _name in rest:
                for table in ('Employee', 'Job_Posting', 'Vacancy_Request'):
                    cur.execute(f"UPDATE {table} SET position_id=? WHERE position_id=?",
                                (keep[0]['position_id'], r['position_id']))
                merged += 1
        else:
            keep = entries[0]
        insert_rows.append(keep)
    if merged:
        print(f"[MIGRATION] Position rebuild: {merged} normalized duplicates merged.")
    con.commit()

    cur.execute("PRAGMA foreign_keys = OFF")
    cur.execute("DROP TABLE IF EXISTS Position_new")
    cur.execute(POSITION_DDL.replace('Position', 'Position_new'))
    cur.executemany(
        """INSERT INTO Position_new
           (position_id, position_name, department_id, is_active, created_at)
           VALUES (?,?,?,?,?)""",
        [(r['position_id'], name, r['department_id'], r['is_active'], r['created_at'])
         for (r, name) in insert_rows])
    cur.execute("DROP TABLE Position")
    cur.execute("ALTER TABLE Position_new RENAME TO Position")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_position_active ON Position(is_active)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_position_dept_active ON Position(department_id, is_active)")
    con.commit()
    cur.execute("PRAGMA foreign_keys = ON")
    print("[MIGRATION] Position table rebuilt with COLLATE NOCASE + CHECK constraints.")


def migrate_position_catalog():
    """Create the Position catalog table, add link columns to existing tables,
    and backfill catalog rows + links from existing free-text titles.
    Idempotent – safe to re-run on existing databases."""
    con = get_connection()
    cur = con.cursor()

    cur.execute(POSITION_DDL)
    _rebuild_position_table(con, cur)

    adds = [
        ('Employee',       'position_id', 'INTEGER REFERENCES Position(position_id)'),
        ('Job_Posting',    'position_id', 'INTEGER REFERENCES Position(position_id)'),
        ('Vacancy_Request','position_id', 'INTEGER REFERENCES Position(position_id)'),
        ('Vacancy_Request','is_custom',   'INTEGER DEFAULT 0'),
    ]
    for table, col, ddl in adds:
        cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]
        if col not in cols:
            print(f"[MIGRATION] Adding {table}.{col} ...")
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    indexes = [
        ('idx_position_active',            'Position',       'is_active'),
        ('idx_position_dept_active',       'Position',       'department_id, is_active'),
        ('idx_employee_position',          'Employee',       'position_id'),
        ('idx_job_posting_position',       'Job_Posting',    'position_id'),
        ('idx_vacancy_request_position',   'Vacancy_Request','position_id'),
    ]
    for idx, table, col in indexes:
        cur.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {table}({col})")
    cur.execute("DROP INDEX IF EXISTS idx_position_department")

    con.commit()
    con.close()

    backfill_position_links()
    print("[OK] Position catalog migration completed.")


def seed_department_managers():
    """Assign demo department managers by (email, department name, branch) so the
    department-manager flow (vacancy requests, dept-scoped approvals) works out of the box.
    Idempotent – re-runs safely and skips employees/departments that don't exist."""
    con = get_connection()
    cur = con.cursor()
    assignments = [
        # (manager email, department name, branch name)
        ('hafiz@smarthr.my',   'Engineering', 'Penang Office'),
        ('shanthi@smarthr.my', 'Finance',     'KL Headquarters'),
        ('vincent@smarthr.my', 'Engineering', 'KL Headquarters'),
        ('fauzi@smarthr.my',   'Operations',  'Johor Bahru Branch'),
    ]
    updated = 0
    for email, dept_name, branch_name in assignments:
        emp = cur.execute("SELECT employee_id FROM Employee WHERE email=?", (email,)).fetchone()
        if not emp:
            print(f"[SEED] Skipping department manager seed for {email} – employee not found.")
            continue
        dept = cur.execute("""
            SELECT d.department_id FROM Department d
            JOIN Branch b ON d.branch_id=b.branch_id
            WHERE d.department_name=? AND b.name=?
        """, (dept_name, branch_name)).fetchone()
        if not dept:
            print(f"[SEED] Skipping department manager seed for {email} – department '{dept_name}' ({branch_name}) not found.")
            continue
        cur.execute("UPDATE Department SET department_manager_id=? WHERE department_id=?",
                    (emp['employee_id'], dept['department_id']))
        updated += 1
    con.commit()
    con.close()
    print(f"[OK] Department managers seeded ({updated} assigned).")


BRANCH_LABELS = {1: 'KL', 2: 'Penang', 3: 'Johor Bahru'}
BRANCH_MANAGER_MAP = [
    (1, 'weiliang@smarthr.my'),
    (2, 'cheeseng@smarthr.my'),
    (3, 'kevin_loh@smarthr.my'),
]
C_SUITE_RECRUITS = [
    # (email, full_name, position_name, hire_date, base_salary)
    ('coo@smarthr.my', 'Alex Chen', 'Chief Operating Officer', '2020-03-15', 15000.00),
    ('cfo@smarthr.my', 'Priya Raj', 'Chief Financial Officer', '2020-03-15', 14500.00),
]


def _get_or_create_dept(cur, branch_id, dept_name):
    row = cur.execute("SELECT department_id FROM Department WHERE branch_id=? AND department_name=?",
                      (branch_id, dept_name)).fetchone()
    if row:
        return row['department_id']
    cur.execute("INSERT INTO Department(branch_id, department_name) VALUES(?,?)",
                (branch_id, dept_name))
    print(f"[SEED] Department '{dept_name}' created (branch {branch_id}).")
    return cur.lastrowid


def _get_or_create_position(cur, dept_id, position_name):
    name = ' '.join(position_name.split())
    row = cur.execute("""SELECT position_id FROM Position
                         WHERE department_id=? AND LOWER(position_name)=LOWER(?)""",
                      (dept_id, name)).fetchone()
    if row:
        return row['position_id']
    cur.execute("INSERT INTO Position(position_name, department_id) VALUES(?,?)",
                (name, dept_id))
    print(f"[SEED] Position '{name}' created (dept {dept_id}).")
    return cur.lastrowid


def cleanup_demo_artifacts():
    """Remove leftover demo/test artifacts from earlier development:
    branch "New Branch" plus its orphan departments/employees/positions.
    Idempotent - no-op (and prints nothing) once they are gone.

    The artifact employees may have accumulated dependent rows from earlier
    test runs (attendance, payroll, permissions, ...), so FK enforcement is
    suspended for the duration of this cleanup only."""
    con = get_connection()
    cur = con.cursor()
    branch_ids = [r['branch_id'] for r in cur.execute(
        "SELECT branch_id FROM Branch WHERE name='New Branch'").fetchall()]
    dept_ids = [r['department_id'] for r in cur.execute(
        "SELECT department_id FROM Department WHERE department_name IN ('New Department','New Department 2.0')").fetchall()]
    if not branch_ids and not dept_ids:
        con.close()
        return
    emp_ids = [r['employee_id'] for r in cur.execute(
        "SELECT employee_id FROM Employee WHERE branch_id IN (%s) OR department_id IN (%s)"
        % (','.join('?' * max(len(branch_ids), 1)), ','.join('?' * max(len(dept_ids), 1))),
        (branch_ids or [0]) + (dept_ids or [0])).fetchall()]

    con.execute("PRAGMA foreign_keys = OFF")
    try:
        ph = lambda n: ','.join('?' * n)
        if emp_ids:
            dependent_tables = [
                'Face_Encoding', 'Attendance', 'Invoice', 'Leave_Balance',
                'Leave_Application', 'Payroll', 'Payslip', 'Performance_Score',
                'AuditLog', 'Employee_Permission', 'Salary_Increment',
                'Bonus_Proposal', 'Performance_Review', 'IC_Access_Request',
                'Notification',
            ]
            for table in dependent_tables:
                try:
                    cur.execute(f"DELETE FROM {table} WHERE employee_id IN ({ph(len(emp_ids))})", emp_ids)
                except sqlite3.OperationalError:
                    pass  # table/column absent on this DB - nothing to purge
            cur.execute(f"DELETE FROM Employee WHERE employee_id IN ({ph(len(emp_ids))})", emp_ids)
        if dept_ids:
            cur.execute(f"DELETE FROM Position WHERE department_id IN ({ph(len(dept_ids))})", dept_ids)
            cur.execute(f"DELETE FROM Department WHERE department_id IN ({ph(len(dept_ids))})", dept_ids)
        if branch_ids:
            cur.execute(f"DELETE FROM Branch WHERE branch_id IN ({ph(len(branch_ids))})", branch_ids)
        con.commit()
    finally:
        con.execute("PRAGMA foreign_keys = ON")
    con.close()
    print("[CLEANUP] Removed demo artifacts: branch 'New Branch' and departments "
          "'New Department' / 'New Department 2.0' (with their employees/positions).")


def seed_branch_manager_departments():
    """Create the Branch Manager / Top Management org structure:

    - 'Top Management' department (KL) holding the C-suite positions.
    - One 'Branch Manager' department per branch with a '(location) Branch
      Manager' catalog position; each is managed by its branch manager.
    - Move brian (CEO), weiliang (KL), cheeseng (Penang), kevin_loh (JB) into
      their new departments (department_id + position_id move together so
      backfill_position_links() keeps them linked).
    - Seed COO/CFO demo employees only if no suitable existing employee found.

    Idempotent - looks up before every insert/update; skips missing employees.
    """
    con = get_connection()
    cur = con.cursor()

    # 1. Departments
    top_mgmt = _get_or_create_dept(cur, 1, 'Top Management')
    bm_depts = {}
    for branch_id, _email in BRANCH_MANAGER_MAP:
        bm_depts[branch_id] = _get_or_create_dept(cur, branch_id, 'Branch Manager')

    # 2. Positions (Top Management C-suite + per-branch Branch Manager)
    ceo_pos = _get_or_create_position(cur, top_mgmt, 'Chief Executive Officer')
    coo_pos = _get_or_create_position(cur, top_mgmt, 'Chief Operating Officer')
    cfo_pos = _get_or_create_position(cur, top_mgmt, 'Chief Financial Officer')
    bm_pos = {}
    for branch_id, _email in BRANCH_MANAGER_MAP:
        bm_pos[branch_id] = _get_or_create_position(
            cur, bm_depts[branch_id], f"{BRANCH_LABELS[branch_id]} Branch Manager")

    # 3. Move the four existing managers into the structure
    dept_branch = {top_mgmt: 1}
    dept_branch.update({bm_depts[b]: b for b in (1, 2, 3)})
    moves = [
        ('brian@smarthr.my', top_mgmt, 'Chief Executive Officer', ceo_pos),
        ('weiliang@smarthr.my', bm_depts[1], 'KL Branch Manager', bm_pos[1]),
        ('cheeseng@smarthr.my', bm_depts[2], 'Penang Branch Manager', bm_pos[2]),
        ('kevin_loh@smarthr.my', bm_depts[3], 'Johor Bahru Branch Manager', bm_pos[3]),
    ]
    for email, dept_id, pos_name, pos_id in moves:
        emp = cur.execute("SELECT employee_id FROM Employee WHERE email=?", (email,)).fetchone()
        if not emp:
            print(f"[SEED] Skipping move for {email} - employee not found.")
            continue
        cur.execute("""UPDATE Employee
                       SET department_id=?, branch_id=?, position=?, position_id=?
                       WHERE employee_id=?""",
                    (dept_id, dept_branch[dept_id], pos_name, pos_id, emp['employee_id']))

    # 4. C-suite demo employees (only when no suitable existing employee)
    year = date.today().year
    leave_types = cur.execute("SELECT leave_type_id, default_days FROM Leave_Type").fetchall()
    pos_ids = {p['position_name']: p['position_id'] for p in cur.execute(
        "SELECT position_id, position_name FROM Position WHERE department_id=?", (top_mgmt,))}
    for email, full_name, pos_name, hire_date, salary in C_SUITE_RECRUITS:
        if cur.execute("SELECT employee_id FROM Employee WHERE email=? LIMIT 1", (email,)).fetchone():
            continue
        cur.execute("""INSERT INTO Employee
            (company_id, branch_id, department_id, full_name, position, position_id,
             employment_type, employment_status, hire_date, base_salary, email,
             password_hash, work_start_time, work_end_time, role_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (1, 1, top_mgmt, full_name, pos_name, pos_ids[pos_name],
             'Full-Time', 'Active', hire_date, salary, email,
             generate_password_hash('Manager@123'), '09:00', '18:00', 3))
        emp_id = cur.lastrowid
        for lt in leave_types:
            cur.execute("""INSERT OR IGNORE INTO Leave_Balance
                           (employee_id, leave_type_id, year, entitled_days)
                           VALUES (?,?,?,?)""",
                        (emp_id, lt['leave_type_id'], year, lt['default_days']))
        for perm in cur.execute("SELECT permission_id FROM Role_Permission WHERE role_id=3").fetchall():
            cur.execute("INSERT OR IGNORE INTO Employee_Permission(employee_id, permission_id, is_active, reason) VALUES(?,?,1,'initial_role_assignment')",
                        (emp_id, perm['permission_id']))
        print(f"[SEED] C-suite employee created: {email} ({full_name}).")

    # 5. The Branch Manager departments are managed by their branch managers
    for branch_id, email in BRANCH_MANAGER_MAP:
        emp = cur.execute("SELECT employee_id FROM Employee WHERE email=?", (email,)).fetchone()
        if emp:
            cur.execute("UPDATE Department SET department_manager_id=? WHERE department_id=?",
                        (emp['employee_id'], bm_depts[branch_id]))

    con.commit()
    con.close()
    print("[OK] Branch Manager / Top Management structure seeded.")


def migrate_attendance_request_table():
    """Create Attendance_Request table if it doesn't exist (for existing databases)."""
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS Attendance_Request (
            request_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id     INTEGER NOT NULL,
            request_date    TEXT NOT NULL,
            check_in_time   TEXT NOT NULL,
            check_out_time  TEXT,
            reason          TEXT NOT NULL,
            system_evidence TEXT,
            status          TEXT DEFAULT 'Pending' CHECK(status IN ('Pending','Approved','Rejected')),
            reviewed_by     INTEGER,
            reviewed_at     TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (employee_id) REFERENCES Employee(employee_id),
            FOREIGN KEY (reviewed_by) REFERENCES Employee(employee_id)
        )
    """)
    con.commit()
    con.close()
    print("[OK] Attendance_Request table migration completed.")

def init_db():
    os.makedirs('instance', exist_ok=True)
    os.makedirs('uploads', exist_ok=True)

    con = get_connection()
    with open(SCHEMA_PATH, 'r') as f:
        con.executescript(f.read())
    con.commit()
    print("[OK] Schema applied.")
    con.close()
    
    # Run migrations for existing databases
    migrate_add_branch_address_fields()
    migrate_attendance_request_table()
    migrate_increment_policy_table()
    migrate_department_manager_id()
    migrate_position_catalog()
    
    con = get_connection()
    cur = con.cursor()

    # ── Roles ──────────────────────────────────────────────────────────────
    roles = [('Admin',), ('HR',), ('Manager',), ('Employee',), ('HR Manager',), ('HR Director',)]
    cur.executemany("INSERT OR IGNORE INTO Role(role_name) VALUES(?)", roles)

    # ── Permissions ────────────────────────────────────────────────────────
    # Define all available permissions
    permissions = [
        # Employee Management
        ('view_employees', 'View employee list and details', 'employees'),
        ('add_employee', 'Create new employees', 'employees'),
        ('edit_employee', 'Edit employee information', 'employees'),
        ('delete_employee', 'Delete/deactivate employees', 'employees'),
        ('view_payroll', 'View payroll information', 'payroll'),
        ('generate_payroll', 'Generate payroll records', 'payroll'),
        
        # Leave Management
        ('apply_leave', 'Apply for leave', 'leave'),
        ('view_leave', 'View own leave balance', 'leave'),
        ('approve_leave', 'Approve leave requests', 'leave'),
        ('view_all_leave', 'View all employee leave records', 'leave'),
        
        # Attendance
        ('view_attendance', 'View own attendance', 'attendance'),
        ('view_all_attendance', 'View all attendance records', 'attendance'),
        ('manual_attendance', 'Create manual attendance records', 'attendance'),
        
        # Invoices
        ('submit_invoice', 'Submit expense invoices', 'invoice'),
        ('view_invoice', 'View own invoices', 'invoice'),
        ('approve_invoice', 'Approve invoices', 'invoice'),
        ('view_all_invoice', 'View all invoices', 'invoice'),
        
        # Organization
        ('manage_organization', 'Manage org structure (branches, departments)', 'organization'),
        ('manage_roles', 'Manage roles and permissions', 'organization'),
        
        # Reports
        ('view_reports', 'View reports', 'reports'),
        ('generate_reports', 'Generate custom reports', 'reports'),
        
        # Audit
        ('view_audit_log', 'View audit logs', 'audit'),
        ('manage_audit_log', 'Archive audit logs', 'audit'),
        
        # Dashboard
        ('access_dashboard', 'Access main dashboard', 'main'),
        ('view_analytics', 'View analytics and statistics', 'main'),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO Permission(permission_name, description, module_name) VALUES(?,?,?)",
        permissions
    )
    
    # Map permissions to roles
    # Admin has all permissions
    cur.execute("SELECT role_id FROM Role WHERE role_name='Admin'")
    admin_role = cur.fetchone()[0]
    cur.execute("SELECT permission_id FROM Permission")
    all_perms = cur.fetchall()
    for perm in all_perms:
        cur.execute("INSERT OR IGNORE INTO Role_Permission(role_id, permission_id) VALUES(?,?)",
                   (admin_role, perm[0]))
    
    # HR role permissions
    cur.execute("SELECT role_id FROM Role WHERE role_name='HR'")
    hr_role = cur.fetchone()[0]
    hr_perms = [
        'view_employees', 'edit_employee', 'view_payroll', 'generate_payroll',
        'approve_leave', 'view_all_leave', 'view_all_attendance', 'manual_attendance',
        'approve_invoice', 'view_all_invoice', 'manage_organization',
        'view_reports', 'generate_reports', 'view_audit_log', 'access_dashboard'
    ]
    for perm_name in hr_perms:
        cur.execute("SELECT permission_id FROM Permission WHERE permission_name=?", (perm_name,))
        perm = cur.fetchone()
        if perm:
            cur.execute("INSERT OR IGNORE INTO Role_Permission(role_id, permission_id) VALUES(?,?)",
                       (hr_role, perm[0]))
    
    # Manager role permissions
    cur.execute("SELECT role_id FROM Role WHERE role_name='Manager'")
    manager_role = cur.fetchone()[0]
    manager_perms = [
        'view_employees', 'apply_leave', 'view_leave', 'approve_leave',
        'view_attendance', 'view_all_attendance', 'submit_invoice', 'view_invoice',
        'approve_invoice', 'view_reports', 'access_dashboard', 'view_analytics'
    ]
    for perm_name in manager_perms:
        cur.execute("SELECT permission_id FROM Permission WHERE permission_name=?", (perm_name,))
        perm = cur.fetchone()
        if perm:
            cur.execute("INSERT OR IGNORE INTO Role_Permission(role_id, permission_id) VALUES(?,?)",
                       (manager_role, perm[0]))
    
    # HR Manager role permissions (same as Admin – all permissions)
    cur.execute("SELECT role_id FROM Role WHERE role_name='HR Manager'")
    hr_manager_role = cur.fetchone()
    if hr_manager_role:
        hr_manager_role = hr_manager_role[0]
        cur.execute("SELECT permission_id FROM Permission")
        all_perms = cur.fetchall()
        for perm in all_perms:
            cur.execute("INSERT OR IGNORE INTO Role_Permission(role_id, permission_id) VALUES(?,?)",
                       (hr_manager_role, perm[0]))

    # Employee role permissions
    cur.execute("SELECT role_id FROM Role WHERE role_name='Employee'")
    emp_role = cur.fetchone()[0]
    emp_perms = [
        'apply_leave', 'view_leave', 'view_attendance', 'submit_invoice',
        'view_invoice', 'access_dashboard'
    ]
    for perm_name in emp_perms:
        cur.execute("SELECT permission_id FROM Permission WHERE permission_name=?", (perm_name,))
        perm = cur.fetchone()
        if perm:
            cur.execute("INSERT OR IGNORE INTO Role_Permission(role_id, permission_id) VALUES(?,?)",
                       (emp_role, perm[0]))

    # ── Company ────────────────────────────────────────────────────────────
    cur.execute("""INSERT OR IGNORE INTO Company(company_id,name,address,contact_no,email)
                   VALUES(1,'Maju Teknologi Sdn Bhd',
                   'No. 12, Jalan Semarak, 50450 Kuala Lumpur, Malaysia',
                   '+603-2110 8888','info@majutek.com.my')""")

    # ── Branches ───────────────────────────────────────────────────────────
    cur.execute("""INSERT OR IGNORE INTO Branch(branch_id,company_id,name,address,contact_no)
                   VALUES(1,1,'KL Headquarters',
                   'No. 12, Jalan Semarak, 50450 Kuala Lumpur, Malaysia','+603-2110 8888')""")
    cur.execute("""INSERT OR IGNORE INTO Branch(branch_id,company_id,name,address,contact_no)
                   VALUES(2,1,'Penang Office',
                   'Unit 5-3, Krystal Point, 11700 Gelugor, Pulau Pinang, Malaysia','+604-658 9000')""")

    # ── Departments ────────────────────────────────────────────────────────
    depts = [
        (1, 1, 'Engineering', None),
        (2, 1, 'Human Resources', None),
        (3, 1, 'Operations', None),
        (4, 1, 'Finance', None),
        (5, 1, 'Administration', None),
        (6, 2, 'Engineering', None),
        (7, 2, 'Operations', None),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO Department(department_id,branch_id,department_name,department_manager_id) VALUES(?,?,?,?)",
        depts
    )

    # ── Employees ──────────────────────────────────────────────────────────
    employees = [
        # (id, co, br, dept, name, ic, contact, address, dob, gender, ec_name, ec_no,
        #  position, emp_type, emp_status, hire_date, salary, role_id, email, password)
        (1, 1, 1, 5, 'Ahmad Zainal bin Abdullah',  '800101145001', '+601128889001',
         'No 5, Jalan Bukit Bintang, 55100 KL', '1980-01-14', 'Male',
         'Siti Zainal', '+601128889002', 'System Administrator',
         'Full-Time', 'Active', '2020-01-15', 0.00, 1,
         'admin@smarthr.my', generate_password_hash('Admin@123')),

        (2, 1, 1, 2, 'Amantha Lee Mei Ling', '850603085002', '+601123456001',
         'No 22, Jalan Ampang Hilir, 55000 KL', '1985-06-03', 'Female',
         'Lee Ah Kow', '+601123456002', 'HR Director',
         'Full-Time', 'Active', '2019-03-10', 9500.00, 5,
         'hr@smarthr.my', generate_password_hash('Hr@123')),

        (3, 1, 1, 3, 'Brian Harris', '780215085003', '+601187654321',
         'No 8, Jalan Utama, 50480 KL', '1978-02-15', 'Male',
         'Janet Harris', '+601187654322', 'Chief Executive Officer',
         'Full-Time', 'Active', '2018-06-01', 18000.00, 3,
         'brian@smarthr.my', generate_password_hash('Manager@123')),

        (4, 1, 1, 3, 'Elizabeth Lopez', '930211086004', '+601134567890',
         'No 14, Jalan Damansara, 50490 KL', '1993-02-11', 'Female',
         'Jose Lopez', '+601134567891', 'Operations Executive',
         'Full-Time', 'Active', '2023-02-11', 4800.00, 4,
         'elizabeth@smarthr.my', generate_password_hash('Employee@123')),

        (5, 1, 1, 1, 'Ryan Tan Chee Keong', '920819015005', '+601145678901',
         'No 33, Jalan Kepong, 52100 KL', '1992-08-19', 'Male',
         'Tan Ah Seng', '+601145678902', 'Senior Software Engineer',
         'Full-Time', 'Active', '2022-08-19', 7200.00, 4,
         'ryan@smarthr.my', generate_password_hash('Employee@123')),

        (6, 1, 1, 2, 'Sarah Lim Hui Shan', '950303086006', '+601156789012',
         'No 7, Jalan Cheras, 56100 KL', '1995-03-03', 'Female',
         'Lim Ah Moi', '+601156789013', 'HR Executive',
         'Full-Time', 'On Leave', '2024-01-03', 4500.00, 2,
         'sarah@smarthr.my', generate_password_hash('Employee@123')),

        (7, 1, 1, 4, 'Nurul Hana binti Mohd Yusof', '011115086007', '+601167890123',
         'No 19, Jalan Duta, 50480 KL', '2001-11-15', 'Female',
         'Mohd Yusof', '+601167890124', 'Finance Executive',
         'Contract', 'Active', '2025-11-15', 3800.00, 4,
         'nurul@smarthr.my', generate_password_hash('Employee@123')),

        (8, 1, 1, 1, 'Kevin Lim Boon Kiat', '900627015008', '+601178901234',
         'No 45, Jalan PJ, 47810 Petaling Jaya', '1990-06-27', 'Male',
         'Lim Boon Hock', '+601178901235', 'Junior Developer',
         'Part-Time', 'Inactive', '2021-06-27', 2800.00, 4,
         'kevin@smarthr.my', generate_password_hash('Employee@123')),

        (9, 1, 2, 6, 'Muhammad Hafiz bin Razali', '880512095009', '+601189012345',
         'No 3, Jalan Macalister, 10400 Penang', '1988-05-12', 'Male',
         'Razali bin Hamid', '+601189012346', 'Engineering Manager',
         'Full-Time', 'Active', '2021-03-01', 8500.00, 3,
         'hafiz@smarthr.my', generate_password_hash('Manager@123')),

        (10, 1, 2, 7, 'Priya Krishnamurthy', '910730086010', '+601190123456',
         'No 11, Jalan Penang, 10000 Penang', '1991-07-30', 'Female',
         'Krishnamurthy V', '+601190123457', 'Operations Coordinator',
         'Full-Time', 'Active', '2022-07-15', 5200.00, 4,
         'priya@smarthr.my', generate_password_hash('Employee@123')),
    ]
    cur.executemany("""
        INSERT OR IGNORE INTO Employee
        (employee_id,company_id,branch_id,department_id,full_name,ic_number,contact_no,
         address,date_of_birth,gender,emergency_contact_name,emergency_contact_no,
         position,employment_type,employment_status,hire_date,base_salary,
         role_id,email,password_hash)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, employees)

    # ── Assign Permissions to Employees ────────────────────────────────────
    # For each employee, grant all permissions associated with their role
    for emp_id, company_id, branch_id, dept_id, full_name, ic, contact, address, dob, gender, ec_name, ec_no, position, emp_type, emp_status, hire_date, salary, role_id, email, pw in employees:
        cur.execute("SELECT permission_id FROM Role_Permission WHERE role_id=?", (role_id,))
        role_perms = cur.fetchall()
        for perm in role_perms:
            cur.execute("INSERT OR IGNORE INTO Employee_Permission(employee_id, permission_id, is_active, reason) VALUES(?,?,1,'initial_role_assignment')",
                       (emp_id, perm[0]))

    # ── Leave Types ────────────────────────────────────────────────────────
    leave_types = [
        (1, 'Annual Leave',    14, 1, 0, 'Paid annual leave entitlement'),
        (2, 'Sick Leave',      14, 1, 1, 'Medical leave with MC required'),
        (3, 'Emergency Leave',  3, 1, 0, 'For family emergencies'),
        (4, 'Unpaid Leave',    30, 0, 0, 'No-pay leave upon approval'),
        (5, 'Maternity Leave', 90, 1, 1, 'Maternity leave (female)'),
        (6, 'Paternity Leave',  3, 1, 0, 'Paternity leave (male)'),
        (7, 'Examination Leave',5, 1, 1, 'For official examinations'),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO Leave_Type(leave_type_id,type_name,default_days,is_paid,requires_document,description) VALUES(?,?,?,?,?,?)",
        leave_types
    )

    # ── Leave Entitlements ─────────────────────────────────────────────────
    entitlements = [
        (1, 'Full-Time', 0, 14, 2026),
        (1, 'Full-Time', 2, 16, 2026),
        (1, 'Full-Time', 5, 18, 2026),
        (1, 'Part-Time', 0,  7, 2026),
        (1, 'Contract',  0, 10, 2026),
        (2, 'Full-Time', 0, 14, 2026),
        (2, 'Part-Time', 0,  7, 2026),
        (2, 'Contract',  0, 10, 2026),
        (3, 'Full-Time', 0,  3, 2026),
        (4, 'Full-Time', 0, 30, 2026),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO Leave_Entitlement(leave_type_id,employment_type,min_service_years,entitled_days,effective_year) VALUES(?,?,?,?,?)",
        entitlements
    )

    # ── Leave Balances (2026) ──────────────────────────────────────────────
    # (employee_id, leave_type_id, year, entitled, used, pending)
    balances = [
        (4, 1, 2026, 14, 4.0, 0.0),  (4, 2, 2026, 14, 2.0, 0.0), (4, 3, 2026, 3, 0, 0),
        (5, 1, 2026, 14, 6.0, 0.0),  (5, 2, 2026, 14, 0.0, 0.0), (5, 3, 2026, 3, 0, 0),
        (6, 1, 2026, 14, 6.0, 3.0),  (6, 2, 2026, 14, 1.0, 0.0), (6, 3, 2026, 3, 0, 0),
        (7, 1, 2026, 10, 0.0, 0.0),  (7, 2, 2026, 10, 0.0, 0.0), (7, 3, 2026, 3, 0, 0),
        (8, 1, 2026,  7, 2.0, 0.0),  (8, 2, 2026,  7, 0.0, 0.0),
        (9, 1, 2026, 16, 2.0, 0.0),  (9, 2, 2026, 14, 0.0, 0.0), (9, 3, 2026, 3, 0, 0),
        (10,1, 2026, 14, 0.0, 0.0),  (10,2, 2026, 14, 0.0, 0.0), (10,3, 2026, 3, 0, 0),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO Leave_Balance(employee_id,leave_type_id,year,entitled_days,used_days,pending_days) VALUES(?,?,?,?,?,?)",
        balances
    )

    # ── Leave Applications ─────────────────────────────────────────────────
    apps = [
        (1,6,1,'2026-04-07','2026-04-09',3,'Family vacation',None,'Pending',   None,None,None),
        (2,6,2,'2026-04-05','2026-04-05',1,'Medical checkup','mc_nurul.pdf','Approved',2,'2026-04-04','Approved. Rest well.'),
        (3,5,1,'2026-03-20','2026-03-25',4,'Annual break',  None,'Approved',  2,'2026-03-19','Enjoy your leave.'),
        (4,5,1,'2026-04-10','2026-04-10',1,'Fever',         None,'Pending',   None,None,None),
    ]
    cur.executemany("""
        INSERT OR IGNORE INTO Leave_Application
        (leave_id,employee_id,leave_type_id,start_date,end_date,total_days,reason,supporting_doc,
         status,reviewed_by,reviewed_at,review_comment)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
    """, apps)

    # ── Attendance Records (April 2026 sample) ──────────────────────────────
    att = [
        (4, 1, '2026-04-21 09:00:00', '2026-04-21 18:00:00', 8.0, 0.0, None, 'Approved', 0),
        (4, 1, '2026-04-22 08:55:00', '2026-04-22 18:05:00', 8.17, 0.0, None, 'Approved', 0),
        (5, 1, '2026-04-21 09:10:00', '2026-04-21 19:30:00', 9.33, 1.33, None, 'Approved', 0),
        (5, 1, '2026-04-22 09:00:00', '2026-04-22 20:00:00', 9.0, 1.0, None, 'Flagged', 0),
        (7, 1, '2026-04-21 09:00:00', '2026-04-21 18:00:00', 8.0, 0.0, None, 'Approved', 0),
        (9, 2, '2026-04-21 08:30:00', '2026-04-21 17:30:00', 8.0, 0.0, None, 'Approved', 0),
        (10,2, '2026-04-21 09:05:00', '2026-04-21 18:00:00', 7.92, 0.0, None, 'Approved', 0),
    ]
    cur.executemany("""
        INSERT OR IGNORE INTO Attendance
        (employee_id,branch_id,check_in,check_out,hours_worked,overtime_hours,
         confidence_score,status,is_manual_entry)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, att)

    # ── May 2026 Attendance Data (Demo for Performance Module) ──────────────
    # Generate attendance for all active employees across 21 weekdays of May 2026
    # May 1 (Fri) = Labour Day — skipped as holiday
    # Working days: May 4 (Mon) – May 29 (Fri) = 20 days
    # Each employee has varied check-in/out to demonstrate performance scoring
    import random
    random.seed(42)

    active_emps = cur.execute("SELECT employee_id, branch_id, full_name FROM Employee WHERE is_active=1").fetchall()
    weekdays = []
    from datetime import date, timedelta
    d = date(2026, 5, 1)
    while d.month == 5:
        if d.weekday() < 5 and d.day != 1:  # skip Labour Day (May 1)
            weekdays.append(d)
        d += timedelta(days=1)

    may_att = []
    for emp in active_emps:
        eid = emp['employee_id']
        bid = emp['branch_id']
        for day in weekdays:
            # ~85% attendance rate overall
            if random.random() > 0.85:
                continue
            # Base check-in at 09:00 with some variance
            mins_late = random.choices(
                [0, 0, 0, 0, 0, 0, 5, 10, 15, 30],  # mostly on time, some late
                weights=[40, 10, 10, 10, 5, 5, 8, 6, 4, 2]
            )[0]
            ci_h, ci_m = 9, mins_late
            ci_str = f"{day.isoformat()} {ci_h:02d}:{ci_m:02d}:00"

            # Check-out: ~8h work + some OT
            ot = random.choices(
                [0, 0, 0, 0, 0.5, 1.0, 1.5, 2.0],
                weights=[40, 20, 10, 10, 8, 6, 4, 2]
            )[0]
            co_h, co_m = 18 + int(ot), int((ot % 1) * 60)
            if co_h >= 24: co_h, co_m = 23, 59
            co_str = f"{day.isoformat()} {co_h:02d}:{co_m:02d}:00"

            hrs = round((co_h + co_m/60) - (ci_h + ci_m/60), 2)
            ot_hrs = round(max(0, hrs - 9), 2)  # OT beyond 9h (including 1h break)
            may_att.append((eid, bid, ci_str, co_str, hrs, ot_hrs, None, 'Approved', 0))

    cur.executemany("""
        INSERT OR IGNORE INTO Attendance
        (employee_id,branch_id,check_in,check_out,hours_worked,overtime_hours,
         confidence_score,status,is_manual_entry)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, may_att)
    print(f"[OK] Generated {len(may_att)} May 2026 attendance records.")

    # ── Invoices ───────────────────────────────────────────────────────────
    inv = [
        (1, 4, 'inv_0842.jpg', 'receipt_techcorp.jpg',   'image', 'TechCorp Sdn Bhd',
         'INV-0842', '2026-04-02', '2026-04-16', 'MYR', 1.0, 3018.87, 181.13, 3200.00, 3200.00,
         'IT Equipment', 'Laptop maintenance service package', 'Approved', 2, '2026-04-03', None),

        (2, 5, 'inv_0841.jpg', 'receipt_officepro.jpg',  'image', 'OfficePro Supplies',
         'INV-0841', '2026-04-01', '2026-04-15', 'MYR', 1.0, 735.85,  44.15,  780.00, 780.00,
         'Office Supplies', 'Stationery and printing supplies', 'Approved', 2, '2026-04-02', None),

        (3, 4, 'inv_0843.jpg', 'receipt_petrol.jpg',     'image', 'Petronas',
         'INV-0843', '2026-04-08', '2026-04-22', 'MYR', 1.0, 120.00,  0.00,   120.00, 120.00,
         'Transport', 'Petrol claim for client visit', 'Pending', None, None, None),

        (4, 7, 'inv_0840.jpg', 'receipt_training.jpg',   'image', 'HR Academy Malaysia',
         'INV-0840', '2026-03-28', '2026-04-11', 'MYR', 1.0, 1500.00, 90.00,  1590.00, 1590.00,
         'Training', 'HR skills workshop registration', 'Rejected', 2, '2026-04-01',
         'Out of approved training budget for Q1.'),
    ]
    cur.executemany("""
        INSERT OR IGNORE INTO Invoice
        (invoice_id,employee_id,filename,original_name,file_type,vendor_name,
         invoice_number,invoice_date,due_date,currency,exchange_rate,subtotal,
         tax_amount,total_amount,total_amount_myr,category,description,status,
         approved_by,approved_at,rejection_reason)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, inv)

    # ── Payroll (April 2026) ───────────────────────────────────────────────
    def calc_payroll(emp_id, base, ot_pay=0, claims=0, bonus=0):
        gross = base + ot_pay + claims + bonus
        epf_e   = round(gross * 0.11, 2)
        epf_er  = round(gross * 0.13, 2)
        socso_e = round(min(gross, 5000) * 0.005, 2)
        socso_er= round(min(gross, 5000) * 0.0175, 2)
        eis_e   = round(min(gross, 5000) * 0.002, 2)
        eis_er  = round(min(gross, 5000) * 0.002, 2)
        pcb = round(max(0, (gross - 3000) * 0.01), 2) if gross > 3000 else 0
        total_ded = epf_e + socso_e + eis_e + pcb
        net = round(gross - total_ded, 2)
        return (emp_id, 4, 2026, base, ot_pay, 0, bonus, claims, 0, gross,
                epf_e, epf_er, socso_e, socso_er, eis_e, eis_er, pcb,
                total_ded, net, 'Finalised', 2)

    payrolls = [
        calc_payroll(4, 4800.00, ot_pay=0,    claims=3200.00),
        calc_payroll(5, 7200.00, ot_pay=360.00,claims=780.00),
        calc_payroll(6, 4500.00, ot_pay=0,    claims=0),
        calc_payroll(7, 3800.00, ot_pay=0,    claims=0),
        calc_payroll(8, 2800.00, ot_pay=0,    claims=0),
        calc_payroll(9, 8500.00, ot_pay=0,    claims=0),
        calc_payroll(10,5200.00, ot_pay=0,    claims=0),
    ]
    cur.executemany("""
        INSERT OR IGNORE INTO Payroll
        (employee_id,pay_period_month,pay_period_year,base_salary,overtime_pay,commission,bonus,
         invoice_claims,leave_adjustment,gross_pay,epf_employee,epf_employer,socso_employee,
         socso_employer,eis_employee,eis_employer,pcb_tax,total_deductions,net_pay,status,generated_by)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, payrolls)

    # ── Audit Log seed ─────────────────────────────────────────────────────
    logs = [
        (1,'LOGIN','Auth','Admin logged in','Employee',1,'Success',None,'127.0.0.1'),
        (2,'LOGIN','Auth','HR Director logged in','Employee',2,'Success',None,'192.168.1.23'),
        (2,'APPROVE','Leave','Approved leave application LV-002','Leave_Application',2,'Success','{"leave_id":2}','192.168.1.23'),
        (4,'APPLY_LEAVE','Leave','Elizabeth applied for Annual Leave','Leave_Application',1,'Success','{"days":3}','192.168.1.34'),
        (5,'LOGIN','Auth','Failed login attempt','Employee',5,'Failed',None,'192.168.1.57'),
    ]
    cur.executemany("""
        INSERT INTO AuditLog
        (employee_id,action,module_name,description,target_table,target_record_id,
         action_status,action_details,ip_address)
        VALUES(?,?,?,?,?,?,?,?,?)
""", logs)

    con.commit()
    con.close()
    print("[OK] Database seeded with Malaysian demo data.")

    # Remove leftover demo/test artifacts (branch "New Branch", orphan depts)
    cleanup_demo_artifacts()

    # Assign demo department managers (idempotent, runs after seeding)
    seed_department_managers()

    # Link freshly-seeded demo employees/postings to the position catalog (fresh installs)
    backfill_position_links()

    # Branch Manager / Top Management org structure for the three real branches
    seed_branch_manager_departments()

    print()
    print("=== DEFAULT LOGIN CREDENTIALS ===")
    print("System Admin : admin@smarthr.my        / Admin@123")
    print("HR Director  : hr@smarthr.my           / Hr@123")
    print("CEO          : brian@smarthr.my        / Manager@123")
    print("COO / CFO    : coo@smarthr.my, cfo@smarthr.my  / Manager@123")
    print("Branch Mgrs  : weiliang@smarthr.my (KL), cheeseng@smarthr.my (PG), kevin_loh@smarthr.my (JB)  / Manager@123")
    print("Dept Mgrs    : hafiz@smarthr.my (PG Eng), vincent@smarthr.my (KL Eng), shanthi@smarthr.my (KL Fin), fauzi@smarthr.my (JB Ops)  / Manager@123")
    print("Employee     : elizabeth@smarthr.my / Employee@123  (others same password)")
    print("=================================")

if __name__ == '__main__':
    init_db()

