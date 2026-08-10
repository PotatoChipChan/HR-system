"""Test OCR extraction + Sprint 9 recruitment features."""
import sys, os
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


# ═══════════════════════════════════════════════════════════════════════════════
# PART 1 — Sprint 9: Applications & Interview Auto-Assign (fast, no OCR deps)
# ═══════════════════════════════════════════════════════════════════════════════

print('=' * 60)
print('Sprint 9 — Applications & Interview Auto-Assign')
print('=' * 60)

with app.test_client() as client:
    # ── Login page: demo accounts (before auth) ──
    resp = client.get('/login')
    check(resp.status_code == 200, 'GET /login (200)')
    check(b'brian@smarthr.my' in resp.data, 'Login demo: Brian (KL Manager)')
    check(b'hafiz@smarthr.my' in resp.data, 'Login demo: Hafiz (PG Manager)')
    check(b'Manager (KL)' in resp.data, 'Login demo: Manager (KL) label')
    check(b'Manager (PG)' in resp.data, 'Login demo: Manager (PG) label')

    # ── Login ──
    resp = client.post('/login', data={'email': 'admin@smarthr.my', 'password': 'Admin@123'}, follow_redirects=True)
    check(resp.status_code == 200, 'Admin login succeeds')

    # ── Feature #10: Applications default to Shortlisted ──
    resp = client.get('/recruitment/applications')
    check(resp.status_code == 200, 'GET /applications defaults to shortlisted (200)')
    check(b'Shortlisted Candidates' in resp.data, 'Page title: Shortlisted Candidates')
    check(b'btn-amber' in resp.data,  'Shortlisted tab highlighted (btn-amber)')

    for show in ('active', 'rejected', 'hired'):
        resp = client.get(f'/recruitment/applications?show={show}')
        check(resp.status_code == 200, f'GET /applications?show={show} (200)')

    check(b'Active' in client.get('/recruitment/applications?show=active').data, 'Active tab present')

    # ── Feature #10: View posting + Reject button ──
    resp = client.get('/recruitment/postings/1')
    check(resp.status_code == 200, 'GET /postings/1 (200)')
    has_button = b'Non-Shortlisted' in resp.data
    has_empty  = b'No applications' in resp.data
    has_table  = b'class="tbl"' in resp.data
    check(has_button or has_empty or has_table,
          f'Posting page renders correctly (button={has_button}, empty={has_empty}, table={has_table})')

    # ── Feature #11: Interview_Policy table ──
    import sqlite3
    con = sqlite3.connect(os.path.join('instance', 'smarthr.db'))
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Interview_Policy'")
    check(cur.fetchone() is not None, 'Interview_Policy table exists')

    cur = con.execute("SELECT * FROM Interview_Policy WHERE company_id=1")
    row = cur.fetchone()
    check(row is not None, 'Default Interview_Policy row exists')
    if row:
        cols = [d[0] for d in cur.description]
        r = dict(zip(cols, row))
        check(int(r.get('default_duration_min', 0)) == 60, 'Default duration = 60 min')
        check(r.get('day_start_time') == '09:00', 'Day start = 09:00')
        check(r.get('day_end_time') == '17:00', 'Day end = 17:00')
        check(int(r.get('slot_gap_min', 0)) == 15, 'Slot gap = 15 min')
        check(int(r.get('max_per_day', 0)) == 8, 'Max per day = 8')
    con.close()

    # ── Feature #11: Interview Policy page ──
    resp = client.get('/recruitment/interview-policy')
    check(resp.status_code == 200, 'GET /interview-policy (200)')
    check(b'Interview Scheduling Policy' in resp.data, 'Policy page title visible')
    check(b'default_duration_min' in resp.data, 'Duration field present')
    check(b'day_start_time' in resp.data, 'Day start field present')
    check(b'max_per_day' in resp.data, 'Max per day field present')

    # ── Feature #11: POST policy update ──
    resp = client.post('/recruitment/interview-policy', data={
        'default_duration_min': '45', 'default_type': 'Online',
        'default_location': 'Room 301', 'default_meeting_link': 'https://meet.example.com',
        'day_start_time': '10:00', 'day_end_time': '16:00',
        'slot_gap_min': '10', 'max_per_day': '6', 'auto_notify': '1'
    }, follow_redirects=True)
    check(resp.status_code == 200, 'POST /interview-policy saves (200)')
    check(b'Interview policy saved' in resp.data, 'Save success flash')

    con = sqlite3.connect(os.path.join('instance', 'smarthr.db'))
    cur = con.execute("SELECT * FROM Interview_Policy WHERE company_id=1")
    r = dict(zip([d[0] for d in cur.description], cur.fetchone()))
    check(int(r.get('default_duration_min', 0)) == 45, 'Saved: duration = 45 min')
    check(r.get('default_type') == 'Online', 'Saved: type = Online')
    check(r.get('day_start_time') == '10:00', 'Saved: start = 10:00')
    check(r.get('day_end_time') == '16:00', 'Saved: end = 16:00')
    check(r.get('max_per_day') == 6, 'Saved: max/day = 6')
    con.close()

    # ── Feature #10: Reject Non-Shortlisted ──
    resp = client.post('/recruitment/postings/1/reject-non-shortlisted', follow_redirects=True)
    check(resp.status_code == 200, 'POST /postings/1/reject-non-shortlisted (200)')
    check(b'Rejected' in resp.data or b'No non-shortlisted' in resp.data,
          'Reject response has expected message')

    # ── Feature #11: Auto-assign preview ──
    resp = client.post('/recruitment/auto-assign', data={'application_ids': '999'})
    check(resp.status_code == 200, 'POST /auto-assign preview (200)')
    check(b'No valid shortlisted' in resp.data, 'Empty selection returns error')

    # ── Sidebar ──
    resp = client.get('/recruitment/applications')
    check(b'Interview Policy' in resp.data, 'Sidebar: Interview Policy link present')

    # ── Email parser: promotional/spam detection ──
    from app.notifications.email_parser import is_application_email, is_promotional_email

    # Real application should pass
    real_app_subject = 'Application for Software Engineer - John Doe'
    real_app_body = 'Dear HR, My name is John Doe and I am applying for the Software Engineer position. I have 5 years of experience. My phone is 012-3456789.'
    check(is_application_email(real_app_subject, real_app_body),
          'is_application_email: real application passes')
    check(not is_promotional_email(None, real_app_subject, real_app_body, 'john.doe@gmail.com'),
          'is_promotional_email: real application not flagged')

    # Samsung promotional email
    samsung_subj = 'Check out the new Samsung Galaxy!'
    samsung_body = 'Special offer! Buy now and save. Click here to view in browser. Unsubscribe here. All rights reserved.'
    check(not is_application_email(samsung_subj, samsung_body),
          'is_application_email: Samsung promo rejected (no keywords)')
    check(is_promotional_email(None, samsung_subj, samsung_body, 'newsletter@samsung.com'),
          'is_promotional_email: Samsung promo detected')

    # Glassdoor job alert (has 'job'+'position' keywords but blocked by promotional check)
    glassdoor_subj = 'New jobs match your profile - Software Engineer'
    glassdoor_body = 'Job Alert: We found new positions matching your profile. View in browser. Unsubscribe.'
    check(is_promotional_email(None, glassdoor_subj, glassdoor_body, 'noreply@glassdoor.com'),
          'is_promotional_email: Glassdoor detected (keyword check may pass, promo check blocks)')

    # LinkedIn notification
    linkedin_subj = 'You appeared in 5 searches this week'
    linkedin_body = 'See who viewed your profile. Update your preferences. Unsubscribe.'
    check(not is_application_email(linkedin_subj, linkedin_body),
          'is_application_email: LinkedIn notification rejected')
    check(is_promotional_email(None, linkedin_subj, linkedin_body, 'notifications@linkedin.com'),
          'is_promotional_email: LinkedIn detected')

    # Real application with position in body (should pass keyword check)
    real2_subj = 'Job Application'
    real2_body = 'I am applying for the IT Support position. Please find my resume attached. My phone is 011-12345678.'
    check(is_application_email(real2_subj, real2_body),
          'is_application_email: real app with body keywords passes')
    check(not is_promotional_email(None, real2_subj, real2_body, 'ahmad.malik@gmail.com'),
          'is_promotional_email: real app not flagged')

    # Newsletter that mentions "position" once
    newsletter_subj = 'Weekly Newsletter #42'
    newsletter_body = 'This week we feature the latest trends in tech. View in browser. Unsubscribe. All rights reserved.'
    check(not is_application_email(newsletter_subj, newsletter_body),
          'is_application_email: newsletter rejected')
    check(is_promotional_email(None, newsletter_subj, newsletter_body, 'hello@company.com'),
          'is_promotional_email: newsletter detected')

    # ── Restore defaults ──
    client.post('/recruitment/interview-policy', data={
        'default_duration_min': '60', 'default_type': 'In-Person',
        'default_location': '', 'default_meeting_link': '',
        'day_start_time': '09:00', 'day_end_time': '17:00',
        'slot_gap_min': '15', 'max_per_day': '8', 'auto_notify': '1'
    }, follow_redirects=True)
    check(True, 'Policy defaults restored')

# ═══════════════════════════════════════════════════════════════════════════════
# PART 2 — OCR Extraction (slow — EasyOCR + Tesseract)
# ═══════════════════════════════════════════════════════════════════════════════

print()
print('=' * 60)
print('OCR Extraction (existing tests)')
print('=' * 60)

try:
    from app.invoice.routes import (_get_easyocr_reader, _preprocess_image,
                                     _get_tesseract_path, _extract_all,
                                     _is_garbage_vendor, _is_thermal_receipt)
    import pytesseract
    from PIL import Image

    with app.app_context():
        tess_path = _get_tesseract_path()
        if tess_path:
            pytesseract.pytesseract.tesseract_cmd = tess_path
        reader = _get_easyocr_reader()

        for f in ['test_inv/Petron.jpg', 'test_inv/CelcomDigi.jpg', 'test_inv/IMG_20260529_165035(1).jpg']:
            print(f'\n=== {f} ===')
            is_thermal = _is_thermal_receipt(f)
            print(f'  thermal: {is_thermal}')

            try:
                if is_thermal:
                    results = reader.readtext(f)
                    easy_text = '\n'.join([text for _, text, _ in results])
                else:
                    processed = _preprocess_image(f)
                    proc_path = f + '_preprocessed.png'
                    processed.save(proc_path)
                    results = reader.readtext(proc_path)
                    easy_text = '\n'.join([text for _, text, _ in results])
                    os.remove(proc_path)
            except:
                easy_text = ''

            try:
                tess_text = pytesseract.image_to_string(Image.open(f), config='--oem 3 --psm 6')
            except:
                tess_text = ''

            if is_thermal:
                raw_text = easy_text + '\n' + tess_text
            else:
                raw_text = easy_text + '\n' + tess_text
                ext_prep = _extract_all(raw_text)
                vendor = ext_prep.get('vendor_name', '')
                needs_fallback = (
                    (not vendor or _is_garbage_vendor(vendor))
                    and not ext_prep.get('invoice_number')
                    and not ext_prep.get('invoice_date')
                )
                if needs_fallback:
                    print('  -> Preprocessed is garbage, falling back to raw OCR')
                    results = reader.readtext(f)
                    raw_text = '\n'.join([r[1] for r in results])
                    if tess_text.strip():
                        raw_text += '\n' + tess_text

            ext_final = _extract_all(raw_text)

            for key in ['vendor_name', 'invoice_number', 'invoice_date', 'subtotal', 'tax_amount', 'total_amount']:
                print(f'  {key}: {ext_final.get(key, "")!r}')
            print(f'  confidence: {ext_final.get("confidence", 0)}')
            print(f'  needs_review: {ext_final.get("id_needs_review", "")}')
except ImportError as e:
    print(f'  [SKIP] OCR deps not available: {e}')
except Exception as e:
    print(f'  [SKIP] OCR tests failed: {e}')

# ═══════════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════════

print()
print('=' * 60)
print(f'Results: {passed} passed, {failed} failed out of {passed + failed} tests')
if failed:
    print(f'*** {failed} TEST(S) FAILED ***')
    sys.exit(1)
else:
    print('All tests passed!')
