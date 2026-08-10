"""Branch Managers, Top Management & Branch-Filtered Department Managers.

Covers: seeded org structure (Branch Manager + Top Management departments,
C-suite + per-branch branch-manager positions), employee moves + links,
C-suite demo logins, department-manager assignments, the Branch Manager
session exemption (branch-wide scoping), the department-form branch filter
(data-branch-id + server-side cross-branch rejection), artifact cleanup, and
seed idempotence.
"""
import sys, os, sqlite3
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

print('=' * 60)
print('Branch Manager / Top Management structure')
print('=' * 60)

# -- 1. Departments --------------------------------------------------------
for branch_name in ('KL Headquarters', 'Penang Office', 'Johor Bahru Branch'):
    rows = q("""SELECT COUNT(*) c FROM Department d
                JOIN Branch b ON d.branch_id=b.branch_id
                WHERE b.name=? AND d.department_name='Branch Manager'""",
             (branch_name,))
    check(rows[0]['c'] == 1, f"Department 'Branch Manager' exists in {branch_name} (exactly one)")

tm = q("""SELECT department_id FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
          WHERE b.name='KL Headquarters' AND d.department_name='Top Management'""")
check(len(tm) == 1, 'Department Top Management exists in KL headquarters')
top_mgmt_id = tm[0]['department_id']

# -- 2. Positions in the catalog --------------------------------------------
POS_EXPECT = [
    ('Chief Executive Officer', 'Top Management'),
    ('Chief Operating Officer', 'Top Management'),
    ('Chief Financial Officer', 'Top Management'),
    ('KL Branch Manager', 'Branch Manager'),
    ('Penang Branch Manager', 'Branch Manager'),
    ('Johor Bahru Branch Manager', 'Branch Manager'),
]
for pos_name, dept_name in POS_EXPECT:
    rows = q("""SELECT COUNT(*) c FROM Position p JOIN Department d ON p.department_id=d.department_id
                WHERE p.position_name=? AND d.department_name=?""",
             (pos_name, dept_name))
    check(rows[0]['c'] == 1, f"Position '{pos_name}' exists in '{dept_name}' department")

# -- 3. Employee moves + catalog links --------------------------------------
EXPECTED_LINKS = [
    ('brian@smarthr.my',     'Top Management', 'Chief Executive Officer'),
    ('weiliang@smarthr.my',  'Branch Manager', 'KL Branch Manager'),
    ('cheeseng@smarthr.my',  'Branch Manager', 'Penang Branch Manager'),
    ('kevin_loh@smarthr.my', 'Branch Manager', 'Johor Bahru Branch Manager'),
]
for email, dept_name, pos_name in EXPECTED_LINKS:
    rows = q("""SELECT e.department_id, e.position_id, d.department_name, p.position_name
                FROM Employee e
                JOIN Department d ON e.department_id=d.department_id
                JOIN Position p ON e.position_id=p.position_id
                WHERE e.email=?""", (email,))
    ok = (len(rows) == 1 and rows[0]['department_name'] == dept_name
          and rows[0]['position_name'] == pos_name)
    check(ok, f'{email} linked to {dept_name} / {pos_name}')

# -- 4. Department-manager assignments --------------------------------------
BM_MANAGERS = {
    'KL Headquarters': 'weiliang@smarthr.my',
    'Penang Office': 'cheeseng@smarthr.my',
    'Johor Bahru Branch': 'kevin_loh@smarthr.my',
}
for branch_name, email in BM_MANAGERS.items():
    rows = q("""SELECT e.email FROM Department d
                JOIN Branch b ON d.branch_id=b.branch_id
                JOIN Employee e ON e.employee_id=d.department_manager_id
                WHERE b.name=? AND d.department_name='Branch Manager'""",
             (branch_name,))
    check(len(rows) == 1 and rows[0]['email'] == email,
          f'{branch_name} Branch Manager dept is managed by {email}')

rows = q("SELECT department_manager_id FROM Department WHERE department_id=?", (top_mgmt_id,))
check(rows[0]['department_manager_id'] is None,
      'Top Management department has no department manager (CEO stays branch-wide)')

# -- 5. C-suite demo employees ----------------------------------------------
for email, name in (('coo@smarthr.my', 'Alex Chen'), ('cfo@smarthr.my', 'Priya Raj')):
    rows = q("""SELECT e.department_id, e.position, p.position_name, d.department_name
                FROM Employee e
                JOIN Department d ON e.department_id=d.department_id
                JOIN Position p ON e.position_id=p.position_id
                WHERE e.email=?""", (email,))
    ok = len(rows) == 1 and rows[0]['department_name'] == 'Top Management'
    check(ok, f'{email} ({name}) exists in Top Management with a catalog position')
    if rows:
        ok = rows[0]['position'] == rows[0]['position_name']
        check(ok, f'{email} position text matches the catalog title')

for email in ('coo@smarthr.my', 'cfo@smarthr.my'):
    rows = q("""SELECT COUNT(*) c FROM Leave_Balance lb JOIN Employee e ON lb.employee_id=e.employee_id
                WHERE e.email=? AND lb.year = strftime('%Y','now')""", (email,))
    check(rows[0]['c'] >= 7, f'{email} has current-year leave balances for all leave types')

with app.test_client() as c:
    r = login(c, 'coo@smarthr.my', 'Manager@123')
    check(r.status_code == 200, 'COO login succeeds (Manager@123)')
    with c.session_transaction() as sess:
        check(sess.get('user_role') == 'Manager', 'COO session role = Manager')
with app.test_client() as c:
    r = login(c, 'cfo@smarthr.my', 'Manager@123')
    check(r.status_code == 200, 'CFO login succeeds (Manager@123)')

# -- 6. Scoping exemption: branch managers stay branch-wide -----------------
# Top Management exists only in KL; the other branches' branch-wide lists show
# their own normal departments instead.
SCOPE = [
    ('weiliang@smarthr.my', 1, 'KL Branch Manager',     'Penang Branch Manager', 'Top Management'),
    ('cheeseng@smarthr.my', 2, 'Penang Branch Manager', 'KL Branch Manager',     'Engineering'),
    ('kevin_loh@smarthr.my', 3, 'Johor Bahru Branch Manager', 'KL Branch Manager', 'Finance'),
]
for email, branch_id, own_pos, other_pos, branch_wide_proof in SCOPE:
    with app.test_client() as c:
        login(c, email, 'Manager@123')
        with c.session_transaction() as sess:
            check(sess.get('is_dept_manager') is False,
                  f'{email} is NOT flagged as dept manager (Branch Manager exemption)')
            check(sess.get('managed_dept_id') is None,
                  f'{email} has no managed_dept_id (branch-wide)')
            check(sess.get('branch_id') == branch_id,
                  f'{email} branch_id = {branch_id}')
        r = c.get('/recruitment/vacancy-request')
        check(r.status_code == 200, f'{email} can open vacancy-request page')
        data = r.data
        check(branch_wide_proof.encode() in data,
              f'{email} sees all branch depts (branch-wide, incl. {branch_wide_proof})')
        check(own_pos.encode() in data, f'{email} sees own-branch positions ({own_pos})')
        check(other_pos.encode() not in data,
              f'{email} does NOT see other-branch positions ({other_pos})')

# -- 7. Department form: branch filter + server-side guard ------------------
with app.test_client() as c:
    login(c, 'admin@smarthr.my', 'Admin@123')
    r = c.get('/organization/department/add')
    check(r.status_code == 200, 'Admin can open add-department form')
    check(b'data-branch-id' in r.data, 'Manager dropdown options carry data-branch-id')

    # Crafted POST: Penang branch + a KL manager (weiliang id 15)
    r = c.post('/organization/department/add', data={
        'branch_id': 2, 'department_name': 'Tmp CrossBranch',
        'department_manager_id': '15'}, follow_redirects=True)
    check(b'does not belong' in r.data, 'Add POST: cross-branch manager rejected')
    rows = q("SELECT COUNT(*) c FROM Department WHERE department_name='Tmp CrossBranch'")
    check(rows[0]['c'] == 0, 'Add POST: no department row created after rejection')

    # Same guard on edit POST (dept 6 is Penang)
    r = c.post('/organization/department/6/edit', data={
        'department_name': 'Engineering', 'department_manager_id': '15'},
        follow_redirects=True)
    check(b'does not belong' in r.data, 'Edit POST: cross-branch manager rejected')
    rows = q("SELECT department_manager_id FROM Department WHERE department_id=6")
    check(rows[0]['department_manager_id'] == 9,
          'Edit POST: existing manager (hafiz) kept after rejection')

    # Valid same-branch assignment still works
    r = c.post('/organization/department/6/edit', data={
        'department_name': 'Engineering', 'department_manager_id': '19'},
        follow_redirects=True)
    rows = q("SELECT department_manager_id FROM Department WHERE department_id=6")
    check(rows[0]['department_manager_id'] == 19,
          'Edit POST: same-branch manager (cheeseng) accepted')
    c.post('/organization/department/6/edit', data={
        'department_name': 'Engineering', 'department_manager_id': '9'},
        follow_redirects=True)
    rows = q("SELECT department_manager_id FROM Department WHERE department_id=6")
    check(rows[0]['department_manager_id'] == 9,
          'Edit POST: original manager restored (cleanup)')

# -- 7b. Posting form: depts without catalog positions are excluded -----------
tmp_dept = x("INSERT INTO Department(branch_id, department_name) VALUES(2, 'Tmp NoPos Dept')")
tmp_pos = None
try:
    with app.test_client() as c:
        login(c, 'admin@smarthr.my', 'Admin@123')
        r = c.get('/recruitment/postings/add')
        check(b'Tmp NoPos Dept' not in r.data,
              'Posting form: fresh dept with no catalog positions hidden')

    tmp_pos = x("INSERT INTO Position(position_name, department_id) VALUES(?,?)",
                ('Tmp NoPos Engineer', tmp_dept))
    with app.test_client() as c:
        login(c, 'admin@smarthr.my', 'Admin@123')
        r = c.get('/recruitment/postings/add')
        check(b'Tmp NoPos Dept' in r.data,
              'Posting form: dept appears once it has an active catalog position')
        check(b'Tmp NoPos Engineer' in r.data,
              'Posting form: its catalog position is listed')
finally:
    if tmp_pos:
        x("DELETE FROM Position WHERE position_id=?", (tmp_pos,))
    x("DELETE FROM Department WHERE department_id=?", (tmp_dept,))
    check(q("SELECT COUNT(*) c FROM Department WHERE department_name='Tmp NoPos Dept'")[0]['c'] == 0,
          'Posting form: temp dept cleaned up (FK-safe)')

# -- 8. Artifact cleanup -----------------------------------------------------
rows = q("SELECT COUNT(*) c FROM Branch WHERE name='New Branch'")
check(rows[0]['c'] == 0, "Branch 'New Branch' removed")
rows = q("SELECT COUNT(*) c FROM Department WHERE department_name IN ('New Department','New Department 2.0')")
check(rows[0]['c'] == 0, "Departments 'New Department' / 'New Department 2.0' removed")
rows = q("SELECT COUNT(*) c FROM Employee WHERE email IN ('newemployee@smarthr.my','newemployee2@smarthr.my')")
check(rows[0]['c'] == 0, 'Artifact employees removed')

# -- 9. Seed idempotence ------------------------------------------------------
def stable_snapshot():
    return (
        q("SELECT COUNT(*) c FROM Department")[0]['c'],
        q("SELECT COUNT(*) c FROM Position")[0]['c'],
        q("SELECT COUNT(*) c FROM Employee WHERE email IN ('coo@smarthr.my','cfo@smarthr.my')")[0]['c'],
        q("""SELECT COUNT(*) c FROM Department WHERE department_name='Branch Manager'""")[0]['c'],
    )

before = stable_snapshot()
init_db.seed_branch_manager_departments()
init_db.cleanup_demo_artifacts()
after = stable_snapshot()
check(before == after, 'Re-running seed + cleanup changes nothing (idempotent)')

print('=' * 60)
print(f'Results: {passed} passed, {failed} failed out of {passed + failed} tests')
if failed:
    print('*** SOME TESTS FAILED ***')
    sys.exit(1)
print('All tests passed!')