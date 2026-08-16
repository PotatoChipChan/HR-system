"""app/notifications/email_monitor.py – IMAP inbox poller"""
import os
import re
import uuid
import imaplib
import email as email_lib
from datetime import datetime, timedelta
from email.header import decode_header
from flask import current_app
from app.database import query, execute, log_audit
from app.notifications.email_parser import (
    parse_application_email, detect_offer_reply, extract_contract_id,
    is_application_email, is_auto_reply, is_promotional_email, extract_email,
    extract_posting_ref, detect_reschedule_request
)

MAX_EMAILS_PER_POLL = 10
IMAP_TIMEOUT = 10


def decode_email_header(header):
    if not header:
        return ''
    parts = decode_header(header)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(charset or 'utf-8', errors='replace'))
            except LookupError:
                result.append(part.decode('utf-8', errors='replace'))
        else:
            result.append(str(part))
    return ''.join(result)


def get_email_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode('utf-8', errors='replace')
    payload = msg.get_payload(decode=True)
    if payload:
        return payload.decode('utf-8', errors='replace')
    return ''


def is_employee_email(from_hdr):
    email_addr = extract_email(from_hdr)
    if not email_addr:
        return False
    emp = query("SELECT 1 FROM Employee WHERE email=?", (email_addr,), one=True)
    return emp is not None


def save_signed_contract_attachment(msg, contract_id):
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        if part.get('Content-Disposition') is None:
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = decode_email_header(filename)
        ext = os.path.splitext(filename)[1].lower()
        if ext != '.pdf':
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        contracts_dir = os.path.join(current_app.root_path, '..', 'uploads', 'contracts')
        os.makedirs(contracts_dir, exist_ok=True)
        safe_name = f"contract_{contract_id}_signed.pdf"
        filepath = os.path.join(contracts_dir, safe_name)
        with open(filepath, 'wb') as f:
            f.write(payload)
        return filepath
    return None


def save_resume_attachment(msg, resume_dir):
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        if part.get('Content-Disposition') is None:
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = decode_email_header(filename)
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ('.pdf', '.doc', '.docx'):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        safe_name = f"resume_{uuid.uuid4().hex[:8]}{ext}"
        filepath = os.path.join(resume_dir, safe_name)
        with open(filepath, 'wb') as f:
            f.write(payload)
        return safe_name
    return None


def _match_posting_by_location(body, matches):
    """Disambiguate multiple posting matches by scanning body for location clues."""
    if not body or not matches:
        return None
    body_lower = body.lower()
    for m in matches:
        branch_name = m['branch_name'].lower()
        if branch_name in body_lower:
            return m
    for m in matches:
        branch = query("SELECT city, state FROM Branch WHERE branch_id=?", (m['branch_id'],), one=True)
        if branch:
            if branch['city'] and branch['city'].lower() in body_lower:
                return m
            if branch['state'] and branch['state'].lower() in body_lower:
                return m
    return None


def create_application_from_email(msg, parsed):
    subject = decode_email_header(msg['Subject']) or ''
    body = get_email_body(msg)
    from_hdr = decode_email_header(msg['From']) or ''
    email_addr = extract_email(from_hdr)
    if not email_addr:
        return None

    # Dedup by Message-ID (unique per email, not per sender)
    msg_id = decode_email_header(msg.get('Message-ID', '')) or decode_email_header(msg.get('Message-Id', '')) or ''
    msg_id = msg_id.strip().strip('<>')
    if msg_id:
        try:
            execute("ALTER TABLE Job_Application ADD COLUMN message_id TEXT")
        except Exception:
            pass  # column already exists
        dup = query("SELECT 1 FROM Job_Application WHERE message_id=? LIMIT 1", (msg_id,), one=True)
        if dup:
            print(f"[EMAIL MONITOR] Skipping already-processed email (Message-ID: {msg_id})")
            return None

    # Emergency contact columns for applications submitted via the template
    for col in ('emergency_contact_name', 'emergency_contact_no'):
        try:
            execute(f"ALTER TABLE Job_Application ADD COLUMN {col} TEXT")
        except Exception:
            pass  # column already exists

    posting = None
    # Priority 1: Exact posting reference from mailto: footer
    posting_ref = parsed.get('posting_ref') or extract_posting_ref(body)
    if posting_ref:
        posting = query("""
            SELECT jp.posting_id, jp.title, jp.branch_id, b.name as branch_name
            FROM Job_Posting jp
            JOIN Branch b ON jp.branch_id=b.branch_id
            WHERE jp.posting_id=? AND jp.status IN ('Open','Partially Filled')
        """, (posting_ref,), one=True)
        if posting:
            print(f"[EMAIL MONITOR] Matched application to posting #{posting_ref} via Ref tag")

    # Priority 2: Match by position name in body
    if not posting and parsed['position_raw']:
        position_str = parsed['position_raw'].lower()
        # Get ALL matching postings for branch-aware disambiguation
        matches = query("""
            SELECT jp.posting_id, jp.title, jp.branch_id, b.name as branch_name
            FROM Job_Posting jp
            JOIN Branch b ON jp.branch_id=b.branch_id
            WHERE ? LIKE '%' || lower(jp.title) || '%' AND jp.status IN ('Open','Partially Filled')
        """, (position_str,))
        if matches:
            if len(matches) == 1:
                posting = matches[0]
            else:
                posting = _match_posting_by_location(body, matches)
                if not posting:
                    # Fallback: try word matching with location
                    words = [w for w in position_str.split() if len(w) > 2]
                    for w in words:
                        sub_matches = query("""
                            SELECT jp.posting_id, jp.title, jp.branch_id, b.name as branch_name
                            FROM Job_Posting jp
                            JOIN Branch b ON jp.branch_id=b.branch_id
                            WHERE lower(jp.title) LIKE ? AND jp.status IN ('Open','Partially Filled')
                        """, (f"%{w}%",))
                        if sub_matches:
                            if len(sub_matches) == 1:
                                posting = sub_matches[0]
                                break
                            posting = _match_posting_by_location(body, sub_matches)
                            if posting:
                                break
                    if not posting:
                        print(f"[EMAIL MONITOR] Ambiguous posting match for '{position_str}': {[m['branch_name'] for m in matches]} — posting_id left NULL, HR should assign")

    resume_dir = os.path.join(current_app.root_path, '..', 'uploads', 'resumes')
    os.makedirs(resume_dir, exist_ok=True)

    applicant_name = (parsed.get('name') or '').strip() or email_addr
    # The application template's Email field wins over the sender address, so
    # applications sent from a third-party account still record the candidate.
    applicant_email = (parsed.get('email') or '').strip() or email_addr

    app_id = execute("""
        INSERT INTO Job_Application
        (posting_id, applicant_name, applicant_email, applicant_phone,
         applicant_ic, applicant_address,
         emergency_contact_name, emergency_contact_no,
         cover_letter, source, status, message_id)
        VALUES (?,?,?,?,?,?,?,?,?,'Email','New',?)
    """, (
        posting['posting_id'] if posting else None,
        applicant_name,
        applicant_email,
        parsed.get('phone'),
        parsed.get('ic'),
        parsed.get('address'),
        parsed.get('emergency_contact_name'),
        parsed.get('emergency_contact_no'),
        (body[:10000] if body else '') + ('\n\n---\n[Full email body truncated]' if body and len(body) > 10000 else ''),
        msg_id or None,
    ))

    resume_path = save_resume_attachment(msg, resume_dir)
    if resume_path:
        execute("UPDATE Job_Application SET resume_path=? WHERE application_id=?",
                (resume_path, app_id))

    try:
        log_audit('EMAIL_APPLICATION', 'Recruitment',
                  f'New application from {parsed["name"] or email_addr} via email',
                  action_details={'position': parsed['position_raw'],
                                  'application_id': app_id})
    except Exception as e:
        print(f"[EMAIL MONITOR] Audit log failed: {e}")

    # Auto AI shortlisting and screening
    if posting:
        try:
            posting_data = query("""
                SELECT title, description, requirements FROM Job_Posting WHERE posting_id=?
            """, (posting['posting_id'],), one=True)
            if posting_data:
                posting_data = dict(posting_data)
                app_data = query("""
                    SELECT application_id, applicant_name, cover_letter, resume_path
                    FROM Job_Application WHERE application_id=?
                """, (app_id,), one=True)
                if app_data:
                    from app.recruitment.scorer import score_and_persist
                    result = score_and_persist(app_id, posting_data, dict(app_data),
                                               app_root=current_app.root_path)
                    if result and result.get('is_shortlisted'):
                        print(f"[EMAIL MONITOR] Auto-shortlisted app {app_id} (score: {result['score']})")
                    elif result:
                        print(f"[EMAIL MONITOR] App {app_id} scored {result['score']} — below threshold")
                    else:
                        print(f"[EMAIL MONITOR] App {app_id} screening not scored")
        except Exception as e:
            print(f"[EMAIL MONITOR] AI scoring failed: {e}")

    return app_id


def surface_offer_reply(contract_id, intent, msg=None):
    """G31: an email containing an offer accept/decline intent NEVER mutates
    Contract or Job_Application. A numeric contract ID alone is not
    authorization -- acceptance happens only through the tokenized web flow
    (recruitment.accept_offer). This function only:
      - stores a signed PDF attachment (if any) for HR verification,
      - writes an audit entry,
      - notifies same-company HR/Admin that the reply needs manual review.
    """
    contract = query("""
        SELECT c.*, ja.applicant_name, ja.applicant_email, ja.company_id
        FROM Contract c
        JOIN Job_Application ja ON c.application_id=ja.application_id
        WHERE c.contract_id=?
    """, (contract_id,), one=True)
    if not contract:
        print(f"[EMAIL MONITOR] Offer reply for unknown contract {contract_id} ignored")
        return False

    signed_path = None
    if msg:
        try:
            signed_path = save_signed_contract_attachment(msg, contract_id)
        except Exception as e:
            print(f"[EMAIL MONITOR] Failed to store signed attachment: {e}")

    log_audit('EMAIL_OFFER_REPLY', 'Recruitment',
              f'Offer {contract_id}: candidate replied with {intent} via email '
              f'{f"(signed PDF stored at {signed_path})" if signed_path else "(no signed PDF attached)"} '
              f'-- awaiting manual HR review; email cannot authorize acceptance')

    try:
        from app.notifications.routes import send_in_app_to_company
        if contract['company_id']:
            send_in_app_to_company(
                contract['company_id'],
                ('Admin', 'HR', 'HR Manager'),
                f'Offer {intent.title()} via Email',
                f'{contract["applicant_name"]} replied with a {intent} for offer #{contract_id} '
                f'via email. Complete the action using the secure acceptance link or HR actions.',
                type='Warning',
                related_url='/recruitment/applications')
    except Exception as e:
        print(f"[EMAIL MONITOR] Review notification failed: {e}")

    return True
    return True


def _get_last_sync():
    cfg = query("SELECT last_sync FROM Email_Config WHERE is_active=1 ORDER BY config_id DESC LIMIT 1", one=True)
    if cfg and cfg['last_sync']:
        return cfg['last_sync']
    return None


def _save_last_sync():
    try:
        now = datetime.now().strftime('%d-%b-%Y %H:%M:%S')
        cfg = query("SELECT config_id FROM Email_Config WHERE is_active=1 ORDER BY config_id DESC LIMIT 1", one=True)
        if cfg:
            execute("UPDATE Email_Config SET last_sync=? WHERE config_id=?", (now, cfg['config_id']))
        else:
            execute("""INSERT INTO Email_Config (email, provider, host, port, username, last_sync, is_active)
                       VALUES (?, 'IMAP', ?, ?, ?, ?, 1)""",
                    (os.environ.get('MAIL_USERNAME', ''),
                     os.environ.get('IMAP_HOST', 'imap.gmail.com'),
                     int(os.environ.get('IMAP_PORT', 993)),
                     os.environ.get('MAIL_USERNAME', ''),
                     now))
    except Exception as e:
        print(f"[EMAIL MONITOR] Failed to save last_sync: {e}")


def _build_search_criteria():
    # Always search last 7 days; dedup prevents duplicate processing
    since_date = (datetime.now() - timedelta(days=7)).strftime('%d-%b-%Y')
    return f'(SINCE {since_date})'


def surface_reschedule_request(from_hdr, subject, body, msg_id=None):
    """Surface a candidate interview-reschedule request for manual HR review.

    Only ever creates an in-app notification -- interviews are never mutated
    by email.

    Resolution (never guesses):
      1. If the reply retains the invitation's INT-<id> reference and that
         interview belongs to the sender's applications, the notification
         names that exact interview.
      2. Otherwise a generic notification lists every upcoming interview for
         the sender's applications so HR can choose the right one.
      3. A sender with no upcoming interviews gets no notification.

    Deduplication is persistent: the Message-ID is stored in SQLite
    (Reschedule_Email_Processed), so a server restart cannot re-notify for
    the same message within the scan window. Returns True when a
    notification was created.
    """
    if not msg_id:
        return False
    from datetime import datetime as _dt
    try:
        exists = query(
            "SELECT 1 FROM Reschedule_Email_Processed WHERE msg_id=?", (msg_id,), one=True)
        if exists:
            return False
        execute("INSERT INTO Reschedule_Email_Processed (msg_id, processed_at) VALUES (?, datetime('now'))",
                (msg_id,))
        # Housekeeping: drop rows far beyond the 7-day scan window.
        execute("DELETE FROM Reschedule_Email_Processed WHERE processed_at < datetime('now','-30 days')")
    except Exception as e:
        print(f"[EMAIL MONITOR] Reschedule dedup failed: {e}")
        return False

    import re
    m = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', from_hdr or '')
    if not m:
        return False
    sender = m.group(0).lower()

    upcoming = query("""
        SELECT i.interview_id, i.scheduled_at, jp.title as job_title,
               ja.applicant_name, ja.company_id
        FROM Job_Application ja
        JOIN Interview i ON i.application_id = ja.application_id
        LEFT JOIN Job_Posting jp ON ja.posting_id = jp.posting_id
        WHERE lower(ja.applicant_email)=?
          AND i.status IN ('Scheduled','Confirmed')
          AND i.scheduled_at >= datetime('now')
        ORDER BY i.scheduled_at ASC
    """, (sender,))
    if not upcoming:
        return False
    company_id = upcoming[0]['company_id']
    applicant_name = upcoming[0]['applicant_name']

    from app.notifications.email_parser import extract_interview_ref
    ref = extract_interview_ref(subject, body)
    matched = None
    if ref is not None:
        for u in upcoming:
            if u['interview_id'] == ref:
                matched = u
                break

    if matched is not None:
        message = ('%s asked to reschedule interview INT-%d (%s, %s). '
                   'Manual review required.'
                   % (applicant_name, matched['interview_id'],
                      matched['job_title'] or 'position',
                      matched['scheduled_at'][:16]))
    else:
        parts = ['%s asked to reschedule an interview. Upcoming interviews: '
                 % applicant_name]
        parts.append('; '.join(
            'INT-%d (%s, %s)' % (u['interview_id'], u['job_title'] or 'position',
                                 u['scheduled_at'][:16]) for u in upcoming))
        parts.append('Select the correct interview to reschedule — manual review required.')
        message = ' '.join(parts)

    try:
        from app.notifications.routes import send_in_app_to_company
        send_in_app_to_company(
            company_id,
            ('Admin', 'HR', 'HR Manager'),
            'Interview Reschedule Request',
            message,
            type='Info',
            related_url='/recruitment/interviews')
    except Exception as e:
        print(f"[EMAIL MONITOR] Reschedule notification failed: {e}")
        return False
    return True


def poll_inbox():
    config = _get_imap_config()
    if not config:
        return 0, 0, 0

    try:
        mail = imaplib.IMAP4_SSL(config['host'], config['port'], timeout=IMAP_TIMEOUT)
        mail.login(config['username'], config['password'])
        mail.select('INBOX')
    except Exception as e:
        print(f"[EMAIL MONITOR] IMAP connection failed: {e}")
        return 0, 0, 0

    criteria = _build_search_criteria()
    try:
        status, messages = mail.search(None, criteria)
    except Exception as e:
        print(f"[EMAIL MONITOR] IMAP search failed: {e}")
        mail.logout()
        return 0, 0, 0

    if status != 'OK' or not messages[0]:
        mail.logout()
        _save_last_sync()
        return 0, 0, 0

    all_ids = messages[0].split()
    # Process most recent messages first (IMAP returns oldest first)
    message_ids = all_ids[-MAX_EMAILS_PER_POLL:]
    if len(all_ids) > MAX_EMAILS_PER_POLL:
        print(f"[EMAIL MONITOR] {len(all_ids)} messages found, processing last {MAX_EMAILS_PER_POLL} (most recent)")

    new_apps = 0
    new_accepts = 0
    new_declines = 0

    for num in message_ids:
        try:
            status, msg_data = mail.fetch(num, '(RFC822)')
            if status != 'OK':
                continue

            raw_email = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw_email)

            subject = decode_email_header(msg['Subject']) or ''
            from_hdr = decode_email_header(msg['From']) or ''
            body = get_email_body(msg)

            if is_auto_reply(msg):
                continue

            # Skip promotional / bulk marketing emails (e.g. Samsung, Glassdoor, LinkedIn)
            if is_promotional_email(msg, subject, body, from_hdr):
                continue

            # Check for offer accept/decline replies (works for both
            # fresh mailto emails and actual replies).
            # G31: the reply only surfaces for manual HR review -- the email
            # can never accept/decline the offer by itself.
            contract_id = extract_contract_id(subject, body)
            if contract_id:
                intent = detect_offer_reply(subject, body)
                if intent == 'accept' or (intent == 'ambiguous'
                        and subject and subject.lower().startswith('re:')):
                    if surface_offer_reply(contract_id, 'accept', msg):
                        new_accepts += 1
                elif intent == 'decline':
                    if surface_offer_reply(contract_id, 'decline', msg):
                        new_declines += 1
                continue

            # Candidate interview-reschedule requests: manual-review
            # notification only; interviews are never changed from email.
            if not is_application_email(subject, body) and detect_reschedule_request(subject, body):
                if surface_reschedule_request(from_hdr, subject, body, msg.get('Message-ID')):
                    print(f"[EMAIL MONITOR] Reschedule request surfaced for manual review: {from_hdr}")
                continue

            # Skip replies/forwards (they aren't new applications)
            first_word = subject.strip().split()[0].lower() if subject.strip() else ''
            if first_word.startswith('re:') or first_word.startswith('fwd:'):
                continue

            # Only skip non-application emails from employees
            # (allows employees to apply via their personal email)
            if not is_application_email(subject, body) and is_employee_email(from_hdr):
                continue

            if is_application_email(subject, body):
                parsed = parse_application_email(subject, body, from_hdr)
                if parsed['confidence'] >= 0.3:
                    result = create_application_from_email(msg, parsed)
                    if result is not None:
                        new_apps += 1
        except Exception as e:
            print(f"[EMAIL MONITOR] Error processing message {num}: {e}")
            continue

    _save_last_sync()

    try:
        mail.logout()
    except Exception:
        pass
    return new_apps, new_accepts, new_declines


def _get_imap_config():
    host = os.environ.get('IMAP_HOST', 'imap.gmail.com')
    port = int(os.environ.get('IMAP_PORT', 993))
    username = os.environ.get('MAIL_USERNAME')
    password = os.environ.get('MAIL_PASSWORD')
    if not username or not password:
        print("[EMAIL MONITOR] IMAP not configured (missing MAIL_USERNAME/MAIL_PASSWORD)")
        return None
    return {'host': host, 'port': port, 'username': username, 'password': password}
