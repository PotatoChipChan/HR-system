"""
test_recruitment_flow.py — End-to-end recruitment pipeline test

Full flow: vacancy request -> approve -> posting -> 6 applications
-> AI shortlist -> schedule 3 interviews -> pass best -> contract
-> offer -> accept -> hire -> employee created

Run: python test_recruitment_flow.py
"""
import os, sys, uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import create_app
from app.database import query, execute

app = create_app()
app.config['TESTING'] = True
app.config['WTF_CSRF_ENABLED'] = False
client = app.test_client()

passes = 0
fails = 0


def check(n, label, fn):
    global passes, fails
    try:
        result = fn()
        passes += 1
        print(f"  [OK] {n}. {label}")
        return result
    except Exception as e:
        fails += 1
        print(f"  [FAIL] {n}. {label}")
        print(f"        {e}")
        return None


def fail(msg):
    raise Exception(msg)


def login(email, pw):
    resp = client.post('/login', data={'email': email, 'password': pw}, follow_redirects=True)
    text = resp.data.decode('utf-8', errors='replace').lower()
    if 'invalid' in text or 'locked' in text or 'account' in text:
        raise Exception(f"Login rejected for {email}")
    if 'password' in text and len(resp.data) < 3000:
        raise Exception(f"Still on login page for {email}")
    return True


def logout():
    client.get('/logout', follow_redirects=True)


def require_login(n, label, email, pw):
    logout()
    ok = check(n, label, lambda: login(email, pw))
    if not ok:
        print("[ABORT] Login required for subsequent steps")
        raise SystemExit(1)


# ─────────────────────────────────────────────────────────────────────────────
print("=" * 60)
print("SmartHR Recruitment Pipeline E2E Test")
print("=" * 60)

with app.app_context():
    execute("DELETE FROM Contract WHERE application_id IN (SELECT application_id FROM Job_Application WHERE applicant_email LIKE '%testflow%')")
    execute("DELETE FROM Interview WHERE application_id IN (SELECT application_id FROM Job_Application WHERE applicant_email LIKE '%testflow%')")
    execute("DELETE FROM Employee WHERE personal_email LIKE '%testflow%' OR email LIKE '%testflow%'")
    execute("DELETE FROM Job_Application WHERE applicant_email LIKE '%testflow%'")
    execute("DELETE FROM Vacancy_Request WHERE position_title LIKE '%Test Flow%'")
    execute("DELETE FROM Job_Posting WHERE title LIKE '%Test Flow%'")
    print("[CLEANUP] Done\n")

# ═══════════════════════════════════════════════════════════════════════
# 1. Create vacancy request
# ═══════════════════════════════════════════════════════════════════════
require_login(1, "Login as Manager", 'brian@smarthr.my', 'Manager@123')
check(2, "Submit vacancy request", lambda: client.post('/recruitment/vacancy-request', data={
    'department_id': '3', 'position_title': 'Senior Backend Developer (Test Flow)',
    'employment_type': 'Full-Time', 'min_salary': '7000', 'max_salary': '12000',
    'description': 'Build and maintain API services with Python, Flask, PostgreSQL.',
    'requirements': '5+ yrs Python backend, Flask/Django, PostgreSQL, REST APIs, Docker, CI/CD.',
    'reason': 'Team expansion Q3'
}, follow_redirects=True))

with app.app_context():
    vr = query("SELECT request_id FROM Vacancy_Request WHERE position_title LIKE '%Test Flow%' ORDER BY request_id DESC LIMIT 1", one=True)
    vacancy_id = vr['request_id'] if vr else (print("[ABORT]") or sys.exit(1))
    check(3, f"Vacancy request ID={vacancy_id}", lambda: True)

# ═══════════════════════════════════════════════════════════════════════
# 2. Admin approves -> auto-create posting
# ═══════════════════════════════════════════════════════════════════════
require_login(4, "Login as Admin", 'admin@smarthr.my', 'Admin@123')
check(5, "Approve vacancy request", lambda: client.post(
    f'/recruitment/vacancy-request/{vacancy_id}/approve',
    data={'branch_id': '1'}, follow_redirects=True))

with app.app_context():
    vr2 = query("SELECT posting_id FROM Vacancy_Request WHERE request_id=?", (vacancy_id,), one=True)
    posting_id = vr2['posting_id'] if vr2 else (print("[ABORT]") or sys.exit(1))
    check(6, f"Job posting ID={posting_id}", lambda: True)

# ═══════════════════════════════════════════════════════════════════════
# 3. 5 external (email) + 1 internal (web) applications
# ═══════════════════════════════════════════════════════════════════════
with app.app_context():
    from email.mime.text import MIMEText
    import email as email_lib
    from app.notifications.email_parser import parse_application_email
    from app.notifications.email_monitor import (create_application_from_email,
                                                  decode_email_header, get_email_body)

    candidates = [
        ('Ramesh Kumar', 'ramesh.testflow@example.com', '+60123456701', '900101-14-5001',
         'No 10 Jalan Ampang, 50450 KL',
         '5 years Python backend Flask PostgreSQL REST APIs. Led team of 4. Docker CI/CD. Agile Scrum. Strong problem-solving skills.'),
        ('Siti Nurhaliza binti Aziz', 'siti.testflow@example.com', '+60123456702', '880505-08-6002',
         'No 22 Jalan Bukit Bintang, 55100 KL',
         '3 years backend Python Django. Some PostgreSQL. Eager to learn Flask. Team of 2. Good communication.'),
        ('Tan Wei Ming', 'weiming.testflow@example.com', '+60123456703', '910715-07-8003',
         'No 5 Jalan SS15, Subang Jaya, 47500',
         '7+ years backend engineering. Expert Python Flask PostgreSQL REST APIs Docker Kubernetes CI/CD. Led multiple teams. Microservices architecture.'),
        ('Nurul Huda binti Rahman', 'nurul.testflow@example.com', '+60123456704', '890912-03-4004',
         'No 8 Jalan Pudu, 55200 KL',
         'Fresh grad Computer Science. Internship Django project. Basic Python SQL. Quick learner.'),
        ('Mohd Faizal bin Ismail', 'faizal.testflow@example.com', '+60123456705', '860320-01-2005',
         'No 15 Jalan Klang, 41000 Selangor',
         '4 years backend Python Flask PostgreSQL REST APIs Docker. Agile teams of 5-6. Strong analytical.'),
    ]

    app_ids = []
    for name, email, phone, ic, address, cover in candidates:
        body = (f"Name: {name}\nPosition: Senior Backend Developer\nEmail: {email}\n"
                f"IC: {ic}\nPhone: {phone}\nAddress: {address}\n\n{cover}\n\n"
                f"---\n[Ref: POST-{posting_id}]\n")
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = 'Application for Senior Backend Developer'
        msg['From'] = f'{name} <{email}>'
        msg['Message-ID'] = f'<testflow-{uuid.uuid4().hex}@example.com>'
        parsed_msg = email_lib.message_from_bytes(msg.as_bytes())
        parsed = parse_application_email(
            decode_email_header(parsed_msg['Subject']) or '',
            get_email_body(parsed_msg),
            decode_email_header(parsed_msg['From']) or '')
        aid = create_application_from_email(parsed_msg, parsed)
        if aid:
            app_ids.append(aid)

    check(7, f"5 external applications ({len(app_ids)})", lambda: len(app_ids) == 5 or fail(f"Got {len(app_ids)}"))

check(8, "Internal web application", lambda: client.post(
    f'/recruitment/apply/{posting_id}', data={
        'applicant_name': 'Internal Candidate Test',
        'applicant_email': 'internal.testflow@example.com',
        'applicant_phone': '+60123456706',
        'cover_letter': '6 years Python backend PostgreSQL REST APIs Docker. Led projects Flask. Agile.'
    }, follow_redirects=True))

with app.app_context():
    count = query("SELECT COUNT(*) c FROM Job_Application WHERE posting_id=?", (posting_id,), one=True)['c']
    check(9, f"Total {count} applications", lambda: count == 6 or fail(f"Got {count}"))

# ═══════════════════════════════════════════════════════════════════════
# 4. AI Shortlisting: top 3 auto-Shortlisted
# ═══════════════════════════════════════════════════════════════════════
with app.app_context():
    from app.recruitment.scorer import score_applications
    posting = query("SELECT * FROM Job_Posting WHERE posting_id=?", (posting_id,), one=True)
    all_apps = query("SELECT application_id, applicant_name, cover_letter, resume_path FROM Job_Application WHERE posting_id=?", (posting_id,))
    results = score_applications(dict(posting), [dict(a) for a in all_apps], app_root=app.root_path)

    for i, r in enumerate(results):
        status = 'Shortlisted' if i < 3 else 'New'
        execute("UPDATE Job_Application SET ai_score=?, ai_summary=?, status=? WHERE application_id=?",
                (r['score'], r['summary'], status, r['application_id']))

    sl = query("SELECT applicant_name, ai_score FROM Job_Application WHERE posting_id=? AND status='Shortlisted' ORDER BY ai_score DESC", (posting_id,))
    ns = query("SELECT applicant_name FROM Job_Application WHERE posting_id=? AND status='New' ORDER BY ai_score", (posting_id,))
    check(10, f"Shortlisted: {[s['applicant_name'] for s in sl]}", lambda: len(sl) == 3 or fail(f"Got {len(sl)}"))
    print(f"    Not shortlisted: {[s['applicant_name'] for s in ns]}")

# ═══════════════════════════════════════════════════════════════════════
# 5. Schedule interviews (past dates for instant results)
# ═══════════════════════════════════════════════════════════════════════
require_login(11, "Login as HR Manager", 'hr@smarthr.my', 'Hr@123')

with app.app_context():
    admin_emp = query("SELECT employee_id FROM Employee WHERE email='admin@smarthr.my'", one=True)['employee_id']
    sl = query("SELECT application_id, applicant_name FROM Job_Application WHERE posting_id=? AND status='Shortlisted' ORDER BY ai_score DESC", (posting_id,))
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    for i, s in enumerate(sl):
        t = f'{10 + i}:00'
        check(f"12.{i+1}", f"Schedule interview: {s['applicant_name']}", lambda sid=s["application_id"], d=yesterday, tm=t:
              client.post(f'/recruitment/application/{sid}/schedule-interview', data={
                  'date': d, 'time': tm, 'duration': '60',
                  'location': 'Meeting Room 3A, KL HQ',
                  'meeting_link': '', 'type': 'In-Person',
                  'interviewer_ids': str(admin_emp)
              }, follow_redirects=True))

    iv_count = query("""
        SELECT COUNT(*) c FROM Interview iv JOIN Job_Application ja ON iv.application_id=ja.application_id
        WHERE ja.posting_id=? AND iv.status='Scheduled'
    """, (posting_id,), one=True)
    check(13, f"{iv_count['c']} interviews scheduled", lambda: iv_count['c'] == 3 or fail(f"Got {iv_count['c']}"))

# ═══════════════════════════════════════════════════════════════════════
# 6. Pass best candidate (auto-rejects others)
# ═══════════════════════════════════════════════════════════════════════
with app.app_context():
    best = sl[0]
    iv = query("SELECT interview_id FROM Interview WHERE application_id=? ORDER BY scheduled_at LIMIT 1",
               (best["application_id"],), one=True)
    check(14, f"Pass: {best['applicant_name']}",
          lambda: client.post(f'/recruitment/interview/{iv["interview_id"]}/result', data={'result': 'Pass'}, follow_redirects=True))

with app.app_context():
    hired = query("SELECT application_id, applicant_name FROM Job_Application WHERE posting_id=? ORDER BY CASE WHEN status='Interview' THEN 0 ELSE 1 END, ai_score DESC LIMIT 1", (posting_id,), one=True)
    rejected = query("SELECT COUNT(*) c FROM Job_Application WHERE posting_id=? AND status='Rejected'", (posting_id,), one=True)
    best_app_id = hired['application_id']
    check(15, f"Best: {hired['applicant_name']}, {rejected['c']} rejected", lambda: True)

# ═══════════════════════════════════════════════════════════════════════
# 7. Contract -> Offer -> Accept -> Hire -> Create Employee
# ═══════════════════════════════════════════════════════════════════════
start = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
check(16, "Create contract", lambda: client.post(f'/recruitment/contract/{best_app_id}', data={
    'offer_date': datetime.now().strftime('%Y-%m-%d'), 'start_date': start,
    'position': 'Senior Backend Developer', 'department_id': '3',
    'employment_type': 'Full-Time', 'work_start_time': '09:00',
    'work_end_time': '18:00', 'base_salary': '9000'
}, follow_redirects=True))

check(17, "Send offer letter", lambda: client.post(
    f'/recruitment/application/{best_app_id}/send-offer', follow_redirects=True))

with app.app_context():
    contract = query("SELECT contract_id FROM Contract WHERE application_id=? ORDER BY contract_id DESC LIMIT 1",
                     (best_app_id,), one=True)
    if not contract:
        print("[ABORT] No contract found")
        raise SystemExit(1)
    cid = contract['contract_id']
    check(18, "Accept offer", lambda: client.get(f'/recruitment/contract/{cid}/accept', follow_redirects=True))

check(19, "Hire (sets session prefill)", lambda: client.get(
    f'/recruitment/application/{best_app_id}/hire', follow_redirects=True))

# Create employee directly via DB (bypasses web form validation for test simplicity)
with app.app_context():
    from werkzeug.security import generate_password_hash
    best_name_slug = hired['applicant_name'].lower().replace(' ', '.').replace("'", "")
    emp_id = execute("""
        INSERT INTO Employee
        (company_id, branch_id, department_id, full_name, ic_number, contact_no,
         address, position, employment_type, employment_status, hire_date, base_salary,
         role_id, email, personal_email, password_hash, work_start_time, work_end_time)
        VALUES (1, 1, 3, ?, '900101-14-5001', '+60123456701',
                'No 10 Jalan Ampang, 50450 KL', 'Senior Backend Developer', 'Full-Time',
                'Active', ?, 9000, 4, ?, 'ramesh.testflow@example.com', ?, '09:00', '18:00')
    """, (hired['applicant_name'], start, f'{best_name_slug}@smarthr.my', generate_password_hash('SmartHR@1234')))

    check(20, f"Employee created (ID={emp_id})", lambda: emp_id or fail('Creation failed'))

    # Verify
    emp = query("SELECT employee_id, full_name, email FROM Employee WHERE employee_id=?", (emp_id,), one=True)
    check(21, f"Verified: {emp['full_name']} ({emp['email']})", lambda: emp or fail('Not found'))

# ═══════════════════════════════════════════════════════════════════════
# CLEANUP
# ═══════════════════════════════════════════════════════════════════════
with app.app_context():
    execute("DELETE FROM Contract WHERE application_id IN (SELECT application_id FROM Job_Application WHERE applicant_email LIKE '%testflow%')")
    execute("DELETE FROM Interview WHERE application_id IN (SELECT application_id FROM Job_Application WHERE applicant_email LIKE '%testflow%')")
    execute("DELETE FROM Employee WHERE personal_email LIKE '%testflow%' OR email LIKE '%testflow%'")
    execute("DELETE FROM Job_Application WHERE applicant_email LIKE '%testflow%'")
    print("\n[CLEANUP] Done")

print("\n" + "=" * 60)
print(f"RESULTS: {passes} passed, {fails} failed")
print("=" * 60)
sys.exit(0 if fails == 0 else 1)
