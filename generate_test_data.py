"""
Synthetic invoice generator for batch testing.
Generates 50 native-text PDFs + 50 PNG images with diverse layouts + known ground truth.
"""

import os, json, random, math
from io import BytesIO
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.units import mm, cm
from reportlab.lib import colors
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                 Spacer, Frame, PageTemplate, BaseDocTemplate)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.platypus.doctemplate import PageTemplate
from reportlab.platypus.frames import Frame
import pypdfium2 as pdfium
from PIL import Image

random.seed(42)

# ── Output directories ──────────────────────────────────────────────
OUT_DIR = 'test_batch'
PDF_DIR = os.path.join(OUT_DIR, 'pdf')
IMG_DIR = os.path.join(OUT_DIR, 'image')
GT_DIR  = os.path.join(OUT_DIR, 'ground_truth')
os.makedirs(PDF_DIR, exist_ok=True)
os.makedirs(IMG_DIR, exist_ok=True)
os.makedirs(GT_DIR, exist_ok=True)

# ── Vendor / company catalog ────────────────────────────────────────
VENDORS = [
    {'name': 'Acme Corporation', 'addr': '123 Industrial Blvd, Springfield, IL 62701', 'reg': 'ACME-2024-001'},
    {'name': 'Global Tech Solutions', 'addr': '45 Innovation Drive, San Jose, CA 95134', 'reg': 'GTS-98765'},
    {'name': 'Pinnacle Supplies Ltd', 'addr': '78 Commerce Way, London EC2A 1PQ, UK', 'reg': 'GB123456789'},
    {'name': 'Nordic Enterprises AB', 'addr': 'Strandvagen 1, 114 51 Stockholm, Sweden', 'reg': 'SE556000-1234'},
    {'name': 'Pacific Rim Trading', 'addr': '8 Shenton Way, #20-01, Singapore 068811', 'reg': '202012345K'},
    {'name': 'Berliner Handels GmbH', 'addr': 'Friedrichstrasse 100, 10117 Berlin, Germany', 'reg': 'DE123456789'},
    {'name': 'Maple Leaf Industries', 'addr': '200 King St W, Toronto, ON M5V 3C6', 'reg': 'GST123456789RT0001'},
    {'name': 'Sunrise Distributors', 'addr': '1-2-3 Minami Aoyama, Minato-ku, Tokyo 107-0062', 'reg': 'T4010001012345'},
    {'name': 'Outback Office Supplies', 'addr': '45 George St, Sydney NSW 2000, Australia', 'reg': 'ABN 12 345 678 901'},
    {'name': 'Delta Services SARL', 'addr': '15 Rue de la Paix, 75002 Paris, France', 'reg': 'FR12345678901'},
]

INVOICE_PREFIXES = ['INV', 'INV-', '#', 'INVOICE-', 'I', 'ORD-', 'RCPT-', '']

# ── Invoice data generator ──────────────────────────────────────────

def rand_price(low=5, high=2000):
    return round(random.uniform(low, high), 2)

def pick_random(lst):
    return lst[random.randint(0, len(lst) - 1)]

DESCRIPTIONS = [
    'Consulting Services (Hourly)', 'Software License - Annual', 'Web Hosting - Monthly',
    'Office Supplies Bundle', 'Network Equipment', 'Security Audit Service',
    'Cloud Storage (500GB)', 'Technical Support (Premium)', 'Data Migration Service',
    'Hardware Maintenance Contract', 'Professional Training Session', 'API Access Subscription',
    'Server Rack Rental', 'SSL Certificate Renewal', 'Domain Registration (5 years)',
    'Email Hosting Service', 'Backup Solution License', 'VPN Access (10 seats)',
    'Performance Optimization', 'UI/UX Design Service', 'Content Writing Package',
    'Social Media Management', 'SEO Audit Report', 'Mobile App Maintenance',
    'Database Optimization', 'Penetration Testing', 'Compliance Consultation',
    'Employee Onboarding Kit', 'Marketing Collateral Design', 'Video Production Service',
]

def generate_line_items(count=None):
    if count is None:
        count = random.randint(3, 12)
    items = []
    sub = 0
    for _ in range(count):
        desc = pick_random(DESCRIPTIONS)
        qty = random.randint(1, 20)
        unit = rand_price(5, 500)
        total = round(qty * unit, 2)
        items.append({'description': desc, 'quantity': qty, 'unit_price': unit, 'total': total})
        sub += total
    sub = round(sub, 2)
    return items, sub

def generate_invoice_data(invoice_id, template=None):
    v = pick_random(VENDORS)
    prefix = pick_random(INVOICE_PREFIXES)
    inv_num = prefix + str(random.randint(10000, 99999))
    items, subtotal = generate_line_items()

    # Decide if discount and shipping are present
    has_discount = random.random() < 0.4
    has_shipping = random.random() < 0.3
    has_tax = random.random() < 0.85

    discount = 0
    shipping = 0
    if has_discount:
        discount = round(subtotal * random.uniform(0.05, 0.20), 2)
    if has_shipping:
        shipping = rand_price(5, 50)

    tax_rate = pick_random([0.0, 0.06, 0.08, 0.10, 0.13, 0.19, 0.20, 0.25])
    if not has_tax:
        tax_rate = 0.0
    tax = round((subtotal - discount) * tax_rate, 2)

    total = round(subtotal - discount + shipping + tax, 2)

    # Multi-tax possibility (like SST + service tax)
    has_second_tax = random.random() < 0.08 and tax_rate > 0
    tax2 = 0
    tax2_label = ''
    if has_second_tax:
        tax2 = round((subtotal - discount) * 0.06, 2)
        tax2_label = 'Service Tax (6%)'
        total = round(total + tax2, 2)

    # Templates that don't display discount/shipping/tax2 need them zeroed
    if template == 'layout_eu_vat':
        discount = 0
        shipping = 0
        tax2 = 0
        tax2_label = ''
        total = round(subtotal + tax, 2)

    return {
        'id': invoice_id,
        'vendor_name': v['name'],
        'vendor_address': v['addr'],
        'vendor_reg': v['reg'],
        'invoice_number': inv_num,
        'date': '2026-%02d-%02d' % (random.randint(1, 12), random.randint(1, 28)),
        'terms': 'Net %d' % random.choice([15, 30, 45, 60]),
        'due_date': '2026-%02d-%02d' % (random.randint(1, 12), random.randint(1, 28)),
        'currency': pick_random(['USD', 'EUR', 'GBP', 'MYR', 'AUD', 'SGD']),
        'line_items': items,
        'subtotal': subtotal,
        'discount': discount,
        'shipping': shipping,
        'tax': tax,
        'tax_label': 'VAT (%.0f%%)' % (tax_rate * 100) if tax_rate > 0 else '',
        'tax_rate': tax_rate,
        'tax2': tax2,
        'tax2_label': tax2_label,
        'total': total,
    }


# ── Layout templates ────────────────────────────────────────────────

def _make_styles(base_font='Helvetica', title_font='Helvetica-Bold', size=10):
    styles = getSampleStyleSheet()
    normal = ParagraphStyle('InvNormal', parent=styles['Normal'],
                            fontName=base_font, fontSize=size, leading=size*1.2)
    bold = ParagraphStyle('InvBold', parent=normal, fontName=title_font)
    small = ParagraphStyle('InvSmall', parent=normal, fontSize=size-2)
    title = ParagraphStyle('InvTitle', parent=normal, fontName=title_font, fontSize=size+6, spaceAfter=4*mm)
    right = ParagraphStyle('InvRight', parent=normal, alignment=TA_RIGHT)
    center = ParagraphStyle('InvCenter', parent=normal, alignment=TA_CENTER)
    return normal, bold, small, title, right, center


def _p(text, style):
    return Paragraph(text.replace('\n', '<br/>'), style)


class LayoutRegistry:

    def __init__(self):
        self.layouts = []

    def register(self, fn):
        self.layouts.append(fn)
        return fn

    def all(self):
        return self.layouts


layout_registry = LayoutRegistry()


# ── Layout Template 1: Classic bordered table, summary right ────────
@layout_registry.register
def layout_classic(data, pdf_path):
    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    normal, bold, small, title, right, center = _make_styles()
    elems = []
    # Header
    elems.append(_p('<b>%s</b>' % data['vendor_name'], title))
    elems.append(_p(data['vendor_address'], small))
    elems.append(_p('Reg: %s' % data['vendor_reg'], small))
    elems.append(Spacer(1, 8*mm))
    # Invoice info
    info_style = normal if random.random() < 0.5 else right
    elems.append(_p('<b>Invoice:</b> %s' % data['invoice_number'], info_style))
    elems.append(_p('<b>Date:</b> %s' % data['date'], info_style))
    elems.append(_p('<b>Terms:</b> %s' % data['terms'], info_style))
    elems.append(Spacer(1, 6*mm))
    # Table
    table_data = [['#', 'Description', 'Qty', 'Unit Price', 'Amount']]
    for i, item in enumerate(data['line_items'], 1):
        table_data.append([
            str(i), item['description'], str(item['quantity']),
            '%s %.2f' % (data['currency'], item['unit_price']),
            '%s %.2f' % (data['currency'], item['total']),
        ])
    col_widths = [12*mm, 80*mm, 15*mm, 30*mm, 30*mm]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#4472C4')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('ALIGN', (2,0), (-1,-1), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#D9E2F3')]),
    ]))
    elems.append(t)
    elems.append(Spacer(1, 4*mm))
    # Summary block (right-aligned)
    summary_lines = [('<b>Subtotal:</b>', '%s %.2f' % (data['currency'], data['subtotal']))]
    if data['discount']:
        summary_lines.append(('<b>Discount:</b>', '-%s %.2f' % (data['currency'], data['discount'])))
    if data['shipping']:
        summary_lines.append(('<b>Shipping:</b>', '%s %.2f' % (data['currency'], data['shipping'])))
    if data['tax']:
        summary_lines.append(('<b>%s:</b>' % data['tax_label'], '%s %.2f' % (data['currency'], data['tax'])))
    if data['tax2']:
        summary_lines.append(('<b>%s:</b>' % data['tax2_label'], '%s %.2f' % (data['currency'], data['tax2'])))
    summary_lines.append(('<b>Total:</b>', '%s %.2f' % (data['currency'], data['total'])))
    summary_data = [[_p(l, right), _p(v, right)] for l, v in summary_lines]
    st = Table(summary_data, colWidths=[50*mm, 40*mm])
    st.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('LINEABOVE', (0,-1), (-1,-1), 1.5, colors.black),
        ('TOPPADDING', (0,0), (-1,-1), 3),
    ]))
    elems.append(st)
    doc.build(elems)
    return True


# ── Layout Template 2: Minimal, borderless, inline summary ──────────
@layout_registry.register
def layout_minimal(data, pdf_path):
    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                            leftMargin=25*mm, rightMargin=25*mm,
                            topMargin=15*mm, bottomMargin=15*mm)
    normal, bold, small, title, right, center = _make_styles('Helvetica', size=9)
    elems = []
    elems.append(_p('<b>%s</b>' % data['vendor_name'], ParagraphStyle('Big', parent=normal, fontSize=16, spaceAfter=2*mm)))
    elems.append(Spacer(1, 2*mm))
    # Two-column info block
    info_data = [
        [_p('<b>Invoice #:</b> %s' % data['invoice_number'], normal), _p('<b>Date:</b> %s' % data['date'], right)],
        [_p('<b>Terms:</b> %s' % data['terms'], normal), _p('<b>Due:</b> %s' % data['due_date'], right)],
    ]
    it = Table(info_data, colWidths=[90*mm, 90*mm])
    it.setStyle(TableStyle([('FONTNAME', (0,0), (-1,-1), 'Helvetica'), ('FONTSIZE', (0,0), (-1,-1), 9)]))
    elems.append(it)
    elems.append(Spacer(1, 6*mm))
    # Items table (borderless)
    th_data = [['Item', 'Qty', 'Price', 'Total']]
    for item in data['line_items']:
        th_data.append([
            item['description'], str(item['quantity']),
            '%s %.2f' % (data['currency'], item['unit_price']),
            '%s %.2f' % (data['currency'], item['total']),
        ])
    cw = [80*mm, 15*mm, 30*mm, 30*mm]
    tt = Table(th_data, colWidths=cw)
    ts = [('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
          ('FONTSIZE', (0,0), (-1,-1), 8),
          ('LINEBELOW', (0,0), (-1,0), 1, colors.black),
          ('LINEBELOW', (0,-1), (-1,-1), 0.5, colors.grey),
          ('ALIGN', (1,1), (-1,-1), 'RIGHT'),
          ('TOPPADDING', (0,0), (-1,-1), 2),
          ('BOTTOMPADDING', (0,0), (-1,-1), 2),
         ]
    ts.append(('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'))
    tt.setStyle(TableStyle(ts))
    elems.append(tt)
    elems.append(Spacer(1, 3*mm))
    # Summary inline
    sdata = [[_p('<b>Subtotal:</b>', normal), _p('%s %.2f' % (data['currency'], data['subtotal']), right)]]
    if data['discount']:
        sdata.append([_p('<b>Discount:</b>', normal), _p('-%s %.2f' % (data['currency'], data['discount']), right)])
    if data['shipping']:
        sdata.append([_p('<b>Shipping:</b>', normal), _p('%s %.2f' % (data['currency'], data['shipping']), right)])
    if data['tax']:
        sdata.append([_p('<b>%s:</b>' % data['tax_label'], normal), _p('%s %.2f' % (data['currency'], data['tax']), right)])
    if data['tax2']:
        sdata.append([_p('<b>%s:</b>' % data['tax2_label'], normal), _p('%s %.2f' % (data['currency'], data['tax2']), right)])
    sdata.append([_p('<b>Total Due:</b>', normal), _p('%s %.2f' % (data['currency'], data['total']), right)])
    st2 = Table(sdata, colWidths=[50*mm, 40*mm])
    st2.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('LINEABOVE', (0,-1), (-1,-1), 2, colors.black),
        ('TOPPADDING', (0,0), (-1,-1), 2),
    ]))
    elems.append(st2)
    doc.build(elems)
    return True


# ── Layout Template 3: Stacked label-value, receipt style ───────────
@layout_registry.register
def layout_receipt(data, pdf_path):
    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                            leftMargin=30*mm, rightMargin=30*mm,
                            topMargin=15*mm, bottomMargin=15*mm)
    normal, bold, small, title, right, center = _make_styles('Courier', size=9)
    elems = []
    elems.append(_p('<b>%s</b>' % data['vendor_name'], center))
    elems.append(_p(data['vendor_address'], center))
    elems.append(_p('Reg: %s' % data['vendor_reg'], center))
    elems.append(Spacer(1, 4*mm))
    elems.append(_p('=' * 60, center))
    elems.append(_p('INVOICE # %s' % data['invoice_number'], center))
    elems.append(_p('Date: %s' % data['date'], center))
    elems.append(_p('=' * 60, center))
    elems.append(Spacer(1, 3*mm))
    # Items as stacked lines
    elems.append(_p('<b>QTY  DESCRIPTION                   AMOUNT</b>', normal))
    elems.append(_p('-' * 50, normal))
    for item in data['line_items']:
        desc = item['description'][:30].ljust(30)
        line = '%3d  %s  %s %7.2f' % (item['quantity'], desc, data['currency'], item['total'])
        elems.append(_p(line, normal))
    elems.append(Spacer(1, 3*mm))
    elems.append(_p('-' * 50, normal))
    # Summary (show all intermediate values)
    summary_items = [('SUBTOTAL', '%s %.2f' % (data['currency'], data['subtotal']))]
    if data['discount']:
        summary_items.append(('DISCOUNT', '-%s %.2f' % (data['currency'], data['discount'])))
    if data['shipping']:
        summary_items.append(('SHIPPING', '%s %.2f' % (data['currency'], data['shipping'])))
    if data['tax']:
        summary_items.append((data['tax_label'][:15], '%s %.2f' % (data['currency'], data['tax'])))
    if data['tax2']:
        summary_items.append((data['tax2_label'][:15], '%s %.2f' % (data['currency'], data['tax2'])))
    summary_items.append(('TOTAL', '%s %.2f' % (data['currency'], data['total'])))
    sdata2 = [[_p(l, normal), _p(v, right)] for l, v in summary_items]
    st3 = Table(sdata2, colWidths=[60*mm, 40*mm])
    st3.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Courier'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('LINEABOVE', (0,-1), (-1,-1), 1, colors.black),
    ]))
    elems.append(st3)
    doc.build(elems)
    return True


# ── Layout Template 4: Boxed summary, colored header ────────────────
@layout_registry.register
def layout_boxed(data, pdf_path):
    doc = SimpleDocTemplate(pdf_path, pagesize=letter,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    normal, bold, small, title, right, center = _make_styles('Helvetica', size=9)
    elems = []
    # Header colored box
    hdr_color = pick_random(['#2F5496', '#1F4E79', '#385723', '#833C0B', '#BF8F00'])
    hdr_data = [[_p('<b>%s</b>' % data['vendor_name'], ParagraphStyle('W', parent=normal, textColor=colors.white, fontSize=14)),
                 _p('Invoice # %s<br/>Date: %s' % (data['invoice_number'], data['date']),
                    ParagraphStyle('WR', parent=normal, textColor=colors.white, alignment=TA_RIGHT))]]
    ht = Table(hdr_data, colWidths=[90*mm, 90*mm])
    ht.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor(hdr_color)),
        ('TEXTCOLOR', (0,0), (-1,-1), colors.white),
        ('TOPPADDING', (0,0), (-1,-1), 6*mm),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6*mm),
        ('LEFTPADDING', (0,0), (-1,-1), 6*mm),
        ('RIGHTPADDING', (0,0), (-1,-1), 6*mm),
    ]))
    elems.append(ht)
    elems.append(Spacer(1, 6*mm))
    # Items table
    th_data2 = [['Description', 'Quantity', 'Unit Price', 'Total']]
    for item in data['line_items']:
        th_data2.append([
            item['description'], str(item['quantity']),
            '%s %.2f' % (data['currency'], item['unit_price']),
            '%s %.2f' % (data['currency'], item['total']),
        ])
    cw2 = [80*mm, 18*mm, 28*mm, 28*mm]
    tt2 = Table(th_data2, colWidths=cw2)
    tt2.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('GRID', (0,0), (-1,-1), 0.5, colors.lightgrey),
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor(hdr_color)),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('ALIGN', (1,1), (-1,-1), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    elems.append(tt2)
    elems.append(Spacer(1, 5*mm))
    # Boxed summary
    box_lines = [('<b>Subtotal</b>', '%s %.2f' % (data['currency'], data['subtotal']))]
    if data['discount']:
        box_lines.append(('<b>Discount</b>', '-%s %.2f' % (data['currency'], data['discount'])))
    if data['shipping']:
        box_lines.append(('<b>Shipping</b>', '%s %.2f' % (data['currency'], data['shipping'])))
    if data['tax']:
        box_lines.append(('<b>%s</b>' % data['tax_label'], '%s %.2f' % (data['currency'], data['tax'])))
    if data['tax2']:
        box_lines.append(('<b>%s</b>' % data['tax2_label'], '%s %.2f' % (data['currency'], data['tax2'])))
    box_lines.append(('<b>Total Due</b>', '%s %.2f' % (data['currency'], data['total'])))
    bd = [[_p(l, normal), _p(v, right)] for l, v in box_lines]
    bt = Table(bd, colWidths=[50*mm, 40*mm])
    bt.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('BOX', (0,0), (-1,-1), 1.5, colors.HexColor(hdr_color)),
        ('LINEABOVE', (0,-1), (-1,-1), 2, colors.HexColor(hdr_color)),
        ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#E8F0FE')),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('RIGHTPADDING', (0,0), (-1,-1), 8),
    ]))
    elems.append(bt)
    doc.build(elems)
    return True


# ── Layout Template 5: Two-column, label on left, value on right ────
@layout_registry.register
def layout_twocol(data, pdf_path):
    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    normal, bold, small, title, right, center = _make_styles('Helvetica', size=10)
    elems = []
    elems.append(_p('<b>%s</b>' % data['vendor_name'], ParagraphStyle('H', parent=normal, fontSize=15, spaceAfter=2*mm)))
    elems.append(Spacer(1, 4*mm))
    # Two column: left = billing info, right = invoice details
    left_col = [
        _p('<b>Invoice #:</b> %s' % data['invoice_number'], normal),
        _p('<b>Date:</b> %s' % data['date'], normal),
        _p('<b>Due Date:</b> %s' % data['due_date'], normal),
    ]
    right_col = [
        _p('<b>Terms:</b> %s' % data['terms'], right),
        _p('<b>Currency:</b> %s' % data['currency'], right),
        _p('<b>Vendor Reg:</b> %s' % data['vendor_reg'], right),
    ]
    info_t = Table([[left_col, right_col]], colWidths=[90*mm, 90*mm])
    info_t.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'TOP')]))
    elems.append(info_t)
    elems.append(Spacer(1, 4*mm))
    # Items as simple stacked paragraphs (no table grid)
    elems.append(_p('<b>Items:</b>', bold))
    elems.append(Spacer(1, 2*mm))
    for i, item in enumerate(data['line_items'], 1):
        line = '%d. %s — %d x %s %.2f = <b>%s %.2f</b>' % (
            i, item['description'], item['quantity'], data['currency'],
            item['unit_price'], data['currency'], item['total'])
        elems.append(_p(line, normal))
    elems.append(Spacer(1, 5*mm))
    # Totals
    total_lines = [
        _p('<b>Subtotal:</b>       %s %.2f' % (data['currency'], data['subtotal']), normal),
    ]
    if data['discount']:
        total_lines.append(_p('<b>Discount:</b>       -%s %.2f' % (data['currency'], data['discount']), normal))
    if data['shipping']:
        total_lines.append(_p('<b>Shipping:</b>      %s %.2f' % (data['currency'], data['shipping']), normal))
    if data['tax']:
        total_lines.append(_p('<b>%s:</b>  %s %.2f' % (data['tax_label'], data['currency'], data['tax']), normal))
    if data['tax2']:
        total_lines.append(_p('<b>%s:</b>  %s %.2f' % (data['tax2_label'], data['currency'], data['tax2']), normal))
    total_lines.append(_p('<b>TOTAL:</b>         %s %.2f' % (data['currency'], data['total']), bold))
    for tl in total_lines:
        elems.append(tl)
    doc.build(elems)
    return True


# ── Layout Template 6: Compact fuel receipt style ───────────────────
@layout_registry.register
def layout_fuel_receipt(data, pdf_path):
    doc = SimpleDocTemplate(pdf_path, pagesize=(80*mm, 200*mm),
                            leftMargin=5*mm, rightMargin=5*mm,
                            topMargin=5*mm, bottomMargin=5*mm)
    normal, bold, small, title, right, center = _make_styles('Courier', size=8)
    elems = []
    elems.append(_p('<b>%s</b>' % data['vendor_name'], center))
    elems.append(_p(data['vendor_address'][:40], center))
    elems.append(Spacer(1, 3*mm))
    elems.append(_p('INVOICE %s' % data['invoice_number'], center))
    elems.append(_p('Date: %s' % data['date'], center))
    elems.append(_p('-' * 45, center))
    elems.append(_p('Item                   Amount', normal))
    elems.append(_p('-' * 45, center))
    for item in data['line_items'][:8]:
        desc = item['description'][:20].ljust(20)
        elems.append(_p('%s %s %7.2f' % (desc, data['currency'], item['total']), normal))
    elems.append(_p('-' * 45, center))
    elems.append(_p('SUBTOTAL     %s %7.2f' % (data['currency'], data['subtotal']), normal))
    if data['discount']:
        elems.append(_p('DISCOUNT     -%s %7.2f' % (data['currency'], data['discount']), normal))
    if data['shipping']:
        elems.append(_p('SHIPPING     %s %7.2f' % (data['currency'], data['shipping']), normal))
    if data['tax']:
        elems.append(_p('%s %s %7.2f' % (data['tax_label'][:15].ljust(15), data['currency'], data['tax']), normal))
    if data['tax2']:
        elems.append(_p('%s %s %7.2f' % (data['tax2_label'][:15].ljust(15), data['currency'], data['tax2']), normal))
    elems.append(_p('TOTAL        %s %7.2f' % (data['currency'], data['total']), bold))
    doc.build(elems)
    return True


# ── Layout Template 7: Left-aligned modern ──────────────────────────
@layout_registry.register
def layout_modern_left(data, pdf_path):
    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=15*mm, bottomMargin=15*mm)
    normal, bold, small, title, right, center = _make_styles('Helvetica', size=9)
    elems = []
    accent = pick_random(['#FF6B35', '#004E89', '#16A085', '#8E44AD', '#C0392B'])
    elems.append(Paragraph('<font color="%s" size="18"><b>%s</b></font>' % (accent, data['vendor_name']), normal))
    elems.append(_p(data['vendor_address'], small))
    elems.append(Spacer(1, 3*mm))
    # Separator line
    elems.append(_p('<hr color="%s"/>' % accent, center))
    elems.append(Spacer(1, 3*mm))
    elems.append(_p('<b>INVOICE</b>  #%s    |    %s' % (data['invoice_number'], data['date']), normal))
    elems.append(Spacer(1, 6*mm))
    # Table
    th4 = [['Description', 'Qty', 'Unit Price', 'Total']]
    for item in data['line_items']:
        th4.append([
            item['description'], str(item['quantity']),
            '%s %.2f' % (data['currency'], item['unit_price']),
            '%s %.2f' % (data['currency'], item['total']),
        ])
    cw4 = [80*mm, 15*mm, 28*mm, 28*mm]
    tt4 = Table(th4, colWidths=cw4)
    tt4.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('LINEBELOW', (0,0), (-1,0), 2, colors.HexColor(accent)),
        ('LINEBELOW', (0,-1), (-1,-1), 0.5, colors.HexColor(accent)),
        ('ALIGN', (1,1), (-1,-1), 'RIGHT'),
        ('TOPPADDING', (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
    ]))
    elems.append(tt4)
    elems.append(Spacer(1, 4*mm))
    # Summary
    sd = [['Subtotal', '%s %.2f' % (data['currency'], data['subtotal'])]]
    if data['discount']:
        sd.append(['Discount', '-%s %.2f' % (data['currency'], data['discount'])])
    if data['shipping']:
        sd.append(['Shipping', '%s %.2f' % (data['currency'], data['shipping'])])
    if data['tax']:
        sd.append([data['tax_label'], '%s %.2f' % (data['currency'], data['tax'])])
    if data['tax2']:
        sd.append([data['tax2_label'], '%s %.2f' % (data['currency'], data['tax2'])])
    sd.append(['TOTAL', '%s %.2f' % (data['currency'], data['total'])])
    sd_t = Table(sd, colWidths=[60*mm, 40*mm])
    sd_style = [('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
                ('FONTSIZE', (0,0), (-1,-1), 10),
                ('ALIGN', (1,0), (1,-1), 'RIGHT'),
                ('LINEABOVE', (0,-1), (-1,-1), 2, colors.HexColor(accent)),
                ('TOPPADDING', (0,0), (-1,-1), 2)]
    sd_style.append(('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'))
    sd_t.setStyle(TableStyle(sd_style))
    elems.append(sd_t)
    doc.build(elems)
    return True


# ── Layout Template 8: European VAT invoice ─────────────────────────
@layout_registry.register
def layout_eu_vat(data, pdf_path):
    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    normal, bold, small, title, right, center = _make_styles('Helvetica', size=9)
    elems = []
    elems.append(_p('<b>%s</b>' % data['vendor_name'], ParagraphStyle('H', parent=normal, fontSize=14)))
    elems.append(_p(data['vendor_address'], small))
    elems.append(_p('VAT: %s' % data['vendor_reg'], small))
    elems.append(Spacer(1, 6*mm))
    # EU-standard header block
    eu_info = [
        [_p('<b>Seller:</b> %s<br/>%s<br/>VAT: %s' % (data['vendor_name'], data['vendor_address'], data['vendor_reg']), small),
         _p('<b>Invoice No:</b> %s<br/><b>Date:</b> %s<br/><b>Due:</b> %s' % (data['invoice_number'], data['date'], data['due_date']), right)],
    ]
    eit = Table(eu_info, colWidths=[90*mm, 90*mm])
    eit.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'TOP')]))
    elems.append(eit)
    elems.append(Spacer(1, 6*mm))
    # Line items
    th5 = [['Description', 'Qty', 'Net Price', 'VAT Rate', 'Net Total']]
    for item in data['line_items']:
        vat_rate = data['tax_rate'] if data['tax_rate'] > 0 else 0.0
        net = item['total']
        th5.append([item['description'], str(item['quantity']),
                    '%s %.2f' % (data['currency'], item['unit_price']),
                    '%.0f%%' % (vat_rate * 100),
                    '%s %.2f' % (data['currency'], net)])
    cw5 = [65*mm, 12*mm, 22*mm, 18*mm, 22*mm]
    tt5 = Table(th5, colWidths=cw5)
    tt5.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 7),
        ('GRID', (0,0), (-1,-1), 0.3, colors.grey),
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#E8E8E8')),
        ('ALIGN', (1,1), (-1,-1), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    elems.append(tt5)
    elems.append(Spacer(1, 5*mm))
    # VAT Summary
    rate_txt = '%.0f%%' % (data['tax_rate'] * 100) if data['tax_rate'] > 0 else 'Exempt'
    vat_lines = [
        ['Net Total:', '%s %.2f' % (data['currency'], data['subtotal'])],
        ['VAT Rate:', rate_txt],
    ]
    if data['tax']:
        vat_lines.append(['VAT Amount:', '%s %.2f' % (data['currency'], data['tax'])])
    vat_lines.append(['Total Due:', '%s %.2f' % (data['currency'], data['total'])])
    vt = Table(vat_lines, colWidths=[50*mm, 40*mm])
    vt.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('ALIGN', (1,0), (1,-1), 'RIGHT'),
        ('LINEABOVE', (0,-1), (-1,-1), 1.5, colors.black),
    ]))
    elems.append(vt)
    doc.build(elems)
    return True


# ── Main generation ────────────────────────────────────────────────

def render_pdf_to_png(pdf_path, dpi=150):
    """Convert first page of PDF to PNG using pypdfium2."""
    try:
        with pdfium.PdfDocument(pdf_path) as doc:
            if len(doc) == 0:
                return None
            page = doc[0]
            scale = dpi / 72
            bitmap = page.render(scale=scale)
            pil_img = bitmap.to_pil()
            return pil_img
    except Exception:
        return None


def generate():
    layouts = layout_registry.all()
    ground_truth = {}
    idx = 0

    for template_idx, layout_fn in enumerate(layouts):
        # Generate 2 variations per template, some get more if we need to reach 50
        var_count = 2
        # Last few layouts get extra variations to fill to 50
        if template_idx >= len(layouts) - 2:
            var_count = 2 + (50 - len(layouts) * 2) // 2

        for var in range(var_count):
            data = generate_invoice_data(idx, template=layout_fn.__name__)
            name = 'inv_%04d_%s' % (idx, data['invoice_number'].replace('#', 'N').replace('/', '_'))
            pdf_path = os.path.join(PDF_DIR, name + '.pdf')
            img_path = os.path.join(IMG_DIR, name + '.png')

            print('Generating %s...' % name)

            # Generate PDF
            try:
                layout_fn(data, pdf_path)
            except Exception as e:
                print('  ERROR generating PDF: %s' % e)
                continue

            # Convert PDF to PNG for image test set
            try:
                pil_img = render_pdf_to_png(pdf_path)
                if pil_img:
                    pil_img.save(img_path)
            except Exception as e:
                print('  ERROR rendering PNG: %s' % e)
                continue

            # Record ground truth
            gt = {
                'file_pdf': name + '.pdf',
                'file_image': name + '.png',
                'vendor_name': data['vendor_name'],
                'invoice_number': data['invoice_number'],
                'currency': data['currency'],
                'subtotal': data['subtotal'],
                'discount': data['discount'],
                'shipping': data['shipping'],
                'tax': data['tax'],
                'tax_label': data['tax_label'],
                'tax2': data['tax2'],
                'tax2_label': data['tax2_label'],
                'total': data['total'],
                'tax_rate': data['tax_rate'],
                'template': layout_fn.__name__,
            }
            ground_truth[name] = gt
            idx += 1

    # Save ground truth
    gt_path = os.path.join(GT_DIR, 'ground_truth.json')
    with open(gt_path, 'w') as f:
        json.dump(ground_truth, f, indent=2)

    # Copy existing test_inv files
    print('\n--- Copying existing test_inv files ---')
    test_inv_dir = 'test_inv'
    if os.path.isdir(test_inv_dir):
        for fn in os.listdir(test_inv_dir):
            if fn.lower().endswith('.pdf'):
                src = os.path.join(test_inv_dir, fn)
                dst = os.path.join(PDF_DIR, 'existing_' + fn)
                import shutil
                shutil.copy2(src, dst)
                print('  Copied %s' % fn)
            elif fn.lower().endswith(('.jpg', '.jpeg', '.png')):
                src = os.path.join(test_inv_dir, fn)
                dst = os.path.join(IMG_DIR, 'existing_' + fn)
                import shutil
                shutil.copy2(src, dst)
                print('  Copied %s' % fn)

    print('\n=== DONE ===')
    print('Generated %d synthetic invoices' % idx)
    print('PDFs: %s (%d files)' % (PDF_DIR, len(os.listdir(PDF_DIR))))
    print('Images: %s (%d files)' % (IMG_DIR, len(os.listdir(IMG_DIR))))
    print('Ground truth: %s' % gt_path)


if __name__ == '__main__':
    generate()
