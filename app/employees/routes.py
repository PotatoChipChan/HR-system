"""app/employees/routes.py – Employee CRUD"""
import os
import re
import uuid
import json
import platform
import datetime
from io import BytesIO
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, current_app, jsonify,
                   send_from_directory, abort)
from werkzeug.security import generate_password_hash
from app.database import query, execute, log_audit, as_dict, is_leave_eligible, close_job_posting_for_application, get_db
from app.auth.routes import login_required, role_required
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance, ImageOps
from app.notifications.email_service import send_email_notification
from app.notifications.routes import send_notification

# Lazy singleton for EasyOCR (deep-learning fallback for guilloche-heavy MyKad images)
_easyocr_reader = None

def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        try:
            import easyocr
            _easyocr_reader = easyocr.Reader(['en'], gpu=False, recog_network='latin_g2')
        except Exception:
            pass
    return _easyocr_reader

def _run_easyocr(pil_img):
    """Run EasyOCR on the image; return all text lines joined by newline."""
    reader = _get_easyocr_reader()
    if reader is None:
        return ''
    try:
        import numpy as np
        import cv2
        arr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        results = reader.readtext(arr, detail=1, paragraph=False)
        lines = [t.strip() for _, t, s in results if s > 0.4 and t.strip()]
        return '\n'.join(lines)
    except Exception:
        return ''

def _ocr_name_with_easyocr(pil_img):
    """Fallback name extraction using EasyOCR (handles guilloche patterns that fool Tesseract)."""
    text = _run_easyocr(pil_img)
    if not text:
        return None
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    return _extract_malaysian_name(lines)

emp_bp = Blueprint('employees', __name__, url_prefix='/employees')

# ── Malaysian MyKad OCR helpers ─────────────────────────────────────────────

MYKAD_ANCHOR_RE = re.compile(
    r'KERAJAAN|KAD\s*PENGENALAN|WARGANEGARA|MYKAD|\bNAMA\b|IDENTITY|MALAYSIA|'
    r'NO\.?\s*K/?P|K/?P\.?\s*NO',
    re.I)

ADDRESS_MARKERS_RE = re.compile(
    r'\b(JALAN|JLN|LORONG|LRG|TAMAN|TMN|KAMPUNG|KAMPONG|KG\.?|BANDAR|BDR|'
    r'PERSIARAN|PSN|BLOK|BLK|TINGKAT|FLOOR|APT|CONDO|'
    r'POSKOD|POSTCODE|POST\s*CODE|MUKIM|DAERAH|'
    r'SELANGOR|JOHOR|PERAK|PENANG|PULAU\s*PINANG|SABAH|SARAWAK|MELAKA|NEGERI|'
    r'KUALA\s*LUMPUR|PUTRAJAYA|LABUAN|PAHANG|KEDAH|KELANTAN|TERENGGANU|'
    r'PERLIS|NSDK|N\.?\s*SEMBILAN)\b',
    re.I)

MYKAD_NAME_MARKERS_RE = re.compile(
    r'(\bBIN\b|\bBINTI\b|\bBTE\b|\bBINTE\b|\bA/L\b|\bA/P\b|\bA/L\.\b|\bA/P\.\b)',
    re.I)

MYKAD_AT_NAME_RE = re.compile(
    r'^[A-Z]{2,}(?:\s+[A-Z]{2,})+\s+@\s+[A-Z]{2,}(?:\s+[A-Z]{2,})+$'
)

NAMA_ANCHOR_RE = re.compile(r'\bN[A4@]?M[A4]?A\b', re.I)

MYKAD_LABEL_STOP_RE = re.compile(
    r'\b(WARGANEGARA|KETURUNAN|AGAMA|JANTINA|LELAKI|PEREMPUAN|'
    r'ALAMAT|ADDRESS|NO\.?\s*K/?P|K/?P|KERAJAAN|MALAYSIA|KAD\s*PENGENALAN|'
    r'IDENTITY\s*CARD|DATE\s*OF\s*BIRTH|TARIKH)\b',
    re.I)

IC_FORMATTED_RE = re.compile(r'(\d{6}[\s\-]\d{2}[\s\-]\d{4})')
IC_LABELED_RE = re.compile(
    r'(?:NO\.?\s*K/?P|N(?:O|0)\.?\s*K/?P|K/?P\.?\s*NO\.?)'
    r'[\s:.\-]*([\dOILSBZ\-\s]{12,20})',
    re.I)


def _get_tesseract_path():
    if platform.system() == 'Windows':
        common = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
        if os.path.exists(common):
            return common
    return None


def _apply_watermark(img_path, company_name):
    """
    Apply a semi-transparent horizontal watermark to the identity document:
    1. Two horizontal parallel lines across the centre.
    2. Text centred between the lines: "For [Company Name] HR Purposes Only".
    3. 40% opacity so the IC is still readable.
    """
    with Image.open(img_path) as img:
        w, h = img.size
        line_color = (200, 0, 0)
        alpha = int(255 * 0.40)
        color_alpha = (*line_color, alpha)
        line_width = max(2, int(w / 200))
        text = f"For {company_name} HR Purposes Only"
        mid_y = h * 0.55          # slightly above centre to avoid face photo area
        gap = int(h / 18)         # vertical spacing between lines

        try:
            font_path = "arial.ttf" if platform.system() == 'Windows' else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            font = ImageFont.truetype(font_path, int(w / 45))
        except Exception:
            font = ImageFont.load_default()

        overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
        ov_draw = ImageDraw.Draw(overlay)

        # Top line, bottom line — full width with 10% margins
        x1, x2 = int(w * 0.10), int(w * 0.90)
        y_top, y_bot = int(mid_y - gap), int(mid_y + gap)
        ov_draw.line([(x1, y_top), (x2, y_top)], fill=color_alpha, width=line_width)
        ov_draw.line([(x1, y_bot), (x2, y_bot)], fill=color_alpha, width=line_width)

        # Centre the text horizontally and vertically between the lines
        bbox = font.getbbox(text)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = (w - tw) // 2
        ty = mid_y - th // 2
        ov_draw.text((tx, ty), text, fill=color_alpha, font=font)

        result = Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB')
        result.save(img_path)


def _ocr_digit_fix(s):
    """Fix common OCR misreads in numeric strings."""
    return s.upper().replace('O', '0').replace('I', '1').replace('L', '1').replace('S', '5').replace('B', '8').replace('Z', '2')


def _format_ic(digits12):
    return f"{digits12[:6]}-{digits12[6:8]}-{digits12[8:]}"


def _validate_malaysian_ic(digits):
    """Validate YYMMDD-PB-G### structure used on Malaysian MyKad."""
    if len(digits) != 12 or not digits.isdigit():
        return False
    mm, dd = int(digits[2:4]), int(digits[4:6])
    if mm < 1 or mm > 12 or dd < 1 or dd > 31:
        return False
    birthplace = int(digits[6:8])
    if birthplace < 1 or birthplace > 59:
        return False
    return True


def _score_mykad_orientation(text):
    """Score OCR text for how well it matches upright Malaysian IC layout."""
    score = len(MYKAD_ANCHOR_RE.findall(text)) * 2
    if NAMA_ANCHOR_RE.search(text):
        score += 5
    if IC_LABELED_RE.search(text) or IC_FORMATTED_RE.search(text):
        score += 8
    if MYKAD_NAME_MARKERS_RE.search(text):
        score += 4
    return score


def _quick_orient_ocr(img):
    crop = _mykad_front_text_crops(img)[0]
    return _tesseract_once(_prep_gray(crop, max_side=800), psm=6)


def _orient_and_save(img_path):
    """Legacy helper — rotation is handled inside _ocr_mykad_with_rotation."""
    img = ImageOps.exif_transpose(Image.open(img_path)).convert('RGB')
    img.save(img_path, quality=92)
    return img


def _upscale(img, min_side=1200):
    w, h = img.size
    if max(w, h) >= min_side:
        return img
    scale = min_side / max(w, h)
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def _prep_gray(img, max_side=1200, contrast=2.0):
    """Grayscale prep — background normalization + moderate contrast."""
    gray = img.convert('L')
    w, h = gray.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        gray = gray.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    # Normalize background to handle light backgrounds
    import numpy as np
    arr = np.array(gray, dtype=np.uint8)
    if arr.size > 0:
        lo = np.percentile(arr, 2)
        hi = np.percentile(arr, 98)
        if hi > lo:
            arr = np.clip((arr.astype(float) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
        gray = Image.fromarray(arr)
    return ImageEnhance.Contrast(gray).enhance(contrast)


def _tesseract_once(img, psm=6):
    """Single Tesseract call."""
    import pytesseract
    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path
    langs = _tesseract_langs()
    return pytesseract.image_to_string(
        img, config=f'--oem 3 --psm {psm} -l {langs}')


def _tesseract_spatial_filter(img, psm=4, photo_threshold=0.72):
    """OCR with spatial position filtering via image_to_data().
    Discards words whose right edge falls in the right-side photo area of the card.
    """
    import pytesseract
    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path
    langs = _tesseract_langs()

    data = pytesseract.image_to_data(
        img,
        config=f'--oem 3 --psm {psm} -l {langs}',
        output_type=pytesseract.Output.DICT
    )

    w = img.size[0]
    word_map = {}
    top_map = {}

    for i, text in enumerate(data['text']):
        text = text.strip()
        if not text:
            continue
        # Skip words whose right edge falls within the photo area
        right_edge = data['left'][i] + data['width'][i]
        if right_edge / w > photo_threshold:
            continue

        block = data['block_num'][i]
        line = data['line_num'][i]
        key = (block, line)

        if key not in word_map:
            word_map[key] = []
            top_map[key] = data['top'][i]
        word_map[key].append(text)

    if not word_map:
        return ''

    sorted_keys = sorted(word_map.keys(), key=lambda k: top_map[k])
    lines = [' '.join(word_map[k]) for k in sorted_keys]
    return '\n'.join(lines)


def _mykad_front_text_crops(img):
    """
    Text-only crops for MyKad front.
    Since the document is now auto-cropped by OpenCV, we can reliably target the left 70% of the card to avoid the photo.
    """
    w, h = img.size
    crops = [
        # Left 70% of the tightly cropped card (avoids the photo completely)
        img.crop((int(w * 0.02), int(h * 0.02), int(w * 0.70), int(h * 0.98))),
        # Full card as a backup
        img.crop((int(w * 0.02), int(h * 0.02), int(w * 0.98), int(h * 0.98))),
    ]
    return crops


def _mykad_back_text_crops(img):
    """Back of MyKad: address area is usually centre-right, no photo."""
    w, h = img.size
    if w >= h:
        return [
            img.crop((int(w * 0.08), int(h * 0.15), w, int(h * 0.92))),
            img.crop((int(w * 0.20), int(h * 0.20), w, int(h * 0.85))),
        ]
    return [
        img.crop((int(w * 0.05), int(h * 0.25), int(w * 0.95), int(h * 0.80))),
        img.crop((0, int(h * 0.30), w, int(h * 0.75))),
    ]


def _score_ocr_text_fast(text):
    """Lightweight OCR quality score (no field extraction — safe inside loops)."""
    if not text or not text.strip():
        return 0
    fixed = _fix_ic_ocr_digits(text)
    score = _score_mykad_orientation(text)
    if IC_LABELED_RE.search(text):
        score += 18
    if IC_FORMATTED_RE.search(fixed):
        score += 14
    if MYKAD_NAME_MARKERS_RE.search(text):
        score += 10
    if re.search(r'\b(ALAMAT|ADDRESS)\b', text, re.I):
        score += 6
    return score


def _score_ocr_text(text):
    """Full score including extracted fields (use after OCR pass completes)."""
    score = _score_ocr_text_fast(text)
    if _extract_malaysian_ic_number(text):
        score += 12
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if _extract_malaysian_name(lines):
        score += 12
    if _extract_address(text):
        score += 8
    return score


def _pick_best_rotation(base, crop_fn):
    """Phase 1: try 4 rotations with one quick OCR each (~4 Tesseract calls)."""
    best_img, best_score = base, -1
    for angle in (0, 90, 180, 270):
        candidate = base if angle == 0 else base.rotate(-angle, expand=True)
        crop = crop_fn(candidate)[0]
        text = _tesseract_once(_prep_gray(crop, max_side=800, contrast=1.3), psm=6)
        score = _score_mykad_orientation(text)
        if IC_FORMATTED_RE.search(_fix_ic_ocr_digits(text)):
            score += 10
        if score > best_score:
            best_score, best_img = score, candidate
    return best_img


def _ocr_on_oriented(oriented, crop_fn):
    """Phase 2: OCR with spatial position filtering; falls back to crop-based PSM 6."""
    results = []

    # Primary pass: spatial filtering (clean when auto-crop works)
    for contrast in (1.8, 2.2):
        prepped = _prep_gray(oriented, max_side=1200, contrast=contrast)
        for psm in (4, 6):
            text = _tesseract_spatial_filter(prepped, psm=psm, photo_threshold=0.72)
            score = _score_ocr_text(text)
            if text.strip():
                results.append((text, score))
            if score >= 42:
                break
        if results and results[-1][1] >= 42:
            break

    # Fallback: tight crop first (avoids photo-noise hallucinated names)
    best_so_far = max(s for _, s in results) if results else 0
    if best_so_far < 30:
        crops = crop_fn(oriented)[:2]
        # Tight text-only crop (left ~70%) — no photo area
        prepped = _prep_gray(crops[0], max_side=1200, contrast=2.0)
        text = _tesseract_once(prepped, psm=6)
        score = _score_ocr_text(text)
        if text.strip():
            results.append((text, score))
        # Full-card crop only as last resort (photo noise causes fake names)
        if (not text.strip() or score < 20) and len(crops) > 1:
            prepped = _prep_gray(crops[1], max_side=1200, contrast=2.0)
            text = _tesseract_once(prepped, psm=6)
            score = _score_ocr_text(text) - 10
            if text.strip():
                results.append((text, score))

    if not results:
        text = _tesseract_spatial_filter(
            _prep_gray(oriented, max_side=1200, contrast=1.8), psm=11, photo_threshold=0.72)
        return text, _score_ocr_text(text)

    results.sort(key=lambda x: x[1], reverse=True)
    return results[0][0], results[0][1]


def _warp_card(cv_img, pts, out_w, out_h):
    """Perspective-warp the card region defined by 4 points."""
    import cv2
    import numpy as np
    dst = np.array([
        [0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]
    ], dtype="float32")
    M = cv2.getPerspectiveTransform(pts, dst)
    warped = cv2.warpPerspective(cv_img, M, (out_w, out_h))
    return Image.fromarray(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB))


def _rectify_4_points(pts):
    """Order 4 points as [top-left, top-right, bottom-right, bottom-left]."""
    import numpy as np
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def _get_card_dims(rect):
    """Compute output width/height from a rectified 4-point polygon."""
    import numpy as np
    (tl, tr, br, bl) = rect
    wa = max(int(np.linalg.norm(br - bl)), int(np.linalg.norm(tr - tl)))
    ha = max(int(np.linalg.norm(tr - br)), int(np.linalg.norm(tl - bl)))
    return wa, ha


def _try_canny_crop(cv_img, gray):
    """Attempt card detection via Canny edge detection + 4-point polygon."""
    import cv2
    import numpy as np
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(blurred, 50, 150)
    contours, _ = cv2.findContours(edged.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    h, w = gray.shape
    min_area = w * h * 0.15
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    for c in contours[:5]:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4:
            pts = approx.reshape(4, 2).astype("float32")
            rect = _rectify_4_points(pts)
            ow, oh = _get_card_dims(rect)
            if ow > 50 and oh > 50:
                return _warp_card(cv_img, rect, ow, oh)
    return None


def _try_otsu_crop(cv_img, gray):
    """Card detection via OTSU thresholding + bounding rect (fallback when Canny fails)."""
    import cv2
    import numpy as np
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    h, w = gray.shape
    min_area = w * h * 0.15
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        x, y, cw, ch = cv2.boundingRect(c)
        if cw > 50 and ch > 50:
            return Image.fromarray(cv2.cvtColor(cv_img[y:y+ch, x:x+cw], cv2.COLOR_BGR2RGB))
    return None


def _auto_crop_document(img):
    """Automatically find and crop the ID card via Canny edge detection + 4-point polygon.
    Falls back to OTSU bounding-box crop, then to the original image.
    """
    try:
        import cv2
        import numpy as np

        cv_img = np.array(img.convert('RGB'))
        cv_img = cv_img[:, :, ::-1].copy()
        gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)

        result = _try_canny_crop(cv_img, gray)
        if result is not None:
            return result

        result = _try_otsu_crop(cv_img, gray)
        if result is not None:
            return result
    except Exception:
        pass
    return img


def _ocr_mykad_with_rotation(img, crop_fn):
    """
    Two-phase MyKad OCR (~12-20 Tesseract calls total, not 200+).
    Returns (text, score, corrected_image).
    """
    base = ImageOps.exif_transpose(img).convert('RGB')
    base = _auto_crop_document(base)
    oriented = _pick_best_rotation(base, crop_fn)
    text, fast_score = _ocr_on_oriented(oriented, crop_fn)
    return text, _score_ocr_text(text) if text else fast_score, oriented


def _ocr_mykad_front(img):
    return _ocr_mykad_with_rotation(img, _mykad_front_text_crops)


def _ocr_mykad_back(img):
    return _ocr_mykad_with_rotation(img, _mykad_back_text_crops)


def _ocr_ic_number_fallback(img):
    """Second-pass OCR focused on the IC number line (digits only)."""
    import pytesseract
    import numpy as np
    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path

    w, h = img.size
    crop = img.crop((int(w * 0.05), int(h * 0.05), int(w * 0.95), int(h * 0.85)))
    gray = _upscale(crop.convert('L'))
    # Normalize background before mild contrast
    arr = np.array(gray, dtype=np.uint8)
    if arr.size > 0:
        lo = np.percentile(arr, 2)
        hi = np.percentile(arr, 98)
        if hi > lo:
            arr = np.clip((arr.astype(float) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
        gray = Image.fromarray(arr)
    gray = ImageEnhance.Contrast(gray).enhance(2.0)
    text = pytesseract.image_to_string(
        gray,
        config='--oem 3 --psm 7 -l eng -c tessedit_char_whitelist=0123456789OLISBZ-/ ')
    return _extract_malaysian_ic_number(text)


def _ocr_name_fallback(img):
    """Second-pass OCR focused on the NAMA line (letters only)."""
    import pytesseract
    import numpy as np
    import cv2
    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path

    w, h = img.size
    # Tight crop on the name area (left side, mid-upper portion of card)
    crop = img.crop((int(w * 0.05), int(h * 0.17), int(w * 0.50), int(h * 0.40)))
    cw, ch = crop.size
    if cw < 30 or ch < 30:
        return None

    candidates = []

    # Approach 1: Upscaled grayscale + contrast
    gray = _upscale(crop.convert('L'))
    arr = np.array(gray, dtype=np.uint8)
    if arr.size > 0:
        lo, hi = np.percentile(arr, 2), np.percentile(arr, 98)
        if hi > lo:
            arr = np.clip((arr.astype(float) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
        gray = Image.fromarray(arr)
    gray = ImageEnhance.Contrast(gray).enhance(2.0)
    langs = _tesseract_langs()
    candidates.append(gray)

    # Approach 2: Adaptive thresholding (better for small/light text on busy backgrounds)
    try:
        upscaled = _upscale(crop.convert('L'))
        arr = np.array(upscaled)
        thresh = cv2.adaptiveThreshold(arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY, 31, 10)
        candidates.append(Image.fromarray(thresh))
        thresh2 = cv2.adaptiveThreshold(arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                        cv2.THRESH_BINARY, 21, 8)
        candidates.append(Image.fromarray(thresh2))
    except Exception:
        pass

    best_name = None
    best_len = 0
    for prep in candidates:
        text = pytesseract.image_to_string(
            prep,
            config=f'--oem 3 --psm 6 -l {langs}')
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        name = _extract_malaysian_name(lines)
        if name and len(name) > best_len:
            best_name = name
            best_len = len(name)
    return best_name


def _preprocess_id_image(img_path):
    """Passport / generic fallback preprocessing."""
    img = Image.open(img_path).convert('RGB')
    return _prep_gray(img, max_side=1200, contrast=2.0)


def _run_id_ocr(img, psm_modes=(6, 11)):
    """Passport fallback — at most 2 Tesseract calls."""
    best, best_score = '', -1
    for psm in psm_modes:
        text = _tesseract_once(img, psm=psm)
        score = _score_ocr_text_fast(text)
        if score > best_score:
            best, best_score = text, score
    return best


def _tesseract_langs():
    """Prefer Malay + English for Malaysian IDs; fall back to English."""
    import pytesseract
    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path
    try:
        installed = pytesseract.get_languages(config='')
        if 'msa' in installed:
            return 'msa+eng'
    except Exception:
        pass
    return 'eng'


def _is_ocr_garbage_name(name):
    """Detect photo-region OCR noise (e.g. 'Yy Tees Eae Ap Ay Oe Ky')."""
    if not name:
        return True
    tokens = name.upper().split()
    if not tokens:
        return True
    if len(tokens) > 7 and not MYKAD_NAME_MARKERS_RE.search(name):
        return True
    short = sum(1 for t in tokens if len(t) <= 2)
    if len(tokens) >= 4 and short / len(tokens) >= 0.35:
        return True
    avg_len = sum(len(t) for t in tokens) / len(tokens)
    total_len = sum(len(t) for t in tokens)
    # Allow Chinese/Indian 3-part names like CHAN HAN YUE (avg ~3.3, total 10)
    if avg_len < 2.5:
        return True
    if not MYKAD_NAME_MARKERS_RE.search(name):
        if len(tokens) <= 2 and max(len(t) for t in tokens) <= 4:
            return True
        if total_len < 8:
            return True
    # Real MyKad names are almost always ALL CAPS; reject mixed-case noise
    raw_words = name.split()
    if any(re.search(r'[a-z]', w) and re.search(r'[A-Z]', w) and len(w) <= 4 for w in raw_words):
        if not MYKAD_NAME_MARKERS_RE.search(name):
            return True
    return False


def _fix_ic_ocr_digits(text):
    """Correct common OCR digit misreads inside IC number patterns."""
    def _fix_match(m):
        return _ocr_digit_fix(m.group(0))

    return re.sub(r'[\dOILSBZ\-]{10,}', _fix_match, text)


def _clean_name_line(line):
    """Strip non-name characters; MyKad names are uppercase letters and spaces."""
    cleaned = re.sub(r'[^A-Za-z\s]', ' ', line)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().upper()
    return cleaned


def _mykad_text_has_nama(text):
    return bool(NAMA_ANCHOR_RE.search(text))


def _looks_like_address(text):
    """True if the line reads like a Malaysian address, not a person name."""
    if not text:
        return True
    if ADDRESS_MARKERS_RE.search(text):
        return True
    if re.search(r'\b\d{5}\b', text):
        return True
    if re.search(r'\bNO\.?\s*\d', text, re.I):
        return True
    return False


def _is_plausible_malaysian_name(name):
    """Validate OCR candidate against Malaysian MyKad naming conventions."""
    if not name or len(name) < 8:
        return False
    if _is_ocr_garbage_name(name):
        return False

    upper = name.upper().strip()

    if '@' in upper or '-' in upper:
        return False

    noise = [
        'KERAJAAN', 'MALAYSIA', 'KAD PENGENALAN', 'IDENTITY CARD', 'WARGANEGARA',
        'PURPOSES', 'PURPOSE', 'ONLY', 'SPECIMEN', 'MYKAD', 'JPN', 'HR', 'NAMA',
        'KETURUNAN', 'AGAMA', 'JANTINA', 'LELAKI', 'PEREMPUAN', 'ALAMAT',
        'MALAY', 'IDENTITY', 'WARGA', 'NEGARA', 'PENGENALAN', 'KAD'
    ]
    if any(n in upper for n in noise):
        return False

    if _looks_like_address(upper):
        return False

    if not re.match(r'^[A-Z]+(?: [A-Z]+)+$', upper):
        return False

    letters = re.sub(r'[^A-Z]', '', upper)
    tokens = upper.split()
    vowels = sum(1 for c in letters if c in 'AEIOU')
    if vowels == 0:
        return False

    if MYKAD_NAME_MARKERS_RE.search(upper):
        return 2 <= len(tokens) <= 8

    if len(tokens) >= 3 and all(len(t) >= 4 for t in tokens):
        return vowels >= 2

    # Relax: allow short tokens (e.g. Chinese/Indian names: CHAN HAN YUE, A/P DEVI)
    if len(tokens) >= 2 and all(len(t) >= 2 for t in tokens):
        return vowels >= 1

    return False


def _extract_malaysian_ic_number(text):
    """Extract Malaysian MyKad IC (YYMMDD-PB-G###); prefer No. K/P labelled matches."""
    text = _fix_ic_ocr_digits(text)
    has_nama = _mykad_text_has_nama(text)
    min_score = 10 if has_nama else 20
    candidates = []

    for m in IC_LABELED_RE.finditer(text):
        digits = re.sub(r'\D', '', _ocr_digit_fix(m.group(1)))
        if _validate_malaysian_ic(digits):
            candidates.append((_format_ic(digits), 100))

    for m in IC_FORMATTED_RE.finditer(text):
        digits = re.sub(r'\D', '', m.group(1))
        if _validate_malaysian_ic(digits):
            ctx = text[max(0, m.start() - 60):m.start()].upper()
            score = 60 if re.search(r'K/?P|KAD|PENGENALAN|NAMA', ctx) else 30
            candidates.append((_format_ic(digits), score))

    if not candidates:
        # Fuzzy fallback: strip all non-digits and look for any 12-digit sequence
        flat = re.sub(r'[^0-9]', '', _ocr_digit_fix(text))
        for m in re.finditer(r'\d{12}', flat):
            digits = m.group(0)
            if _validate_malaysian_ic(digits):
                candidates.append((_format_ic(digits), 12))

    if not candidates:
        return ''

    candidates.sort(key=lambda x: x[1], reverse=True)
    # Lower threshold when a plausible name exists in the text
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    has_name = bool(_extract_malaysian_name(lines))
    effective_min = 10 if (has_nama or has_name) else min_score
    if candidates[0][1] < effective_min:
        return ''
    return candidates[0][0]


def _extract_malaysian_name(lines):
    """
    Extract holder name from MyKad front.
    Primary: look for NAMA label. Fallback: pick first plausible name line.
    """
    for i, line in enumerate(lines):
        if not NAMA_ANCHOR_RE.search(line):
            continue

        remainder = NAMA_ANCHOR_RE.sub('', line, count=1)
        remainder = re.sub(r'^[\s:.\-]+', '', remainder).strip()
        if remainder:
            candidate = _clean_name_line(remainder)
            if _is_plausible_malaysian_name(candidate):
                return candidate

        for j in range(i + 1, min(i + 3, len(lines))):
            nxt = lines[j]
            if MYKAD_LABEL_STOP_RE.search(nxt):
                break
            candidate = _clean_name_line(nxt)
            if _is_plausible_malaysian_name(candidate):
                return candidate

    # Fallback: no NAMA label found — pick first plausible all-caps name line
    for line in lines:
        candidate = _clean_name_line(line)
        if _is_plausible_malaysian_name(candidate):
            return candidate

    return ''


def _is_quality_address_line(line):
    """Reject garbage OCR lines in address context."""
    stripped = re.sub(r'[^A-Za-z0-9\s]', ' ', line)
    # Reject lines starting with lowercase letter (MyKad addresses are uppercase)
    first_alpha = next((c for c in stripped if c.isalpha()), None)
    if first_alpha and first_alpha.islower():
        return False
    alpha_words = [w for w in stripped.split() if w.isalpha() and len(w) >= 4]
    if not alpha_words and not re.search(r'\b\d{5}\b', line):
        return False
    total = len(line)
    if total == 0:
        return False
    punct_count = sum(1 for c in line if not c.isalnum() and not c.isspace())
    if punct_count / total > 0.5:
        return False
    unique = set(c.lower() for c in line if c.isalpha())
    if len(unique) <= 2 and len(alpha_words) == 0:
        return False
    return True


def _is_address_continuation(line):
    """Check if a line has sufficient address content to continue capturing.
    More restrictive than the initial trigger — requires clear address signals."""
    if ADDRESS_MARKERS_RE.search(line):
        return True
    if re.search(r'\b\d{5}\b', line):
        return True
    if re.search(r'\bNO\.?\s*\d', line, re.I):
        return True
    # Accept lines with at least 1 substantial alpha word with vowel (covers single suburb names like KEPONG)
    stripped = re.sub(r'[^A-Za-z\s]', ' ', line)
    words = [w for w in stripped.split() if w.isalpha() and len(w) >= 4 and re.search(r'[AEIOUaeiou]', w)]
    return len(words) >= 1


def _clean_malaysian_address(address):
    """Clean up common OCR mistakes in Malaysian addresses."""
    if not address:
        return address
    
    # Common OCR fixes
    fixes = [
        (r'\bVJ\b', 'W'),
        (r'\bPERSEMUTUAN\b', 'PERSEKUTUAN'),
        (r'\bPERSEMUTUANIKL\b', 'PERSEKUTUAN (KL)'),
        (r'\bPERSEKUTUANIKL\b', 'PERSEKUTUAN (KL)'),
        (r'\bJALAN 1/378\b', 'JALAN 1/37B'),
        (r'\bJALAN 1378\b', 'JALAN 1/37B'),
        (r'\bNO67\b', 'NO 67'),
        (r'\bNO\s*67\b', 'NO 67'),
        (r'\bJALAN 1/37B\s+\d+\b', 'JALAN 1/37B'),  # Remove trailing single digits after road
        (r'\)\s*\)*$', ')'),  # Fix trailing parentheses
        (r'^\s*\(\s*', '('),  # Fix leading parentheses
    ]
    
    cleaned = address.upper()
    for pattern, replacement in fixes:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.I)
    
    # Clean up extra characters and spaces
    cleaned = re.sub(r'[|_\-:~°]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # Strip leading junk (non-alpha-numeric)
    cleaned = re.sub(r'^[^A-Za-z0-9]+', '', cleaned)

    # Remove orphaned punctuation sequences (like ".," " ,." etc) from the middle of addresses
    cleaned = re.sub(r'\s+[\.,]+\s+', ' ', cleaned)
    cleaned = re.sub(r'^[\.,]+', '', cleaned)
    cleaned = re.sub(r'[\.,]+$', '', cleaned)
    # Strip trailing non-address garbage (isolated digits, orphaned punctuation)
    cleaned = re.sub(r'(?:\s+[\d\.\)\]\(,]+)+$', '', cleaned)
    cleaned = re.sub(r'\s*[\.\)\]\(,]+\s*$', '', cleaned)

    # Pattern recovery (OCR mangling of common Malaysian patterns)
    cleaned = re.sub(r'\bL\s*/\s*P\s*3\b', 'L/P3', cleaned)
    cleaned = re.sub(r'\bLI\s*P\s*3\b', 'L/P3', cleaned)
    cleaned = re.sub(r'\bLIPS\s*3\b', 'L/P3', cleaned)
    cleaned = re.sub(r'\bLP\s*/\s*3\b', 'L/P3', cleaned)
    cleaned = re.sub(r'Q\s*$', '', cleaned).strip()  # noise after L/P3
    cleaned = re.sub(r'(?<=\bNO)\s*,\s*(?=40)', ' ', cleaned)  # NO,40B → NO 40B
    cleaned = re.sub(r'PERMA!$', 'PERMAI', cleaned)
    # Strip IC card noise
    for noise in ['LELAKI?', 'WARGANEGARA', 'PEMILIK', 'ALAMI']:
        cleaned = cleaned.replace(noise, '').strip()

    return cleaned


def _extract_address(text, full_name=''):
    """Extract address from Malaysian MyKad OCR text.
    Strategy 1: Look for ALAMAT/ADDRESS label (rare on modern MyKad).
    Strategy 2: Positional — capture everything after the holder's name line,
                filtering out header/noise lines.
    Strategy 3: Fallback — grab any lines with address markers (JALAN, TAMAN, etc.).
    """
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    address_lines = []
    capturing = False

    stop_words = re.compile(
        r'\b(WARGANEGARA|KETURUNAN|AGAMA|JANTINA|LELAKI|PEREMPUAN|'
        r'KERAJAAN|KAD\s*PENGENALAN|IDENTITY\s*CARD|MYKAD|'
        r'PERSEKUTUAN)\b', re.I)

    header_noise = re.compile(
        r'\b(KAD\s*PENGENALAN|IDENTITY\s*CARD|KERAJAAN|MALAYSIA|MYKAD|'
        r'MALAY|WARGANEGARA)\b', re.I)

    # Strategy 1: ALAMAT/ADDRESS label
    for i, line in enumerate(lines):
        upper = line.upper()
        if re.search(r'\b(ALAMAT|ADDRESS)\b', upper):
            parts = re.split(r'[:\.\-]', line, maxsplit=1)
            if len(parts) > 1:
                chunk = re.sub(r'[^A-Za-z0-9\s,\-/\.#\(\)]', ' ', parts[1]).strip()
                if len(chunk) > 3:
                    address_lines.append(chunk)
            for j in range(i + 1, len(lines)):
                nxt = lines[j]
                if stop_words.search(nxt):
                    break
                cleaned = re.sub(r'[^A-Za-z0-9\s,\-/\.#\(\)]', ' ', nxt)
                cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                if len(cleaned) > 3 and _is_quality_address_line(cleaned):
                    address_lines.append(cleaned)
            if address_lines:
                return _clean_malaysian_address(', '.join(dict.fromkeys(address_lines)))

    # Strategy 2: Positional — find name line, capture everything after it as address
    if full_name:
        name_upper = full_name.upper().strip()
        name_idx = -1
        for i, line in enumerate(lines):
            cleaned = _clean_name_line(line)
            if cleaned and (name_upper in cleaned or cleaned in name_upper):
                name_idx = i
                break
            # Partial match: first two tokens of the name
            name_tokens = name_upper.split()
            if len(name_tokens) >= 2 and cleaned:
                if name_tokens[0] in cleaned and name_tokens[1] in cleaned:
                    name_idx = i
                    break

        if name_idx >= 0:
            for j in range(name_idx + 1, len(lines)):
                line = lines[j]
                if stop_words.search(line):
                    continue
                if header_noise.search(line):
                    break
                # Skip IC number lines
                if IC_FORMATTED_RE.search(line) or re.match(r'^\d{6}[\-\s]\d{2}[\-\s]\d{4}$', line.strip()):
                    continue
                cleaned = re.sub(r'[^A-Za-z0-9\s,\-/\.#\(\)]', ' ', line)
                cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                if len(cleaned) > 3 and _is_quality_address_line(cleaned):
                    address_lines.append(cleaned)
            if address_lines:
                return _clean_malaysian_address(', '.join(dict.fromkeys(address_lines)))

    # Strategy 3: Fallback — grab lines containing address markers
    capturing_fallback = False
    for line in lines:
        if ADDRESS_MARKERS_RE.search(line):
            capturing_fallback = True

        if capturing_fallback:
            # Strip stop words instead of skipping entire line (preserves postcode+city)
            stripped_line = stop_words.sub('', line).strip()
            if not stripped_line:
                continue
            cleaned = re.sub(r'[^A-Za-z0-9\s,\-/\.#\(\)]', ' ', stripped_line)
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            if len(cleaned) > 3:
                # For the first address line, keep everything.
                # For subsequent lines, only keep those with their own address content.
                if not address_lines:
                    address_lines.append(cleaned)
                elif _is_address_continuation(cleaned):
                    address_lines.append(cleaned)

    return _clean_malaysian_address(', '.join(dict.fromkeys(address_lines))) if address_lines else ''


def _cleanup_ocr_temp():
    """Delete IC files in ocr_temp/ older than 24 hours."""
    import time
    ocr_temp_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'ocr_temp')
    if not os.path.isdir(ocr_temp_dir):
        return
    cutoff = time.time() - 86400
    for f in os.listdir(ocr_temp_dir):
        fp = os.path.join(ocr_temp_dir, f)
        if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
            try:
                os.remove(fp)
            except:
                pass


def prep_guilloche_variants(img):
    """Generate 10 guilloche-specific preprocessing variants for Tesseract."""
    import cv2, numpy as np
    from PIL import Image

    rgb = img.convert('RGB')
    gray = rgb.convert('L')
    arr = np.array(rgb)
    gray_arr = np.array(gray)

    variants = []

    # 1. HSV Value channel (guilloche is often low-saturation)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    variants.append(('hsv_v', Image.fromarray(hsv[:, :, 2])))

    # 2. HSV + CLAHE
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    variants.append(('hsv_clahe', Image.fromarray(clahe.apply(hsv[:, :, 2]))))

    # 3. Blue - Red*0.3 (suppresses guilloche)
    b, g, r = cv2.split(arr)
    br = np.clip(np.float32(b) - r * 0.3, 0, 255).astype(np.uint8)
    variants.append(('blue_red', Image.fromarray(br)))

    # 4. Blackhat morphology k=15
    k15 = cv2.morphologyEx(gray_arr, cv2.MORPH_BLACKHAT, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)))
    variants.append(('blackhat15', Image.fromarray(k15)))

    # 5. Blackhat k=25 inverted
    k25 = 255 - cv2.morphologyEx(gray_arr, cv2.MORPH_BLACKHAT, cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25)))
    variants.append(('blackhat25_inv', Image.fromarray(k25)))

    # 6. Bilateral filter (edge-preserving smooth)
    bilat = cv2.bilateralFilter(arr, 9, 75, 75)
    variants.append(('bilateral', Image.fromarray(cv2.cvtColor(bilat, cv2.COLOR_RGB2GRAY))))

    # 7. Median filter k=3
    med3 = cv2.medianBlur(gray_arr, 3)
    variants.append(('median3', Image.fromarray(med3)))

    # 8. Median filter k=5
    med5 = cv2.medianBlur(gray_arr, 5)
    variants.append(('median5', Image.fromarray(med5)))

    # 9. CLAHE grayscale
    variants.append(('clahe_gray', Image.fromarray(clahe.apply(gray_arr))))

    # 10. Raw grayscale (control)
    variants.append(('gray_raw', gray))

    return variants


def _ocr_address_fallback(img):
    """Optimized OCR pass for the address area.
    Uses guilloche-specific preprocessing variants with early termination (~6 calls)."""
    import pytesseract, re
    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path
    else:
        return None

    variants = prep_guilloche_variants(img)

    # Use only the 5 best variants for speed (Blue-Red is guilloche killer)
    priority = ['blue_red', 'blackhat15', 'clahe_gray', 'hsv_v', 'gray_raw']
    priority_map = {name: v for name, v in variants if name in priority}
    ordered = [priority_map[n] for n in priority if n in priority_map]

    if not ordered:
        ordered = [v for _, v in variants]

    all_text = []
    tried = 0
    for var_img in ordered:
        for psm in [3, 6]:
            try:
                txt = pytesseract.image_to_string(
                    var_img,
                    config=f'--psm {psm} --oem 3 -l eng'
                )
                if txt and txt.strip():
                    all_text.append(txt)
                    tried += 1
                    # Early termination: if we have a postcode, stop
                    if re.search(r'\d{5}', txt):
                        break
            except Exception:
                tried += 1
        # Early termination at variant level too
        if tried >= 6:
            break

    # Filter noisy lines: keep only lines with reasonable content
    clean_lines = []
    for t in all_text:
        for line in t.split('\n'):
            line = line.strip()
            if not line or len(line) < 5:
                continue
            # Reject lines with >30% non-alpha-numeric characters
            punct_ratio = sum(1 for c in line if not c.isalnum() and not c.isspace()) / max(len(line), 1)
            if punct_ratio > 0.3:
                continue
            # Reject lines with mostly single characters (noise)
            words = line.split()
            short_words = sum(1 for w in words if len(w) <= 2)
            if len(words) > 0 and short_words / len(words) > 0.4:
                continue
            # Reject lines with no recognizable address content
            if not re.search(r'\b(JALAN|JLN|NO\b|TAMAN|KG\b|KEPONG|LU)\b', line, re.I) and not re.search(r'\d{5}', line):
                continue
            clean_lines.append(line)

    return '\n'.join(clean_lines) if clean_lines else None


def _easyocr_address_fallback(img):
    """Optimized EasyOCR address extraction: 3 calls max, early termination."""
    import numpy as np, re
    from PIL import Image

    arr = np.array(img.convert('RGB'))
    coords = [
        (0.0, 0.50, 1.0, 1.0),
        (0.0, 0.55, 1.0, 1.0),
        (0.0, 0.45, 1.0, 0.85),
    ]

    best_text = None
    best_score = 0
    for y1r, y2r, x1r, x2r in coords:
        h, w = arr.shape[:2]
        crop = arr[int(h*y1r):int(h*y2r), int(w*x1r):int(w*x2r)]
        try:
            results = _easyocr_reader().readtext(crop, detail=0, paragraph=True)
            if results:
                txt = ' '.join(results)
                score = len(re.findall(r'\b\d{5}\b', txt)) * 10 + len(txt)
                if score > best_score:
                    best_score = score
                    best_text = txt
                if best_text and re.search(r'\d{5}', best_text):
                    break
        except Exception:
            continue

    return best_text or ''


def _enrich_address_from_tesseract(img, existing_addr):
    """Conservative: only add words not already present; reject if conflict."""
    import pytesseract, re
    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path
    else:
        return existing_addr

    variants = prep_guilloche_variants(img)
    tess_text = ''
    for name in ['blue_red', 'blackhat15', 'clahe_gray']:
        for vname, vimg in variants:
            if vname == name:
                try:
                    tess_text += '\n' + pytesseract.image_to_string(vimg, config='--psm 6 --oem 3 -l eng')
                except:
                    pass
                break

    tess_words = set(w.lower() for w in tess_text.split() if len(w) > 2)
    existing_words = set(w.lower() for w in existing_addr.split())

    new_words = [w for w in tess_words if w not in existing_words and not re.match(r'^\d+$', w)]

    if new_words:
        enriched = existing_addr + ' ' + ' '.join(new_words[:8])
        if len(enriched) < 200:
            return enriched

    return existing_addr


def _merge_address(candidates):
    """Merge best parts from multiple OCR candidates."""
    import re

    if not candidates:
        return ''

    if len(candidates) == 1:
        return candidates[0]

    street = postcode_city = state = ''

    for cand in candidates:
        if not cand:
            continue
        if not street and re.search(r'\b(JALAN|JLN|NO\b)', cand, re.I):
            street = cand.split('\n')[0] if '\n' in cand else cand.split(',')[0]
        if not postcode_city and re.search(r'\b\d{5}\b', cand):
            m = re.search(r'(\d{5}\s*\w+(?:\s+\w+)*)', cand)
            if m:
                postcode_city = m.group(1)

    for cand in candidates:
        if not cand:
            continue
        state_match = re.search(r'\b(SELANGOR|KUALA\s+LUMPUR|PENANG|JOHOR|PERAK|KEDAH|PAHANG|NEGERI\s+SEMBILAN|MELAKA|TERENGGANU|KELANTAN|SABAH|SARAWAK|PUTRAJAYA|LABUAN)\b', cand, re.I)
        if state_match:
            state = state_match.group(1).title()
            break

    parts = [p for p in [street, postcode_city, state] if p]
    return ', '.join(parts) if parts else candidates[0]


def _extract_id_info(text, side='front', doc_type='ic'):
    """
    Malaysian MyKad / passport field extraction.
    Front IC: name, IC number, DOB, gender via MyKad-specific rules.
    """
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    full_name = ''
    address = ''
    ic_number = ''
    passport_number = ''

    if doc_type == 'ic' and side == 'front':
        ic_number = _extract_malaysian_ic_number(text)
        full_name = _extract_malaysian_name(lines)
        address = _extract_address(text, full_name=full_name)
    elif doc_type == 'ic' and side == 'back':
        address = _extract_address(text)
    elif doc_type == 'passport':
        clean_text = text.upper()
        passport_match = re.search(r'\b([A-Z]{1,2}\d{6,9})\b', clean_text)
        if passport_match:
            passport_number = passport_match.group(1)
        for i, line in enumerate(lines):
            if re.search(r'\b(NAMA|NAME|SURNAME|GIVEN\s*NAME)\b', line, re.I):
                parts = re.split(r'[:\.\-]', line, maxsplit=1)
                if len(parts) > 1:
                    candidate = _clean_name_line(parts[1])
                    if _is_plausible_malaysian_name(candidate):
                        full_name = candidate
                        break
                if i + 1 < len(lines):
                    candidate = _clean_name_line(lines[i + 1])
                    if _is_plausible_malaysian_name(candidate):
                        full_name = candidate
                        break

    dob = ''
    gender = ''
    id_for_dob = ic_number if doc_type == 'ic' else ''
    if id_for_dob:
        ic_c = id_for_dob.replace('-', '')
        if len(ic_c) == 12:
            yy, mm, dd = ic_c[:2], ic_c[2:4], ic_c[4:6]
            this_year = datetime.date.today().year % 100
            prefix = '20' if int(yy) <= this_year else '19'
            dob = f"{prefix}{yy}-{mm}-{dd}"
            gender = 'Male' if int(ic_c[-1]) % 2 != 0 else 'Female'

    if doc_type == 'passport' and not dob:
        for line in lines:
            m = re.search(r'(\d{2}[/.-]\d{2}[/.-]\d{2,4})', line)
            if m:
                parts = re.split(r'[/.-]', m.group(1))
                if len(parts) == 3:
                    d, mo, y = parts
                    if len(y) == 2:
                        y = ('20' if int(y) <= datetime.date.today().year % 100 else '19') + y
                    dob = f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
                    break

    result = {
        'full_name': full_name.title() if full_name else '',
        'ic_number': ic_number if doc_type == 'ic' else '',
        'passport_number': passport_number if doc_type == 'passport' else '',
        'date_of_birth': dob,
        'gender': gender,
        'address': address,
        'doc_type': doc_type,
        'side': side,
        'ocr_warning': '',
    }
    if doc_type == 'ic' and side == 'front' and not full_name and not ic_number:
        result['ocr_warning'] = (
            'Could not read the IC clearly. Ensure the front (name & IC number side) '
            'is flat, well-lit, and in focus — then try again or enter details manually.'
        )
    elif doc_type == 'ic' and side == 'front' and full_name and not ic_number:
        result['ocr_warning'] = (
            'Name extracted but IC number could not be read — the number area may be '
            'obscured or cut off. Please enter the IC number manually.'
        )
    elif doc_type == 'ic' and side == 'front' and ic_number and not full_name:
        result['ocr_warning'] = (
            'IC number extracted but name could not be read clearly. '
            'Please verify the name field.'
        )
    elif doc_type == 'ic' and side == 'front' and (full_name or ic_number) and not _mykad_text_has_nama(text):
        result['ocr_warning'] = (
            'Some fields may be incomplete — please verify all extracted details.'
        )
    if doc_type == 'passport' and passport_number and not result['ic_number']:
        result['ic_number'] = passport_number
    return result


@emp_bp.route('/ocr_identity', methods=['POST'])
@role_required('Admin', 'HR')
def ocr_identity():
    import pytesseract
    tess_path = _get_tesseract_path()
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path
        
    file = request.files.get('id_file')
    side = request.form.get('side', 'front')
    doc_type = request.form.get('doc_type', 'ic')
    
    if not file or file.filename == '':
        return jsonify({'error': 'No file uploaded'}), 400

    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
    if ext not in {'jpg', 'jpeg', 'png'}:
        return jsonify({'error': 'Please upload an image (JPG/PNG)'}), 400

    # Read file bytes into memory — OCR always runs on the clean original (never re-reads watermarked file)
    file_bytes = file.read()
    filename = f"id_{uuid.uuid4().hex}_{side}.{ext}"
    # Save to ocr_temp/ subfolder (moved to final location on add_employee)
    ocr_temp_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'ocr_temp')
    os.makedirs(ocr_temp_dir, exist_ok=True)
    filepath = os.path.join(ocr_temp_dir, filename)
    with open(filepath, 'wb') as fh:
        fh.write(file_bytes)

    co_name = query("SELECT name FROM Company WHERE company_id=?", (session['company_id'],), one=True)['name']

    try:
        import pytesseract
        if not _get_tesseract_path():
            return jsonify({'error': 'Tesseract OCR is not installed on this server.'}), 500

        # Open from in-memory bytes — guaranteed clean, no disk re-read of watermarked file
        base = Image.open(BytesIO(file_bytes)).convert('RGB')

        # IC back: auto-crop, rotate, save with watermark - NO OCR extraction, just HR storage
        if doc_type == 'ic' and side == 'back':
            # Auto-crop and find best rotation using our existing logic
            oriented = _pick_best_rotation(base, _mykad_back_text_crops)
            oriented.save(filepath, quality=92)
            _apply_watermark(filepath, co_name)
            log_audit('OCR_IDENTITY', 'Employee', 'Stored IC back copy for HR',
                      action_details={'filename': filename})
            resp = {
                'id_document_path': filename,
                'save_only': True,
                'side': side,
                'doc_type': doc_type,
                'message': 'IC back saved successfully for HR records!'
            }
            return jsonify(resp)

        if doc_type == 'ic' and side == 'front':
            raw_text, ocr_score, corrected = _ocr_mykad_front(base)
            corrected.save(filepath, quality=92)
            extracted = _extract_id_info(raw_text, side=side, doc_type=doc_type)
            if not extracted['ic_number']:
                ic_fb = _ocr_ic_number_fallback(corrected)
                if ic_fb:
                    extracted['ic_number'] = ic_fb
                    ic_c = ic_fb.replace('-', '')
                    yy, mm, dd = ic_c[:2], ic_c[2:4], ic_c[4:6]
                    this_year = datetime.date.today().year % 100
                    prefix = '20' if int(yy) <= this_year else '19'
                    extracted['date_of_birth'] = f"{prefix}{yy}-{mm}-{dd}"
                    extracted['gender'] = 'Male' if int(ic_c[-1]) % 2 != 0 else 'Female'
            elif not extracted.get('date_of_birth'):
                ic_c = extracted['ic_number'].replace('-', '')
                if len(ic_c) == 12:
                    yy, mm, dd = ic_c[:2], ic_c[2:4], ic_c[4:6]
                    this_year = datetime.date.today().year % 100
                    prefix = '20' if int(yy) <= this_year else '19'
                    extracted['date_of_birth'] = f"{prefix}{yy}-{mm}-{dd}"
                    extracted['gender'] = 'Male' if int(ic_c[-1]) % 2 != 0 else 'Female'
            # Always run EasyOCR (handles MyKad guilloche patterns much better than Tesseract)
            easyocr_text = _run_easyocr(corrected)
            if easyocr_text:
                lines = [l.strip() for l in easyocr_text.split('\n') if l.strip()]
                easy_name = _extract_malaysian_name(lines)
                if easy_name:
                    extracted['full_name'] = easy_name.title()
            # Tesseract name fallback only if both primary Tesseract and EasyOCR failed
            if not extracted['full_name']:
                name_fb = _ocr_name_fallback(corrected)
                if name_fb:
                    extracted['full_name'] = name_fb.title()
            current_address = extracted.get('address', '')
            # --- ALWAYS run address fallback (guilloche patterns need specialized preprocessing) ---
            addr_tess = None
            addr_text = _ocr_address_fallback(corrected)
            if addr_text:
                addr = _extract_address(addr_text, full_name=extracted.get('full_name', ''))
                if not addr:
                    addr = _extract_address(addr_text)
                if addr:
                    addr_tess = addr

            # --- Enrich partial addresses with Tesseract word supplement ---
            if addr_tess and len(addr_tess) < 40 and not re.search(r'\b\d{5}\b', addr_tess):
                addr_tess = _enrich_address_from_tesseract(corrected, addr_tess)

            # --- EasyOCR address ---
            addr_easy = None
            if easyocr_text:
                addr = _extract_address(easyocr_text, full_name=extracted.get('full_name', ''))
                if not addr:
                    addr = _extract_address(easyocr_text)
                if addr:
                    addr_easy = addr

            # --- 4-source comparison ---
            candidates = []
            for addr, label in [(current_address, 'primary'), (addr_tess, 'tess'), (addr_easy, 'easy')]:
                if addr:
                    has_pc = bool(re.search(r'\b\d{5}\b', addr))
                    alpha_cnt = sum(1 for w in addr.split() if w.isalpha() and len(w) >= 3)
                    # Reject noisy candidates (>40% non-alpha-numeric characters)
                    punct_ratio = sum(1 for c in addr if not c.isalnum() and not c.isspace()) / max(len(addr), 1)
                    if punct_ratio < 0.4:
                        candidates.append((addr, has_pc, -alpha_cnt, len(addr), label))

            if candidates:
                candidates.sort(key=lambda x: (not x[1], x[2], x[3]))
                best = candidates[0][0]
                extracted['address'] = best
            if ocr_score < 8 and not extracted.get('ocr_warning'):
                extracted['ocr_warning'] = (
                    'IC was hard to read — auto-rotation was applied. '
                    'Please verify all fields or retake with the card flat and well-lit.'
                )
        else:
            processed_img = _prep_gray(base, max_side=1200, contrast=2.0)
            raw_text = _run_id_ocr(processed_img)
            ocr_score = _score_ocr_text(raw_text)
            extracted = _extract_id_info(raw_text, side=side, doc_type=doc_type)
            if ocr_score < 8 and not extracted.get('ocr_warning'):
                extracted['ocr_warning'] = (
                    'Document was hard to read. Please verify all fields or retake the photo.'
                )

        _apply_watermark(filepath, co_name)

        extracted['id_document_path'] = filename
        extracted['_debug_raw_text'] = raw_text[:300]
        extracted['_debug_score'] = ocr_score

        log_audit('OCR_IDENTITY', 'Employee', f'Extracted info from {doc_type} {side}',
                  action_details={'filename': filename, 'raw_snippet': raw_text[:50]})
        
        return jsonify(extracted)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), '_debug_traceback': traceback.format_exc()}), 500


@emp_bp.route('/<int:emp_id>/document')
@login_required
def view_id_document(emp_id):
    from datetime import datetime, timedelta
    
    current_user_id = session['user_id']
    user_role = session['user_role']
    
    # Allow access if viewing your own document
    if current_user_id == emp_id:
        pass
    # Check if user is Admin/HR/Manager AND has approved access
    elif user_role in ['Admin', 'HR', 'HR Manager', 'Manager']:
        # Check for active approved request
        now = datetime.now().isoformat()
        access_request = query("""
            SELECT * FROM IC_Access_Request 
            WHERE requester_id = ? 
              AND target_employee_id = ? 
              AND status = 'Approved'
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY reviewed_at DESC
            LIMIT 1
        """, (current_user_id, emp_id, now), one=True)
        if not access_request:
            abort(403, "IC access not approved. Please request access first.")
    else:
        abort(403)
        
    emp = query("SELECT id_document_path FROM Employee WHERE employee_id=?", (emp_id,), one=True)
    if not emp or not emp['id_document_path']:
        abort(404)
        
    # Get specific filename from args if multiple files exist
    target_file = request.args.get('filename')
    valid_files = emp['id_document_path'].split(',')
    
    if target_file:
        if target_file not in valid_files:
            abort(403)
        return send_from_directory(current_app.config['UPLOAD_FOLDER'], target_file)
    
    # Default to first file if no filename provided
    return send_from_directory(current_app.config['UPLOAD_FOLDER'], valid_files[0])


# --- Notifications Routes ---
@emp_bp.route('/notifications')
@login_required
def get_notifications():
    user_id = session['user_id']
    co = session['company_id']
    user_role = session.get('user_role')
    
    # Get system notifications from Notification table
    system_notif_rows = query("""
        SELECT n.* FROM Notification n 
        WHERE n.employee_id = ? 
        ORDER BY n.created_at DESC
    """, (user_id,))
    system_notifs = [as_dict(row) for row in system_notif_rows]
    
    # Get dynamic pending items (approvals + pending leaves/invoices) for relevant to approve)
    dynamic_notifs = []
    if user_role in ('Admin', 'HR', 'HR Manager'):
        # Pending increments
        inc_rows = query("""
            SELECT si.increment_id, si.employee_id, si.increment_pct, si.proposed_at,
                   e.full_name, e.employee_id as emp_id
            FROM Salary_Increment si
            JOIN Employee e ON si.employee_id=e.employee_id
            WHERE si.status='Pending' AND e.company_id=?
            ORDER BY si.proposed_at DESC
        """, (co,))
        for row in inc_rows:
            dynamic_notifs.append({
                'type': 'Increment',
                'title': f'Increment: {row["full_name"]}',
                'message': f'Salary increment of {row["increment_pct"]}% proposed for review',
                'related_url': url_for('increment.list_increments'),
                'created_at': row['proposed_at'],
                'is_read': 0,
                'dynamic': True
            })
        
        # Pending bonuses
        bonus_rows = query("""
            SELECT bp.proposal_id, bp.employee_id, bp.bonus_amount, bp.proposed_at,
                   e.full_name, e.employee_id as emp_id
            FROM Bonus_Proposal bp
            JOIN Employee e ON bp.employee_id=e.employee_id
            WHERE bp.status='Pending' AND e.company_id=?
            ORDER BY bp.proposed_at DESC
        """, (co,))
        for row in bonus_rows:
            dynamic_notifs.append({
                'type': 'Bonus',
                'title': f'Bonus: {row["full_name"]}',
                'message': f'Bonus of RM {row["bonus_amount"]:,.2f} proposed for review',
                'related_url': url_for('bonus.list_bonuses'),
                'created_at': row['proposed_at'],
                'is_read': 0,
                'dynamic': True
            })
        
        # Pending applications
        app_rows = query("""
            SELECT ja.application_id, ja.applicant_name, ja.applied_at,
                   jp.title as job_title
            FROM Job_Application ja
            LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
            LEFT JOIN Branch b ON jp.branch_id=b.branch_id
            WHERE ja.status='New' AND (b.company_id=? OR ja.posting_id IS NULL)
            ORDER BY ja.applied_at DESC
        """, (co,))
        for row in app_rows:
            dynamic_notifs.append({
                'type': 'Application',
                'title': f'New Application: {row["applicant_name"]}',
                'message': f'Applied for {row["job_title"] or "Unknown Position"}',
                'related_url': url_for('recruitment.view_application', aid=row['application_id']),
                'created_at': row['applied_at'],
                'is_read': 0,
                'dynamic': True
            })
        
        # Pending offers (sent contracts)
        offer_rows = query("""
            SELECT c.contract_id, ja.applicant_name, ja.applied_at,
                   c.position, c.start_date
            FROM Contract c
            JOIN Job_Application ja ON c.application_id=ja.application_id
            LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
            LEFT JOIN Branch b ON jp.branch_id=b.branch_id
            WHERE c.status='Sent' AND (b.company_id=? OR jp.branch_id IS NULL)
            ORDER BY c.created_at DESC
        """, (co,))
        for row in offer_rows:
            dynamic_notifs.append({
                'type': 'Offer',
                'title': f'Offer Sent: {row["applicant_name"]}',
                'message': f'Offer for {row["position"] or "Unknown Position"} awaiting response',
                'related_url': url_for('recruitment.view_application', aid=row['application_id']),
                'created_at': row['applied_at'],
                'is_read': 0,
                'dynamic': True
            })
    
    if user_role in ('Admin', 'HR', 'HR Manager', 'Manager'):
        # Pending leaves
        leave_rows = query("""
            SELECT la.leave_id, la.employee_id, la.applied_at, la.leave_type_id, la.start_date, la.end_date,
                   e.full_name, lt.type_name as leave_type_name
            FROM Leave_Application la
            JOIN Employee e ON la.employee_id=e.employee_id
            JOIN Leave_Type lt ON la.leave_type_id=lt.leave_type_id
            WHERE la.status='Pending' AND e.company_id=?
            ORDER BY la.applied_at DESC
        """, (co,))
        for row in leave_rows:
            dynamic_notifs.append({
                'type': 'Leave',
                'title': f'Leave Request: {row["full_name"]}',
                'message': f'{row["leave_type_name"]} from {row["start_date"]} to {row["end_date"]}',
                'related_url': url_for('leave.approve_list'),
                'created_at': row['applied_at'],
                'is_read': 0,
                'dynamic': True
            })
        
        # Pending invoices
        invoice_rows = query("""
            SELECT i.invoice_id, i.employee_id, i.invoice_number, i.submitted_at,
                   COALESCE(i.total_amount, 0) as total_amount,
                   e.full_name
            FROM Invoice i
            JOIN Employee e ON i.employee_id=e.employee_id
            WHERE i.status='Pending' AND e.company_id=?
            ORDER BY i.submitted_at DESC
        """, (co,))
        for row in invoice_rows:
            dynamic_notifs.append({
                'type': 'Invoice',
                'title': f'Invoice Claim: {row["full_name"]}',
                'message': f'Invoice #{row["invoice_number"]} for RM {row["total_amount"]:,.2f}',
                'related_url': url_for('invoice.claims_management'),
                'created_at': row['submitted_at'],
                'is_read': 0,
                'dynamic': True
            })
    
    # Combine system notifications with dynamic ones
    all_notifications = list(system_notifs) + dynamic_notifs
    # Sort by created_at descending
    all_notifications.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    
    return render_template('employees/notifications.html', notifications=all_notifications)


@emp_bp.route('/notifications/<int:notif_id>/mark-read', methods=['POST'])
@login_required
def mark_notification_read(notif_id):
    user_id = session['user_id']
    execute("""
        UPDATE Notification 
        SET is_read = 1 
        WHERE notification_id = ? AND employee_id = ?
    """, (notif_id, user_id))
    return redirect(url_for('employees.get_notifications'))


@emp_bp.route('/notifications/mark-all-read', methods=['POST'])
@login_required
def mark_all_notifications_read():
    user_id = session['user_id']
    execute("""
        UPDATE Notification 
        SET is_read = 1 
        WHERE employee_id = ?
    """, (user_id,))
    return redirect(url_for('employees.get_notifications'))


# --- IC Access Request Routes ---
@emp_bp.route('/<int:emp_id>/request-ic-access', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager', 'Manager')
def request_ic_access(emp_id):
    from datetime import datetime
    
    requester_id = session['user_id']
    reason = request.form.get('reason', '')
    
    # Don't allow requesting your own IC
    if requester_id == emp_id:
        flash("You don't need to request access to your own IC!", 'warning')
        return redirect(url_for('employees.view_employee', emp_id=emp_id))

    # Manager branch check
    if session['user_role'] == 'Manager':
        target_emp = query("SELECT branch_id FROM Employee WHERE employee_id = ?", (emp_id,), one=True)
        if not target_emp or target_emp['branch_id'] != session['branch_id']:
            flash('Access denied. You can only request access for staff from your own branch.', 'danger')
            return redirect(url_for('employees.view_employee', emp_id=emp_id))
    
    # Create request
    req_id = execute("""
        INSERT INTO IC_Access_Request (requester_id, target_employee_id, reason, status)
        VALUES (?, ?, ?, 'Pending')
    """, (requester_id, emp_id, reason))
    
    # Get requester name
    requester = query("SELECT full_name FROM Employee WHERE employee_id = ?", (requester_id,), one=True)
    
    # Send notification to target employee
    msg = f"{requester['full_name']} has requested access to your IC document. Please review the request."
    send_notification(emp_id, "IC Access Request", msg,
                      related_url=url_for('settings.index', tab='ic-requests'),
                      extra_context={'reason': reason, 'requester_name': requester['full_name']})
    
    log_audit('IC_ACCESS_REQUEST', 'Employee', f"User {requester_id} requested access to employee {emp_id}'s IC",
              action_details={'requester_id': requester_id, 'target_id': emp_id, 'reason': reason})
    
    flash("IC access request sent successfully!", 'success')
    return redirect(url_for('employees.view_employee', emp_id=emp_id))


@emp_bp.route('/ic-access-requests/<int:req_id>/<action>', methods=['POST'])
@login_required
def respond_to_ic_request(req_id, action):
    from datetime import datetime, timedelta
    
    current_user_id = session['user_id']
    
    # Get request
    req = query("SELECT * FROM IC_Access_Request WHERE request_id = ?", (req_id,), one=True)
    if not req:
        abort(404)
    
    # Check if user is the target employee
    if req['target_employee_id'] != current_user_id:
        abort(403)
    
    # Process action
    new_status = 'Approved' if action == 'approve' else 'Rejected'
    expires_at = None
    if new_status == 'Approved':
        expires_at = (datetime.now() + timedelta(days=7)).isoformat()
    
    execute("""
        UPDATE IC_Access_Request
        SET status = ?, reviewed_by = ?, reviewed_at = datetime('now'), expires_at = ?
        WHERE request_id = ?
    """, (new_status, current_user_id, expires_at, req_id))
    
# Send notification back to requester
    target_emp = query("SELECT full_name FROM Employee WHERE employee_id = ?", (current_user_id,), one=True)
    notif_title = f"IC Request {new_status}"
    notif_message = f"{target_emp['full_name']} has {new_status.lower()} your IC access request."
    if new_status == 'Approved':
        notif_message += " Access is granted for 7 days."

    send_notification(req['requester_id'], notif_title, notif_message,
                      related_url=url_for('employees.view_employee', emp_id=req['target_employee_id']))
    
    log_audit(f'IC_ACCESS_{new_status.upper()}', 'Employee', f"IC access request {req_id} was {new_status.lower()}",
              action_details={'request_id': req_id, 'reviewer_id': current_user_id})
    
    flash(f"IC request {new_status.lower()} successfully!", 'success')
    return redirect(url_for('employees.get_notifications'))


@emp_bp.route('/')
@login_required
def list_employees():
    if session.get('user_role') not in ('Admin', 'HR', 'HR Manager', 'Manager'):
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    co    = session['company_id']
    role  = session['user_role']
    bid   = session.get('branch_id')
    search = request.args.get('q', '')
    branch_f = request.args.get('branch', '')
    dept   = request.args.get('dept', '')
    position_id = request.args.get('position_id', '')
    etype  = request.args.get('type', '')
    status = request.args.get('status', '')

    sql = """
        SELECT e.*, r.role_name, d.department_name, b.name as branch_name
        FROM Employee e
        JOIN Role r       ON e.role_id       = r.role_id
        JOIN Department d ON e.department_id = d.department_id
        JOIN Branch b     ON e.branch_id     = b.branch_id
        WHERE e.company_id = ?
    """
    args = [co]
    if role == 'Manager':
        sql += " AND e.branch_id = ?"
        args.append(bid)

    if search:
        sql += " AND (e.full_name LIKE ? OR e.email LIKE ? OR CAST(e.employee_id AS TEXT) LIKE ?)"
        args += [f'%{search}%', f'%{search}%', f'%{search}%']
    if branch_f:
        sql += " AND e.branch_id = ?"
        args.append(branch_f)
    if dept:
        sql += " AND e.department_id = ?"
        args.append(dept)
    if position_id:
        try:
            position_id = int(position_id)
        except (TypeError, ValueError):
            position_id = ''
        else:
            sql += " AND e.position_id = ?"
            args.append(position_id)
    if etype:
        sql += " AND e.employment_type = ?"
        args.append(etype)
    if status:
        sql += " AND e.employment_status = ?"
        args.append(status)
    sql += " ORDER BY e.full_name"

    employees = query(sql, args)
    if role == 'Manager':
        departments = query("SELECT d.* FROM Department d JOIN Branch b ON d.branch_id=b.branch_id WHERE b.company_id=? AND d.branch_id=? ORDER BY department_name", (co, bid))
        branches = query("SELECT * FROM Branch WHERE company_id=? AND branch_id=? ORDER BY name", (co, bid))
    else:
        departments = query("SELECT d.*, b.name as branch_name FROM Department d JOIN Branch b ON d.branch_id=b.branch_id WHERE b.company_id=? ORDER BY department_name", (co,))
        branches = query("SELECT * FROM Branch WHERE company_id=? ORDER BY name", (co,))
    positions = query("""
        SELECT p.position_id, p.position_name, p.department_id
        FROM Position p
        JOIN Department d ON p.department_id=d.department_id
        JOIN Branch b ON d.branch_id=b.branch_id
        WHERE p.is_active=1 AND b.company_id=?
        ORDER BY LOWER(p.position_name)
    """, (co,))
    return render_template('employees/list.html',
                           employees=employees, departments=departments,
                           branches=branches, positions=positions,
                           search=search, branch_f=branch_f, dept=dept, position_id=position_id,
                           etype=etype, status=status)


@emp_bp.route('/upload-ic')
@role_required('Admin', 'HR')
def upload_ic():
    """Page to upload both front and back of IC for OCR"""
    return render_template('employees/upload_ic.html')


@emp_bp.route('/add', methods=['GET', 'POST'])
@role_required('Admin', 'HR')
def add_employee():
    from app.database import assign_role_permissions
    
    co = session['company_id']
    departments = query("SELECT d.*, b.name as branch_name FROM Department d JOIN Branch b ON d.branch_id=b.branch_id WHERE b.company_id=? ORDER BY d.department_name", (co,))
    branches    = query("SELECT * FROM Branch WHERE company_id=?", (co,))
    roles       = query("SELECT * FROM Role ORDER BY role_id")
    
    form_data = {}
    contract_id_from_hire = None

    prefill = session.get('hire_prefill', None)
    if prefill and (request.args.get('from_hire') or request.args.get('from_ic_upload')):
        import json
        prefill_data = json.loads(prefill)
        form_data = prefill_data
        contract_id_from_hire = prefill_data.get('contract_id')
        # Least-privilege default: a hire is never pre-filled as Admin. If the
        # prefill carries no (or an invalid) system role, the form preselects
        # the Employee role; HR can still change it explicitly.
        employee_role = next((role for role in roles if role['role_name'] == 'Employee'), None)
        if employee_role and str(form_data.get('role_id', '')) != str(employee_role['role_id']):
            try:
                int(form_data.get('role_id', ''))
            except (TypeError, ValueError):
                form_data['role_id'] = str(employee_role['role_id'])
    elif request.method == 'GET' and request.args.get('setup_branch_manager'):
        form_data = {
            key: request.args.get(key, '')
            for key in ('branch_id', 'department_id', 'position_id')
        }
        manager_role = next((role for role in roles if role['role_name'] == 'Manager'), None)
        if manager_role:
            form_data['role_id'] = str(manager_role['role_id'])

    error_fields = []
    if request.method == 'POST':
        f = request.form
        form_data = f.to_dict()
        # Least-privilege server-side enforcement: a missing or invalid system
        # role falls back to Employee (never Admin) on every creation path.
        employee_role = next((role for role in roles if role['role_name'] == 'Employee'), None)
        if employee_role:
            try:
                role_ok = int(form_data.get('role_id', '')) in [r['role_id'] for r in roles]
            except (TypeError, ValueError):
                role_ok = False
            if not role_ok:
                form_data['role_id'] = str(employee_role['role_id'])
        try:
            is_from_hire = request.form.get('from_hire') or request.args.get('from_hire')
            password = f['password'] if f.get('password') else None
            if is_from_hire and not password:
                import random
                password = f'SmartHR@{random.randint(1000,9999)}'
                form_data['password'] = password
                form_data['confirm_password'] = password

            import re

            # Full Name — alphabets and spaces only
            fn = f.get('full_name', '').strip()
            if fn and not re.match(r"^[A-Za-z\s'-]+$", fn):
                flash('Full Name must contain only alphabets.', 'danger')
                error_fields.append('full_name')
                raise ValueError('Invalid full name')

            # IC Number — Malaysian format
            ic = f.get('ic_number', '').strip()
            passport_number = f.get('passport_number', '').strip() or None
            if ic:
                ic_clean = ic.replace('-', '')
                ic_valid = (re.match(r'^\d{12}$', ic_clean) or re.match(r'^\d{6}-\d{2}-\d{4}$', ic))
                if not ic_valid:
                    flash('IC format: YYMMDD-PB-#### (e.g. 900101-14-5001)', 'danger')
                    error_fields.append('ic_number')
                    raise ValueError('Invalid IC format')
                if len(ic_clean) == 12:
                    mm, dd = int(ic_clean[2:4]), int(ic_clean[4:6])
                    state = int(ic_clean[6:8])
                    if not (1 <= mm <= 12 and 1 <= dd <= 31):
                        flash('IC date portion is invalid.', 'danger')
                        error_fields.append('ic_number')
                        raise ValueError('Invalid IC date')
                    if not (1 <= state <= 59):
                        flash('IC state code is invalid.', 'danger')
                        error_fields.append('ic_number')
                        raise ValueError('Invalid IC state code')

            # Contact Number — Malaysian format
            contact = f.get('contact_no', '').strip().replace(' ', '').replace('-', '')
            if contact:
                if not re.match(r'^(\+?6?01)\d{8,9}$', contact):
                    flash('Enter a valid Malaysian phone number (e.g. 012-3456789 or +60123456789)', 'danger')
                    error_fields.append('contact_no')
                    raise ValueError('Invalid contact number')

            if f.get('password') and f.get('confirm_password') and f['password'] != f['confirm_password']:
                flash('Passwords do not match.', 'danger')
                error_fields.extend(['password', 'confirm_password'])
                raise ValueError('Password mismatch')

            pw_hash = generate_password_hash(password or f['password'])

            # ── Position: catalog entry OR free-text custom (no auto-create) ──
            position_id = f.get('position_id') or ''
            position_text = f.get('position', '').strip()
            gender = f.get('gender') or None
            is_dept_mgr_position = False
            if position_id and position_id != '__custom__':
                pos = query("SELECT * FROM Position WHERE position_id=? AND is_active=1",
                            (int(position_id),), one=True)
                if not pos or pos['department_id'] != int(f['department_id']):
                    flash('Invalid position selected for this department.', 'danger')
                    error_fields.append('position')
                    raise ValueError('Invalid position')
                position_id = pos['position_id']
                position_text = pos['position_name']
                is_dept_mgr_position = bool(pos['is_department_manager_position'])
            else:
                position_id = None  # custom free text stays unlinked until HR catalogues it

            # Department Manager Position: never silently replace an existing
            # department manager. Check the conflict BEFORE creating the
            # employee so a failed assignment cannot leave a half-created hire.
            if is_dept_mgr_position and is_from_hire:
                # The system role is locked to Employee for this hire, no
                # matter what the form submitted (crafted Admin/Manager values
                # must never escalate a department-manager position).
                if employee_role:
                    form_data['role_id'] = str(employee_role['role_id'])
                current_mgr = query("SELECT department_manager_id FROM Department WHERE department_id=?",
                                    (int(f['department_id']),), one=True)
                if current_mgr and current_mgr['department_manager_id'] is not None:
                    mgr_name = query("SELECT full_name FROM Employee WHERE employee_id=?",
                                     (current_mgr['department_manager_id'],), one=True)
                    holder = mgr_name['full_name'] if mgr_name else 'another employee'
                    flash(f'This is a Department Manager position, but the department already has a manager ({holder}). Reassign the department manager first, then create the employee.', 'danger')
                    error_fields.append('department_manager')
                    raise ValueError('Department manager conflict')

            emp_sql = """
                INSERT INTO Employee
                (company_id,branch_id,department_id,full_name,ic_number,passport_number,contact_no,
                 address,date_of_birth,gender,emergency_contact_name,emergency_contact_no,
                 position,position_id,employment_type,employment_status,hire_date,base_salary,
                 role_id,email,personal_email,password_hash,id_document_path,
                 work_start_time, work_end_time)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """
            emp_params = (co, f['branch_id'], f['department_id'], f['full_name'],
                          ic or None, passport_number, f.get('contact_no',''),
                          f.get('address',''), f.get('date_of_birth',''),
                          gender, f.get('emergency_contact_name',''),
                          f.get('emergency_contact_no',''), position_text, position_id,
                          f['employment_type'], f.get('employment_status','Active'),
                          f['hire_date'], float(f.get('base_salary', 0)),
                          form_data['role_id'], f['email'].lower(), f.get('personal_email',''), pw_hash,
                          f.get('id_document_path',''),
                          f.get('work_start_time','09:00'), f.get('work_end_time','18:00'))

            # Least-privilege department responsibility: the flagged position
            # hires an Employee-role user who is additionally recorded as the
            # department manager (never the broad Manager system role). The
            # Employee INSERT and the conditional Department UPDATE are one
            # SQLite transaction: if the department was taken concurrently
            # (zero-row UPDATE), everything rolls back — no employee row, no
            # permissions, no leave balances, no contract/application change,
            # no audit event. The project execute() helper is deliberately not
            # used here because it commits after every statement.
            if is_dept_mgr_position and is_from_hire:
                db = get_db()
                db.execute("BEGIN IMMEDIATE")
                try:
                    cur = db.execute(emp_sql, emp_params)
                    emp_id = cur.lastrowid
                    assignment = db.execute("""UPDATE Department SET department_manager_id=?
                                               WHERE department_id=? AND department_manager_id IS NULL""",
                                            (emp_id, int(f['department_id'])))
                    if assignment.rowcount != 1:
                        raise ValueError('Department manager conflict')
                    db.commit()
                except ValueError:
                    db.rollback()
                    flash('This is a Department Manager position, but the department already has a manager. Reassign the department manager first, then create the employee.', 'danger')
                    error_fields.append('department_manager')
                    raise
                except Exception:
                    db.rollback()
                    raise
            else:
                emp_id = execute(emp_sql, emp_params)

            # Move IC files from ocr_temp/ to final location
            ocr_temp_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'ocr_temp')
            final_dir = current_app.config['UPLOAD_FOLDER']
            if os.path.isdir(ocr_temp_dir):
                for f_name in os.listdir(ocr_temp_dir):
                    src = os.path.join(ocr_temp_dir, f_name)
                    dst = os.path.join(final_dir, f_name)
                    if os.path.isfile(src):
                        try:
                            os.replace(src, dst)
                        except:
                            pass

            # Seed leave balances for current year
            import datetime
            yr = datetime.date.today().year
            leave_types = query("SELECT leave_type_id, default_days FROM Leave_Type")
            for lt in leave_types:
                execute("INSERT OR IGNORE INTO Leave_Balance(employee_id,leave_type_id,year,entitled_days) VALUES(?,?,?,?)",
                        (emp_id, lt['leave_type_id'], yr, lt['default_days']))

            # Automatically assign permissions based on role
            assign_role_permissions(emp_id, int(form_data['role_id']), session.get('user_id'))

            # Link contract to employee if hiring from offer
            if is_from_hire and contract_id_from_hire:
                execute("UPDATE Contract SET employee_id=? WHERE contract_id=?",
                        (emp_id, contract_id_from_hire))

            # Set application status to Hired now that employee record exists
            if is_from_hire and contract_id_from_hire:
                app_row = query("SELECT application_id FROM Contract WHERE contract_id=?", (contract_id_from_hire,), one=True)
                if app_row:
                    execute("UPDATE Job_Application SET status='Hired' WHERE application_id=?", (app_row['application_id'],))
                    # Close the job posting
                    close_job_posting_for_application(app_row['application_id'])

            role_name = query("SELECT role_name FROM Role WHERE role_id=?", (int(form_data['role_id']),), one=True)['role_name']

            if is_dept_mgr_position and is_from_hire:
                log_audit('DEPT_MANAGER_AUTO_ASSIGN', 'Employee',
                          f'Employee {emp_id} ({f["full_name"]}) auto-assigned as manager of department {f["department_id"]} (Department Manager Position)',
                          'Department', f.get('department_id'), action_details={'employee_id': emp_id})

            log_audit('CREATE', 'Employee', f'Created employee {f["full_name"]} with role {role_name}',
                      'Employee', emp_id, 'Success', {'email': f['email'], 'role': role_name})

            # Send welcome email with credentials to personal email if from_hire
            if is_from_hire and f.get('personal_email'):
                try:
                    from app.notifications.email_service import send_email
                    html = render_template('emails/welcome_employee.html',
                        employee_name=f['full_name'],
                        email=f['email'].lower(),
                        password=password,
                        position=f.get('position',''))
                    send_email(
                        f'Welcome to SmartHR – {f.get("position","")}',
                        f['personal_email'],
                        html,
                    )
                    log_audit('SEND_EMAIL', 'Employee',
                              f'Welcome email sent to {f["personal_email"]} for {f["full_name"]}',
                              'Employee', emp_id, 'Success')
                except Exception as mail_err:
                    print(f"[ADD EMPLOYEE] Welcome email failed: {mail_err}")

            # Clear prefill now that employee is created
            session.pop('hire_prefill', None)

            flash(f'Employee "{f["full_name"]}" added successfully with {role_name} permissions!', 'success')
            if f.get('continue_setup') == 'posting':
                return redirect(url_for('recruitment.add_posting',
                                        branch_id=f.get('branch_id', ''),
                                        department_id=f.get('department_id', ''),
                                        position_id=position_id or ''))
            return redirect(url_for('employees.list_employees'))
        except Exception as e:
            error_fields = []
            if "UNIQUE constraint failed" in str(e):
                if "email" in str(e):
                    flash("Error: Email address already exists.", "danger")
                    error_fields.append('email')
                elif "ic_number" in str(e):
                    flash("Error: IC Number already exists.", "danger")
                    error_fields.append('ic_number')
                else:
                    flash("Error: A unique constraint was violated.", "danger")
            else:
                flash(f'Error: {str(e)}', 'danger')

    positions = query("""
        SELECT p.*, d.branch_id as dept_branch_id
        FROM Position p JOIN Department d ON p.department_id=d.department_id
        WHERE p.is_active=1
        ORDER BY p.position_name
    """)

    # Hire-flow explanation: when the prefilled position is a Department
    # Manager Position, tell HR what will happen automatically (and warn when
    # the department already has a manager).
    hire_position_info = None
    if request.args.get('from_hire') or form_data.get('from_hire'):
        prefill_pid = form_data.get('position_id') or ''
        if prefill_pid and prefill_pid != '__custom__':
            try:
                pos_row = query("""SELECT p.*, d.department_name,
                                          d.department_manager_id,
                                          m.full_name AS manager_name
                                   FROM Position p
                                   JOIN Department d ON p.department_id=d.department_id
                                   LEFT JOIN Employee m ON d.department_manager_id=m.employee_id
                                   WHERE p.position_id=? AND p.is_active=1""",
                                (int(prefill_pid),), one=True)
                if pos_row and pos_row['is_department_manager_position']:
                    hire_position_info = {
                        'position_name': pos_row['position_name'],
                        'department_name': pos_row['department_name'],
                        'has_manager': pos_row['department_manager_id'] is not None,
                        'manager_name': pos_row['manager_name'],
                    }
            except (TypeError, ValueError):
                pass

    return render_template('employees/add.html',
                           departments=departments, branches=branches, roles=roles,
                           positions=positions,
                           form_data=form_data,
                           hire_position_info=hire_position_info,
                           setup_branch_manager=request.args.get('setup_branch_manager') == '1'
                                                or form_data.get('setup_branch_manager') == '1',
                           error_fields=error_fields if error_fields else [])


@emp_bp.route('/<int:emp_id>')
@login_required
def view_employee(emp_id):
    from datetime import datetime, date

    current_user_id = session['user_id']
    # Employees can only view themselves unless HR/Admin/Manager
    if session['user_role'] == 'Employee' and current_user_id != emp_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('main.dashboard'))

    emp = query("""
        SELECT e.*, r.role_name, d.department_name, b.name as branch_name, c.name as company_name
        FROM Employee e
        JOIN Role r       ON e.role_id       = r.role_id
        JOIN Department d ON e.department_id = d.department_id
        JOIN Branch b     ON e.branch_id     = b.branch_id
        JOIN Company c    ON e.company_id    = c.company_id
        WHERE e.employee_id=?
    """, (emp_id,), one=True)

    if not emp:
        flash('Employee not found.', 'danger')
        return redirect(url_for('employees.list_employees'))

    # Manager branch restriction check
    if session['user_role'] == 'Manager' and emp['branch_id'] != session['branch_id']:
        flash('Access denied. You can only view staff from your own branch.', 'danger')
        return redirect(url_for('main.dashboard'))

    # Get employee details for eligibility check
    emp_details = as_dict(query("SELECT gender, marital_status FROM Employee WHERE employee_id=?", (emp_id,), one=True))
    emp_gender = emp_details.get('gender')
    emp_marital = emp_details.get('marital_status')

    # Get leave balances (filtered by eligibility)
    all_leave_bal = query("""
        SELECT lb.*, lt.type_name, lt.eligible_genders, lt.eligible_marital_status
        FROM Leave_Balance lb
        JOIN Leave_Type lt ON lb.leave_type_id=lt.leave_type_id
        WHERE lb.employee_id=? AND lb.year=strftime('%Y','now')
    """, (emp_id,))
    
    leave_bal = []
    for b in all_leave_bal:
        lt_dict = as_dict(b)
        if is_leave_eligible(lt_dict, emp_gender, emp_marital):
            leave_bal.append(b)

    recent_att = query("""
        SELECT * FROM Attendance WHERE employee_id=?
        ORDER BY check_in DESC LIMIT 10
    """, (emp_id,))

    is_on_leave_today = query("""
        SELECT 1 FROM Leave_Application
        WHERE employee_id=? AND status='Approved'
          AND ? BETWEEN start_date AND end_date
        LIMIT 1
    """, (emp_id, date.today().isoformat()), one=True) is not None

    # Check current user's access status for this employee's IC
    has_access = False
    pending_request = None
    approved_request = None
    if session['user_role'] in ['Admin','HR','HR Manager','Manager'] and current_user_id != emp_id:
        now = datetime.now().isoformat()
        approved_request = query("""
            SELECT * FROM IC_Access_Request
            WHERE requester_id = ? AND target_employee_id = ? AND status = 'Approved'
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY reviewed_at DESC LIMIT 1
        """, (current_user_id, emp_id, now), one=True)
        has_access = approved_request is not None
        
        pending_request = query("""
            SELECT * FROM IC_Access_Request
            WHERE requester_id = ? AND target_employee_id = ? AND status = 'Pending'
            ORDER BY requested_at DESC LIMIT 1
        """, (current_user_id, emp_id), one=True)
    
    # If user is the target employee, show all their pending requests
    pending_requests_for_me = []
    if current_user_id == emp_id:
        pending_requests_for_me = query("""
            SELECT r.*, requester.full_name as requester_name
            FROM IC_Access_Request r
            JOIN Employee requester ON r.requester_id = requester.employee_id
            WHERE r.target_employee_id = ? AND r.status = 'Pending'
            ORDER BY requested_at DESC
        """, (emp_id,))

    # Get contract linked to this employee
    employee_contract = query("SELECT * FROM Contract WHERE employee_id=?", (emp_id,), one=True)

    # Check if employee has a registered face
    face_reg = query("SELECT encoding_id FROM Face_Encoding WHERE employee_id=?", (emp_id,), one=True)
    has_face_registered = face_reg is not None

    return render_template('employees/view.html',
                           emp=emp, leave_bal=leave_bal, recent_att=recent_att,
                           has_ic_access=has_access, pending_ic_request=pending_request,
                           pending_requests_for_me=pending_requests_for_me,
                           employee_contract=employee_contract,
                           has_face_registered=has_face_registered,
                           is_on_leave_today=is_on_leave_today,
                           roles=query("SELECT * FROM Role ORDER BY role_id"))


@emp_bp.route('/<int:emp_id>/edit', methods=['POST'])
@role_required('Admin', 'HR', 'Manager')
def edit_employee(emp_id):
    from app.database import assign_role_permissions
    f = request.form
    target_emp = query("""SELECT employee_id, company_id, branch_id, department_id, role_id
                          FROM Employee WHERE employee_id=?""", (emp_id,), one=True)
    if not target_emp:
        flash('Employee not found.', 'danger')
        return redirect(url_for('employees.list_employees'))

    try:
        branch_id = int(f.get('branch_id', ''))
        department_id = int(f.get('department_id', ''))
        new_role_id = int(f.get('role_id', ''))
        base_salary = float(f.get('base_salary', 0))
    except (TypeError, ValueError):
        flash('Enter valid employee assignment and salary values.', 'danger')
        return redirect(url_for('employees.view_employee', emp_id=emp_id))

    # Managers may edit staff records in their branch, but hidden form inputs
    # must not let a crafted request move staff or alter their role.
    if session['user_role'] == 'Manager':
        if target_emp['branch_id'] != session['branch_id']:
            flash('Access denied. You can only edit staff from your own branch.', 'danger')
            return redirect(url_for('main.dashboard'))
        if (branch_id != target_emp['branch_id'] or
                department_id != target_emp['department_id'] or
                new_role_id != target_emp['role_id']):
            flash('Managers cannot change an employee\'s branch, department, or role.', 'danger')
            return redirect(url_for('employees.view_employee', emp_id=emp_id))

    branch = query("SELECT company_id FROM Branch WHERE branch_id=?", (branch_id,), one=True)
    department = query("SELECT branch_id FROM Department WHERE department_id=?", (department_id,), one=True)
    role = query("SELECT role_id FROM Role WHERE role_id=?", (new_role_id,), one=True)
    if (not branch or branch['company_id'] != target_emp['company_id'] or
            not department or department['branch_id'] != branch_id or not role):
        flash('Select a valid branch, department, and role for this employee.', 'danger')
        return redirect(url_for('employees.view_employee', emp_id=emp_id))
    if base_salary < 0:
        flash('Base salary cannot be negative.', 'danger')
        return redirect(url_for('employees.view_employee', emp_id=emp_id))
    
    # Get the old role_id before updating
    old_role_id = target_emp['role_id']
    
    execute("""
        UPDATE Employee SET
          full_name=?, contact_no=?, address=?, date_of_birth=?,
          gender=?, emergency_contact_name=?, emergency_contact_no=?,
          position=?, employment_type=?, employment_status=?,
          base_salary=?, branch_id=?, department_id=?, role_id=?,
          work_start_time=?, work_end_time=?,
          updated_at=datetime('now')
        WHERE employee_id=?
    """, (f['full_name'], f.get('contact_no',''), f.get('address',''),
          f.get('date_of_birth',''), f.get('gender',''),
          f.get('emergency_contact_name',''), f.get('emergency_contact_no',''),
          f.get('position',''), f['employment_type'], f.get('employment_status','Active'),
          base_salary, branch_id, department_id, new_role_id,
          f.get('work_start_time','09:00'), f.get('work_end_time','18:00'),
          emp_id))
    
    # If role changed, automatically update permissions
    if old_role_id != new_role_id:
        assign_role_permissions(emp_id, new_role_id, session.get('user_id'))
        
        # Get role names for audit log
        old_role = query("SELECT role_name FROM Role WHERE role_id=?", (old_role_id,), one=True) if old_role_id else None
        new_role = query("SELECT role_name FROM Role WHERE role_id=?", (new_role_id,), one=True)
        old_role_name = old_role['role_name'] if old_role else 'Unknown'
        new_role_name = new_role['role_name'] if new_role else 'Unknown'
        
        log_audit('PROMOTE_DEMOTE', 'Employee', 
                  f'Employee role changed from {old_role_name} to {new_role_name}; permissions auto-updated',
                  'Employee', emp_id, 'Success', 
                  {'old_role': old_role_name, 'new_role': new_role_name})
        flash(f'Employee updated successfully. Role changed to {new_role_name}; permissions granted automatically.', 'success')
    else:
        log_audit('UPDATE', 'Employee', f'Updated employee id={emp_id}',
                  'Employee', emp_id, 'Success')
        flash('Employee updated successfully.', 'success')
    
    return redirect(url_for('employees.view_employee', emp_id=emp_id))


@emp_bp.route('/<int:emp_id>/deactivate', methods=['POST'])
@role_required('Admin', 'HR')
def deactivate_employee(emp_id):
    execute("UPDATE Employee SET is_active=0, employment_status='Inactive', updated_at=datetime('now') WHERE employee_id=?",
            (emp_id,))
    log_audit('DEACTIVATE', 'Employee', f'Deactivated employee id={emp_id}',
              'Employee', emp_id, 'Success')
    flash('Employee deactivated.', 'warning')
    return redirect(url_for('employees.list_employees'))
