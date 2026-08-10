"""
Geometric invoice extraction using pdfplumber bounding boxes.

Template-agnostic approach:
1. Extract text with character-level positions from pdfplumber
2. Cluster characters into rows by vertical proximity
3. Segment each row into label and value by horizontal gap analysis
4. Score labels against keyword banks
5. Validate with math (subtotal + tax = total)

No ML, no templates, no API keys — pure geometry and arithmetic.
"""
import re
import unicodedata
from collections import defaultdict


# ── Keyword banks for label classification ──────────────────────────────────

TOTAL_KEYWORDS = [
    'total', 'amount due', 'balance due', 'grand total', 'total paid',
    'total due', 'amount payable', 'payable amount',
    'total amount', 'due',
    'jumlah', 'jumlah keseluruhan', 'jumlah bayaran',
    'gesamtbetrag', 'totalbetrag', 'summe',
    'montant total', 'montant du', 'totale',
    'importe total', 'total importe',
]

SUBTOTAL_KEYWORDS = [
    'subtotal', 'sub total', 'sub-total', 'net amount', 'amount before tax',
    'before gst', 'before sst', 'before tax',
    'net worth', 'price net', 'net price',
    'total ht', 'sous-total', 'sous total',
    'zwischensumme', 'nettobetrag',
    'base amount', 'taxable amount',
    'net total',
]

TAX_KEYWORDS = [
    'tax', 'vat', 'gst', 'sst', 'sales tax', 'service tax',
    'tax amount', 'tax total', 'tax on',
    'tva', 'tva amount', 'tva total',
    'mwst', 'steuer', 'umsatzsteuer',
    'service charge',
]

DISCOUNT_KEYWORDS = [
    'discount', 'rebate', 'voucher', 'promo', 'adjustment',
    'adj', 'deduction', 'minus', 'subsidy', 'subsidi',
    'rabatt', 'remise', 'escompte',
]

SHIPPING_KEYWORDS = [
    'shipping', 'delivery', 'freight', 'postage', 'handling',
    'shipping and handling', 'shipping & handling',
    'port d\'envoi', 'livraison',
]

FEE_KEYWORDS = [
    'parking fee', 'toll fee', 'fare', 'usage fee',
]


# ── Number parsing ──────────────────────────────────────────────────────────

def _parse_number(text):
    """
    Parse a number string handling various formats:
    - US: 1,234.56
    - EU: 1.234,56 or 1 234,56
    - Simple: 1234.56
    - With currency: $1,234.56 / RM 98.00 / EUR 1.234,56
    Returns: float or None
    """
    if not text:
        return None
    cleaned = text.strip()
    # Remove currency symbols and codes
    cleaned = re.sub(
        r'(?i)(?:RM|MYR|USD|EUR|GBP|SGD|AUD|JPY|INR|CAD|HKD|THB|IDR|CHF'
        r'|S\$|A\$|HK\$|C\$|€|£|\$|¥|₹|฿|Rp)',
        '', cleaned
    ).strip()
    if not cleaned:
        return None
    # Handle negative in parentheses: (1,234.56) → -1234.56
    neg = False
    if cleaned.startswith('(') and cleaned.endswith(')'):
        neg = True
        cleaned = cleaned[1:-1]
    # Remove thousands separators (commits in US format, dots/spaces in EU)
    # Heuristic: if last separator is comma before 2 digits → EU format
    eu_match = re.match(r'^[\d.\s]*\d*,(\d{2})$', cleaned)
    if eu_match:
        # EU format: 1.234,56 or 1 234,56
        cleaned = cleaned.replace(' ', '').replace('.', '').replace(',', '.')
    else:
        # US format: 1,234.56
        cleaned = cleaned.replace(',', '')
    # Remove remaining non-numeric chars except minus and dot
    cleaned = re.sub(r'[^\d.\-]', '', cleaned)
    if not cleaned or cleaned in ('.', '-'):
        return None
    try:
        val = float(cleaned)
        return -val if neg else val
    except ValueError:
        return None


# ── Row clustering ──────────────────────────────────────────────────────────

def _cluster_rows(chars, y_tolerance=3.0):
    """
    Group characters into rows based on vertical proximity.
    chars: list of dicts from pdfplumber with 'top', 'x0', 'x1', 'text'
    Returns: list of rows, each row is a list of chars sorted by x0
    """
    if not chars:
        return []
    # Sort by vertical position, then horizontal
    sorted_chars = sorted(chars, key=lambda c: (round(c.get('top', 0)), c.get('x0', 0)))
    rows = []
    current_row = [sorted_chars[0]]
    for char in sorted_chars[1:]:
        # Check if this char is on the same row (vertical overlap)
        ref_top = current_row[0].get('top', 0)
        char_top = char.get('top', 0)
        if abs(char_top - ref_top) <= y_tolerance:
            current_row.append(char)
        else:
            # Sort row by x position
            rows.append(sorted(current_row, key=lambda c: c.get('x0', 0)))
            current_row = [char]
    if current_row:
        rows.append(sorted(current_row, key=lambda c: c.get('x0', 0)))
    return rows


# ── Row text builder with gap-aware spacing ─────────────────────────────────

def _row_text(row_chars):
    """
    Build a string from row characters, inserting spaces where the
    horizontal gap between consecutive chars exceeds a threshold.
    This prevents pdfplumber's glue-concatenation from merging distinct
    values (e.g. 'RCPT-88172Date:' instead of 'RCPT-88172 Date:').
    """
    if not row_chars:
        return ''
    chars_sorted = sorted(row_chars, key=lambda c: c.get('x0', 0))
    # Estimate average char width for threshold
    widths = [c.get('x1', c.get('x0', 0)) - c.get('x0', 0) for c in chars_sorted if c.get('text', '').strip()]
    avg_w = sum(widths) / max(1, len(widths)) if widths else 6
    gap_threshold = avg_w * 1.5

    parts = []
    prev_x1 = None
    for c in chars_sorted:
        x0 = c.get('x0', 0)
        if prev_x1 is not None and (x0 - prev_x1) > gap_threshold:
            parts.append(' ')
        parts.append(c.get('text', ''))
        prev_x1 = c.get('x1', x0 + avg_w)
    return ''.join(parts)


# ── Row segmentation ────────────────────────────────────────────────────────

def _segment_row(row_chars, min_gap_ratio=2.0):
    """
    Split a row into (label_text, value_text) by finding the largest horizontal gap.
    Returns: (label, value, label_x, value_x)
    """
    if not row_chars:
        return ('', '', 0, 0)

    # Strip leading whitespace chars (they create artificial gaps at row start)
    start = 0
    while start < len(row_chars) and not row_chars[start].get('text', '').strip():
        start += 1
    if start < len(row_chars):
        row_chars = row_chars[start:]

    if len(row_chars) <= 1:
        text = _row_text(row_chars)
        return (text.strip(), '', row_chars[0].get('x0', 0) if row_chars else 0, 0)

    # Calculate gaps between consecutive characters
    gaps = []
    for i in range(len(row_chars) - 1):
        x1_end = row_chars[i].get('x1', row_chars[i].get('x0', 0) + 6)
        x2_start = row_chars[i + 1].get('x0', 0)
        gap = x2_start - x1_end
        gaps.append((gap, i))

    if not gaps:
        full_text = ''.join(c.get('text', '') for c in row_chars)
        return (full_text, '', row_chars[0].get('x0', 0), 0)

    # Calculate mean gap (excluding the largest ones for skew resistance)
    gap_values = sorted([g for g, _ in gaps])
    if len(gap_values) > 4:
        # Trim top and bottom 20%
        trim = len(gap_values) // 5
        mean_gap = sum(gap_values[trim:-trim]) / max(1, len(gap_values[2*trim:]))
    else:
        mean_gap = sum(gap_values) / max(1, len(gap_values))

    # Find the best split point: the largest gap that exceeds mean by min_gap_ratio
    threshold = max(mean_gap * min_gap_ratio, 8)  # at least 8pt gap
    candidates = [(g, i) for g, i in gaps if g >= threshold]

    if candidates:
        # Use the largest gap
        best_gap, split_idx = max(candidates, key=lambda x: x[0])
        label = ''.join(c.get('text', '') for c in row_chars[:split_idx + 1]).strip()
        value = ''.join(c.get('text', '') for c in row_chars[split_idx + 1:]).strip()
        label_x = row_chars[0].get('x0', 0)
        value_x = row_chars[split_idx + 1].get('x0', 0)
        return (label, value, label_x, value_x)

    # No large gap found — treat the entire row as a label (no value on this row)
    full_text = ''.join(c.get('text', '') for c in row_chars)
    return (full_text.strip(), '', row_chars[0].get('x0', 0), 0)


# ── Label classification ────────────────────────────────────────────────────

def _classify_label(label_text):
    """
    Score a label against keyword banks.
    Returns: (field_type, score)
    field_type: 'total', 'subtotal', 'tax', 'discount', 'shipping', 'fee', or None
    """
    if not label_text:
        return (None, 0)
    label_lower = label_text.lower().strip()
    # Remove common prefixes/suffixes
    label_lower = re.sub(r'^[\s:.\-]+|[\s:.\-]+$', '', label_lower)
    if not label_lower:
        return (None, 0)

    # Reject lines that are primarily tax registration numbers (e.g. "SST W10-2509-32000446")
    # These contain long digit sequences with hyphens/letters but are NOT tax amounts.
    if re.search(r'\d{8,}', label_lower) or re.search(r'\d{4,}[\s\-]\d{4,}', label_lower):
        return (None, 0)

    # Reject lines containing date-related keywords (might contain "Total" or "Due" as words)
    if re.search(r'\b(?:date|due\s*date|page|tel|fax|phone|email|www|http|ref)\b', label_lower):
        return (None, 0)

    # Exact match gets highest score
    for kw in TOTAL_KEYWORDS:
        if label_lower == kw:
            return ('total', 100)
    for kw in SUBTOTAL_KEYWORDS:
        if label_lower == kw:
            return ('subtotal', 100)

    # Partial match with scoring
    scores = {}
    for field, keywords in [
        ('total', TOTAL_KEYWORDS),
        ('subtotal', SUBTOTAL_KEYWORDS),
        ('tax', TAX_KEYWORDS),
        ('discount', DISCOUNT_KEYWORDS),
        ('shipping', SHIPPING_KEYWORDS),
        ('fee', FEE_KEYWORDS),
    ]:
        score = 0
        for kw in keywords:
            if re.search(r'\b' + re.escape(kw) + r'\b', label_lower):
                score += len(kw) * 2
        # Bonus for being at the start of the label
        if score > 0 and label_lower.startswith(keywords[0][:4]):
            score += 5
        scores[field] = score

    best_field = max(scores, key=scores.get)
    if scores[best_field] > 0:
        return (best_field, scores[best_field])
    return (None, 0)


# ── Math validation ─────────────────────────────────────────────────────────

def _validate_math(subtotal, tax, discount, shipping, total):
    """
    Check if the extracted values satisfy: subtotal - discount + tax + shipping = total
    Returns: (is_valid, corrected_total, confidence_boost)
    """
    if total <= 0:
        return (False, 0, 0)
    if subtotal <= 0:
        return (True, total, 0)

    expected = subtotal - discount + tax + shipping
    error = abs(total - expected)

    if error < 0.01:
        return (True, total, 0.2)  # Perfect match
    elif error < 0.50:
        return (True, total, 0.1)  # Close (rounding)
    elif error < 2.0:
        return (True, total, 0.05)  # Somewhat close

    # Math doesn't check out — try to deduce missing fields
    if subtotal > 0 and total > 0:
        if tax <= 0:
            deduced = total - subtotal + discount - shipping
            if 0 < deduced < subtotal * 0.5:
                return (True, total, 0.05)
        if discount <= 0:
            deduced = subtotal + tax + shipping - total
            if 0 < deduced < subtotal * 0.3:
                return (True, total, 0.05)

    return (False, total, -0.15)  # Penalty for mismatch


# ── Constraint-satisfaction solver ────────────────────────────────────────────

def _solve_by_constraint(candidates):
    """
    Try all valid (subtotal, tax, total) combinations from available candidates
    using the math formula: total ≈ subtotal + tax + shipping - discount.

    Phase 1: simple (total = subtotal + tax)
    Phase 2: extended (total = subtotal - discount + shipping + tax)

    Returns dict with keys: subtotal, tax, total, discount, shipping, math_valid, confidence
    or None if no valid combination found.
    """
    if not candidates:
        return None

    # Flatten all candidates into a pool with field info
    pool = []
    for field, items in candidates.items():
        for item in items:
            pool.append({
                'value': item['value'],
                'field': field,
                'score': item['score'],
                'y': item['y'],
                'x': item['x'],
                'label': item['label'],
            })

    if not pool:
        return None

    # Deduplicate by (value, y, x)
    seen = set()
    unique = []
    for p in pool:
        key = (p['value'], round(p['y'], 1), p['x'])
        if key not in seen:
            seen.add(key)
            unique.append(p)

    best = None
    best_score = -99999

    def _score_combo(sub_val, tax_val, disc_val, ship_val, tot_val,
                     sub_item, tax_item, disc_item, ship_item, tot_item):
        """Score a candidate combination."""
        score = 0

        # Label match bonuses
        if sub_item and sub_item['field'] == 'subtotal':
            score += sub_item['score'] * 4
        elif sub_item:
            score += 5  # generic number used as subtotal

        if tax_item and tax_item['field'] == 'tax':
            score += tax_item['score'] * 4
        elif tax_item and tax_item['field'] == 'total':
            score += tax_item['score'] * 1  # row labeled "Total" might contain tax value
        elif tax_item:
            score += 5

        if tot_item and tot_item['field'] == 'total':
            score += tot_item['score'] * 4
        elif tot_item and tot_item['field'] == 'subtotal':
            score += tot_item['score'] * 1
        elif tot_item:
            score += 10

        if disc_item and disc_item['field'] == 'discount':
            score += disc_item['score'] * 3

        if ship_item and ship_item['field'] == 'shipping':
            score += ship_item['score'] * 3

        # Wrong-role penalty: using a candidate outside its labeled field
        if tax_item and tax_item['field'] not in ('tax', None):
            score -= 200
        if disc_item and disc_item['field'] not in ('discount', None):
            score -= 200
        if ship_item and ship_item['field'] not in ('shipping', None):
            score -= 200

        # Math validates = strong signal
        score += 300

        # Reasonableness checks
        if sub_val > 0:
            score += 30
        if tax_val > 0:
            score += 20
        if tax_val > 0 and sub_val > 0 and tax_val < sub_val * 0.5:
            score += 30  # reasonable tax rate
        if tot_val > sub_val:
            score += 20
        if disc_val > 0 and disc_val < sub_val:
            score += 20
        if ship_val > 0 and ship_val < sub_val:
            score += 20

        # Position bonus (totals at bottom half of page)
        bottom_threshold = 200
        if tot_item and tot_item['y'] > bottom_threshold:
            score += 30

        # Independent evidence: different y-positions = more rows = more confident
        rows_used = set()
        if sub_item: rows_used.add(round(sub_item['y'], 1))
        if tax_item: rows_used.add(round(tax_item['y'], 1))
        if tot_item: rows_used.add(round(tot_item['y'], 1))
        if disc_item: rows_used.add(round(disc_item['y'], 1))
        if ship_item: rows_used.add(round(ship_item['y'], 1))
        score += len(rows_used) * 15

        return score

    # ── Phase 1: simple formula (total = subtotal + tax) ──────────
    for tot_item in unique:
        tv = tot_item['value']
        if tv < 0.01:
            continue

        for sub_item in [None] + unique:
            sv = sub_item['value'] if sub_item else 0
            if sv < 0 or sv > tv * 1.05:
                continue

            for tax_item in [None] + unique:
                xv = tax_item['value'] if tax_item else 0
                if xv < 0:
                    continue

                # Can't use same item for two roles unless it's 0
                same_yx = lambda a, b: (a and b and round(a['y'], 1) == round(b['y'], 1) and a['x'] == b['x'])
                if same_yx(sub_item, tax_item) or same_yx(sub_item, tot_item) or same_yx(tax_item, tot_item):
                    continue

                expected = sv + xv
                error = abs(tv - expected)

                if error > 0.01 and error > tv * 0.005:
                    continue

                score = _score_combo(sv, xv, 0, 0, tv,
                                     sub_item, tax_item, None, None, tot_item)
                score -= error * 100

                if score > best_score:
                    best_score = score
                    best = {
                        'subtotal': sv,
                        'tax': xv,
                        'total': tv,
                        'discount': 0,
                        'shipping': 0,
                        'math_valid': True,
                        'confidence': min(0.99, max(0.01, score / 800)),
                    }

    # ── Phase 2: extended (total = subtotal - sum(discounts) + sum(shipping) + sum(taxes)) ──
    discount_items = [p for p in unique if p['field'] == 'discount']
    shipping_items = [p for p in unique if p['field'] == 'shipping']
    tax_items_all = [p for p in unique if p['field'] == 'tax']

    def _gen_combos(items, max_n=2):
        yield []
        for item in items:
            yield [item]
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                yield [items[i], items[j]]

    disc_combos = list(_gen_combos(discount_items))
    ship_combos = list(_gen_combos(shipping_items))
    tax_combos = list(_gen_combos(tax_items_all))

    for tot_item in unique:
        tv = tot_item['value']
        if tv < 0.01:
            continue

        for sub_item in [None] + unique:
            sv = sub_item['value'] if sub_item else 0
            if sv < 0 or sv > tv * 2:
                continue

            for disc_list in disc_combos:
                dv = sum(p['value'] for p in disc_list)
                if dv < 0 or dv > sv:
                    continue

                for ship_list in ship_combos:
                    shv = sum(p['value'] for p in ship_list)
                    if shv < 0 or shv > sv:
                        continue

                    for tax_list in tax_combos:
                        xv = sum(p['value'] for p in tax_list)
                        if xv < 0:
                            continue

                        # Deduplicate items
                        items_list = [sub_item] + disc_list + ship_list + tax_list + [tot_item]
                        seen_yx = set()
                        dup = False
                        for it in items_list:
                            if it:
                                key = (round(it['y'], 1), it['x'])
                                if key in seen_yx:
                                    dup = True
                                    break
                                seen_yx.add(key)
                        if dup:
                            continue

                        expected = sv - dv + shv + xv
                        error = abs(tv - expected)

                        if error > 0.01 and error > tv * 0.005:
                            continue

                        score = _score_combo(sv, xv, dv, shv, tv,
                                             sub_item,
                                             tax_list[-1] if tax_list else None,
                                             disc_list[-1] if disc_list else None,
                                             ship_list[-1] if ship_list else None,
                                             tot_item)
                        score += len(disc_list) * 50
                        score += len(ship_list) * 50
                        score += len(tax_list) * 50
                        score -= error * 100

                        if score > best_score:
                            best_score = score
                            best = {
                                'subtotal': sv,
                                'tax': xv,
                                'total': tv,
                                'discount': dv,
                                'shipping': shv,
                                'math_valid': True,
                                'confidence': min(0.99, max(0.01, score / 1000)),
                            }

    return best


# ── Vendor extraction from pdfplumber ────────────────────────────────────────

def _extract_vendor_from_rows(rows):
    """
    Try to extract vendor name from the top rows of the PDF.
    Returns the FIRST single line that looks like a company name.
    """
    noise_patterns = re.compile(
        r'(?i)^(?:invoice|receipt|bill|tax\s*invoice|page|date|tel|fax|phone|'
        r'email|www|http|subtotal|total|amount|payment|terms|order|ship|'
        r'quantity|description|item|price|rate|qty|sku|#\s*\d)',
    )
    noise_only = re.compile(
        r'(?i)^(?:invoice|receipt|bill|purchase\s*order|quotation|quote|'
        r'delivery\s*note|credit\s*note|statement|from|to|ship\s*to|bill\s*to)[:\s]*$'
    )
    corp_suffix = re.compile(
        r'(?i)\b(?:SDN\.?\s*BHD\.?|BHD\.?|LIMITED|LLP|PVT\.?\s*LTD|'
        r'ENTERPRISE|CORPORATION|INC\.?|LLC|CO\.?|COMPANY|GMBH|AG|SARL)\b'
    )
    # Address-like patterns to skip
    address_like = re.compile(
        r'(?i)^\d+\s+\w+\s+(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr|'
        r'Court|Ct|Place|Pl|Boulevard|Blvd|Highway|Hwy|Lot|No\.|Section|'
        r'Bandar|Jalan|Jl\.|Selangor|Kuala\s*Lumpur|Malaysia)\b'
    )
    # Lines that are purely numeric (registration numbers, phone numbers)
    numeric_only = re.compile(r'^[\d\s\-.,/()]+$')
    has_long_number = re.compile(r'\d{6,}')

    # First pass: look for corp suffix (highest confidence)
    for row in rows[:20]:
        line = _row_text(row).strip()
        if not line or len(line) < 3:
            continue
        if corp_suffix.search(line):
            # Strip long number sequences (registration numbers, phone numbers)
            cleaned = re.sub(r'\s*\d{6,}\s*', ' ', line).strip()
            # Strip trailing document labels and dates
            cleaned = re.sub(
                r'(?i)\s+(?:order|invoice|receipt|bill|quotation|quote|'
                r'purchase\s*order|delivery|statement)\s*(?:number|no|#|receipt|'
                r'invoice|order|bill|quotation)?\s*[\w]*$',
                '', cleaned
            ).strip()
            cleaned = re.sub(
                r'(?i)\s+(?:date|due|terms?|page)[:\s]*[\d\-/]+\s*$',
                '', cleaned
            ).strip()
            # Clean up multiple spaces
            cleaned = re.sub(r'\s{2,}', ' ', cleaned)
            if cleaned:
                return cleaned[:80]
            return line[:80]

    # Second pass: look for reasonable company names
    for row in rows[:20]:
        line = _row_text(row).strip()
        if not line or len(line) < 3:
            continue
        if noise_patterns.match(line):
            continue
        if noise_only.match(line):
            continue
        if numeric_only.match(line):
            continue
        if has_long_number.search(line):
            continue
        if address_like.match(line):
            continue
        if '@' in line:
            continue
        # Strip trailing document labels and dates first
        cleaned = re.sub(
            r'(?i)\s*(?:invoice|receipt|order\s*number|bill\s*number|'
            r'inv\.?\s*no\.?|invoice\s*number)\s*[\w\-]*$',
            '', line
        ).strip()
        cleaned = re.sub(
            r'(?i)\s+(?:date|due|terms?|page)[:\s]*[\d\-/]+\s*$',
            '', cleaned
        ).strip()
        if not cleaned:
            continue
        # Reasonable company name: 3-40 chars, has vowels, mostly alpha
        alpha = sum(1 for c in cleaned if c.isalpha())
        vowels = sum(1 for c in cleaned.lower() if c in 'aeiou')
        if 3 <= len(cleaned) <= 50 and alpha > len(cleaned) * 0.6 and vowels > 0:
            return cleaned
    return ''


# ── Main extraction ─────────────────────────────────────────────────────────

def geo_extract(pdf_path):
    """
    Extract invoice fields using geometric layout analysis.

    Returns dict with:
        raw_text: full text extracted from PDF
        vendor_name: detected vendor
        invoice_number: detected invoice number
        subtotal: extracted subtotal
        tax_amount: extracted tax (combined tax + service charge)
        total_amount: extracted total
        math_valid: whether math checks out
        confidence: float 0-1
        candidates: dict of all found candidates per field (for debugging)
        char_count: total characters extracted
        page_count: number of pages
    """
    import pdfplumber

    result = {
        'raw_text': '',
        'vendor_name': '',
        'invoice_number': '',
        'subtotal': 0.0,
        'tax_amount': 0.0,
        'total_amount': 0.0,
        'discount': 0.0,
        'shipping': 0.0,
        'math_valid': False,
        'confidence': 0.0,
        'candidates': defaultdict(list),
        'char_count': 0,
        'page_count': 0,
    }

    try:
        with pdfplumber.open(pdf_path) as pdf:
            result['page_count'] = len(pdf.pages)
            all_text_parts = []

            for page in pdf.pages:
                page_text = page.extract_text() or ''
                all_text_parts.append(page_text)

                chars = page.chars
                result['char_count'] += len(chars)
                if not chars:
                    continue

                # Cluster characters into rows
                rows = _cluster_rows(chars)

                # Extract vendor from top rows
                if not result['vendor_name']:
                    result['vendor_name'] = _extract_vendor_from_rows(rows)

                # Extract invoice number from rows
                if not result['invoice_number']:
                    # Phase 1: scan ALL rows for explicit label match first
                    for row in rows:
                        line = _row_text(row).strip()
                        m = re.search(
                            r'(?i)(?:invoice|inv)\s*(?:number|#|num\b|no\.?)\s*[:\-]?\s*#?\s*([A-Z0-9][\w\-]{2,30})(?!\w)',
                            line
                        )
                        if m:
                            result['invoice_number'] = m.group(1)
                            break
                if not result['invoice_number']:
                    # Phase 2: standalone pattern, skipping registration-like lines
                    reg_pattern = re.compile(r'(?i)(?:reg|registration|vat|gst|sst|tax\s*id|phone|tel|fax)[:\s]')
                    for row in rows:
                        line = _row_text(row).strip()
                        if reg_pattern.search(line):
                            continue
                        m = re.search(r'#?\s*([A-Z]{1,6}[\-]?\d{4,10})(?!\w)', line)
                        if m:
                            candidate = m.group(1)
                            # Accept only 1 letter if followed by 5+ digits or 2+ letters if 4+ digits
                            letters = sum(1 for ch in candidate if ch.isalpha())
                            digits = sum(1 for ch in candidate if ch.isdigit())
                            if letters + digits >= 4 and (letters >= 2 or digits >= 5):
                                result['invoice_number'] = candidate
                                break

                # Process each row for financial fields — extract ALL numbers
                for row in rows:
                    row_y = row[0].get('top', 0) if row else 0
                    row_text = _row_text(row)
                    label, _, lx, _ = _segment_row(row)

                    # Find ALL currency-like numbers in this row (skip percentages, dates)
                    for m in re.finditer(r'[\$€£]?\s*(\d[\d,.]*\d)(?!\s*%|%)', row_text):
                        val = _parse_number(m.group(0))
                        if val is None or val <= 0:
                            continue

                        # Classify label
                        field_type, score = _classify_label(label)
                        if not field_type:
                            field_type, score = _classify_label(f'{label}')

                        if field_type:
                            result['candidates'][field_type].append({
                                'value': val,
                                'score': score,
                                'label': label,
                                'y': row_y,
                                'x': m.start(),
                            })

            result['raw_text'] = '\n'.join(all_text_parts)

    except Exception as e:
        result['raw_text'] = f'[pdfplumber error: {e}]'
        return result

    # ── Constraint-satisfaction solver ────────────────────────────
    solution = _solve_by_constraint(result['candidates'])

    if solution:
        result['subtotal'] = solution['subtotal']
        result['tax_amount'] = solution['tax']
        result['total_amount'] = solution['total']
        result['discount'] = solution['discount']
        result['shipping'] = solution['shipping']
        result['math_valid'] = solution['math_valid']
        result['confidence'] = solution['confidence']
    else:
        # Fallback: greedy selection per field
        for field, cands in result['candidates'].items():
            if cands:
                if field == 'total':
                    best = max(cands, key=lambda c: (c['score'], c['value']))
                else:
                    best = max(cands, key=lambda c: c['score'])
                if field == 'total':
                    result['total_amount'] = best['value']
                elif field == 'subtotal':
                    result['subtotal'] = best['value']
                elif field == 'tax':
                    result['tax_amount'] = best['value']
                elif field == 'discount':
                    result['discount'] = best['value']
                elif field == 'shipping':
                    result['shipping'] = best['value']

        _v, corrected, conf_boost = _validate_math(
            result['subtotal'], result['tax_amount'],
            result['discount'], result['shipping'], result['total_amount']
        )
        result['total_amount'] = corrected
        result['confidence'] = min(1.0,
            0.2 * bool(result['vendor_name']) +
            0.3 * (result['total_amount'] > 0) +
            0.15 * (result['subtotal'] > 0) +
            0.15 * (result['tax_amount'] > 0) +
            0.2 * bool(result['math_valid']) +
            conf_boost)

    # Convert candidates to plain dict for JSON serialization
    result['candidates'] = {
        k: [(c['value'], c['label']) for c in v]
        for k, v in result['candidates'].items()
    }

    return result


def geo_extract_text_only(pdf_path):
    """
    Lightweight extraction: just get text + vendor from pdfplumber.
    Used as a pre-processing step for _extract_all() fallback.
    """
    import pdfplumber
    try:
        with pdfplumber.open(pdf_path) as pdf:
            parts = []
            vendor = ''
            for page in pdf.pages:
                text = page.extract_text() or ''
                parts.append(text)
                if not vendor and page.chars:
                    rows = _cluster_rows(page.chars)
                    vendor = _extract_vendor_from_rows(rows)
            return '\n'.join(parts), vendor
    except Exception:
        return '', ''
