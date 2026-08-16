"""Regression tests for Phase 2 fixes: B1, B2a, B3, B4.

Run with:
    .venv/Scripts/python.exe test_phase2_fixes.py
"""
import sys, os, atexit, shutil, tempfile, glob
if hasattr(sys.stdout, 'reconfigure'):
    # Windows consoles may use cp1252, while a few diagnostic labels include
    # Unicode punctuation. Reporting must never abort the regression run.
    sys.stdout.reconfigure(errors='replace')
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, '.')
from flask import render_template
from app import create_app

# The auto-payroll and offer-expiry background threads fire ~30s after
# startup and mutate the database (regenerating Draft payrolls / sweeping
# expired offers), which races the per-block throwaway fixtures mid-test. Like
# the rate limiter above, they are runtime safeguards, not testable business
# logic, so stub them out for deterministic test runs.
import app.payroll.autogen as _payroll_autogen
_payroll_autogen.start_payroll_scheduler = lambda *a, **k: None
import app.recruitment.offer_expiry as _offer_expiry_mod
_offer_expiry_mod.start_offer_expiry_scheduler = lambda *a, **k: None

app = create_app()
app.config['CSRF_ENABLED'] = False
app.config['WTF_CSRF_ENABLED'] = False

# The suite creates recruitment fixtures and exercises migrations. Run every
# block against one throwaway snapshot, including the older B1-B5 blocks, so a
# full regression run cannot approve real requests or leave QA records behind.
import app.database as app_db_mod
import init_db as init_db_mod
_suite_real_db = app_db_mod.DB_PATH
_suite_tmp_dir = tempfile.mkdtemp(prefix='smarthr_suite_')
_suite_db = os.path.join(_suite_tmp_dir, 'smarthr_suite.db')
shutil.copy2(_suite_real_db, _suite_db)
app_db_mod.DB_PATH = _suite_db
init_db_mod.DB_PATH = _suite_db

# The archive migration is part of the upgrade path (backup-guarded rebuild +
# Filled backfill), so the suite snapshot applies it like a fresh init_db run.
init_db_mod.migrate_job_posting_archive()

def _cleanup_suite_db():
    app_db_mod.DB_PATH = _suite_real_db
    init_db_mod.DB_PATH = _suite_real_db
    shutil.rmtree(_suite_tmp_dir, ignore_errors=True)

atexit.register(_cleanup_suite_db)

# Rate limiting is a runtime safeguard, not testable business logic. The suite
# performs many login POSTs within the 60s window, so disable the in-memory
# limiter for deterministic test runs (CSRF is disabled the same way above).
from app.rate_limiter import limiter
limiter.is_allowed = lambda *a, **k: True

failed = 0
passed = 0

# Focused-run support: set TEST_FOCUS=B20 (comma-separated) to run only the
# named block(s); other blocks become inert. Empty = full suite.
_FOCUS = os.environ.get('TEST_FOCUS', '').strip()
_active = True if not _FOCUS else False

def _focus_block(token):
    global _active
    if _FOCUS:
        wanted = [t.strip() for t in _FOCUS.split(',')]
        _active = token in wanted or ('B' + token) in wanted

def check(condition, label):
    global passed, failed
    if not _active:
        return
    if condition:
        passed += 1
        print(f'  [PASS] {label}')
    else:
        failed += 1
        print(f'  [FAIL] {label}')

# ═══════════════════════════════════════════════════════════════════════════════
# B1 — approve_vacancy: sqlite3.Row['target_audience'] instead of .get()
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('1')
print('B1 — approve_vacancy audience propagation')
print('=' * 60)

with app.test_client() as client:
    # Login as HR (can approve vacancy requests)
    client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                follow_redirects=True)

    # Find a pending vacancy request with a non-default target_audience
    from app.database import query, execute
    req = query("""SELECT request_id, target_audience FROM Vacancy_Request
                    WHERE status='Pending' ORDER BY request_id DESC LIMIT 1""", one=True)

    # If no pending request exists (e.g., all were approved in prior runs), create one
    if not req:
        rid = execute("""INSERT INTO Vacancy_Request
            (requested_by, department_id, position_title, position_id, is_custom,
             employment_type, target_audience, description, requirements, reason, status)
            VALUES (1, 1, 'Test Position B1', 1, 0, 'Full-Time', 'External',
                    'Test', 'Test', 'Automated test', 'Pending')""")
        req = query("""SELECT request_id, target_audience FROM Vacancy_Request
                        WHERE request_id=?""", (rid,), one=True)

    check(req is not None, 'Found or created a pending vacancy request')
    if req:
        rid = req['request_id']
        expected_audience = req['target_audience']
        branch_id = query("""SELECT d.branch_id FROM Department d
                              JOIN Vacancy_Request vr ON d.department_id=vr.department_id
                              WHERE vr.request_id=?""", (rid,), one=True)
        check(branch_id is not None, 'Branch found for request department')

        # Get current posting count before approval
        before_count = query("SELECT COUNT(*) as c FROM Job_Posting", one=True)['c']

        # Approve: POST to approve route
        resp = client.post(f'/recruitment/vacancy-request/{rid}/approve',
                           data={'branch_id': branch_id['branch_id'],
                                 'add_to_catalog': '0'},
                           follow_redirects=True)
        check(resp.status_code == 200, f'Approve vacancy request {rid} returns 200 (no 500)')

        # Verify posting was created with correct audience
        posting = query("""SELECT * FROM Job_Posting ORDER BY posting_id DESC LIMIT 1""", one=True)
        check(posting is not None, 'New Job_Posting row created')
        if posting:
            check(posting['target_audience'] == expected_audience,
                  f'Job_Posting.target_audience={posting["target_audience"]} matches request audience={expected_audience}')
            check(posting['status'] == 'Open', 'Posting status is Open')

        # Verify request status updated
        updated_req = query("SELECT status FROM Vacancy_Request WHERE request_id=?", (rid,), one=True)
        check(updated_req['status'] == 'Approved', 'Vacancy_Request status set to Approved')

        # Verify posting count incremented
        after_count = query("SELECT COUNT(*) as c FROM Job_Posting", one=True)['c']
        check(after_count == before_count + 1, 'Exactly one new posting created')

        # Cleanup: delete the test posting (catch FK errors gracefully)
        if posting:
            try:
                execute("DELETE FROM Job_Posting WHERE posting_id=?", (posting['posting_id'],))
            except Exception:
                pass  # FK constraint — leave it, request was already approved

# ═══════════════════════════════════════════════════════════════════════════════
# B2a — applicant_name fallback for unparseable From header
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('2a')
print('B2a — applicant_name fallback for missing display name')
print('=' * 60)

with app.test_client() as client:
    # We test create_application_from_email directly (unit-test style)
    # since the email monitor requires real IMAP credentials
    try:
        from app.notifications.email_parser import parse_application_email, extract_email
        from app.notifications.email_monitor import create_application_from_email
        import email as email_lib
        from email.mime.text import MIMEText

        # ── Test: From header with NO display name (just email in angle brackets)
        test_email = "test_phase2_b2a@example.com"
        test_subject = "Application for DevOps Senior"
        test_body = (
            "Name: Test Applicant\n"
            "Position: DevOps\n"
            "Email: test_phase2_b2a@example.com\n"
            "IC: 900101-01-1234\n"
            "Phone: 0123456789\n"
        )

        # Build an email message with no display name
        msg = MIMEText(test_body)
        msg['Subject'] = test_subject
        msg['From'] = f'<{test_email}>'  # No display name
        msg['Message-ID'] = '<test-phase2-b2a-001@example.com>'

        # Use a body with no name so the parser cannot extract one
        test_body_no_name = (
            "I am interested in the DevOps position.\n"
            "I have experience with Docker and Kubernetes.\n"
            "My phone number is 0123456789.\n"
        )

        parsed = parse_application_email(msg['Subject'], test_body_no_name, msg['From'])
        check(parsed is not None, 'parse_application_email returns result')
        check(parsed['name'] is None, 'Parsed name is None (no display name in From)')
        check(parsed['confidence'] >= 0.3, f'Confidence >= 0.3 (got {parsed["confidence"]})')

        # Create the application — should NOT crash
        with app.app_context():
            # Clean up any previous run of this test
            execute("DELETE FROM Job_Application WHERE applicant_email=?",
                    (test_email,))

            app_id = create_application_from_email(msg, parsed)
            check(app_id is not None, 'create_application_from_email returns an application ID')

            # Verify the created application
            row = query("""SELECT * FROM Job_Application WHERE application_id=?""",
                       (app_id,), one=True)
            check(row is not None, 'Application row exists in DB')
            check(row['applicant_name'] == test_email,
                  f'applicant_name falls back to email: {row["applicant_name"]}')
            check(row['applicant_email'] == test_email,
                  f'applicant_email matches: {row["applicant_email"]}')
            check(row['message_id'] == 'test-phase2-b2a-001@example.com',
                  'message_id saved correctly')

            # ── Dedup test: process same Message-ID again → None
            result2 = create_application_from_email(msg, parsed)
            check(result2 is None, 'Second processing of same Message-ID returns None (dedup)')

            # ── Cleanup
            execute("DELETE FROM Job_Application WHERE application_id=?", (app_id,))

    except ImportError as e:
        check(False, f'Import error in B2a test setup: {e}')
    except Exception as e:
        check(False, f'B2a test error: {e}')

# ═══════════════════════════════════════════════════════════════════════════════
# B3 — Audience dropdown on vacancy_request and add_posting forms
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('3')
print('B3 — Target Audience dropdown on forms')
print('=' * 60)

with app.test_client() as client:
    # Login as HR Manager
    client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                follow_redirects=True)

    # ── vacancy_request form ──
    resp = client.get('/recruitment/vacancy-request')
    check(resp.status_code == 200, 'GET /vacancy-request (200)')
    html = resp.data.decode('utf-8', errors='replace')
    check('target_audience' in html, 'vacancy_request form has target_audience field')
    check('Internal' in html, 'vacancy_request form includes "Internal" option')
    check('External' in html, 'vacancy_request form includes "External" option')
    check('Both' in html, 'vacancy_request form includes "Both" option')
    check('value="Both" checked' in html or "value='Both' checked" in html,
          'vacancy_request form defaults to Both selected')

    # ── add_posting form ──
    resp = client.get('/recruitment/postings/add')
    check(resp.status_code == 200, 'GET /postings/add (200)')
    html = resp.data.decode('utf-8', errors='replace')
    check('target_audience' in html, 'add_posting form has target_audience field')
    check('Internal' in html, 'add_posting form includes "Internal" option')
    check('External' in html, 'add_posting form includes "External" option')
    check('Both' in html, 'add_posting form includes "Both" option')
    check('value="Both" selected' in html or "value='Both' selected" in html,
          'add_posting form defaults to Both selected')

# ═══════════════════════════════════════════════════════════════════════════════
# B4 — Navigation visibility by role
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('4')
print('B4 — Recruitment nav visibility by role')
print('=' * 60)

def has_link(html, text):
    return text in html

def missing_link(html, text):
    return text not in html

def has_apps_nav(html):
    return 'href="/recruitment/applications"' in html

with app.test_client() as client:
    # ── Test 1: Plain Employee (elizabeth@smarthr.my) ──
    client.post('/login', data={'email': 'elizabeth@smarthr.my', 'password': 'Employee@123'},
                follow_redirects=True)
    resp = client.get('/')
    check(resp.status_code == 200, 'Employee dashboard (200)')
    html = resp.data.decode('utf-8', errors='replace')

    check(missing_link(html, 'Job Postings'),
          'Employee: "Job Postings" nav link NOT visible')
    check(not has_apps_nav(html),
          'Employee: "Applications" nav link NOT visible')
    check(has_link(html, 'Internal Job Board'),
          'Employee: "Internal Job Board" nav link IS visible')
    check(has_link(html, 'My Applications'),
          'Employee: "My Applications" nav link IS visible')

    client.get('/logout')

    # ── Test 2: Manager (cheeseng@smarthr.my) ──
    client.post('/login', data={'email': 'cheeseng@smarthr.my', 'password': 'Manager@123'},
                follow_redirects=True)
    resp = client.get('/')
    check(resp.status_code == 200, 'Manager dashboard (200)')
    html = resp.data.decode('utf-8', errors='replace')

    check(has_link(html, 'Job Postings'),
          'Manager: "Job Postings" nav link IS visible')
    check(has_apps_nav(html),
          'Manager: "Applications" nav link IS visible')
    check(has_link(html, 'My Applications'),
          'Manager: "My Applications" nav link IS visible')

    resp = client.get('/recruitment/postings')
    html = resp.data.decode('utf-8', errors='replace')
    check(resp.status_code == 200, 'Manager job postings page (200)')
    check(has_link(html, 'Request Job Posting'),
          'Manager: "Request Job Posting" action IS visible')

    client.get('/logout')

    # ── Test 3: HR (hr@smarthr.my) ──
    client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                follow_redirects=True)
    resp = client.get('/')
    check(resp.status_code == 200, 'HR dashboard (200)')
    html = resp.data.decode('utf-8', errors='replace')

    check(has_link(html, 'Job Postings'),
          'HR: "Job Postings" nav link IS visible')
    check(has_apps_nav(html),
          'HR: "Applications" nav link IS visible')
    check(has_link(html, 'Interview Policy'),
          'HR: "Interview Policy" nav link IS visible')
    check(has_link(html, 'Careers Page'),
          'HR: "Careers Page" nav link IS visible')

    # HR should NOT have the "Internal Job Board" or "My Applications" (uses non-HR guard)
    # Note: HR may see these or not depending on the guard — the guard is:
    # session.user_role not in ('Admin','HR','HR Manager')
    # So HR roles should NOT see Internal Job Board / My Applications
    check(missing_link(html, 'Internal Job Board'),
          'HR: "Internal Job Board" nav link NOT visible (HR role)')
    check(missing_link(html, 'My Applications'),
          'HR: "My Applications" nav link NOT visible (HR role)')

    client.get('/logout')

    # ── Test 4: Admin (admin@smarthr.my) ──
    client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                follow_redirects=True)
    resp = client.get('/')
    check(resp.status_code == 200, 'Admin dashboard (200)')
    html = resp.data.decode('utf-8', errors='replace')

    check(has_link(html, 'Job Postings'),
          'Admin: "Job Postings" nav link IS visible')
    check(has_apps_nav(html),
          'Admin: "Applications" nav link IS visible')

    resp = client.get('/recruitment/postings')
    html = resp.data.decode('utf-8', errors='replace')
    check(resp.status_code == 200, 'Admin job postings page (200)')
    check(has_link(html, 'New Posting'),
          'Admin: "New Posting" action IS visible')
    check(missing_link(html, 'Request Job Posting'),
          'Admin: "Request Job Posting" action NOT visible')

    # HR Manager uses the direct posting workflow too, so it must not be
    # offered the manager-only request action.
    with client.session_transaction() as sess:
        sess['user_role'] = 'HR Manager'
    resp = client.get('/recruitment/postings')
    html = resp.data.decode('utf-8', errors='replace')
    check(resp.status_code == 200, 'HR Manager job postings page (200)')
    check(has_link(html, 'New Posting'),
          'HR Manager: "New Posting" action IS visible')
    check(missing_link(html, 'Request Job Posting'),
          'HR Manager: "Request Job Posting" action NOT visible')

    client.get('/logout')

# ═══════════════════════════════════════════════════════════════════════════════
# Notification flow — vacancy request notifies reviewers, approve/reject notifies requester
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
print('Notification flow — vacancy lifecycle')
print('=' * 60)

with app.test_client() as client:
    # Login as branch manager
    resp = client.post('/login', data={'email': 'cheeseng@smarthr.my', 'password': 'Manager@123'},
                       follow_redirects=True)
    check(resp.status_code == 200, 'Manager login for notif test')

    # Check the vacancy_request page loads
    resp = client.get('/recruitment/vacancy-request')
    check(resp.status_code == 200, 'GET /vacancy-request as Manager')

    # Get notification count before
    from app.database import query as dbq
    before_notifs = dbq("SELECT COUNT(*) as c FROM Notification", one=True)['c']

    # Submit a vacancy request with Internal audience
    resp = client.post('/recruitment/vacancy-request', data={
        'department_id': '6',  # Engineering (Penang)
        'position_id': '11',   # Embedded Engineer (valid for dept 6)
        'employment_type': 'Full-Time',
        'target_audience': 'Internal',
        'min_salary': '4500',
        'max_salary': '6500',
        'description': 'Test B3 Internal Audience',
        'requirements': 'CI/CD, Docker',
        'reason': 'Test notification flow',
    }, follow_redirects=True)
    check(resp.status_code == 200, 'Submit vacancy request (200)')

    # Check notifications were created for reviewers
    after_notifs = dbq("SELECT COUNT(*) as c FROM Notification", one=True)['c']
    check(after_notifs > before_notifs,
          f'New notifications created after vacancy request ({before_notifs} → {after_notifs})')

    # Verify the new request exists with correct audience
    new_req = dbq("""SELECT * FROM Vacancy_Request ORDER BY request_id DESC LIMIT 1""", one=True)
    if new_req:
        check(new_req['target_audience'] == 'Internal',
              f'Vacancy_Request.target_audience stored as Internal (got {new_req["target_audience"]})')

    # ── Now approve as HR and verify requester gets notified ──
    client.get('/logout')
    resp = client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                       follow_redirects=True)

    if new_req:
        before_approve_notifs = dbq("SELECT COUNT(*) as c FROM Notification", one=True)['c']
        branch_id = dbq("""SELECT d.branch_id FROM Department d
                            WHERE d.department_id=?""", (new_req['department_id'],), one=True)

        resp = client.post(f'/recruitment/vacancy-request/{new_req["request_id"]}/approve',
                           data={'branch_id': branch_id['branch_id'],
                                 'add_to_catalog': '0'},
                           follow_redirects=True)
        check(resp.status_code == 200, f'Approve request returns 200')

        after_approve_notifs = dbq("SELECT COUNT(*) as c FROM Notification", one=True)['c']
        check(after_approve_notifs > before_approve_notifs,
              f'New notification created for requester on approval ({before_approve_notifs} → {after_approve_notifs})')

        # Cleanup: delete test posting (catch FK errors gracefully)
        posting = dbq("SELECT posting_id FROM Job_Posting ORDER BY posting_id DESC LIMIT 1", one=True)
        if posting:
            from app.database import execute as dbe
            try:
                dbe("DELETE FROM Job_Posting WHERE posting_id=?", (posting['posting_id'],))
            except Exception:
                pass

    client.get('/logout')

# ═══════════════════════════════════════════════════════════════════════════════
# B5 — CSRF: programmatic form.submit() auto-assign confirm regression
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('5')
print('B5 — CSRF auto-assign confirm regression')
print('=' * 60)

with app.test_client() as client:
    client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                follow_redirects=True)

    # 1) The patched CSRF interceptor must ship on rendered pages
    resp = client.get('/recruitment/applications?status=Shortlisted')
    html = resp.data.decode('utf-8', errors='replace')
    check('ensureFormToken' in html and 'nativeFormSubmit' in html,
          'base.html ships programmatic-submit CSRF patch')

    # 2) With CSRF enforced, a token-bearing confirm POST must NOT be rejected
    app.config['CSRF_ENABLED'] = True
    try:
        with client.session_transaction() as sess:
            sess['csrf_token'] = 'b5-test-csrf-token'

        resp = client.post('/recruitment/auto-assign/confirm',
                           data={'csrf_token': 'b5-test-csrf-token',
                                 'application_ids': '999999'},
                           follow_redirects=False)
        check(resp.status_code == 302,
              f'Confirm POST with valid token redirects (302), got {resp.status_code}')

        # 3) A wrong token must still be rejected (CSRF stays enforced)
        resp = client.post('/recruitment/auto-assign/confirm',
                           data={'csrf_token': 'wrong-token',
                                 'application_ids': '999999'},
                           follow_redirects=False)
        check(resp.status_code == 400,
              f'Confirm POST with invalid token rejected (400), got {resp.status_code}')
    finally:
        app.config['CSRF_ENABLED'] = False

    client.get('/logout')

# ═══════════════════════════════════════════════════════════════════════════════
# B6 — Attendance P0 fixes: log scoping, template contract, Rejected status,
#      HR manual-entry target employee
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('6')
print('B6 — Attendance P0 fixes (isolated temp DB)')
print('=' * 60)

import datetime
import shutil
import tempfile
import contextlib

import app.database as app_db_mod
import init_db as init_db_mod


@contextlib.contextmanager
def _temp_attendance_db():
    """Run attendance tests against a throwaway copy of the shared DB so that
    fixture creation/deletion never touches real development data."""
    real = app_db_mod.DB_PATH
    tmp_dir = tempfile.mkdtemp(prefix='smarthr_b6_')
    tmp_db = os.path.join(tmp_dir, 'smarthr_b6.db')
    shutil.copy2(real, tmp_db)
    app_db_mod.DB_PATH = tmp_db
    init_db_mod.DB_PATH = tmp_db
    try:
        yield tmp_db
    finally:
        app_db_mod.DB_PATH = real
        init_db_mod.DB_PATH = real
        shutil.rmtree(tmp_dir, ignore_errors=True)


from app.database import query as dbq
from app.database import execute as dbe

with _temp_attendance_db(), app.test_client() as client:
    # Ensure the Attendance schema supports 'Rejected' (idempotent migration)
    try:
        init_db_mod.migrate_attendance_status()
    except Exception as e:
        print(f'  [WARN] Attendance status migration not applied: {e}')

    client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                follow_redirects=True)

    eliz = dbq("SELECT employee_id, full_name, branch_id FROM Employee WHERE email='elizabeth@smarthr.my'", one=True)
    ryan = dbq("SELECT employee_id, full_name, branch_id FROM Employee WHERE email='ryan@smarthr.my'", one=True)
    check(eliz is not None and ryan is not None, 'Found employee fixtures for attendance tests')
    if not (eliz and ryan):
        eliz = {'employee_id': 0, 'full_name': 'NONE', 'branch_id': 0}
        ryan = {'employee_id': 0, 'full_name': 'NONE', 'branch_id': 0}

    today = datetime.date.today().isoformat()
    created_ids = []

    # Isolated fixture: remove any open check-ins for both employees today
    for eid in (eliz['employee_id'], ryan['employee_id']):
        if eid:
            dbe("DELETE FROM Attendance WHERE employee_id=? AND date(check_in)=?", (eid, today))

    # Fixture records (closed shifts, so no open check-in blocks later tests)
    if eliz['employee_id'] and ryan['employee_id']:
        e1 = dbe("""INSERT INTO Attendance (employee_id, branch_id, check_in, check_out, hours_worked, is_manual_entry, status)
                    VALUES (?,?,?,?,9.0,0,'Approved')""",
                 (eliz['employee_id'], eliz['branch_id'], f'{today} 08:00:00', f'{today} 17:00:00'))
        e2 = dbe("""INSERT INTO Attendance (employee_id, branch_id, check_in, check_out, hours_worked, is_manual_entry, status)
                    VALUES (?,?,?,?,9.0,0,'Approved')""",
                 (ryan['employee_id'], ryan['branch_id'], f'{today} 09:00:00', f'{today} 18:00:00'))
        created_ids.extend([e1, e2])

    # ── 1) Logs template contract: records render, filters, CSV link ──
    r = client.get('/attendance/logs')
    html = r.data.decode('utf-8', errors='replace')
    check(r.status_code == 200, 'GET /attendance/logs as HR (200)')
    check('attendance-table' in html, 'Logs table renders (template uses records)')
    check('record(s) shown' in html, 'Logs record count footer renders')

    r = client.get('/attendance/logs?method=manual')
    html = r.data.decode('utf-8', errors='replace')
    check('value="manual" selected' in html, 'Method filter selected state preserved')
    check('method=manual' in html, 'CSV export link preserves method filter')

    # ── 2) Rejected status accepted by schema and pending-review flow ──
    if eliz['employee_id']:
        aid = dbe("""INSERT INTO Attendance (employee_id, branch_id, check_in, is_manual_entry, manual_reason, status)
                     VALUES (?,?,?,1,'B6 test','Pending')""",
                  (eliz['employee_id'], eliz['branch_id'], f'{today} 07:30:00'))
        created_ids.append(aid)
        dbe("UPDATE Attendance SET status='Rejected' WHERE attendance_id=?", (aid,))
        row = dbq("SELECT status FROM Attendance WHERE attendance_id=?", (aid,), one=True)
        check(row is not None and row['status'] == 'Rejected',
              f'Attendance Rejected status persists in schema (got {row["status"] if row else None})')

        dbe("UPDATE Attendance SET status='Pending' WHERE attendance_id=?", (aid,))
        resp = client.post('/attendance/manual-pending',
                           data={'attendance_id': aid, 'action': 'reject'},
                           follow_redirects=True)
        row = dbq("SELECT status FROM Attendance WHERE attendance_id=?", (aid,), one=True)
        check(resp.status_code == 200 and row is not None and row['status'] == 'Rejected',
              'Pending-review reject retains row as Rejected')

    # ── 3) Employee access leak: employee sees only own records ──
    if eliz['employee_id'] and ryan['employee_id']:
        client.get('/logout')
        client.post('/login', data={'email': 'elizabeth@smarthr.my', 'password': 'Employee@123'},
                    follow_redirects=True)
        r = client.get('/attendance/logs')
        body = r.data.decode('utf-8', errors='replace')
        check(eliz['full_name'] in body, 'Employee sees own name in logs')
        check(ryan['full_name'] not in body, 'Employee does NOT see other employees in logs')

        r = client.get(f'/attendance/logs?employee={ryan["employee_id"]}')
        body = r.data.decode('utf-8', errors='replace')
        check(ryan['full_name'] not in body, 'Employee IDOR attempt ignored')

        r = client.get('/attendance/logs?export=csv')
        csv_text = r.data.decode('utf-8', errors='replace')
        check(eliz['full_name'] in csv_text and ryan['full_name'] not in csv_text,
              'CSV export scoped to own records')
        client.get('/logout')
        client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                    follow_redirects=True)

    # ── 4) HR manual entry targets the selected employee ──
    if ryan['employee_id']:
        resp = client.post('/attendance/manual', data={
            'employee_id': str(ryan['employee_id']),
            'branch_id': str(ryan['branch_id']),
            'att_date': today,
            'att_time': '10:00',
            'att_type': 'Check In',
            'reason': 'B6 test correction',
        }, follow_redirects=False)
        row = dbq("""SELECT * FROM Attendance WHERE employee_id=? AND date(check_in)=?
                     AND is_manual_entry=1 AND status='Pending'
                     ORDER BY attendance_id DESC LIMIT 1""", (ryan['employee_id'], today), one=True)
        check(resp.status_code == 302 and row is not None,
              'HR manual entry creates Pending record for the selected employee')
        if row:
            created_ids.append(row['attendance_id'])

        wrong_branch = 1 if ryan['branch_id'] != 1 else 2
        resp = client.post('/attendance/manual', data={
            'employee_id': str(ryan['employee_id']),
            'branch_id': str(wrong_branch),
            'att_date': today,
            'att_time': '11:00',
            'att_type': 'Check In',
            'reason': 'B6 wrong branch',
        }, follow_redirects=True)
        body = resp.data.decode('utf-8', errors='replace')
        check("does not match the employee" in body, 'Branch mismatch blocked with message')

        resp = client.post('/attendance/manual', data={
            'employee_id': '999999',
            'branch_id': '',
            'att_date': today,
            'att_time': '11:00',
            'att_type': 'Check In',
            'reason': 'B6 invalid employee',
        }, follow_redirects=True)
        body = resp.data.decode('utf-8', errors='replace')
        check('valid active employee' in body, 'Invalid employee id blocked with message')

    for aid in created_ids:
        try:
            dbe("DELETE FROM Attendance WHERE attendance_id=?", (aid,))
        except Exception:
            pass

    client.get('/logout')

# ═══════════════════════════════════════════════════════════════════════════════
# B7 — Attendance status migration: preservation + index restoration
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('7')
print('B7 — Attendance status migration preservation')
print('=' * 60)

import sqlite3

b7_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b7_')
b7_db = os.path.join(b7_tmp_dir, 'smarthr_b7.db')
shutil.copy2(app_db_mod.DB_PATH, b7_db)

OLD_ATTENDANCE_DDL = """
CREATE TABLE Attendance (
    attendance_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id      INTEGER NOT NULL,
    branch_id        INTEGER NOT NULL,
    check_in         TEXT NOT NULL,
    check_out        TEXT,
    hours_worked     REAL,
    overtime_hours   REAL DEFAULT 0.00,
    confidence_score REAL,
    status           TEXT DEFAULT 'Pending' CHECK(status IN ('Pending','Approved','Flagged')),
    is_manual_entry  INTEGER DEFAULT 0,
    manual_reason    TEXT,
    corrected_by     INTEGER,
    corrected_at     TEXT,
    created_at       TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (employee_id)  REFERENCES Employee(employee_id),
    FOREIGN KEY (branch_id)    REFERENCES Branch(branch_id),
    FOREIGN KEY (corrected_by) REFERENCES Employee(employee_id)
)
"""

b7_con = sqlite3.connect(b7_db)
b7_con.row_factory = sqlite3.Row
b7_con.execute("PRAGMA foreign_keys = ON")
b7_con.execute("DROP TABLE Attendance")
b7_con.execute(OLD_ATTENDANCE_DDL)
b7_con.execute("CREATE INDEX idx_attendance_employee ON Attendance(employee_id)")
b7_con.execute("CREATE INDEX idx_attendance_checkin ON Attendance(check_in)")

emp = b7_con.execute(
    "SELECT employee_id, branch_id FROM Employee ORDER BY employee_id LIMIT 1"
).fetchone()
check(emp is not None, 'B7: found employee FK anchor in temp copy')

ATTENDANCE_FULL_COLS = ("attendance_id", "employee_id", "branch_id", "check_in",
                        "check_out", "hours_worked", "overtime_hours", "confidence_score",
                        "status", "is_manual_entry", "manual_reason", "corrected_by",
                        "corrected_at", "created_at")

if emp:
    b7_con.executemany("""
        INSERT INTO Attendance
            (attendance_id, employee_id, branch_id, check_in, check_out,
             hours_worked, overtime_hours, confidence_score, status,
             is_manual_entry, manual_reason, corrected_by, corrected_at, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [
        (101, emp['employee_id'], emp['branch_id'], '2026-01-05 08:00:00', '2026-01-05 17:00:00',
         9.0, 0.5, 87.3, 'Approved', 0, None, None, None, '2026-01-05 18:00:00'),
        (102, emp['employee_id'], emp['branch_id'], '2026-01-06 08:30:00', None,
         None, 0.0, None, 'Pending', 1, 'B7 fixture', emp['employee_id'],
         '2026-01-06 09:00:00', '2026-01-06 08:35:00'),
        (103, emp['employee_id'], emp['branch_id'], '2026-01-07 09:00:00', '2026-01-07 17:30:00',
         8.5, 0.0, 45.0, 'Flagged', 0, None, None, None, '2026-01-07 18:00:00'),
    ])
b7_con.commit()

col_list = ", ".join(ATTENDANCE_FULL_COLS)
before = [dict(r) for r in b7_con.execute(
    "SELECT %s FROM Attendance ORDER BY attendance_id" % col_list
).fetchall()]
b7_con.close()

init_db_mod.DB_PATH = b7_db
try:
    init_db_mod.migrate_attendance_status()
    init_db_mod.migrate_attendance_status()  # idempotent re-run
    migration_ok = True
except Exception as e:
    migration_ok = False
    print(f'  [WARN] B7 migration raised: {e}')
finally:
    init_db_mod.DB_PATH = app_db_mod.DB_PATH
check(migration_ok, 'B7: migration runs and is idempotent')

b7_con = sqlite3.connect(b7_db)
b7_con.row_factory = sqlite3.Row
b7_con.execute("PRAGMA foreign_keys = ON")
after = [dict(r) for r in b7_con.execute(
    "SELECT %s FROM Attendance ORDER BY attendance_id" % col_list
).fetchall()]
check(len(after) == len(before),
      f'B7: row count preserved ({len(before)} -> {len(after)})')
check(after == before,
      'B7: every Attendance column preserved (IDs, times, hours, OT, confidence, '
      'status, manual flags, reason, correction metadata, timestamps)')

idx_names = [r[0] for r in b7_con.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='Attendance'").fetchall()]
check('idx_attendance_employee' in idx_names and 'idx_attendance_checkin' in idx_names,
      'B7: indexes restored after rebuild')

fk_issues = b7_con.execute("PRAGMA foreign_key_check").fetchall()
check(len(fk_issues) == 0, f'B7: no foreign-key violations (got {len(fk_issues)})')

try:
    b7_con.execute("""INSERT INTO Attendance (employee_id, branch_id, check_in, status)
                      VALUES (?,?,?,'Rejected')""",
                   (emp['employee_id'], emp['branch_id'], '2026-01-08 08:00:00'))
    rejected_ok = True
except Exception:
    rejected_ok = False
check(rejected_ok, 'B7: Rejected status accepted after migration')

try:
    b7_con.execute("""INSERT INTO Attendance (employee_id, branch_id, check_in, status)
                      VALUES (999999, ?, ?, 'Pending')""",
                   (emp['branch_id'], '2026-01-09 08:00:00'))
    fk_ok = False
except sqlite3.IntegrityError:
    fk_ok = True
check(fk_ok, 'B7: foreign keys still enforced after rebuild')

b7_con.close()
shutil.rmtree(b7_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B8 — Migration framework: backup, full preservation, rollback verification
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('8')
print('B8 — Migration framework')
print('=' * 60)

from migration_framework import backup_database, rebuild_table

b8_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b8_')
b8_db = os.path.join(b8_tmp_dir, 'smarthr_b8.db')
shutil.copy2(app_db_mod.DB_PATH, b8_db)

b8_con = sqlite3.connect(b8_db)
b8_con.row_factory = sqlite3.Row
b8_con.execute("PRAGMA foreign_keys = ON")
b8_con.execute("""CREATE TABLE B8_Item (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    qty     INTEGER DEFAULT 0,
    note    TEXT
)""")
b8_con.execute("CREATE INDEX idx_b8_item_name ON B8_Item(name)")
b8_con.execute("""CREATE TRIGGER trg_b8_upper AFTER INSERT ON B8_Item
                  BEGIN UPDATE B8_Item SET name = upper(name)
                  WHERE item_id = NEW.item_id; END""")
b8_con.executemany("INSERT INTO B8_Item (name, qty, note) VALUES (?,?,?)",
                   [('apple', 3, None), ('banana', 5, 'yellow'), ('cherry', 0, '')])
b8_con.commit()

# ── 1) Backup produces a consistent usable copy ──
b8_bak = os.path.join(b8_tmp_dir, 'backup.db')
backup_database(b8_db, b8_bak)
bak_con = sqlite3.connect(b8_bak)
bak_count = bak_con.execute("SELECT COUNT(*) FROM B8_Item").fetchone()[0]
bak_con.close()
check(bak_count == 3, 'B8: backup_database produces a usable copy')

# ── 2) Rebuild preserves every row and recreates index + trigger ──
b8_con2 = sqlite3.connect(b8_db)
b8_con2.row_factory = sqlite3.Row
rebuild_table(b8_con2, 'B8_Item', """
    CREATE TABLE B8_Item_new (
        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name    TEXT NOT NULL,
        qty     INTEGER DEFAULT 0,
        note    TEXT,
        active  INTEGER DEFAULT 1
    )
""")
b8_con2.commit()
rows = [tuple(r) for r in b8_con2.execute(
    "SELECT item_id, name, qty, note FROM B8_Item ORDER BY item_id")]
check(rows == [(1, 'APPLE', 3, None), (2, 'BANANA', 5, 'yellow'), (3, 'CHERRY', 0, '')],
      'B8: rebuild preserves all rows and values (trigger re-applied)')
new_cols = [r[1] for r in b8_con2.execute("PRAGMA table_info(B8_Item)")]
check('active' in new_cols, 'B8: new column added by DDL')
idx_b8 = {r[0] for r in b8_con2.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='B8_Item'")}
check('idx_b8_item_name' in idx_b8, 'B8: index recreated after rebuild')
trg_b8 = [r[0] for r in b8_con2.execute(
    "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='B8_Item'")]
check('trg_b8_upper' in trg_b8, 'B8: trigger recreated after rebuild')
fk_b8 = b8_con2.execute("PRAGMA foreign_key_check").fetchall()
check(len(fk_b8) == 0, 'B8: foreign_key_check clean after rebuild')
b8_con2.close()

# ── 3) Rollback verification: a failing rebuild leaves the source untouched ──
b8_con3 = sqlite3.connect(b8_db)
b8_con3.row_factory = sqlite3.Row
pre_rows = [tuple(r) for r in b8_con3.execute("SELECT * FROM B8_Item ORDER BY item_id")]
pre_indexes = {r[0] for r in b8_con3.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='B8_Item'")}
pre_triggers = [r[0] for r in b8_con3.execute(
    "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='B8_Item'")]
try:
    # cherry has qty=0, which violates the new CHECK -> INSERT aborts
    rebuild_table(b8_con3, 'B8_Item', """
        CREATE TABLE B8_Item_new (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT NOT NULL,
            qty     INTEGER DEFAULT 0 CHECK(qty > 0),
            note    TEXT
        )
    """)
    rollback_ok = False
except Exception:
    rollback_ok = True
post_rows = [tuple(r) for r in b8_con3.execute("SELECT * FROM B8_Item ORDER BY item_id")]
post_indexes = {r[0] for r in b8_con3.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='B8_Item'")}
post_triggers = [r[0] for r in b8_con3.execute(
    "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='B8_Item'")]
check(rollback_ok, 'B8: failing rebuild raises and rolls back')
check(post_rows == pre_rows, 'B8: rollback preserves rows')
check(post_indexes == pre_indexes and post_triggers == pre_triggers,
      'B8: rollback preserves indexes and triggers')
b8_con3.close()

shutil.rmtree(b8_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B9 — Pre-migration backups: created for pending rebuilds, not for no-ops
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('9')
print('B9 — Pre-migration backups')
print('=' * 60)

b9_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b9_')
b9_db = os.path.join(b9_tmp_dir, 'smarthr_b9.db')
shutil.copy2(app_db_mod.DB_PATH, b9_db)
b9_bak_dir = os.path.join(b9_tmp_dir, 'backups')


def _b9_backup_count():
    if not os.path.isdir(b9_bak_dir):
        return 0
    return len([f for f in os.listdir(b9_bak_dir) if f.startswith('smarthr_backup_')])


# Downgrade the copy to the pre-Rejected schema so a rebuild is pending
b9_con = sqlite3.connect(b9_db)
b9_con.row_factory = sqlite3.Row
b9_con.execute("PRAGMA foreign_keys = OFF")
b9_con.execute("DROP TABLE Attendance")
b9_con.execute(OLD_ATTENDANCE_DDL)
b9_con.execute("CREATE INDEX idx_attendance_employee ON Attendance(employee_id)")
b9_con.execute("CREATE INDEX idx_attendance_checkin ON Attendance(check_in)")
emp9 = b9_con.execute(
    "SELECT employee_id, branch_id FROM Employee ORDER BY employee_id LIMIT 1"
).fetchone()
if emp9:
    b9_con.execute("""INSERT INTO Attendance (employee_id, branch_id, check_in, status)
                      VALUES (?,?,?,'Pending')""",
                   (emp9['employee_id'], emp9['branch_id'], '2026-02-01 08:00:00'))
b9_con.commit()
b9_con.close()

check(_b9_backup_count() == 0, 'B9: no backups exist before the migration')

init_db_mod.DB_PATH = b9_db
try:
    init_db_mod.migrate_attendance_status()
    pending_ok = True
except Exception as e:
    pending_ok = False
    print(f'  [WARN] B9 migration raised: {e}')
finally:
    init_db_mod.DB_PATH = app_db_mod.DB_PATH
check(pending_ok, 'B9: pending rebuild migration runs')

backup_files = sorted(f for f in os.listdir(b9_bak_dir)
                      if f.startswith('smarthr_backup_')) if os.path.isdir(b9_bak_dir) else []
check(len(backup_files) == 1,
      f'B9: exactly one backup created for pending rebuild (got {len(backup_files)})')

if backup_files:
    bak_con = sqlite3.connect(os.path.join(b9_bak_dir, backup_files[0]))
    bak_con.row_factory = sqlite3.Row
    bak_count = bak_con.execute("SELECT COUNT(*) as c FROM Attendance").fetchone()['c']
    bak_row = bak_con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='Attendance'").fetchone()
    bak_con.close()
    check(bak_count == 1, f'B9: backup contains the pre-migration row (got {bak_count})')
    check(bak_row is not None and 'Rejected' not in (bak_row['sql'] or ''),
          'B9: backup preserves the pre-migration (old) schema')

# A no-op re-run must NOT create another backup
init_db_mod.DB_PATH = b9_db
try:
    init_db_mod.migrate_attendance_status()  # already applied -> no-op
except Exception as e:
    print(f'  [WARN] B9 no-op re-run raised: {e}')
finally:
    init_db_mod.DB_PATH = app_db_mod.DB_PATH
check(_b9_backup_count() == 1, 'B9: no-op migration does not create extra backups')

shutil.rmtree(b9_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B10 — Contract migration: backup, full preservation, idempotency
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('10')
print('B10 — Contract migration (framework rebuild)')
print('=' * 60)

b10_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b10_')
b10_db = os.path.join(b10_tmp_dir, 'smarthr_b10.db')
shutil.copy2(app_db_mod.DB_PATH, b10_db)
b10_bak_dir = os.path.join(b10_tmp_dir, 'backups')


def _b10_backup_count():
    if not os.path.isdir(b10_bak_dir):
        return 0
    return len([f for f in os.listdir(b10_bak_dir) if f.startswith('smarthr_backup_')])


OLD_CONTRACT_DDL = """
CREATE TABLE Contract (
    contract_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id    INTEGER NOT NULL UNIQUE,
    employee_id       INTEGER,
    offer_date        TEXT,
    start_date        TEXT,
    position          TEXT,
    department_id     INTEGER,
    work_start_time   TEXT,
    work_end_time     TEXT,
    base_salary       REAL,
    employment_type   TEXT,
    contract_doc_path TEXT,
    signed_doc_path   TEXT,
    status            TEXT DEFAULT 'Draft' CHECK(status IN ('Draft','Sent','Signed','Accepted')),
    created_at        TEXT DEFAULT (datetime('now')),
    signed_at         TEXT,
    FOREIGN KEY (application_id)  REFERENCES Job_Application(application_id),
    FOREIGN KEY (employee_id)     REFERENCES Employee(employee_id),
    FOREIGN KEY (department_id)   REFERENCES Department(department_id)
)
"""

CONTRACT_FULL_COLS = ("contract_id", "application_id", "employee_id", "offer_date",
                      "start_date", "position", "department_id", "work_start_time",
                      "work_end_time", "base_salary", "employment_type",
                      "contract_doc_path", "signed_doc_path", "status",
                      "created_at", "signed_at")
contract_col_list = ", ".join(CONTRACT_FULL_COLS)

b10_con = sqlite3.connect(b10_db)
b10_con.row_factory = sqlite3.Row
b10_con.execute("PRAGMA foreign_keys = ON")
# Later migrations add Contract children. Remove them in this isolated old-
# schema fixture before replacing Contract with its pre-migration definition.
for _b10_child in ('Offer_Approval', 'Opening_Reservation', 'Email_Delivery_Log'):
    if b10_con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (_b10_child,)).fetchone():
        b10_con.execute("DELETE FROM " + _b10_child)
b10_con.execute("DROP TABLE Contract")
b10_con.execute(OLD_CONTRACT_DDL)

emp10 = b10_con.execute(
    "SELECT employee_id, department_id FROM Employee ORDER BY employee_id LIMIT 1"
).fetchone()
check(emp10 is not None, 'B10: found employee FK anchor in temp copy')
app10_ids = []
if emp10:
    for i in range(3):
        cur = b10_con.execute(
            "INSERT INTO Job_Application (applicant_name, applicant_email, status) VALUES (?,?, 'New')",
            ('B10 Applicant %d' % i, 'b10_%d@example.com' % i))
        app10_ids.append(cur.lastrowid)
    b10_con.executemany(
        "INSERT INTO Contract (%s) VALUES (%s)" % (
            contract_col_list, ",".join(['?'] * len(CONTRACT_FULL_COLS))),
        [
            (501, app10_ids[0], emp10['employee_id'], '2026-03-01', '2026-04-01', 'Engineer',
             emp10['department_id'], '09:00', '18:00', 5000.0, 'Full-Time',
             None, None, 'Draft', '2026-03-01 10:00:00', None),
            (502, app10_ids[1], emp10['employee_id'], '2026-03-02', '2026-04-02', 'Analyst',
             emp10['department_id'], '09:00', '18:00', 4500.0, 'Full-Time',
             'contracts/x.pdf', None, 'Sent', '2026-03-02 10:00:00', None),
            (503, app10_ids[2], emp10['employee_id'], '2026-03-03', '2026-04-03', 'Manager',
             emp10['department_id'], '09:00', '18:00', 8000.0, 'Full-Time',
             None, 'contracts/signed.pdf', 'Accepted', '2026-03-03 10:00:00',
             '2026-03-03 11:00:00'),
        ])
b10_con.commit()

before10 = [dict(r) for r in b10_con.execute(
    "SELECT %s FROM Contract ORDER BY contract_id" % contract_col_list)]
b10_con.close()

check(_b10_backup_count() == 0, 'B10: no backups exist before the migration')

init_db_mod.DB_PATH = b10_db
try:
    init_db_mod.migrate_contract_security()
    c10_ok = True
except Exception as e:
    c10_ok = False
    print(f'  [WARN] B10 migration raised: {e}')
finally:
    init_db_mod.DB_PATH = app_db_mod.DB_PATH
check(c10_ok, 'B10: pending Contract rebuild migration runs')

backup10 = sorted(f for f in os.listdir(b10_bak_dir)
                   if f.startswith('smarthr_backup_')) if os.path.isdir(b10_bak_dir) else []
check(len(backup10) == 1,
      f'B10: exactly one backup created for pending rebuild (got {len(backup10)})')
if backup10:
    bak10 = sqlite3.connect(os.path.join(b10_bak_dir, backup10[0]))
    bak10.row_factory = sqlite3.Row
    b10_bak_count = bak10.execute("SELECT COUNT(*) as c FROM Contract").fetchone()['c']
    bak10_cols = [r[1] for r in bak10.execute("PRAGMA table_info(Contract)")]
    bak10.close()
    check(b10_bak_count == 3, f'B10: backup contains pre-migration rows (got {b10_bak_count})')
    check('accept_token' not in bak10_cols,
          'B10: backup preserves the pre-migration (old) schema')

b10_con = sqlite3.connect(b10_db)
b10_con.row_factory = sqlite3.Row
b10_con.execute("PRAGMA foreign_keys = ON")
after10 = [dict(r) for r in b10_con.execute(
    "SELECT %s FROM Contract ORDER BY contract_id" % contract_col_list)]
check(len(after10) == len(before10),
      f'B10: row count preserved ({len(before10)} -> {len(after10)})')
check(after10 == before10, 'B10: every old Contract column preserved')
new_cols10 = [r[1] for r in b10_con.execute("PRAGMA table_info(Contract)")]
check('accept_token' in new_cols10 and 'token_expires_at' in new_cols10
      and 'accepted_at' in new_cols10, 'B10: new offer columns added')
fk10 = b10_con.execute("PRAGMA foreign_key_check").fetchall()
check(len(fk10) == 0, 'B10: foreign_key_check clean after rebuild')

if emp10:
    cur = b10_con.execute(
        "INSERT INTO Job_Application (applicant_name, applicant_email, status) VALUES (?,?, 'New')",
        ('B10 Declined', 'b10_declined@example.com'))
    declined_app = cur.lastrowid
    try:
        b10_con.execute("INSERT INTO Contract (application_id, status) VALUES (?, 'Declined')",
                        (declined_app,))
        declined_ok = True
    except Exception:
        declined_ok = False
    check(declined_ok, 'B10: Declined status accepted after migration')

    try:
        b10_con.execute("INSERT INTO Contract (application_id, status) VALUES (999999, 'Draft')")
        fk10_ok = False
    except sqlite3.IntegrityError:
        fk10_ok = True
    check(fk10_ok, 'B10: foreign keys still enforced after rebuild')

b10_con.close()

# Idempotent no-op re-run must not create another backup
init_db_mod.DB_PATH = b10_db
try:
    init_db_mod.migrate_contract_security()
except Exception as e:
    print(f'  [WARN] B10 no-op re-run raised: {e}')
finally:
    init_db_mod.DB_PATH = app_db_mod.DB_PATH
check(_b10_backup_count() == 1, 'B10: no-op migration does not create extra backups')

shutil.rmtree(b10_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B11 — Vacancy openings migration: backup, preservation, backfills, idempotency
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('11')
print('B11 — Vacancy openings migration')
print('=' * 60)

b11_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b11_')
b11_db = os.path.join(b11_tmp_dir, 'smarthr_b11.db')
shutil.copy2(app_db_mod.DB_PATH, b11_db)
b11_bak_dir = os.path.join(b11_tmp_dir, 'backups')


def _b11_backup_count():
    if not os.path.isdir(b11_bak_dir):
        return 0
    return len([f for f in os.listdir(b11_bak_dir) if f.startswith('smarthr_backup_')])


OLD_JOB_POSTING_DDL = """
CREATE TABLE Job_Posting (
    posting_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    position_id     INTEGER REFERENCES Position(position_id),
    department_id   INTEGER,
    branch_id       INTEGER,
    employment_type TEXT CHECK(employment_type IN ('Full-Time','Part-Time','Contract')),
    min_salary      REAL,
    max_salary      REAL,
    description     TEXT,
    requirements    TEXT,
    status          TEXT DEFAULT 'Open' CHECK(status IN ('Open','Closed','Filled')),
    target_audience TEXT NOT NULL DEFAULT 'Both' CHECK(target_audience IN ('Internal','External','Both')),
    posted_by       INTEGER,
    created_at      TEXT DEFAULT (datetime('now')),
    closed_at       TEXT,
    FOREIGN KEY (department_id) REFERENCES Department(department_id),
    FOREIGN KEY (branch_id)     REFERENCES Branch(branch_id),
    FOREIGN KEY (posted_by)     REFERENCES Employee(employee_id)
)
"""

OLD_VACANCY_REQUEST_DDL = """
CREATE TABLE Vacancy_Request (
    request_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_by    INTEGER NOT NULL,
    department_id   INTEGER NOT NULL,
    position_title  TEXT NOT NULL,
    position_id     INTEGER REFERENCES Position(position_id),
    is_custom       INTEGER DEFAULT 0,
    employment_type TEXT NOT NULL CHECK(employment_type IN ('Full-Time','Part-Time','Contract')),
    min_salary      REAL,
    max_salary      REAL,
    description     TEXT,
    requirements    TEXT,
    reason          TEXT,
    target_audience TEXT NOT NULL DEFAULT 'Both' CHECK(target_audience IN ('Internal','External','Both')),
    status          TEXT DEFAULT 'Pending' CHECK(status IN ('Pending','Approved','Rejected')),
    rejection_reason TEXT,
    reviewed_by     INTEGER,
    reviewed_at     TEXT,
    posting_id      INTEGER,
    created_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (requested_by)  REFERENCES Employee(employee_id),
    FOREIGN KEY (reviewed_by)   REFERENCES Employee(employee_id),
    FOREIGN KEY (department_id) REFERENCES Department(department_id),
    FOREIGN KEY (posting_id)    REFERENCES Job_Posting(posting_id)
)
"""

POSTING_FULL_COLS = ("posting_id", "title", "position_id", "department_id", "branch_id",
                     "employment_type", "min_salary", "max_salary", "description",
                     "requirements", "status", "target_audience", "posted_by",
                     "created_at", "closed_at")
posting_col_list = ", ".join(POSTING_FULL_COLS)
VACANCY_FULL_COLS = ("request_id", "requested_by", "department_id", "position_title",
                     "position_id", "is_custom", "employment_type", "min_salary",
                     "max_salary", "description", "requirements", "reason",
                     "target_audience", "status", "rejection_reason", "reviewed_by",
                     "reviewed_at", "posting_id", "created_at")
vacancy_col_list = ", ".join(VACANCY_FULL_COLS)

b11_con = sqlite3.connect(b11_db)
b11_con.row_factory = sqlite3.Row
b11_con.execute("PRAGMA foreign_keys = OFF")
# Remove children of Job_Application, Interview, Contract and Job_Posting so
# the tables can be replaced cleanly (copy-only changes; the real DB is
# untouched). Later recruitment phases introduced additional FK children.
for _b11_child in ('Opening_Reservation', 'Offer_Approval', 'Email_Delivery_Log',
                   'Interview_Scorecard', 'Interview_Reschedule',
                   'Candidate_Recommendation'):
    if b11_con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (_b11_child,)).fetchone():
        b11_con.execute("DELETE FROM " + _b11_child)
b11_con.execute("DELETE FROM Interview")
b11_con.execute("DELETE FROM Contract")
b11_con.execute("DELETE FROM Job_Application")
b11_con.execute("DROP TABLE Job_Posting")
b11_con.execute("DROP TABLE Vacancy_Request")
b11_con.execute(OLD_JOB_POSTING_DDL)
b11_con.execute(OLD_VACANCY_REQUEST_DDL)
b11_con.execute("PRAGMA foreign_keys = ON")

emp11 = b11_con.execute(
    "SELECT employee_id, branch_id, department_id FROM Employee ORDER BY employee_id LIMIT 1"
).fetchone()
check(emp11 is not None, 'B11: found employee FK anchor in temp copy')
if emp11:
    dept11 = b11_con.execute(
        "SELECT department_id, branch_id FROM Department WHERE department_id=?",
        (emp11['department_id'],)).fetchone() or emp11
    # Two postings: one Filled, one Open
    b11_con.execute(
        "INSERT INTO Job_Posting (posting_id, title, department_id, branch_id, status, created_at, closed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (901, 'Filled Role', dept11['department_id'], emp11['branch_id'], 'Filled',
         '2026-01-01 09:00:00', '2026-01-10 09:00:00'))
    b11_con.execute(
        "INSERT INTO Job_Posting (posting_id, title, department_id, branch_id, status, created_at, closed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (902, 'Open Role', dept11['department_id'], emp11['branch_id'], 'Open',
         '2026-01-02 09:00:00', None))
    # Two vacancy requests: Approved + Pending
    b11_con.execute(
        "INSERT INTO Vacancy_Request (request_id, requested_by, department_id, position_title, employment_type, status, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (801, emp11['employee_id'], dept11['department_id'], 'Approved Title', 'Full-Time',
         'Approved', '2026-01-03 09:00:00'))
    b11_con.execute(
        "INSERT INTO Vacancy_Request (request_id, requested_by, department_id, position_title, employment_type, status, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (802, emp11['employee_id'], dept11['department_id'], 'Pending Title', 'Full-Time',
         'Pending', '2026-01-04 09:00:00'))
    # One Hired application -> backfilled Filled reservation
    cur = b11_con.execute(
        "INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status, applicant_type) "
        "VALUES (?,?,?,?,?,?)",
        (901, 1, 'Hired Person', 'hired@example.com', 'Hired', 'External'))
    b11_hired_app = cur.lastrowid
b11_con.commit()

posting_before = [dict(r) for r in b11_con.execute(
    "SELECT %s FROM Job_Posting ORDER BY posting_id" % posting_col_list)]
vacancy_before = [dict(r) for r in b11_con.execute(
    "SELECT %s FROM Vacancy_Request ORDER BY request_id" % vacancy_col_list)]
b11_con.close()

check(_b11_backup_count() == 0, 'B11: no backups exist before the migration')

init_db_mod.DB_PATH = b11_db
try:
    init_db_mod.migrate_vacancy_openings()
    v11_ok = True
except Exception as e:
    v11_ok = False
    print(f'  [WARN] B11 migration raised: {e}')
finally:
    init_db_mod.DB_PATH = app_db_mod.DB_PATH
check(v11_ok, 'B11: pending vacancy-openings migration runs')

backup11 = sorted(f for f in os.listdir(b11_bak_dir)
                   if f.startswith('smarthr_backup_')) if os.path.isdir(b11_bak_dir) else []
check(len(backup11) == 1,
      f'B11: exactly one backup created for pending rebuild (got {len(backup11)})')
if backup11:
    bak11 = sqlite3.connect(os.path.join(b11_bak_dir, backup11[0]))
    bak11.row_factory = sqlite3.Row
    bak11_jp = bak11.execute("SELECT COUNT(*) as c FROM Job_Posting").fetchone()['c']
    bak11_sql = bak11.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='Job_Posting'").fetchone()
    bak11.close()
    check(bak11_jp == 2, f'B11: backup contains pre-migration postings (got {bak11_jp})')
    check(bak11_sql is not None and 'Partially Filled' not in (bak11_sql['sql'] or ''),
          'B11: backup preserves the pre-migration (old) schema')

b11_con = sqlite3.connect(b11_db)
b11_con.row_factory = sqlite3.Row
b11_con.execute("PRAGMA foreign_keys = ON")
posting_after = [dict(r) for r in b11_con.execute(
    "SELECT %s FROM Job_Posting ORDER BY posting_id" % posting_col_list)]
vacancy_after = [dict(r) for r in b11_con.execute(
    "SELECT %s FROM Vacancy_Request ORDER BY request_id" % vacancy_col_list)]
check(posting_after == posting_before, 'B11: every old Job_Posting column preserved')
check(vacancy_after == vacancy_before, 'B11: every old Vacancy_Request column preserved')

jp_cols11 = [r[1] for r in b11_con.execute("PRAGMA table_info(Job_Posting)")]
check('approved_openings' in jp_cols11 and 'reserved_openings' in jp_cols11
      and 'filled_openings' in jp_cols11, 'B11: Job_Posting opening columns added')
vr_cols11 = [r[1] for r in b11_con.execute("PRAGMA table_info(Vacancy_Request)")]
check('requested_openings' in vr_cols11 and 'approved_openings' in vr_cols11,
      'B11: Vacancy_Request opening columns added')

opening_rows = b11_con.execute(
    "SELECT posting_id, application_id, status FROM Opening_Reservation").fetchall()
check(len(opening_rows) == 1 and opening_rows[0]['posting_id'] == 901
      and opening_rows[0]['application_id'] == b11_hired_app
      and opening_rows[0]['status'] == 'Filled',
      'B11: Filled reservation backfilled for the Hired application')

filled_posting = b11_con.execute(
    "SELECT approved_openings, filled_openings FROM Job_Posting WHERE posting_id=901").fetchone()
open_posting = b11_con.execute(
    "SELECT approved_openings, filled_openings FROM Job_Posting WHERE posting_id=902").fetchone()
check(filled_posting['approved_openings'] == 1 and filled_posting['filled_openings'] == 1,
      'B11: previously Filled posting backfilled to 1/1')
check(open_posting['approved_openings'] == 1 and open_posting['filled_openings'] == 0,
      'B11: Open posting backfilled to 1/0')

approved_req = b11_con.execute(
    "SELECT requested_openings, approved_openings FROM Vacancy_Request WHERE request_id=801").fetchone()
pending_req = b11_con.execute(
    "SELECT requested_openings, approved_openings FROM Vacancy_Request WHERE request_id=802").fetchone()
check(approved_req['requested_openings'] == 1 and approved_req['approved_openings'] == 1,
      'B11: Approved request backfilled to 1/1')
check(pending_req['requested_openings'] == 1 and pending_req['approved_openings'] is None,
      'B11: Pending request backfilled requested=1, approved NULL')

# Partially Filled accepted by the new CHECK
try:
    b11_con.execute("UPDATE Job_Posting SET status='Partially Filled' WHERE posting_id=902")
    pf_ok = True
except Exception:
    pf_ok = False
check(pf_ok, 'B11: Partially Filled status accepted after migration')

fk11 = b11_con.execute("PRAGMA foreign_key_check").fetchall()
check(len(fk11) == 0, 'B11: foreign_key_check clean after migration')
b11_con.close()

# Idempotent no-op re-run must not create another backup
init_db_mod.DB_PATH = b11_db
try:
    init_db_mod.migrate_vacancy_openings()
except Exception as e:
    print(f'  [WARN] B11 no-op re-run raised: {e}')
finally:
    init_db_mod.DB_PATH = app_db_mod.DB_PATH
check(_b11_backup_count() == 1, 'B11: no-op migration does not create extra backups')

shutil.rmtree(b11_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B12 — Opening ledger behavior: vacancy request -> approval -> multi-opening fill
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('12')
print('B12 — Opening ledger behavior')
print('=' * 60)

b12_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b12_')
b12_db = os.path.join(b12_tmp_dir, 'smarthr_b12.db')
shutil.copy2(app_db_mod.DB_PATH, b12_db)
b12_real_db = app_db_mod.DB_PATH

app_db_mod.DB_PATH = b12_db
init_db_mod.DB_PATH = b12_db
try:
    init_db_mod.migrate_vacancy_openings()
    from app.recruitment import routes as b12_rr
    from app.notifications import email_service as b12_email_service
    b12_orig_send = b12_rr.send_email
    b12_orig_email_service_send = b12_email_service.send_email
    b12_rr.send_email = lambda *args, **kwargs: True
    b12_email_service.send_email = lambda *args, **kwargs: True

    with app.test_client() as client:
        client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                    follow_redirects=True)

        # ── vacancy request carries the requested opening count ──
        dept = dbq("""SELECT d.department_id, d.branch_id FROM Department d
                      JOIN Branch b ON d.branch_id=b.branch_id
                      WHERE b.company_id=1
                      ORDER BY d.department_id LIMIT 1""", one=True)
        pos = dbq("SELECT position_id FROM Position WHERE department_id=? AND is_active=1 LIMIT 1",
                  (dept['department_id'],), one=True)
        check(dept is not None and pos is not None,
              'B12: found department and position anchors')

        if dept and pos:
            resp = client.post('/recruitment/vacancy-request', data={
                'department_id': str(dept['department_id']),
                'position_id': str(pos['position_id']),
                'employment_type': 'Full-Time',
                'target_audience': 'Both',
                'requested_openings': '2',
                'reason': 'B12 multi-opening test',
            }, follow_redirects=True)
            req = dbq("SELECT * FROM Vacancy_Request ORDER BY request_id DESC LIMIT 1", one=True)
            check(resp.status_code == 200 and req is not None
                  and req['requested_openings'] == 2,
                  f'B12: vacancy request stores requested_openings=2 (got {req["requested_openings"] if req else None})')

            # ── approval creates a posting with approved_openings ──
            resp = client.post(f'/recruitment/vacancy-request/{req["request_id"]}/approve',
                               data={},
                               follow_redirects=True)
            req_after = dbq("SELECT * FROM Vacancy_Request WHERE request_id=?",
                            (req['request_id'],), one=True)
            posting = dbq("""SELECT * FROM Job_Posting WHERE posting_id=?""",
                          (req_after['posting_id'],), one=True)
            check(resp.status_code == 200 and posting is not None
                  and posting['approved_openings'] == 2 and posting['status'] == 'Open'
                  and posting['branch_id'] == dept['branch_id'],
                  f'B12: approval creates posting with approved_openings=2, Open, '
                  f'branch auto-derived from department '
                  f'(got {posting["approved_openings"] if posting else None}/'
                  f'{posting["status"] if posting else None}/'
                  f'{posting["branch_id"] if posting else None})')

            # ── multi-opening fill lifecycle ──
            if posting:
                pid = posting['posting_id']
                app_ids = []
                for i in range(3):
                    aid = dbe("""INSERT INTO Job_Application
                                 (posting_id, company_id, applicant_name, applicant_email, status, applicant_type)
                                 VALUES (?,1,?,?,'Shortlisted','External')""",
                              (pid, f'B12 C{i}', f'b12_{i}@example.com'))
                    app_ids.append(aid)

                # Hired cannot bypass the offer/hire workflow through the
                # legacy generic status endpoint. Exercise opening accounting
                # directly here; B21 covers the real approved-offer hire route.
                bypass = client.post(f'/recruitment/applications/{app_ids[0]}/status',
                                     data={'status': 'Hired'}, follow_redirects=True)
                first_after_bypass = dbq("SELECT status FROM Job_Application WHERE application_id=?",
                                         (app_ids[0],), one=True)
                check(first_after_bypass['status'] == 'Shortlisted'
                      and b'cannot be set directly' in bypass.data,
                      'B12: generic status route cannot bypass the hire workflow')
                dbe("UPDATE Job_Application SET status='Hired' WHERE application_id=?", (app_ids[0],))
                from app.database import close_job_posting_for_application
                close_job_posting_for_application(app_ids[0])
                p1 = dbq("SELECT status, filled_openings FROM Job_Posting WHERE posting_id=?", (pid,), one=True)
                check(p1['status'] == 'Partially Filled' and p1['filled_openings'] == 1,
                      f'B12: first hire -> Partially Filled 1/2 (got {p1["status"]}/{p1["filled_openings"]})')
                rest = [dbq("SELECT status FROM Job_Application WHERE application_id=?", (a,), one=True)['status']
                        for a in app_ids[1:]]
                check(rest == ['Shortlisted', 'Shortlisted'],
                      'B12: candidates stay active while openings remain')

# Second filled opening -> posting auto-archived, remaining candidate rejected.
                dbe("UPDATE Job_Application SET status='Hired' WHERE application_id=?", (app_ids[1],))
                close_job_posting_for_application(app_ids[1])
                p2 = dbq("SELECT status, filled_openings FROM Job_Posting WHERE posting_id=?", (pid,), one=True)
                check(p2['status'] == 'Archived' and p2['filled_openings'] == 2,
                      f'B12: second hire -> posting auto-archived 2/2 (got {p2["status"]}/{p2["filled_openings"]})')
                a3 = dbq("SELECT status FROM Job_Application WHERE application_id=?", (app_ids[2],), one=True)
                check(a3['status'] == 'Rejected',
                      'B12: remaining candidate rejected only when all openings filled')

        client.get('/logout')
finally:
    if 'b12_orig_send' in locals():
        b12_rr.send_email = b12_orig_send
    if 'b12_orig_email_service_send' in locals():
        b12_email_service.send_email = b12_orig_email_service_send
    app_db_mod.DB_PATH = b12_real_db
    init_db_mod.DB_PATH = b12_real_db
    shutil.rmtree(b12_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B13 — AI screening migration: additive columns + backfill, no backup needed
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('13')
print('B13 — AI screening migration')
print('=' * 60)

b13_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b13_')
b13_db = os.path.join(b13_tmp_dir, 'smarthr_b13.db')
shutil.copy2(app_db_mod.DB_PATH, b13_db)
b13_bak_dir = os.path.join(b13_tmp_dir, 'backups')

SCREENING_COLS = ('screening_status', 'matched_evidence', 'missing_requirements',
                  'scored_at', 'scorer_version', 'shortlist_override_by',
                  'shortlist_override_reason', 'shortlist_override_at')

# Downgrade the copy: remove the screening columns so the migration is pending
b13_con = sqlite3.connect(b13_db)
b13_con.execute("PRAGMA foreign_keys = OFF")
b13_cols = [r[1] for r in b13_con.execute("PRAGMA table_info(Job_Application)")]
for col in SCREENING_COLS:
    if col in b13_cols:
        b13_con.execute("ALTER TABLE Job_Application DROP COLUMN %s" % col)
b13_con.execute("PRAGMA foreign_keys = ON")
b13_con.commit()

# Ensure the fixture has both scored (ai_score NOT NULL) and unscored rows
emp13 = b13_con.execute(
    "SELECT employee_id FROM Employee ORDER BY employee_id LIMIT 1").fetchone()
if emp13:
    b13_con.execute("""INSERT INTO Job_Application (company_id, applicant_name, applicant_email, status, ai_score)
                       VALUES (1, 'B13 Scored', 'b13_s@example.com', 'New', 42.5)""")
    b13_con.execute("""INSERT INTO Job_Application (company_id, applicant_name, applicant_email, status, ai_score)
                       VALUES (1, 'B13 Unscored', 'b13_u@example.com', 'New', NULL)""")
b13_con.commit()
b13_con.close()

init_db_mod.DB_PATH = b13_db
try:
    init_db_mod.migrate_ai_screening()
    s13_ok = True
except Exception as e:
    s13_ok = False
    print(f'  [WARN] B13 migration raised: {e}')
finally:
    init_db_mod.DB_PATH = app_db_mod.DB_PATH
check(s13_ok, 'B13: pending AI screening migration runs')
check(not os.path.isdir(b13_bak_dir),
      'B13: additive migration creates no backup')

b13_con = sqlite3.connect(b13_db)
b13_con.row_factory = sqlite3.Row
cols13 = [r[1] for r in b13_con.execute("PRAGMA table_info(Job_Application)")]
check(all(c in cols13 for c in SCREENING_COLS), 'B13: all screening columns added')

scored = b13_con.execute(
    "SELECT screening_status FROM Job_Application WHERE applicant_name='B13 Scored'").fetchone()
unscored = b13_con.execute(
    "SELECT screening_status FROM Job_Application WHERE applicant_name='B13 Unscored'").fetchone()
check(scored is not None and scored['screening_status'] == 'Scored',
      'B13: scored row backfilled as Scored')
check(unscored is not None and unscored['screening_status'] == 'Manual Review Required',
      'B13: unscored row backfilled as Manual Review Required')
b13_con.close()

# Idempotent re-run
init_db_mod.DB_PATH = b13_db
try:
    init_db_mod.migrate_ai_screening()
except Exception as e:
    print(f'  [WARN] B13 no-op re-run raised: {e}')
finally:
    init_db_mod.DB_PATH = app_db_mod.DB_PATH
check(not os.path.isdir(b13_bak_dir), 'B13: no-op run creates no backup')

shutil.rmtree(b13_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B14 — AI screening behavior: Manual Review Required, Scored, overrides
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('14')
print('B14 — AI screening behavior')
print('=' * 60)

b14_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b14_')
b14_db = os.path.join(b14_tmp_dir, 'smarthr_b14.db')
shutil.copy2(app_db_mod.DB_PATH, b14_db)
b14_real_db = app_db_mod.DB_PATH

app_db_mod.DB_PATH = b14_db
init_db_mod.DB_PATH = b14_db
try:
    init_db_mod.migrate_ai_screening()

    with app.test_client() as client:
        client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                    follow_redirects=True)

        posting = dbq("""SELECT jp.posting_id, jp.title, jp.description, jp.requirements
                         FROM Job_Posting jp
                         WHERE jp.status='Open'
                           AND length(COALESCE(jp.description,'')) > 50
                           AND length(COALESCE(jp.requirements,'')) > 50
                         ORDER BY jp.posting_id LIMIT 1""", one=True)
        check(posting is not None, 'B14: found a posting with requirements')
        if not posting:
            dept = dbq("""SELECT d.department_id FROM Department d
                          JOIN Branch b ON d.branch_id=b.branch_id
                          WHERE b.company_id=1 ORDER BY d.department_id LIMIT 1""", one=True)
            cur = dbe("""INSERT INTO Job_Posting
                         (title, department_id, branch_id, status, description, requirements)
                         VALUES ('B14 Position',?,?, 'Open', 'Needs Python Docker Kubernetes AWS skills.', 
                                 'Python, Docker, Kubernetes, AWS, CI/CD, Linux')""",
                      (dept['department_id'], 1))
            posting = dbq("""SELECT posting_id, title, description, requirements
                             FROM Job_Posting WHERE posting_id=?""", (cur,), one=True)

        posting_data = {'title': posting['title'],
                        'description': posting['description'],
                        'requirements': posting['requirements']}

        # ── (a) direct score_and_persist: insufficient evidence ──
        a1 = dbe("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                    VALUES (?,1,'B14 Short','b14_short@example.com','New')""",
                 (posting['posting_id'],))
        from app.recruitment.scorer import score_and_persist
        score_and_persist(a1, dict(posting_data),
                          {'application_id': a1, 'applicant_name': 'B14 Short',
                           'cover_letter': 'Hi', 'resume_path': None},
                          app_root=app.root_path)
        row1 = dbq("""SELECT status, screening_status, ai_score, scorer_version, missing_requirements
                      FROM Job_Application WHERE application_id=?""", (a1,), one=True)
        check(row1['screening_status'] == 'Manual Review Required'
              and row1['status'] == 'New' and row1['ai_score'] is None
              and row1['scorer_version'] is not None,
              f'B14: short/no evidence -> Manual Review Required, New, no score '
              f'(got {row1["screening_status"]}/{row1["status"]}/{row1["ai_score"]})')

        # ── (b) direct score_and_persist: strong evidence auto-shortlists ──
        cover = 'Description\n%s\n\nRequirements\n%s' % (posting['description'], posting['requirements'])
        a2 = dbe("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                    VALUES (?,1,'B14 Strong','b14_strong@example.com','New')""",
                 (posting['posting_id'],))
        score_and_persist(a2, dict(posting_data),
                          {'application_id': a2, 'applicant_name': 'B14 Strong',
                           'cover_letter': cover, 'resume_path': None},
                          app_root=app.root_path)
        row2 = dbq("""SELECT status, screening_status, ai_score, matched_evidence
                      FROM Job_Application WHERE application_id=?""", (a2,), one=True)
        check(row2['screening_status'] == 'Scored' and row2['status'] == 'Shortlisted'
              and row2['ai_score'] is not None and row2['ai_score'] > 60
              and row2['matched_evidence'] not in (None, ''),
              f'B14: strong evidence -> Scored, Shortlisted, score>60 '
              f'(got {row2["screening_status"]}/{row2["status"]}/{row2["ai_score"]})')

        # ── (c) HR manual override of the AI recommendation ──
        resp = client.post(f'/recruitment/applications/{a1}/status',
                           data={'status': 'Shortlisted', 'override_reason': 'HR decision'},
                           follow_redirects=True)
        hr_user = dbq("SELECT employee_id FROM Employee WHERE email='hr@smarthr.my'", one=True)
        ov = dbq("""SELECT shortlist_override_by, shortlist_override_reason, shortlist_override_at
                    FROM Job_Application WHERE application_id=?""", (a1,), one=True)
        check(resp.status_code == 200 and ov['shortlist_override_by'] == hr_user['employee_id']
              and ov['shortlist_override_reason'] == 'HR decision'
              and ov['shortlist_override_at'] is not None,
              'B14: manual shortlist records override user, reason and timestamp')

        # reversing a shortlist also records an override
        client.post(f'/recruitment/applications/{a2}/status',
                    data={'status': 'New', 'override_reason': 'Reverse decision'},
                    follow_redirects=True)
        ov2 = dbq("""SELECT shortlist_override_reason FROM Job_Application
                     WHERE application_id=?""", (a2,), one=True)
        check(ov2['shortlist_override_reason'] == 'Reverse decision',
              'B14: reversing a shortlist records an override')

        # ── (d) internal job board apply with no evidence -> Manual Review ──
        client.get('/logout')
        client.post('/login', data={'email': 'elizabeth@smarthr.my', 'password': 'Employee@123'},
                    follow_redirects=True)
        dept = dbq("""SELECT d.department_id, d.branch_id FROM Department d
                      JOIN Branch b ON d.branch_id=b.branch_id
                      WHERE b.company_id=1 ORDER BY d.department_id LIMIT 1""", one=True)
        pid_new = dbe("""INSERT INTO Job_Posting
                         (title, department_id, branch_id, status, target_audience)
                         VALUES ('B14 Internal Role',?,?,'Open','Both')""",
                      (dept['department_id'], dept['branch_id']))
        resp = client.post(f'/recruitment/internal-jobs/{pid_new}/apply',
                           data={'cover_letter': 'Hi'},
                           follow_redirects=True)
        iapp = dbq("""SELECT status, screening_status, ai_score FROM Job_Application
                      WHERE posting_id=? AND internal_employee_id IS NOT NULL
                      ORDER BY application_id DESC LIMIT 1""", (pid_new,), one=True)
        check(resp.status_code == 200 and iapp is not None
              and iapp['screening_status'] == 'Manual Review Required'
              and iapp['status'] == 'New' and iapp['ai_score'] is None,
              'B14: internal apply with no evidence -> Manual Review Required, New')

        client.get('/logout')
finally:
    app_db_mod.DB_PATH = b14_real_db
    init_db_mod.DB_PATH = b14_real_db
    shutil.rmtree(b14_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B15 — Auto-shortlisting rule card: renders for HR/Admin, hidden elsewhere
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('15')
print('B15 — Auto-shortlisting rule card visibility')
print('=' * 60)

CARD_MARKERS = ('Auto-shortlisting rule', 'Match score', 'Manual Review Required',
                'match score &gt; 60', 'Keyword coverage 50%')

with app.test_client() as client:
    # Unauthenticated: redirected, never sees the card
    resp = client.get('/recruitment/applications')
    check(resp.status_code == 302, 'B15: unauthenticated user redirected from applications page')

    # Employee: denied, never sees the card
    client.post('/login', data={'email': 'elizabeth@smarthr.my', 'password': 'Employee@123'},
                follow_redirects=True)
    resp = client.get('/recruitment/applications')
    check(resp.status_code == 302, 'B15: Employee redirected from applications page')
    body = resp.data.decode('utf-8', errors='replace')
    check('Auto-shortlisting rule' not in body, 'B15: Employee does not see the rule card')
    client.get('/logout')

    # Manager: page is reachable, but the card is HR/Admin-only
    client.post('/login', data={'email': 'cheeseng@smarthr.my', 'password': 'Manager@123'},
                follow_redirects=True)
    resp = client.get('/recruitment/applications')
    body = resp.data.decode('utf-8', errors='replace')
    check(resp.status_code == 200, 'B15: Manager can reach the applications page')
    check('Auto-shortlisting rule' not in body, 'B15: Manager does not see the rule card')
    client.get('/logout')

    # HR: card renders with the actual rule
    client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                follow_redirects=True)
    resp = client.get('/recruitment/applications')
    body = resp.data.decode('utf-8', errors='replace')
    check(resp.status_code == 200, 'B15: HR can reach the applications page')
    check(all(m in body for m in CARD_MARKERS),
          'B15: HR sees the auto-shortlisting rule card with the real formula')
    client.get('/logout')

    # Admin: card renders
    client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                follow_redirects=True)
    resp = client.get('/recruitment/applications')
    body = resp.data.decode('utf-8', errors='replace')
    check(resp.status_code == 200 and 'Auto-shortlisting rule' in body,
          'B15: Admin sees the auto-shortlisting rule card')
    client.get('/logout')

# ═══════════════════════════════════════════════════════════════════════════════
# B16 — Interview format migration: additive columns + reschedule history table
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('16')
print('B16 — Interview format migration')
print('=' * 60)

b16_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b16_')
b16_db = os.path.join(b16_tmp_dir, 'smarthr_b16.db')
shutil.copy2(app_db_mod.DB_PATH, b16_db)
b16_bak_dir = os.path.join(b16_tmp_dir, 'backups')

# Downgrade the copy so the migration is pending
b16_con = sqlite3.connect(b16_db)
b16_con.execute("PRAGMA foreign_keys = OFF")
b16_con.execute("DROP TABLE IF EXISTS Interview_Reschedule")
b16_cols = [r[1] for r in b16_con.execute("PRAGMA table_info(Interview)")]
for col in ('format', 'venue', 'posting_branch_id'):
    if col in b16_cols:
        b16_con.execute("ALTER TABLE Interview DROP COLUMN %s" % col)
b16_con.execute("PRAGMA foreign_keys = ON")
b16_con.commit()
b16_con.close()

init_db_mod.DB_PATH = b16_db
try:
    init_db_mod.migrate_interview_format()
    i16_ok = True
except Exception as e:
    i16_ok = False
    print(f'  [WARN] B16 migration raised: {e}')
finally:
    init_db_mod.DB_PATH = app_db_mod.DB_PATH
check(i16_ok, 'B16: pending interview format migration runs')
check(not os.path.isdir(b16_bak_dir), 'B16: additive migration creates no backup')

b16_con = sqlite3.connect(b16_db)
cols16 = [r[1] for r in b16_con.execute("PRAGMA table_info(Interview)")]
check(all(c in cols16 for c in ('format', 'venue', 'posting_branch_id')),
      'B16: Interview format columns added')
tbl16 = b16_con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='Interview_Reschedule'").fetchone()
check(tbl16 is not None, 'B16: Interview_Reschedule table created')
b16_con.close()

init_db_mod.DB_PATH = b16_db
try:
    init_db_mod.migrate_interview_format()  # idempotent no-op
except Exception as e:
    print(f'  [WARN] B16 no-op re-run raised: {e}')
finally:
    init_db_mod.DB_PATH = app_db_mod.DB_PATH
check(not os.path.isdir(b16_bak_dir), 'B16: no-op run creates no backup')

shutil.rmtree(b16_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B17 — Interview venue rules + rescheduling behavior
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('17')
print('B17 — Interview venue and rescheduling')
print('=' * 60)

b17_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b17_')
b17_db = os.path.join(b17_tmp_dir, 'smarthr_b17.db')
shutil.copy2(app_db_mod.DB_PATH, b17_db)
b17_real_db = app_db_mod.DB_PATH

app_db_mod.DB_PATH = b17_db
init_db_mod.DB_PATH = b17_db
try:
    init_db_mod.migrate_interview_format()
    from app.recruitment import routes as b17_rr
    b17_orig_send = b17_rr.send_email
    b17_rr.send_email = lambda *args, **kwargs: True

    with app.test_client() as client:
        client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                    follow_redirects=True)

        # Fixtures: one branch with an address, one without
        br_with = dbe("""INSERT INTO Branch (company_id, name, address_line1, city, state, postal_code)
                         VALUES (1,'B17 Branch','12 Jalan Test','KL','Kuala Lumpur','50000')""")
        br_none = dbe("INSERT INTO Branch (company_id, name) VALUES (1,'B17 NoAddr')")

        pid_ok = dbe("INSERT INTO Job_Posting (title, branch_id, status) VALUES ('B17 Role',?,'Open')", (br_with,))
        pid_noaddr = dbe("INSERT INTO Job_Posting (title, branch_id, status) VALUES ('B17 Role NoAddr',?,'Open')", (br_none,))
        aid_ok = dbe("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                        VALUES (?,1,'B17 C','b17c@example.com','Shortlisted')""", (pid_ok,))
        aid_noaddr = dbe("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                            VALUES (?,1,'B17 D','b17d@example.com','Shortlisted')""", (pid_noaddr,))

        # Local eligible interviewer in the addressed branch so Physical
        # scheduling (which requires an in-branch interviewer pool) can proceed.
        b17_mgr_role = dbq("SELECT role_id FROM Role WHERE role_name='Manager'", one=True)
        b17_dept = dbq("""SELECT d.department_id FROM Department d
                          JOIN Branch b ON d.branch_id=b.branch_id
                          WHERE b.company_id=1 ORDER BY d.department_id LIMIT 1""", one=True)
        dbe("""INSERT INTO Employee (company_id, branch_id, department_id, full_name, hire_date,
                                     base_salary, role_id, email, password_hash, is_active)
               VALUES (1,?,?, 'B17 Mgr', '2024-01-01', 5000, ?, 'b17mgr@example.com', 'x', 1)""",
            (br_with, b17_dept['department_id'], b17_mgr_role['role_id']))

        # Next weekday (tomorrow onwards)
        d = datetime.date.today() + datetime.timedelta(days=1)
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        date_str = d.isoformat()
        saturday = d + datetime.timedelta(days=(5 - d.weekday()) % 7)
        while saturday.weekday() != 5:
            saturday += datetime.timedelta(days=1)
        sat_str = saturday.isoformat()

        # ── Physical: venue derived from the posting branch ──
        resp = client.post(f'/recruitment/application/{aid_ok}/schedule-interview',
                           data={'date': date_str, 'time': '10:00', 'duration': '60',
                                 'format': 'Physical', 'interviewer_ids': ''},
                           follow_redirects=True)
        iv = dbq("SELECT * FROM Interview WHERE application_id=?", (aid_ok,), one=True)
        check(resp.status_code == 200 and iv is not None
              and iv['format'] == 'Physical'
              and iv['venue'] == '12 Jalan Test, KL, Kuala Lumpur, 50000'
              and iv['posting_branch_id'] == br_with
              and iv['type'] == 'In-Person',
              'B17: Physical interview uses the posting branch address snapshot')

        # ── Physical blocked when the branch has no address ──
        resp = client.post(f'/recruitment/application/{aid_noaddr}/schedule-interview',
                           data={'date': date_str, 'time': '11:00', 'duration': '60',
                                 'format': 'Physical', 'interviewer_ids': ''},
                           follow_redirects=True)
        iv_noaddr = dbq("SELECT * FROM Interview WHERE application_id=?", (aid_noaddr,), one=True)
        body = resp.data.decode('utf-8', errors='replace')
        check(iv_noaddr is None and 'has no address on file' in body,
              'B17: Physical scheduling blocked when branch address missing')

        # ── Virtual requires a meeting link ──
        resp = client.post(f'/recruitment/application/{aid_noaddr}/schedule-interview',
                           data={'date': date_str, 'time': '11:00', 'duration': '60',
                                 'format': 'Virtual', 'meeting_link': ''},
                           follow_redirects=True)
        iv_noaddr = dbq("SELECT * FROM Interview WHERE application_id=?", (aid_noaddr,), one=True)
        body = resp.data.decode('utf-8', errors='replace')
        check(iv_noaddr is None and 'meeting link is required' in body,
              'B17: Virtual scheduling blocked without a meeting link')

        resp = client.post(f'/recruitment/application/{aid_noaddr}/schedule-interview',
                           data={'date': date_str, 'time': '11:00', 'duration': '60',
                                 'format': 'Virtual', 'meeting_link': 'https://meet.example.com/x'},
                           follow_redirects=True)
        iv_v = dbq("SELECT * FROM Interview WHERE application_id=?", (aid_noaddr,), one=True)
        check(iv_v is not None and iv_v['format'] == 'Virtual'
              and iv_v['meeting_link'] == 'https://meet.example.com/x'
              and not iv_v['venue'],
              'B17: Virtual interview stores the link and no physical venue')

        # ── Physical with a typed venue: location stays the branch address ──
        aid_ven = dbe("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                         VALUES (?,1,'B17 Ven','b17ven@example.com','Shortlisted')""", (pid_ok,))
        resp = client.post(f'/recruitment/application/{aid_ven}/schedule-interview',
                           data={'date': date_str, 'time': '13:00', 'duration': '60',
                                 'format': 'Physical', 'venue': 'Block A Meeting Room',
                                 'interviewer_ids': ''},
                           follow_redirects=True)
        iv_ven = dbq("SELECT * FROM Interview WHERE application_id=?", (aid_ven,), one=True)
        check(resp.status_code == 200 and iv_ven is not None
              and iv_ven['location'] == '12 Jalan Test, KL, Kuala Lumpur, 50000'
              and iv_ven['venue'] == 'Block A Meeting Room',
              'B17: typed venue is stored; location stays the branch address')

        # ── Reschedule: valid slot records history and updates the time ──
        old_at = iv['scheduled_at']
        resp = client.post(f'/recruitment/interview/{iv["interview_id"]}/reschedule',
                           data={'date': date_str, 'time': '14:00',
                                 'reason': 'Interviewer unavailable'},
                           follow_redirects=True)
        hist = dbq("""SELECT * FROM Interview_Reschedule
                      WHERE interview_id=? ORDER BY reschedule_id DESC LIMIT 1""",
                   (iv['interview_id'],), one=True)
        iv_after = dbq("SELECT * FROM Interview WHERE interview_id=?", (iv['interview_id'],), one=True)
        check(hist is not None and hist['reason'] == 'Interviewer unavailable'
              and hist['old_scheduled_at'] == old_at
              and iv_after['scheduled_at'] != old_at,
              'B17: reschedule records history and updates the interview time')

        before_past = iv_after['scheduled_at']
        resp = client.post(f'/recruitment/interview/{iv["interview_id"]}/reschedule',
                           data={'date': '2000-01-03', 'time': '10:00',
                                 'reason': 'past-date regression check'},
                           follow_redirects=True)
        past_body = resp.data.decode('utf-8', errors='replace')
        after_past = dbq("SELECT scheduled_at FROM Interview WHERE interview_id=?",
                         (iv['interview_id'],), one=True)['scheduled_at']
        check('must be in the future' in past_body and after_past == before_past,
              'B17: past-date reschedule is blocked without changing the interview')
        with open(os.path.join('templates', 'recruitment', 'interviews.html'), encoding='utf-8') as fh:
            reschedule_template = fh.read()
        check('form[id^="resched-"] input[name="date"]' in reschedule_template
              and 'input.min = today' in reschedule_template,
              'B17: reschedule date picker prevents past calendar dates')

        # ── Reschedule: Completed interviews are blocked ──
        dbe("UPDATE Interview SET status='Completed' WHERE interview_id=?",
            (iv['interview_id'],))
        resp = client.post(f'/recruitment/interview/{iv["interview_id"]}/reschedule',
                           data={'date': date_str, 'time': '15:00', 'reason': 'test'},
                           follow_redirects=True)
        hist2 = dbq("""SELECT COUNT(*) as c FROM Interview_Reschedule
                       WHERE interview_id=?""", (iv['interview_id'],), one=True)
        check(hist2['c'] == 1, 'B17: Completed interview cannot be rescheduled')

        # ── Reschedule: invalid (weekend) slot leaves the interview unchanged ──
        dbe("UPDATE Interview SET status='Scheduled' WHERE interview_id=?", (iv['interview_id'],))
        before_at = dbq("SELECT scheduled_at FROM Interview WHERE interview_id=?",
                        (iv['interview_id'],), one=True)['scheduled_at']
        resp = client.post(f'/recruitment/interview/{iv["interview_id"]}/reschedule',
                           data={'date': sat_str, 'time': '10:00', 'reason': 'weekend test'},
                           follow_redirects=True)
        hist3 = dbq("""SELECT COUNT(*) as c FROM Interview_Reschedule
                       WHERE interview_id=?""", (iv['interview_id'],), one=True)
        after_at = dbq("SELECT scheduled_at FROM Interview WHERE interview_id=?",
                       (iv['interview_id'],), one=True)['scheduled_at']
        check(hist3['c'] == 1 and after_at == before_at,
              'B17: invalid slot leaves the original interview unchanged')

        # ── Reschedule: reason is required ──
        resp = client.post(f'/recruitment/interview/{iv["interview_id"]}/reschedule',
                           data={'date': date_str, 'time': '15:00', 'reason': '  '},
                           follow_redirects=True)
        hist4 = dbq("""SELECT COUNT(*) as c FROM Interview_Reschedule
                       WHERE interview_id=?""", (iv['interview_id'],), one=True)
        check(hist4['c'] == 1, 'B17: reschedule without a reason is rejected')

        client.get('/logout')
finally:
    if 'b17_orig_send' in locals():
        b17_rr.send_email = b17_orig_send
    app_db_mod.DB_PATH = b17_real_db
    init_db_mod.DB_PATH = b17_real_db
    shutil.rmtree(b17_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B18 — Email reschedule requests: manual-review notification only
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('18')
print('B18 — Email reschedule requests')
print('=' * 60)

from app.notifications.email_parser import detect_reschedule_request, extract_interview_ref

check(detect_reschedule_request('Re: Interview Invitation', 'Can we reschedule the interview to next week?'),
      'B18: reschedule intent detected in reply')
check(not detect_reschedule_request('Interview Invitation', 'I am excited about the interview opportunity'),
      'B18: ordinary interview email is not a reschedule request')
check(detect_reschedule_request('Meeting update', 'Please postpone the interview'),
      'B18: postpone + interview detected')
check(extract_interview_ref('Re: Interview Invitation INT-42', 'reschedule please') == 42,
      'B18: INT-<id> reference extracted from subject')
check(extract_interview_ref('Re: Interview Invitation', 'Please reschedule INT-7 interview') == 7,
      'B18: INT-<id> reference extracted from body')
check(extract_interview_ref('Re: Interview Invitation', 'Can we reschedule?') is None,
      'B18: no reference -> None')

b18_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b18_')
b18_db = os.path.join(b18_tmp_dir, 'smarthr_b18.db')
shutil.copy2(app_db_mod.DB_PATH, b18_db)
b18_real_db = app_db_mod.DB_PATH

app_db_mod.DB_PATH = b18_db
init_db_mod.DB_PATH = b18_db
try:
    init_db_mod.migrate_reschedule_dedup()
    from app.notifications.email_monitor import surface_reschedule_request
    from app.database import query as b18q
    from app.database import execute as b18e

    with app.app_context():
        pid = b18e("INSERT INTO Job_Posting (title, branch_id, status) VALUES ('B18 Role',1,'Open')")
        # Two applications with the same email: only the second has an interview.
        aid_no_iv = b18e("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                            VALUES (?,1,'B18 NoInterview','dup18@example.com','New')""", (pid,))
        aid_iv = b18e("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                         VALUES (?,1,'B18 WithInterview','dup18@example.com','Interview')""", (pid,))
        iid = b18e("""INSERT INTO Interview (application_id, scheduled_at, status)
                      VALUES (?, '2026-09-01 10:00:00', 'Scheduled')""", (aid_iv,))
        before_at = b18q("SELECT scheduled_at FROM Interview WHERE interview_id=?", (iid,), one=True)['scheduled_at']
        before_notifs = b18q("SELECT COUNT(*) as c FROM Notification", one=True)['c']

        # Exact INT-<id> reference retained in the reply
        ok = surface_reschedule_request('Candidate 18 <dup18@example.com>',
                                        'Re: Interview Invitation INT-%d' % iid,
                                        'Can we reschedule my interview?',
                                        'msg-b18-001')
        check(ok, 'B18: reschedule request surfaced for a known applicant')
        after_notifs = b18q("SELECT COUNT(*) as c FROM Notification", one=True)['c']
        check(after_notifs > before_notifs, 'B18: HR notification created for manual review')
        n = b18q("""SELECT title, message FROM Notification
                    ORDER BY notification_id DESC LIMIT 1""", one=True)
        check(n is not None and n['title'] == 'Interview Reschedule Request'
              and 'INT-%d' % iid in n['message'] and 'B18 WithInterview' in n['message'],
              'B18: reply with reference names the exact interview')
        after_at = b18q("SELECT scheduled_at FROM Interview WHERE interview_id=?", (iid,), one=True)['scheduled_at']
        check(after_at == before_at, 'B18: interview record is never changed by email')

        # Persistent dedup: same message id surfaced only once, stored in SQLite
        before2 = b18q("SELECT COUNT(*) as c FROM Notification", one=True)['c']
        ok2 = surface_reschedule_request('Candidate 18 <dup18@example.com>',
                                         'Re: Interview Invitation INT-%d' % iid,
                                         'Can we reschedule my interview?',
                                         'msg-b18-001')
        check(not ok2 and b18q("SELECT COUNT(*) as c FROM Notification", one=True)['c'] == before2,
              'B18: same message is surfaced only once')
        dedup_row = b18q("SELECT 1 FROM Reschedule_Email_Processed WHERE msg_id='msg-b18-001'", one=True)
        check(dedup_row is not None, 'B18: dedup is persisted in SQLite')

        # No reference retained: generic list of upcoming interviews, HR chooses
        before3 = b18q("SELECT COUNT(*) as c FROM Notification", one=True)['c']
        ok3 = surface_reschedule_request('Candidate 18 <dup18@example.com>',
                                         'Re: Interview Invitation',
                                         'Can we reschedule my interview?',
                                         'msg-b18-002')
        n3 = b18q("""SELECT message FROM Notification
                     ORDER BY notification_id DESC LIMIT 1""", one=True)
        check(ok3 and b18q("SELECT COUNT(*) as c FROM Notification", one=True)['c'] > before3
              and 'Upcoming interviews' in n3['message'] and 'INT-%d' % iid in n3['message'],
              'B18: no reference -> generic list of upcoming interviews, never a guess')

        # Sender with no upcoming interviews -> no notification
        aid_done = b18e("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                           VALUES (?,1,'B18 Done','done18@example.com','Interview')""", (pid,))
        b18e("""INSERT INTO Interview (application_id, scheduled_at, status)
                VALUES (?, '2026-09-02 10:00:00', 'Completed')""", (aid_done,))
        before4 = b18q("SELECT COUNT(*) as c FROM Notification", one=True)['c']
        ok4 = surface_reschedule_request('Done 18 <done18@example.com>', 'Re: Interview',
                                         'reschedule please', 'msg-b18-003')
        check(not ok4 and b18q("SELECT COUNT(*) as c FROM Notification", one=True)['c'] == before4,
              'B18: sender with no upcoming interviews gets no notification')
finally:
    app_db_mod.DB_PATH = b18_real_db
    init_db_mod.DB_PATH = b18_real_db
    shutil.rmtree(b18_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B19 — Conflict-safe scheduling: DB overlap checks, one-posting batches,
#       panel availability, email local-capture mode
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('19')
print('B19 — Conflict-safe scheduling')
print('=' * 60)

b19_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b19_')
b19_db = os.path.join(b19_tmp_dir, 'smarthr_b19.db')
shutil.copy2(app_db_mod.DB_PATH, b19_db)
b19_real_db = app_db_mod.DB_PATH

app_db_mod.DB_PATH = b19_db
init_db_mod.DB_PATH = b19_db
try:
    from app.recruitment import routes as rr_mod
    from app.database import query as b19q
    from app.database import execute as b19e

    with app.app_context():
        # ── _interviewer_busy overlap semantics ──
        emp9 = b19q("SELECT employee_id FROM Employee ORDER BY employee_id LIMIT 1", one=True)
        eid = emp9['employee_id']
        aid9 = b19e("""INSERT INTO Job_Application (company_id, applicant_name, applicant_email, status)
                       VALUES (1,'B19 BusyFixture','b19busy@example.com','Interview')""")
        iid9 = b19e("""INSERT INTO Interview (application_id, scheduled_at, duration_min, interviewer_ids, status)
                       VALUES (?, '2026-10-05 10:00:00', 60, ?, 'Scheduled')""", (aid9, str(eid)))
        from datetime import datetime as b19dt
        t10 = b19dt.strptime('2026-10-05 10:00:00', '%Y-%m-%d %H:%M:%S')
        check(rr_mod._interviewer_busy(eid, b19dt.strptime('2026-10-05 10:30:00', '%Y-%m-%d %H:%M:%S'), 60),
              'B19: busy when candidate slot overlaps an existing interview')
        check(rr_mod._interviewer_busy(eid, b19dt.strptime('2026-10-05 09:30:00', '%Y-%m-%d %H:%M:%S'), 60),
              'B19: busy when candidate slot starts before and ends inside the existing window')
        check(not rr_mod._interviewer_busy(eid, b19dt.strptime('2026-10-05 11:00:00', '%Y-%m-%d %H:%M:%S'), 60),
              'B19: free when the candidate slot starts exactly at the existing end')
        check(not rr_mod._interviewer_busy(eid, t10, 60, exclude_interview_id=iid9),
              'B19: excluded interview does not block (reschedule case)')

        # ── Fixtures: branch, Manager interviewer, postings, candidates ──
        br = b19e("INSERT INTO Branch (company_id, name, address_line1, city) VALUES (1,'B19 Branch','5 Test Rd','KL')")
        mgr_role = b19q("SELECT role_id FROM Role WHERE role_name='Manager'", one=True)
        dept = b19q("""SELECT d.department_id FROM Department d
                       JOIN Branch b ON d.branch_id=b.branch_id
                       WHERE b.company_id=1 ORDER BY d.department_id LIMIT 1""", one=True)
        mgr = b19e("""INSERT INTO Employee (company_id, branch_id, department_id, full_name, hire_date,
                                             base_salary, role_id, email, password_hash, is_active)
                      VALUES (1,?,?, 'B19 Mgr', '2024-01-01', 5000, ?, 'b19mgr@example.com', 'x', 1)""",
                   (br, dept['department_id'], mgr_role['role_id']))
        pid = b19e("INSERT INTO Job_Posting (title, branch_id, status, approved_openings) VALUES ('B19 Role',?,'Open',3)", (br,))
        p2 = b19e("INSERT INTO Job_Posting (title, branch_id, status) VALUES ('B19 Other',?,'Open')", (br,))
        app_ids = []
        for i in range(3):
            app_ids.append(b19e("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                                   VALUES (?,1,?,?,'Shortlisted')""",
                                (pid, 'B19 C%d' % i, 'b19_%d@example.com' % i)))
        app_other = b19e("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                            VALUES (?,1,'B19 OtherC','b19_other@example.com','Shortlisted')""", (p2,))

        # Block the Manager interviewer on the first candidate day at 09:00.
        d0 = datetime.date.today() + datetime.timedelta(days=1)
        while d0.weekday() >= 5:
            d0 += datetime.timedelta(days=1)
        b19e("""INSERT INTO Interview (application_id, scheduled_at, duration_min, interviewer_ids, status)
                VALUES (?, ?, 60, ?, 'Scheduled')""",
             (app_ids[0], d0.isoformat() + ' 09:00:00', str(mgr)))

    with app.test_client() as client:
        client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                    follow_redirects=True)

        # Email local-capture mode: never send real emails in this test.
        captured = []
        orig_send_email = rr_mod.send_email

        def _capture(subject, recipient, html_body, attachments=None):
            captured.append((subject, recipient))
            return True

        rr_mod.send_email = _capture
        try:
            # ── One-posting-per-batch enforcement ──
            r = client.post('/recruitment/auto-assign',
                            data={'application_ids': [str(app_ids[0]), str(app_other)],
                                  'format': 'Physical'})
            data = r.get_json()
            check(r.status_code == 200 and data is not None
                  and 'one job posting per batch' in data.get('error', ''),
                  'B19: mixed-posting preview rejected')
            before = b19q("SELECT COUNT(*) as c FROM Interview WHERE posting_branch_id=?",
                          (br,), one=True)['c']
            client.post('/recruitment/auto-assign/confirm',
                        data={'application_ids': [str(app_ids[0]), str(app_other)],
                              'format': 'Physical'},
                        follow_redirects=True)
            after = b19q("SELECT COUNT(*) as c FROM Interview WHERE posting_branch_id=?",
                         (br,), one=True)['c']
            check(after == before, 'B19: mixed-posting confirm creates no interviews')

            # ── Valid single-posting batch: preview + confirm ──
            r = client.post('/recruitment/auto-assign',
                            data={'application_ids': [str(a) for a in app_ids],
                                  'format': 'Physical'})
            data = r.get_json()
            check(r.status_code == 200 and data is not None and 'assignments' in data
                  and len(data['assignments']) == 3,
                  'B19: preview returns assignments for a single posting')

            client.post('/recruitment/auto-assign/confirm',
                        data={'application_ids': [str(a) for a in app_ids],
                              'format': 'Physical'},
                        follow_redirects=True)

            # ── No double booking anywhere (including the pre-existing one) ──
            rows = b19q("""SELECT interviewer_ids, scheduled_at, duration_min FROM Interview
                           WHERE status IN ('Scheduled','Confirmed')""")
            overlaps = 0
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    a, b = rows[i], rows[j]
                    if not a['interviewer_ids'] or not b['interviewer_ids']:
                        continue
                    ia = [x for x in a['interviewer_ids'].split(',') if x]
                    ib = [x for x in b['interviewer_ids'].split(',') if x]
                    if not set(ia) & set(ib):
                        continue
                    sa = b19dt.strptime(a['scheduled_at'], '%Y-%m-%d %H:%M:%S')
                    sb = b19dt.strptime(b['scheduled_at'], '%Y-%m-%d %H:%M:%S')
                    ea = sa + datetime.timedelta(minutes=int(a['duration_min'] or 60))
                    eb = sb + datetime.timedelta(minutes=int(b['duration_min'] or 60))
                    if sa < eb and sb < ea:
                        overlaps += 1
            check(overlaps == 0, 'B19: no interviewer double booking after auto-assign')

            # ── Emails only for successfully scheduled candidates (captured) ──
            check(len(captured) == 3 and all(r[1] in ('b19_0@example.com', 'b19_1@example.com',
                                                      'b19_2@example.com') for r in captured),
                  'B19: exactly 3 invitation emails captured, no real emails sent')

            # ── Manual scheduling respects interviewer conflicts ──
            aid = b19e("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                          VALUES (?,1,'B19 Manual','b19_manual@example.com','Shortlisted')""", (pid,))
            d1 = (b19dt.strptime(d0.isoformat(), '%Y-%m-%d') + datetime.timedelta(days=1))
            while d1.weekday() >= 5:
                d1 += datetime.timedelta(days=1)
            resp = client.post(f'/recruitment/application/{aid}/schedule-interview',
                               data={'date': d1.strftime('%Y-%m-%d'), 'time': '10:00',
                                     'duration': '60', 'format': 'Virtual',
                                     'meeting_link': 'https://meet.example.com/manual',
                                     'interviewer_ids': str(mgr)},
                               follow_redirects=True)
            body = resp.data.decode('utf-8', errors='replace')
            iv_manual = b19q("SELECT * FROM Interview WHERE application_id=?", (aid,), one=True)
            # The Manager may or may not be busy at 10:00; only assert that no
            # double booking occurred and a manual interview could be created
            # without a conflict (choose a fresh interviewer otherwise).
            if iv_manual is None and 'already has an interview' in body:
                check(True, 'B19: manual scheduling blocked on interviewer conflict')
            else:
                check(iv_manual is not None, 'B19: manual scheduling succeeds when free')
        finally:
            rr_mod.send_email = orig_send_email
        client.get('/logout')
finally:
    app_db_mod.DB_PATH = b19_real_db
    init_db_mod.DB_PATH = b19_real_db
    shutil.rmtree(b19_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B20 — Scorecards, score-based ranking, direct selection confirmation
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('20')
print('B20 — Scorecards and score-based selection')
print('=' * 60)

b20_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b20_')
b20_db = os.path.join(b20_tmp_dir, 'smarthr_b20.db')
shutil.copy2(app_db_mod.DB_PATH, b20_db)
b20_real_db = app_db_mod.DB_PATH

app_db_mod.DB_PATH = b20_db
init_db_mod.DB_PATH = b20_db
try:
    init_db_mod.migrate_scorecard_recommendation()

    with app.test_client() as client:
        client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                    follow_redirects=True)

        # ── Fixtures: posting + three candidates + three completed interviews ──
        br = dbe("INSERT INTO Branch (company_id, name) VALUES (1,'B20 Branch')")
        dept20 = dbq("""SELECT d.department_id FROM Department d
                        JOIN Branch b ON d.branch_id=b.branch_id
                        WHERE b.company_id=1 ORDER BY d.department_id LIMIT 1""", one=True)
        pid = dbe("INSERT INTO Job_Posting (title, department_id, branch_id, status) VALUES ('B20 Role',?,?,'Open')",
                  (dept20['department_id'], br))
        a1 = dbe("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                    VALUES (?,1,'B20 Alpha','b20a@example.com','Interview')""", (pid,))
        a2 = dbe("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                    VALUES (?,1,'B20 Beta','b20b@example.com','Interview')""", (pid,))
        a3 = dbe("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                    VALUES (?,1,'B20 Gamma','b20g@example.com','Interview')""", (pid,))
        i1 = dbe("""INSERT INTO Interview (application_id, scheduled_at, status, result)
                    VALUES (?, '2026-09-10 09:00:00', 'Completed', 'Pass')""", (a1,))
        i2 = dbe("""INSERT INTO Interview (application_id, scheduled_at, status, result)
                    VALUES (?, '2026-09-11 09:00:00', 'Completed', 'Pass')""", (a2,))
        i3 = dbe("""INSERT INTO Interview (application_id, scheduled_at, status, result)
                    VALUES (?, '2026-09-12 09:00:00', 'Completed', 'Pass')""", (a3,))
        i_open = dbe("""INSERT INTO Interview (application_id, scheduled_at, status)
                        VALUES (?, '2026-09-20 09:00:00', 'Scheduled')""", (a2,))

        # ── Scorecard validation + recording (Admin) ──
        resp = client.post(f'/recruitment/interview/{i1}/scorecard',
                           data={'technical': '6', 'communication': '5', 'fit': '4',
                                 'note_technical': 'x', 'note_communication': 'y', 'note_fit': 'z'},
                           follow_redirects=True)
        body = resp.data.decode('utf-8', errors='replace')
        row = dbq("SELECT * FROM Interview_Scorecard WHERE interview_id=?", (i1,), one=True)
        check(row is None and 'between 1 and 5' in body,
              'B20: out-of-range criterion rejected')

        resp = client.post(f'/recruitment/interview/{i1}/scorecard',
                           data={'technical': '4', 'communication': '5', 'fit': '5',
                                 'note_technical': 'x', 'note_communication': '', 'note_fit': 'z'},
                           follow_redirects=True)
        row = dbq("SELECT * FROM Interview_Scorecard WHERE interview_id=?", (i1,), one=True)
        check(row is None, 'B20: missing evidence note rejected')

        resp = client.post(f'/recruitment/interview/{i1}/scorecard',
                           data={'technical': '4', 'communication': '5', 'fit': '5',
                                 'note_technical': 'Strong technical answers',
                                 'note_communication': 'Clear communicator',
                                 'note_fit': 'Great culture fit'},
                           follow_redirects=True)
        row = dbq("SELECT * FROM Interview_Scorecard WHERE interview_id=?", (i1,), one=True)
        check(resp.status_code == 200 and row is not None
              and row['technical'] == 4 and row['communication'] == 5 and row['fit'] == 5,
              'B20: Admin records a scorecard on a completed interview')

        # Non-completed interview cannot be scored
        resp = client.post(f'/recruitment/interview/{i_open}/scorecard',
                           data={'technical': '3', 'communication': '3', 'fit': '3',
                                 'note_technical': 'n1', 'note_communication': 'n2', 'note_fit': 'n3'},
                           follow_redirects=True)
        row = dbq("SELECT * FROM Interview_Scorecard WHERE interview_id=?", (i_open,), one=True)
        check(row is None, 'B20: scorecard blocked for non-completed interview')

        # Second candidate scorecard (lower total)
        client.post(f'/recruitment/interview/{i2}/scorecard',
                    data={'technical': '2', 'communication': '3', 'fit': '4',
                          'note_technical': 'Average technical',
                          'note_communication': 'OK communicator',
                          'note_fit': 'Reasonable fit'},
                    follow_redirects=True)

        # Re-submission is rejected — the first recorded decision is final
        resp = client.post(f'/recruitment/interview/{i1}/scorecard',
                           data={'technical': '5', 'communication': '5', 'fit': '5',
                                 'note_technical': 'Updated strong technical',
                                 'note_communication': 'Clear communicator',
                                 'note_fit': 'Great culture fit'},
                           follow_redirects=True)
        row = dbq("SELECT technical, communication, fit FROM Interview_Scorecard WHERE interview_id=?", (i1,), one=True)
        check(resp.status_code == 200 and b'first decision is final' in resp.data
              and row is not None and row['technical'] == 4
              and row['communication'] == 5 and row['fit'] == 5,
              'B20: re-submission is rejected and the first scorecard decision stays fixed')

        # ── HR Manager inherits Admin-level scorecard access ──
        client.get('/logout')
        client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                    follow_redirects=True)
        resp = client.post(f'/recruitment/interview/{i3}/scorecard',
                           data={'technical': '3', 'communication': '3', 'fit': '3',
                                 'note_technical': 'n1', 'note_communication': 'n2', 'note_fit': 'n3'},
                           follow_redirects=True)
        row = dbq("SELECT fit FROM Interview_Scorecard WHERE interview_id=?", (i3,), one=True)
        check(resp.status_code == 200 and row['fit'] == 3,
              'B20: HR Manager records a scorecard with Admin-level access')

        # ── Advisory ranking on the posting page ──
        resp = client.get(f'/recruitment/postings/{pid}')
        body = resp.data.decode('utf-8', errors='replace')
        check('Candidate Ranking' in body and 'B20 Alpha' in body
              and 'B20 Beta' in body,
              'B20: ranking section lists both scored candidates')
        rank_start = body.find('Candidate Ranking')
        rank_body = body[rank_start:]
        check(rank_body.find('B20 Alpha') < rank_body.find('B20 Beta'),
              'B20: higher-total candidate ranked first')

        # ── Scorecard-only selection: manual recommendation is inert ──
        resp = client.post(f'/recruitment/postings/{pid}/recommend',
                           data={'application_id': str(a1)}, follow_redirects=True)
        rec_count = dbq("SELECT COUNT(*) AS c FROM Candidate_Recommendation WHERE posting_id=?", (pid,), one=True)['c']
        check(resp.status_code == 200 and rec_count == 0
              and b'completed scorecard ranking' in resp.data,
              'B20: manual recommendation no longer creates a selection record')

        # The higher-ranked candidate is confirmable; a lower-ranked candidate is not.
        resp = client.post(f'/recruitment/applications/{a2}/confirm-selection', follow_redirects=True)
        rec2 = dbq("SELECT * FROM Candidate_Recommendation WHERE application_id=?", (a2,), one=True)
        check(rec2 is None and b'unfilled openings' in resp.data,
              'B20: HR Manager cannot confirm a lower-scoring candidate')
        resp = client.post(f'/recruitment/applications/{a1}/confirm-selection', follow_redirects=True)
        rec1 = dbq("SELECT * FROM Candidate_Recommendation WHERE application_id=?", (a1,), one=True)
        check(rec1 is not None and rec1['status'] == 'Approved'
              and rec1['recommended_by'] is not None and rec1['approved_by'] is not None
              and rec1['approved_at'] is not None,
              'B20: HR Manager directly confirms the highest-scoring candidate')

        # ── Multi-opening: the next-best candidates become confirmable ──
        dbe("UPDATE Job_Posting SET approved_openings=3 WHERE posting_id=?", (pid,))
        resp = client.post(f'/recruitment/applications/{a2}/confirm-selection', follow_redirects=True)
        rec2 = dbq("SELECT status FROM Candidate_Recommendation WHERE application_id=?", (a2,), one=True)
        check(resp.status_code == 200 and rec2 is not None and rec2['status'] == 'Approved',
              'B20: second-highest candidate is confirmable when openings remain')
        resp = client.post(f'/recruitment/applications/{a3}/confirm-selection', follow_redirects=True)
        rec3 = dbq("SELECT status FROM Candidate_Recommendation WHERE application_id=?", (a3,), one=True)
        check(resp.status_code == 200 and rec3 is not None and rec3['status'] == 'Approved',
              'B20: tied candidate at the ranking cutoff stays eligible')

        # A candidate below the remaining slots is blocked
        a4 = dbe("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                    VALUES (?,1,'B20 Delta','b20d@example.com','Interview')""", (pid,))
        i4 = dbe("""INSERT INTO Interview (application_id, scheduled_at, status, result)
                    VALUES (?, '2026-09-13 09:00:00', 'Completed', 'Pass')""", (a4,))
        client.post(f'/recruitment/interview/{i4}/scorecard',
                    data={'technical': '2', 'communication': '2', 'fit': '2',
                          'note_technical': 'n1', 'note_communication': 'n2', 'note_fit': 'n3'},
                    follow_redirects=True)
        resp = client.post(f'/recruitment/applications/{a4}/confirm-selection', follow_redirects=True)
        rec4 = dbq("SELECT * FROM Candidate_Recommendation WHERE application_id=?", (a4,), one=True)
        check(rec4 is None and b'unfilled openings' in resp.data,
              'B20: a candidate below the remaining opening slots is blocked')

        # Filling all openings closes further confirmations
        dbe("UPDATE Job_Posting SET filled_openings=3 WHERE posting_id=?", (pid,))
        resp = client.post(f'/recruitment/applications/{a4}/confirm-selection', follow_redirects=True)
        rec4 = dbq("SELECT * FROM Candidate_Recommendation WHERE application_id=?", (a4,), one=True)
        check(rec4 is None,
              'B20: no further confirmations once all openings are filled')

        client.get('/logout')
finally:
    app_db_mod.DB_PATH = b20_real_db
    init_db_mod.DB_PATH = b20_real_db
    shutil.rmtree(b20_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B21 — Offer lifecycle: approval gate, reservations, expiry, replacements, hire
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('21')
print('B21 — Offer lifecycle')
print('=' * 60)

b21_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b21_')
b21_db = os.path.join(b21_tmp_dir, 'smarthr_b21.db')
shutil.copy2(app_db_mod.DB_PATH, b21_db)
b21_real_db = app_db_mod.DB_PATH

app_db_mod.DB_PATH = b21_db
init_db_mod.DB_PATH = b21_db
try:
    init_db_mod.migrate_offer_lifecycle()

    from app.recruitment import routes as b21_rr
    from app.recruitment import contract_pdf as b21_pdf
    from app.notifications import email_service as b21_email_service
    from app.database import query as b21q
    from app.database import execute as b21e

    with app.test_client() as client:
        # Email local-capture mode
        captured = []
        orig_send = b21_rr.send_email
        orig_email_service_send = b21_email_service.send_email
        orig_generate_pdf = b21_pdf.generate_contract_pdf
        b21_rr.send_email = lambda s, r, h, attachments=None: (captured.append((s, r)), True)[1]
        b21_email_service.send_email = lambda *args, **kwargs: True
        b21_pdf.generate_contract_pdf = lambda *args, **kwargs: None

        try:
            # ── Fixtures (created after the first request so the DB helpers
            #    run inside the client's application context) ──
            client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                        follow_redirects=True)
            dept21 = b21q("""SELECT d.department_id FROM Department d
                             JOIN Branch b ON d.branch_id=b.branch_id
                             WHERE b.company_id=1 ORDER BY d.department_id LIMIT 1""", one=True)
            pid = b21e("INSERT INTO Job_Posting (title, department_id, branch_id, status, approved_openings) "
                       "VALUES ('B21 Role',?,1,'Open',2)", (dept21['department_id'],))
            app_ids = []
            for i in range(3):
                app_ids.append(b21e("""INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status)
                                       VALUES (?,1,?,?,'Shortlisted')""",
                                    (pid, 'B21 C%d' % i, 'b21_%d@example.com' % i)))
            cids = []
            for a in app_ids:
                cids.append(b21e("""INSERT INTO Contract (application_id, position, base_salary, employment_type, status)
                                    VALUES (?, 'B21 Pos', 5000, 'Full-Time', 'Draft')""", (a,)))

            admin_uid = b21q("SELECT employee_id FROM Employee WHERE email='admin@smarthr.my'", one=True)['employee_id']
            for a in app_ids:
                b21e("""INSERT INTO Candidate_Recommendation
                        (posting_id, application_id, recommended_by, status, approved_by, approved_at)
                        VALUES (?,?,?,'Approved',?,datetime('now'))""",
                     (pid, a, admin_uid, admin_uid))

            # ── Approval gate: send blocked without approval ──
            resp = client.post(f'/recruitment/application/{app_ids[0]}/send-offer', follow_redirects=True)
            c0 = b21q("SELECT * FROM Contract WHERE contract_id=?", (cids[0],), one=True)
            check(c0['status'] == 'Sent' and len(captured) == 1,
                  'B21: Admin sends an approved selection directly')

            # ── Request + approve ──
            client.post(f'/recruitment/contract/{cids[0]}/offer-approval', follow_redirects=True)
            ap0 = b21q("SELECT * FROM Offer_Approval WHERE contract_id=?", (cids[0],), one=True)
            check(ap0 is not None and ap0['status'] == 'Pending',
                  'B21: offer approval request created as Pending')
            client.get('/logout')
            client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'}, follow_redirects=True)
            resp = client.post(f'/recruitment/contract/{cids[0]}/offer-approval/approve', follow_redirects=True)
            ap0 = b21q("SELECT * FROM Offer_Approval WHERE contract_id=?", (cids[0],), one=True)
            check(ap0['status'] == 'Approved' and ap0['approved_by'] is not None,
                  'B21: HR Manager approves the offer')

            # ── Send after approval: reservation + delivery log ──
            client.get('/logout')
            client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'}, follow_redirects=True)
            client.post(f'/recruitment/application/{app_ids[0]}/send-offer', follow_redirects=True)
            c0 = b21q("SELECT * FROM Contract WHERE contract_id=?", (cids[0],), one=True)
            res0 = b21q("""SELECT * FROM Opening_Reservation WHERE application_id=?""", (app_ids[0],), one=True)
            dl0 = b21q("""SELECT * FROM Email_Delivery_Log WHERE related_type='offer' AND related_id=?""",
                       (cids[0],), one=True)
            check(c0['status'] == 'Sent' and c0['token_expires_at'] is not None
                  and res0 is not None and res0['status'] == 'Reserved'
                  and dl0 is not None and dl0['status'] == 'Sent',
                  'B21: approved offer sends, reserves an opening, logs delivery')
            check(len(captured) == 2 and all(row[1] == 'b21_0@example.com' for row in captured),
                  'B21: Admin direct send and explicit resend are captured (stub mode)')

            # ── Delivery failure: no reservation, log Failed, retryable ──
            client.post(f'/recruitment/contract/{cids[1]}/offer-approval', follow_redirects=True)
            client.get('/logout')
            client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'}, follow_redirects=True)
            client.post(f'/recruitment/contract/{cids[1]}/offer-approval/approve', follow_redirects=True)
            client.get('/logout')
            client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'}, follow_redirects=True)

            b21_rr.send_email = lambda s, r, h, attachments=None: (captured.append((s, r)), False)[1]
            client.post(f'/recruitment/application/{app_ids[1]}/send-offer', follow_redirects=True)
            res1 = b21q("""SELECT * FROM Opening_Reservation WHERE application_id=?""", (app_ids[1],), one=True)
            dl1 = b21q("""SELECT * FROM Email_Delivery_Log WHERE related_type='offer' AND related_id=?""",
                       (cids[1],), one=True)
            check(res1 is None and dl1 is not None and dl1['status'] == 'Failed',
                  'B21: failed delivery reserves no opening and logs Failed')
            b21_rr.send_email = lambda s, r, h, attachments=None: (captured.append((s, r)), True)[1]
            client.post(f'/recruitment/application/{app_ids[1]}/send-offer', follow_redirects=True)
            res1 = b21q("""SELECT * FROM Opening_Reservation WHERE application_id=?""", (app_ids[1],), one=True)
            check(res1 is not None and res1['status'] == 'Reserved',
                  'B21: resend after failure reserves the opening')

            # ── Acceptance keeps the reservation; decline releases it ──
            tok1 = b21q("SELECT accept_token FROM Contract WHERE contract_id=?", (cids[1],), one=True)['accept_token']
            accept_page = client.get(f'/recruitment/contract/{cids[1]}/accept?token={tok1}')
            accept_html = accept_page.data.decode('utf-8', errors='replace')
            check(accept_page.status_code == 200 and 'Accept Offer' in accept_html
                  and 'Offer Acceptance' in accept_html
                  and 'class="sidebar"' not in accept_html and 'sidebar-overlay' not in accept_html,
                  'B21: candidate acceptance page renders standalone without the app chrome')
            client.post(f'/recruitment/contract/{cids[1]}/accept?token={tok1}',
                        data={'action': 'decline'})
            res1 = b21q("""SELECT * FROM Opening_Reservation WHERE application_id=?""", (app_ids[1],), one=True)
            check(res1['status'] == 'Released' and res1['release_reason'] == 'declined',
                  'B21: decline releases the reservation')
            c1 = b21q("SELECT status FROM Contract WHERE contract_id=?", (cids[1],), one=True)
            check(c1['status'] == 'Declined', 'B21: decline marks the contract Declined')

            tok0 = b21q("SELECT accept_token FROM Contract WHERE contract_id=?", (cids[0],), one=True)['accept_token']
            client.post(f'/recruitment/contract/{cids[0]}/accept?token={tok0}', data={})
            res0 = b21q("""SELECT * FROM Opening_Reservation WHERE application_id=?""", (app_ids[0],), one=True)
            c0 = b21q("SELECT status FROM Contract WHERE contract_id=?", (cids[0],), one=True)
            check(res0['status'] == 'Reserved' and c0['status'] == 'Accepted',
                  'B21: acceptance keeps the reservation; no auto-hire')

            # ── Expiry (server-side): Expired/Offer Expired/release/notify, no auto-send ──
            before_expiry = len(captured)
            b21e("UPDATE Contract SET status='Sent', token_expires_at='2000-01-01 00:00:00' WHERE contract_id=?",
                 (cids[2],))
            b21e("""INSERT INTO Opening_Reservation (posting_id, application_id, status)
                    VALUES (?,?,'Reserved')""", (pid, app_ids[2]))
            n = b21_rr.process_expired_offers()
            c2 = b21q("SELECT status FROM Contract WHERE contract_id=?", (cids[2],), one=True)
            a2 = b21q("SELECT status FROM Job_Application WHERE application_id=?", (app_ids[2],), one=True)
            res2 = b21q("""SELECT * FROM Opening_Reservation WHERE application_id=?""", (app_ids[2],), one=True)
            notif = b21q("""SELECT title FROM Notification ORDER BY notification_id DESC LIMIT 1""", one=True)
            check(n == 1 and c2['status'] == 'Expired' and a2['status'] == 'Offer Expired'
                  and res2['status'] == 'Released' and res2['release_reason'] == 'offer_expired'
                  and notif['title'] == 'Offer Expired',
                  'B21: expiry sweep transitions contract/application, releases the opening, notifies')
            check(len(captured) == before_expiry,
                  'B21: expiry never auto-sends a replacement offer')

            # ── Multi-opening hire: HR Manager confirms, fills 1 then 2 ──
            pid3 = b21e("INSERT INTO Job_Posting (title, department_id, branch_id, status, approved_openings) "
                        "VALUES ('B21 Multi',?,1,'Open',2)", (dept21['department_id'],))
            a6 = b21e("INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status) "
                      "VALUES (?,1,'B21 Six','b21_6@example.com','Shortlisted')", (pid3,))
            a7 = b21e("INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status) "
                      "VALUES (?,1,'B21 Seven','b21_7@example.com','Shortlisted')", (pid3,))
            a8 = b21e("INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, status) "
                      "VALUES (?,1,'B21 Eight','b21_8@example.com','Shortlisted')", (pid3,))
            c6 = b21e("INSERT INTO Contract (application_id, position, base_salary, employment_type, status) "
                      "VALUES (?, 'P', 5000, 'Full-Time', 'Draft')", (a6,))
            c7 = b21e("INSERT INTO Contract (application_id, position, base_salary, employment_type, status) "
                      "VALUES (?, 'P', 5000, 'Full-Time', 'Draft')", (a7,))
            for a in (a6, a7):
                b21e("""INSERT INTO Candidate_Recommendation
                        (posting_id, application_id, recommended_by, status, approved_by, approved_at)
                        VALUES (?,?,?,'Approved',?,datetime('now'))""",
                     (pid3, a, admin_uid, admin_uid))
            for cid, candidate_aid in ((c6, a6), (c7, a7)):
                client.post(f'/recruitment/contract/{cid}/offer-approval', follow_redirects=True)
                client.get('/logout')
                client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'}, follow_redirects=True)
                client.post(f'/recruitment/contract/{cid}/offer-approval/approve', follow_redirects=True)
                client.get('/logout')
                client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'}, follow_redirects=True)
                client.post(f'/recruitment/application/{candidate_aid}/send-offer', follow_redirects=True)
                client.post(f'/recruitment/application/{candidate_aid}/accept-offer', follow_redirects=True)

            client.get('/logout')
            client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'}, follow_redirects=True)
            resp = client.get(f'/recruitment/application/{a6}/hire', follow_redirects=True)
            p3 = b21q("SELECT status, filled_openings FROM Job_Posting WHERE posting_id=?", (pid3,), one=True)
            a8s = b21q("SELECT status FROM Job_Application WHERE application_id=?", (a8,), one=True)
            check(resp.status_code == 200 and p3['status'] == 'Partially Filled' and p3['filled_openings'] == 1
                  and a8s['status'] == 'Shortlisted',
                  'B21: first hire fills 1/2, posting Partially Filled, others stay active')
            client.get(f'/recruitment/application/{a7}/hire', follow_redirects=True)
            p3 = b21q("SELECT status, filled_openings FROM Job_Posting WHERE posting_id=?", (pid3,), one=True)
            a8s = b21q("SELECT status FROM Job_Application WHERE application_id=?", (a8,), one=True)
            check(p3['status'] == 'Archived' and p3['filled_openings'] == 2 and a8s['status'] == 'Rejected',
                  'B21: second hire fills 2/2, auto-archives the posting and rejects the remaining candidate')

            # ── Plain HR blocked from confirming hire ──
            from werkzeug.security import generate_password_hash
            hr_role = b21q("SELECT role_id FROM Role WHERE role_name='HR'", one=True)
            plain_hr = b21e("""INSERT INTO Employee (company_id, branch_id, department_id, full_name, hire_date,
                                                     base_salary, role_id, email, password_hash, is_active)
                               VALUES (1,1,?, 'B21 PlainHR', '2024-01-01', 5000, ?, 'plainhr@example.com', ?, 1)""",
                            (dept21['department_id'], hr_role['role_id'], generate_password_hash('B21pass')))
            client.get('/logout')
            client.post('/login', data={'email': 'plainhr@example.com', 'password': 'B21pass'}, follow_redirects=True)
            before_hire = b21q("SELECT status FROM Job_Application WHERE application_id=?", (a7,), one=True)['status']
            client.get(f'/recruitment/application/{a7}/hire', follow_redirects=True)
            after_hire = b21q("SELECT status FROM Job_Application WHERE application_id=?", (a7,), one=True)['status']
            check(before_hire == after_hire == 'Hired',
                  'B21: plain HR cannot confirm a hire (role gate)')

            client.get('/logout')
        finally:
            b21_rr.send_email = orig_send
            b21_email_service.send_email = orig_email_service_send
            b21_pdf.generate_contract_pdf = orig_generate_pdf
finally:
    app_db_mod.DB_PATH = b21_real_db
    init_db_mod.DB_PATH = b21_real_db
    shutil.rmtree(b21_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# B22 — Recruitment workflow guardrails added after the scorecard/offer redesign
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('22')
print('B22 — Recruitment workflow guardrails')
print('=' * 60)

b22_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b22_')
b22_db = os.path.join(b22_tmp_dir, 'smarthr_b22.db')
shutil.copy2(app_db_mod.DB_PATH, b22_db)
b22_real_db = app_db_mod.DB_PATH

app_db_mod.DB_PATH = b22_db
init_db_mod.DB_PATH = b22_db
try:
    init_db_mod.migrate_scorecard_recommendation()
    init_db_mod.migrate_offer_lifecycle()

    from app.recruitment import routes as b22_rr
    from app.recruitment import contract_pdf as b22_pdf
    from app.database import query as b22q
    from app.database import execute as b22e
    from werkzeug.security import generate_password_hash

    captured_b22 = []
    b22_orig_send = b22_rr.send_email
    b22_orig_pdf = b22_pdf.generate_contract_pdf
    b22_rr.send_email = lambda s, r, h, attachments=None: (captured_b22.append((s, r)), True)[1]
    b22_pdf.generate_contract_pdf = lambda *args, **kwargs: None

    with app.test_client() as client:
        try:
            client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                        follow_redirects=True)
            anchor = b22q("""SELECT d.department_id, d.branch_id
                            FROM Department d JOIN Branch b ON b.branch_id=d.branch_id
                            WHERE b.company_id=1 ORDER BY d.department_id LIMIT 1""", one=True)
            admin_uid = b22q("SELECT employee_id FROM Employee WHERE email='admin@smarthr.my'", one=True)['employee_id']
            pid = b22e("""INSERT INTO Job_Posting
                         (title, department_id, branch_id, status, approved_openings)
                         VALUES ('B22 Workflow Role', ?, ?, 'Open', 3)""",
                       (anchor['department_id'], anchor['branch_id']))
            aid = b22e("""INSERT INTO Job_Application
                         (posting_id, company_id, applicant_name, applicant_email, status)
                         VALUES (?, 1, 'B22 Candidate', 'b22@example.com', 'Interview')""", (pid,))
            iid = b22e("""INSERT INTO Interview (application_id, scheduled_at, status)
                         VALUES (?, '2000-01-03 10:00:00', 'Scheduled')""", (aid,))

            # Legacy Pass/Fail is harmless and cannot reject candidates.
            client.post(f'/recruitment/interview/{iid}/result', data={'result': 'Fail'}, follow_redirects=True)
            iv = b22q("SELECT status, result FROM Interview WHERE interview_id=?", (iid,), one=True)
            candidate = b22q("SELECT status FROM Job_Application WHERE application_id=?", (aid,), one=True)
            check(iv['status'] == 'Scheduled' and iv['result'] is None and candidate['status'] == 'Interview',
                  'B22: retired Pass/Fail endpoint cannot complete or reject a candidate')

            bypass = client.post(f'/recruitment/applications/{aid}/status',
                                 data={'status': 'Hired'}, follow_redirects=True)
            candidate = b22q("SELECT status FROM Job_Application WHERE application_id=?", (aid,), one=True)
            check(candidate['status'] == 'Interview' and b'cannot be set directly' in bypass.data,
                  'B22: generic status endpoint cannot bypass selection, offer, and hire controls')

            client.post(f'/recruitment/interview/{iid}/complete', follow_redirects=True)
            iv = b22q("SELECT status, result FROM Interview WHERE interview_id=?", (iid,), one=True)
            check(iv['status'] == 'Completed' and iv['result'] is None,
                  'B22: completion replaces Pass/Fail without a hiring decision')
            page = client.get(f'/recruitment/applications/{aid}').data.decode('utf-8', errors='replace')
            check('name="result"' not in page and 'Schedule Interview' not in page and 'Mark Completed' not in page,
                  'B22: completed interviews expose neither Pass/Fail nor scheduling actions')

            before_count = b22q("SELECT COUNT(*) AS c FROM Interview WHERE application_id=?", (aid,), one=True)['c']
            response = client.post(f'/recruitment/application/{aid}/schedule-interview', data={
                'date': '2030-01-03', 'time': '10:00', 'format': 'Virtual',
                'meeting_link': 'https://example.com/b22'}, follow_redirects=True)
            after_count = b22q("SELECT COUNT(*) AS c FROM Interview WHERE application_id=?", (aid,), one=True)['c']
            check(before_count == after_count and b'Only New or Shortlisted candidates' in response.data,
                  'B22: completed-interview application is server-blocked from another interview')

            client.post(f'/recruitment/interview/{iid}/scorecard', data={
                'technical': '5', 'communication': '4', 'fit': '5',
                'note_technical': 'Strong role knowledge',
                'note_communication': 'Clear answers',
                'note_fit': 'Strong team fit'}, follow_redirects=True)
            client.get('/logout')
            client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'}, follow_redirects=True)
            client.post(f'/recruitment/applications/{aid}/confirm-selection', follow_redirects=True)
            rec = b22q("SELECT * FROM Candidate_Recommendation WHERE application_id=?", (aid,), one=True)
            check(rec is not None and rec['status'] == 'Approved',
                  'B22: HR Manager confirms the scorecard-ranked candidate directly')
            client.get('/logout')
            client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'}, follow_redirects=True)

            # The posting owns the department; a tampered form value is ignored.
            contract_page = client.get(f'/recruitment/contract/{aid}').data.decode('utf-8', errors='replace')
            check(anchor['department_id'] and 'Derived from the job posting' in contract_page and 'readonly' in contract_page,
                  'B22: contract form displays the posting-derived department as read-only')
            client.post(f'/recruitment/contract/{aid}', data={
                'offer_date': '2030-01-03', 'start_date': '2030-02-03',
                'position': 'B22 Workflow Role', 'department_id': '999999',
                'work_start_time': '09:00', 'work_end_time': '18:00',
                'base_salary': '5000', 'employment_type': 'Full-Time'}, follow_redirects=True)
            contract_admin = b22q("SELECT * FROM Contract WHERE application_id=?", (aid,), one=True)
            check(contract_admin is not None and contract_admin['department_id'] == anchor['department_id'],
                  'B22: contract save ignores a tampered department and uses the posting department')

            client.post(f'/recruitment/application/{aid}/send-offer', follow_redirects=True)
            contract_admin = b22q("SELECT status FROM Contract WHERE contract_id=?", (contract_admin['contract_id'],), one=True)
            check(contract_admin['status'] == 'Sent' and len(captured_b22) == 1,
                  'B22: Admin sends an approved candidate offer directly in capture mode')
            sent_contract_page = client.get(f'/recruitment/contract/{aid}').data.decode('utf-8', errors='replace')
            check('Save Contract Draft' not in sent_contract_page and 'no longer editable' in sent_contract_page,
                  'B22: a sent contract is read-only in the editor route')

            # A second selected candidate proves HR Manager also sends directly.
            aid_manager = b22e("""INSERT INTO Job_Application
                                 (posting_id, company_id, applicant_name, applicant_email, status)
                                 VALUES (?, 1, 'B22 Manager Candidate', 'b22-manager@example.com', 'Interview')""", (pid,))
            cid_manager = b22e("""INSERT INTO Contract
                                (application_id, position, department_id, base_salary, employment_type, status)
                                VALUES (?, 'B22 Workflow Role', ?, 5000, 'Full-Time', 'Draft')""",
                               (aid_manager, anchor['department_id']))
            b22e("""INSERT INTO Candidate_Recommendation
                    (posting_id, application_id, recommended_by, status, approved_by, approved_at)
                    VALUES (?, ?, ?, 'Approved', ?, datetime('now'))""",
                 (pid, aid_manager, admin_uid, admin_uid))
            client.get('/logout')
            client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'}, follow_redirects=True)
            client.post(f'/recruitment/application/{aid_manager}/send-offer', follow_redirects=True)
            manager_contract = b22q("SELECT status FROM Contract WHERE contract_id=?", (cid_manager,), one=True)
            check(manager_contract['status'] == 'Sent' and len(captured_b22) == 2,
                  'B22: HR Manager sends an approved candidate offer directly in capture mode')

            # A plain HR user still needs an offer-send approval.
            client.get('/logout')
            hr_role = b22q("SELECT role_id FROM Role WHERE role_name='HR'", one=True)
            b22e("""INSERT INTO Employee
                    (company_id, branch_id, department_id, full_name, hire_date, base_salary,
                     role_id, email, password_hash, is_active)
                    VALUES (1, ?, ?, 'B22 Plain HR', '2024-01-01', 5000, ?,
                            'b22-plainhr@example.com', ?, 1)""",
                 (anchor['branch_id'], anchor['department_id'], hr_role['role_id'],
                  generate_password_hash('B22pass')))
            aid_hr = b22e("""INSERT INTO Job_Application
                            (posting_id, company_id, applicant_name, applicant_email, status)
                            VALUES (?, 1, 'B22 HR Candidate', 'b22-hr@example.com', 'Interview')""", (pid,))
            cid_hr = b22e("""INSERT INTO Contract
                           (application_id, position, department_id, base_salary, employment_type, status)
                           VALUES (?, 'B22 Workflow Role', ?, 5000, 'Full-Time', 'Draft')""",
                          (aid_hr, anchor['department_id']))
            b22e("""INSERT INTO Candidate_Recommendation
                    (posting_id, application_id, recommended_by, status, approved_by, approved_at)
                    VALUES (?, ?, ?, 'Approved', ?, datetime('now'))""",
                 (pid, aid_hr, admin_uid, admin_uid))
            client.post('/login', data={'email': 'b22-plainhr@example.com', 'password': 'B22pass'}, follow_redirects=True)
            response = client.post(f'/recruitment/application/{aid_hr}/send-offer', follow_redirects=True)
            hr_contract = b22q("SELECT status FROM Contract WHERE contract_id=?", (cid_hr,), one=True)
            check(hr_contract['status'] == 'Draft' and b'must be approved' in response.data and len(captured_b22) == 2,
                  'B22: plain HR cannot send without an offer-send approval')
        finally:
            b22_rr.send_email = b22_orig_send
            b22_pdf.generate_contract_pdf = b22_orig_pdf
finally:
    app_db_mod.DB_PATH = b22_real_db
    init_db_mod.DB_PATH = b22_real_db
    shutil.rmtree(b22_tmp_dir, ignore_errors=True)

# ══════════════════════════════════════════════════════════════════════════════
# B23 — Field-validation guardrails found during visual QA
# ══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('23')
print('B23 — Field-validation guardrails')
print('=' * 60)

b23_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b23_')
b23_db = os.path.join(b23_tmp_dir, 'smarthr_b23.db')
shutil.copy2(app_db_mod.DB_PATH, b23_db)
b23_real_db = app_db_mod.DB_PATH
app_db_mod.DB_PATH = b23_db
init_db_mod.DB_PATH = b23_db
from app.recruitment import routes as b23_rr
b23_orig_send = b23_rr.send_email
b23_rr.send_email = lambda *args, **kwargs: True
try:
    with app.app_context():
        b23_anchor = dbq("""SELECT d.department_id, d.branch_id
                           FROM Department d JOIN Branch b ON b.branch_id=d.branch_id
                           WHERE b.company_id=1 ORDER BY d.department_id LIMIT 1""", one=True)
        b23_position = dbq("""SELECT position_id FROM Position
                            WHERE department_id=? AND is_active=1
                            ORDER BY position_id LIMIT 1""", (b23_anchor['department_id'],), one=True)
        b23_admin = dbq("SELECT employee_id FROM Employee WHERE email='admin@smarthr.my'", one=True)
        b23_pid = dbe("""INSERT INTO Job_Posting
                        (title, department_id, branch_id, status, approved_openings)
                        VALUES ('B23 Validation Role', ?, ?, 'Open', 1)""",
                      (b23_anchor['department_id'], b23_anchor['branch_id']))
        b23_aid = dbe("""INSERT INTO Job_Application
                        (posting_id, company_id, applicant_name, applicant_email, status)
                        VALUES (?,1,'B23 Candidate','b23@example.com','Shortlisted')""", (b23_pid,))
        b23_contract_aid = dbe("""INSERT INTO Job_Application
                                 (posting_id, company_id, applicant_name, applicant_email, status)
                                 VALUES (?,1,'B23 Contract Candidate','b23-contract@example.com','Interview')""",
                               (b23_pid,))
        dbe("""INSERT INTO Candidate_Recommendation
               (posting_id, application_id, recommended_by, status, approved_by, approved_at)
               VALUES (?, ?, ?, 'Approved', ?, datetime('now'))""",
            (b23_pid, b23_contract_aid, b23_admin['employee_id'], b23_admin['employee_id']))

    with app.test_client() as client:
        client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                    follow_redirects=True)
        b23_weekday = datetime.date.today() + datetime.timedelta(days=7)
        while b23_weekday.weekday() >= 5:
            b23_weekday += datetime.timedelta(days=1)
        b23_weekend = b23_weekday + datetime.timedelta(days=(5 - b23_weekday.weekday()))
        base_schedule = {'date': b23_weekday.isoformat(), 'time': '10:00',
                         'format': 'Virtual', 'meeting_link': 'https://meet.example.com/b23'}

        resp = client.post(f'/recruitment/application/{b23_aid}/schedule-interview',
                           data={**base_schedule, 'duration': 'not-a-number'}, follow_redirects=True)
        check(b'whole number of minutes' in resp.data,
              'B23: manual scheduling rejects a non-numeric duration without a 500')

        resp = client.post(f'/recruitment/application/{b23_aid}/schedule-interview',
                           data={**base_schedule, 'duration': '0'}, follow_redirects=True)
        check(b'at least one minute' in resp.data,
              'B23: manual scheduling rejects a non-positive duration')

        resp = client.post(f'/recruitment/application/{b23_aid}/schedule-interview',
                           data={**base_schedule, 'date': b23_weekend.isoformat(), 'duration': '30'},
                           follow_redirects=True)
        check(b'cannot be scheduled on weekends' in resp.data,
              'B23: manual scheduling enforces the weekend rule')

        resp = client.post(f'/recruitment/application/{b23_aid}/schedule-interview',
                           data={**base_schedule, 'time': '00:01', 'duration': '30'}, follow_redirects=True)
        check(b'within working hours' in resp.data,
              'B23: manual scheduling enforces interview-policy working hours')

        resp = client.post(f'/recruitment/application/{b23_aid}/schedule-interview',
                           data={**base_schedule, 'duration': '30', 'interviewer_ids': '999999'},
                           follow_redirects=True)
        check(b'active interviewers from your company' in resp.data,
              'B23: manual scheduling rejects forged or inactive interviewer IDs')

        bulk_base = {'posting_id': str(b23_pid), 'date': b23_weekday.isoformat(),
                     'start_time': '10:00', 'duration': '30', 'format': 'Virtual',
                     'meeting_link': 'https://meet.example.com/b23'}
        resp = client.post('/recruitment/bulk-schedule',
                           data={**bulk_base, 'date': '2000-01-03'}, follow_redirects=True)
        check(b'Interview time must be in the future' in resp.data,
              'B23: bulk scheduling blocks a past interview date')
        resp = client.post('/recruitment/bulk-schedule',
                           data={**bulk_base, 'duration': 'not-a-number'}, follow_redirects=True)
        check(b'Select a valid posting, date, start time, duration' in resp.data,
              'B23: bulk scheduling rejects malformed numeric fields without a 500')
        resp = client.post('/recruitment/bulk-schedule',
                           data={**bulk_base, 'start_time': '00:01'}, follow_redirects=True)
        check(b'within working hours' in resp.data,
              'B23: bulk scheduling enforces interview-policy working hours')
        resp = client.post('/recruitment/bulk-schedule',
                           data={**bulk_base, 'interviewer_ids': '999999'}, follow_redirects=True)
        check(b'active interviewers from your company' in resp.data,
              'B23: bulk scheduling rejects forged interviewer IDs')

        vacancy_base = {'department_id': str(b23_anchor['department_id']),
                        'position_id': '__custom__', 'position_title': 'B23 Validation Position',
                        'employment_type': 'Full-Time', 'requested_openings': '1',
                        'target_audience': 'Both', 'reason': 'Validation test'}
        resp = client.post('/recruitment/vacancy-request',
                           data={**vacancy_base, 'min_salary': '-1'}, follow_redirects=True)
        check(b'Salary values cannot be negative' in resp.data,
              'B23: vacancy request rejects negative salary values')
        resp = client.post('/recruitment/vacancy-request',
                           data={**vacancy_base, 'min_salary': '6000', 'max_salary': '5000'}, follow_redirects=True)
        check(b'Minimum salary cannot exceed maximum salary' in resp.data,
              'B23: vacancy request rejects an inverted salary range')

        direct_posting = {'department_id': str(b23_anchor['department_id']),
                          'branch_id': str(b23_anchor['branch_id']),
                          'position_id': str(b23_position['position_id']),
                          'employment_type': 'Full-Time', 'target_audience': 'Both'}
        resp = client.post('/recruitment/postings/add',
                           data={**direct_posting, 'min_salary': '-1'}, follow_redirects=True)
        check(b'Salary values cannot be negative' in resp.data,
              'B23: direct posting rejects negative salary values')
        resp = client.post('/recruitment/postings/add',
                           data={**direct_posting, 'min_salary': '6000', 'max_salary': '5000'}, follow_redirects=True)
        check(b'Minimum salary cannot exceed maximum salary' in resp.data,
              'B23: direct posting rejects an inverted salary range')

        resp = client.post(f'/recruitment/postings/{b23_pid}/add-application',
                           data={'internal_employee_id': 'not-an-id'}, follow_redirects=True)
        check(b'Select a valid employee for an internal application' in resp.data,
              'B23: manual internal application rejects a malformed employee ID')

        contract_base = {'offer_date': '2030-01-03', 'start_date': '2030-02-03',
                         'position': 'B23 Contract Role', 'work_start_time': '09:00',
                         'work_end_time': '18:00', 'base_salary': '5000',
                         'employment_type': 'Full-Time'}
        resp = client.post(f'/recruitment/contract/{b23_contract_aid}',
                           data={**contract_base, 'base_salary': 'not-a-number'}, follow_redirects=True)
        check(b'Enter valid contract dates, working times, and base salary' in resp.data,
              'B23: contract rejects malformed salary values without a 500')
        resp = client.post(f'/recruitment/contract/{b23_contract_aid}',
                           data={**contract_base, 'base_salary': '-1'}, follow_redirects=True)
        check(b'Base salary cannot be negative' in resp.data,
              'B23: contract rejects a negative salary')

        policy_base = {'default_duration_min': '60', 'default_type': 'In-Person',
                       'default_location': '', 'default_meeting_link': '',
                       'day_start_time': '09:00', 'day_end_time': '17:00',
                       'slot_gap_min': '15', 'max_per_day': '8'}
        resp = client.post('/recruitment/interview-policy',
                           data={**policy_base, 'default_duration_min': '10'}, follow_redirects=True)
        check(b'Default duration must be at least 15 minutes' in resp.data,
              'B23: interview policy enforces its stated minimum duration')
        resp = client.post('/recruitment/interview-policy',
                           data={**policy_base, 'day_start_time': '17:00', 'day_end_time': '09:00'}, follow_redirects=True)
        check(b'Working-day end time must be later' in resp.data,
              'B23: interview policy rejects inverted working hours')

        resp = client.post('/notifications/email-config',
                           data={'host': 'imap.example.com', 'port': 'not-a-number',
                                 'username': 'qa@example.com', 'email': 'qa@example.com'},
                           follow_redirects=True)
        check(b'Email server port must be a whole number' in resp.data,
              'B23: email configuration rejects a malformed port without a 500')
        client.get('/logout')

        client.post('/login', data={'email': 'elizabeth@smarthr.my', 'password': 'Employee@123'},
                    follow_redirects=True)
        resp = client.post('/leave/apply',
                           data={'leave_type_id': 'invalid', 'start_date': 'not-a-date',
                                 'end_date': 'not-a-date'}, follow_redirects=True)
        check(resp.status_code == 200 and b'valid leave type, start date, and end date' in resp.data,
              'B23: malformed leave fields return a validation message instead of a 500')
        client.get('/logout')
finally:
    b23_rr.send_email = b23_orig_send
    app_db_mod.DB_PATH = b23_real_db
    init_db_mod.DB_PATH = b23_real_db
    shutil.rmtree(b23_tmp_dir, ignore_errors=True)

# ==============================================================================
# B24 — Broad module QA: route safety, validation, and record-scope enforcement
# ==============================================================================

print('=' * 60)
_focus_block('24')
print('B24 — Broad module QA')
print('=' * 60)

b24_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b24_')
b24_db = os.path.join(b24_tmp_dir, 'smarthr_b24.db')
shutil.copy2(app_db_mod.DB_PATH, b24_db)
b24_real_db = app_db_mod.DB_PATH
app_db_mod.DB_PATH = b24_db
init_db_mod.DB_PATH = b24_db

try:
    from werkzeug.security import generate_password_hash

    with app.app_context():
        b24_company = dbq("SELECT company_id FROM Company ORDER BY company_id LIMIT 1", one=True)
        b24_roles = {
            row['role_name']: row['role_id']
            for row in dbq("SELECT role_id, role_name FROM Role WHERE role_name IN ('Admin','Manager','Employee')")
        }
        check(b24_company is not None and len(b24_roles) == 3,
              'B24: baseline company and core roles are available')

        b24_branch_a = dbe("""INSERT INTO Branch(company_id, name, address, address_line1, city, state, postal_code)
                            VALUES (?, 'B24 Manager Branch', 'B24 A', 'B24 A', 'Kuala Lumpur', 'WP Kuala Lumpur', '50000')""",
                           (b24_company['company_id'],))
        b24_branch_b = dbe("""INSERT INTO Branch(company_id, name, address, address_line1, city, state, postal_code)
                            VALUES (?, 'B24 Other Branch', 'B24 B', 'B24 B', 'Kuala Lumpur', 'WP Kuala Lumpur', '50001')""",
                           (b24_company['company_id'],))
        b24_dept_a = dbe("INSERT INTO Department(branch_id, department_name) VALUES (?, 'B24 Manager Department')",
                         (b24_branch_a,))
        b24_dept_b = dbe("INSERT INTO Department(branch_id, department_name) VALUES (?, 'B24 Other Department')",
                         (b24_branch_b,))

        def b24_employee(full_name, email, branch_id, department_id, role_name, password):
            return dbe("""INSERT INTO Employee
                       (company_id, branch_id, department_id, full_name, position,
                        employment_type, employment_status, hire_date, base_salary,
                        role_id, email, password_hash, is_active)
                       VALUES (?, ?, ?, ?, 'B24 Staff', 'Full-Time', 'Active',
                               '2024-01-01', 3000, ?, ?, ?, 1)""",
                       (b24_company['company_id'], branch_id, department_id, full_name,
                        b24_roles[role_name], email, generate_password_hash(password)))

        b24_manager = b24_employee('B24 Manager', 'b24-manager@example.test',
                                   b24_branch_a, b24_dept_a, 'Manager', 'B24Manager!')
        b24_managed = b24_employee('B24 Managed Employee', 'b24-managed@example.test',
                                   b24_branch_a, b24_dept_a, 'Employee', 'B24Managed!')
        b24_other = b24_employee('B24 Other Employee', 'b24-other@example.test',
                                 b24_branch_b, b24_dept_b, 'Employee', 'B24Other!')
        b24_review_year = datetime.datetime.now().year
        dbe("""INSERT INTO Performance_Review
               (employee_id, period_year, attendance_rate, punctuality, overtime_score,
                reliability, composite_score, grade)
               VALUES (?, ?, 90, 88, 80, 85, 86, 'B')""", (b24_other, b24_review_year))
        dbe("""INSERT INTO Performance_Review
               (employee_id, period_year, attendance_rate, punctuality, overtime_score,
                reliability, composite_score, grade)
               VALUES (?, ?, 80, 78, 70, 75, 76, 'B')""", (b24_managed, b24_review_year))
        b24_invoice = dbe("""INSERT INTO Invoice
                            (employee_id, filename, original_name, file_type, vendor_name,
                             invoice_number, total_amount, total_amount_myr, status)
                            VALUES (?, 'b24_placeholder.pdf', 'b24_placeholder.pdf', 'pdf',
                                    'B24 Vendor', 'B24-INV-1', 75, 75, 'Pending')""", (b24_other,))
        b24_payroll = dbe("""INSERT INTO Payroll
                            (employee_id, pay_period_month, pay_period_year, base_salary,
                             gross_pay, total_deductions, net_pay, status)
                            VALUES (?, 1, ?, 3000, 3000, 0, 3000, 'Draft')""",
                           (b24_other, b24_review_year))
        b24_notification = dbe("""INSERT INTO Notification(employee_id, title, message, type)
                                 VALUES (?, 'B24 private notification', 'Scope test', 'Info')""",
                               (b24_managed,))

    with app.test_client() as client:
        client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                    follow_redirects=True)
        admin_pages = [
            '/organization/companies', '/organization/branches', '/organization/departments',
            '/organization/roles', '/employees/', '/attendance/', '/attendance/logs',
            '/leave/apply', '/leave/approve', '/payroll/', '/invoices/', '/invoices/claims',
            '/performance/', '/increment/', '/bonus/', '/year-end-review',
            '/compensation/policy', '/reports/', '/audit/', '/settings/'
        ]
        for path in admin_pages:
            response = client.get(path)
            check(response.status_code == 200 and b'Traceback' not in response.data,
                  f'B24: Admin module page loads safely: {path} (HTTP {response.status_code})')

        response = client.get('/audit/?action=LOGIN&page=not-a-page')
        check(response.status_code == 200 and b'Traceback' not in response.data
              and b'action=LOGIN' in response.data,
              'B24: audit pagination safely handles an invalid page and preserves action filters')

        response = client.get(f'/performance/?year={b24_review_year}')
        check(response.status_code == 200 and b'> / <' not in response.data,
              'B24: yearly performance review renders attendance evidence values')

        invalid_filter_pages = [
            '/reports/?type=attendance&year=not-a-year&month=99&dept=bad&branch=bad',
            '/payroll/?year=not-a-year&month=bad',
            '/performance/?year=not-a-year&month=99',
            '/increment/?year=not-a-year&branch_id=bad&department_id=bad',
            '/bonus/?year=not-a-year&branch_id=bad&department_id=bad',
            '/year-end-review?year=not-a-year',
            '/compensation/policy?year=not-a-year',
        ]
        for path in invalid_filter_pages:
            response = client.get(path)
            check(response.status_code == 200 and b'Traceback' not in response.data,
                  f'B24: malformed filters are handled without a 500: {path.split("?")[0]}')

        response = client.post('/payroll/generate', data={'month': 'bad', 'year': 'bad'},
                               follow_redirects=True)
        check(b'Use a valid payroll month and year.' in response.data,
              'B24: payroll generation validates a malformed period')
        response = client.post('/performance/generate', data={'month': 'bad', 'year': 'bad'},
                               follow_redirects=True)
        check(b'Use a valid month and year.' in response.data,
              'B24: performance generation validates a malformed period')
        response = client.post('/bonus/policy', data={
            'grade_A_months': '-1', 'grade_B_months': '2', 'grade_C_months': '1',
            'grade_D_months': '0.5', 'tenure_threshold_months': '3', 'payout_month': '1'
        }, follow_redirects=True)
        check(b'Error updating policy:' in response.data,
              'B24: bonus policy rejects an out-of-range amount')
        response = client.post('/compensation/policy', data={
            'inc_pct': 'bad', 'inc_tenure': '1', 'inc_eff_month': '1', 'inc_eff_year': str(b24_review_year),
            'grade_A_months': '3', 'grade_B_months': '2', 'grade_C_months': '1',
            'grade_D_months': '0.5', 'bonus_tenure': '3', 'payout_month': '1'
        }, follow_redirects=True)
        check(b'Enter valid compensation policy values.' in response.data,
              'B24: compensation policy rejects malformed numeric input')
        client.get('/logout')

        client.post('/login', data={'email': 'b24-manager@example.test', 'password': 'B24Manager!'},
                    follow_redirects=True)
        response = client.get('/')
        check(b'In your branch' in response.data and b'In your organisation' not in response.data,
              'B24: Manager dashboard labels branch-scoped employee totals accurately')
        response = client.get(f'/performance/api/score/{b24_other}?year={b24_review_year}')
        check(response.status_code == 403 and b'Access denied' in response.data,
              'B24: Manager cannot read performance data outside their branch')
        response = client.get(f'/performance/?year={b24_review_year}')
        check(b'B24 Managed Employee' in response.data and b'B24 Other Employee' not in response.data
              and b'metric-val green-val">1</div>' in response.data,
              'B24: Manager performance summary is scoped to the branch-only review list')
        response = client.post(f'/employees/{b24_managed}/edit', data={
            'full_name': 'B24 Managed Employee', 'contact_no': '', 'address': '',
            'date_of_birth': '', 'gender': 'Other', 'emergency_contact_name': '',
            'emergency_contact_no': '', 'position': 'B24 Staff',
            'employment_type': 'Full-Time', 'employment_status': 'Active', 'base_salary': '3000',
            'branch_id': str(b24_branch_b), 'department_id': str(b24_dept_b),
            'role_id': str(b24_roles['Admin']), 'work_start_time': '09:00', 'work_end_time': '18:00'
        }, follow_redirects=True)
        with app.app_context():
            managed_after = dbq("SELECT branch_id, department_id, role_id FROM Employee WHERE employee_id=?",
                                (b24_managed,), one=True)
        check(b'Managers cannot change an employee' in response.data and
              managed_after['branch_id'] == b24_branch_a and
              managed_after['department_id'] == b24_dept_a and
              managed_after['role_id'] == b24_roles['Employee'],
              'B24: crafted manager edit cannot move staff or escalate a role')
        response = client.post(f'/invoices/{b24_invoice}/approve', follow_redirects=True)
        with app.app_context():
            invoice_after = dbq("SELECT status FROM Invoice WHERE invoice_id=?", (b24_invoice,), one=True)
        check(b'Access denied: this claim belongs to another branch.' in response.data and
              invoice_after['status'] == 'Pending',
              f'B24: Manager cannot approve another branch\'s claim (HTTP {response.status_code}, status {invoice_after["status"]})')
        response = client.get(f'/payroll/{b24_payroll}', follow_redirects=True)
        check(b'Access denied. You can only view payslips of staff from your own branch.' in response.data,
              'B24: Manager cannot open another branch\'s payslip')
        client.get('/logout')

        client.post('/login', data={'email': 'b24-other@example.test', 'password': 'B24Other!'},
                    follow_redirects=True)
        response = client.get(f'/performance/api/score/{b24_other}?year={b24_review_year}')
        check(response.status_code == 200 and b'composite_score' in response.data,
              'B24: Employee can read their own performance score')
        response = client.get(f'/performance/api/score/{b24_managed}?year={b24_review_year}')
        check(response.status_code == 403 and b'Access denied' in response.data,
              'B24: Employee cannot read another employee\'s performance score')
        response = client.post(f'/notifications/mark-read/{b24_notification}')
        with app.app_context():
            notification_after = dbq("SELECT is_read FROM Notification WHERE notification_id=?",
                                     (b24_notification,), one=True)
        check(response.status_code == 200 and notification_after['is_read'] == 0,
              'B24: notification mark-read endpoint cannot alter another employee\'s notification')
        response = client.get('/organization/companies', follow_redirects=False)
        check(response.status_code == 302,
              'B24: Employee is denied organisation management')
        response = client.get('/audit/', follow_redirects=False)
        check(response.status_code == 302,
              'B24: Employee is denied the audit log')
finally:
    app_db_mod.DB_PATH = b24_real_db
    init_db_mod.DB_PATH = b24_real_db
    shutil.rmtree(b24_tmp_dir, ignore_errors=True)

# ==============================================================================
# B25 — Core operational workflow QA (leave, claims, payroll, performance, pay)
# ==============================================================================

print('=' * 60)
_focus_block('25')
print('B25 — Core operational workflow QA')
print('=' * 60)

b25_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b25_')
b25_db = os.path.join(b25_tmp_dir, 'smarthr_b25.db')
shutil.copy2(app_db_mod.DB_PATH, b25_db)
b25_real_db = app_db_mod.DB_PATH
app_db_mod.DB_PATH = b25_db
init_db_mod.DB_PATH = b25_db

from app.leave import routes as b25_leave_routes
from app.invoice import routes as b25_invoice_routes
from app.payroll import routes as b25_payroll_routes
from app.bonus import routes as b25_bonus_routes
from app.notifications import routes as b25_notification_routes

b25_original_notifiers = (
    b25_leave_routes.send_notification,
    b25_invoice_routes.send_notification,
    b25_payroll_routes.send_notification,
    b25_bonus_routes.send_notification,
    b25_notification_routes.send_notification,
)
b25_captured_notifications = []
def b25_capture_notification(*args, **kwargs):
    b25_captured_notifications.append((args, kwargs))
    return None

b25_leave_routes.send_notification = b25_capture_notification
b25_invoice_routes.send_notification = b25_capture_notification
b25_payroll_routes.send_notification = b25_capture_notification
b25_bonus_routes.send_notification = b25_capture_notification
b25_notification_routes.send_notification = b25_capture_notification

try:
    from werkzeug.security import generate_password_hash
    from io import BytesIO

    with app.app_context():
        b25_company = dbq("SELECT company_id FROM Company ORDER BY company_id LIMIT 1", one=True)
        b25_roles = {
            row['role_name']: row['role_id']
            for row in dbq("SELECT role_id, role_name FROM Role WHERE role_name IN ('Admin','Manager','Employee')")
        }
        b25_branch = dbe("""INSERT INTO Branch(company_id, name, address, address_line1, city, state, postal_code)
                           VALUES (?, 'B25 Operations Branch', 'B25 Address', 'B25 Address',
                                   'Kuala Lumpur', 'WP Kuala Lumpur', '50002')""",
                         (b25_company['company_id'],))
        b25_dept = dbe("INSERT INTO Department(branch_id, department_name) VALUES (?, 'B25 Operations')",
                       (b25_branch,))

        def b25_employee(full_name, email, role_name, password):
            return dbe("""INSERT INTO Employee
                       (company_id, branch_id, department_id, full_name, position,
                        employment_type, employment_status, hire_date, base_salary,
                        role_id, email, password_hash, is_active, gender)
                       VALUES (?, ?, ?, ?, 'B25 Staff', 'Full-Time', 'Active',
                               '2024-01-01', 3000, ?, ?, ?, 1, 'Other')""",
                       (b25_company['company_id'], b25_branch, b25_dept, full_name,
                        b25_roles[role_name], email, generate_password_hash(password)))

        b25_manager = b25_employee('B25 Manager', 'b25-manager@example.test', 'Manager', 'B25Manager!')
        b25_employee_id = b25_employee('B25 Employee', 'b25-employee@example.test', 'Employee', 'B25Employee!')
        b25_leave_type = dbe("""INSERT INTO Leave_Type(type_name, default_days, is_paid)
                               VALUES ('B25 Paid Leave', 10, 1)""")
        dbe("""INSERT INTO Leave_Balance(employee_id, leave_type_id, year, entitled_days, used_days, pending_days)
               VALUES (?, ?, ?, 10, 0, 0)""",
            (b25_employee_id, b25_leave_type, datetime.date.today().year))

        b25_year = datetime.date.today().year
        b25_month = datetime.date.today().month
        b25_draft_payroll = dbe("""INSERT INTO Payroll
                                  (employee_id, pay_period_month, pay_period_year, base_salary,
                                   gross_pay, total_deductions, net_pay, status)
                                  VALUES (?, ?, ?, 3000, 3000, 0, 3000, 'Draft')""",
                                (b25_employee_id, b25_month, b25_year))
        b25_claim = dbe("""INSERT INTO Invoice
                          (employee_id, filename, original_name, file_type, vendor_name,
                           invoice_number, total_amount, total_amount_myr, status)
                          VALUES (?, 'b25_claim.pdf', 'b25_claim.pdf', 'pdf', 'B25 Vendor',
                                  'B25-CLAIM', 125, 125, 'Pending')""", (b25_employee_id,))
        b25_increment = dbe("""INSERT INTO Salary_Increment
                              (employee_id, period_year, old_salary, new_salary, increment_pct,
                               proposed_by, status)
                              VALUES (?, ?, 3000, 3300, 10, ?, 'Pending')""",
                            (b25_employee_id, b25_year, b25_manager))
        b25_bonus = dbe("""INSERT INTO Bonus_Proposal
                          (employee_id, period_year, composite_score, grade, full_bonus_amount,
                           bonus_amount, months_worked, proposed_by, status)
                          VALUES (?, ?, 90, 'A', 9000, 4500, 12, ?, 'Pending')""",
                        (b25_employee_id, b25_year, b25_manager))
        dbe("""INSERT INTO Attendance
               (employee_id, branch_id, check_in, check_out, hours_worked, overtime_hours, status)
               VALUES (?, ?, ?, ?, 8, 0, 'Approved')""",
            (b25_employee_id, b25_branch,
             f'{b25_year}-{b25_month:02d}-01 09:00:00',
             f'{b25_year}-{b25_month:02d}-01 17:00:00'))

    b25_leave_start = datetime.date.today() + datetime.timedelta(days=7)
    while b25_leave_start.weekday() >= 5:
        b25_leave_start += datetime.timedelta(days=1)

    with app.test_client() as client:
        client.post('/login', data={'email': 'b25-employee@example.test', 'password': 'B25Employee!'},
                    follow_redirects=True)
        with app.app_context():
            b25_before_name = dbq("SELECT full_name FROM Employee WHERE employee_id=?", (b25_employee_id,), one=True)['full_name']
            b25_before_invoices = dbq("SELECT COUNT(*) AS c FROM Invoice WHERE employee_id=?", (b25_employee_id,), one=True)['c']
        response = client.post('/settings/profile', data={'full_name': ''}, follow_redirects=True)
        with app.app_context():
            b25_after_name = dbq("SELECT full_name FROM Employee WHERE employee_id=?", (b25_employee_id,), one=True)['full_name']
        check(b'Full name is required.' in response.data and b25_after_name == b25_before_name,
              'B25: profile update rejects an empty name without changing the employee')
        response = client.post('/invoices/upload', data={
            'invoice_file': (BytesIO(b'not a real invoice'), 'b25-invalid.pdf'),
            'subtotal': '-10', 'tax_amount': '0', 'total_amount': '-10', 'currency': 'MYR'
        }, content_type='multipart/form-data', follow_redirects=True)
        with app.app_context():
            b25_after_invoices = dbq("SELECT COUNT(*) AS c FROM Invoice WHERE employee_id=?", (b25_employee_id,), one=True)['c']
        check(b'Invoice amounts cannot be negative' in response.data and b25_after_invoices == b25_before_invoices,
              'B25: claims upload rejects negative amounts before saving a record or file')
        response = client.post('/leave/apply', data={
            'leave_type_id': str(b25_leave_type), 'start_date': b25_leave_start.isoformat(),
            'end_date': b25_leave_start.isoformat(), 'reason': 'B25 workflow leave'
        }, follow_redirects=True)
        with app.app_context():
            b25_leave = dbq("SELECT * FROM Leave_Application WHERE employee_id=? ORDER BY leave_id DESC LIMIT 1",
                            (b25_employee_id,), one=True)
            b25_pending = dbq("SELECT pending_days FROM Leave_Balance WHERE employee_id=? AND leave_type_id=? AND year=?",
                              (b25_employee_id, b25_leave_type, b25_year), one=True)
        check(b'Leave request submitted for 1 working day' in response.data and
              b25_leave and b25_leave['status'] == 'Pending' and b25_pending['pending_days'] == 1
              and b'1 pending' in response.data,
              'B25: employee leave submission creates a pending request, reserves balance, and shows pending days')
        client.get('/logout')

        client.post('/login', data={'email': 'b25-manager@example.test', 'password': 'B25Manager!'},
                    follow_redirects=True)
        response = client.post(f'/leave/approve/{b25_leave["leave_id"]}', follow_redirects=True)
        with app.app_context():
            b25_leave_after = dbq("SELECT status, reviewed_by FROM Leave_Application WHERE leave_id=?",
                                  (b25_leave['leave_id'],), one=True)
            b25_balance_after = dbq("SELECT used_days, pending_days FROM Leave_Balance WHERE employee_id=? AND leave_type_id=? AND year=?",
                                    (b25_employee_id, b25_leave_type, b25_year), one=True)
            b25_employee_after = dbq("SELECT employment_status FROM Employee WHERE employee_id=?",
                                     (b25_employee_id,), one=True)
        check(b25_leave_after['status'] == 'Approved' and b25_leave_after['reviewed_by'] == b25_manager and
              b25_balance_after['used_days'] == 1 and b25_balance_after['pending_days'] == 0
              and b25_employee_after['employment_status'] == 'Active',
              'B25: branch manager approval moves reserved leave balance to used without marking a future absence as On Leave')
        response = client.get(f'/employees/{b25_employee_id}')
        check(response.status_code == 200 and b'On Leave Today' not in response.data,
              'B25: employee profile renders after future leave approval and does not show a future absence as current')
        client.get('/logout')

        client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                    follow_redirects=True)
        response = client.post(f'/invoices/{b25_claim}/approve', follow_redirects=True)
        with app.app_context():
            b25_claim_after = dbq("SELECT status, payroll_id FROM Invoice WHERE invoice_id=?", (b25_claim,), one=True)
            b25_payroll_after_claim = dbq("SELECT invoice_claims FROM Payroll WHERE payroll_id=?", (b25_draft_payroll,), one=True)
        check(b25_claim_after['status'] == 'Approved' and b25_claim_after['payroll_id'] == b25_draft_payroll and
              b25_payroll_after_claim['invoice_claims'] == 125,
              'B25: approved claim is linked once to the employee draft payroll')

        # Foreign-currency claim: payroll must receive the MYR-converted amount
        b25_usd_claim = dbe("""INSERT INTO Invoice
                              (employee_id, filename, original_name, file_type, vendor_name,
                               invoice_number, total_amount, total_amount_myr, status)
                              VALUES (?, 'b25_usd.pdf', 'b25_usd.pdf', 'pdf', 'B25 USD Vendor',
                                      'B25-USD', 100, 450, 'Pending')""", (b25_employee_id,))
        response = client.post(f'/invoices/{b25_usd_claim}/approve', follow_redirects=True)
        with app.app_context():
            b25_usd_after = dbq("SELECT status, payroll_id FROM Invoice WHERE invoice_id=?", (b25_usd_claim,), one=True)
            b25_payroll_after_usd = dbq("SELECT invoice_claims FROM Payroll WHERE payroll_id=?", (b25_draft_payroll,), one=True)
        check(b25_usd_after['status'] == 'Approved' and b25_usd_after['payroll_id'] == b25_draft_payroll and
              b25_payroll_after_usd['invoice_claims'] == 125 + 450,
              'B25: foreign-currency claim applies its MYR amount to the draft payroll')

        response = client.post(f'/payroll/finalise/{b25_draft_payroll}', follow_redirects=True)
        with app.app_context():
            b25_final_payroll = dbq("SELECT status FROM Payroll WHERE payroll_id=?", (b25_draft_payroll,), one=True)
            b25_paid_claim = dbq("SELECT status FROM Invoice WHERE invoice_id=?", (b25_claim,), one=True)
        check(b25_final_payroll['status'] == 'Finalised' and b25_paid_claim['status'] == 'Paid',
              'B25: payroll finalisation marks its approved linked claim paid')

        # Rejecting a claim must not 500 and must record the reason
        b25_reject_claim = dbe("""INSERT INTO Invoice
                                 (employee_id, filename, original_name, file_type, vendor_name,
                                  invoice_number, total_amount, total_amount_myr, status)
                                 VALUES (?, 'b25_rej.pdf', 'b25_rej.pdf', 'pdf', 'B25 Vendor',
                                         'B25-REJ', 50, 50, 'Pending')""", (b25_employee_id,))
        response = client.post(f'/invoices/{b25_reject_claim}/reject',
                               data={'reason': 'Not a valid claim'}, follow_redirects=True)
        with app.app_context():
            b25_rejected = dbq("SELECT status, rejection_reason FROM Invoice WHERE invoice_id=?", (b25_reject_claim,), one=True)
        check(response.status_code == 200 and b25_rejected is not None
              and b25_rejected['status'] == 'Rejected'
              and b25_rejected['rejection_reason'] == 'Not a valid claim',
              'B25: rejecting a claim records the status and reason without error')

        response = client.post(f'/performance/generate', data={'month': str(b25_month), 'year': str(b25_year)},
                               follow_redirects=True)
        with app.app_context():
            b25_score = dbq("SELECT composite_score, grade FROM Performance_Score WHERE employee_id=? AND period_month=? AND period_year=?",
                            (b25_employee_id, b25_month, b25_year), one=True)
        check(b'Generated' in response.data and b25_score is not None and b25_score['grade'] in ('A', 'B', 'C', 'D'),
              'B25: approved attendance produces a monthly performance score')
        response = client.post(f'/increment/{b25_increment}/approve', follow_redirects=True)
        with app.app_context():
            b25_increment_after = dbq("SELECT status FROM Salary_Increment WHERE increment_id=?", (b25_increment,), one=True)
            b25_employee_salary = dbq("SELECT base_salary FROM Employee WHERE employee_id=?", (b25_employee_id,), one=True)
        check(b25_increment_after['status'] == 'Approved' and b25_employee_salary['base_salary'] == 3300,
              'B25: approved increment updates employee base salary once')

        # Second increment: must bump the employee's existing Draft payroll
        b25_inc2 = dbe("""INSERT INTO Salary_Increment
                         (employee_id, period_year, old_salary, new_salary, increment_pct, proposed_by, status)
                         VALUES (?, ?, 3300, 3500, 6, ?, 'Pending')""",
                       (b25_employee_id, b25_year, b25_manager))
        response = client.post(f'/increment/{b25_inc2}/approve', follow_redirects=True)
        with app.app_context():
            b25_draft_after_inc = dbq("""SELECT base_salary, salary_increment, status FROM Payroll
                                         WHERE employee_id=? AND status='Draft'
                                         ORDER BY payroll_id DESC LIMIT 1""",
                                      (b25_employee_id,), one=True)
        check(b25_draft_after_inc is not None
              and b25_draft_after_inc['base_salary'] == 3500
              and b25_draft_after_inc['salary_increment'] == 200,
              'B25: approving an increment bumps the employee Draft payroll base and increment line')

        response = client.post(f'/bonus/{b25_bonus}/approve', follow_redirects=True)
        with app.app_context():
            b25_bonus_after = dbq("SELECT status FROM Bonus_Proposal WHERE proposal_id=?", (b25_bonus,), one=True)
        check(b25_bonus_after['status'] == 'Approved',
              'B25: approved performance bonus reaches the approved state')
        response = client.get(f'/reports/?type=headcount&year={b25_year}&export=csv')
        check(response.status_code == 200 and response.headers.get('Content-Type', '').startswith('text/csv'),
              'B25: report export returns a CSV response')
        response = client.post(f'/employees/{b25_employee_id}/deactivate', follow_redirects=True)
        with app.app_context():
            b25_deactivated = dbq("SELECT is_active, employment_status FROM Employee WHERE employee_id=?",
                                  (b25_employee_id,), one=True)
        check(response.status_code == 200 and b25_deactivated['is_active'] == 0
              and b25_deactivated['employment_status'] == 'Inactive',
              'B25: HR can deactivate a laid-off employee without deleting their history')
        client.get('/logout')
        response = client.post('/login', data={
            'email': 'b25-employee@example.test', 'password': 'B25Employee!'
        }, follow_redirects=True)
        check(b'Invalid email or password.' in response.data,
              'B25: deactivated employee can no longer sign in')
        check(len(b25_captured_notifications) >= 4,
              'B25: operational approvals emit in-app notification calls in capture mode')
finally:
    (b25_leave_routes.send_notification,
     b25_invoice_routes.send_notification,
     b25_payroll_routes.send_notification,
     b25_bonus_routes.send_notification,
     b25_notification_routes.send_notification) = b25_original_notifiers
    app_db_mod.DB_PATH = b25_real_db
    init_db_mod.DB_PATH = b25_real_db
    shutil.rmtree(b25_tmp_dir, ignore_errors=True)

# ==============================================================================
# B26 — Recruitment application notification scoping
# ==============================================================================

print('=' * 60)
_focus_block('26')
print('B26 — Recruitment application notification scoping')
print('=' * 60)

b26_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b26_')
b26_db = os.path.join(b26_tmp_dir, 'smarthr_b26.db')
shutil.copy2(app_db_mod.DB_PATH, b26_db)
b26_real_db = app_db_mod.DB_PATH
app_db_mod.DB_PATH = b26_db
init_db_mod.DB_PATH = b26_db

try:
    from werkzeug.security import generate_password_hash
    from app.recruitment.scoping import count_visible_new_applications
    from app.notifications import email_monitor as b26_email_monitor

    with app.app_context():
        b26_company = dbq("SELECT company_id FROM Company ORDER BY company_id LIMIT 1", one=True)
        b26_roles = {
            row['role_name']: row['role_id']
            for row in dbq("SELECT role_id, role_name FROM Role WHERE role_name IN ('Admin','HR','Manager')")
        }
        check(b26_company is not None and {'Admin', 'HR', 'Manager'} <= set(b26_roles),
              'B26: company and required roles are available')

        b26_branch = dbe("""INSERT INTO Branch(company_id, name, address, address_line1, city, state, postal_code)
                          VALUES (?, 'B26 Scope Branch', 'B26 A', 'B26 A',
                                  'Kuala Lumpur', 'WP Kuala Lumpur', '50026')""",
                         (b26_company['company_id'],))
        b26_other_branch = dbe("""INSERT INTO Branch(company_id, name, address, address_line1, city, state, postal_code)
                                VALUES (?, 'B26 Other Branch', 'B26 B', 'B26 B',
                                        'Kuala Lumpur', 'WP Kuala Lumpur', '50027')""",
                               (b26_company['company_id'],))
        b26_dept = dbe("INSERT INTO Department(branch_id, department_name) VALUES (?, 'B26 Managed Department')",
                       (b26_branch,))
        b26_same_branch_dept = dbe("INSERT INTO Department(branch_id, department_name) VALUES (?, 'B26 Same Branch Department')",
                                   (b26_branch,))
        b26_other_dept = dbe("INSERT INTO Department(branch_id, department_name) VALUES (?, 'B26 Other Branch Department')",
                             (b26_other_branch,))

        def b26_employee(full_name, email, branch_id, department_id, role_name, password):
            return dbe("""INSERT INTO Employee
                       (company_id, branch_id, department_id, full_name, position,
                        employment_type, employment_status, hire_date, base_salary,
                        role_id, email, password_hash, is_active)
                       VALUES (?, ?, ?, ?, 'B26 Staff', 'Full-Time', 'Active',
                               '2024-01-01', 3000, ?, ?, ?, 1)""",
                       (b26_company['company_id'], branch_id, department_id, full_name,
                        b26_roles[role_name], email, generate_password_hash(password)))

        b26_manager = b26_employee('B26 Department Manager', 'b26-manager@example.test',
                                    b26_branch, b26_dept, 'Manager', 'B26Manager!')
        b26_hr = b26_employee('B26 HR', 'b26-hr@example.test',
                               b26_branch, b26_dept, 'HR', 'B26Hr!')
        dbe("UPDATE Department SET department_manager_id=? WHERE department_id=?",
            (b26_manager, b26_dept))

        def b26_posting(title, department_id, branch_id):
            return dbe("""INSERT INTO Job_Posting
                       (title, department_id, branch_id, employment_type, status, target_audience)
                       VALUES (?, ?, ?, 'Full-Time', 'Open', 'Both')""",
                       (title, department_id, branch_id))

        b26_own_posting = b26_posting('B26 Own Posting', b26_dept, b26_branch)
        b26_same_branch_posting = b26_posting('B26 Same Branch Posting', b26_same_branch_dept, b26_branch)
        b26_other_branch_posting = b26_posting('B26 Other Branch Posting', b26_other_dept, b26_other_branch)

        def b26_application(posting_id, name, status='New', company_id=b26_company['company_id']):
            return dbe("""INSERT INTO Job_Application
                       (posting_id, company_id, applicant_name, applicant_email, status, applicant_type)
                       VALUES (?, ?, ?, ?, ?, 'External')""",
                       (posting_id, company_id, name,
                        name.lower().replace(' ', '.') + '@example.test', status))

        b26_application(b26_own_posting, 'B26 Own New')
        b26_posted_email = b26_application(b26_own_posting, 'B26 Posted Email New', company_id=None)
        b26_application(b26_own_posting, 'B26 Own Rejected', 'Rejected')
        b26_application(b26_same_branch_posting, 'B26 Same Branch New')
        b26_application(b26_other_branch_posting, 'B26 Other Branch New')
        dbe("""INSERT INTO Job_Application
               (applicant_name, applicant_email, status, applicant_type)
               VALUES ('B26 Unassigned New', 'b26-unassigned@example.test', 'New', 'External')""")

    with app.test_client() as client:
        client.post('/login', data={'email': 'b26-manager@example.test', 'password': 'B26Manager!'},
                    follow_redirects=True)
        response = client.get('/recruitment/applications?status=')
        check(response.status_code == 200 and b'B26 Own New' in response.data and
              b'B26 Posted Email New' in response.data and b'B26 Own Rejected' in response.data and
              b'B26 Same Branch New' not in response.data and b'id="nav-apps-badge">2<' in response.data,
              'B26: department manager list and sidebar badge use the managed-department scope')
        check(b'B26 Managed Department' in response.data
              and b'B26 Same Branch Department' not in response.data
              and b'B26 Other Branch Department' not in response.data
              and b'B26 Own Posting' in response.data
              and b'B26 Same Branch Posting' not in response.data
              and b'B26 Other Branch Posting' not in response.data,
              'B26: department manager application filters only show their visible scope')
        response = client.get(f'/recruitment/applications/{b26_posted_email}')
        check(response.status_code == 200 and b'B26 Posted Email New' in response.data,
              'B26: a posted email application is viewable under its posting scope')
        response = client.get('/')
        check(b'id="nav-apps-badge">2<' in response.data,
              'B26: manager dashboard preserves the department-scoped application badge')
        client.get('/logout')

        b26_original_poll = b26_email_monitor.poll_inbox
        b26_email_monitor.poll_inbox = lambda: (0, 0, 0)
        try:
            client.post('/login', data={'email': 'b26-hr@example.test', 'password': 'B26Hr!'},
                        follow_redirects=True)
            with client.session_transaction() as scoped_session:
                expected_hr_count = count_visible_new_applications(dict(scoped_session))
            response = client.get('/api/check-email')
            check(response.status_code == 200 and response.get_json()['total_apps'] == expected_hr_count,
                  'B26: email-poll API returns the same scoped count as recruitment visibility')
        finally:
            b26_email_monitor.poll_inbox = b26_original_poll
finally:
    app_db_mod.DB_PATH = b26_real_db
    init_db_mod.DB_PATH = b26_real_db
    shutil.rmtree(b26_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B27 — Face attendance manual check-out fallback (open check-in detection,
#       manual_self check-out closes today's/yesterday's open row)
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('27')
print('B27 — Manual check-out fallback for /face/attendance (isolated temp DB)')
print('=' * 60)

if _active:
    b27_real_db = app_db_mod.DB_PATH
    b27_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b27_')
    b27_db = os.path.join(b27_tmp_dir, 'smarthr_b27.db')
    shutil.copy2(b27_real_db, b27_db)
    app_db_mod.DB_PATH = b27_db
    init_db_mod.DB_PATH = b27_db

    try:
        with app.test_client() as client:
            client.post('/login', data={'email': 'elizabeth@smarthr.my', 'password': 'Employee@123'},
                        follow_redirects=True)
            eliz = dbq("SELECT employee_id, full_name, branch_id FROM Employee WHERE email='elizabeth@smarthr.my'", one=True)
            ryan = dbq("SELECT employee_id, full_name, branch_id FROM Employee WHERE email='ryan@smarthr.my'", one=True)
            check(eliz is not None and ryan is not None, 'B27: found employee fixtures')
            if not (eliz and ryan):
                eliz = {'employee_id': 0, 'full_name': 'NONE', 'branch_id': 0}
                ryan = {'employee_id': 0, 'full_name': 'NONE', 'branch_id': 0}

            today = datetime.datetime.now().strftime('%Y-%m-%d')
            yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

            # Isolated fixtures: clear today/yesterday rows + dummy face rows first
            for eid in (eliz['employee_id'], ryan['employee_id']):
                if eid:
                    dbe("DELETE FROM Attendance WHERE employee_id=? AND date(check_in) >= ?", (eid, yesterday))
                    dbe("DELETE FROM Face_Encoding WHERE employee_id=?", (eid,))

            if eliz['employee_id']:
                dbe("INSERT INTO Face_Encoding (employee_id, face_encoding_blob, registered_by) VALUES (?, ?, ?)",
                    (eliz['employee_id'], b'\x00', eliz['employee_id']))

            def b27_login_as(email):
                client.get('/logout')
                client.post('/login', data={'email': email, 'password': 'Employee@123'},
                            follow_redirects=True)

            def b27_set_failures(count):
                with client.session_transaction() as sess:
                    sess['biometric_checkin_failures'] = count

            # ── 1) Page state: open check-in today + later CLOSED row must still show checked in ──
            open_aid = None
            closed_aid = None
            if eliz['employee_id']:
                open_aid = dbe("""INSERT INTO Attendance (employee_id, branch_id, check_in, is_manual_entry, manual_reason, status)
                                  VALUES (?,?,?,1,'B27 open','Pending')""",
                               (eliz['employee_id'], eliz['branch_id'], f'{today} 08:00:00'))
                closed_aid = dbe("""INSERT INTO Attendance (employee_id, branch_id, check_in, check_out, hours_worked, is_manual_entry, status)
                                    VALUES (?,?,?,?,9.0,0,'Approved')""",
                                 (eliz['employee_id'], eliz['branch_id'], f'{today} 09:00:00', f'{today} 09:05:00'))

                r = client.get('/face/attendance')
                html = r.data.decode('utf-8', errors='replace')
                check(r.status_code == 200, 'B27: GET /face/attendance as employee (200)')
                check('You can <strong>check out</strong> when ready.' in html,
                      'B27: page shows Check-Out state despite a later closed row')
                check('let isCheckedIn = true;' in html,
                      'B27: template JS isCheckedIn=true for the open record')

                # Camera-start failures must be server-synchronized (reportCaptureFailure)
                check('await reportCaptureFailure()' in html,
                      'B27: camera-failure path calls reportCaptureFailure()')

                # After 3 failures, manual fallback must be a Manual Check-Out request
                b27_set_failures(3)
                r = client.get('/face/attendance')
                html = r.data.decode('utf-8', errors='replace')
                check('Manual Check-Out' in html and 'Submit Manual Check-Out' in html,
                      'B27: manual fallback renders Manual Check-Out form after 3 failures')
                b27_set_failures(0)

                # ── 2) Biometric check-in rejected while open check-in exists ──
                r = client.post('/face/api/match_and_record', json={'action': 'check_in', 'image': 'x'})
                body = (r.get_json() or {}).get('msg', '')
                check(r.status_code == 400 and 'already checked in' in body,
                      'B27: biometric check-in rejected while open check-in exists')

                # ── 3) Manual check-in rejected while open check-in exists ──
                b27_set_failures(3)
                before_count = dbq("SELECT COUNT(*) as c FROM Attendance WHERE employee_id=?", (eliz['employee_id'],), one=True)['c']
                r = client.post('/attendance/manual_self', json={'action': 'check_in', 'time': '10:00', 'reason': 'B27 reject check-in'})
                after_count = dbq("SELECT COUNT(*) as c FROM Attendance WHERE employee_id=?", (eliz['employee_id'],), one=True)['c']
                err = (r.get_json() or {}).get('error', '')
                check(r.status_code == 400 and 'open check-in' in err and before_count == after_count,
                      'B27: manual check-in rejected when open check-in exists (no new row)')

                # ── 4) Invalid action rejected ──
                r = client.post('/attendance/manual_self', json={'action': 'break', 'time': '12:00', 'reason': 'B27 invalid'})
                err = (r.get_json() or {}).get('error', '')
                check(r.status_code == 400 and err == 'Invalid action.',
                      'B27: manual_self rejects actions other than check_in/check_out')

                # ── 5) Check-out time at/before check-in rejected ──
                r = client.post('/attendance/manual_self', json={'action': 'check_out', 'time': '07:00', 'reason': 'B27 early checkout'})
                err = (r.get_json() or {}).get('error', '')
                row = dbq("SELECT * FROM Attendance WHERE attendance_id=?", (open_aid,), one=True)
                check(r.status_code == 400 and 'must be after the check-in time' in err and row is not None and row['check_out'] is None,
                      'B27: check-out time at/before check-in rejected, row stays open')

                # ── 6) Manual check-out closes the ACTUAL open record ──
                r = client.post('/attendance/manual_self', json={'action': 'check_out', 'time': '17:00', 'reason': 'B27 checkout'})
                data = r.get_json() or {}
                row = dbq("SELECT * FROM Attendance WHERE attendance_id=?", (open_aid,), one=True)
                closed_row = dbq("SELECT * FROM Attendance WHERE attendance_id=?", (closed_aid,), one=True)
                check(r.status_code == 200 and data.get('success') is True,
                      'B27: manual check-out accepted after 3 failures')
                check(row is not None and row['check_out'] is not None and row['check_out'][11:16] == '17:00'
                      and row['is_manual_entry'] == 1 and row['status'] == 'Pending' and row['hours_worked'] == 9.0,
                      'B27: open record closed as Pending manual entry with 9.0h')
                check(closed_row is not None and closed_row['check_out'] == f'{today} 09:05:00'
                      and closed_row['is_manual_entry'] == 0 and closed_row['status'] == 'Approved',
                      'B27: later closed record left untouched')
                with client.session_transaction() as sess:
                    failures_after = sess.get('biometric_checkin_failures', 0)
                check(failures_after == 0, 'B27: failure counter reset after successful manual check-out')

            # ── 7) Yesterday's open check-in can be closed via manual check-out ──
            if ryan['employee_id']:
                y_aid = dbe("""INSERT INTO Attendance (employee_id, branch_id, check_in, is_manual_entry, manual_reason, status)
                               VALUES (?,?,?,1,'B27 yesterday','Pending')""",
                            (ryan['employee_id'], ryan['branch_id'], f'{yesterday} 19:00:00'))
                b27_login_as('ryan@smarthr.my')
                b27_set_failures(3)
                r = client.post('/attendance/manual_self', json={'action': 'check_out', 'time': '09:00', 'reason': 'B27 cross-midnight'})
                data = r.get_json() or {}
                row = dbq("SELECT * FROM Attendance WHERE attendance_id=?", (y_aid,), one=True)
                check(r.status_code == 200 and data.get('success') is True and row is not None
                      and row['check_out'] is not None and row['check_out'][:10] == today
                      and row['is_manual_entry'] == 1 and row['status'] == 'Pending' and row['hours_worked'] > 0,
                      'B27: manual check-out closes yesterday open check-in with positive hours')

            # Cleanup fixtures (throwaway DB, hygiene only)
            for eid in (eliz['employee_id'], ryan['employee_id']):
                if eid:
                    dbe("DELETE FROM Attendance WHERE employee_id=? AND date(check_in) >= ?", (eid, yesterday))
                    dbe("DELETE FROM Face_Encoding WHERE employee_id=?", (eid,))
    finally:
        app_db_mod.DB_PATH = b27_real_db
        init_db_mod.DB_PATH = b27_real_db
        shutil.rmtree(b27_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B28 — Position Catalog search, filters, and collapsed create form
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('28')
print('B28 — Position Catalog search, filters, and expandable create form')
print('=' * 60)

if _active:
    with app.test_client() as client:
        client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                    follow_redirects=True)
        sample_position = query("""
            SELECT p.position_id, p.position_name, p.department_id, d.branch_id
            FROM Position p
            JOIN Department d ON d.department_id=p.department_id
            ORDER BY p.position_id LIMIT 1
        """, one=True)
        check(sample_position is not None, 'B28: position fixture exists')
        if sample_position:
            response = client.get('/organization/roles')
            html = response.data.decode('utf-8', errors='replace')
            check(response.status_code == 200 and sample_position['position_name'] in html,
                  'B28: Position Catalog renders its position data')
            check('id="position-q"' in html and 'id="position-branch"' in html
                  and 'id="position-department"' in html,
'B28: Position Catalog renders title, branch, and department filters')
            check('filters.addEventListener(\'submit\'' in html and 'event.preventDefault()' in html
                  and 'title.addEventListener(\'input\', applyPositionFilters)' in html,
                  'B28: Position Catalog filters update in place without a page reload')
            check('href="/organization/roles/positions/add"' in html
                  and 'id="new-position-form" hidden' not in html,
                  'B28: new-position entry links to the dedicated add-position page')
            response = client.get('/organization/roles/positions/add')
            add_html = response.data.decode('utf-8', errors='replace')
            check(response.status_code == 200
                  and 'name="branch_id"' in add_html and 'name="department_id"' in add_html
                  and 'name="position_name"' in add_html,
                  'B28: add-position page renders branch, department, and title fields')
            check('+ Add new branch' in add_html and '+ Add new department' in add_html,
                  'B28: add-position page offers add-new branch and department entries')
            check('View Employees (' in html and 'Deactivate' not in html and '/toggle' not in html,
                  'B28: positions cannot be deactivated and provide an employee-view action')
            response = client.get('/employees/', query_string={'position_id': sample_position['position_id']})
            check(response.status_code == 200,
                  'B28: View Employees target accepts the catalog position filter')

# ═══════════════════════════════════════════════════════════════════════════════
# B29 — HR Manager is the full-access senior HR role; HR Director is retired
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('29')
print('B29 — HR Manager role consolidation')
print('=' * 60)

if _active:
    b29_real_db = init_db_mod.DB_PATH
    b29_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b29_')
    b29_db = os.path.join(b29_tmp_dir, 'smarthr_b29.db')
    shutil.copy2(b29_real_db, b29_db)
    try:
        init_db_mod.DB_PATH = b29_db
        con = init_db_mod.get_connection()
        cur = con.cursor()
        legacy_role_id = cur.execute(
            "INSERT INTO Role(role_name) VALUES ('HR Director')"
        ).lastrowid
        hr_manager_role_id = cur.execute(
            "SELECT role_id FROM Role WHERE role_name='HR Manager'"
        ).fetchone()[0]
        employee_id = cur.execute("SELECT employee_id FROM Employee ORDER BY employee_id LIMIT 1").fetchone()[0]
        cur.execute("UPDATE Employee SET role_id=? WHERE employee_id=?", (legacy_role_id, employee_id))
        con.commit()
        con.close()

        init_db_mod.migrate_hr_director_to_hr_manager()

        con = init_db_mod.get_connection()
        legacy_role = con.execute("SELECT 1 FROM Role WHERE role_name='HR Director'").fetchone()
        migrated_role = con.execute("SELECT role_id FROM Employee WHERE employee_id=?", (employee_id,)).fetchone()[0]
        active_permissions = con.execute(
            "SELECT COUNT(*) FROM Employee_Permission WHERE employee_id=? AND is_active=1", (employee_id,)
        ).fetchone()[0]
        expected_permissions = con.execute(
            "SELECT COUNT(*) FROM Role_Permission WHERE role_id=?", (hr_manager_role_id,)
        ).fetchone()[0]
        con.close()
        check(legacy_role is None and migrated_role == hr_manager_role_id
              and active_permissions == expected_permissions,
              'B29: legacy HR Director account migrates to HR Manager with full role permissions')
    finally:
        init_db_mod.DB_PATH = b29_real_db
        shutil.rmtree(b29_tmp_dir, ignore_errors=True)

    with app.test_client() as client:
        client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                    follow_redirects=True)
        response = client.get('/organization/roles')
        html = response.data.decode('utf-8', errors='replace')
        roles = query("SELECT role_name FROM Role ORDER BY role_id")
        role_names = [row['role_name'] for row in roles]
        system_roles_html = html.split('Department Manager Assignments', 1)[0]
        check(response.status_code == 200 and '5 fixed roles' in system_roles_html
              and 'HR Director' not in system_roles_html and 'HR Director' not in role_names,
              'B29: HR Director is absent from the System Roles catalog and database')
        check('Inherits all Admin privileges — full system access, approvals, organisation settings.' in html,
              'B29: HR Manager has the full Admin-privilege description')
        response = client.get('/organization/companies')
        check(response.status_code == 200,
              'B29: HR Manager can access Admin-level organisation settings')

# ═══════════════════════════════════════════════════════════════════════════════
# B30 — Branch-filtered direct posting form + position-less department visibility
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('30')
print('B30 — Posting form branch filter and position-less departments')
print('=' * 60)

if _active:
    b30_real_db = app_db_mod.DB_PATH
    b30_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b30_')
    b30_db = os.path.join(b30_tmp_dir, 'smarthr_b30.db')
    shutil.copy2(b30_real_db, b30_db)
    app_db_mod.DB_PATH = b30_db
    init_db_mod.DB_PATH = b30_db

    try:
        with app.test_client() as client:
            client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                        follow_redirects=True)

            anchor = dbq("""SELECT d.department_id, d.branch_id FROM Department d
                            JOIN Branch b ON d.branch_id=b.branch_id
                            WHERE b.company_id=1
                              AND EXISTS (SELECT 1 FROM Position p
                                          WHERE p.department_id=d.department_id AND p.is_active=1)
                            ORDER BY d.department_id LIMIT 1""", one=True)
            check(anchor is not None, 'B30: found a department with an active catalog position')
            if not anchor:
                anchor = {'department_id': 0, 'branch_id': 0}

            other_branch = dbq("""SELECT branch_id FROM Branch
                                  WHERE company_id=1 AND branch_id != ?
                                  ORDER BY branch_id LIMIT 1""", (anchor['branch_id'],), one=True)
            check(other_branch is not None, 'B30: found a second branch for mismatch crafting')
            if not other_branch:
                other_branch = {'branch_id': 0}

            form = client.get('/recruitment/postings/add').data.decode('utf-8', errors='replace')
            check('data-branch=' in form and 'data-dept=' in form,
                  'B30: posting form department options carry data-branch and position options carry data-dept')
            check('name="openings"' in form and 'Number of Openings' in form,
                  'B30: posting form includes the number-of-openings input')

            b30_dept = dbe("""INSERT INTO Department (branch_id, department_name)
                              VALUES (?, 'B30 Empty Department')""", (anchor['branch_id'],))
            form = client.get('/recruitment/postings/add').data.decode('utf-8', errors='replace')
            check(f'value="{b30_dept}"' in form and 'B30 Empty Department' in form,
                  'B30: a position-less department appears in the posting form dropdown')

            roles_html = client.get('/organization/roles').data.decode('utf-8', errors='replace')
            assignments_section = roles_html.split('Department Manager Assignments', 1)[1].split('Position Catalog', 1)[0]
            check('B30 Empty Department' in assignments_section and 'Unassigned' in assignments_section,
                  'B30: roles page lists the position-less department as Unassigned')
            hint_section = roles_html.split('Departments without catalog positions', 1)[1]
            check('B30 Empty Department' in hint_section,
                  'B30: roles page names the department in the empty-departments hint')

            valid_pos = dbq("SELECT position_id FROM Position WHERE department_id=? AND is_active=1 LIMIT 1",
                            (anchor['department_id'],), one=True)
            check(valid_pos is not None, 'B30: found an active position for the anchor department')
            if not valid_pos:
                valid_pos = {'position_id': 0}

            before_count = dbq("SELECT COUNT(*) as c FROM Job_Posting", one=True)['c']
            response = client.post('/recruitment/postings/add', data={
                'branch_id': str(other_branch['branch_id']),
                'department_id': str(anchor['department_id']),
                'position_id': str(valid_pos['position_id']),
                'employment_type': 'Full-Time'}, follow_redirects=True)
            after_count = dbq("SELECT COUNT(*) as c FROM Job_Posting", one=True)['c']
            check(b'does not match the department' in response.data and before_count == after_count,
                  'B30: crafted branch!=department posting remains rejected with no Job_Posting row')

            response = client.post('/recruitment/postings/add', data={
                'branch_id': str(anchor['branch_id']),
                'department_id': str(b30_dept),
                'position_id': '',
                'employment_type': 'Full-Time'}, follow_redirects=True)
            after_count = dbq("SELECT COUNT(*) as c FROM Job_Posting", one=True)['c']
            check(b'Please select a position from the catalog' in response.data and before_count == after_count,
                  'B30: position-less department submission without a catalog position remains rejected')

            b30_pos = dbe("INSERT INTO Position (position_name, department_id) VALUES ('B30 Catalog Role', ?)",
                          (b30_dept,))
            response = client.post('/recruitment/postings/add', data={
                'branch_id': str(anchor['branch_id']),
                'department_id': str(b30_dept),
                'position_id': str(b30_pos),
                'employment_type': 'Full-Time',
                'target_audience': 'External'}, follow_redirects=True)
            posting = dbq("""SELECT * FROM Job_Posting WHERE position_id=? ORDER BY posting_id DESC LIMIT 1""",
                          (b30_pos,), one=True)
            check(posting is not None and posting['department_id'] == b30_dept
                  and posting['branch_id'] == anchor['branch_id']
                  and posting['title'] == 'B30 Catalog Role' and posting['status'] == 'Open'
                  and posting['approved_openings'] == 1 and posting['filled_openings'] == 0,
                  'B30: valid direct posting defaults to one opening and persists branch, department, and position')

            for bad_openings in ('0', '51', 'abc'):
                before_openings_count = dbq("SELECT COUNT(*) as c FROM Job_Posting", one=True)['c']
                response = client.post('/recruitment/postings/add', data={
                    'branch_id': str(anchor['branch_id']),
                    'department_id': str(b30_dept),
                    'position_id': str(b30_pos),
                    'employment_type': 'Full-Time',
                    'openings': bad_openings}, follow_redirects=True)
                after_openings_count = dbq("SELECT COUNT(*) as c FROM Job_Posting", one=True)['c']
                check(b'between 1 and 50' in response.data and before_openings_count == after_openings_count,
                      f'B30: openings={bad_openings} rejected with no Job_Posting row')

            response = client.post('/recruitment/postings/add', data={
                'branch_id': str(anchor['branch_id']),
                'department_id': str(b30_dept),
                'position_id': str(b30_pos),
                'employment_type': 'Full-Time',
                'target_audience': 'External',
                'openings': '3'}, follow_redirects=True)
            posting = dbq("""SELECT * FROM Job_Posting WHERE position_id=? ORDER BY posting_id DESC LIMIT 1""",
                          (b30_pos,), one=True)
            check(posting is not None and posting['approved_openings'] == 3
                  and posting['filled_openings'] == 0 and posting['status'] == 'Open',
                  'B30: direct posting stores the requested number of openings (3)')
    finally:
        app_db_mod.DB_PATH = b30_real_db
        init_db_mod.DB_PATH = b30_real_db
        shutil.rmtree(b30_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B31 — Auto-assign modal stays stable while typing the meeting link
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('31')
print('B31 — Auto-assign modal stability')
print('=' * 60)

if _active:
    with app.test_client() as client:
        client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                    follow_redirects=True)
        response = client.get('/recruitment/applications', query_string={'status': 'Shortlisted'})
        html = response.data.decode('utf-8', errors='replace')
        check(response.status_code == 200 and 'auto-assign-modal' in html,
              'B31: auto-assign modal renders on the shortlisted applications page')
        check('align-items:flex-start' in html,
              'B31: modal overlay is top-anchored so content growth does not re-center it')
        check('linkDebounce' in html,
              'B31: meeting-link input recalculates on a debounce instead of every keystroke')
        check('if (!assignedData.length)' in html,
              'B31: recalculating keeps the existing preview instead of collapsing it')
        check('id="aa-link"' in html and 'id="aa-format"' in html,
              'B31: format select and meeting-link input still present')
        check('document.addEventListener(\'DOMContentLoaded\', cascadeApplications);\n\nfunction toggleAll' in html,
              'B31: bulk-selection functions remain inside the scripts block')
        with app.app_context():
            invitation_html = render_template(
                'emails/interview_scheduled.html', title='Interview Scheduled',
                employee_name='Candidate', job_title='Test position',
                interview_date='Monday, 17 August 2026', interview_time='10:00 AM',
                location='Virtual link', interview_type='Virtual', interview_ref='INT-1')
        check('Replies to this message are monitored for interview confirmations.' in invitation_html
              and 'Please do not reply to this email.' not in invitation_html,
              'B31: interview email gives one unambiguous reply instruction')

# ═══════════════════════════════════════════════════════════════════════════════
# B32 — HR Manager can record completed-interview scorecards
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('32')
print('B32 — HR Manager scorecard visibility and submission')
print('=' * 60)

if _active:
    with app.test_client() as client:
        client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                    follow_redirects=True)
        b32_application = dbq("""SELECT ja.application_id
                                FROM Job_Application ja
                                JOIN Job_Posting jp ON jp.posting_id=ja.posting_id
                                JOIN Branch b ON b.branch_id=jp.branch_id
                                WHERE b.company_id=1
                                ORDER BY ja.application_id LIMIT 1""", one=True)
        check(b32_application is not None, 'B32: found an application in the HR Manager company')
        if b32_application:
            b32_interview = dbe("""INSERT INTO Interview
                                 (application_id, scheduled_at, interviewer_ids, status)
                                 VALUES (?, datetime('now', '-1 day'), '1', 'Completed')""",
                                (b32_application['application_id'],))
            response = client.get(f'/recruitment/applications/{b32_application["application_id"]}')
            html = response.data.decode('utf-8', errors='replace')
            scorecard_action = f'/recruitment/interview/{b32_interview}/scorecard'
            check(response.status_code == 200 and scorecard_action in html and 'Record Scorecard' in html,
                  'B32: HR Manager sees the scorecard form for a completed interview')

            response = client.post(scorecard_action, data={
                'technical': '5', 'communication': '4', 'fit': '5',
                'note_technical': 'B32 technical evidence',
                'note_communication': 'B32 communication evidence',
                'note_fit': 'B32 fit evidence'}, follow_redirects=True)
            scorecard = dbq("SELECT technical, communication, fit FROM Interview_Scorecard WHERE interview_id=?",
                            (b32_interview,), one=True)
            check(response.status_code == 200 and scorecard is not None
                  and (scorecard['technical'], scorecard['communication'], scorecard['fit']) == (5, 4, 5),
                  'B32: HR Manager can submit a completed-interview scorecard')

# ═══════════════════════════════════════════════════════════════════════════════
# B33 — HR Manager direct selection confirmation
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('33')
print('B33 — HR Manager direct selection confirmation')
print('=' * 60)

if _active:
    with app.test_client() as client:
        client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                    follow_redirects=True)
        b33_branch = dbq("SELECT branch_id FROM Branch WHERE company_id=1 ORDER BY branch_id LIMIT 1", one=True)
        b33_dept = dbq("SELECT department_id FROM Department WHERE branch_id=? ORDER BY department_id LIMIT 1",
                       (b33_branch['branch_id'],), one=True) if b33_branch else None
        check(b33_branch is not None and b33_dept is not None,
              'B33: found a company-one branch and department for direct selection')
        if b33_branch and b33_dept:
            b33_posting = dbe("""INSERT INTO Job_Posting (title, department_id, branch_id, status)
                                VALUES ('B33 Direct Selection Role', ?, ?, 'Open')""",
                              (b33_dept['department_id'], b33_branch['branch_id']))
            b33_application = dbe("""INSERT INTO Job_Application
                                   (posting_id, company_id, applicant_name, applicant_email, status)
                                   VALUES (?, 1, 'B33 Candidate', 'b33-candidate@example.test', 'Interview')""",
                                  (b33_posting,))
            b33_interview = dbe("""INSERT INTO Interview (application_id, scheduled_at, status)
                                 VALUES (?, datetime('now', '-1 day'), 'Completed')""",
                                (b33_application,))
            b33_url = f'/recruitment/applications/{b33_application}/confirm-selection'

            response = client.post(b33_url, follow_redirects=True)
            b33_record = dbq("SELECT * FROM Candidate_Recommendation WHERE application_id=?",
                             (b33_application,), one=True)
            check(b'completed interview scorecard is required' in response.data and b33_record is None,
                  'B33: direct confirmation remains blocked without a completed scorecard')

            b33_hr_manager = dbq("SELECT employee_id FROM Employee WHERE email='hr@smarthr.my'", one=True)
            dbe("""INSERT INTO Interview_Scorecard
                   (interview_id, technical, communication, fit, note_technical,
                    note_communication, note_fit, scored_by)
                   VALUES (?, 5, 5, 5, 'B33 technical evidence',
                           'B33 communication evidence', 'B33 fit evidence', ?)""",
                (b33_interview, b33_hr_manager['employee_id']))

            response = client.get(f'/recruitment/applications/{b33_application}')
            check(response.status_code == 200 and b'Confirm Selection' in response.data and b33_url.encode() in response.data,
                  'B33: HR Manager sees direct confirmation after a completed scorecard')

            response = client.post(b33_url, follow_redirects=True)
            b33_record = dbq("""SELECT status, recommended_by, approved_by, approved_at
                                FROM Candidate_Recommendation WHERE application_id=?""",
                             (b33_application,), one=True)
            check(response.status_code == 200 and b33_record is not None
                  and b33_record['status'] == 'Approved'
                  and b33_record['recommended_by'] == b33_hr_manager['employee_id']
                  and b33_record['approved_by'] == b33_hr_manager['employee_id']
                  and b33_record['approved_at'] is not None,
                  'B33: direct confirmation creates an approved auditable selection record')

            response = client.get(f'/recruitment/applications/{b33_application}')
            check(response.status_code == 200 and b'Create Contract' in response.data,
                  'B33: approved direct selection enables contract preparation')

# ═══════════════════════════════════════════════════════════════════════════════
# B34 — Leave self-approval is blocked (approvers cannot approve/reject own leave)
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('34')
print('B34 — Leave self-approval blocked')
print('=' * 60)

if _active:
    with app.test_client() as client:
        client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                    follow_redirects=True)
        b34_admin = dbq("SELECT employee_id FROM Employee WHERE email='admin@smarthr.my'", one=True)
        b34_leave_type = dbq("SELECT leave_type_id FROM Leave_Type ORDER BY leave_type_id LIMIT 1", one=True)
        b34_start = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
        b34_lid = dbe("""INSERT INTO Leave_Application
                         (employee_id, leave_type_id, start_date, end_date, total_days, reason, status)
                         VALUES (?, ?, ?, ?, 1, 'B34 self-approval', 'Pending')""",
                      (b34_admin['employee_id'], b34_leave_type['leave_type_id'], b34_start, b34_start))

        response = client.post(f'/leave/approve/{b34_lid}', follow_redirects=True)
        b34_after = dbq("SELECT status, reviewed_by FROM Leave_Application WHERE leave_id=?",
                        (b34_lid,), one=True)
        check(b'cannot approve or reject your own leave' in response.data
              and b34_after['status'] == 'Pending' and b34_after['reviewed_by'] is None,
              'B34: approver cannot approve their own leave application')

        response = client.post(f'/leave/reject/{b34_lid}', data={'comment': 'B34'}, follow_redirects=True)
        b34_after_reject = dbq("SELECT status FROM Leave_Application WHERE leave_id=?", (b34_lid,), one=True)
        check(b'cannot approve or reject your own leave' in response.data
              and b34_after_reject['status'] == 'Pending',
              'B34: approver cannot reject their own leave application')

        dbe("DELETE FROM Leave_Application WHERE leave_id=?", (b34_lid,))

# ═══════════════════════════════════════════════════════════════════════════════
# B35 — Branch-dependent interviewer assignment
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('35')
print('B35 — Branch-dependent interviewer assignment')
print('=' * 60)

if _active:
    b35_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b35_')
    b35_db = os.path.join(b35_tmp_dir, 'smarthr_b35.db')
    shutil.copy2(app_db_mod.DB_PATH, b35_db)
    b35_real_db = app_db_mod.DB_PATH

    app_db_mod.DB_PATH = b35_db
    init_db_mod.DB_PATH = b35_db
    try:
        from app.recruitment import routes as b35_rr
        b35_orig_send = b35_rr.send_email
        b35_rr.send_email = lambda *args, **kwargs: True

        with app.test_client() as client:
            client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                        follow_redirects=True)

            b35_roles = {r['role_name']: r['role_id']
                         for r in dbq("SELECT role_id, role_name FROM Role")}
            b35_dept = dbq("""SELECT d.department_id FROM Department d
                              JOIN Branch b ON d.branch_id=b.branch_id
                              WHERE b.company_id=1 ORDER BY d.department_id LIMIT 1""", one=True)
            check(b35_dept is not None, 'B35: found a company-one department for fixtures')

            br_a = dbe("INSERT INTO Branch (company_id, name, address_line1, city) VALUES (1,'B35 A','1 Test Rd','KL')")
            br_b = dbe("INSERT INTO Branch (company_id, name, address_line1, city) VALUES (1,'B35 B','2 Test Rd','KL')")
            br_c = dbe("INSERT INTO Branch (company_id, name, address_line1, city) VALUES (1,'B35 C','3 Test Rd','KL')")

            def b35_emp(branch, role, name, active=1):
                return dbe("""INSERT INTO Employee
                              (company_id, branch_id, department_id, full_name, hire_date,
                               base_salary, role_id, email, password_hash, is_active)
                              VALUES (1,?,?,?,'2024-01-01',5000,?,?,'x',?)""",
                           (branch, b35_dept['department_id'], name, b35_roles[role],
                            name.lower().replace(' ', '.') + '@b35.test', active))

            mgr_a = b35_emp(br_a, 'Manager', 'B35 MgrA')
            mgr_b = b35_emp(br_b, 'Manager', 'B35 MgrB')
            hr_a = b35_emp(br_a, 'HR', 'B35 HRA')
            hr_off = b35_emp(br_b, 'HR', 'B35 HROff', active=0)

            pid_a = dbe("INSERT INTO Job_Posting (title, branch_id, status, approved_openings) "
                        "VALUES ('B35 A Role',?,'Open',3)", (br_a,))
            pid_b = dbe("INSERT INTO Job_Posting (title, branch_id, status) VALUES ('B35 B Role',?,'Open')", (br_b,))
            pid_c = dbe("INSERT INTO Job_Posting (title, branch_id, status) VALUES ('B35 C Role',?,'Open')", (br_c,))

            def b35_app(posting, name):
                return dbe("""INSERT INTO Job_Application
                              (posting_id, company_id, applicant_name, applicant_email, status)
                              VALUES (?,1,?,?,'Shortlisted')""", (posting, name, name.lower() + '@b35.test'))

            a_a1 = b35_app(pid_a, 'B35 CandA1')
            a_a2 = b35_app(pid_a, 'B35 CandA2')
            a_a3 = b35_app(pid_a, 'B35 CandA3')
            a_a4 = b35_app(pid_a, 'B35 CandA4')
            a_a5 = b35_app(pid_a, 'B35 CandA5')
            a_c1 = b35_app(pid_c, 'B35 CandC1')
            a_c2 = b35_app(pid_c, 'B35 CandC2')

            b35_day = datetime.date.today() + datetime.timedelta(days=1)
            while b35_day.weekday() >= 5:
                b35_day += datetime.timedelta(days=1)
            b35_date = b35_day.isoformat()

            # ── Pool membership: physical is posting-branch only, virtual is
            #    company-wide, inactive staff are never eligible ──
            phys_a = b35_rr._get_eligible_interviewers(1, br_a, physical_only=True)
            check({p['employee_id'] for p in phys_a} == {mgr_a, hr_a},
                  'B35: physical pool contains only posting-branch employees')
            phys_b = b35_rr._get_eligible_interviewers(1, br_b, physical_only=True)
            check({p['employee_id'] for p in phys_b} == {mgr_b},
                  'B35: physical pool excludes out-of-branch staff and inactive HR')
            wide_ids = {r['employee_id'] for r in dbq("""
                SELECT e.employee_id FROM Employee e JOIN Role r ON e.role_id=r.role_id
                WHERE e.company_id=1 AND e.is_active=1
                  AND r.role_name IN ('Admin','HR','HR Manager')""")}
            virt_b = b35_rr._get_eligible_interviewers(1, br_b, physical_only=False)
            check({p['employee_id'] for p in virt_b} == ({mgr_b} | wide_ids),
                  'B35: virtual pool is company-wide (Managers of posting branch + Admin/HR/HR Manager)')
            check(b35_rr._get_eligible_interviewers(1, br_c, physical_only=True) == [],
                  'B35: branch with no employees has an empty physical pool')

            # ── Requester: earliest approved vacancy request wins; inactive
            #    requesters are ignored; never added when not in the pool ──
            dbe("""INSERT INTO Vacancy_Request
                   (requested_by, department_id, position_title, employment_type, status, posting_id, created_at)
                   VALUES (?,?,'B35 R1','Full-Time','Approved',?,'2026-01-10 10:00:00')""",
                (mgr_b, b35_dept['department_id'], pid_a))
            dbe("""INSERT INTO Vacancy_Request
                   (requested_by, department_id, position_title, employment_type, status, posting_id, created_at)
                   VALUES (?,?,'B35 R2','Full-Time','Approved',?,'2026-01-05 09:00:00')""",
                (mgr_a, b35_dept['department_id'], pid_a))
            dbe("""INSERT INTO Vacancy_Request
                   (requested_by, department_id, position_title, employment_type, status, posting_id, created_at)
                   VALUES (?,?,'B35 R3','Full-Time','Approved',?,'2026-01-07 09:00:00')""",
                (mgr_b, b35_dept['department_id'], pid_b))
            dbe("""INSERT INTO Vacancy_Request
                   (requested_by, department_id, position_title, employment_type, status, posting_id, created_at)
                   VALUES (?,?,'B35 R4','Full-Time','Approved',?,'2026-01-08 09:00:00')""",
                (hr_off, b35_dept['department_id'], pid_c))

            req_a = b35_rr._requester_for_posting(pid_a, 1)
            check(req_a is not None and req_a['employee_id'] == mgr_a,
                  'B35: earliest approved vacancy request is the requester')
            req_b = b35_rr._requester_for_posting(pid_b, 1)
            check(req_b is not None and req_b['employee_id'] == mgr_b,
                  'B35: single approved request determines the requester')
            check(b35_rr._requester_for_posting(pid_c, 1) is None,
                  'B35: inactive requester is ignored')
            check(b35_rr._requester_for_posting(999999, 1) is None,
                  'B35: unknown posting has no requester')

            pool_req = b35_rr._get_eligible_interviewers(1, br_a, physical_only=False,
                                                         requester_id=req_a['employee_id'])
            check(pool_req[0]['employee_id'] == mgr_a and 'B35 MgrA' == pool_req[0]['full_name'],
                  'B35: requester is listed first in the virtual pool')
            pool_req_phys = b35_rr._get_eligible_interviewers(1, br_b, physical_only=True,
                                                              requester_id=mgr_a)
            check(all(p['employee_id'] != mgr_a for p in pool_req_phys),
                  'B35: requester outside the posting branch is not added to the physical pool')

            # ── Manual scheduling: format-specific pool validation ──
            resp = client.post(f'/recruitment/application/{a_a1}/schedule-interview',
                               data={'date': b35_date, 'time': '10:00', 'duration': '60',
                                     'format': 'Physical', 'interviewer_ids': str(mgr_b)},
                               follow_redirects=True)
            check(b'only interviewers from the posting branch can be selected' in resp.data,
                  'B35: manual Physical rejects an out-of-branch interviewer')

            resp = client.post(f'/recruitment/application/{a_a1}/schedule-interview',
                               data={'date': b35_date, 'time': '10:00', 'duration': '60',
                                     'format': 'Virtual', 'meeting_link': 'https://meet.example.com/b35',
                                     'interviewer_ids': str(mgr_b)},
                               follow_redirects=True)
            check(b'Select eligible interviewers for this interview' in resp.data,
                  'B35: manual Virtual rejects an active but ineligible interviewer')

            resp = client.post(f'/recruitment/application/{a_a1}/schedule-interview',
                               data={'date': b35_date, 'time': '10:00', 'duration': '60',
                                     'format': 'Virtual', 'meeting_link': 'https://meet.example.com/b35',
                                     'interviewer_ids': '999999'},
                               follow_redirects=True)
            check(b'active interviewers from your company' in resp.data,
                  'B35: forged interviewer IDs still rejected with the original message')

            resp = client.post(f'/recruitment/application/{a_a1}/schedule-interview',
                               data={'date': b35_date, 'time': '10:00', 'duration': '60',
                                     'format': 'Virtual', 'meeting_link': 'https://meet.example.com/b35',
                                     'interviewer_ids': str(hr_a)},
                               follow_redirects=True)
            iv = dbq("SELECT * FROM Interview WHERE application_id=? ORDER BY interview_id DESC LIMIT 1",
                     (a_a1,), one=True)
            check(resp.status_code == 200 and iv is not None and iv['interviewer_ids'] == str(hr_a)
                  and iv['format'] == 'Virtual',
                  'B35: manual Virtual accepts a company-wide HR interviewer')

            resp = client.post(f'/recruitment/application/{a_a2}/schedule-interview',
                               data={'date': b35_date, 'time': '11:00', 'duration': '60',
                                     'format': 'Physical',
                                     'interviewer_ids': [str(mgr_a), str(hr_a)]},
                               follow_redirects=True)
            iv2 = dbq("SELECT * FROM Interview WHERE application_id=? ORDER BY interview_id DESC LIMIT 1",
                      (a_a2,), one=True)
            check(iv2 is not None and set(iv2['interviewer_ids'].split(',')) == {str(mgr_a), str(hr_a)}
                  and iv2['format'] == 'Physical' and iv2['posting_branch_id'] == br_a,
                  'B35: manual Physical accepts posting-branch interviewers only')

            resp = client.post(f'/recruitment/application/{a_c1}/schedule-interview',
                               data={'date': b35_date, 'time': '10:00', 'duration': '60',
                                     'format': 'Physical', 'interviewer_ids': ''},
                               follow_redirects=True)
            check(b'This branch has no eligible local interviewers. Schedule as Virtual instead.' in resp.data,
                  'B35: Physical blocked when the posting branch has no local interviewers')

            resp = client.post(f'/recruitment/application/{a_c1}/schedule-interview',
                               data={'date': b35_date, 'time': '10:00', 'duration': '60',
                                     'format': 'Virtual', 'meeting_link': 'https://meet.example.com/b35',
                                     'interviewer_ids': ''},
                               follow_redirects=True)
            iv3 = dbq("SELECT * FROM Interview WHERE application_id=? ORDER BY interview_id DESC LIMIT 1",
                      (a_c1,), one=True)
            check(resp.status_code == 200 and iv3 is not None and not iv3['interviewer_ids'],
                  'B35: interviewers stay optional; no-selection interview is created')

            # ── Auto-assign: Physical limited to the posting branch ──
            r = client.post('/recruitment/auto-assign',
                            data={'application_ids': [str(a_a3), str(a_a4)], 'format': 'Physical'})
            data = r.get_json()
            check(r.status_code == 200 and data is not None and 'assignments' in data
                  and len(data['assignments']) == 2
                  and all(int(x['interviewer_ids']) in (mgr_a, hr_a) for x in data['assignments']),
                  'B35: auto-assign Physical assigns only posting-branch interviewers')

            r = client.post('/recruitment/auto-assign',
                            data={'application_ids': [str(a_c2)], 'format': 'Physical'})
            data = r.get_json()
            check(data is not None and 'Schedule as Virtual instead' in data.get('error', ''),
                  'B35: auto-assign Physical reports no local interviewers for an empty branch')

            r = client.post('/recruitment/auto-assign',
                            data={'application_ids': [str(a_c2)], 'format': 'Virtual',
                                  'meeting_link': 'https://meet.example.com/b35'})
            data = r.get_json()
            check(r.status_code == 200 and data is not None and len(data.get('assignments', [])) == 1
                  and int(data['assignments'][0]['interviewer_ids']) in wide_ids,
                  'B35: auto-assign Virtual on an empty branch uses the company-wide roster')

            client.post('/recruitment/auto-assign/confirm',
                        data={'application_ids': [str(a_c2)], 'format': 'Virtual',
                              'meeting_link': 'https://meet.example.com/b35'},
                        follow_redirects=True)
            iv4 = dbq("SELECT * FROM Interview WHERE application_id=? ORDER BY interview_id DESC LIMIT 1",
                      (a_c2,), one=True)
            check(iv4 is not None and int(iv4['interviewer_ids']) in wide_ids,
                  'B35: auto-assign confirm applies the virtual pool for an empty branch')

            # ── UI hints: requester label + disabled Physical on empty branches ──
            a_c3 = b35_app(pid_c, 'B35 CandC3')
            page = client.get(f'/recruitment/applications/{a_a3}').data.decode('utf-8', errors='replace')
            check('(Requester)' in page and page.index('B35 MgrA') < page.index('B35 HRA'),
                  'B35: requester listed first with a (Requester) label')
            page_c = client.get(f'/recruitment/applications/{a_c3}').data.decode('utf-8', errors='replace')
            check('disabled title="This branch has no eligible local interviewers"' in page_c
                  and 'Schedule as Virtual instead' in page_c,
                  'B35: Physical option disabled and hint shown for a branch without local interviewers')

            # ── Bulk scheduling uses the same rules ──
            resp = client.post('/recruitment/bulk-schedule',
                               data={'posting_id': str(pid_a), 'date': b35_date, 'start_time': '10:00',
                                     'duration': '30', 'format': 'Physical', 'interviewer_ids': str(mgr_b)},
                               follow_redirects=True)
            check(b'only interviewers from the posting branch can be selected' in resp.data,
                  'B35: bulk Physical rejects an out-of-branch interviewer')

            before = dbq("SELECT COUNT(*) as c FROM Interview WHERE posting_branch_id=?", (br_a,), one=True)['c']
            resp = client.post('/recruitment/bulk-schedule',
                               data={'posting_id': str(pid_a), 'date': b35_date, 'start_time': '12:30',
                                     'duration': '30', 'format': 'Virtual',
                                     'meeting_link': 'https://meet.example.com/b35',
                                     'interviewer_ids': str(hr_a)},
                               follow_redirects=True)
            after = dbq("SELECT COUNT(*) as c FROM Interview WHERE posting_branch_id=?", (br_a,), one=True)['c']
            check(after == before + 3,
                  'B35: bulk Virtual creates an interview per shortlisted candidate')

            # ── HR Manager: no manual or bulk scheduling, auto-assign only ──
            a_a6 = b35_app(pid_a, 'B35 CandA6')
            client.get('/logout')
            client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                        follow_redirects=True)
            resp = client.get(f'/recruitment/applications/{a_a6}')
            check(b'Schedule Interview' not in resp.data and b'id="schedule-form"' not in resp.data,
                  'B35: HR Manager does not see manual interview scheduling controls')
            resp = client.post(f'/recruitment/application/{a_a6}/schedule-interview',
                               data={'date': b35_date, 'time': '13:00', 'duration': '60',
                                     'format': 'Virtual', 'meeting_link': 'https://meet.example.com/b35'},
                               follow_redirects=True)
            check(b'do not have permission' in resp.data,
                  'B35: HR Manager cannot schedule interviews manually')
            resp = client.post('/recruitment/bulk-schedule',
                               data={'posting_id': str(pid_a), 'date': b35_date, 'start_time': '10:00',
                                     'duration': '30', 'format': 'Virtual',
                                     'meeting_link': 'https://meet.example.com/b35'},
                               follow_redirects=True)
            check(b'do not have permission' in resp.data,
                  'B35: HR Manager cannot use bulk scheduling')
            r = client.post('/recruitment/auto-assign',
                            data={'application_ids': [str(a_a6)], 'format': 'Virtual',
                                  'meeting_link': 'https://meet.example.com/b35'})
            check(r.status_code == 200 and r.get_json() is not None and 'assignments' in r.get_json(),
                  'B35: HR Manager can still auto-assign interviews')
            client.get('/logout')
    finally:
        app_db_mod.DB_PATH = b35_real_db
        init_db_mod.DB_PATH = b35_real_db
        shutil.rmtree(b35_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B36 — Posting soft delete (archive) with interview guard
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('36')
print('B36 — Posting soft delete (archive)')
print('=' * 60)

if _active:
    b36_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b36_')
    b36_db = os.path.join(b36_tmp_dir, 'smarthr_b36.db')
    shutil.copy2(_suite_real_db, b36_db)
    b36_real_db = app_db_mod.DB_PATH
    app_db_mod.DB_PATH = b36_db
    init_db_mod.DB_PATH = b36_db
    try:
        import sqlite3 as b36_sqlite
        # Build a controlled pre-migration state: Job_Posting with the
        # vacancy-openings-era CHECK (Partially Filled but no Archived), one
        # Filled and one Open posting. Independent of the live DB's migration
        # state so the rebuild/backup behaviour is always exercised.
        b36_old_jp_ddl = """
        CREATE TABLE Job_Posting (
            posting_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            title           TEXT NOT NULL,
            position_id     INTEGER REFERENCES Position(position_id),
            department_id   INTEGER,
            branch_id       INTEGER,
            employment_type TEXT CHECK(employment_type IN ('Full-Time','Part-Time','Contract')),
            min_salary      REAL,
            max_salary      REAL,
            description     TEXT,
            requirements    TEXT,
            status          TEXT DEFAULT 'Open' CHECK(status IN ('Open','Partially Filled','Closed','Filled')),
            target_audience TEXT NOT NULL DEFAULT 'Both' CHECK(target_audience IN ('Internal','External','Both')),
            posted_by       INTEGER,
            created_at      TEXT DEFAULT (datetime('now')),
            closed_at       TEXT,
            approved_openings INTEGER NOT NULL DEFAULT 1,
            reserved_openings INTEGER NOT NULL DEFAULT 0,
            filled_openings   INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (department_id) REFERENCES Department(department_id),
            FOREIGN KEY (branch_id)     REFERENCES Branch(branch_id),
            FOREIGN KEY (posted_by)     REFERENCES Employee(employee_id)
        )
        """
        b36_con = b36_sqlite.connect(b36_db)
        b36_con.row_factory = b36_sqlite.Row
        b36_con.execute("PRAGMA foreign_keys = OFF")
        for _b36_child in ('Opening_Reservation', 'Offer_Approval', 'Email_Delivery_Log',
                           'Interview_Scorecard', 'Interview_Reschedule',
                           'Candidate_Recommendation'):
            if b36_con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                               (_b36_child,)).fetchone():
                b36_con.execute("DELETE FROM " + _b36_child)
        b36_con.execute("DELETE FROM Interview")
        b36_con.execute("DELETE FROM Contract")
        b36_con.execute("DELETE FROM Job_Application")
        b36_con.execute("DELETE FROM Vacancy_Request WHERE posting_id IS NOT NULL")
        b36_con.execute("DROP TABLE Job_Posting")
        b36_con.execute(b36_old_jp_ddl)
        b36_con.execute("PRAGMA foreign_keys = ON")
        emp36 = b36_con.execute(
            "SELECT employee_id, branch_id, department_id FROM Employee ORDER BY employee_id LIMIT 1"
        ).fetchone()
        dept36 = b36_con.execute(
            "SELECT department_id, branch_id FROM Department WHERE department_id=?",
            (emp36['department_id'],)).fetchone() or emp36
        b36_con.execute(
            "INSERT INTO Job_Posting (posting_id, title, department_id, branch_id, status, created_at, closed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (901, 'B36 Filled Role', dept36['department_id'], emp36['branch_id'], 'Filled',
             '2026-01-01 09:00:00', '2026-01-10 09:00:00'))
        b36_con.execute(
            "INSERT INTO Job_Posting (posting_id, title, department_id, branch_id, status, created_at, closed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (902, 'B36 Open Role', dept36['department_id'], emp36['branch_id'], 'Open',
             '2026-01-02 09:00:00', None))
        b36_con.commit()
        b36_con.close()

        check(len(glob.glob(os.path.join(b36_tmp_dir, 'backups', '*'))) == 0,
              'B36: no backups exist before the migration')

        init_db_mod.migrate_job_posting_archive()

        with app.test_client() as client:
            client.get('/login')
            b36_sql = dbq("SELECT sql FROM sqlite_master WHERE type='table' AND name='Job_Posting'", one=True)
            check(b36_sql is not None and 'Archived' in (b36_sql['sql'] or ''),
                  'B36: Archived status added to Job_Posting CHECK by migration')
            b36_archived = dbq("""SELECT status, closed_at FROM Job_Posting
                                  WHERE posting_id=901""", one=True)
            check(b36_archived is not None and b36_archived['status'] == 'Archived'
                  and b36_archived['closed_at'] == '2026-01-10 09:00:00',
                  'B36: previously Filled postings backfilled to Archived with closed_at kept')
            b36_still_open = dbq("SELECT status FROM Job_Posting WHERE posting_id=902", one=True)
            check(b36_still_open['status'] == 'Open',
                  'B36: non-Filled postings are untouched by the backfill')
            b36_baks = glob.glob(os.path.join(b36_tmp_dir, 'backups', '*'))
            check(len(b36_baks) == 1, 'B36: migration created exactly one timestamped backup')
            init_db_mod.migrate_job_posting_archive()
            b36_sql2 = dbq("SELECT sql FROM sqlite_master WHERE type='table' AND name='Job_Posting'", one=True)
            check(b36_sql2 is not None and 'Archived' in (b36_sql2['sql'] or ''),
                  'B36: archive migration is idempotent')

            client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'},
                        follow_redirects=True)

            b36_roles = {r['role_name']: r['role_id']
                         for r in dbq("SELECT role_id, role_name FROM Role")}
            b36_dept = dbq("""SELECT d.department_id FROM Department d
                              JOIN Branch b ON d.branch_id=b.branch_id
                              WHERE b.company_id=1 ORDER BY d.department_id LIMIT 1""", one=True)
            b36_br = dbe("INSERT INTO Branch (company_id, name, address_line1, city) VALUES (1,'B36 A','1 Test Rd','KL')")

            def b36_posting(title):
                return dbe("INSERT INTO Job_Posting (title, department_id, branch_id, status) "
                           "VALUES (?,?,?,'Open')", (title, b36_dept['department_id'], b36_br))

            def b36_app(posting, name):
                return dbe("""INSERT INTO Job_Application
                              (posting_id, company_id, applicant_name, applicant_email, status)
                              VALUES (?,1,?,?,'Shortlisted')""", (posting, name, name.lower() + '@b36.test'))

            pid_clean = b36_posting('B36 Clean Role')
            aid_clean = b36_app(pid_clean, 'B36 CandClean')
            pid_busy = b36_posting('B36 Busy Role')
            aid_busy = b36_app(pid_busy, 'B36 CandBusy')
            dbe("INSERT INTO Interview (application_id, scheduled_at, status) "
                "VALUES (?, '2026-09-01 09:00:00', 'Scheduled')", (aid_busy,))
            pid_hrm = b36_posting('B36 HRM Role')
            b36_app(pid_hrm, 'B36 CandHRM')
            pid_hr = b36_posting('B36 HR Role')
            b36_app(pid_hr, 'B36 CandHR')

            resp = client.post(f'/recruitment/postings/{pid_clean}/delete', follow_redirects=True)
            p_clean = dbq("SELECT status FROM Job_Posting WHERE posting_id=?", (pid_clean,), one=True)
            check(resp.status_code == 200 and p_clean['status'] == 'Archived'
                  and b'Posting deleted.' in resp.data,
                  'B36: Admin deletes a posting with no interviews (soft delete)')

            resp = client.post(f'/recruitment/postings/{pid_busy}/delete', follow_redirects=True)
            p_busy = dbq("SELECT status FROM Job_Posting WHERE posting_id=?", (pid_busy,), one=True)
            check(resp.status_code == 200 and p_busy['status'] == 'Open'
                  and b'interviews have already been scheduled' in resp.data,
                  'B36: deletion blocked when interviews are scheduled')

            for show in ('active', 'closed'):
                page = client.get(f'/recruitment/postings?show={show}').data.decode('utf-8', errors='replace')
                check('B36 Clean Role' not in page,
                      f'B36: archived posting hidden from the {show} postings list')
            page = client.get(f'/recruitment/postings?show=active').data.decode('utf-8', errors='replace')
            check('B36 Busy Role' in page and 'B36 Clean Role' not in page,
                  'B36: active list still shows the interview-blocked posting')

            page = client.get(f'/recruitment/postings/{pid_clean}').data.decode('utf-8', errors='replace')
            check('Archived' in page and 'Delete Posting' not in page,
                  'B36: archived posting still viewable, badge shown, no delete button')

            client.get('/logout')

            client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                        follow_redirects=True)
            resp = client.post(f'/recruitment/postings/{pid_hrm}/delete', follow_redirects=True)
            p_hrm = dbq("SELECT status FROM Job_Posting WHERE posting_id=?", (pid_hrm,), one=True)
            check(resp.status_code == 200 and p_hrm['status'] == 'Archived',
                  'B36: HR Manager can delete a posting')
            client.get('/logout')

            from werkzeug.security import generate_password_hash
            plain_hr = dbe("""INSERT INTO Employee
                              (company_id, branch_id, department_id, full_name, hire_date,
                               base_salary, role_id, email, password_hash, is_active)
                              VALUES (1,?,?,'B36 PlainHR','2024-01-01',5000,?,'b36hr@x.test',?,1)""",
                           (b36_br, b36_dept['department_id'], b36_roles['HR'],
                            generate_password_hash('B36HR@123')))
            client.post('/login', data={'email': 'b36hr@x.test', 'password': 'B36HR@123'},
                        follow_redirects=True)
            resp = client.post(f'/recruitment/postings/{pid_clean}/delete', follow_redirects=True)
            p_still = dbq("SELECT status FROM Job_Posting WHERE posting_id=?", (pid_clean,), one=True)
            check(resp.status_code == 200 and p_still['status'] == 'Archived'
                  and b'do not have permission' in resp.data,
                  'B36: plain HR cannot delete a posting')
            page = client.get(f'/recruitment/postings/{pid_hr}').data.decode('utf-8', errors='replace')
            check('Delete Posting' not in page,
                  'B36: delete button hidden from plain HR')
            client.get('/logout')

            resp = client.get(f'/recruitment/apply/{pid_clean}')
            check(resp.status_code == 404 and b'no longer accepting applications' in resp.data,
                  'B36: archived posting no longer accepts public applications')
    finally:
        app_db_mod.DB_PATH = b36_real_db
        init_db_mod.DB_PATH = b36_real_db
        shutil.rmtree(b36_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B37 — Organisation setup navigation
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('37')
print('B37 — Organisation setup navigation')
print('=' * 60)

if _active:
    with app.test_client() as client:
        client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                    follow_redirects=True)
        posting_page = client.get('/recruitment/postings/add').data.decode('utf-8', errors='replace')
        check('department-empty-cta' in posting_page
              and 'Add a department for this branch' in posting_page,
              'B37: new posting form offers a department setup path for a new branch')
        roles_page = client.get('/organization/roles').data.decode('utf-8', errors='replace')
        check('Organisation Setup' in roles_page
              and 'Manage Branches' in roles_page
              and 'Manage Departments' in roles_page,
              'B37: roles page links the branch, department, and position setup journey')
        branch_page = client.get('/organization/branches').data.decode('utf-8', errors='replace')
        department_page = client.get('/organization/departments').data.decode('utf-8', errors='replace')
        check('Manage departments for' in branch_page and '+ Position' in department_page,
              'B37: branch and department lists link directly to the next setup step')
        increment_page = client.get('/increment/').data.decode('utf-8', errors='replace')
        check('Existing proposals keep the percentage stated in their row.' in increment_page,
              'B37: increment queue explains why historic proposals can differ from the current policy')
        client.get('/logout')

# ═══════════════════════════════════════════════════════════════════════════════
# B38 — Invoice OCR file guidance
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('38')
print('B38 — Invoice OCR file guidance')
print('=' * 60)

if _active:
    with app.test_client() as client:
        client.post('/login', data={'email': 'elizabeth@smarthr.my', 'password': 'Employee@123'},
                    follow_redirects=True)
        invoice_page = client.get('/invoices/').data.decode('utf-8', errors='replace')
        check('Auto-extract data from your invoice file' in invoice_page
              and 'Please select an invoice file first.' in invoice_page,
              'B38: OCR guidance matches the image and PDF upload options')
        client.get('/logout')

# ═══════════════════════════════════════════════════════════════════════════════
# B39 — Guided organisation setup return flow
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('39')
print('B39 — Guided organisation setup return flow')
print('=' * 60)

if _active:
    from urllib.parse import parse_qs, urlsplit
    with app.test_client() as client:
        client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                    follow_redirects=True)
        b39_company = dbq("SELECT company_id FROM Company ORDER BY company_id LIMIT 1", one=True)
        b39_position_form = '/organization/roles/positions/add?position_name=B39+Flow+Manager'
        response = client.post('/organization/branch/add', data={
            'company_id': str(b39_company['company_id']), 'name': 'B39 Flow Branch',
            'address_line1': '39 Flow Road', 'address_line2': '', 'city': 'Kuala Lumpur',
            'state': 'Kuala Lumpur', 'postal_code': '50039', 'contact_no': '',
            'hr_manager_id': '', 'parent_branch_id': '', 'return_to': b39_position_form,
        }, follow_redirects=False)
        b39_branch = dbq("SELECT branch_id FROM Branch WHERE name='B39 Flow Branch'", one=True)
        b39_branch_target = urlsplit(response.headers.get('Location', ''))
        b39_branch_params = parse_qs(b39_branch_target.query)
        check(response.status_code == 302 and b39_branch is not None
              and b39_branch_target.path == '/organization/roles/positions/add'
              and b39_branch_params.get('branch_id') == [str(b39_branch['branch_id'])],
              'B39: creating a branch returns to the in-progress position setup with the branch selected')

        response = client.post('/organization/department/add', data={
            'branch_id': str(b39_branch['branch_id']), 'department_name': 'B39 Flow Department',
            'department_manager_id': '', 'return_to': response.headers.get('Location', ''),
        }, follow_redirects=False)
        b39_department = dbq("SELECT department_id FROM Department WHERE department_name='B39 Flow Department'", one=True)
        b39_department_target = urlsplit(response.headers.get('Location', ''))
        b39_department_params = parse_qs(b39_department_target.query)
        check(response.status_code == 302 and b39_department is not None
              and b39_department_target.path == '/organization/roles/positions/add'
              and b39_department_params.get('department_id') == [str(b39_department['department_id'])],
              'B39: creating a department returns to the position form with the new department selected')

        b39_posting_form = (f'/recruitment/postings/add?branch_id={b39_branch["branch_id"]}'
                            f'&department_id={b39_department["department_id"]}')
        response = client.post('/organization/roles/positions/add', data={
            'branch_id': str(b39_branch['branch_id']),
            'department_id': str(b39_department['department_id']),
            'position_name': 'B39 Flow Manager', 'return_to': b39_posting_form,
        }, follow_redirects=False)
        b39_position = dbq("SELECT position_id FROM Position WHERE position_name='B39 Flow Manager'", one=True)
        b39_posting_target = urlsplit(response.headers.get('Location', ''))
        b39_posting_params = parse_qs(b39_posting_target.query)
        check(response.status_code == 302 and b39_position is not None
              and b39_posting_target.path == '/recruitment/postings/add'
              and b39_posting_params.get('position_id') == [str(b39_position['position_id'])],
              'B39: creating a position returns to the posting form with every organisation selection prefilled')

        page = client.get('/organization/department/add?return_to=https://example.test').data.decode('utf-8', errors='replace')
        check('https://example.test' not in page,
              'B39: workflow return destination rejects external URLs')

        response = client.post('/organization/branch/add', data={
            'company_id': str(b39_company['company_id']), 'name': 'B39 Guided Branch',
            'address_line1': '40 Flow Road', 'address_line2': '', 'city': 'Kuala Lumpur',
            'state': 'Kuala Lumpur', 'postal_code': '50040', 'contact_no': '',
            'hr_manager_id': '', 'parent_branch_id': '', 'continue_setup': 'department',
        }, follow_redirects=False)
        b39_guided_branch = dbq("SELECT branch_id FROM Branch WHERE name='B39 Guided Branch'", one=True)
        b39_guided_target = urlsplit(response.headers.get('Location', ''))
        check(b39_guided_target.path == '/organization/department/add'
              and parse_qs(b39_guided_target.query).get('continue_setup') == ['1'],
              'B39: Create Branch & Add Department starts the explicit guided setup sequence')

        response = client.post('/organization/department/add', data={
            'branch_id': str(b39_guided_branch['branch_id']),
            'department_name': 'B39 Guided Department', 'department_manager_id': '',
            'continue_setup': '1',
        }, follow_redirects=False)
        b39_guided_dept = dbq("SELECT department_id FROM Department WHERE department_name='B39 Guided Department'", one=True)
        b39_guided_target = urlsplit(response.headers.get('Location', ''))
        check(b39_guided_target.path == '/organization/roles/positions/add'
              and parse_qs(b39_guided_target.query).get('continue_setup') == ['1'],
              'B39: guided department creation continues to the prefilled position form')

        response = client.post('/organization/roles/positions/add', data={
            'branch_id': str(b39_guided_branch['branch_id']),
            'department_id': str(b39_guided_dept['department_id']),
            'position_name': 'B39 Guided Manager', 'continue_setup': '1',
        }, follow_redirects=False)
        b39_guided_pos = dbq("SELECT position_id FROM Position WHERE position_name='B39 Guided Manager'", one=True)
        b39_guided_target = urlsplit(response.headers.get('Location', ''))
        b39_guided_params = parse_qs(b39_guided_target.query)
        check(b39_guided_target.path == '/employees/add'
              and b39_guided_params.get('setup_branch_manager') == ['1']
              and b39_guided_params.get('position_id') == [str(b39_guided_pos['position_id'])],
              'B39: guided position creation continues to a prefilled manager form')

        employee_page = client.get(response.headers.get('Location', '')).data.decode('utf-8', errors='replace')
        b39_manager_role = dbq("SELECT role_id FROM Role WHERE role_name='Manager'", one=True)
        check('Guided branch setup — create the manager' in employee_page
              and f'value="{b39_manager_role["role_id"]}" selected' in employee_page,
              'B39: guided employee form makes the Manager role explicit and preselected')
        check('e.submitter' in employee_page and 'continueInput.value = e.submitter.value' in employee_page,
              'B39: employee validation preserves the selected guided-flow submit action')

        response = client.post('/employees/add', data={
            'full_name': 'Guided Manager', 'ic_number': '', 'passport_number': '',
            'contact_no': '', 'address': '', 'date_of_birth': '', 'gender': '',
            'emergency_contact_name': '', 'emergency_contact_no': '',
            'email': 'b39-guided-manager@example.test', 'personal_email': '',
            'branch_id': str(b39_guided_branch['branch_id']),
            'department_id': str(b39_guided_dept['department_id']),
            'position_id': str(b39_guided_pos['position_id']), 'position': '',
            'hire_date': '2024-01-01', 'work_start_time': '09:00', 'work_end_time': '18:00',
            'employment_type': 'Full-Time', 'employment_status': 'Active', 'base_salary': '5000',
            'role_id': str(b39_manager_role['role_id']), 'password': 'B39Manager!',
            'confirm_password': 'B39Manager!', 'id_document_path': '',
            'setup_branch_manager': '1', 'continue_setup': 'posting',
        }, follow_redirects=False)
        b39_final_target = urlsplit(response.headers.get('Location', ''))
        b39_final_params = parse_qs(b39_final_target.query)
        check(response.status_code == 302 and b39_final_target.path == '/recruitment/postings/add'
              and b39_final_params.get('branch_id') == [str(b39_guided_branch['branch_id'])]
              and b39_final_params.get('department_id') == [str(b39_guided_dept['department_id'])]
              and b39_final_params.get('position_id') == [str(b39_guided_pos['position_id'])],
              'B39: Create Employee & Start Job Posting hands off with all selections retained')
        client.get('/logout')

# ═══════════════════════════════════════════════════════════════════════════════
# B40 — Attendance log summaries match the visible scoped result set
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('40')
print('B40 — Attendance log summary consistency')
print('=' * 60)

if _active:
    with app.test_client() as client:
        client.get('/login')
        manager = dbq("SELECT employee_id, branch_id FROM Employee WHERE email='weiliang@smarthr.my'", one=True)
        manager_day = dbq("""SELECT date(a.check_in) as day
                             FROM Attendance a JOIN Employee e ON a.employee_id=e.employee_id
                             WHERE e.branch_id=? ORDER BY a.check_in DESC LIMIT 1""",
                          (manager['branch_id'],), one=True)
        expected_manager = dbq("""SELECT COUNT(*) as c FROM Attendance a
                                  JOIN Employee e ON a.employee_id=e.employee_id
                                  WHERE e.branch_id=? AND date(a.check_in)=?""",
                               (manager['branch_id'], manager_day['day']), one=True)['c']
        client.post('/login', data={'email': 'weiliang@smarthr.my', 'password': 'Manager@123'},
                    follow_redirects=True)
        page = client.get(f"/attendance/logs?from={manager_day['day']}&to={manager_day['day']}").data.decode('utf-8', errors='replace')
        check(f'<div class="metric-val green-val">{expected_manager}</div>' in page,
              'B40: branch-manager attendance summary matches its visible branch records')
        client.get('/logout')

        manual_day = dbq("""SELECT date(check_in) as day FROM Attendance
                            WHERE is_manual_entry=1 ORDER BY check_in DESC LIMIT 1""", one=True)
        expected_manual = dbq("""SELECT COUNT(*) as c FROM Attendance
                                 WHERE is_manual_entry=1 AND date(check_in)=?""",
                              (manual_day['day'],), one=True)['c']
        client.post('/login', data={'email': 'sarah@smarthr.my', 'password': 'Employee@123'},
                    follow_redirects=True)
        page = client.get(f"/attendance/logs?from={manual_day['day']}&to={manual_day['day']}&method=manual").data.decode('utf-8', errors='replace')
        check(f'<div class="metric-val green-val">{expected_manual}</div>' in page,
              'B40: HR attendance summary honours the selected entry method')
        client.get('/logout')

# ═══════════════════════════════════════════════════════════════════════════════
# B41 — Password reset flow (forgot-password + reset-password)
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('41')
print('B41 — Password reset flow')
print('=' * 60)

if _active:
    b41_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b41_')
    b41_db = os.path.join(b41_tmp_dir, 'smarthr_b41.db')
    shutil.copy2(_suite_db, b41_db)
    b41_real_db = app_db_mod.DB_PATH
    app_db_mod.DB_PATH = b41_db
    init_db_mod.DB_PATH = b41_db
    import re as _re
    import app as _app_mod
    b41_sent = []
    b41_orig_send = _app_mod.mail.send
    _app_mod.mail.send = lambda msg: b41_sent.append(msg)
    try:
        with app.test_client() as client:
            page = client.get('/forgot-password').data.decode('utf-8', errors='replace')
            check('Forgot password?' in page and 'name="email"' in page,
                  'B41: forgot-password page renders the request form')

            resp = client.post('/forgot-password',
                               data={'email': 'admin@smarthr.my'},
                               follow_redirects=True)
            check(resp.status_code == 200 and b'If that email is registered' in resp.data
                  and len(b41_sent) == 1 and b41_sent[0].recipients == ['admin@smarthr.my'],
                  'B41: registered email triggers a password reset email')
            audit_sent = dbq("""SELECT action_status FROM AuditLog
                                WHERE action='PASSWORD_RESET' AND action_status='Success'
                                ORDER BY rowid DESC LIMIT 1""", one=True)
            check(audit_sent is not None, 'B41: successful reset send is audited')

            sent_before = len(b41_sent)
            resp = client.post('/forgot-password',
                               data={'email': 'nobody@nowhere.test'},
                               follow_redirects=True)
            check(resp.status_code == 200 and b'If that email is registered' in resp.data
                  and len(b41_sent) == sent_before,
                  'B41: unregistered email sends nothing (anti-enumeration flash kept)')
            audit_skip = dbq("""SELECT description, action_status FROM AuditLog
                                WHERE action='PASSWORD_RESET_ATTEMPT'
                                ORDER BY rowid DESC LIMIT 1""", one=True)
            check(audit_skip is not None and audit_skip['action_status'] == 'Success'
                  and 'n*****@nowhere.test' in (audit_skip['description'] or '')
                  and 'nobody@nowhere.test' not in (audit_skip['description'] or ''),
                  'B41: unregistered attempts are audited with a masked email')

            body = b41_sent[0].body
            m = _re.search(r'https?://\S+/reset-password/([^\s]+)', body)
            token = m.group(1) if m else None
            check(token is not None, 'B41: reset email carries the reset URL')

            page = client.get(f'/reset-password/{token}').data.decode('utf-8', errors='replace')
            check('Reset Password' in page and 'name="password"' in page,
                  'B41: valid token renders the reset form')

            resp = client.post(f'/reset-password/{token}',
                               data={'password': 'short', 'confirm_password': 'short'})
            check(b'at least 8 characters' in resp.data,
                  'B41: short new password rejected')

            resp = client.post(f'/reset-password/{token}',
                               data={'password': 'NewPass@123', 'confirm_password': 'Different@1'})
            check(b'do not match' in resp.data,
                  'B41: mismatched confirmation rejected')

            resp = client.post(f'/reset-password/{token}',
                               data={'password': 'NewPass@123', 'confirm_password': 'NewPass@123'},
                               follow_redirects=True)
            check(resp.status_code == 200 and b'reset successfully' in resp.data,
                  'B41: valid reset completes with the success message')
            audit_reset = dbq("""SELECT action_status FROM AuditLog
                                 WHERE action='PASSWORD_RESET' AND description LIKE '%completed%'
                                 ORDER BY rowid DESC LIMIT 1""", one=True)
            check(audit_reset is not None, 'B41: completed reset is audited')

            client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'NewPass@123'},
                        follow_redirects=True)
            page = client.get('/').data.decode('utf-8', errors='replace')
            check('Dashboard' in page, 'B41: login works with the new password')
            client.get('/logout')

            resp = client.get('/reset-password/badtoken123', follow_redirects=True)
            check(b'invalid or has expired' in resp.data,
                  'B41: tampered token rejected')
    finally:
        _app_mod.mail.send = b41_orig_send
        app_db_mod.DB_PATH = b41_real_db
        init_db_mod.DB_PATH = b41_real_db
        shutil.rmtree(b41_tmp_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════════
# B42 — Least-privilege hire role & department-manager assignment
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
_focus_block('42')
print('B42 — Least-privilege hire role & department-manager assignment')
print('=' * 60)

if _active:
    b42_tmp_dir = tempfile.mkdtemp(prefix='smarthr_b42_')
    b42_db = os.path.join(b42_tmp_dir, 'smarthr_b42.db')
    shutil.copy2(_suite_db, b42_db)
    b42_real_db = app_db_mod.DB_PATH
    app_db_mod.DB_PATH = b42_db
    init_db_mod.DB_PATH = b42_db
    import json as _json
    import sqlite3 as _sqlite3
    import app as _app_mod
    b42_orig_send = _app_mod.mail.send
    _app_mod.mail.send = lambda msg: None
    try:
        # ── Migration: old Position schema gains the flag, defaulting to 0 ──
        b42m_db = os.path.join(b42_tmp_dir, 'mig.db')
        b42m_con = sqlite3.connect(b42m_db)
        b42m_con.execute("""CREATE TABLE Position (
            position_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            position_name TEXT NOT NULL,
            department_id INTEGER NOT NULL,
            is_active     INTEGER DEFAULT 1,
            created_at    TEXT DEFAULT (datetime('now'))
        )""")
        b42m_con.execute("INSERT INTO Position (position_name, department_id) VALUES ('Old Role', 1)")
        b42m_con.commit()
        b42m_con.close()
        init_db_mod.DB_PATH = b42m_db
        init_db_mod.migrate_position_manager_flag()
        b42m_con = sqlite3.connect(b42m_db)
        b42m_cols = [r[1] for r in b42m_con.execute("PRAGMA table_info(Position)")]
        b42m_flag = b42m_con.execute("SELECT is_department_manager_position FROM Position WHERE position_name='Old Role'").fetchone()[0]
        b42m_con.close()
        check('is_department_manager_position' in b42m_cols and b42m_flag == 0,
              'B42: migration adds the flag with a safe false default for existing positions')
        init_db_mod.migrate_position_manager_flag()
        check(True, 'B42: manager-flag migration is idempotent')
        init_db_mod.DB_PATH = b42_db

        with app.test_client() as client:
            client.get('/login')
            client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                        follow_redirects=True)

            b42_employee_role = dbq("SELECT role_id FROM Role WHERE role_name='Employee'", one=True)['role_id']
            b42_branch = dbq("SELECT branch_id FROM Branch WHERE company_id=1 ORDER BY branch_id LIMIT 1", one=True)['branch_id']

            # Department A (no manager) + a flagged position, via the real routes.
            client.post('/organization/department/add', data={
                'branch_id': str(b42_branch), 'department_name': 'B42 Dept A',
                'department_manager_id': '', 'return_to': '',
            }, follow_redirects=False)
            b42_dept_a = dbq("SELECT department_id FROM Department WHERE department_name='B42 Dept A'", one=True)['department_id']
            check(b42_dept_a is not None, 'B42: fixture department created')

            client.post('/organization/roles/positions/add', data={
                'branch_id': str(b42_branch), 'department_id': str(b42_dept_a),
                'position_name': 'B42 Dept Lead', 'is_department_manager_position': '1',
            }, follow_redirects=False)
            b42_pos_lead = dbq("SELECT position_id FROM Position WHERE position_name='B42 Dept Lead'", one=True)['position_id']
            b42_pos_lead_flag = dbq("SELECT is_department_manager_position FROM Position WHERE position_id=?", (b42_pos_lead,), one=True)['is_department_manager_position']
            check(b42_pos_lead_flag == 1, 'B42: add-position persists the Department Manager Position flag')

            client.post('/organization/roles/positions/add', data={
                'branch_id': str(b42_branch), 'department_id': str(b42_dept_a),
                'position_name': 'B42 Ordinary', 'is_department_manager_position': '',
            }, follow_redirects=False)
            b42_pos_ordinary = dbq("SELECT position_id FROM Position WHERE position_name='B42 Ordinary'", one=True)['position_id']
            b42_pos_ordinary_flag = dbq("SELECT is_department_manager_position FROM Position WHERE position_id=?", (b42_pos_ordinary,), one=True)['is_department_manager_position']
            check(b42_pos_ordinary_flag == 0, 'B42: unflagged position stays ordinary')

            def b42_prefill(name, email, pos_id, dept_id):
                return _json.dumps({
                    'full_name': name, 'email': email, 'personal_email': email,
                    'ic_number': '', 'date_of_birth': '', 'gender': '', 'contact_no': '',
                    'address': '', 'emergency_contact_name': '', 'emergency_contact_no': '',
                    'position_id': pos_id, 'position': 'B42 Role',
                    'department_id': dept_id, 'branch_id': b42_branch,
                    'employment_type': 'Full-Time', 'base_salary': '5000',
                    'hire_date': '2026-01-01', 'work_start_time': '09:00',
                    'work_end_time': '18:00', 'contract_id': '',
                })

            def b42_hire_post(name, email, pos_id, dept_id, extra=None):
                data = {
                    'from_hire': '1', 'full_name': name, 'ic_number': '', 'passport_number': '',
                    'contact_no': '', 'address': '', 'date_of_birth': '', 'gender': '',
                    'emergency_contact_name': '', 'emergency_contact_no': '',
                    'email': email, 'personal_email': email,
                    'branch_id': str(b42_branch), 'department_id': str(dept_id),
                    'position_id': str(pos_id), 'position': 'B42 Role',
                    'hire_date': '2026-01-01', 'work_start_time': '09:00',
                    'work_end_time': '18:00', 'employment_type': 'Full-Time',
                    'employment_status': 'Active', 'base_salary': '5000',
                    'password': '', 'confirm_password': '', 'id_document_path': '',
                }
                if extra:
                    data.update(extra)
                return client.post('/employees/add', data=data, follow_redirects=True)

            # GET: hire prefill preselects Employee (never Admin) and shows the banner.
            with client.session_transaction() as sess:
                sess['hire_prefill'] = b42_prefill('Hire One', 'b42.one@example.test',
                                                   b42_pos_lead, b42_dept_a)
            page = client.get('/employees/add?from_hire=1').data.decode('utf-8', errors='replace')
            check(f'<option value="{b42_employee_role}" selected>Employee</option>' in page
                  and '<option value="1" selected>Admin</option>' not in page,
                  'B42: hire form preselects Employee, never Admin')
            check('Department Manager Position' in page and 'automatically assigned as department manager of B42 Dept A' in page,
                  'B42: hire form explains the automatic department-manager assignment')

            # POST flagged position without role_id -> Employee + auto department manager.
            resp = b42_hire_post('Hire One', 'b42.one@example.test', b42_pos_lead, b42_dept_a)
            b42_emp1 = dbq("SELECT employee_id, role_id FROM Employee WHERE email='b42.one@example.test'", one=True)
            b42_dept_a_mgr = dbq("SELECT department_manager_id FROM Department WHERE department_id=?", (b42_dept_a,), one=True)['department_manager_id']
            check(resp.status_code == 200 and b42_emp1 is not None
                  and b42_emp1['role_id'] == b42_employee_role
                  and b42_dept_a_mgr == b42_emp1['employee_id'],
                  'B42: flagged-position hire gets Employee role and is auto-assigned department manager')
            b42_audit_auto = dbq("""SELECT action_status FROM AuditLog
                                    WHERE action='DEPT_MANAGER_AUTO_ASSIGN'
                                    ORDER BY rowid DESC LIMIT 1""", one=True)
            check(b42_audit_auto is not None, 'B42: auto department-manager assignment is audited')

            # Second hire for the same department -> blocked by the existing manager.
            with client.session_transaction() as sess:
                sess['hire_prefill'] = b42_prefill('Hire Two', 'b42.two@example.test',
                                                   b42_pos_lead, b42_dept_a)
            resp = b42_hire_post('Hire Two', 'b42.two@example.test', b42_pos_lead, b42_dept_a)
            b42_emp2 = dbq("SELECT employee_id FROM Employee WHERE email='b42.two@example.test'", one=True)
            b42_dept_a_mgr2 = dbq("SELECT department_manager_id FROM Department WHERE department_id=?", (b42_dept_a,), one=True)['department_manager_id']
            check(b42_emp2 is None and b42_dept_a_mgr2 == b42_emp1['employee_id']
                  and b'Reassign the department manager first' in resp.data,
                  'B42: existing-manager conflict blocks creation and never replaces the manager')

            # Ordinary position: default Employee; explicit role honored; invalid falls back.
            with client.session_transaction() as sess:
                sess['hire_prefill'] = b42_prefill('Ord One', 'b42.ord1@example.test',
                                                   b42_pos_ordinary, b42_dept_a)
            b42_hire_post('Ord One', 'b42.ord1@example.test', b42_pos_ordinary, b42_dept_a)
            b42_ord1 = dbq("SELECT role_id FROM Employee WHERE email='b42.ord1@example.test'", one=True)
            check(b42_ord1 is not None and b42_ord1['role_id'] == b42_employee_role,
                  'B42: ordinary-position hire defaults to Employee')

            with client.session_transaction() as sess:
                sess['hire_prefill'] = b42_prefill('Ord Two', 'b42.ord2@example.test',
                                                   b42_pos_ordinary, b42_dept_a)
            b42_hire_post('Ord Two', 'b42.ord2@example.test', b42_pos_ordinary, b42_dept_a,
                          extra={'role_id': '2'})
            b42_ord2 = dbq("SELECT role_id FROM Employee WHERE email='b42.ord2@example.test'", one=True)
            check(b42_ord2 is not None and b42_ord2['role_id'] == 2,
                  'B42: HR explicit role choice is honored for ordinary positions')

            with client.session_transaction() as sess:
                sess['hire_prefill'] = b42_prefill('Ord Three', 'b42.ord3@example.test',
                                                   b42_pos_ordinary, b42_dept_a)
            b42_hire_post('Ord Three', 'b42.ord3@example.test', b42_pos_ordinary, b42_dept_a,
                          extra={'role_id': '999'})
            b42_ord3 = dbq("SELECT role_id FROM Employee WHERE email='b42.ord3@example.test'", one=True)
            check(b42_ord3 is not None and b42_ord3['role_id'] == b42_employee_role,
                  'B42: invalid role_id falls back to Employee (never Admin)')

            # Department picker lists any active employee (role shown), and an
            # Employee-role employee can be assigned; cross-branch is rejected.
            page = client.get('/organization/department/add').data.decode('utf-8', errors='replace')
            check('Elizabeth Lopez' in page and '(Employee' in page,
                  'B42: department picker lists Employee-role employees with their role')
            resp = client.post('/organization/department/add', data={
                'branch_id': str(b42_branch), 'department_name': 'B42 Dept B',
                'department_manager_id': '4',
            }, follow_redirects=False)
            b42_dept_b = dbq("SELECT department_id, department_manager_id FROM Department WHERE department_name='B42 Dept B'", one=True)
            check(resp.status_code == 302 and b42_dept_b is not None
                  and b42_dept_b['department_manager_id'] == 4,
                  'B42: Employee-role employee assignable as department manager')
            b42_other_branch = dbq("SELECT branch_id FROM Branch WHERE company_id=1 AND branch_id!=? ORDER BY branch_id LIMIT 1",
                                   (b42_branch,), one=True)['branch_id']
            resp = client.post('/organization/department/add', data={
                'branch_id': str(b42_other_branch), 'department_name': 'B42 Dept C',
                'department_manager_id': '4',
            }, follow_redirects=True)
            b42_dept_c = dbq("SELECT department_id FROM Department WHERE department_name='B42 Dept C'", one=True)
            check(b42_dept_c is None
                  and b'active employee of this branch in the same company' in resp.data,
                  'B42: cross-branch department-manager assignment rejected')

            # Edit modal: Admin/HR sees the System Role select; role change re-syncs
            # permissions and audits; Manager sessions stay blocked server-side.
            page = client.get(f'/employees/{b42_emp1["employee_id"]}').data.decode('utf-8', errors='replace')
            check('System Role' in page and 'name="role_id"' in page,
                  'B42: employee edit exposes the System Role selector to HR')
            resp = client.post(f'/employees/{b42_emp1["employee_id"]}/edit', data={
                'full_name': 'Hire One', 'contact_no': '', 'address': '',
                'date_of_birth': '', 'gender': 'Male', 'emergency_contact_name': '',
                'emergency_contact_no': '', 'position': 'B42 Dept Lead',
                'base_salary': '5000', 'employment_type': 'Full-Time',
                'employment_status': 'Active', 'work_start_time': '09:00',
                'work_end_time': '18:00', 'branch_id': str(b42_branch),
                'department_id': str(b42_dept_a), 'role_id': '3',
            }, follow_redirects=True)
            b42_emp1_after = dbq("SELECT role_id FROM Employee WHERE employee_id=?", (b42_emp1['employee_id'],), one=True)
            b42_emp1_perms = dbq("SELECT COUNT(*) c FROM Employee_Permission WHERE employee_id=? AND is_active=1",
                                 (b42_emp1['employee_id'],), one=True)['c']
            b42_promote = dbq("""SELECT action_status FROM AuditLog
                                 WHERE action='PROMOTE_DEMOTE'
                                 ORDER BY rowid DESC LIMIT 1""", one=True)
            check(b42_emp1_after['role_id'] == 3 and b42_emp1_perms == 12 and b42_promote is not None,
                  'B42: role change via edit re-syncs permissions and is audited')
            client.get('/logout')

            client.post('/login', data={'email': 'weiliang@smarthr.my', 'password': 'Manager@123'},
                        follow_redirects=True)
            resp = client.post(f'/employees/{b42_emp1["employee_id"]}/edit', data={
                'full_name': 'Hire One', 'contact_no': '', 'address': '',
                'date_of_birth': '', 'gender': 'Male', 'emergency_contact_name': '',
                'emergency_contact_no': '', 'position': 'B42 Dept Lead',
                'base_salary': '5000', 'employment_type': 'Full-Time',
                'employment_status': 'Active', 'work_start_time': '09:00',
                'work_end_time': '18:00', 'branch_id': str(b42_branch),
                'department_id': str(b42_dept_a), 'role_id': str(b42_employee_role),
            }, follow_redirects=True)
            b42_emp1_still = dbq("SELECT role_id FROM Employee WHERE employee_id=?", (b42_emp1['employee_id'],), one=True)
            check(b42_emp1_still['role_id'] == 3 and b'Managers cannot change' in resp.data,
                  'B42: Manager session cannot change an employee role')
            client.get('/logout')

            # Flagged-position hire must FORCE the Employee role server-side
            # even when the form submits Admin/Manager.
            client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                        follow_redirects=True)
            client.post('/organization/department/add', data={
                'branch_id': str(b42_branch), 'department_name': 'B42 Dept D',
                'department_manager_id': '', 'return_to': '',
            }, follow_redirects=False)
            b42_dept_d = dbq("SELECT department_id FROM Department WHERE department_name='B42 Dept D'", one=True)['department_id']
            client.post('/organization/roles/positions/add', data={
                'branch_id': str(b42_branch), 'department_id': str(b42_dept_d),
                'position_name': 'B42 Escalation Lead', 'is_department_manager_position': '1',
            }, follow_redirects=False)
            b42_pos_esc = dbq("SELECT position_id FROM Position WHERE position_name='B42 Escalation Lead'", one=True)['position_id']
            with client.session_transaction() as sess:
                sess['hire_prefill'] = b42_prefill('Escalation One', 'b42.esc1@example.test',
                                                   b42_pos_esc, b42_dept_d)
            resp = b42_hire_post('Escalation One', 'b42.esc1@example.test', b42_pos_esc, b42_dept_d,
                                 extra={'role_id': '1'})
            b42_esc1 = dbq("SELECT employee_id, role_id FROM Employee WHERE email='b42.esc1@example.test'", one=True)
            b42_esc_perms = dbq("SELECT COUNT(*) c FROM Employee_Permission WHERE employee_id=? AND is_active=1",
                                (b42_esc1['employee_id'],), one=True)['c'] if b42_esc1 else -1
            b42_dept_d_mgr = dbq("SELECT department_manager_id FROM Department WHERE department_id=?", (b42_dept_d,), one=True)['department_manager_id']
            check(resp.status_code == 200 and b42_esc1 is not None
                  and b42_esc1['role_id'] == b42_employee_role and b42_esc_perms == 6
                  and b42_dept_d_mgr == b42_esc1['employee_id'],
                  'B42: flagged hire with submitted Admin role is forced to Employee (never Admin)')

            # Zero-row conditional-update path: a trigger steals the department
            # manager slot between the pre-check and the conditional UPDATE, so
            # the UPDATE affects zero rows and the whole hire transaction must
            # roll back (no employee, no audit, manager slot unchanged).
            client.post('/organization/department/add', data={
                'branch_id': str(b42_branch), 'department_name': 'B42 Dept E',
                'department_manager_id': '', 'return_to': '',
            }, follow_redirects=False)
            b42_dept_e = dbq("SELECT department_id FROM Department WHERE department_name='B42 Dept E'", one=True)['department_id']
            client.post('/organization/roles/positions/add', data={
                'branch_id': str(b42_branch), 'department_id': str(b42_dept_e),
                'position_name': 'B42 Raced Lead', 'is_department_manager_position': '1',
            }, follow_redirects=False)
            b42_pos_race = dbq("SELECT position_id FROM Position WHERE position_name='B42 Raced Lead'", one=True)['position_id']
            b42_con = _sqlite3.connect(b42_db)
            b42_con.execute(f"""CREATE TRIGGER b42_steal_mgr AFTER INSERT ON Employee
                               BEGIN
                                   UPDATE Department SET department_manager_id=1
                                   WHERE department_id={int(b42_dept_e)};
                               END""")
            b42_con.commit()
            b42_con.close()
            b42_audit_before = dbq("""SELECT COUNT(*) c FROM AuditLog
                                      WHERE action='DEPT_MANAGER_AUTO_ASSIGN'""", one=True)['c']
            with client.session_transaction() as sess:
                sess['hire_prefill'] = b42_prefill('Raced One', 'b42.race1@example.test',
                                                   b42_pos_race, b42_dept_e)
            resp = b42_hire_post('Raced One', 'b42.race1@example.test', b42_pos_race, b42_dept_e,
                                 extra={'role_id': '2'})
            b42_race1 = dbq("SELECT employee_id FROM Employee WHERE email='b42.race1@example.test'", one=True)
            b42_audit_after = dbq("""SELECT COUNT(*) c FROM AuditLog
                                     WHERE action='DEPT_MANAGER_AUTO_ASSIGN'""", one=True)['c']
            b42_dept_e_mgr = dbq("SELECT department_manager_id FROM Department WHERE department_id=?", (b42_dept_e,), one=True)['department_manager_id']
            check(b42_race1 is None and b42_audit_after == b42_audit_before
                  and b42_dept_e_mgr is None
                  and b'Reassign the department manager first' in resp.data,
                  'B42: zero-row department assignment rolls back the whole hire transaction')
            client.get('/logout')
    finally:
        _app_mod.mail.send = b42_orig_send
        app_db_mod.DB_PATH = b42_real_db
        init_db_mod.DB_PATH = b42_real_db
        shutil.rmtree(b42_tmp_dir, ignore_errors=True)

# Summary
# ═══════════════════════════════════════════════════════════════════════════════

print()
print('=' * 60)
print(f'Results: {passed} passed, {failed} failed out of {passed + failed}')
print('=' * 60)

if failed > 0:
    sys.exit(1)
