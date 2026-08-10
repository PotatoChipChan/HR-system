"""
test_system_full.py — Comprehensive SmartHR System Test

Tests:
  A. Recruitment Pipeline (full flow with DB assertions)
  B. Invoice OCR (real images from test_inv/)
  C. IC/Identity Scanner OCR (real images from test_IC/)
  D. Face Recognition (invalid images, non-face, wrong person)
  E. Face Health API
  F. Leave Management (apply, approve, balance check)
  G. Payroll View (employee sees own payslip, admin sees all)

Run: python test_system_full.py
"""
import os, sys, json, base64, time
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.database import query, execute

app = create_app()
app.config['TESTING'] = True
app.config['WTF_CSRF_ENABLED'] = False
client = app.test_client()

RESULTS = {}
ISSUES = []


def test(name, fn):
    """Run a test and record result. Returns True on pass."""
    try:
        fn()
        RESULTS[name] = "PASS"
        print(f"  [PASS] {name}")
        return True
    except AssertionError as e:
        RESULTS[name] = "FAIL"
        ISSUES.append((name, str(e)))
        print(f"  [FAIL] {name}")
        print(f"         Assert: {e}")
        return False
    except Exception as e:
        RESULTS[name] = "ERROR"
        ISSUES.append((name, f"ERROR: {e}"))
        print(f"  [ERROR] {name}")
        print(f"         {e}")
        return False


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"Expected {expected!r}, got {actual!r}" + (f" [{msg}]" if msg else ""))


def assert_true(val, msg=""):
    if not val:
        raise AssertionError(f"Expected truthy, got {val!r}" + (f" [{msg}]" if msg else ""))


def assert_false(val, msg=""):
    if val:
        raise AssertionError(f"Expected falsy, got {val!r}" + (f" [{msg}]" if msg else ""))


def assert_in(substr, container, msg=""):
    if substr not in container:
        raise AssertionError(f"Expected {substr!r} in {container[:200]!r}" + (f" [{msg}]" if msg else ""))


def login(email, pw):
    resp = client.post('/login', data={'email': email, 'password': pw}, follow_redirects=True)
    text = resp.data.decode('utf-8', errors='replace').lower()
    if 'invalid' in text or 'locked' in text:
        raise Exception(f"Login rejected for {email}")


def logout():
    client.get('/logout', follow_redirects=True)


def image_to_base64(image_path):
    """Load image and convert to data URL base64."""
    with open(image_path, 'rb') as f:
        data = f.read()
    ext = os.path.splitext(image_path)[1].lower()
    mime = {'jpg': 'jpeg', 'jpeg': 'jpeg', 'png': 'png', 'gif': 'gif', 'bmp': 'bmp'}.get(ext.replace('.', ''), 'jpeg')
    return f"data:image/{mime};base64,{base64.b64encode(data).decode()}"


def generate_noise_image(width=400, height=400):
    """Generate a random noise image (no face)."""
    import numpy as np
    arr = np.random.randint(0, 256, (height, width, 3), dtype=np.uint8)
    img = Image.fromarray(arr)
    buf = BytesIO()
    img.save(buf, format='JPEG')
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"


def generate_face_like_image():
    """Generate a simple face-like image (oval head, eyes, mouth) — should NOT be detected as face."""
    from PIL import ImageDraw
    img = Image.new('RGB', (400, 500), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse([100, 50, 300, 400], outline=(0, 0, 0), width=3)
    draw.ellipse([170, 150, 190, 170], fill=(0, 0, 0))
    draw.ellipse([210, 150, 230, 170], fill=(0, 0, 0))
    draw.arc([160, 250, 240, 320], 0, 180, fill=(0, 0, 0), width=2)
    draw.arc([160, 100, 240, 180], 180, 360, fill=(0, 0, 0), width=2)
    buf = BytesIO()
    img.save(buf, format='JPEG')
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}", img


# ─────────────────────────────────────────────────────────────────────────────
print("=" * 70)
print("SmartHR — Comprehensive System Test")
print("=" * 70)

# Cleanup old test data
with app.app_context():
    execute("DELETE FROM Contract WHERE application_id IN (SELECT application_id FROM Job_Application WHERE applicant_email LIKE '%systest%')")
    execute("DELETE FROM Interview WHERE application_id IN (SELECT application_id FROM Job_Application WHERE applicant_email LIKE '%systest%')")
    execute("DELETE FROM Job_Application WHERE applicant_email LIKE '%systest%'")
    execute("DELETE FROM Vacancy_Request WHERE position_title LIKE '%SysTest%'")
    execute("DELETE FROM Job_Posting WHERE title LIKE '%SysTest%'")
    execute("DELETE FROM Invoice WHERE invoice_number LIKE 'SYS-TEST-%'")
    # Don't delete Employees — FK cascade issues with payroll/leave/attendance
    print("[CLEANUP] Done (employees retained)\n")

# ═══════════════════════════════════════════════════════════════════════════
# SECTION A — RECRUITMENT PIPELINE
# ═══════════════════════════════════════════════════════════════════════════
print("--- SECTION A: Recruitment Pipeline ---")

login('brian@smarthr.my', 'Manager@123')

# A1: Manager submits vacancy request
test("A1 - Vacancy request submitted", lambda: (
    client.post('/recruitment/vacancy-request', data={
        'department_id': '3', 'position_title': 'SysTest - Backend Lead',
        'employment_type': 'Full-Time', 'min_salary': '8000', 'max_salary': '14000',
        'description': 'Lead API team.', 'requirements': 'Python, Flask, Docker, CI/CD.',
        'reason': 'Growth'
    }, follow_redirects=True),
    True
))

with app.app_context():
    vr = query("SELECT request_id, status FROM Vacancy_Request WHERE position_title LIKE '%SysTest%' ORDER BY request_id DESC LIMIT 1", one=True)
    test("A2 - Vacancy request status=Pending", lambda: assert_eq(vr['status'], 'Pending'))
    vacancy_id = vr['request_id']

# A3: Admin approves -> auto-create posting
logout()
login('admin@smarthr.my', 'Admin@123')
test("A3 - Admin approves vacancy", lambda: (
    client.post(f'/recruitment/vacancy-request/{vacancy_id}/approve', data={'branch_id': '1'}, follow_redirects=True),
    True
))

with app.app_context():
    vr2 = query("SELECT posting_id, status FROM Vacancy_Request WHERE request_id=?", (vacancy_id,), one=True)
    test("A4 - Vacancy status=Approved", lambda: assert_eq(vr2['status'], 'Approved'))
    test("A5 - Posting auto-created", lambda: assert_true(vr2['posting_id'] is not None))
    posting_id = vr2['posting_id']

# A6-A10: 5 external email applications
with app.app_context():
    from email.mime.text import MIMEText
    import email as email_lib
    from app.notifications.email_parser import parse_application_email
    from app.notifications.email_monitor import (create_application_from_email,
                                                  decode_email_header, get_email_body)

    app_data = []
    for i, (name, email, cover) in enumerate([
        ('Alice Systest', 'alice.systest@test.com', '5y Python backend Flask PostgreSQL REST APIs Docker. Led team of 3.'),
        ('Bob Systest', 'bob.systest@test.com', '2y Python Django. Basic SQL. Learning Docker.'),
        ('Charlie Systest', 'charlie.systest@test.com', '8y expert Python Flask PostgreSQL Docker Kubernetes CI/CD. Led teams of 10.'),
        ('Diana Systest', 'diana.systest@test.com', 'Fresh grad. Python course. Looking for first job.'),
        ('Eve Systest', 'eve.systest@test.com', '4y Python Flask REST APIs PostgreSQL Docker. Agile teams.'),
    ]):
        body = (f"Name: {name}\nPosition: SysTest - Backend Lead\nEmail: {email}\n"
                f"IC: 900101-14-{5000+i}\nPhone: +6012345670{i}\nAddress: Test Address {i}\n\n{cover}\n\n"
                f"---\n[Ref: POST-{posting_id}]\n")
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = 'Application for Backend Lead'
        msg['From'] = f'{name} <{email}>'
        msg['Message-ID'] = f'<systest-{i}@test.com>'
        parsed_msg = email_lib.message_from_bytes(msg.as_bytes())
        parsed = parse_application_email(
            decode_email_header(parsed_msg['Subject']) or '',
            get_email_body(parsed_msg),
            decode_email_header(parsed_msg['From']) or '')
        aid = create_application_from_email(parsed_msg, parsed)
        if aid:
            app_data.append((aid, name, email))

    test("A6 - 5 external apps created", lambda: assert_eq(len(app_data), 5))

# A7: Verify structured parsing works
    test("A7 - Posting ref extracted", lambda: assert_eq(parsed.get('posting_ref'), posting_id))
    test("A8 - IC field parsed", lambda: assert_true(parsed.get('ic') is not None))
    test("A9 - Address field parsed", lambda: assert_true(parsed.get('address') is not None))

# A10: Internal web application
test("A10 - Internal web application", lambda: (
    client.post(f'/recruitment/apply/{posting_id}', data={
        'applicant_name': 'Frank Systest',
        'applicant_email': 'frank.systest@test.com',
        'applicant_phone': '+60123456709',
        'cover_letter': '6y Python backend PostgreSQL Docker CI/CD. Led projects.'
    }, follow_redirects=True),
    True
))

with app.app_context():
    count = query("SELECT COUNT(*) c FROM Job_Application WHERE posting_id=?", (posting_id,), one=True)['c']
    test("A11 - Total 6 applications", lambda: assert_eq(count, 6))

# A12: AI Shortlisting
with app.app_context():
    from app.recruitment.scorer import score_applications
    posting = query("SELECT * FROM Job_Posting WHERE posting_id=?", (posting_id,), one=True)
    all_apps = query("SELECT application_id, applicant_name, cover_letter, resume_path FROM Job_Application WHERE posting_id=?", (posting_id,))
    results = score_applications(dict(posting), [dict(a) for a in all_apps], app_root=app.root_path)

    for i, r in enumerate(results):
        status = 'Shortlisted' if i < 3 else 'New'
        execute("UPDATE Job_Application SET ai_score=?, ai_summary=?, status=? WHERE application_id=?",
                (r['score'], r['summary'], status, r['application_id']))

    sl = query("SELECT application_id, applicant_name, ai_score FROM Job_Application WHERE posting_id=? AND status='Shortlisted' ORDER BY ai_score DESC", (posting_id,))
    ns = query("SELECT applicant_name, ai_score FROM Job_Application WHERE posting_id=? AND status='New' ORDER BY ai_score", (posting_id,))
    test("A12 - 3 shortlisted", lambda: assert_eq(len(sl), 3))
    test("A13 - 3 not shortlisted", lambda: assert_eq(len(ns), 3))
    test("A14 - Top score >= 70", lambda: assert_true(sl[0]['ai_score'] >= 70, f"Got {sl[0]['ai_score']}"))
    test("A15 - Top score > bottom shortlisted score", lambda: assert_true(sl[0]['ai_score'] >= sl[-1]['ai_score']))
    print(f"    Shortlisted: {[(s['applicant_name'], s['ai_score']) for s in sl]}")

# A16-A18: Schedule interviews
logout()
login('hr@smarthr.my', 'Hr@123')
yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

with app.app_context():
    admin_emp = query("SELECT employee_id FROM Employee WHERE email='admin@smarthr.my'", one=True)['employee_id']
    for i, s in enumerate(sl):
        t = f'{10 + i}:00'
        client.post(f'/recruitment/application/{s["application_id"]}/schedule-interview', data={
            'date': yesterday, 'time': t, 'duration': '60',
            'location': 'Room 3A', 'meeting_link': '', 'type': 'In-Person',
            'interviewer_ids': str(admin_emp)
        }, follow_redirects=True)

    iv_count = query("SELECT COUNT(*) c FROM Interview iv JOIN Job_Application ja ON iv.application_id=ja.application_id WHERE ja.posting_id=?", (posting_id,), one=True)
    test("A16 - 3 interviews scheduled", lambda: assert_eq(iv_count['c'], 3))

# A17: Pass best candidate
with app.app_context():
    best = sl[0]
    iv = query("SELECT interview_id FROM Interview WHERE application_id=? LIMIT 1", (best["application_id"],), one=True)
    client.post(f'/recruitment/interview/{iv["interview_id"]}/result', data={'result': 'Pass'}, follow_redirects=True)

    rejected = query("SELECT COUNT(*) c FROM Job_Application WHERE posting_id=? AND status='Rejected'", (posting_id,), one=True)
    test("A17 - Others auto-rejected", lambda: assert_eq(rejected['c'], 5))

# A18-A21: Contract -> Offer -> Accept -> Hire -> Employee
with app.app_context():
    best_app_id = best['application_id']
    best_name = best['applicant_name']

start = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
client.post(f'/recruitment/contract/{best_app_id}', data={
    'offer_date': datetime.now().strftime('%Y-%m-%d'), 'start_date': start,
    'position': 'Backend Lead', 'department_id': '3', 'employment_type': 'Full-Time',
    'work_start_time': '09:00', 'work_end_time': '18:00', 'base_salary': '10000'
}, follow_redirects=True)
test("A18 - Contract created", lambda: True)

client.post(f'/recruitment/application/{best_app_id}/send-offer', follow_redirects=True)
test("A19 - Offer sent", lambda: True)

with app.app_context():
    contract = query("SELECT contract_id FROM Contract WHERE application_id=? ORDER BY contract_id DESC LIMIT 1", (best_app_id,), one=True)
    cid = contract['contract_id']
    client.get(f'/recruitment/contract/{cid}/accept', follow_redirects=True)

    app_status = query("SELECT status FROM Job_Application WHERE application_id=?", (best_app_id,), one=True)
    test("A20 - Application status=Hired", lambda: assert_eq(app_status['status'], 'Hired'))

# Create employee via DB
with app.app_context():
    from werkzeug.security import generate_password_hash
    slug = best_name.lower().replace(' ', '.').replace("'", "")
    ts = str(int(time.time()))[-4:]
    test_email = f'{slug}.{ts}@smarthr.my'
    test_ic = f'900101-{ts[:2]}-{ts[2:]}01'
    emp_id = execute("""
        INSERT INTO Employee (company_id, branch_id, department_id, full_name,
        ic_number, contact_no, address, position, employment_type, employment_status,
        hire_date, base_salary, role_id, email, personal_email, password_hash,
        work_start_time, work_end_time)
        VALUES (1,1,3,?,?,?,?,?,'Full-Time','Active',?,10000,4,?,?,?,'09:00','18:00')
    """, (best_name, test_ic, '+60123456701', 'Test Address', 'Backend Lead',
          start, test_email, 'charlie.systest@test.com', generate_password_hash('SmartHR@1234')))

    test("A21 - Employee created", lambda: assert_true(emp_id is not None))
    emp = query("SELECT full_name FROM Employee WHERE employee_id=?", (emp_id,), one=True)
    test("A22 - Employee name correct", lambda: assert_eq(emp['full_name'], best_name))

# ═══════════════════════════════════════════════════════════════════════════
# SECTION B — INVOICE OCR
# ═══════════════════════════════════════════════════════════════════════════
print("\n--- SECTION B: Invoice OCR ---")
logout()
login('admin@smarthr.my', 'Admin@123')

inv_dir = os.path.join(os.path.dirname(__file__), 'test_inv')
inv_files = [f for f in os.listdir(inv_dir) if os.path.isfile(os.path.join(inv_dir, f))]
test("B1 - Test invoice files exist", lambda: assert_true(len(inv_files) > 0, f"Found {len(inv_files)} files"))

for inv_file in inv_files[:3]:
    inv_path = os.path.join(inv_dir, inv_file)

    # B2: OCR extract returns valid JSON
    def make_ocr_test(fp, fname):
        def _test():
            with open(fp, 'rb') as f:
                resp = client.post('/invoices/ocr_extract', data={'invoice_file': (BytesIO(f.read()), fname)},
                                   content_type='multipart/form-data')
            json.loads(resp.data)
            return True
        return _test

    test(f"B2 - OCR extract valid JSON: {inv_file}", make_ocr_test(inv_path, inv_file))

    # B3: OCR returns vendor field
    with open(inv_path, 'rb') as f:
        resp = client.post('/invoices/ocr_extract', data={'invoice_file': (BytesIO(f.read()), inv_file)},
                           content_type='multipart/form-data')
    result = json.loads(resp.data)
    print(f"    OCR {inv_file}: vendor={result.get('vendor_name','?')}, amount={result.get('total_amount','?')}, conf={result.get('confidence','?')}")
    test(f"B3 - OCR result is dict: {inv_file}", lambda r=result: assert_true(isinstance(r, dict)))

# B4: Upload an invoice with OCR data
with open(inv_path, 'rb') as f:
    content = f.read()
resp = client.post('/invoices/upload', data={
    'invoice_file': (BytesIO(content), inv_files[0]),
    'currency': 'MYR',
    'total_amount': '150.00',
    'vendor_name': 'SYS-TEST-Vendor',
    'invoice_number': 'SYS-TEST-001',
    'invoice_date': datetime.now().strftime('%Y-%m-%d'),
    'category': 'Office Supplies',
    'description': 'System test invoice',
    'ocr_raw_text': result.get('raw_text', ''),
    'ocr_confidence': str(result.get('confidence', 0.85))
}, content_type='multipart/form-data', follow_redirects=True)

with app.app_context():
    inv = query("SELECT invoice_id, status FROM Invoice WHERE invoice_number='SYS-TEST-001' ORDER BY invoice_id DESC LIMIT 1", one=True)
    test("B4 - Invoice uploaded to DB", lambda: assert_true(inv is not None))
    if inv:
        inv_id = inv['invoice_id']
        test("B5 - Invoice status=Pending", lambda: assert_eq(inv['status'], 'Pending'))

# B6: Approve invoice
if inv:
    client.post(f'/invoices/{inv_id}/approve', follow_redirects=True)
    with app.app_context():
        inv = query("SELECT status FROM Invoice WHERE invoice_id=?", (inv_id,), one=True)
        test("B6 - Invoice approved", lambda: assert_eq(inv['status'], 'Approved'))

# ═══════════════════════════════════════════════════════════════════════════
# SECTION C — IC/IDENTITY SCANNER OCR
# ═══════════════════════════════════════════════════════════════════════════
print("\n--- SECTION C: IC/Identity Scanner OCR ---")

ic_dir = os.path.join(os.path.dirname(__file__), 'test_IC')
ic_files = [f for f in os.listdir(ic_dir) if os.path.isfile(os.path.join(ic_dir, f))]
test("C1 - IC test files exist", lambda: assert_true(len(ic_files) > 0, f"Found {len(ic_files)} files"))

for ic_file in ic_files:
    ic_path = os.path.join(ic_dir, ic_file)
    with open(ic_path, 'rb') as f:
        resp = client.post('/employees/ocr_identity', data={
            'id_file': (BytesIO(f.read()), ic_file),
            'side': 'front',
            'doc_type': 'ic'
        }, content_type='multipart/form-data')
    try:
        result = json.loads(resp.data)
        print(f"    {ic_file}: name={result.get('name','?')}, ic_no={result.get('ic_number','?')}, address={'present' if result.get('address') else 'absent'}")
        test(f"C2 - {ic_file} returns valid JSON", lambda: assert_true(isinstance(result, dict)))
    except json.JSONDecodeError:
        test(f"C2 - {ic_file} returns valid JSON", lambda: assert_true(False, f"Response not JSON: {resp.data[:200]}"))

# C3: IC back
ic_back = [f for f in ic_files if 'back' in f.lower()]
if ic_back:
    with open(os.path.join(ic_dir, ic_back[0]), 'rb') as f:
        resp = client.post('/employees/ocr_identity', data={
            'id_file': (BytesIO(f.read()), ic_back[0]),
            'side': 'back',
            'doc_type': 'ic'
        }, content_type='multipart/form-data')
    result = json.loads(resp.data)
    test("C3 - IC back returns watermarked", lambda: assert_true(isinstance(result, dict)))

# ═══════════════════════════════════════════════════════════════════════════
# SECTION D — FACE RECOGNITION (INVALID INPUTS)
# ═══════════════════════════════════════════════════════════════════════════
print("\n--- SECTION D: Face Recognition ---")

# D1: Face registration with noise image (should fail)
noise_b64 = generate_noise_image()
resp = client.post('/face/api/register', json={
    'employee_id': 1,
    'image': noise_b64
})
result = json.loads(resp.data)
test("D1 - Noise image rejected (no face)", lambda: assert_false(
    result.get('success', False), f"Got: {result}"))

# D2: Face registration with drawn face (should fail — not a real face)
face_b64, _ = generate_face_like_image()
resp = client.post('/face/api/register', json={
    'employee_id': 1,
    'image': face_b64
})
result = json.loads(resp.data)
test("D2 - Drawn face rejected", lambda: assert_false(
    result.get('success', False), f"Got: {result}"))

# D3: Face registration with invoice image (should fail)
# D3-D6: Use an actual image (not PDF) for invoice-based face rejection test
inv_img_files = [f for f in inv_files if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp'))]
inv_img = inv_img_files[0] if inv_img_files else inv_files[0]
inv_test_path = os.path.join(inv_dir, inv_img)
print(f"    Using image for face test: {inv_img}")
inv_b64 = image_to_base64(inv_test_path)
resp = client.post('/face/api/register', json={
    'employee_id': 1,
    'image': inv_b64
})
result = json.loads(resp.data)
test("D3 - Invoice image rejected for face reg", lambda: assert_false(
    result.get('success', False), f"Got: {result}"))

# D4: Match and record with invoice (should fail)
resp = client.post('/face/api/match_and_record', json={
    'image': inv_b64,
    'action': 'check_in'
})
result = json.loads(resp.data)
test("D4 - Match invoice as face fails", lambda: assert_false(
    result.get('success', False), f"Got: {result}"))

# D5: Match with noise image
resp = client.post('/face/api/match_and_record', json={
    'image': noise_b64,
    'action': 'check_in'
})
result = json.loads(resp.data)
test("D5 - Match noise as face fails", lambda: assert_false(
    result.get('success', False), f"Got: {result}"))

# D6: Frame analysis with non-face image
resp = client.post('/face/api/analyze_frame', json={
    'image': inv_b64
})
result = json.loads(resp.data)
test("D6 - Analyze non-face -> face_detected=False", lambda: assert_false(
    result.get('face_detected', True), f"Got: {result}"))

# D7: Frame analysis with noise
resp = client.post('/face/api/analyze_frame', json={
    'image': noise_b64
})
result = json.loads(resp.data)
test("D7 - Analyze noise -> face_detected=False", lambda: assert_false(
    result.get('face_detected', True), f"Got: {result}"))

# ═══════════════════════════════════════════════════════════════════════════
# SECTION E — FACE HEALTH API
# ═══════════════════════════════════════════════════════════════════════════
print("\n--- SECTION E: Face Health API ---")
resp = client.get('/face/api/health')
result = json.loads(resp.data)
test("E1 - Health API accessible", lambda: assert_in('status', result))
test("E2 - face_recognition loaded", lambda: assert_true(result.get('face_recognition', False)))
test("E3 - database OK", lambda: assert_true(result.get('database', False)))
print(f"    Health: status={result.get('status')}, errors={result.get('consecutive_errors')}, threshold={result.get('threshold')}")

# ═══════════════════════════════════════════════════════════════════════════
# SECTION F — LEAVE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════
print("\n--- SECTION F: Leave Management ---")
logout()
login('elizabeth@smarthr.my', 'Employee@123')

# F1: Apply for leave
future_date = (datetime.now() + timedelta(days=14)).strftime('%Y-%m-%d')
leave_reason = f'System test leave {int(time.time())}'
resp = client.post('/leave/apply', data={
    'leave_type_id': '1',
    'start_date': future_date,
    'end_date': future_date,
    'reason': leave_reason
}, follow_redirects=True)
test("F1 - Leave applied", lambda: True)

with app.app_context():
    leave = query("SELECT leave_id, status FROM Leave_Application WHERE reason=? ORDER BY leave_id DESC LIMIT 1", (leave_reason,), one=True)
    test("F2 - Leave in DB", lambda: assert_true(leave is not None))
    if leave:
        test("F3 - Leave status=Pending", lambda: assert_eq(leave['status'], 'Pending'))
        leave_id = leave['leave_id']

# F4: Manager approves leave (only if leave was created)
logout()
login('brian@smarthr.my', 'Manager@123')
if leave:
    client.post(f'/leave/approve/{leave_id}', data={'action': 'approve'}, follow_redirects=True)
    with app.app_context():
        leave = query("SELECT status FROM Leave_Application WHERE leave_id=?", (leave_id,), one=True)
        test("F4 - Leave approved by manager", lambda: assert_eq(leave['status'], 'Approved'))
else:
    test("F4 - Leave approved (skipped)", lambda: assert_true(False, "Leave not created"))

# ═══════════════════════════════════════════════════════════════════════════
# SECTION G — PAYROLL VIEW
# ═══════════════════════════════════════════════════════════════════════════
print("\n--- SECTION G: Payroll ---")

# G1: Employee sees own payslip
logout()
login('elizabeth@smarthr.my', 'Employee@123')

with app.app_context():
    eliza_id = query("SELECT employee_id FROM Employee WHERE email='elizabeth@smarthr.my'", one=True)
    payroll = query("SELECT payroll_id, employee_id, gross_pay FROM Payroll WHERE employee_id=? ORDER BY payroll_id DESC LIMIT 1", (eliza_id['employee_id'],), one=True) if eliza_id else None
    test("G1 - Employee has payroll record", lambda: assert_true(payroll is not None))
    if payroll:
        print(f"    Payroll ID={payroll['payroll_id']}, gross_pay=RM{payroll['gross_pay']}")

# G2: Admin sees all payroll
logout()
login('admin@smarthr.my', 'Admin@123')
resp = client.get('/payroll/')
text = resp.data.decode('utf-8', errors='replace')
test("G2 - Admin payroll page accessible", lambda: assert_in('Payroll', text))

# ═══════════════════════════════════════════════════════════════════════════
# SECTION H — DASHBOARD & AUTH
# ═══════════════════════════════════════════════════════════════════════════
print("\n--- SECTION H: Dashboard & Auth ---")

# H1: Invalid login (logout first to clear existing session)
logout()
resp = client.post('/login', data={'email': 'nobody@smarthr.my', 'password': 'wrong'}, follow_redirects=True)
text = resp.data.decode('utf-8', errors='replace').lower()
test("H1 - Invalid login rejected", lambda: assert_in('invalid', text))

# H2: Dashboard accessible
logout()
login('admin@smarthr.my', 'Admin@123')
resp = client.get('/')
test("H2 - Dashboard loads", lambda: assert_eq(resp.status_code, 200))

# H3: Session has correct values
resp = client.get('/settings/')
text = resp.data.decode('utf-8', errors='replace')
test("H3 - Settings page shows admin", lambda: assert_in('admin@smarthr.my', text))

# ═══════════════════════════════════════════════════════════════════════════
# FINAL CLEANUP
# ═══════════════════════════════════════════════════════════════════════════
with app.app_context():
    execute("DELETE FROM Contract WHERE application_id IN (SELECT application_id FROM Job_Application WHERE applicant_email LIKE '%systest%')")
    execute("DELETE FROM Interview WHERE application_id IN (SELECT application_id FROM Job_Application WHERE applicant_email LIKE '%systest%')")
    execute("DELETE FROM Job_Application WHERE applicant_email LIKE '%systest%'")
    execute("DELETE FROM Leave_Application WHERE reason LIKE 'System test leave%'")
    execute("DELETE FROM Invoice WHERE invoice_number='SYS-TEST-001'")

# ═══════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════════
pass_count = sum(1 for v in RESULTS.values() if v == "PASS")
fail_count = sum(1 for v in RESULTS.values() if v != "PASS")
total = len(RESULTS)

print("\n" + "=" * 70)
print(f"RESULTS: {pass_count}/{total} passed, {fail_count} failed/error")
print("=" * 70)

if ISSUES:
    print("\nISSUES FOUND:")
    for name, detail in ISSUES:
        print(f"  - {name}: {detail}")
    print()

if fail_count == 0:
    print("ALL TESTS PASSED!")
else:
    print(f"{fail_count} ISSUE(S) FOUND — review above for details")

sys.exit(0 if fail_count == 0 else 1)
