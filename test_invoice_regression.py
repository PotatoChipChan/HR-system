"""Regression test for invoice OCR extraction."""
import sys; sys.path.insert(0, '.')
import os

from app.invoice.routes import _extract_vendor, _extract_invoice_id
import pdfplumber

test_cases = [
    ('test_inv/85553_20260429.pdf', {
        'vendor_contains': 'Nicolas',
    }),
    ('test_inv/invoice-0-4.pdf', {
        'vendor_contains': 'Bioplex',
        'invoice_contains': 'BPXINV-00550',
    }),
    ('test_inv/Downloadable-PDF-Invoices-Add-On-Samples.pdf', {
        'vendor_contains': 'Your Business Name',
        'invoice_contains': '13594027',
    }),
    ('test_inv/wordpress-pdf-invoice-plugin-sample.pdf', {
        'vendor_contains': 'DEMO',
        'invoice_contains': 'INV-3337',
    }),
    ('test_inv/receipt_260505M0X3N9FN.pdf', {
        'vendor_contains': 'YS AUTOMART',
        'invoice_contains': '260505M0X3N9FN',
    }),
]

all_ok = True
for path, expected in test_cases:
    print(f'=== {os.path.basename(path)} ===')
    try:
        with pdfplumber.open(path) as pdf:
            text = '\n'.join(page.extract_text() or '' for page in pdf.pages)

        lines = [l.strip() for l in text.split('\n') if l.strip()]
        vendor = _extract_vendor(lines)
        inv_result = _extract_invoice_id(text, lines)
        inv_id = inv_result[0] if inv_result else ''

        vendor_ok = expected.get('vendor_contains', '').lower() in (vendor or '').lower()
        inv_ok = expected.get('invoice_contains', '').lower() in (inv_id or '').lower()

        print(f'  Vendor: {vendor} {"OK" if vendor_ok else "FAIL"}')
        print(f'  Invoice: {inv_id} {"OK" if inv_ok else "SKIP" if "invoice_contains" not in expected else "FAIL"}')

        file_ok = vendor_ok and inv_ok
        all_ok = all_ok and file_ok
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'  ERROR: {e}')
        all_ok = False
    print()

print(f'{"ALL PASS" if all_ok else "SOME FAILED"}')
