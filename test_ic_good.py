"""Test OCR on IC front good orientation.jpg"""
import sys; sys.path.insert(0, '.')
from PIL import Image
from app.employees.routes import (
    _ocr_mykad_front, _extract_id_info, _ocr_name_fallback,
    _run_easyocr, _extract_address, _get_easyocr_reader,
    _ocr_address_fallback, _extract_malaysian_name
)

r = _get_easyocr_reader()
img = Image.open('test_inv/IC front good orientation.jpg').convert('RGB')
print('Size:', img.size)

raw_text, ocr_score, corrected = _ocr_mykad_front(img)
extracted = _extract_id_info(raw_text, side='front', doc_type='ic')
print('Main pass: score=%d' % ocr_score)
ic = extracted.get('ic_number', '')
name = extracted.get('full_name', '')
addr = extracted.get('address', '')
print('  IC:', ic)
print('  Name:', name)
print('  Address:', addr)
print('  Raw:', raw_text[:200])

# Reproduce pipeline logic: run EasyOCR once, use for name + address
easyocr_text = _run_easyocr(corrected)
print('\nEasyOCR text:')
for l in easyocr_text.split('\n'):
    print('  [' + l + ']')

lines = [l.strip() for l in easyocr_text.split('\n') if l.strip()]
name_easy = _extract_malaysian_name(lines)

# Pipeline: EasyOCR name overrides Tesseract if different (reliable MyKad)
if name_easy:
    prev_name = extracted.get('full_name', '')
    if name_easy.upper() != prev_name.upper():
        print(f'Name override: {prev_name!r} → {name_easy!r}')
    extracted['full_name'] = name_easy.title()

# Pipeline: run address extraction with EasyOCR text
addr_easy = _extract_address(easyocr_text, full_name=name_easy or '')
if not addr_easy:
    addr_easy = _extract_address(easyocr_text)
print('Address from EasyOCR:', addr_easy)
