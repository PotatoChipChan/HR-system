"""Debug vendor extraction with combined text."""
import sys
sys.path.insert(0, '.')
from app import create_app
from app.invoice.routes import _extract_all, _get_tesseract_path, _extract_vendor
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
    
    combined = easy_text + '\n' + tess_text
    lines = [l.strip() for l in combined.split('\n') if l.strip()]
    
    print(f'Total lines: {len(lines)}')
    print(f'First 10 lines:')
    for i, line in enumerate(lines[:10]):
        print(f'  {i}: {line!r}')
    
    # Check what _extract_all does to the text
    ext = _extract_all(combined)
    print(f'\nVendor: {ext["vendor_name"]!r}')
    print(f'Subtotal: {ext["subtotal"]}')
    print(f'Total: {ext["total_amount"]}')
