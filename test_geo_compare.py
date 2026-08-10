"""
Test: Compare old (PyPDF2 + regex) vs new (pdfplumber + geo_extract) on all PDFs in test_inv/.

Run: python test_geo_compare.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path
import json

# -- Old extraction (PyPDF2 + regex) --
def old_extract(pdf_path):
    """Original extraction: PyPDF2 text + _extract_all regex."""
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(str(pdf_path))
        full_text = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                full_text += page_text + "\n"

        if not full_text.strip():
            return {'raw_text': '', 'error': 'no text extracted'}

        from app.invoice.routes import _extract_all
        result = _extract_all(full_text)
        result['raw_text'] = full_text
        return result
    except Exception as e:
        return {'raw_text': '', 'error': str(e)}


# -- New extraction (pdfplumber + geo_extract) --
def new_extract(pdf_path):
    """New extraction: pdfplumber bounding boxes + geometric analysis."""
    from app.invoice.geo_extract import geo_extract
    return geo_extract(str(pdf_path))


# -- Test cases with expected values --
TEST_DIR = Path(__file__).parent / 'test_inv'

EXPECTED = {
    'invoice_Aaron Bergman_36258.pdf': {
        'vendor_contains': 'SuperStore',  # SuperStore is the vendor, Aaron Bergman is the customer
        'total': 50.10,
        'subtotal': 48.71,
    },
    'invoice_Aaron Bergman_36259.pdf': {
        'vendor_contains': 'SuperStore',  # SuperStore is the vendor, Aaron Bergman is the customer
        'total': 58.11,
        'subtotal': 53.82,
    },
    'wordpress-pdf-invoice-plugin-sample.pdf': {
        'vendor_contains': 'DEMO',
        'total': 93.50,
        'subtotal': 85.00,
        'tax': 8.50,
    },
    'invoice-0-4.pdf': {
        'vendor_contains': 'Bioplex',
        'total': 6610.95,
        'subtotal': 5964.50,
        'tax': 596.45,
    },
    '85553_20260429.pdf': {
        'vendor_contains': 'Nicolas',
    },
    'receipt_260505M0X3N9FN.pdf': {
        'vendor_contains': 'YS AUTOMART',
    },
    'Downloadable-PDF-Invoices-Add-On-Samples.pdf': {
        'invoice_contains': '13594027',
    },
}


def check_field(label, actual, expected_val, tolerance=0.50):
    """Check if actual matches expected within tolerance."""
    if expected_val is None:
        return None
    if actual is None:
        actual = 0
    match = abs(actual - expected_val) <= tolerance
    status = '[OK]' if match else '[FAIL]'
    print(f'  {label}: {actual} (expected {expected_val}) {status}')
    return match


def check_contains(label, actual, expected_substr):
    """Check if actual string contains expected substring."""
    if expected_substr is None:
        return None
    if not actual:
        actual = ''
    match = expected_substr.lower() in actual.lower()
    status = '[OK]' if match else '[FAIL]'
    print(f'  {label}: "{actual[:50]}" (expected contains "{expected_substr}") {status}')
    return match


def main():
    print('=' * 70)
    print('Invoice Extraction Comparison: Old (PyPDF2) vs New (pdfplumber + geo)')
    print('=' * 70)

    results = []
    all_pass = True

    for filename, expected in sorted(EXPECTED.items()):
        pdf_path = TEST_DIR / filename
        if not pdf_path.exists():
            print(f'\n  WARNING: {filename}: NOT FOUND, skipping')
            continue

        print(f'\n{"-" * 70}')
        print(f'PDF: {filename}')
        print(f'{"-" * 70}')

        old_result = old_extract(pdf_path)
        new_result = new_extract(pdf_path)

        old_text_len = len(old_result.get('raw_text', ''))
        new_text_len = len(new_result.get('raw_text', ''))
        print(f'  Old text: {old_text_len} chars | New text: {new_text_len} chars')

        file_pass = True
        if 'vendor_contains' in expected:
            old_vendor = old_result.get('vendor_name', '')
            new_vendor = new_result.get('vendor_name', '')
            print(f'\n  --- Vendor ---')
            old_ok = check_contains('Old', old_vendor, expected['vendor_contains'])
            new_ok = check_contains('New', new_vendor, expected['vendor_contains'])
            if new_ok is False and old_ok is True:
                file_pass = False
                all_pass = False

        if 'invoice_contains' in expected:
            old_inv = old_result.get('invoice_number', '')
            new_inv = new_result.get('invoice_number', '')
            print(f'\n  --- Invoice Number ---')
            old_ok = check_contains('Old', old_inv, expected['invoice_contains'])
            new_ok = check_contains('New', new_inv, expected['invoice_contains'])
            if new_ok is False and old_ok is True:
                file_pass = False
                all_pass = False

        if 'total' in expected:
            old_total = old_result.get('total_amount', 0)
            new_total = new_result.get('total_amount', 0)
            print(f'\n  --- Total ---')
            old_ok = check_field('Old', old_total, expected['total'])
            new_ok = check_field('New', new_total, expected['total'])
            if new_ok is False and old_ok is True:
                file_pass = False
                all_pass = False

        if 'subtotal' in expected:
            old_sub = old_result.get('subtotal', 0)
            new_sub = new_result.get('subtotal', 0)
            print(f'\n  --- Subtotal ---')
            old_ok = check_field('Old', old_sub, expected['subtotal'])
            new_ok = check_field('New', new_sub, expected['subtotal'])
            if new_ok is False and old_ok is True:
                file_pass = False
                all_pass = False

        if 'tax' in expected:
            old_tax = old_result.get('tax_amount', 0)
            new_tax = new_result.get('tax_amount', 0)
            print(f'\n  --- Tax ---')
            old_ok = check_field('Old', old_tax, expected['tax'])
            new_ok = check_field('New', new_tax, expected['tax'])
            if new_ok is False and old_ok is True:
                file_pass = False
                all_pass = False

        print(f'\n  --- New extractor info ---')
        print(f'  Confidence: {new_result.get("confidence", 0):.2f}')
        print(f'  Math valid: {new_result.get("math_valid", False)}')
        cands = new_result.get('candidates', {})
        if cands:
            for field, pairs in cands.items():
                vals = [(v, l) for v, l in pairs[:3]]
                print(f'  Candidates [{field}]: {vals}')

        status = '[PASS]' if file_pass else '[REGRESSION]'
        print(f'\n  Result: {status}')
        results.append((filename, file_pass))

    print(f'\n{"=" * 70}')
    print('SUMMARY')
    print(f'{"=" * 70}')
    for filename, passed in results:
        status = '[OK]' if passed else '[FAIL]'
        print(f'  {status} {filename}')

    total = len(results)
    passed = sum(1 for _, p in results if p)
    print(f'\n  {passed}/{total} passed')
    if all_pass:
        print('\n  No regressions -- new extraction is at least as good as old on all files')
    else:
        print('\n  Regressions detected -- some files are worse with new extraction')
    print(f'{"=" * 70}')

    return 0 if all_pass else 1


if __name__ == '__main__':
    sys.exit(main())
