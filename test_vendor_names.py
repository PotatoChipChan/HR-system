"""Quick vendor-name check for CelcomDigi.jpg and Screenshot IOI."""
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

    for fpath, expected_vendor in [
        ('test_inv/CelcomDigi.jpg', 'CelcomDigi'),
        ('test_inv/Screenshot 2026-05-27 013400.png', 'CLUB IOI'),
    ]:
        print(f'\n=== {fpath} ===')
        print(f'  Expected vendor contains: {expected_vendor!r}')

        # EasyOCR on preprocessed
        try:
            processed = _preprocess_image(fpath)
            proc_path = fpath + '_pre.png'
            processed.save(proc_path)
            results = reader.readtext(proc_path)
            easy_text = '\n'.join([t for _, t, _ in results])
            os.remove(proc_path)
        except Exception as e:
            easy_text = ''

        print(f'\n  EasyOCR lines (first 12):')
        for line in easy_text.split('\n')[:12]:
            if line.strip():
                print(f'    {line!r}')

        # Tesseract
        try:
            tess_text = pytesseract.image_to_string(Image.open(fpath), config='--oem 3 --psm 6')
        except:
            tess_text = ''

        ext_easy = _extract_all(easy_text)
        ext_easy['raw_text'] = easy_text
        ext_tess = _extract_all(tess_text)
        ext_tess['raw_text'] = tess_text

        from app.invoice.routes import _merge_ocr_results
        merged = _merge_ocr_results(ext_easy, ext_tess)

        print(f'\n  EasyOCR vendor: {ext_easy.get("vendor_name")!r}')
        print(f'  Tesseract vendor: {ext_tess.get("vendor_name")!r}')
        print(f'  Merged vendor: {merged.get("vendor_name")!r}')

        ok = expected_vendor.lower() in (merged.get('vendor_name') or '').lower()
        print(f'  Result: {"PASS" if ok else "FAIL"}')
