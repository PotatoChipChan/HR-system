"""Debug vendor extraction with normalized text."""
import sys, re
sys.path.insert(0, '.')
from app import create_app
from app.invoice.routes import _extract_vendor, _get_tesseract_path
import easyocr, pytesseract
from PIL import Image

app = create_app()
with app.app_context():
    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path
    
    reader = easyocr.Reader(['en'], gpu=False)
    results = reader.readtext('test_inv/Petron.jpg')
    easy_text = '\n'.join([r[1] for r in results])
    tess_text = pytesseract.image_to_string(Image.open('test_inv/Petron.jpg'), config='--oem 3 --psm 6')
    
    text = easy_text + '\n' + tess_text
    
    # Apply the same normalizations as _extract_all
    _ADDR_START_RE = re.compile(
        r'(?i)^\s*(?:suite|unit|floor|level|no\.?|lot|jalan|lorong|taman|'
        r'road|street|avenue|drive|lane|block|blk|building|tower|plaza|'
        r'km\s*\d|parsel|phase|blok)\b'
    )
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
    
    text = re.sub(r'([A-Z0-9])\s+([-_/])\s*', r'\1\2', text)
    text = re.sub(r'(?<=\d)[ \t]+(?=\d)', '', text)
    
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    
    print(f'Total lines after normalization: {len(lines)}')
    print(f'First 15 lines:')
    for i, line in enumerate(lines[:15]):
        print(f'  {i}: {line!r}')
    
    vendor = _extract_vendor(lines)
    print(f'\nVendor: {vendor!r}')
