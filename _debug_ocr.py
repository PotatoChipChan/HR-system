import sys; sys.path.insert(0, '.')
import re
from PIL import Image
import pytesseract
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

from app import create_app; app = create_app()

with app.app_context():
    from app.invoice.routes import _extract_all, _TOTAL_RE, _SUBTOTAL_RE, _TAX_RE, _SHIPPING_RE, _DISCOUNT_RE

    img = Image.open('test_batch/image/inv_0007_N59672.png')
    raw = pytesseract.image_to_string(img)
    print("=== RAW OCR TEXT ===")
    for l in raw.split('\n'):
        print(repr(l))

    # --- replicate preprocessing from _extract_all ---
    text = raw
    text = re.sub(r'\b(\d{1,3})\s(\d{3}),(\d{2})\b', r'\1\2.\3', text)
    text = re.sub(r'\b(\d{1,3})\.(\d{3}),(\d{2})\b', r'\1\2.\3', text)
    text = re.sub(r'(?<![.,\d])(\d+),(\d{2})(?!\d)', r'\1.\2', text)
    text = re.sub(r'(?<!\d)(\d{1,2})\s+(\d{2})\b',
                  lambda m: f'{m.group(1)}.{m.group(2)}'
                  if len(m.group(2)) == 2 and not re.search(r'[A-Za-z]', m.group(0))
                  else m.group(0), text)
    text = re.sub(r'-(\d+)-(\d{2})\b', r'-\1.\2', text)

    _ADDR_START_RE = re.compile(
        r'(?i)^\s*(?:suite|unit|floor|level|no\.?|lot|jalan|lorong|taman|'
        r'road|street|avenue|drive|lane|block|blk|building|tower|plaza|'
        r'km\s*\d|parsel|phase|blok)\b')
    _raw_lines = text.split('\n')
    _joined_lines = []
    _i = 0
    while _i < len(_raw_lines):
        _cur = _raw_lines[_i].strip()
        if _cur and _i + 1 < len(_raw_lines):
            _nxt = _raw_lines[_i + 1].strip()
            if (_cur and len(_cur) <= 30 and not re.search(r'\d', _cur)
                    and _nxt and re.search(r'\d', _nxt)
                    and not _ADDR_START_RE.match(_nxt)):
                _joined_lines.append(f"{_cur} {_nxt}")
                _i += 2
                continue
        _joined_lines.append(_cur if _cur else '')
        _i += 1
    text = '\n'.join(_joined_lines)
    text_flat = ' '.join(l.strip() for l in text.split('\n') if l.strip())

    text = re.sub(r'([A-Z0-9])\s+([-_/])\s*', r'\1\2', text)
    text = re.sub(r'(\d{2})/M(\d)/(\d{5})', lambda m: f'{m.group(1)}/0{m.group(2)}/20{m.group(3)[-2:]}', text)
    text = re.sub(r'(\d{2})/M(\d)/(\d{4})', lambda m: f'{m.group(1)}/0{m.group(2)}/{m.group(3)}', text)
    text = re.sub(r'(?<=\d)[ \t]+(?=\d)', '', text)

    lines = [l.strip() for l in text.split('\n') if l.strip()]

    print("\n=== AFTER PREPROC + JOIN + NORMALIZE ===")
    for l in lines:
        print(repr(l))
    print("flat:", repr(text_flat))

    # Check regex matching
    print("\n=== TOTAL RE ===")
    for m in _TOTAL_RE.finditer(text):
        print(f"  Match: {repr(m.group(0))} -> val={m.group(1)} ctx={repr(text[max(0,m.start()-50):m.start()])}")
    # Also flat
    for m in _TOTAL_RE.finditer(text_flat):
        print(f"  FLAT Match: {repr(m.group(0))} -> val={m.group(1)} ctx={repr(text_flat[max(0,m.start()-50):m.start()])}")

    print("\n=== SUBTOTAL RE ===")
    for m in _SUBTOTAL_RE.finditer(text):
        print(f"  Match: {repr(m.group(0))} -> val={m.group(1)}")

    print("\n=== SHIPPING RE ===")
    for m in _SHIPPING_RE.finditer(text):
        print(f"  Match: {repr(m.group(0))} -> val={m.group(1)}")

    # Now the get_amounts function
    def get_amounts(regex, exclude_keywords=None, use_flat=False, text_flat=text_flat, text=text):
        results = []
        search_text = text_flat if use_flat else text
        for m in regex.finditer(search_text):
            ctx = search_text[max(0, m.start()-50):m.start()].lower()
            if exclude_keywords and any(k in ctx for k in exclude_keywords):
                continue
            try:
                val_str = m.group(1).replace(',', '').replace('$', '')
                val = float(val_str)
                if "-" in search_text[max(0, m.start()-2):m.start()]:
                    val = -val
                results.append({'val': val, 'start': m.start(), 'end': m.end(), 'full': m.group(0), 'ctx': ctx})
            except:
                continue
        return results

    totals = get_amounts(_TOTAL_RE)
    print(f"\n=== totals_cands: {totals} ===")

    totals_flat = get_amounts(_TOTAL_RE, use_flat=True)
    print(f"=== totals_cands (flat): {totals_flat} ===")
