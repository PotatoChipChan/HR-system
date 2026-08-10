"""Batch test for all 100+ invoices: PDF (geo_extract) + Image (OCR pipeline)."""

import json, csv, os, sys, glob
from datetime import datetime

sys.path.insert(0, '.')
from PIL import Image
import pytesseract
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

from app.invoice.geo_extract import geo_extract
from app import create_app
app = create_app()

GT_PATH = 'test_batch/ground_truth/ground_truth.json'
PDF_DIR = 'test_batch/pdf'
IMG_DIR = 'test_batch/image'
OUT_CSV = 'test_batch_100_results.csv'

def load_ground_truth():
    with open(GT_PATH) as f:
        return json.load(f)

def test_pdf(pdf_path, gt):
    r = geo_extract(pdf_path)
    return {
        'vendor_ok': (r.get('vendor_name','') or '').strip().lower() == gt.get('vendor_name','').strip().lower(),
        'inv_ok': (r.get('invoice_number','') or '').replace('#','').strip() == gt.get('invoice_number','').replace('#','').strip(),
        'sub_ok': abs(r.get('subtotal',0) - gt.get('subtotal',0)) < 0.01,
        'tax_ok': abs(r.get('tax_amount',0) - gt.get('tax',0)) < 0.01,
        'ship_ok': abs(r.get('shipping',0) - gt.get('shipping',0)) < 0.01,
        'disc_ok': abs(r.get('discount',0) - gt.get('discount',0)) < 0.01,
        'tot_ok': abs(r.get('total_amount',0) - gt.get('total',0)) < 0.01,
        'confidence': r.get('confidence', 0),
        'math_valid': r.get('math_valid', False),
        'vendor_found': r.get('vendor_name','') or '',
        'sub_found': r.get('subtotal',0),
        'tax_found': r.get('tax_amount',0),
        'ship_found': r.get('shipping',0),
        'disc_found': r.get('discount',0),
        'tot_found': r.get('total_amount',0),
        'inv_found': r.get('invoice_number',''),
    }

def test_image(img_path, gt, app_ctx):
    from app.invoice.routes import _extract_all
    try:
        img = Image.open(img_path)
        raw = pytesseract.image_to_string(img)
        with app_ctx:
            r = _extract_all(raw)
    except Exception as e:
        return {'error': str(e)}
    
    return {
        'vendor_ok': (r.get('vendor_name','') or '').strip().lower() == gt.get('vendor_name','').strip().lower(),
        'inv_ok': (r.get('invoice_number','') or '').replace('#','').strip() == gt.get('invoice_number','').replace('#','').strip(),
        'sub_ok': abs(r.get('subtotal',0) - gt.get('subtotal',0)) < 0.01,
        'tax_ok': abs(r.get('tax_amount',0) - gt.get('tax',0)) < 0.01,
        'tot_ok': abs(r.get('total_amount',0) - gt.get('total',0)) < 0.01,
        'confidence': r.get('confidence', 0),
        'vendor_found': r.get('vendor_name','') or '',
        'sub_found': r.get('subtotal',0),
        'tax_found': r.get('tax_amount',0),
        'tot_found': r.get('total_amount',0),
        'inv_found': r.get('invoice_number',''),
    }

def main():
    gt = load_ground_truth()
    
    rows = []
    # Group by name (each invoice has PDF + image)
    for name, d in sorted(gt.items()):
        pdf_path = os.path.join(PDF_DIR, d['file_pdf'])
        img_path = os.path.join(IMG_DIR, d['file_image'])
        
        row = {
            'name': name,
            'template': d.get('template',''),
            'gt_vendor': d['vendor_name'],
            'gt_inv': d['invoice_number'],
            'gt_sub': d['subtotal'],
            'gt_tax': d['tax'],
            'gt_ship': d.get('shipping',0),
            'gt_disc': d.get('discount',0),
            'gt_tot': d['total'],
            'img_score': 'N/A', 'img_vendor': '', 'img_inv': '',
            'img_sub': '', 'img_tax': '', 'img_tot': '',
            'img_conf': '', 'img_vendor_found': '', 'img_inv_found': '',
            'img_sub_found': '', 'img_tax_found': '', 'img_tot_found': '',
            'img_error': '',
            'pdf_score': 'N/A', 'pdf_vendor': '', 'pdf_inv': '',
            'pdf_sub': '', 'pdf_tax': '', 'pdf_ship': '', 'pdf_disc': '', 'pdf_tot': '',
            'pdf_conf': '', 'pdf_math': '', 'pdf_vendor_found': '', 'pdf_inv_found': '',
            'pdf_sub_found': '', 'pdf_tax_found': '', 'pdf_ship_found': '', 'pdf_disc_found': '', 'pdf_tot_found': '',
        }
        
        # Test PDF
        if os.path.exists(pdf_path):
            pr = test_pdf(pdf_path, d)
            row['pdf_vendor'] = pr.get('vendor_ok','?')
            row['pdf_inv'] = pr.get('inv_ok','?')
            row['pdf_sub'] = pr.get('sub_ok','?')
            row['pdf_tax'] = pr.get('tax_ok','?')
            row['pdf_ship'] = pr.get('ship_ok','?')
            row['pdf_disc'] = pr.get('disc_ok','?')
            row['pdf_tot'] = pr.get('tot_ok','?')
            row['pdf_conf'] = pr.get('confidence',0)
            row['pdf_math'] = pr.get('math_valid',False)
            row['pdf_vendor_found'] = pr.get('vendor_found','')
            row['pdf_inv_found'] = pr.get('inv_found','')
            row['pdf_sub_found'] = pr.get('sub_found',0)
            row['pdf_tax_found'] = pr.get('tax_found',0)
            row['pdf_ship_found'] = pr.get('ship_found',0)
            row['pdf_disc_found'] = pr.get('disc_found',0)
            row['pdf_tot_found'] = pr.get('tot_found',0)
            n_pdf_ok = sum(1 for k in ['sub','tax','ship','disc','tot','vendor','inv'] if pr.get(k+'_ok',False))
            row['pdf_score'] = f"{n_pdf_ok}/7"
        else:
            row['pdf_score'] = 'N/A'
        
        # Test Image
        if os.path.exists(img_path):
            ir = test_image(img_path, d, app.app_context())
            if 'error' in ir:
                row['img_error'] = ir['error']
                row['img_score'] = 'ERR'
            else:
                row['img_vendor'] = ir.get('vendor_ok','?')
                row['img_inv'] = ir.get('inv_ok','?')
                row['img_sub'] = ir.get('sub_ok','?')
                row['img_tax'] = ir.get('tax_ok','?')
                row['img_tot'] = ir.get('tot_ok','?')
                row['img_conf'] = ir.get('confidence',0)
                row['img_vendor_found'] = ir.get('vendor_found','')
                row['img_inv_found'] = ir.get('inv_found','')
                row['img_sub_found'] = ir.get('sub_found',0)
                row['img_tax_found'] = ir.get('tax_found',0)
                row['img_tot_found'] = ir.get('tot_found',0)
                n_img_ok = sum(1 for k in ['sub','tax','tot','vendor','inv'] if ir.get(k+'_ok',False))
                row['img_score'] = f"{n_img_ok}/5"
        else:
            row['img_score'] = 'N/A'
        
        rows.append(row)
    
    # Write CSV
    if rows:
        fieldnames = list(rows[0].keys())
        with open(OUT_CSV, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
    
    # Summary
    pdf_entries = [r for r in rows if r['pdf_score'] != 'N/A']
    img_entries = [r for r in rows if r.get('img_score','N/A') not in ('N/A', 'ERR', '')]
    
    pdf_full = sum(1 for r in pdf_entries if r['pdf_score'] == '7/7')
    img_full = sum(1 for r in img_entries if r['img_score'] == '5/5')
    
    print(f"=== Batch Test Results ===")
    print(f"PDF (geo_extract): {pdf_full}/{len(pdf_entries)} full pass ({pdf_full/len(pdf_entries)*100:.1f}%)")
    print(f"Image (OCR):       {img_full}/{len(img_entries)} full pass ({img_full/len(img_entries)*100:.1f}%)")
    print(f"\nResults saved to: {OUT_CSV}")
    
    # Show failed images
    print(f"\n--- Image failures ---")
    for r in img_entries:
        s = r['img_score']
        if s != '5/5':
            print(f"  {r['name'][:30]}: {s} (sub={r.get('img_sub','?')} tax={r.get('img_tax','?')} tot={r.get('img_tot','?')} vendor={r.get('img_vendor','?')} inv={r.get('img_inv','?')})")

if __name__ == '__main__':
    main()
