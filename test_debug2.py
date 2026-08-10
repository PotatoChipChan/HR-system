"""Debug India_front.jpeg address sources."""
import sys, re; sys.path.insert(0, '.')
from PIL import Image
from app.employees.routes import (
    _ocr_mykad_front, _run_easyocr, _extract_address,
    _extract_malaysian_name
)
from app.employees.guilloche_removal import remove_guilloche

img = Image.open('test_IC/India_front.jpeg').convert('RGB')
raw, score, corrected = _ocr_mykad_front(img)

# Original EasyOCR
easy1 = _run_easyocr(corrected)
print("=== Original EasyOCR ===")
print(easy1)

# FFT EasyOCR
gc = remove_guilloche(corrected)
easy2 = _run_easyocr(gc)
print("\n=== FFT EasyOCR ===")
print(easy2)

# Address from each source separately
lines1 = [l.strip() for l in easy1.split('\n') if l.strip()]
name1 = _extract_malaysian_name(lines1)
addr1 = _extract_address(easy1, full_name=name1 or '')
addr1b = _extract_address(easy1)
print(f"\nEasyOCR addr (w/ name): {addr1!r}")
print(f"EasyOCR addr (no name): {addr1b!r}")

lines2 = [l.strip() for l in easy2.split('\n') if l.strip()]
name2 = _extract_malaysian_name(lines2)
addr2 = _extract_address(easy2, full_name=name2 or '')
addr2b = _extract_address(easy2)
print(f"\nFFT addr (w/ name): {addr2!r}")
print(f"FFT addr (no name): {addr2b!r}")

# What does Tesseract give?
from app.employees.routes import _extract_id_info
extracted = _extract_id_info(raw, side='front', doc_type='ic')
print(f"\nTesseract addr: {extracted.get('address', '')!r}")
