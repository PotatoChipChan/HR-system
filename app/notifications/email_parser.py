"""app/notifications/email_parser.py – Free-text email parser & intent detection"""
import re


def extract_email(from_hdr):
    m = re.search(r'<([^>]+)>', from_hdr or '')
    return m.group(1) if m else (from_hdr or '').strip()


def parse_name_from_header(from_hdr):
    m = re.match(r'^([^<]+)', from_hdr or '')
    if m:
        name = m.group(1).strip().strip('"\'')
        if name and '@' not in name:
            return name
    return None


def extract_posting_ref(body):
    """Extract posting reference from email body footer: [Ref: POST-3]"""
    m = re.search(r'\[Ref:\s*POST-(\d+)\]', body or '')
    if m:
        return int(m.group(1))
    m = re.search(r'Ref:\s*POST-(\d+)', body or '', re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def try_structured_body(body):
    """Parse structured application format:
    Name: John Doe
    Position: Software Engineer
    Email: john@example.com
    IC: 900101-01-1234
    Phone: +6012-3456789
    Address: 123 Jalan Example, KL
    """
    if not body:
        return None

    fields = {}
    patterns = {
        'name': r'(?i)^Name\s*:\s*(.+?)\s*$',
        'position_raw': r'(?i)^(?:Position|Job|Role|Position Applied)\s*:\s*(.+?)\s*$',
        'email': r'(?i)^(?:Email|E-mail|Email Address)\s*:\s*(.+?)\s*$',
        'ic': r'(?i)^(?:IC|IC Number|NRIC|MyKad|Identity Card)\s*:\s*(.+?)\s*$',
        'phone': r'(?i)^(?:Phone|Contact|Mobile|Contact Number|Phone Number)\s*:\s*(.+?)\s*$',
        'address': r'(?i)^(?:Address|Home Address|Current Address)\s*:\s*(.+?)\s*$',
        'emergency_contact_name': r'(?i)^(?:Emergency Contact Name|EC Name|Next of Kin Name)\s*:\s*(.+?)\s*$',
        'emergency_contact_no': r'(?i)^(?:Emergency Contact (?:No|Number|Phone|Contact)|EC (?:No|Number|Phone|Contact))\s*:\s*(.+?)\s*$',
    }

    lines = body.split('\n')
    for key, pat in patterns.items():
        for line in lines:
            m = re.match(pat, line.strip())
            if m:
                val = m.group(1).strip()
                if val:
                    fields[key] = val
                    break

    # If we have at least 3 structured fields, it's a match
    if len(fields) >= 3:
        fields['confidence'] = 1.0
        return fields

    return None


def try_subject_template(subject):
    """Match structured subject: APPLY: Position - Name - Phone"""
    if not subject:
        return None
    m = re.match(
        r'APPLY\s*:?\s*(.+?)\s*[-–]\s*(.+?)\s*[-–]\s*(.+)$',
        subject, re.IGNORECASE
    )
    if not m:
        return None
    phone = m.group(3).strip()
    # Validate phone looks like a phone number
    phone_clean = re.sub(r'[\s\-\(\)]', '', phone)
    if not re.match(r'^01\d{7,9}$', phone_clean):
        phone = None
    return {
        'position_raw': m.group(1).strip(),
        'name': m.group(2).strip(),
        'phone': phone,
    }


def try_subject_regex(subject):
    """Match common subject patterns for applications"""
    if not subject:
        return None
    result = {}

    # Patterns that can extract both position and name (e.g. "IT Support - John Doe")
    name_suffix_patterns = [
        r'(?:Application|Apply)\s+for\s+(.+?)\s*[-–]\s*(.+?)[?.!,\s]*$',
        r'(?:Job|Position)\s+(?:Application|Apply)\s*[:–-]\s*(.+?)\s*[-–]\s*(.+?)[?.!,\s]*$',
    ]
    for pat in name_suffix_patterns:
        m = re.search(pat, subject, re.IGNORECASE)
        if m:
            result['position_raw'] = m.group(1).strip()
            result['name'] = m.group(2).strip() if m.group(2) else None
            return result or None

    # Patterns that extract position only (e.g. "IT Support Application")
    position_only_patterns = [
        r'(?:Application|Apply)\s+for\s+(.+?)[?.!,\s]*$',
        r'(?:Job|Position)\s+(?:Application|Apply)\s*[:–-]\s*(.+?)[?.!,\s]*$',
        r'^(.+?)\s+(Application|Apply|Resume|CV)s?[?.!,\s]*$',
        r'^(.+?)\s*[-–]\s*(Application|Apply|Resume|CV)[?.!,\s]*$',
    ]
    for pat in position_only_patterns:
        m = re.search(pat, subject, re.IGNORECASE)
        if m:
            result['position_raw'] = m.group(1).strip()
            result['name'] = None
            return result or None

    return None if not result else result


def try_body_regex(body):
    """Scan body text for name, position, phone clues"""
    if not body:
        return {}
    result = {}

    name_pats = [
        r'(?:My name is|I am|I\'m|Name is|Name\s*:)\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)',
        r'(?:Saya|Nama saya)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)',
    ]
    for pat in name_pats:
        m = re.search(pat, body)
        if m:
            result['name'] = m.group(1).strip()
            break

    pos_pats = [
        r'(?:apply(?:ing)?\s+for|seeking|looking for)\s+(?:the\s+)?(?:position|role|job)\s+(?:as|of|:)?\s*(.+?)(?:\s+(?:at|in|with|for|based)|\.|,|$)',
        r'(?:position|role|job)\s+(?:as|of|:)\s*(.+?)(?:\s+(?:at|in|with|for|based)|\.|,|$)',
    ]
    for pat in pos_pats:
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            if len(raw) < 60:
                result['position_raw'] = raw
                break

    phone_pat = r'(?:(?:\+?6?0)?1[0-9]\s*[-–\s]?\d{3,4}\s*[-–\s]?\d{3,4})'
    m = re.search(phone_pat, body)
    if m:
        raw = m.group(0)
        cleaned = re.sub(r'[\s\-\(\)]', '', raw)
        if re.match(r'^01\d{7,9}$', cleaned):
            result['phone'] = cleaned

    return result


def parse_application_email(subject, body, from_header):
    result = {
        'name': None,
        'email': None,
        'phone': None,
        'ic': None,
        'address': None,
        'emergency_contact_name': None,
        'emergency_contact_no': None,
        'position_raw': None,
        'posting_ref': None,
        'confidence': 0.0,
    }

    # Priority 1: Structured body format (from mailto: career page)
    structured = try_structured_body(body[:2000] if body else '')
    if structured:
        result.update(structured)
        # Extract posting ref from footer
        ref = extract_posting_ref(body)
        if ref:
            result['posting_ref'] = ref
        # Also check subject for additional clues
        if not result.get('position_raw'):
            info = try_subject_regex(subject)
            if info and info.get('position_raw'):
                result['position_raw'] = info['position_raw']
        if not result.get('name'):
            result['name'] = parse_name_from_header(from_header)
        # Sender address is only a fallback when the template omits Email:
        if not result.get('email'):
            result['email'] = extract_email(from_header)
        result['confidence'] = 1.0
        return result

    # Priority 2: Subject template format
    info = try_subject_template(subject)
    if info:
        result.update(info)
        result['email'] = extract_email(from_header)
        result['confidence'] = 1.0
        return result

    # Priority 3: Free-form subject regex
    info = try_subject_regex(subject)
    if info:
        result.update(info)
        result['confidence'] = 0.8

    # Priority 4: Body regex (free-form)
    body_info = try_body_regex(body[:500] if body else '')
    if body_info:
        if result['name'] is None:
            result['name'] = body_info.get('name')
        if result['phone'] is None:
            result['phone'] = body_info.get('phone')
        if result['position_raw'] is None:
            result['position_raw'] = body_info.get('position_raw')
        if result['position_raw'] and result['name']:
            result['confidence'] = max(result['confidence'], 0.7)
        elif result['name'] or result['position_raw']:
            result['confidence'] = max(result['confidence'], 0.5)

    if result['name'] is None:
        result['name'] = parse_name_from_header(from_header)
        if result['name']:
            result['confidence'] = max(result['confidence'], 0.3)

    if result['email'] is None:
        result['email'] = extract_email(from_header)

    return result


def extract_contract_id(subject, body=''):
    if not subject:
        return None
    m = re.search(r'(?:ACCEPT|DECLINE)\s+OFFER\s+(\d+)', subject, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r'Offer\s*#(\d+)', subject, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r'Offer\s*#(\d+)', body, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def detect_offer_reply(subject, body):
    text = f"{subject or ''} {body or ''}"
    score = 0

    m = re.search(r'ACCEPT\s+OFFER\s+\d+', subject or '', re.IGNORECASE)
    if m:
        score += 3

    m = re.search(r'DECLINE\s+OFFER\s+\d+', subject or '', re.IGNORECASE)
    if m:
        score -= 3

    if re.search(r'\baccept(?:ed|ing|s|ance)?\b', text, re.IGNORECASE):
        score += 2 if re.search(r'Offer\s*#', text, re.IGNORECASE) else 1
    if re.search(r'\b(?:decline|reject(?:ed|ing|s)?)\b', text, re.IGNORECASE):
        score -= 2
    if re.search(r'\b(?:not interested|withdraw)\b', text, re.IGNORECASE):
        score -= 2
    if re.search(r'\b(?:agree|confirm|join|start)\b', text, re.IGNORECASE):
        score += 1
    if re.search(r'\b(?:thank|looking forward|pleased|happy|grateful)\b', text, re.IGNORECASE):
        score += 1

    if score >= 2:
        return 'accept'
    elif score <= -2:
        return 'decline'
    return 'ambiguous'


def is_application_email(subject, body):
    text = f"{subject or ''} {body[:300] if body else ''}".lower()
    keywords = ['apply', 'application', 'applicant', 'job', 'vacancy',
                'position', 'resume', 'cv', 'hiring', 'candidate']
    score = sum(2 for kw in keywords if kw in text)

    # Require at least 2 keyword hits (score >= 4) instead of 1 (score >= 2)
    # This prevents single-word false positives like "job alerts" from Glassdoor
    if score < 4:
        return False

    return True


# ── Promotional email patterns (blocklist) ────────────────────────────────────

PROMO_BODY_KEYWORDS = [
    'unsubscribe', 'opt.out', 'email preferences', 'manage subscription',
    'subscription centre', 'update your preferences', 'preference center',
    'view in browser', 'view this email online', 'download our app',
    'this email was sent', 'all rights reserved', 'trouble viewing',
    'no longer wish to receive', 'mailing list', 'email settings',
    'communication preferences', 'click here to view',

    'newsletter', 'special offer', 'hot deal', 'flash sale',
    'don.t miss', 'act now', 'hurry', 'limited time', 'exclusive',
    'webinar', 'free trial', 'register now', 'ebook', 'whitepaper',

    'job alert', 'job search', 'job board', 'find your dream',
    'recruiting now', 'hiring now', 'talent pool', 'candidate sourcing',
    'recruitment solutions', 'talent solutions',

    'new product', 'introducing', 'announcing', 'launching',
    'follow us on', 'social media', 'privacy policy',
    'notifications.settings',
]

PROMO_FROM_PATTERNS = [
    r'(?:^|@)glassdoor',
    r'(?:^|@)linkedin',
    r'(?:^|@)indeed',
    r'(?:^|@)jobstreet',
    r'(?:^|@)monster',
    r'(?:^|@)careerbuilder',
    r'(?:^|@)hired\.com',
    r'(?:^|@)ziprecruiter',
    r'(?:^|@)samsung',
    r'(?:^|@)facebookmail',
    r'(?:^|@)quora',
    r'(?:^|@)medium',
    r'(?:^|@)substack',
    r'(?:^|@)hubspot',
    r'(?:^|@)mailchimp',
    r'(?:^|@)sendgrid',
    r'newsletter@',
    r'marketing@',
    r'promotions?@',
]

PROMO_SUBJECT_PREFIXES = [
    r'^(?:\[?\s*)(?:webinar|invitation|reminder|sale|offer|deal|new|hot)(?:\s*\]?)',
    r'^(?:\[?\s*)(?:weekly|monthly|daily)\s+(?:digest|roundup|update|news)(?:\s*\]?)',
    r'^(?:\[?\s*)(?:sponsored|promoted|ad|advertisement)(?:\s*\]?)',
    r'[\U0001F300-\U0001FAFF]',  # emoji in subject
]


def is_promotional_email(msg=None, subject='', body='', from_hdr=''):
    from_addr = extract_email(from_hdr).lower() if from_hdr else ''

    # 1. List-Unsubscribe header — strongest signal of bulk marketing
    if msg is not None:
        unsub = msg.get('List-Unsubscribe', '')
        if unsub and 'http' in unsub.lower():
            return True

        # 2. Precedence: bulk header
        precedence = msg.get('Precedence', '')
        if precedence and 'bulk' in precedence.lower():
            return True

    # 3. Known promotional sender domains
    for pat in PROMO_FROM_PATTERNS:
        if re.search(pat, from_addr):
            return True

    # 4. Promotional subject prefixes
    subj = (subject or '').strip()
    for pat in PROMO_SUBJECT_PREFIXES:
        if re.search(pat, subj, re.IGNORECASE):
            return True

    # 5. Promotional keywords in body (check more of the body)
    body_text = (body or '')[:2000].lower()
    keyword_hits = sum(1 for kw in PROMO_BODY_KEYWORDS if kw in body_text)
    if keyword_hits >= 2:
        return True

    # 6. HTML-heavy body without personal info (marketing template)
    body_html = ''
    if msg is not None and msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/html':
                payload = part.get_payload(decode=True)
                if payload:
                    body_html = payload.decode('utf-8', errors='replace')
                    break
    if body_html:
        # Count HTML tags vs plain text
        tag_count = len(re.findall(r'<[^>]+>', body_html))
        text_chars = len(re.sub(r'<[^>]+>', '', body_html).strip())
        # High tag-to-text ratio suggests bulk template
        if tag_count > 30 and text_chars > 0 and tag_count / max(text_chars, 1) > 0.05:
            return True

    return False


def is_auto_reply(msg):
    headers_to_check = ['Auto-Submitted', 'X-Auto-Response-Suppress',
                        'X-Autoreply', 'Precedence']
    for h in headers_to_check:
        val = msg.get(h, '')
        if val and val.lower() not in ('no', '', 'none'):
            return True

    ct = msg.get_content_type()
    if ct == 'text/calendar' or 'delivery-status' in ct:
        return True

    return False


def detect_reschedule_request(subject, body):
    """Heuristic: does this email ask to reschedule/postpone an interview?

    Intent-only signal; the email monitor never mutates an interview from
    an email. Reschedule requests surface as a manual-review notification.
    """
    text = '%s\n%s' % ((subject or ''), (body or ''))
    text = text.lower()
    intent = any(w in text for w in ('reschedule', 're-schedule', 'postpone'))
    return intent and 'interview' in text


INTERVIEW_REF_RE = re.compile(r'\bINT-(\d+)\b', re.IGNORECASE)


def extract_interview_ref(subject, body):
    """Extract an interview reference (INT-<id>) from a reply, if retained.

    Returns the interview_id as int, or None. Used to match a reschedule
    request to the exact interview the candidate means.
    """
    text = '%s\n%s' % ((subject or ''), (body or ''))
    m = INTERVIEW_REF_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None
