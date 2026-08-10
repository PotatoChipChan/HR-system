"""app/invoice/routes.py – Invoice upload, list, approve/reject, and AI OCR extraction"""
import os
import re
import uuid
import json
import platform
from io import BytesIO
import requests
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, current_app, jsonify,
                   send_from_directory, abort)
from app.database import query, execute, log_audit, as_dict
from app.auth.routes import login_required, role_required
from app.notifications.routes import send_notification
from PIL import Image, ImageFilter, ImageEnhance
import pdfplumber


def get_exchange_rate(from_currency, to_currency="MYR"):
    """Fetch exchange rate from ExchangeRate-API (free tier)."""
    if from_currency == to_currency:
        return 1.0
    try:
        # Using ExchangeRate-API (free tier, no API key needed for basic use)
        url = f"https://api.exchangerate-api.com/v4/latest/{from_currency}"
        response = requests.get(url, timeout=5)  # Reduced timeout to 5 seconds
        response.raise_for_status()
        data = response.json()
        return data.get("rates", {}).get(to_currency, 1.0)
    except Exception as e:
        print(f"Error fetching exchange rate: {e}")
        return None

inv_bp = Blueprint('invoice', __name__, url_prefix='/invoices')

ALLOWED_EXT = {'jpg', 'jpeg', 'png', 'pdf'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT





# ── Tesseract helper ──────────────────────────────────────────────────────────
def _get_tesseract_path():
    """Return the Tesseract executable path on Windows, or None on other OS."""
    if platform.system() == 'Windows':
        common = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
        if os.path.exists(common):
            return common
    return None


# ── EasyOCR helper (lazy-loaded) ─────────────────────────────────────────────
_easyocr_reader = None

def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr
        _easyocr_reader = easyocr.Reader(['en'], gpu=False)
    return _easyocr_reader


def _is_thermal_receipt(img_path):
    """Detect thermal receipt: white/light background, black text, no colored elements.
    Works for both raw thermal scans AND phone photos of thermal receipts.
    Key insight: thermal receipts have high light_ratio and low color saturation.
    For phone photos, we analyze the center crop where the receipt content is."""
    try:
        import numpy as np
        img = Image.open(img_path)
        w, h = img.size

        # For phone photos, analyze center crop (where receipt content is)
        center_w = w // 3
        left = w // 2 - center_w // 2
        right = w // 2 + center_w // 2
        center_crop = img.crop((left, 0, right, h))

        # Convert to RGB for color analysis
        rgb_arr = np.array(center_crop.convert('RGB'))
        gray_arr = np.array(center_crop.convert('L'))

        # Check 1: Most pixels should be light (white/light background)
        light_ratio = np.mean(gray_arr > 180)
        if light_ratio < 0.25:  # Not enough white background
            return False

        # Check 2: Low color saturation (thermal prints are grayscale, not colorful)
        r, g, b = rgb_arr[:,:,0], rgb_arr[:,:,1], rgb_arr[:,:,2]
        max_rgb = np.maximum(np.maximum(r, g), b).astype(float)
        min_rgb = np.minimum(np.minimum(r, g), b).astype(float)
        saturation = np.where(max_rgb > 0, (max_rgb - min_rgb) / max_rgb, 0)
        avg_saturation = np.mean(saturation)

        if avg_saturation > 0.35:  # Too colorful = not thermal
            return False

        # Check 3: Moderate contrast (not too high, not too low)
        content_std = np.std(gray_arr)
        if content_std > 90:  # Too high contrast = has colored/dark elements
            return False

        return True
    except Exception:
        return False


def _preprocess_image(img_path):
    """
    Preprocess image for better Tesseract accuracy:
      1. Correct orientation (OSD)
      2. Convert to greyscale
      3. Background normalization / histogram equalization
      4. Upscale if small
      5. Sharpen + gentle contrast boost
    Returns a PIL Image.
    """
    import pytesseract
    
    # 1. Try to correct orientation first
    img = Image.open(img_path)
    
    # Handle EXIF orientation if present
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    # Handle visual orientation via Tesseract OSD
    try:
        tess_path = _get_tesseract_path()
        if tess_path:
            pytesseract.pytesseract.tesseract_cmd = tess_path
        
        # OSD needs a certain amount of text/features to work
        osd = pytesseract.image_to_osd(img)
        rotate_match = re.search(r'Rotate: (\d+)', osd)
        if rotate_match:
            angle = int(rotate_match.group(1))
            if angle != 0:
                # Pillow rotates CCW, OSD angle is CW
                img = img.rotate(-angle, expand=True)
    except Exception:
        pass

    img = img.convert('L')          # greyscale

    # Background normalization: stretch histogram to cover full range
    # This handles lighter backgrounds by redistributing intensity values
    import numpy as np
    arr = np.array(img, dtype=np.uint8)
    if arr.size > 0:
        # Percentile-based stretch (ignore outliers)
        lo = np.percentile(arr, 2)
        hi = np.percentile(arr, 98)
        if hi > lo:
            arr = np.clip((arr.astype(float) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
        # Apply CLAHE (Contrast Limited Adaptive Histogram Equalization)
        try:
            import cv2
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            arr = clahe.apply(arr)
        except ImportError:
            # Fallback: standard equalization
            from PIL import ImageOps
            arr = np.array(ImageOps.equalize(Image.fromarray(arr)), dtype=np.uint8)
        img = Image.fromarray(arr)

    # Upscale if the image is too small
    w, h = img.size
    if w < 1000:
        scale = 1000 / w
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # Sharpen (once is enough)
    img = img.filter(ImageFilter.SHARPEN)

    # Gentle contrast boost (1.3x instead of 2.0x to avoid losing detail on light backgrounds)
    img = ImageEnhance.Contrast(img).enhance(1.3)

    return img


def _preprocess_with_cv2(img_path):
    """
    Stronger preprocessing using OpenCV (adaptive thresholding).
    Returns a PIL Image ready for Tesseract.
    Falls back to PIL-only preprocessing if OpenCV not available.
    """
    try:
        import cv2
        import numpy as np

        img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return _preprocess_image(img_path)

        # Upscale if small
        h, w = img.shape
        if w < 1000:
            scale = 1000 / w
            img = cv2.resize(img, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_CUBIC)

        # Denoise
        img = cv2.fastNlMeansDenoising(img, h=10)

        # Histogram equalization to normalize lighting
        img = cv2.equalizeHist(img)

        # Adaptive thresholding handles lighter backgrounds much better
        # than a fixed binary threshold
        img = cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 31, 2)

        return Image.fromarray(img)

    except ImportError:
        # OpenCV not installed – fall back to PIL-only
        return _preprocess_image(img_path)


def _is_thermal_receipt(img_path):
    """Detect thermal receipt images (narrow, low contrast, tall aspect ratio).
    Thermal receipts from POS machines, petrol pumps, etc. have characteristic
    dimensions and contrast that distinguish them from scanned documents or photos.
    Returns True if the image looks like a thermal receipt.
    """
    try:
        import numpy as np
        img = Image.open(img_path)
        w, h = img.size

        # Thermal receipts are narrow (typically 80mm paper = ~300-600px at 200dpi)
        if w > 800:
            return False

        # Check contrast – thermal prints have low contrast (faded ink on grey/white)
        arr = np.array(img.convert('L'))
        if arr.size == 0:
            return False
        std_dev = float(np.std(arr))
        if std_dev > 60:  # high contrast = scanned doc or photo, not thermal
            return False

        # Thermal receipts are tall and narrow (aspect ratio typically > 2.5)
        aspect = h / w
        if aspect < 2.0:
            return False

        return True
    except Exception:
        return False


# ── OCR extraction helpers ────────────────────────────────────────────────────
# Words/phrases that are NOT vendor names (skip these header lines)
_NOISE_LINES = re.compile(
    r'^(tax\s*invoice|invoice|r[aeiou]c\w*|official\s*receipt|resit|bil|'
    r'statement|quote|quotation|delivery\s*order|purchase\s*order|'
    r'credit\s*note|debit\s*note|proforma|page\s*\d+|tel|fax|ssm|'
    r'gst|sst|no\.?|ref\.?|date|to\s*:?|from\s*:?|bill\s*to|ship\s*to|'
    r'customer\s*(?:name|address|to)|thanks\s+for\s+choosing|payment\s+is\s+due|'
    r'payment\s+method|transaction\s+details|payment\s+details|status\s+successful|'
    r'description\s*total|sub\s*total|transactions?|balance|total)$',
    re.IGNORECASE
)

# ID value after label (allow newline — PDFs often put value on the next line)
_ID_VAL = r'([A-Z0-9][A-Z0-9\-_/]{4,34})'
_ID_GAP = r'\s*[:#.\-=;]?\s*'

# Priority tiers: invoice → transaction → order → receipt → other (first match wins)
_ID_TIERS = [
    ('invoice', re.compile(
        rf'(?i)(?:inv(?:oice|0ice|oice)|nuo?ice|inv\.?|tax\s*invoice)\s*(?:no\.?|number|#){_ID_GAP}{_ID_VAL}'
    )),
    ('transaction', re.compile(
        rf'(?i)transaction\s*(?:no\.?|number|#|id)?{_ID_GAP}{_ID_VAL}'
    )),
    ('order', re.compile(
        rf'(?i)order\s*(?:sn|no\.?|number|#|id){_ID_GAP}{_ID_VAL}'
    )),
    ('receipt', re.compile(
        rf'(?i)r[aeiou]c\w*\s*(?:no\.?|number|#){_ID_GAP}{_ID_VAL}'
    )),
    ('document', re.compile(
        rf'(?i)(?:document|bill|ref(?:erence)?)\s*(?:no\.?|number|#)?{_ID_GAP}{_ID_VAL}'
    )),
]
# Standalone INV-3337 / MJ… — require hyphen or digit after prefix (avoids matching "Order")
_INV_STANDALONE_RE = re.compile(
    r'(?i)(?<![A-Z0-9])(INV|IV|DN|SI|MJ|RC|TX)[\-_][A-Z0-9]{2,30}(?![A-Z0-9\-])'
)

_ID_BLACKLIST = {
    'DATE', 'TAX', 'INV', 'NO', 'TIME', 'TOTAL', 'AMT', 'AMOUNT', 'DUE', 'PAID',
    'NUMBER', 'PAYMENT', 'ORDER', 'NAME', 'TABLE', 'QTY', 'DESC', 'DESCRIPTION',
}

# Date patterns (ordered from most-specific to least)
_DATE_PATTERNS = [
    # DD Month YYYY  /  DD-Month-YYYY  /  DD/Month/YYYY
    re.compile(
        r'(?<![a-z0-9])(\d{1,2})[/\-\s](Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|'
        r'Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|'
        r'Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[/\-\s,]*(\d{4})(?![a-z0-9])',
        re.IGNORECASE
    ),
    # Month DD, YYYY (e.g., January 25, 2016)
    re.compile(
        r'(?<![a-z0-9])(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|'
        r'Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|'
        r'Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})[,\s]+(\d{4})(?![a-z0-9])',
        re.IGNORECASE
    ),
    # YYYY-MM-DD  /  YYYY/MM/DD
    re.compile(r'(?<![a-z0-9])(\d{4})[-/](\d{2})[-/](\d{2})(?![a-z0-9])'),
    # DD-MM-YYYY  /  DD/MM/YYYY
    re.compile(r'(?<![a-z0-9])(\d{2})[-/](\d{2})[-/](\d{4})(?![a-z0-9])'),
    # DD.MM.YYYY  (dot separator – common on European invoices, e.g. 12.02.2004)
    re.compile(r'(?<![a-z0-9])(\d{1,2})\.(\d{1,2})\.(\d{4})(?![a-z0-9])'),
]

_MONTH_MAP = {
    'jan': '01', 'january': '01',
    'feb': '02', 'february': '02',
    'mar': '03', 'march': '03',
    'apr': '04', 'april': '04',
    'may': '05',
    'jun': '06', 'june': '06',
    'jul': '07', 'july': '07',
    'aug': '08', 'august': '08',
    'sep': '09', 'september': '09',
    'oct': '10', 'october': '10',
    'nov': '11', 'november': '11',
    'dec': '12', 'december': '12',
}

# Amount patterns – cover total, grand total, amount due, jumlah, etc.
# Negative lookbehind for 'sub' (subtotal) and 'tax'/'vat' (sales tax total / vat total).
# Negative lookahead prevents matching sub-lines like "total excluding tax".
_TOTAL_RE = re.compile(
    r'(?i)'
    r'(?<!tax\s)(?<!tax)(?<!vat\s)(?<!vat)'   # not preceded by tax/vat ("Sales tax total")
    r'(?<!sub)(?<!net\s)(?<!net)'            # not "subtotal", "net worth"
    r'(?<!gross\s)(?<!gross)'                # not "gross worth"
    r'(?:grand\s*(?:total|talal|ftal|tatal)|(?:total|talal|ftal|tatal)\s*paid|(?:s)?(?:total|talal|ftal|tatal)\s*amt|(?:total|talal|ftal|tatal)\s*(?:amount|payable|due)?|'
    r'amount\s*(?:due|payable)?|balance\s*(?:due)?|(?:total|talal|ftal|tatal)\s*due|'
    r'jumlah(?:\s*keseluruhan|\s*bayaran)?|amt\s*due|net\s*(?:total|payable)|'
    r'gross\s*worth)'
    r'(?!\s*(?:tax|discount|sst|gst|vat|service|charge|excluding|incl))'
    r'[\s.:RM$\-]*(?:[A-Z]{2,4}\s*)?'   # allow optional currency code: CHF, USD, EUR, etc.
    r'([0-9]{1,3}(?:,\d{3})*(?:\.\d{2})|\d+(?:\.\d{2})?)'
)
# "Total <CURRENCY>" pattern: handles invoices like "Total CHF 1056.70" where the
# currency code sits between the label and the amount.
_TOTAL_CURRENCY_RE = re.compile(
    r'(?i)\btotal\s+(?!net\s|gross\s)(?:chf|usd|eur|gbp|myr|sgd|aud|jpy|inr|cad|hkd|thb|idr)\s*'
    r'(?:\d+\s+)?'              # skip bare item-count numbers (e.g. "Total CHF 3 950.00")
    r'([0-9]{1,3}(?:,\d{3})*\.\d{2})'   # require decimal – avoids item counts
)
_SUBTOTAL_RE = re.compile(
    r'(?i)(?<!adjust\s)(?:sub\s*total|subtotal|merchandise\s*subtotal|amount\s*before\s*tax|'
    r'before\s*(?:gst|sst|tax)|net\s*worth|price\s*net)'
    r'[\s.:RM$\-]*(?:[A-Z]{2,4}\s*)?'
    r'([0-9]{1,3}(?:,\d{3})*(?:\.\d{2})|\d+(?:\.\d{2})?)'
)
# Service tax / SST / GST line amounts (OCR may glue label+rate, e.g. "SERVICE TAX66@6.00% 2.72")
# Also captures "Sales tax total" lines (the tax sub-total on European invoices).
# NOTE: use (?:\d+\s*(?:@|at)\s*)? so digits are only consumed when followed by @ or 'at';
#       previously (?:\d+)? greedily ate the leading digit of the amount (e.g. '8' in '81.70').
_TAX_RE = re.compile(
    r'(?i)(?:sales\s*tax(?:\s*total)?|gst|sst|service\s*tax|tax\s*(?:amount|total)?|vat)'
    r'(?:\s+on\s+[a-z\s]+)?'
    r'[\s.:]*'
    r'(?:\d+\s*(?:@|at)\s*)?'    # only eat a number when followed by @ or 'at'
    r'(?:\d+(?:\.\d+)?\s*%\s*)?'  # optional percentage rate
    r'[\s.:RM$CHF\-]*'
    r'([0-9]{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})'
    r'(?!\s*%)'
)
_SHIPPING_TAX_RE = re.compile(
    r'(?i)(?:shipping\s*fee\s*sst|service\s*tax\s*on\s*shipping\s*fee)'
    r'[\s:().%RM$\-]*([0-9]{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})(?!\s*%)'
)
_SERVICE_CHARGE_RE = re.compile(
    r'(?i)service\s*cha[rge]*\s*(?:\(\s*\d+\s*%?\s*\))?[\s.:RM$\-]*'
    r'([0-9]{1,3}(?:,\d{3})*(?:\.\d{2})|\d+(?:\.\d{2})?)'
)
_TAX_RATE_RE = re.compile(
    r'(?i)service\s*tax.*?(\d+(?:\.\d+)?)\s*%'
)
_SVC_CHARGE_RATE_RE = re.compile(
    r'(?i)service\s*cha[rge]*.*?\(\s*(\d+)\s*%\s*\)'
)
# Fee-like lines used by parking/toll receipts that do not have explicit subtotal labels.
_FEE_RE = re.compile(
    r'(?i)(?:parking\s*fee|total\s*parking\s*fee|fare\s*usage|toll\s*fee)'
    r'[\s:>.-]*RM?\s*([0-9]{1,3}(?:,\d{3})*(?:\.\d{2})|\d+(?:\.\d{2})?)'
)
# Top-of-screen wallet deductions like "-RM2.50"
_NEGATIVE_WALLET_RE = re.compile(
    r'(?im)^\s*-\s*RM\s*([0-9]{1,3}(?:,\d{3})*(?:\.\d{2})|\d+(?:\.\d{2})?)\s*$'
)

def _is_garbage_vendor(name):
    """Reject OCR-garbage vendor names (mostly digits, no vowels, too short)."""
    if not name or len(name) <= 3:
        return True
    alpha = sum(1 for c in name if c.isalpha())
    digit = sum(1 for c in name if c.isdigit())
    if alpha + digit > 0 and digit / (alpha + digit) > 0.5:
        return True
    vowels = sum(1 for c in name.lower() if c in 'aeiou')
    if vowels == 0 and len(name) > 5:
        return True
    # Short all-caps words are usually OCR garbage (e.g. "HMIC", "NUOICE")
    if len(name) <= 6 and name.isupper() and vowels <= 1:
        return True
    # Single word with mixed case and no spaces is likely OCR garbage (e.g. "JNUMnunC")
    if ' ' not in name and len(name) <= 10:
        consonants = sum(1 for c in name if c.lower() not in 'aeiou')
        if consonants > vowels * 2:
            return True
    # Starts with special characters like #, @, ~ (OCR artifacts)
    if name[0] in '#@~^`':
        return True
    # Very few alpha characters total (e.g. "Qm 0") — not a real company name
    if alpha <= 4:
        return True
    return False


def _extract_vendor(lines):
    """
    Try to find the vendor/company name.
    1. Look for Malaysian corporate suffixes (SDN BHD, etc.) - HIGH PRIORITY
    2. Fallback to noise-skipping logic.
    """
    # High-priority search for corporate identifiers
    # First pass: look for lines that already contain brand + SDN BHD (e.g. "CelcomDigi Telecommunications Sdn Bhd")
    for line in lines:
        clean = line.strip()
        if re.search(r'\b(SDN\.?\s*BHD\.?|BHD\.?|LIMITED|LLP|ENT\.?|ENTERPRISE|PVT\.?\s*LTD|PVT\.?\s*LIMITED)\b', clean, re.IGNORECASE):
            if re.search(r'\b(shopee|document\s+is\s+issued\s+by)\b', clean, re.IGNORECASE):
                continue
            # Strip trailing punctuation artifacts
            clean = re.sub(r'\s*[-:;,\s]{2,}\s*$', '', clean).strip()
            # If line has words before SDN BHD, it's likely the full vendor name
            before_sdn = re.split(r'\bSDN\b', clean, flags=re.IGNORECASE)[0].strip()
            if before_sdn and len(before_sdn.split()) >= 2:
                return clean
    # Second pass: SDN BHD with brand name from preceding lines
    for idx, line in enumerate(lines):
        clean = line.strip()
        if re.search(r'\b(SDN\.?\s*BHD\.?|BHD\.?|LIMITED|LLP|ENT\.?|ENTERPRISE|PVT\.?\s*LTD|PVT\.?\s*LIMITED)\b', clean, re.IGNORECASE):
            if re.search(r'\b(shopee|document\s+is\s+issued\s+by)\b', clean, re.IGNORECASE):
                continue
            # Prepend brand name from preceding lines (e.g. "CelcomDigi" before "Telecommunications Sdn Bhd")
            brand_parts = []
            for back in range(1, min(4, idx + 1)):
                prev = lines[idx - back].strip()
                if not prev or len(prev) > 30 or re.search(r'\d', prev) \
                        or _is_garbage_vendor(prev) \
                        or re.search(r'(?:RM|MYR|\$|receipt|invoice|date|time|total|amount)', prev, re.IGNORECASE):
                    break
                brand_parts.insert(0, prev)
            if brand_parts:
                return f'{" ".join(brand_parts)} {clean}'
            return clean

    for line_idx, line in enumerate(lines[:25]):
        clean = line.strip()
        # Strip common warning prefixes injected by PyPDF2 (e.g. "Sample - Do not pay!")
        clean = re.sub(r'(?i)^(?:sample|draft|copy|void|cancelled)\b[^A-Za-z]*(?:do\s+not\s+pay\b[^A-Za-z]*)?', '', clean).strip()
        if not clean or len(clean) <= 3:
            continue
        
        # Skip exact noise matches or lines starting with noise phrases
        if _NOISE_LINES.match(clean) or \
           re.match(r'^(thanks\s+for|payment\s+is\s+due|page\s+\d+|order\s+receipt|invoice\s*no|invoice\s*#|date\s*of\s*issue)', clean, re.IGNORECASE):
            continue

        # Skip if it's just numbers and spaces/symbols (like dates 19/04/2026)
        if re.match(r'^[\d\s\.,:\-\/]+$', clean):
            continue
        # Skip status bar artifacts and common OCR hallucinations
        if re.search(r'(^\d{1,2}:\d{2})|(\d+\s*%)|\b(5G|4G|LTE|VoLTE|Wi-Fi|BD\s*OO|OO|00|O0|0O)\b', clean, re.IGNORECASE):
            continue
        # Skip lines that look like currency amounts (e.g. -RM2.50)
        if re.search(r'(?:RM|MYR|\$)\s*\d', clean, re.IGNORECASE):
            continue
        # Skip common transaction header words that are not vendors
        if re.search(r'\b(details|transaction|status|method|r[aeiou]c\w*)\b', clean, re.IGNORECASE):
            continue
        # Skip common receipt key-value pairs
        if re.search(r'\b(date|time|card|ref|reference|entry|exit|fee|balance|wallet|tng|toll|paydirect|lal|seller|client)\b', clean, re.IGNORECASE):
            continue
        # Skip lines starting with "invoice" (e.g. "Invoice Number INV-3337") but allow "Bioplex INVOICE" style
        if re.match(r'(?i)^invoice\b', clean):
            continue
        # Boilerplate / PDF-generator lines
        if re.match(r'(?i)^(powered\s+by|generated\s+by|pdf\s+generated|created\s+with)', clean):
            continue
        # Skip email addresses and lines that are primarily email/URL
        if re.search(r'\b[\w.+-]+@[\w-]+\.[\w.]+\b', clean) and not re.search(r'\bSDN\.?\s*BHD|LIMITED|INC|LLC|CORP', clean, re.IGNORECASE):
            continue
        # Skip date-like lines (e.g. "07 Jullu 2028", "07/06/2026")
        if re.match(r'^\d{1,2}[\s/\-.]*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)', clean, re.IGNORECASE):
            continue
        # Skip fragmented lines ending with closing parentheses/brackets
        # BUT allow vendor names with business keywords in parentheses
        if clean.endswith(')') or clean.endswith(']'):
            if not re.search(r'(?i)\b(cuisine|restaurant|cafe|coffee|hotel|inn|resort|'
                             r'store|shop|mart|market|pharmacy|clinic|hospital|'
                             r'sdn\s*bhd|bhd|limited|llp|enterprise|enterprises|'
                             r'petron|shell|petronas|esso|bp|obil|'
                             r'supermarket|hypermarket|grocery|manjalara|kepong|'
                             r'bukit|sentul|setapak|wangsa|cheras|ampang|petaling|'
                             r'subang|shah|alam|puchong|cyber|jaya)\b', clean):
                continue
        # If the line looks like an address (contains digits + common address words) skip
        if re.search(r'\b(jalan|lorong|no\.|lot|km|taman|bandar|floor|level|suite|unit)\b',
                     clean, re.IGNORECASE):
            continue
        # Skip address-like lines (City, ST ZIP pattern or "NNN Street" etc.)
        if re.search(r'\b[A-Z]{2}\s+\d{5}\b', clean) or \
           re.match(r'(?i)^[A-Za-z\s]+,\s*[A-Z]{2}\b', clean):
            continue
        # Strip trailing numeric noise (e.g. "Restaurant 20180.7239046")
        clean = re.sub(r'\s*[\d.,]{5,}\s*$', '', clean).strip()
        # Strip trailing punctuation artifacts (e.g. "Sdn Bhd - :")
        clean = re.sub(r'\s*[-:;,\s]{2,}\s*$', '', clean).strip()
        if not clean or _is_garbage_vendor(clean):
            continue
        # If we reached here, assume it's the vendor - join with following lines if needed
        return _join_vendor_lines(lines, line_idx)
    return ''


def _join_vendor_lines(lines, start_idx):
    """Join vendor name across multiple lines if they form a coherent name.
    E.g., 'PETRON' + 'BUKIT KEPONG' + '(FI)' -> 'PETRON BUKIT KEPONG (FI)'
    Stops when combined name is long enough, after parenthetical suffix, or next line doesn't fit."""
    result = lines[start_idx].strip()
    for j in range(start_idx + 1, min(start_idx + 4, len(lines))):
        nxt = lines[j].strip()
        # Stop if combined name is already long enough (vendor names are usually < 40 chars)
        if len(result) > 30:
            break
        # Stop if previous line ended with closing paren - it's a complete name
        if result.endswith(')'):
            break
        # Stop if next line is empty, too long, or contains long numbers
        if not nxt or len(nxt) > 40:
            break
        if re.search(r'\d{4,}', nxt):
            break
        # Stop if it matches noise patterns
        if _NOISE_LINES.match(nxt):
            break
        # Stop if it looks like an address
        if re.search(r'(?:jalan|lorong|taman|bandar|km\s*\d|no\.?\s*\d|lot\s*\d)', nxt, re.IGNORECASE):
            break
        # Stop if it's a date or time
        if re.match(r'^\d{1,2}[\s/\-.]*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)', nxt, re.IGNORECASE):
            break
        if re.match(r'^\d{1,2}:\d{2}', nxt):
            break
        # Stop if next line looks like a standalone descriptor (single word, not part of name)
        # But allow parenthetical suffixes like "(FI)" or "(HQ)"
        if ' ' not in nxt and not re.match(r'^\(.*\)$', nxt) and len(result.split()) >= 2:
            break
        # Join if it looks like a continuation (short, no long numbers)
        if len(nxt) <= 30 and not re.search(r'\d{3,}', nxt):
            result += ' ' + nxt
        else:
            break
    return result


def _extract_date(text):
    """Try each date pattern and return YYYY-MM-DD or ''."""
    for p in _DATE_PATTERNS:
        m = p.search(text)
        if m:
            # Handle Month DD, YYYY or DD Month YYYY
            if len(m.groups()) == 3:
                g1, g2, g3 = m.groups()
                # If g1 is month name
                if g1.lower()[:3] in _MONTH_MAP:
                    month = _MONTH_MAP[g1.lower()[:3]]
                    day = g2.zfill(2)
                    year = g3
                # If g2 is month name
                elif g2.lower()[:3] in _MONTH_MAP:
                    day = g1.zfill(2)
                    month = _MONTH_MAP[g2.lower()[:3]]
                    year = g3
                # ISO or other numeric
                else:
                    if len(g1) == 4: # YYYY-MM-DD
                        return f"{g1}-{g2}-{g3}"
                    else:
                        d1, d2 = int(g1), int(g2)
                        if d2 > 12:    # MM/DD/YYYY
                            return f"{g3}-{g1.zfill(2)}-{g2.zfill(2)}"
                        else:          # Default DD/MM/YYYY
                            return f"{g3}-{g2.zfill(2)}-{g1.zfill(2)}"
                return f"{year}-{month}-{day}"
    return ''


# Additional patterns for math validation
_SHIPPING_RE = re.compile(r'(?i)(?:shipping|delivery|postage)(?:\s+(?:&|and)\s+handling)?\s*(?:fee|charge|cost)?[\s.:RM$\-]*([0-9]{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})')
_DISCOUNT_RE = re.compile(r'(?i)(?:voucher|discount|rebate|promo|adjustment|adj|deduction|minus|subsidy|subsidi)[\s.:RM$\-]*([0-9]{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})')

# Malaysian petrol subsidy patterns (e.g., "SUBDI95 -48.34")
_SUBSIDY_RE = re.compile(
    r'(?i)(?:SUBDI|SUBSIDI|DISCOUNT|DISC|RABAT|BAUCAR|VOUCHER)\s*\d*\s*'
    r'[\s:.-]*(-?\d+[\.,]\d{2})'
)

# Fuel receipt structure: "29.657L 3.620 107.36" (litres, price_per_litre, total_price)
_FUEL_LINE_RE = re.compile(
    r'(\d+\.?\d*)\s*L\s+(\d+\.?\d*)\s+(\d+[\.,]\d{2})'
)

# "Total RM:" pattern (Petron-style receipts)
_TOTAL_RM_COLON_RE = re.compile(
    r'(?i)total\s+RM\s*[:.]?\s*(\d+[\.,]\d{2})'
)


def _is_blacklisted_id(val):
    """Reject label words and junk OCR fragments mistaken for invoice numbers."""
    v = val.strip().upper()
    if not v or v in _ID_BLACKLIST:
        return True
    if v.startswith('-') or not re.match(r'^[A-Z0-9]', v, re.IGNORECASE):
        return True
    if re.fullmatch(r'DATE|TIME|PAID|TOTAL|AMOUNT|DUE|NAME|TABLE|PAYMENT', v):
        return True
    if re.match(r'^\d{1,2}\s+(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)', v):
        return True
    if re.match(r'^(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+\d{1,2}', v):
        return True
    return False


def _is_plausible_id(val):
    """Reject partial words (e.g. 'oices' from 'Invoices', 'Payment' from 'Invoice Payment')."""
    v = val.strip()
    if len(v) < 5:
        return False
    if re.match(r'^(INV|IV|DN|SI|MJ|RC|TX)[\-_]', v, re.IGNORECASE):
        return True
    if re.search(r'\d', v):
        return True
    return False


def _fix_id_ocr_digits(val):
    """Fix common OCR misreads in invoice/receipt IDs.

    Context-aware: applies different corrections based on ID type.
    - Receipt numbers (mostly digits): fix T/7 confusion
    - Invoice numbers (mixed alphanumeric): don't convert letters to digits
    """
    if not val:
        return val
    v = val
    # Known 2-letter abbreviations that legitimately contain 'O' — do NOT convert
    _keep_o_seqs = {'PO', 'CO', 'NO', 'TO', 'SO', 'DO', 'GO', 'HO', 'IO', 'BO', 'RO',
                    'OR', 'ON', 'OF', 'OK'}
    if re.search(r'\d', v):
        def _safe_o_repl(m):
            start = m.start()
            # Check if this 'o' is part of a known abbreviation
            preceding = v[max(0, start-1):start+1].upper()
            following = v[start:start+2].upper() if start + 1 < len(v) else ''
            if preceding in _keep_o_seqs or following in _keep_o_seqs:
                return m.group(0)
            return '0'
        v = re.sub(r'(?<=[A-Z])o(?=[^a-zA-Z])', _safe_o_repl, v, flags=re.IGNORECASE)
        v = re.sub(r'(?<=\d)o(?=[A-Z0-9])', '0', v, flags=re.IGNORECASE)
    # Fix 'l'/'I' -> '1' when between digits
    v = re.sub(r'(?<=\d)[lI](?=\d)', '1', v)

    # Context-aware: Only fix '7' -> 'T' in pure-numeric receipt IDs
    # Don't apply to mixed alphanumeric invoice numbers like MJ01CS00076125
    if len(v) >= 12 and re.search(r'\d', v):
        letter_count = sum(1 for c in v if c.isalpha())
        # Pure receipt numbers have mostly digits (<= 2 letters like "RM")
        if letter_count <= 2:
            v = re.sub(r'(?<=\d)7(?=\d{4,})', 'T', v, count=1)

    return v


def _extract_invoice_id(text, lines):
    """
    Pick document ID by priority: invoice → transaction → order → receipt → other.
    Returns (value, source_tier, needs_review).
    """
    for tier, pattern in _ID_TIERS:
        for m in pattern.finditer(text):
            val = m.group(1).strip()
            val = _fix_id_ocr_digits(val)
            if not _is_blacklisted_id(val) and _is_plausible_id(val):
                needs_review = tier not in ('invoice',)
                return val, tier, needs_review

    # Line-based: "Receipt Number:" on one line, value on the next
    _label_line = re.compile(
        r'(?i)^(?:inv(?:oice|0ice|oice)|nuo?ice|r[aeiou]c\w*|order|transaction)\s*(?:no\.?|number|#|sn|id)?\s*[.:]?\s*$'
    )
    _inline_label = re.compile(
        r'(?i)^(?:inv(?:oice|0ice|oice)|nuo?ice|r[aeiou]c\w*|order|transaction)\s*(?:no\.?|number|#|sn|id)?\s*[.;:\s]\s*(\S+)\b.*$'
    )
    tier_for_label = {
        'invoice': 'invoice', 'inv': 'invoice', 'inv0ice': 'invoice',
        'nuoice': 'invoice', 'nuoice': 'invoice',
        'receipt': 'receipt', 'order': 'order', 'transaction': 'transaction',
    }
    for i, line in enumerate(lines):
        m_inline = _inline_label.match(line)
        if m_inline:
            val = m_inline.group(1).strip()
            val = _fix_id_ocr_digits(val)
            if not _is_blacklisted_id(val) and _is_plausible_id(val):
                key = line.split()[0].lower()
                tier = tier_for_label.get(key, 'document')
                return val, tier, tier != 'invoice'
        if _label_line.match(line):
            for j in range(1, min(4, len(lines) - i)):
                val = lines[i + j].strip()
                val = _fix_id_ocr_digits(val)
                if not _is_blacklisted_id(val) and _is_plausible_id(val):
                    key = line.split()[0].lower()
                    tier = tier_for_label.get(key, 'document')
                    return val, tier, tier != 'invoice'

    for m in _INV_STANDALONE_RE.finditer(text):
        val = m.group(0).strip()
        if not _is_blacklisted_id(val):
            return val, 'standalone', True

    return '', '', False


def _pick_best_amount(candidates, prefer_keywords=None, skip_keywords=None, prefer_largest=False):
    """Pick the best labelled amount using context scoring."""
    if not candidates:
        return 0
    prefer_keywords = prefer_keywords or []
    skip_keywords = skip_keywords or []
    best = None
    best_score = -999
    for c in candidates:
        ctx = c.get('ctx', '')
        score = 0
        if any(k in ctx for k in skip_keywords):
            continue
        for i, kw in enumerate(prefer_keywords):
            if kw in ctx:
                score += 50 - (i * 5)
        if best is None or score > best_score or (
            score == best_score and prefer_largest and c['val'] > best['val']
        ):
            best, best_score = c, score
    if best is None:
        return candidates[-1]['val']
    return best['val']


def _plausible_tax_amount(amount, subtotal):
    """OCR often misreads cents (e.g. 2.72 -> 37); reject garbage tax values."""
    if subtotal <= 0:
        return amount > 0
    return 0 < amount <= subtotal * 0.20


def _round_money(value):
    """Normalize monetary values to two decimal places."""
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


# Malaysian F&B receipts often list SERVICE TAX and SERVICE CHARGE on separate lines.
# OCR frequently drops the decimal point (272 -> 2.72) or a leading digit (263 -> 3.63).
def _find_amount_after_label(text, match_start, max_chars=200):
    """Helper to find a decimal amount right after a match in the text."""
    # Look at the next max_chars characters after the match
    search_text = text[match_start:match_start + max_chars]
    m = re.search(r'(\d{1,3}(?:[.,]\d{2})|\d+\.\d{2})', search_text)
    if m:
        return m.group(1)
    return None


def _parse_percent_token(token):
    """Helper to parse percentage from misread tokens like '6,004' or '6 00%'."""
    # Clean the token: replace non-digit/non-decimal with nothing, try to get a number
    digits = re.sub(r'[^\d.,]', '', token)
    if not digits:
        return None
    # Replace comma with dot
    digits = digits.replace(',', '.')
    # Try to extract a number (e.g., 6004 → 6.00, 6 → 6)
    if '.' in digits:
        parts = digits.split('.')
        if len(parts) >= 2:
            # Take first part as integer part (e.g., 6.004 → 6)
            try:
                return float(parts[0])
            except ValueError:
                pass
    # If no decimal, check common rates (6,8,10)
    try:
        num = float(digits)
        if num in (6,8,10):
            return num
        # If num is like 600 → 6, 800 →8, 1000→10
        if num in (600,6000,800,8000,1000,10000):
            return num / 100
    except ValueError:
        pass
    return None


_MYS_SVC_TAX_LINE = re.compile(
    r'(?i)service\s*tax\b[^\n]*?(?:@|at| )\s*([^\n]*?)\s*%?\s*$',
    re.MULTILINE
)
_MYS_SVC_CHARGE_LINE = re.compile(
    r'(?i)(?:service|serv|servce|srvc)\s*(?:char|charge|chrg)[^\n]*?(?:\(\s*([^\n]+?)\s*%\s*\))?\s*$',
    re.MULTILINE
)
_MYS_FB_SPLIT_MARKERS = (
    re.compile(r'(?i)service\s*tax\b'),
    re.compile(r'(?i)(?:service|serv|servce|srvc)\s*(?:char|charge|chrg)'),
)


def _decode_ocr_charge_amount(token, subtotal, rate_pct=None):
    """Recover charge amounts when OCR omits '.' or leading digits."""
    token = token.replace(',', '.')
    if re.search(r'\.\d{2}$', token):
        return float(token)

    digits = re.sub(r'\D', '', token) or '0'
    n = int(digits)
    if n <= 0:
        return 0.0

    candidates = set()
    if n < 100:
        candidates.add(float(n))
    if n >= 10:
        candidates.add(round(n / 100, 2))
    if n >= 100:
        s = str(n)
        if len(s) == 3:
            # e.g. 272 -> 2.72, or 263 -> 3.63 when the leading digit was dropped
            for lead in range(1, 10):
                candidates.add(round(float(f'{lead}.{s[1:]}'), 2))

    if subtotal > 0 and rate_pct:
        expected = round(subtotal * rate_pct / 100, 2)
        best = min(candidates, key=lambda c: abs(c - expected))
        if abs(best - expected) <= 0.11:
            return expected

    if subtotal > 0:
        plausible = sorted(c for c in candidates if 0 < c <= subtotal * 0.20)
        if plausible:
            return plausible[-1]

    return float(n)


def _parse_malaysian_service_charge(text, lines, subtotal):
    """Parse SERVICE CHARGE amount from a Malaysian F&B receipt line."""
    best_svc_match = None
    best_svc_amt = None
    for m_svc in _MYS_SVC_CHARGE_LINE.finditer(text):
        # Check if this match has an amount on same or next line
        raw_amt = None
        line_text = text[m_svc.start():m_svc.end()]
        # Search for amount only AFTER the last % character OR last @ character in the line!
        last_percent = line_text.rfind('%')
        last_at = line_text.rfind('@')
        split_pos = max(last_percent, last_at)
        search_area = line_text[split_pos + 1:] if split_pos != -1 else line_text
        m_amt_same_line = re.search(r'(\d{1,3}(?:[.,]\d{2})|\d+\.\d{2})', search_area)
        if m_amt_same_line:
            raw_amt = m_amt_same_line.group(1)
            # Check if this raw_amt is reasonable:
            decoded = _decode_ocr_charge_amount(raw_amt, subtotal, 10.0)
            if decoded > subtotal * 0.2:
                # Too big, maybe look after the line instead!
                raw_amt = _find_amount_after_label(text, m_svc.end())
        else:
            # Look for amount right after the match in raw text
            raw_amt = _find_amount_after_label(text, m_svc.end())
        if raw_amt:
            best_svc_match = m_svc
            best_svc_amt = raw_amt
            break
    if not best_svc_match or not best_svc_amt:
        return None

    ocr_rate_token = best_svc_match.group(1) if best_svc_match.groups() else None
    # Find all numbers in the entire match, pick the one that's in (6,8,10)
    numbers_in_token = re.findall(r'\d+', text[best_svc_match.start():best_svc_match.end()])
    ocr_rate = None
    for num_str in numbers_in_token:
        try:
            num = int(num_str)
            if num in (6,8,10):
                ocr_rate = float(num)
                break
        except ValueError:
            pass

    for rate in (8, 10, 6, ocr_rate):
        if rate is None:
            continue
        expected = round(subtotal * rate / 100, 2) if subtotal > 0 else 0
        candidate = _decode_ocr_charge_amount(best_svc_amt, subtotal, rate)
        if subtotal > 0 and 0 < candidate <= subtotal * 0.20:
            if abs(candidate - expected) <= 0.11:
                return expected
    if ocr_rate is not None:
        return _decode_ocr_charge_amount(best_svc_amt, subtotal, ocr_rate)
    return _decode_ocr_charge_amount(best_svc_amt, subtotal)


def _find_line_index_in_filtered_lines(match, text, lines):
    """Find the index of the line containing the match in the filtered 'lines' list"""
    # First, get all lines (including empty) from original text
    all_lines = text.split('\n')
    # Find which all_lines index contains the match
    match_all_line_idx = text.count('\n', 0, match.start())
    # Now, find the corresponding index in 'lines'
    filtered_idx = 0
    for i in range(len(all_lines)):
        stripped = all_lines[i].strip()
        if stripped:
            if i == match_all_line_idx:
                return filtered_idx
            filtered_idx += 1
    return None

def _extract_malaysian_split_charges(text, lines, subtotal):
    """
    Parse separate SERVICE TAX + SERVICE CHARGE lines (Malaysian restaurant receipts).
    Returns (service_tax, service_charge) or None when this receipt pattern is absent.
    """
    if not all(marker.search(text) for marker in _MYS_FB_SPLIT_MARKERS):
        return None

    service_tax = service_charge = None

    # Find all tax matches and pick the best one that has an amount
    best_tax_match = None
    best_tax_amt = None
    for m_tax in _MYS_SVC_TAX_LINE.finditer(text):
        # Check if this match has an amount on same or next line
        raw_amt = None
        line_text = text[m_tax.start():m_tax.end()]
        # Search for amount only AFTER the last % character OR last @ character in the line!
        last_percent = line_text.rfind('%')
        last_at = line_text.rfind('@')
        split_pos = max(last_percent, last_at)
        search_area = line_text[split_pos + 1:] if split_pos != -1 else line_text
        m_amt_same_line = re.search(r'(\d{1,3}(?:[.,]\d{2})|\d+\.\d{2})', search_area)
        if m_amt_same_line:
            raw_amt = m_amt_same_line.group(1)
            # Check if this raw_amt is reasonable:
            decoded = _decode_ocr_charge_amount(raw_amt, subtotal, 6.0)
            if decoded > 4:
                # Too big, maybe look after the line instead!
                raw_amt = _find_amount_after_label(text, m_tax.end())
        else:
            # Look for amount right after the match in raw text
            raw_amt = _find_amount_after_label(text, m_tax.end())
        if raw_amt:
            # Now let's decode it and see if it makes sense:
            decoded = _decode_ocr_charge_amount(raw_amt, subtotal, 6.0)
            # If decoded is greater than 4 (unlikely for 6% tax on 45.40), skip this match!
            if decoded > 4:
                continue
            best_tax_match = m_tax
            best_tax_amt = raw_amt
            break  # We found a match with amount, take first reasonable one

    if best_tax_match and best_tax_amt:
        # Find all numbers in the entire match, pick the one that's in (6,8,10)
        numbers_in_token = re.findall(r'\d+', text[best_tax_match.start():best_tax_match.end()])
        rate = None
        for num_str in numbers_in_token:
            try:
                num = int(num_str)
                if num in (6,8,10):
                    rate = float(num)
                    break
            except ValueError:
                pass
        if rate is None:
            rate = 6.0  # Default to 6%
        service_tax = _decode_ocr_charge_amount(best_tax_amt, subtotal, rate)

    service_charge = _parse_malaysian_service_charge(text, lines, subtotal)

    if service_tax is None and service_charge is None:
        return None
    return service_tax or 0.0, service_charge or 0.0


def _extract_currency(text):
    """Extract currency from invoice text using common currency codes and symbols."""
    currency_map = {
        'RM': 'MYR',
        'MYR': 'MYR',
        '$': 'USD',
        'USD': 'USD',
        '€': 'EUR',
        'EUR': 'EUR',
        '£': 'GBP',
        'GBP': 'GBP',
        'S$': 'SGD',
        'SGD': 'SGD',
        'A$': 'AUD',
        'AUD': 'AUD',
        '¥': 'JPY',
        'JPY': 'JPY',
        '₹': 'INR',
        'INR': 'INR',
        'C$': 'CAD',
        'CAD': 'CAD',
        'HK$': 'HKD',
        'HKD': 'HKD',
        '฿': 'THB',
        'THB': 'THB',
        'Rp': 'IDR',
        'IDR': 'IDR',
        'CHF': 'CHF',
    }
    explicit_foreign = re.compile(
        r'(?i)\b(USD|EUR|GBP|SGD|AUD|JPY|INR|CAD|HKD|THB|IDR|CHF)\b'
    )
    malaysian_context = re.compile(
        r'(?i)\b(RM|MYR|service\s*tax|service\s*cha[rge]|sst|sdn\.?\s*bhd)\b'
    )

    total_currency_re = re.compile(
        r'(?i)(?:grand\s*total|total\s*paid|(?:s)?total\s*amt|total\s*(?:amount|payable|due)?|'
        r'amount\s*(?:due|payable)?|balance\s*(?:due)?|total\s*due|'
        r'jumlah(?:\s*keseluruhan|\s*bayaran)?|amt\s*due|net\s*(?:total|payable)?)'
        r'[\s.:\-]*'
        r'(RM|\$|€|£|S\$|A\$|¥|₹|C\$|HK\$|฿|Rp|CHF|USD|EUR|GBP|MYR|SGD|AUD|JPY|INR|CAD|HKD|THB|IDR)'
        r'[\s.:\-]*'
        r'\d'
    )
    m = total_currency_re.search(text)
    if m:
        symbol = m.group(1)
        if symbol == '$' and malaysian_context.search(text) and not explicit_foreign.search(text):
            return 'MYR'
        return currency_map.get(symbol.upper(), 'MYR')

    if malaysian_context.search(text) and not explicit_foreign.search(text):
        return 'MYR'

    for symbol, code in currency_map.items():
        if symbol == '$' and malaysian_context.search(text) and not explicit_foreign.search(text):
            continue
        if re.search(r'(?i)\b' + re.escape(symbol) + r'\b', text):
            return code
        if symbol == '$' and re.search(r'\$\s*\d', text):
            return 'USD'

    return 'MYR'


def _extract_all(text):
    """Run all extractors on OCR text and return a dict with confidence."""
    # Normalize EU numbers (space for thousands, comma for decimals) to US standard
    text = re.sub(r'\b(\d{1,3})\s(\d{3}),(\d{2})\b', r'\1\2.\3', text)
    text = re.sub(r'\b(\d{1,3})\.(\d{3}),(\d{2})\b', r'\1\2.\3', text)
    text = re.sub(r'(?<![.,\d])(\d+),(\d{2})(?!\d)', r'\1.\2', text)
    # Fix spaced decimals from OCR: "59  02" → "59.02", "107 .36" → "107.36"
    # Require non-digit before first group to avoid matching within longer numbers (e.g. "04867 41")
    text = re.sub(r'(?<!\d)(\d{1,2})\s+(\d{2})\b', lambda m: f'{m.group(1)}.{m.group(2)}'
                  if len(m.group(2)) == 2 and not re.search(r'[A-Za-z]', m.group(0)) else m.group(0), text)
    # Fix dash-as-decimal: "-48-34" → "-48.34"
    text = re.sub(r'-(\d+)-(\d{2})\b', r'-\1.\2', text)

    # Join short label-only lines with the next line (fixes EasyOCR splitting
    # labels and values onto separate lines, e.g. "Total\nRM 98.00")
    _ADDR_START_RE = re.compile(
        r'(?i)^\s*(?:suite|unit|floor|level|no\.?|lot|jalan|lorong|taman|'
        r'road|street|avenue|drive|lane|block|blk|building|tower|plaza|'
        r'km\s*\d|parsel|phase|blok)\b'
    )
    _raw_lines = text.split('\n')
    _joined_lines = []
    _i = 0
    while _i < len(_raw_lines):
        _cur = _raw_lines[_i].strip()
        if _cur and _i + 1 < len(_raw_lines):
            _nxt = _raw_lines[_i + 1].strip()
            # Join if current line is a short label (≤30 chars, no digits) and next has digits
            # BUT don't join if the next line looks like an address line (e.g. "Suite 5A-1204")
            if (_cur and len(_cur) <= 30 and not re.search(r'\d', _cur)
                    and _nxt and re.search(r'\d', _nxt)
                    and not _ADDR_START_RE.match(_nxt)):
                _joined_lines.append(f"{_cur} {_nxt}")
                _i += 2
                continue
        _joined_lines.append(_cur if _cur else '')
        _i += 1
    text = '\n'.join(_joined_lines)

    # Create a flattened version for regex matching (joins all lines with spaces)
    # This handles EasyOCR splitting labels and amounts across non-adjacent lines
    text_flat = ' '.join(l.strip() for l in text.split('\n') if l.strip())

    lines = [l.strip() for l in text.split('\n') if l.strip()]
    text_lower = text.lower()
    
    # Normalize spaces around hyphens/slashes in IDs (e.g. "BPXINV -00550" → "BPXINV-00550")
    text = re.sub(r'([A-Z0-9])\s+([-_/])\s*', r'\1\2', text)
    # Fix OCR date garbling: "M7" → "07", "21126" → "2026" in date-like contexts
    text = re.sub(r'(\d{2})/M(\d)/(\d{5})', lambda m: f'{m.group(1)}/0{m.group(2)}/20{m.group(3)[-2:]}', text)
    text = re.sub(r'(\d{2})/M(\d)/(\d{4})', lambda m: f'{m.group(1)}/0{m.group(2)}/{m.group(3)}', text)
    # Remove spaces between digits (OCR splits numbers: "9 04867 41" → "90486741")
    # Only match horizontal whitespace — don't merge across line boundaries
    text = re.sub(r'(?<=\d)[ \t]+(?=\d)', '', text)
    
    # Re-create lines after all normalizations
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    
    # 1. Identify Vendor (Scored)
    vendor = _extract_vendor(lines)
    pattern = None
    if vendor:
        pattern = query("SELECT * FROM Vendor_Pattern WHERE vendor_name=?", (vendor,), one=True)

    # 2. Candidate Collection for Scoring
    
    # --- Date Candidates ---
    date_candidates = []
    for p in _DATE_PATTERNS:
        for m in p.finditer(text):
            val = _extract_date(m.group(0))
            if val:
                score = 50
                ctx = text[max(0, m.start()-30):m.start()].lower()
                if "date" in ctx: score += 30
                if "paid" in ctx: score += 10
                date_candidates.append({'val': val, 'score': score})
    
    # --- Invoice / receipt ID (priority tiers) ---
    invoice_number, id_source, id_needs_review = _extract_invoice_id(text, lines)

    # --- Currency ---
    currency = _extract_currency(text)

    # --- Financial Candidates ---
    def get_amounts(regex, exclude_keywords=None, use_flat=False):
        results = []
        search_text = text_flat if use_flat else text
        for m in regex.finditer(search_text):
            ctx = search_text[max(0, m.start()-50):m.start()].lower()
            if exclude_keywords and any(k in ctx for k in exclude_keywords):
                continue
            try:
                # Handle possible negative sign in context or value
                val_str = m.group(1).replace(',', '').replace('$', '')
                val = float(val_str)
                # Check for minus sign right before the match
                if "-" in search_text[max(0, m.start()-2):m.start()]:
                    val = -val
                results.append({'val': val, 'start': m.start(), 'end': m.end(), 'full': m.group(0), 'ctx': ctx})
            except: continue
        return results

    totals_cands = get_amounts(_TOTAL_RE)
    # Also try flattened text for totals (handles EasyOCR splitting labels across lines)
    if not totals_cands:
        totals_cands = get_amounts(_TOTAL_RE, use_flat=True)
    # Also collect "Total CHF/USD/EUR" style totals (European invoices)
    totals_cands += get_amounts(_TOTAL_CURRENCY_RE)
    subtotals_cands = get_amounts(_SUBTOTAL_RE)
    taxes_cands = get_amounts(_TAX_RE, exclude_keywords=['inclusive', 'inclusive of'])
    shipping_tax_cands = get_amounts(_SHIPPING_TAX_RE)
    service_charge_cands = get_amounts(_SERVICE_CHARGE_RE)
    shipping_cands = get_amounts(_SHIPPING_RE)
    discounts_cands = get_amounts(_DISCOUNT_RE)
    fee_cands = get_amounts(_FEE_RE)

    # Prefer clearly labelled lines; skip discount / tax-summary false positives
    raw_subtotal = _pick_best_amount(
        subtotals_cands,
        prefer_keywords=['merchandise subtotal', 'sub total', 'subtotal'],
        skip_keywords=['discount', 'voucher', 'shipping discount', 'adjust', 'hrs/qty', 'rate/price'],
        prefer_largest=True,
    )
    raw_total = _pick_best_amount(
        totals_cands,
        prefer_keywords=['total paid', 'grand total', 'amount due', 'total due'],
        skip_keywords=['inclusive', 'exclusive', 'quantity', 'items', 'description', 'unit price', 'hrs/qty', 'rate/price'],
    )

    # Try extracting subtotal from "Total <CURRENCY> N 950.00" pattern FIRST.
    # European invoices write: "Total CHF  3  950.00"  (item count between label and amount).
    # This explicit label is more reliable than the summary row which can have EU decimal OCR errors.
    _total_curr_sub_re = re.compile(
        r'(?i)\btotal\s+(?:chf|usd|eur|gbp|myr|sgd|aud|jpy|inr|cad|hkd|thb|idr)\s+'
        r'\d+\s+'              # item count (e.g. "3" in "Total CHF 3 950.00")
        r'([0-9]{1,3}(?:,\d{3})*\.\d{2})'   # subtotal amount (must have decimal)
    )
    m_sub = _total_curr_sub_re.search(text)
    if m_sub:
        try:
            raw_subtotal = float(m_sub.group(1).replace(',', ''))
        except ValueError:
            pass

    # Cross-validate with summary table rows (e.g. "214  Livraisons  8.60  960,00  81.70  1056.70")
    # Numbers in these rows include: rate(%), subtotal-excl-tax, tax-amount, grand-total.
    # We parse ALL decimal numbers (handling both US 1,234.56 and EU 960,00 formats),
    # then filter out values that are clearly percentage rates (< 50) before indexing.
    # The row typically starts with a code (e.g. 214) or a percentage (e.g. 10%).
    _summary_row_re = re.compile(r'(?im)^\s*(?:\d{1,3}%?)\s+\S[^\n]*$')
    # Match US decimal (1,234.56) and EU decimal (960,00 / 1.234,56)
    _summary_num_re = re.compile(
        r'\b([0-9]{1,4}(?:[.,]\d{3})*[.,]\d{2})\b'
    )
    _confirmed_summary_total = 0.0   # save for math-validator guard
    _confirmed_summary_tax = 0.0     # save for calc_taxes
    summary_rows = []
    for sm in _summary_row_re.finditer(text):
        raw_nums = []
        for tok in _summary_num_re.findall(sm.group(0)):
            try:
                # Determine format: EU decimal has comma as last separator before 2 digits
                if '.' not in tok and re.search(r',\d{2}$', tok):
                    val = float(tok.replace(',', '.'))
                else:
                    val = float(tok.replace(',', ''))
                raw_nums.append(val)
            except (ValueError, OverflowError):
                continue
        if len(raw_nums) >= 3:
            summary_rows.append(raw_nums)
    
    # Also check for stacked summary lines (each value on its own line, like:
    # SUMMARY
    # VAT %
    # Net Worth
    # VAT
    # Gross Worth
    # 10%
    # 1,676,976.00
    # 167,697.60
    # 1,844,673.60
    summary_header_re = re.compile(r'(?i)^\s*(SUMMARY|TOTAL|SUMMARY\s*OF)\s*$')
    # Find summary header, then collect the next 10 lines for numeric values
    summary_nums = []
    for i, line in enumerate(lines):
        if summary_header_re.match(line.strip()):
            # Check next 10 lines for currency-style numbers
            for j in range(i+1, min(i+15, len(lines))):
                check_line = lines[j].strip()
                m = _summary_num_re.search(check_line)
                if m:
                    tok = m.group(1)
                    try:
                        if '.' not in tok and re.search(r',\d{2}$', tok):
                            val = float(tok.replace(',', '.'))
                        else:
                            val = float(tok.replace(',', ''))
                        if val > 0:
                            summary_nums.append(val)
                    except (ValueError, OverflowError):
                        continue
            if len(summary_nums) >= 2:
                summary_rows.append(summary_nums)
    
    if summary_rows:
        best_row = max(summary_rows, key=len)
        # Filter out rate-like values (< 50) to avoid confusing tax rates with prices
        price_nums = [n for n in best_row if n >= 50]
        if len(price_nums) >= 2:
            if len(price_nums) >= 3:
                summary_grand_total = price_nums[-1]
                summary_tax_val     = price_nums[-2]
                summary_subtotal    = price_nums[-3]
            elif len(price_nums) == 2:
                summary_grand_total = price_nums[-1]
                summary_subtotal    = price_nums[-2]
                summary_tax_val     = 0
            else:
                summary_grand_total = price_nums[-1]
                summary_subtotal    = 0
                summary_tax_val     = 0
            
            _confirmed_summary_total = summary_grand_total
            # Override raw_total when clearly wrong or disagreeing with the summary table
            if summary_subtotal > 0 and abs(raw_total - summary_grand_total) > 2.0:
                raw_total = summary_grand_total
            # Fill subtotal from summary only if label-based extraction found nothing
            if raw_subtotal == 0 and summary_subtotal >= summary_grand_total * 0.2:
                raw_subtotal = summary_subtotal
            # Save tax from summary to override OCR scattered values
            if summary_tax_val > 0:
                _confirmed_summary_tax = summary_tax_val

    # Fallback for parking/toll e-receipts with "Fee" but no subtotal/total labels.
    if raw_subtotal == 0 and fee_cands:
        raw_subtotal = _pick_best_amount(
            fee_cands,
            prefer_keywords=['parking fee', 'fare usage', 'toll fee'],
            skip_keywords=[],
            prefer_largest=True,
        )
    if raw_total == 0 and fee_cands:
        raw_total = _pick_best_amount(
            fee_cands,
            prefer_keywords=['total parking fee', 'parking fee', 'fare usage'],
            skip_keywords=[],
        )

    # Last-resort fallback for wallet deduction screenshots (e.g. "-RM2.50").
    if raw_total == 0:
        m_wallet = _NEGATIVE_WALLET_RE.search(text)
        if m_wallet:
            try:
                raw_total = float(m_wallet.group(1).replace(',', ''))
                if raw_subtotal == 0:
                    raw_subtotal = raw_total
            except ValueError:
                pass

    if _confirmed_summary_tax > 0:
        calc_taxes = _confirmed_summary_tax
    elif shipping_tax_cands:
        calc_taxes = sum(abs(t['val']) for t in shipping_tax_cands)
    else:
        calc_taxes = sum(
            t['val'] for t in taxes_cands
            if _plausible_tax_amount(t['val'], raw_subtotal)
        )
    calc_service = sum(s['val'] for s in service_charge_cands)
    calc_discounts = sum(abs(d['val']) for d in discounts_cands)
    calc_shipping = max([abs(s['val']) for s in shipping_cands] + [0])

    # Malaysian petrol subsidy patterns (e.g., "SUBDI95 -48.34")
    subsidy_matches = _SUBSIDY_RE.findall(text)
    if subsidy_matches:
        for s in subsidy_matches:
            try:
                val = float(s.replace(',', '.'))
                if val > 0:
                    calc_discounts += val
            except ValueError:
                pass

    # Fallback: look for negative amounts (lines starting with -) as discounts
    # This catches OCR-split subsidy lines like "-48-34" (should be -48.34)
    if calc_discounts == 0:
        neg_amounts = re.findall(r'(?m)^\s*-\s*(\d+[\.,\-]\d{2,})', text)
        for neg in neg_amounts:
            try:
                # Handle dash-as-decimal: "-48-34" -> "48.34"
                val_str = neg.replace('-', '.').replace(',', '.')
                val = float(val_str)
                if 0 < val < 100:  # reasonable discount amount
                    calc_discounts += val
            except ValueError:
                pass

    # Fuel receipt structure: set subtotal from fuel price if available
    fuel_matches = _FUEL_LINE_RE.findall(text)
    if fuel_matches and raw_subtotal == 0:
        for _, _, price_str in fuel_matches:
            try:
                price = float(price_str.replace(',', '.'))
                if price > raw_total:  # fuel price before subsidy > total
                    raw_subtotal = price
                    break
            except ValueError:
                pass

    # Malaysian restaurant receipts: SERVICE TAX + SERVICE CHARGE on separate lines.
    # Summary-table SST totals often exclude service charge, so this must override them.
    split_charges = _extract_malaysian_split_charges(text, lines, raw_subtotal)
    if split_charges is not None:
        calc_taxes, calc_service = split_charges
    elif all(marker.search(text) for marker in _MYS_FB_SPLIT_MARKERS):
        parsed_charge = _parse_malaysian_service_charge(text, lines, raw_subtotal)
        if parsed_charge and parsed_charge > 0:
            if calc_taxes <= 0 and _confirmed_summary_tax > 0:
                calc_taxes = _confirmed_summary_tax
            calc_service = parsed_charge

    # When OCR drops tax/charge amounts but keeps rates (common on Malaysian receipts)
    tax_rates = [float(m.group(1)) for m in _TAX_RATE_RE.finditer(text)]
    svc_rates = [float(m.group(1)) for m in _SVC_CHARGE_RATE_RE.finditer(text)]
    if raw_subtotal > 0:
        if calc_taxes == 0 and tax_rates:
            calc_taxes = round(raw_subtotal * (tax_rates[0] / 100), 2)
        if calc_service == 0 and svc_rates:
            calc_service = round(raw_subtotal * (svc_rates[0] / 100), 2)
    
    # Collect all numeric values for exhaustive matching
    num_matches = re.findall(r'(-?[0-9]{1,3}(?:,\d{3})*(?:\.\d{2})|-?\d+\.\d{2})', text)
    unique_amounts = sorted(list(set([abs(float(n.replace(',', ''))) for n in num_matches])), reverse=True)

    # 3. Mathematical Verification Engine
    best_math_match = False
    
    extra_charges = calc_taxes + calc_service + calc_shipping
    labelled_total = raw_total  # preserve "Total Paid" before math override

    has_total_paid = bool(re.search(r'(?i)total\s+paid', text))

    # Reject OCR garbage totals unless a trusted label (e.g. Shopee has vouchers so math won't balance)
    # Guard: never zero out a total that was confirmed by a summary table row.
    if raw_subtotal > 0 and raw_total > 0 and not has_total_paid:
        expected_from_parts = raw_subtotal + extra_charges - calc_discounts
        if raw_total < raw_subtotal * 0.5 or abs(raw_total - expected_from_parts) > 2.0:
            # Only zero if no confirmed summary total, or this total matches it
            if _confirmed_summary_total == 0 or abs(raw_total - _confirmed_summary_total) > 0.05:
                raw_total = 0

    # Math fallback only when no reliable total label (restaurant receipts, etc.)
    if not labelled_total or raw_total == 0:
        for t_cand in unique_amounts[:12]:
            for s_cand in unique_amounts:
                if abs(s_cand - t_cand) < 0.01 and calc_discounts == 0 and calc_shipping == 0 and extra_charges == 0:
                    continue
                if s_cand < 1 or t_cand < s_cand:
                    continue

                expected = s_cand + extra_charges - calc_discounts
                if abs(t_cand - expected) < 0.05 and t_cand > 0:
                    raw_total = t_cand
                    raw_subtotal = s_cand
                    best_math_match = True
                    break
            if best_math_match:
                break

        if raw_subtotal > 0 and raw_total == 0 and extra_charges > 0:
            raw_total = round(raw_subtotal + extra_charges - calc_discounts, 2)
            best_math_match = True

    # RM-amount fallback for receipts where label is garbled/missing
    # (e.g. EasyOCR reads "Talal" for "Total" but the RM amount is on a different line)
    if raw_total == 0:
        rm_amounts = re.findall(r'(?i)RM\s*[:.]?\s*(\d+(?:\.\d{2})?)', text)
        if not rm_amounts:
            rm_amounts = re.findall(r'(?i)RM\s*[:.]?\s*(\d+(?:\.\d{2})?)', text_flat)
        if rm_amounts:
            rm_vals = sorted(set(float(a) for a in rm_amounts), reverse=True)
            if rm_vals:
                raw_total = rm_vals[0]
                if raw_subtotal == 0:
                    raw_subtotal = raw_total

    # Subtotal fallback: if no explicit subtotal label but total > 0 and no tax/charges,
    # assume subtotal = total (e.g., CelcomDigi bill payment, simple receipts)
    if raw_subtotal == 0 and raw_total > 0:
        if calc_taxes == 0 and calc_service == 0 and calc_shipping == 0:
            raw_subtotal = raw_total
        elif calc_discounts > 0:
            # Receipt has discounts but no subtotal label - use the pre-discount amount
            # from unique_amounts if available (e.g., Petron fuel price 107.36)
            for amt in unique_amounts:
                if amt > raw_total and amt < raw_total * 3:  # reasonable pre-discount amount
                    raw_subtotal = amt
                    break

    invoice_date = sorted(date_candidates, key=lambda x: x['score'], reverse=True)[0]['val'] if date_candidates else ''

    # Deduce missing tax if the math perfectly matches a loose number extracted by OCR
    if raw_total > 0 and raw_subtotal > 0 and calc_taxes == 0:
        diff = round(raw_total - raw_subtotal - calc_service - calc_shipping + calc_discounts, 2)
        if diff > 0 and any(abs(cand - diff) < 0.01 for cand in unique_amounts):
            calc_taxes = diff

    # Final logic: Use raw extracted values
    final_total = _round_money(raw_total)
    final_subtotal = _round_money(raw_subtotal)
    final_tax = _round_money(calc_taxes + calc_service)

    # 5. Confidence Calculation with Penalties
    base_conf = 0.0
    if vendor: base_conf += 0.2
    if invoice_number: base_conf += 0.2
    if invoice_date: base_conf += 0.2
    if final_total > 0: base_conf += 0.2
    
    ai_note = ""
    if id_needs_review and invoice_number:
        tier_labels = {
            'transaction': 'transaction number',
            'order': 'order number',
            'receipt': 'receipt number',
            'document': 'reference number',
            'standalone': 'reference code',
        }
        label = tier_labels.get(id_source, id_source)
        ai_note = f"Using {label} ({invoice_number}). Please verify."
    elif not invoice_number:
        ai_note = "Invoice number not found. Please enter manually."
        id_needs_review = True
    elif final_total > 0 and final_subtotal > 0 and abs(
        final_total - final_subtotal - final_tax
    ) < 0.05:
        base_conf += 0.2
        ai_note = "Math verified."
    elif not best_math_match and final_total > 0 and final_subtotal > 0:
        base_conf -= 0.4
        ai_note = "Warning: Total does not tally with extracted Subtotal/Tax."
    elif best_math_match:
        base_conf += 0.2
        ai_note = "Math verified."

    # 6. Category Logic
    category = pattern['category_hint'] if pattern and pattern['category_hint'] else 'Other'
    if category == 'Other':
        if re.search(r'\b(toll|fare|transport|tng|grab|taxi|parking|petrol|shell|petronas)\b', text_lower): category = 'Transport'
        elif re.search(r'\b(meal|food|restaurant|cafe|dining|beverage|kfc|mcd|starbucks)\b', text_lower): category = 'Meals & Entertainment'
        elif re.search(r'\b(laptop|mouse|keyboard|monitor|software|hardware|it|hosting|cloud|domain|aws|azure)\b', text_lower): category = 'IT Equipment'
        elif re.search(r'\b(hotel|accommodation|room|stay|agoda|booking|airbnb)\b', text_lower): category = 'Accommodation'
        elif re.search(r'\b(stationery|paper|pen|office|stapler|printing)\b', text_lower): category = 'Office Supplies'
        elif re.search(r'\b(training|course|seminar|workshop|education|udemy|coursera)\b', text_lower): category = 'Training'

    return {
        'vendor_name':    vendor,
        'invoice_number': invoice_number,
        'invoice_date':   invoice_date,
        'due_date':       '',
        'currency':       currency or 'MYR',
        'subtotal':       final_subtotal,
        'tax_amount':     final_tax,
        'total_amount':   final_total,
        'category':       category,
        'confidence':     max(0.1, min(1.0, base_conf)),
        'ai_note':        ai_note,
        'id_needs_review': id_needs_review,
    }


@inv_bp.route('/learn_pattern', methods=['POST'])
@login_required
def learn_pattern():
    """
    Feedback loop: Saves user corrections to improve future extraction
    for this specific vendor.
    """
    f = request.json
    vendor = f.get('vendor_name')
    if not vendor: return jsonify({'status': 'ignored'})
    
    # Simple logic: If user submitted a value, check if we had an anchor nearby
    # This is a simplified version of pattern learning
    execute("""
        INSERT INTO Vendor_Pattern (vendor_name, category_hint, last_updated)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(vendor_name) DO UPDATE SET
        category_hint=excluded.category_hint,
        occurrence_count=occurrence_count+1,
        last_updated=datetime('now')
    """, (vendor, f.get('category')))
    
    return jsonify({'status': 'learned'})


# ── Routes ────────────────────────────────────────────────────────────────────

@inv_bp.route('/')
@login_required
def list_invoices():
    uid  = session['user_id']
    status_f = request.args.get('status', '')
    sql = ("SELECT i.*, e.full_name, e.position FROM Invoice i "
           "JOIN Employee e ON i.employee_id=e.employee_id "
           "WHERE i.employee_id=?")
    args = [uid]

    if status_f:
        sql += " AND i.status=?"
        args.append(status_f)
    sql += " ORDER BY i.submitted_at DESC"

    raw_invoices = query(sql, args)
    # Convert to dicts and add default values for missing columns
    invoices = []
    for row in raw_invoices:
        inv_dict = as_dict(row)
        inv_dict['currency'] = inv_dict.get('currency', 'MYR')
        inv_dict['total_amount_myr'] = inv_dict.get('total_amount_myr') or inv_dict.get('total_amount', 0)
        invoices.append(inv_dict)
    return render_template('invoice/list.html', invoices=invoices, status_f=status_f)


@inv_bp.route('/claims')
@role_required('Admin', 'HR', 'HR Manager', 'Manager')
def claims_management():
    co = session['company_id']
    role = session['user_role']
    status_f = request.args.get('status', '')
    sql = """SELECT i.*, e.full_name, e.position,
                    o.confidence_score, o.extracted_data, o.created_at as ocr_created_at
             FROM Invoice i
             JOIN Employee e ON i.employee_id=e.employee_id
             LEFT JOIN OCR_Result o ON o.invoice_id=i.invoice_id
             WHERE e.company_id=?"""
    args = [co]
    if role == 'Manager':
        sql += " AND e.branch_id=?"
        args.append(session['branch_id'])
    if status_f:
        sql += " AND i.status=?"
        args.append(status_f)
    sql += " ORDER BY i.submitted_at DESC"
    raw_invoices = query(sql, args)
    # Convert to dicts and add default values for missing columns
    invoices = []
    for row in raw_invoices:
        inv_dict = as_dict(row)
        inv_dict['currency'] = inv_dict.get('currency', 'MYR')
        inv_dict['total_amount_myr'] = inv_dict.get('total_amount_myr') or inv_dict.get('total_amount', 0)
        invoices.append(inv_dict)

    stats_sql = """
        SELECT
          SUM(CASE WHEN i.status='Pending'  THEN 1 ELSE 0 END) as pending,
          SUM(CASE WHEN i.status='Approved' THEN 1 ELSE 0 END) as approved,
          SUM(CASE WHEN i.status='Rejected' THEN 1 ELSE 0 END) as rejected,
          SUM(CASE WHEN i.status='Approved' THEN COALESCE(i.total_amount_myr, i.total_amount) ELSE 0 END) as total_approved
        FROM Invoice i
        JOIN Employee e ON i.employee_id=e.employee_id
        WHERE e.company_id=?
    """
    stats_args = [co]
    if role == 'Manager':
        stats_sql += " AND e.branch_id=?"
        stats_args.append(session['branch_id'])
    stats = query(stats_sql, stats_args, one=True)

    return render_template('invoice/claims.html',
                           invoices=invoices, stats=stats, status_f=status_f)


@inv_bp.route('/upload', methods=['POST'])
@login_required
def upload():
    uid = session['user_id']
    file = request.files.get('invoice_file')
    if not file or file.filename == '':
        flash('No file selected.', 'danger')
        return redirect(url_for('invoice.list_invoices'))
    if not allowed_file(file.filename):
        flash('Only JPG, PNG, PDF files are accepted.', 'danger')
        return redirect(url_for('invoice.list_invoices'))

    ext      = file.filename.rsplit('.', 1)[1].lower()
    ftype    = 'pdf' if ext == 'pdf' else 'image'
    saved_fn = f"{uuid.uuid4().hex}.{ext}"
    save_path = os.path.join(current_app.config['UPLOAD_FOLDER'], saved_fn)
    file.save(save_path)

    f = request.form
    currency = f.get('currency') or 'MYR'
    total_amount = _round_money(f.get('total_amount', 0))
    exchange_rate = None
    total_amount_myr = total_amount
    
    if currency != 'MYR':
        try:
            exchange_rate = get_exchange_rate(currency)
            if exchange_rate:
                total_amount_myr = round(total_amount * exchange_rate, 2)
        except Exception as e:
            print(f"Error converting currency: {e}")
            # Just proceed without conversion if something goes wrong
            exchange_rate = None
            total_amount_myr = total_amount
    
    iid = execute("""
        INSERT INTO Invoice
        (employee_id, filename, original_name, file_type,
         vendor_name, invoice_number, invoice_date, due_date,
         currency, exchange_rate, subtotal, tax_amount, total_amount, total_amount_myr,
         category, description, status)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (uid, saved_fn, file.filename, ftype,
          f.get('vendor_name', ''), f.get('invoice_number', ''),
          f.get('invoice_date', ''), f.get('due_date', ''),
          currency, exchange_rate,
          _round_money(f.get('subtotal', 0)),
          _round_money(f.get('tax_amount', 0)),
          total_amount, total_amount_myr,
          f.get('category', ''), f.get('description', ''), 'Pending'))

    # Persist OCR documentation linked to this exact invoice submission.
    ocr_raw_text = f.get('ocr_raw_text', '').strip()
    ocr_extracted_json = f.get('ocr_extracted_json', '').strip()
    ocr_conf = f.get('ocr_confidence', '').strip()
    if ocr_raw_text or ocr_extracted_json:
        try:
            conf_val = float(ocr_conf) if ocr_conf else 0.0
        except ValueError:
            conf_val = 0.0
        execute("""
            INSERT INTO OCR_Result (invoice_id, raw_text, extracted_data, ocr_engine, confidence_score)
            VALUES (?, ?, ?, 'Tesseract', ?)
            ON CONFLICT(invoice_id) DO UPDATE SET
                raw_text=excluded.raw_text,
                extracted_data=excluded.extracted_data,
                ocr_engine=excluded.ocr_engine,
                confidence_score=excluded.confidence_score,
                created_at=datetime('now')
        """, (iid, ocr_raw_text, ocr_extracted_json, conf_val))

    log_audit('UPLOAD', 'Invoice', f'Uploaded invoice {file.filename}',
              'Invoice', iid, 'Success', {'vendor': f.get('vendor_name', '')})
    flash('Invoice uploaded successfully! Pending HR approval.', 'success')
    return redirect(url_for('invoice.list_invoices'))


@inv_bp.route('/<int:iid>/approve', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager', 'Manager')
def approve(iid):
    uid = session['user_id']
    inv = query("""SELECT i.employee_id, i.invoice_number, i.total_amount, e.branch_id
                    FROM Invoice i
                    JOIN Employee e ON i.employee_id=e.employee_id
                    WHERE i.invoice_id=?""", (iid,), one=True)
    if session.get('user_role') == 'Manager' and inv and inv['branch_id'] != session.get('branch_id'):
        flash('Access denied: this claim belongs to another branch.', 'danger')
        return redirect(url_for('invoice.claims_management'))
    execute("""UPDATE Invoice SET status='Approved', approved_by=?, approved_at=datetime('now')
               WHERE invoice_id=?""", (uid, iid))

    # Apply claim to the next available Draft payroll immediately
    if inv and inv['total_amount']:
        try:
            from app.payroll.helpers import apply_claim_to_payroll
            apply_claim_to_payroll(inv['employee_id'], inv['total_amount'], iid,
                                   user_id=uid)
        except Exception as e:
            print(f"Failed to apply claim to payroll: {e}")

    log_audit('APPROVE', 'Invoice', f'Approved invoice id={iid}',
              'Invoice', iid, 'Success')
    if inv:
        send_notification(inv['employee_id'], 
                         'Invoice Approved', 
                         f"Your invoice {inv['invoice_number'] or '#' + str(iid)} has been approved.",
                         'Success',
                         url_for('invoice.list_invoices'))
    flash('Invoice approved and linked to payroll.', 'success')
    return redirect(url_for('invoice.claims_management'))


@inv_bp.route('/<int:iid>/reject', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager', 'Manager')
def reject(iid):
    uid    = session['user_id']
    reason = request.form.get('reason', '')
    inv = query("""SELECT employee_id, invoice_number, e.branch_id
                   FROM Invoice i JOIN Employee e ON i.employee_id=e.employee_id
                   WHERE i.invoice_id=?""", (iid,), one=True)
    if session.get('user_role') == 'Manager' and inv and inv['branch_id'] != session.get('branch_id'):
        flash('Access denied: this claim belongs to another branch.', 'danger')
        return redirect(url_for('invoice.claims_management'))
    execute("""UPDATE Invoice SET status='Rejected', approved_by=?,
               approved_at=datetime('now'), rejection_reason=?
               WHERE invoice_id=?""", (uid, reason, iid))
    log_audit('REJECT', 'Invoice', f'Rejected invoice id={iid}',
              'Invoice', iid, 'Success', {'reason': reason})
    if inv:
        send_notification(inv['employee_id'], 
                         'Invoice Rejected', 
                         f"Your invoice {inv['invoice_number'] or '#' + str(iid)} has been rejected: {reason if reason else 'No reason provided.'}",
                         'Warning',
                         url_for('invoice.list_invoices'))
    flash('Invoice rejected.', 'warning')
    return redirect(url_for('invoice.claims_management'))


@inv_bp.route('/<int:iid>/document')
@login_required
def view_document(iid):
    co = session['company_id']
    role = session['user_role']
    uid = session['user_id']
    if role in ('Admin', 'HR', 'HR Manager', 'HR Director'):
        inv = query("""SELECT i.filename
                       FROM Invoice i
                       JOIN Employee e ON i.employee_id=e.employee_id
                       WHERE i.invoice_id=? AND e.company_id=?""",
                    (iid, co), one=True)
    elif role == 'Manager':
        inv = query("""SELECT i.filename
                       FROM Invoice i
                       JOIN Employee e ON i.employee_id=e.employee_id
                       WHERE i.invoice_id=? AND e.company_id=? AND e.branch_id=?""",
                    (iid, co, session.get('branch_id')), one=True)
    else:
        inv = query("SELECT filename FROM Invoice WHERE invoice_id=? AND employee_id=?",
                    (iid, uid), one=True)
    if not inv:
        abort(404)
    return send_from_directory(current_app.config['UPLOAD_FOLDER'], inv['filename'])


@inv_bp.route('/<int:iid>/details')
@role_required('Admin', 'HR', 'HR Manager', 'Manager')
def claim_details(iid):
    """Full claim submission for manager/HR review (invoice fields + optional OCR)."""
    co = session['company_id']
    row = query("""
        SELECT i.*, e.full_name, e.position, e.email as employee_email, e.branch_id,
               approver.full_name as approved_by_name
        FROM Invoice i
        JOIN Employee e ON i.employee_id=e.employee_id
        LEFT JOIN Employee approver ON i.approved_by=approver.employee_id
        WHERE i.invoice_id=? AND e.company_id=?
    """, (iid, co), one=True)
    row = as_dict(row)
    if not row:
        return jsonify({'error': 'Claim not found.'}), 404
    if session.get('user_role') == 'Manager' and row.get('branch_id') != session.get('branch_id'):
        return jsonify({'error': 'Access denied: this claim belongs to another branch.'}), 403

    return jsonify({
        'invoice_id': row['invoice_id'],
        'employee_name': row.get('full_name', ''),
        'employee_position': row.get('position', ''),
        'employee_email': row.get('employee_email', ''),
        'vendor_name': row.get('vendor_name', ''),
        'invoice_number': row.get('invoice_number', ''),
        'invoice_date': row.get('invoice_date', ''),
        'due_date': row.get('due_date', ''),
        'currency': row.get('currency', 'MYR'),
        'exchange_rate': row.get('exchange_rate'),
        'subtotal': row.get('subtotal') or 0,
        'tax_amount': row.get('tax_amount') or 0,
        'total_amount': row.get('total_amount') or 0,
        'total_amount_myr': row.get('total_amount_myr') or (row.get('total_amount') or 0),
        'category': row.get('category', ''),
        'description': row.get('description', ''),
        'status': row.get('status', ''),
        'submitted_at': row.get('submitted_at', ''),
        'approved_at': row.get('approved_at', ''),
        'approved_by_name': row.get('approved_by_name', ''),
        'rejection_reason': row.get('rejection_reason', ''),
        'original_name': row.get('original_name', ''),
        'file_type': row.get('file_type', ''),
        'document_url': url_for('invoice.view_document', iid=iid),
    })


@inv_bp.route('/ocr_extract', methods=['POST'])
@login_required
def ocr_extract():
    """
    AI OCR endpoint – preprocesses the image, runs Tesseract, extracts
    structured fields (vendor, invoice number, date, amounts) with improved
    regex, and returns JSON.  Also persists results to OCR_Result table.
    """
    # ── Check Tesseract is available ──────────────────────────────────────
    try:
        import pytesseract
    except ImportError:
        return jsonify({'error': 'pytesseract is not installed. Run: pip install pytesseract'}), 503

    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path

    # Verify the binary works
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        return jsonify({
            'error': (
                'Tesseract OCR binary not found. '
                'Please install it from: '
                'https://github.com/UB-Mannheim/tesseract/wiki '
                'and ensure it is at C:\\Program Files\\Tesseract-OCR\\tesseract.exe'
            )
        }), 503

    # ── Validate uploaded file ────────────────────────────────────────────
    file = request.files.get('invoice_file')
    if not file or file.filename == '':
        return jsonify({'error': 'No file uploaded'}), 400

    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
    if ext not in {'jpg', 'jpeg', 'png', 'pdf'}:
        return jsonify({'error': 'OCR supports JPG, PNG, and PDF files.'}), 400

    # ── Save temp file ────────────────────────────────────────────────────
    temp_fn   = f"ocr_tmp_{uuid.uuid4().hex}.{ext}"
    temp_path = os.path.join(current_app.config['UPLOAD_FOLDER'], temp_fn)
    file.save(temp_path)

    try:
        raw_text = ""
        geo_result = None

        if ext == 'pdf':
            # ── PDF Extraction via pdfplumber (layout-aware, replaces PyPDF2) ──
            from app.invoice.geo_extract import geo_extract, geo_extract_text_only

            geo_result = None
            try:
                geo_result = geo_extract(temp_path)
                raw_text = geo_result['raw_text']
            except Exception:
                raw_text = ''

            # Fallback: if pdfplumber extracted no text (scanned PDF), try image extraction
            if not raw_text.strip():
                try:
                    with pdfplumber.open(temp_path) as pdf:
                        for i, page in enumerate(pdf.pages[:2]):
                            for img_obj in (page.images or []):
                                img_data = img_obj.get('stream', b'')
                                if img_data:
                                    img_pil = Image.open(BytesIO(img_data))
                                    img_tmp_path = temp_path + f"_p{i}_img.png"
                                    img_pil.save(img_tmp_path)
                                    processed_img = _preprocess_image(img_tmp_path)
                                    for psm in [4, 11, 12]:
                                        custom_config = f"--oem 3 --psm {psm}"
                                        try:
                                            raw_text += pytesseract.image_to_string(
                                                processed_img, config=custom_config) + "\n"
                                        except Exception:
                                            pass
                                    os.remove(img_tmp_path)
                except Exception:
                    pass
        else:
            # ── Image OCR with EasyOCR + Tesseract second opinion ──
            reader = _get_easyocr_reader()
            is_thermal = _is_thermal_receipt(temp_path)
            raw_text = ''

            if is_thermal:
                # Skip preprocessing for thermal receipts - CLAHE/sharpen destroys them
                try:
                    results = reader.readtext(temp_path)
                    raw_text = '\n'.join([text for _, text, _ in results]) + '\n'
                except Exception:
                    pass
                # Tesseract as second opinion
                tess_text = ''
                try:
                    import pytesseract
                    tess_path = _get_tesseract_path()
                    if tess_path:
                        pytesseract.pytesseract.tesseract_cmd = tess_path
                        tess_text = pytesseract.image_to_string(
                            Image.open(temp_path), config='--oem 3 --psm 6')
                except Exception:
                    pass
                raw_text = raw_text + '\n' + tess_text
            else:
                # Try preprocessed image first for better OCR on non-thermal images
                try:
                    processed = _preprocess_image(temp_path)
                    proc_path = temp_path + "_preprocessed.png"
                    processed.save(proc_path)
                    results = reader.readtext(proc_path)
                    raw_text = '\n'.join([text for _, text, _ in results]) + '\n'
                    os.remove(proc_path)
                except Exception:
                    pass
                # Single Tesseract call as second opinion (picks up chars EasyOCR misses)
                tess_text = ''
                try:
                    import pytesseract
                    tess_path = _get_tesseract_path()
                    if tess_path:
                        pytesseract.pytesseract.tesseract_cmd = tess_path
                        tess_text = pytesseract.image_to_string(
                            Image.open(temp_path), config='--oem 3 --psm 6')
                except Exception:
                    pass
                full_prep_text = raw_text + '\n' + tess_text

                # Quality check: if preprocessed OCR is mostly garbage, retry with raw OCR
                prep_ext = _extract_all(full_prep_text)
                vendor = prep_ext.get('vendor_name', '')
                needs_raw_fallback = (
                    (not vendor or _is_garbage_vendor(vendor))
                    and not prep_ext.get('invoice_number')
                    and not prep_ext.get('invoice_date')
                )
                if needs_raw_fallback:
                    try:
                        results = reader.readtext(temp_path)
                        raw_text = '\n'.join([text for _, text, _ in results]) + '\n'
                        if tess_text.strip():
                            raw_text = raw_text + '\n' + tess_text
                    except Exception:
                        raw_text = full_prep_text
                else:
                    raw_text = full_prep_text

        if not raw_text.strip():
            return jsonify({'error': 'Could not extract any text from this file.'}), 422

        # ── Extract structured data ──────────────────────────────────────
        extracted = _extract_all(raw_text)
        extracted['raw_text'] = raw_text

        # ── Merge geometric extraction results (PDFs only) ──
        # Vendor: always prefer geo's result — geo extracts single clean lines
        # while regex often picks up multi-line noise (e.g. "Bioplex INVOICE we love...")
        if geo_result and ext == 'pdf':
            if geo_result.get('vendor_name'):
                extracted['vendor_name'] = geo_result['vendor_name']
            # Invoice number: always prefer geo's result
            if geo_result.get('invoice_number'):
                extracted['invoice_number'] = geo_result['invoice_number']
            # Financial fields: only fill gaps (never override correct regex results)
            if extracted.get('total_amount', 0) == 0 and geo_result.get('total_amount', 0) > 0:
                extracted['total_amount'] = geo_result['total_amount']
                extracted['ai_note'] = 'Total filled via layout analysis.'
            if extracted.get('subtotal', 0) == 0 and geo_result.get('subtotal', 0) > 0:
                extracted['subtotal'] = geo_result['subtotal']
            if extracted.get('tax_amount', 0) == 0 and geo_result.get('tax_amount', 0) > 0:
                extracted['tax_amount'] = geo_result['tax_amount']

        log_audit('OCR_EXTRACT', 'Invoice', 'OCR extraction performed',
                  action_details={'vendor': extracted.get('vendor_name', ''), 'conf': extracted.get('confidence', 0)})

        return jsonify(extracted)

    except Exception as e:
        return jsonify({'error': f'OCR processing failed: {str(e)}'}), 500

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
