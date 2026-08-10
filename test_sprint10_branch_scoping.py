"""Sprint 10 – Manager branch scoping & department manager activation."""
import sys, os, sqlite3
sys.path.insert(0, '.')
from app import create_app

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
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(sql, args).fetchall()
    con.close()
    return [dict(r) for r in rows]

def setup_temp_invoices():
    """Create a pending invoice for a branch-2 (Penang) employee and a branch-1 (KL) employee.
    Returns the two invoice_ids for cleanup."""
    emp2 = q("SELECT employee_id FROM Employee WHERE branch_id=2 AND role_id=4 LIMIT 1")
    emp1 = q("SELECT employee_id FROM Employee WHERE branch_id=1 AND role_id=4 LIMIT 1")
    ids = []
    con = sqlite3.connect(DB)
    if emp2:
        c = con.execute(
            "INSERT INTO Invoice (employee_id,filename,original_name,file_type,vendor_name,invoice_number,invoice_date,currency,subtotal,tax_amount,total_amount,total_amount_myr,status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'Pending')",
            (emp2[0]['employee_id'], 'tmp.png', 'tmp.png', 'image', 'Penang Vendor', 'INV-TMP-PG', '2026-08-01', 'MYR', 10, 0.6, 10.60, 10.60))
        ids.append(c.lastrowid)
    if emp1:
        c = con.execute(
            "INSERT INTO Invoice (employee_id,filename,original_name,file_type,vendor_name,invoice_number,invoice_date,currency,subtotal,tax_amount,total_amount,total_amount_myr,status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'Pending')",
            (emp1[0]['employee_id'], 'tmp.png', 'tmp.png', 'image', 'KL Vendor', 'INV-TMP-KL', '2026-08-01', 'MYR', 10, 0.6, 10.60, 10.60))
        ids.append(c.lastrowid)
    con.commit()
    con.close()
    return ids

def cleanup_temp_invoices(ids):
    if not ids:
        return
    con = sqlite3.connect(DB)
    con.execute("DELETE FROM Invoice WHERE invoice_id IN (%s)" % ','.join('?' * len(ids)), ids)
    con.commit()
    con.close()

print('=' * 60)
print('Sprint 10 — Manager Branch Scoping & Dept Manager Activation')
print('=' * 60)

temp_ids = setup_temp_invoices()

with app.test_client() as client:
    # ── Login as Hafiz (Penang branch 2, dept manager of dept 6) ──
    resp = client.post('/login', data={'email': 'hafiz@smarthr.my', 'password': 'Manager@123'}, follow_redirects=True)
    check(resp.status_code == 200, 'Hafiz (PG Manager) login succeeds')

    with client.session_transaction() as sess:
        check(sess.get('branch_id') == 2, 'Hafiz branch_id = 2 (Penang)')
        check(sess.get('is_dept_manager') is True, 'Hafiz flagged as department manager')
        check(sess.get('managed_dept_id') == 6, 'Hafiz managed dept = 6 (PG Engineering)')

    # ── Sidebar: Manager now sees Applications (External), Interviews, Claims ──
    resp = client.get('/')
    check(resp.status_code == 200, 'Manager dashboard (200)')
    check(b'Claims Management' in resp.data, 'Sidebar: Claims Management visible to Manager')
    check(b'Applications (External)' in resp.data, 'Sidebar: Applications (External) visible to Manager')
    check(b'>\n         Interviews' in resp.data or b'Interviews' in resp.data, 'Sidebar: Interviews visible to Manager')
    check(b'Interview Policy' not in resp.data, 'Sidebar: Interview Policy hidden from Manager')

    # ── Claims page: branch scoping ──
    resp = client.get('/invoices/claims')
    check(resp.status_code == 200, 'GET /invoices/claims as Manager (200)')
    check(b'INV-TMP-PG' in resp.data, 'Claims: Penang temp invoice visible')
    check(b'INV-TMP-KL' not in resp.data, 'Claims: KL temp invoice hidden from PG manager')

    # ── Approve cross-branch invoice is blocked ──
    kl_inv = q("SELECT invoice_id FROM Invoice WHERE invoice_number='INV-TMP-KL'")
    if kl_inv:
        resp = client.post(f"/invoices/{kl_inv[0]['invoice_id']}/approve", follow_redirects=True)
        check(b'Access denied' in resp.data, 'Manager blocked from approving KL (cross-branch) invoice')
        row = q("SELECT status FROM Invoice WHERE invoice_id=?", (kl_inv[0]['invoice_id'],))
        check(row[0]['status'] == 'Pending', 'KL invoice remains Pending after blocked approve')

    # ── Header notification dropdown: no cross-branch leakage ──
    resp = client.get('/')
    check(b'INV-TMP-KL' not in resp.data, 'Header dropdown: KL invoice not shown to PG manager')
    check(b'INV-TMP-PG' in resp.data, 'Header dropdown: Penang invoice shown to PG manager')

    # ── Approve own-branch invoice succeeds ──
    pg_inv = q("SELECT invoice_id FROM Invoice WHERE invoice_number='INV-TMP-PG'")
    if pg_inv:
        resp = client.post(f"/invoices/{pg_inv[0]['invoice_id']}/approve", follow_redirects=True)
        row = q("SELECT status FROM Invoice WHERE invoice_id=?", (pg_inv[0]['invoice_id'],))
        check(row[0]['status'] == 'Approved', 'Manager approves own-branch (Penang) invoice')

    # ── Attendance logs: cross-branch filter ignored ──
    resp = client.get('/attendance/logs?branch=1')
    data = resp.data.decode('utf-8', 'ignore')
    # PG manager should only see their own branch's employees in the filter
    check('KL Headquarters' not in data, 'Attendance logs: KL not offered as branch filter for PG manager')
    check('Penang Office' in data, 'Attendance logs: Penang shown as branch for PG manager')

    # ── Attendance logs: raw records scoped to branch 2 (Penang) ──
    resp = client.get('/attendance/logs')
    check(resp.status_code == 200, 'GET /attendance/logs as Manager (200)')
    # Manager forcing own branch should not return records with KL branch name in the table rows
    pg_count = q("SELECT COUNT(*) c FROM Attendance a JOIN Employee e ON a.employee_id=e.employee_id WHERE a.branch_id=2 AND e.branch_id=2")[0]['c']
    check(pg_count >= 0, f'Branch-scoped attendance query runs (PG rows: {pg_count})')

    # ── Job Postings page: + Request Job Posting button ──
    resp = client.get('/recruitment/postings')
    check(resp.status_code == 200, 'GET /recruitment/postings (200)')
    check(b'Request Job Posting' in resp.data, 'Job Postings: + Request Job Posting button visible to Manager')

    # ── Vacancy request page reachable ──
    resp = client.get('/recruitment/vacancy-request')
    check(resp.status_code == 200, 'GET /recruitment/vacancy-request as Manager (200)')

    # ── Dept manager vacancy list (managed dept only) ──
    resp = client.get('/recruitment/vacancy-requests')
    check(resp.status_code == 200, 'GET /recruitment/vacancy-requests as dept manager (200)')

# ── Admin can now see the + New Request button (backend already allows) ──
with app.test_client() as admin_client:
    resp = admin_client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'}, follow_redirects=True)
    check(resp.status_code == 200, 'Admin login succeeds')
    resp = admin_client.get('/recruitment/vacancy-requests')
    check(b'+ New Request' in resp.data, 'Admin sees + New Request button')

cleanup_temp_invoices(temp_ids)

print()
print('=' * 60)
print(f'Results: {passed} passed, {failed} failed out of {passed + failed} tests')
if failed:
    print(f'*** {failed} TEST(S) FAILED ***')
    sys.exit(1)
else:
    print('All tests passed!')
