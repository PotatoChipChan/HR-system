"""Position Catalog - predefined job titles feature tests.

Covers: migration integrity, FK enforcement, catalog CRUD (add/duplicate/
rename/deactivate), authorization for all roles, vacancy-request dropdown
scoping + POST guards, custom-position approval flow, posting catalog-only
input, and employee position linking.
"""
import sys, os, sqlite3, json
sys.path.insert(0, '.')
from app import create_app
import init_db

app = create_app()
failed = 0
passed = 0

def check(condition, label):
    global passed, failed
    if condition:
        passed += 1
        print(f'  [PASS] {label}')
    else:
        failed += 1
        print(f'  [FAIL] {label}')

DB = os.path.join('instance', 'smarthr.db')

def q(sql, args=()):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute(sql, args).fetchall()
    con.close()
    return [dict(r) for r in rows]

def x(sql, args=()):
    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys = ON")
    try:
        cur = con.execute(sql, args)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()

def login(client, email, pw):
    return client.post('/login', data={'email': email, 'password': pw},
                       follow_redirects=True)

TMP_POS6 = 'Tmp QA Engineer'      # temp catalog entry in dept 6 (Hafiz's managed dept)
TMP_POS1 = 'Tmp KL Role'          # temp catalog entry in dept 1 (cross-branch for Hafiz)
TMP_CUSTOM = 'Tmp Cosmic Engineer'
TMP_EMP_EMAIL = 'tmp.pos.test@smarthr.my'
TMP_TITLES = (TMP_POS6, TMP_POS1, TMP_CUSTOM, 'Tmp QA Lead', 'Tmp Solo Dev')

def cleanup():
    con = sqlite3.connect(DB); con.execute("PRAGMA foreign_keys = ON")
    con.execute("DELETE FROM Vacancy_Request WHERE position_title LIKE 'Tmp %'")
    con.execute("DELETE FROM Job_Posting WHERE title LIKE 'Tmp %'")
    con.execute("DELETE FROM Employee WHERE email=?", (TMP_EMP_EMAIL,))
    con.execute("DELETE FROM Position WHERE position_name IN (%s)"
                % ','.join('?' * len(TMP_TITLES)), TMP_TITLES)
    con.execute("DELETE FROM AuditLog WHERE description LIKE '%Tmp %' "
                "OR action_details LIKE '%Tmp %'")
    con.commit(); con.close()

print('=' * 60)
print('Position Catalog - predefined job titles')
print('=' * 60)

# -- 1. Migration integrity ----------------------------------------------
pos_count = q("SELECT COUNT(*) c FROM Position")[0]['c']
check(pos_count > 0, f'Migration: Position table populated ({pos_count} rows)')

unlinked = q("""SELECT COUNT(*) c FROM Employee
                WHERE position IS NOT NULL AND TRIM(position)<>'' AND position_id IS NULL""")
check(unlinked[0]['c'] == 0, 'Migration: every employee with a title is linked to the catalog')

unlinked_p = q("""SELECT COUNT(*) c FROM Job_Posting
                  WHERE title IS NOT NULL AND TRIM(title)<>'' AND position_id IS NULL""")
check(unlinked_p[0]['c'] == 0, 'Migration: every job posting title is linked to the catalog')

dups = q("""SELECT COUNT(*) c FROM (
            SELECT department_id, LOWER(position_name) FROM Position
            GROUP BY department_id, LOWER(position_name) HAVING COUNT(*)>1)""")
check(dups[0]['c'] == 0, 'Migration: no case-insensitive duplicates per department')

try:
    x("INSERT INTO Position(position_name, department_id) VALUES('Tmp FK', 999999)")
    check(False, 'FK enforcement: Position with unknown department rejected')
    x("DELETE FROM Position WHERE position_name='Tmp FK'")
except sqlite3.IntegrityError:
    check(True, 'FK enforcement: Position with unknown department rejected')

pos_ddl = q("SELECT sql FROM sqlite_master WHERE type='table' AND name='Position'")[0]['sql']
check('COLLATE NOCASE' in pos_ddl, 'Migration: Position unique constraint is COLLATE NOCASE')
check('CHECK' in pos_ddl, 'Migration: Position table carries CHECK(TRIM) constraint')
pos_ix = [r['name'] for r in q("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='Position'")]
check('idx_position_dept_active' in pos_ix,
      'Migration: composite index (department_id, is_active) exists')
check('idx_position_department' not in pos_ix,
      'Migration: standalone department index removed (UNIQUE covers it)')

# -- 2. Authorization ----------------------------------------------------
with app.test_client() as c:
    login(c, 'elizabeth@smarthr.my', 'Employee@123')
    r = c.get('/organization/roles', follow_redirects=True)
    check(b'permission' in r.data, 'Authz: Employee blocked from Roles & Permissions page')

with app.test_client() as c:
    login(c, 'hafiz@smarthr.my', 'Manager@123')
    r = c.get('/organization/roles', follow_redirects=True)
    check(b'permission' in r.data, 'Authz: Manager blocked from Roles & Permissions page')

with app.test_client() as c:
    login(c, 'admin@smarthr.my', 'Admin@123')
    r = c.get('/organization/roles')
    check(r.status_code == 200, 'Authz: Admin can open Roles & Permissions page')
    check(b'Position Catalog' in r.data, 'Roles page shows Position Catalog section')

with app.test_client() as c:
    login(c, 'hr@smarthr.my', 'Hr@123')
    r = c.get('/organization/roles')
    check(r.status_code == 200, 'Authz: HR Director can open Roles & Permissions page')

# -- 3. Catalog CRUD (admin) ---------------------------------------------
admin = app.test_client()
login(admin, 'admin@smarthr.my', 'Admin@123')

r = admin.post('/organization/roles/positions/add',
               data={'position_name': TMP_POS6, 'department_id': 6}, follow_redirects=True)
check(b'added to the catalog' in r.data, 'CRUD: add position succeeds')
row = q("SELECT * FROM Position WHERE position_name=? AND department_id=?", (TMP_POS6, 6))
check(len(row) == 1, 'CRUD: added position exists in DB')

admin.post('/organization/roles/positions/add',
           data={'position_name': '  tmp   qa engineer ', 'department_id': 6},
           follow_redirects=True)
row = q("SELECT COUNT(*) c FROM Position WHERE department_id=6 AND LOWER(position_name)='tmp qa engineer'")
check(row[0]['c'] == 1, 'CRUD: case/whitespace duplicate rejected (no second row)')

pid6 = q("SELECT position_id FROM Position WHERE department_id=6 AND LOWER(position_name)='tmp qa engineer'")[0]['position_id']
r = admin.post(f'/organization/roles/positions/{pid6}/rename',
               data={'position_name': 'Tmp QA Lead'}, follow_redirects=True)
row = q("SELECT position_name FROM Position WHERE position_id=?", (pid6,))
check(row[0]['position_name'] == 'Tmp QA Lead', 'CRUD: rename position works')

r = admin.post(f'/organization/roles/positions/{pid6}/toggle', follow_redirects=True)
row = q("SELECT is_active FROM Position WHERE position_id=?", (pid6,))
check(row[0]['is_active'] == 0, 'CRUD: deactivate position works')

r = admin.post(f'/organization/roles/positions/{pid6}/toggle', follow_redirects=True)
row = q("SELECT is_active FROM Position WHERE position_id=?", (pid6,))
check(row[0]['is_active'] == 1, 'CRUD: reactivate position works')

# -- 4. Vacancy request: dropdown scoped + catalog/custom submit ---------
tmp6 = q("SELECT position_id FROM Position WHERE department_id=6 AND position_name='Tmp QA Lead'")[0]['position_id']
tmp1 = x("INSERT INTO Position(position_name, department_id) VALUES(?,?)", (TMP_POS1, 1))

hafiz = app.test_client()
login(hafiz, 'hafiz@smarthr.my', 'Manager@123')

r = hafiz.get('/recruitment/vacancy-request')
check(r.status_code == 200, 'Vacancy request page opens for dept manager')
check(b'Embedded Engineer' in r.data, 'Dropdown: own-dept catalog position shown')
check(b'DevOps Engineer' not in r.data, 'Dropdown: other-dept position hidden')
check(b'Tmp QA Lead' in r.data, 'Dropdown: temp own-dept position shown')
check(b'Tmp KL Role' not in r.data, 'Dropdown: temp other-branch position hidden')
check(b'__custom__' in r.data, 'Dropdown: Custom option present')

r = hafiz.post('/recruitment/vacancy-request', data={
    'department_id': 1, 'position_id': tmp6,
    'employment_type': 'Full-Time', 'reason': 'test'}, follow_redirects=True)
check(b'own department' in r.data, 'POST guard: cross-department request rejected')

r = hafiz.post('/recruitment/vacancy-request', data={
    'department_id': 6, 'position_id': tmp1,
    'employment_type': 'Full-Time', 'reason': 'test'}, follow_redirects=True)
check(b'Invalid position' in r.data, 'POST guard: position from another department rejected')

r = hafiz.post('/recruitment/vacancy-request', data={
    'department_id': 6, 'position_id': str(tmp6),
    'employment_type': 'Full-Time', 'reason': 'test'}, follow_redirects=True)
req = q("""SELECT * FROM Vacancy_Request WHERE position_title='Tmp QA Lead'
           ORDER BY request_id DESC LIMIT 1""")
check(len(req) == 1 and req[0]['is_custom'] == 0 and req[0]['position_id'] == tmp6,
      'Submit: catalog position stored with position_id and is_custom=0')

r = hafiz.post('/recruitment/vacancy-request', data={
    'department_id': 6, 'position_id': '__custom__', 'position_title': '  ' + TMP_CUSTOM + '  ',
    'employment_type': 'Full-Time', 'reason': 'test'}, follow_redirects=True)
req = q("""SELECT * FROM Vacancy_Request WHERE position_title=? ORDER BY request_id DESC LIMIT 1""",
        (TMP_CUSTOM,))
check(len(req) == 1 and req[0]['is_custom'] == 1 and req[0]['position_id'] is None,
      'Submit: custom title stored with is_custom=1 and no catalog link')
row = q("SELECT COUNT(*) c FROM Position WHERE LOWER(position_name)=LOWER(?)", (TMP_CUSTOM,))
check(row[0]['c'] == 0, 'Submit: custom title does NOT auto-create a catalog row')

r = hafiz.post('/recruitment/vacancy-request', data={
    'department_id': 6, 'position_id': '__custom__', 'position_title': '',
    'employment_type': 'Full-Time', 'reason': 'test'}, follow_redirects=True)
check(b'select a position or enter a custom title' in r.data,
      'Submit: empty custom title rejected')

# -- 5. Approve flow: custom position governance (admin) -----------------
custom_req = q("""SELECT * FROM Vacancy_Request WHERE position_title=?
                  ORDER BY request_id DESC LIMIT 1""", (TMP_CUSTOM,))[0]
r = admin.post(f"/recruitment/vacancy-request/{custom_req['request_id']}/approve",
               data={'branch_id': 2}, follow_redirects=True)
posting = q("""SELECT * FROM Job_Posting WHERE title=? ORDER BY posting_id DESC LIMIT 1""",
            (TMP_CUSTOM,))
check(len(posting) == 1 and posting[0]['position_id'] is None,
      'Approve: custom kept as free text -> posting linked to nothing')
row = q("SELECT COUNT(*) c FROM Position WHERE LOWER(position_name)=LOWER(?)", (TMP_CUSTOM,))
check(row[0]['c'] == 0, 'Approve: no catalog row created when not requested')

cat_req = q("""SELECT * FROM Vacancy_Request WHERE position_title='Tmp QA Lead'
               ORDER BY request_id DESC LIMIT 1""")[0]
r = admin.post(f"/recruitment/vacancy-request/{cat_req['request_id']}/approve",
               data={'branch_id': 2}, follow_redirects=True)
posting = q("""SELECT * FROM Job_Posting WHERE title='Tmp QA Lead' ORDER BY posting_id DESC LIMIT 1""")
check(len(posting) == 1 and posting[0]['position_id'] == tmp6,
      'Approve: catalog-linked request -> posting carries position_id')

custom2 = x("""INSERT INTO Vacancy_Request
               (requested_by, department_id, position_title, is_custom,
                employment_type, reason, status)
               VALUES (?,?,?,1,'Full-Time','test','Pending')""",
            (9, 6, 'Tmp ToBeCatalogued'))
r = admin.post(f"/recruitment/vacancy-request/{custom2}/approve",
               data={'branch_id': 2, 'add_to_catalog': '1'}, follow_redirects=True)
row = q("SELECT * FROM Position WHERE department_id=6 AND LOWER(position_name)=LOWER(?)",
        ('Tmp ToBeCatalogued',))
check(len(row) == 1, 'Approve: "Add to catalog" creates the Position row')
req = q("SELECT * FROM Vacancy_Request WHERE request_id=?", (custom2,))[0]
posting = q("""SELECT * FROM Job_Posting WHERE title='Tmp ToBeCatalogued' ORDER BY posting_id DESC LIMIT 1""")[0]
check(req['is_custom'] == 0 and req['position_id'] == row[0]['position_id'],
      'Approve: request re-linked to catalog (is_custom=0, position_id set)')
check(posting['position_id'] == row[0]['position_id'],
      'Approve: posting linked to the new catalog entry')

# -- 6. Job posting: catalog-only input (admin) --------------------------
r = admin.post('/recruitment/postings/add', data={
    'department_id': 6, 'branch_id': 2, 'employment_type': 'Full-Time'}, follow_redirects=True)
check(b'select a position from the catalog' in r.data,
      'Posting: missing position rejected')

r = admin.post('/recruitment/postings/add', data={
    'position_id': str(tmp1), 'department_id': 6, 'branch_id': 2,
    'employment_type': 'Full-Time'}, follow_redirects=True)
check(b'does not belong to the chosen department' in r.data,
      'Posting: cross-department position rejected')

r = admin.post('/recruitment/postings/add', data={
    'position_id': str(tmp6), 'department_id': 6, 'branch_id': 2,
    'employment_type': 'Full-Time'}, follow_redirects=True)
posting = q("""SELECT * FROM Job_Posting WHERE title='Tmp QA Lead'
               ORDER BY posting_id DESC LIMIT 1""")[0]
check(posting['position_id'] == tmp6,
      'Posting: valid submission stores catalog title + position_id')

# -- 7. Add employee: dropdown link + custom free text (no auto-create) --
r = admin.post('/employees/add', data={
    'full_name': 'Tmp Position Tester', 'email': TMP_EMP_EMAIL,
    'branch_id': 2, 'department_id': 6, 'position_id': str(tmp6), 'gender': 'Male',
    'hire_date': '2026-08-01', 'employment_type': 'Full-Time',
    'base_salary': '3000', 'role_id': '4', 'employment_status': 'Active',
    'work_start_time': '09:00', 'work_end_time': '18:00',
    'password': 'Test@123', 'confirm_password': 'Test@123'}, follow_redirects=True)
emp = q("SELECT * FROM Employee WHERE email=?", (TMP_EMP_EMAIL,))
check(len(emp) == 1 and emp[0]['position_id'] == tmp6 and emp[0]['position'] == 'Tmp QA Lead',
      'Employee: catalog selection stores position_id + canonical title')

x("DELETE FROM Employee WHERE email=?", (TMP_EMP_EMAIL,))
r = admin.post('/employees/add', data={
    'full_name': 'Tmp Position Tester', 'email': TMP_EMP_EMAIL,
    'branch_id': 2, 'department_id': 6, 'position_id': '__custom__', 'gender': 'Male',
    'position': 'Tmp Solo Dev',
    'hire_date': '2026-08-01', 'employment_type': 'Full-Time',
    'base_salary': '3000', 'role_id': '4', 'employment_status': 'Active',
    'work_start_time': '09:00', 'work_end_time': '18:00',
    'password': 'Test@123', 'confirm_password': 'Test@123'}, follow_redirects=True)
emp = q("SELECT * FROM Employee WHERE email=?", (TMP_EMP_EMAIL,))
check(len(emp) == 1 and emp[0]['position_id'] is None and emp[0]['position'] == 'Tmp Solo Dev',
      'Employee: custom title stored as free text, no catalog link')
row = q("SELECT COUNT(*) c FROM Position WHERE LOWER(position_name)='tmp solo dev'")
check(row[0]['c'] == 0, 'Employee: custom title does NOT auto-create a catalog row')
x("DELETE FROM Employee WHERE email=?", (TMP_EMP_EMAIL,))

# -- 8. Reject flow: no catalog side effects -----------------------------
custom3 = x("""INSERT INTO Vacancy_Request
               (requested_by, department_id, position_title, is_custom,
                employment_type, reason, status)
               VALUES (?,?,?,1,'Full-Time','test','Pending')""",
            (9, 6, 'Tmp RejectedTitle'))
admin.post(f"/recruitment/vacancy-request/{custom3}/reject",
           data={'rejection_reason': 'not needed'}, follow_redirects=True)
row = q("SELECT status FROM Vacancy_Request WHERE request_id=?", (custom3,))
check(row[0]['status'] == 'Rejected', 'Reject: custom request rejected')
row = q("SELECT COUNT(*) c FROM Position WHERE LOWER(position_name)='tmp rejectedtitle'")
check(row[0]['c'] == 0, 'Reject: no catalog row created')

# -- 9. Inactive positions: hidden from dropdowns but retained -----------
inactive6 = x("INSERT INTO Position(position_name, department_id, is_active) VALUES(?,?,0)",
              ('Tmp Inactive Role', 6))
h2 = app.test_client(); login(h2, 'hafiz@smarthr.my', 'Manager@123')
r = h2.get('/recruitment/vacancy-request')
check(b'Tmp Inactive Role' not in r.data, 'Inactive: hidden from vacancy-request dropdown')

# -- 10. DB-level constraints: NOCASE unique + CHECK(TRIM) backstop -------
try:
    x("INSERT INTO Position(position_name, department_id) VALUES('tmp qa lead', 6)")
    check(False, 'DB: case-collision raw INSERT rejected (COLLATE NOCASE)')
except sqlite3.IntegrityError:
    check(True, 'DB: case-collision raw INSERT rejected (COLLATE NOCASE)')

try:
    x("INSERT INTO Position(position_name, department_id) VALUES(' Tmp QA Lead', 6)")
    check(False, 'DB: padded raw INSERT rejected (CHECK TRIM)')
except sqlite3.IntegrityError:
    check(True, 'DB: padded raw INSERT rejected (CHECK TRIM)')

try:
    x("INSERT INTO Position(position_name, department_id) VALUES('Tmp  QA  Lead', 6)")
    check(False, 'DB: inner-whitespace raw INSERT rejected (CHECK)')
except sqlite3.IntegrityError:
    check(True, 'DB: inner-whitespace raw INSERT rejected (CHECK)')

try:
    x("INSERT INTO Position(position_name, department_id) VALUES('   ', 6)")
    check(False, 'DB: whitespace-only raw INSERT rejected (CHECK)')
except sqlite3.IntegrityError:
    check(True, 'DB: whitespace-only raw INSERT rejected (CHECK)')

# -- 11. Add employee: ownership + inactive position guards ---------------
r = admin.post('/employees/add', data={
    'full_name': 'Tmp Position Tester', 'email': TMP_EMP_EMAIL,
    'branch_id': 2, 'department_id': 6, 'position_id': str(tmp1), 'gender': 'Male',
    'position': '', 'hire_date': '2026-08-01', 'employment_type': 'Full-Time',
    'base_salary': '3000', 'role_id': '4', 'employment_status': 'Active',
    'work_start_time': '09:00', 'work_end_time': '18:00',
    'password': 'Test@123', 'confirm_password': 'Test@123'}, follow_redirects=True)
check(b'Invalid position' in r.data,
      'Employee: cross-department position rejected on submit')
emp = q("SELECT COUNT(*) c FROM Employee WHERE email=?", (TMP_EMP_EMAIL,))
check(emp[0]['c'] == 0, 'Employee: no row created on cross-department rejection')

inactive6 = q("SELECT position_id FROM Position WHERE position_name='Tmp Inactive Role'")[0]['position_id']
r = admin.post('/recruitment/vacancy-request', data={
    'department_id': 6, 'position_id': str(inactive6),
    'employment_type': 'Full-Time', 'reason': 'test'}, follow_redirects=True)
check(b'Invalid position' in r.data,
      'POST: inactive position rejected even with matching department')

r = admin.get('/recruitment/postings/add')
check(b'Tmp Inactive Role' not in r.data,
      'Posting dropdown: inactive position excluded for new records')

r = admin.get('/employees/add')
check(b'Tmp Inactive Role' not in r.data,
      'Employee dropdown: inactive position excluded for new records')

# -- 12. Audit trail: custom position governance --------------------------
aud = q("""SELECT COUNT(*) c FROM AuditLog WHERE action='CREATE_POSITION'
           AND target_table='Position' AND description LIKE '%Tmp ToBeCatalogued%'""")
check(aud[0]['c'] == 1, 'Audit: promote-to-catalog logs CREATE_POSITION entry')

a2 = q("""SELECT action_details FROM AuditLog WHERE action='APPROVE_VACANCY'
          AND action_details LIKE '%Tmp Cosmic Engineer%'
          ORDER BY audit_log_id DESC LIMIT 1""")
ok = False
if a2 and a2[0]['action_details']:
    d = json.loads(a2[0]['action_details'])
    ok = d.get('is_custom') == 1 and d.get('position_title') == TMP_CUSTOM
check(ok, 'Audit: approve-as-free-text records is_custom + title in details')

# -- 13. Backfill idempotence + deterministic first-seen casing -----------
pos_before = q("SELECT COUNT(*) c FROM Position")[0]['c']
emp_before = q("SELECT COUNT(*) c FROM Employee WHERE position_id IS NOT NULL")[0]['c']
init_db.backfill_position_links()
pos_after = q("SELECT COUNT(*) c FROM Position")[0]['c']
emp_after = q("SELECT COUNT(*) c FROM Employee WHERE position_id IS NOT NULL")[0]['c']
check(pos_before == pos_after, 'Backfill: re-run creates no new catalog rows')
check(emp_before == emp_after, 'Backfill: re-run does not change employee links')

cleanup()
x("DELETE FROM Position WHERE position_name IN ('Tmp Inactive Role','Tmp ToBeCatalogued')")

print('=' * 60)
print(f'Results: {passed} passed, {failed} failed out of {passed + failed} tests')
if failed:
    print('*** SOME TESTS FAILED ***')
    sys.exit(1)
print('All tests passed!')
