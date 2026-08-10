"""Diagnostic: test OCR extraction for the two problematic files."""
import sys, os
sys.path.insert(0, '.')

from app import create_app
from app.invoice.routes import (
    _get_easyocr_reader, _preprocess_image, _preprocess_with_cv2,
    _get_tesseract_path, _extract_all, _extract_vendor, _is_garbage_vendor,
    _is_thermal_receipt
)
import pytesseract
from PIL import Image
from PyPDF2 import PdfReader

app = create_app()
with app.app_context():
    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path

    # =========================================================================
    # TEST 1: Screenshot 2026-05-27 013400.png (IOI Parking receipt)
    # Expected: Total RM 12 (Total Parking Fee), Parking Fee RM 12.26
    # =========================================================================
    img_path = 'test_inv/Screenshot 2026-05-27 013400.png'
    print(f'\n{"="*70}')
    print(f'TEST 1: {img_path}')
    print(f'{"="*70}')
    
    # Check thermal detection
    print(f'\n  is_thermal_receipt: {_is_thermal_receipt(img_path)}')
    
    # Get image dimensions
    img = Image.open(img_path)
    print(f'  Image size: {img.size}')
    
    # --- EasyOCR on preprocessed ---
    reader = _get_easyocr_reader()
    try:
        processed = _preprocess_image(img_path)
        proc_path = img_path + '_preprocessed.png'
        processed.save(proc_path)
        results = reader.readtext(proc_path)
        easy_prep_text = '\n'.join([text for _, text, _ in results])
        os.remove(proc_path)
        print(f'\n  --- EasyOCR (preprocessed) raw lines ---')
        for r in results:
            print(f'    [{r[2]:.2f}] {r[1]}')
    except Exception as e:
        easy_prep_text = ''
        print(f'  EasyOCR preprocessed error: {e}')

    # --- EasyOCR on raw image ---
    try:
        results_raw = reader.readtext(img_path)
        easy_raw_text = '\n'.join([text for _, text, _ in results_raw])
        print(f'\n  --- EasyOCR (raw) raw lines ---')
        for r in results_raw:
            print(f'    [{r[2]:.2f}] {r[1]}')
    except Exception as e:
        easy_raw_text = ''
        print(f'  EasyOCR raw error: {e}')

    # --- Tesseract on raw image ---
    try:
        tess_raw = pytesseract.image_to_string(Image.open(img_path), config='--oem 3 --psm 6')
        print(f'\n  --- Tesseract (raw, psm 6) ---')
        for line in tess_raw.split('\n'):
            if line.strip():
                print(f'    {line}')
    except Exception as e:
        tess_raw = ''
        print(f'  Tesseract raw error: {e}')

    # --- Tesseract on raw image with psm 4 ---
    try:
        tess_raw4 = pytesseract.image_to_string(Image.open(img_path), config='--oem 3 --psm 4')
        print(f'\n  --- Tesseract (raw, psm 4) ---')
        for line in tess_raw4.split('\n'):
            if line.strip():
                print(f'    {line}')
    except Exception as e:
        tess_raw4 = ''
        print(f'  Tesseract psm 4 error: {e}')

    # --- Extract from combined (mimics current pipeline) ---
    combined_prep = easy_prep_text + '\n' + tess_raw
    ext_prep = _extract_all(combined_prep)
    
    combined_raw = easy_raw_text + '\n' + tess_raw
    ext_raw = _extract_all(combined_raw)
    
    # Also try Tesseract only
    ext_tess = _extract_all(tess_raw)
    ext_tess4 = _extract_all(tess_raw4)
    
    print(f'\n  --- Extraction Results ---')
    for label, ext in [('preprocessed+tess', ext_prep), ('raw+tess', ext_raw), 
                        ('tess_only_psm6', ext_tess), ('tess_only_psm4', ext_tess4)]:
        print(f'\n  [{label}]')
        for key in ['vendor_name', 'invoice_number', 'invoice_date', 'subtotal', 'tax_amount', 'total_amount', 'confidence', 'ai_note']:
            print(f'    {key}: {ext.get(key, "")!r}')

    # =========================================================================
    # TEST 2: invoice-0-4.pdf (Bioplex invoice)
    # Expected vendor: "Bioplex" (not "Bioplex we love chemistry 5 Rue Bader")
    # =========================================================================
    pdf_path = 'test_inv/invoice-0-4.pdf'
    print(f'\n{"="*70}')
    print(f'TEST 2: {pdf_path}')
    print(f'{"="*70}')
    
    reader_pdf = PdfReader(pdf_path)
    full_text = ""
    for page in reader_pdf.pages:
        page_text = page.extract_text()
        if page_text:
            full_text += page_text + "\n"
    
    print(f'\n  --- PDF extracted text (first 50 lines) ---')
    for i, line in enumerate(full_text.split('\n')[:50]):
        if line.strip():
            print(f'    [{i:3d}] {line}')
    
    lines = [l.strip() for l in full_text.split('\n') if l.strip()]
    vendor = _extract_vendor(lines)
    print(f'\n  Extracted vendor: {vendor!r}')
    
    # Show what _extract_all produces
    ext_pdf = _extract_all(full_text)
    print(f'\n  --- Full extraction ---')
    for key in ['vendor_name', 'invoice_number', 'invoice_date', 'subtotal', 'tax_amount', 'total_amount', 'currency', 'confidence', 'ai_note']:
        print(f'    {key}: {ext_pdf.get(key, "")!r}')
    
    # Debug: Show the first 25 lines and how _extract_vendor processes them
    print(f'\n  --- Vendor extraction debug (first 25 lines) ---')
    import re
    from app.invoice.routes import _NOISE_LINES
    for i, line in enumerate(lines[:25]):
        clean = line.strip()
        clean = re.sub(r'(?i)^(?:sample|draft|copy|void|cancelled)\b[^A-Za-z]*(?:do\s+not\s+pay\b[^A-Za-z]*)?', '', clean).strip()
        noise = bool(_NOISE_LINES.match(clean)) if clean else True
        garbage = _is_garbage_vendor(clean) if clean else True
        has_corp = bool(re.search(r'\b(SDN\.?\s*BHD\.?|BHD\.?|LIMITED|LLP|ENT\.?|ENTERPRISE|PVT\.?\s*LTD|PVT\.?\s*LIMITED)\b', clean, re.IGNORECASE)) if clean else False
        print(f'    [{i:3d}] noise={noise}, garbage={garbage}, corp={has_corp}: {clean!r}')
