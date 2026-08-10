"""Debug raw OCR text from Meat One Cuisine image."""
import sys, os
sys.path.insert(0, '.')
from app import create_app
from app.invoice.routes import _get_easyocr_reader, _preprocess_image, _get_tesseract_path, _extract_all
import pytesseract
from PIL import Image

app = create_app()
with app.app_context():
    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path
    reader = _get_easyocr_reader()

    f = 'test_inv/IMG_20260529_165035(1).jpg'
    print(f'FILE: {f}')

    # Raw EasyOCR
    print('\n--- Raw EasyOCR ---')
    results = reader.readtext(f)
    for bbox, text, conf in results:
        print(f'  [{conf:.2f}] {text!r}')
    raw_easy = '\n'.join([r[1] for r in results])

    # Raw Tesseract
    print('\n--- Raw Tesseract ---')
    tess_text = pytesseract.image_to_string(Image.open(f), config='--oem 3 --psm 6')
    print(tess_text[:3000])

    # Combined extraction
    print('\n--- Extraction Result ---')
    combined = raw_easy + '\n' + tess_text
    ext = _extract_all(combined)
    for key in ['vendor_name', 'invoice_number', 'invoice_date', 'subtotal', 'tax_amount', 'total_amount', 'confidence', 'ai_note']:
        print(f'  {key}: {ext.get(key, "")!r}')
