"""Regression tests for Phase 2 fixes: B1, B2a, B3, B4.

Run with:
    .venv/Scripts/python.exe test_phase2_fixes.py
"""
import sys, os
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, '.')
from app import create_app

app = create_app()
app.config['CSRF_ENABLED'] = False
app.config['WTF_CSRF_ENABLED'] = False

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

# ═══════════════════════════════════════════════════════════════════════════════
# B1 — approve_vacancy: sqlite3.Row['target_audience'] instead of .get()
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
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
    check('value="Both" selected' in html or "value='Both' selected" in html,
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
print('B4 — Recruitment nav visibility by role')
print('=' * 60)

def has_link(html, text):
    return text in html

def missing_link(html, text):
    return text not in html

with app.test_client() as client:
    # ── Test 1: Plain Employee (elizabeth@smarthr.my) ──
    client.post('/login', data={'email': 'elizabeth@smarthr.my', 'password': 'Employee@123'},
                follow_redirects=True)
    resp = client.get('/')
    check(resp.status_code == 200, 'Employee dashboard (200)')
    html = resp.data.decode('utf-8', errors='replace')

    check(missing_link(html, 'Job Postings'),
          'Employee: "Job Postings" nav link NOT visible')
    check(missing_link(html, 'Vacancy Requests'),
          'Employee: "Vacancy Requests" nav link NOT visible')
    check(has_link(html, 'Internal Job Board'),
          'Employee: "Internal Job Board" nav link IS visible')
    check(has_link(html, 'My Applications'),
          'Employee: "My Applications" nav link IS visible')
    check(missing_link(html, 'Applications (External)'),
          'Employee: "Applications (External)" nav link NOT visible')

    client.get('/logout')

    # ── Test 2: Manager (cheeseng@smarthr.my) ──
    client.post('/login', data={'email': 'cheeseng@smarthr.my', 'password': 'Manager@123'},
                follow_redirects=True)
    resp = client.get('/')
    check(resp.status_code == 200, 'Manager dashboard (200)')
    html = resp.data.decode('utf-8', errors='replace')

    check(has_link(html, 'Job Postings'),
          'Manager: "Job Postings" nav link IS visible')
    check(has_link(html, 'Vacancy Requests'),
          'Manager: "Vacancy Requests" nav link IS visible')
    check(has_link(html, 'Applications (External)'),
          'Manager: "Applications (External)" nav link IS visible')
    check(has_link(html, 'My Applications'),
          'Manager: "My Applications" nav link IS visible')

    client.get('/logout')

    # ── Test 3: HR (hr@smarthr.my) ──
    client.post('/login', data={'email': 'hr@smarthr.my', 'password': 'Hr@123'},
                follow_redirects=True)
    resp = client.get('/')
    check(resp.status_code == 200, 'HR dashboard (200)')
    html = resp.data.decode('utf-8', errors='replace')

    check(has_link(html, 'Job Postings'),
          'HR: "Job Postings" nav link IS visible')
    check(has_link(html, 'Vacancy Requests'),
          'HR: "Vacancy Requests" nav link IS visible')
    check(has_link(html, 'Applications (External)'),
          'HR: "Applications (External)" nav link IS visible')
    check(has_link(html, 'Interview Policy'),
          'HR: "Interview Policy" nav link IS visible')
    check(has_link(html, 'Careers Page'),
          'HR: "Careers Page" nav link IS visible')

    # HR should NOT have the "Internal Job Board" or "My Applications" (uses non-HR guard)
    # Note: HR may see these or not depending on the guard — the guard is:
    # session.user_role not in ('Admin','HR','HR Manager','HR Director')
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
    check(has_link(html, 'Vacancy Requests'),
          'Admin: "Vacancy Requests" nav link IS visible')
    check(has_link(html, 'Applications (External)'),
          'Admin: "Applications (External)" nav link IS visible')

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
# Summary
# ═══════════════════════════════════════════════════════════════════════════════

print()
print('=' * 60)
print(f'Results: {passed} passed, {failed} failed out of {passed + failed}')
print('=' * 60)

if failed > 0:
    sys.exit(1)
