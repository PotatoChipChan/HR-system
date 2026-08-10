"""Debug raw OCR text from both images to see what's actually extracted."""
import sys, os
sys.path.insert(0, '.')
from app import create_app
from app.invoice.routes import _get_easyocr_reader, _preprocess_image, _get_tesseract_path
import pytesseract
from PIL import Image

app = create_app()
with app.app_context():
    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path
    reader = _get_easyocr_reader()

    for f in ['test_inv/Petron.jpg', 'test_inv/CelcomDigi.jpg']:
        print(f'\n{"="*60}')
        print(f'FILE: {f}')
        print(f'{"="*60}')

        # Raw EasyOCR (no preprocessing)
        print('\n--- Raw EasyOCR ---')
        try:
            results = reader.readtext(f)
            for bbox, text, conf in results:
                print(f'  [{conf:.2f}] {text!r}')
            raw_easy = '\n'.join([r[1] for r in results])
        except Exception as e:
            print(f'  ERROR: {e}')
            raw_easy = ''

        # Preprocessed EasyOCR
        print('\n--- Preprocessed EasyOCR ---')
        try:
            processed = _preprocess_image(f)
            proc_path = f + '_preprocessed.png'
            processed.save(proc_path)
            results = reader.readtext(proc_path)
            for bbox, text, conf in results:
                print(f'  [{conf:.2f}] {text!r}')
            proc_easy = '\n'.join([r[1] for r in results])
            os.remove(proc_path)
        except Exception as e:
            print(f'  ERROR: {e}')
            proc_easy = ''

        # Raw Tesseract
        print('\n--- Raw Tesseract ---')
        try:
            tess_text = pytesseract.image_to_string(Image.open(f), config='--oem 3 --psm 6')
            print(tess_text[:2000])
        except Exception as e:
            print(f'  ERROR: {e}')
            tess_text = ''
