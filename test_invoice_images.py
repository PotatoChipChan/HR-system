"""Test invoice OCR on image files using actual UI pipeline."""
import sys, os
sys.path.insert(0, '.')
from app import create_app
from app.invoice.routes import _get_easyocr_reader, _preprocess_image, _get_tesseract_path, _extract_all, _is_garbage_vendor
import pytesseract
from PIL import Image

app = create_app()
with app.app_context():
    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path
    reader = _get_easyocr_reader()

    for f in ['test_inv/Petron.jpg', 'test_inv/CelcomDigi.jpg']:
        print(f'\n=== {f} ===')

        # 1. Preprocessed EasyOCR
        try:
            processed = _preprocess_image(f)
            proc_path = f + '_preprocessed.png'
            processed.save(proc_path)
            results = reader.readtext(proc_path)
            easy_text = '\n'.join([text for _, text, _ in results])
            os.remove(proc_path)
        except:
            easy_text = ''

        # 2. Tesseract second opinion
        try:
            tess_text = pytesseract.image_to_string(Image.open(f), config='--oem 3 --psm 6')
        except:
            tess_text = ''

        full_prep = easy_text + '\n' + tess_text
        ext_prep = _extract_all(full_prep)

        # 3. Check if preprocessed is garbage -> fallback to raw
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
        else:
            ext_final = ext_prep

        for key in ['vendor_name', 'invoice_number', 'invoice_date', 'subtotal', 'tax_amount', 'total_amount']:
            print(f'  {key}: {ext_final.get(key, "")!r}')
        print(f'  confidence: {ext_final.get("confidence", 0)}')
        print(f'  needs_review: {ext_final.get("id_needs_review", "")}')
