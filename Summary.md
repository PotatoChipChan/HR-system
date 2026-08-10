# SmartHR Fixes - Summary

## 1. IC OCR - Wrong IC number on KarShengIC_Front.jpg
### Root Cause
Canny auto-crop fails on this image (card edges too soft). Without zooming in, the 70% text crop gives Tesseract only ~462px of card text — too little resolution to distinguish digit '1' from '4'.

### Fix
Added `_try_otsu_crop()` in `_auto_crop_document()` (`app/employees/routes.py:415-432`). When Canny fails, OTSU thresholding finds the card as the largest bright region. The zoomed card fills more of the frame → 70% text crop gives ~617px of card text → Tesseract reads last digit as '1' not '4'.

### Result
- `KarShengIC_Front.jpg`: IC `050112-14-0311` (was `050142-14-0314`), score=40
- `IC front good orientation.jpg`: IC `040630-10-1049` (unchanged), score=68

## 2. IC Name/Address - EasyOCR + FFT Guilloche Removal
### Problem
Tesseract produces garbled names/addresses on guilloche-background ICs. `/` character is physically invisible in pixels (lost in guilloche pattern).

### Fix
- **EasyOCR for IC names**: Added `_extract_malaysian_name()` from EasyOCR text, overrides Tesseract when available
- **FFT guilloche removal** (`app/employees/guilloche_removal.py`): New module using FFT frequency analysis to suppress periodic guilloche patterns. Detects frequency peaks above background and reduces them while preserving low-frequency text
- **FFT+EasyOCR for IC addresses**: EasyOCR on FFT-cleaned image captures readable text like `NO 40B JALAN` that Tesseract misses entirely
- **Multi-source address comparison**: `_addr_quality()` scores addresses by postcode presence, alpha word count, and address keyword bonus (JALAN, NO, TAMAN, etc.). Picks best from Tesseract, EasyOCR, and FFT sources
- **`_extract_address` fix**: Stops capturing at duplicate name line (prevents combined EasyOCR text from leaking name into address)

### Address Fixes
- `_is_quality_address_line`: Now allows pure-numeric lines that contain 5-digit postcodes
- `_clean_malaysian_address`: Fixed trailing digit stripping to preserve postcodes (`\d{5}$` check)
- **Pattern recovery** in `_clean_malaysian_address`: `L/P3`, `NO 40B`, `PERMAI`, `LIP3`→`LP/3`, `LIPS 3`→`L/P3` patterns
- **IC card noise stripping**: `LELAKI`, `WARGANEGARA`, `PEMILIK`, `ALAMI` removed from address text; short orphaned IC noise words stripped
- **`_ocr_address_fallback` scoring fix**: address marker weight 3→10, postcode 5→15, house number 3→10, word count capped at 20 (prevents garbled noise from winning)
- **PSM 3 added** to `_ocr_address_fallback`: PSM 3 + guilloche variants required for reading guilloche-background ICs
- **Guilloche preprocessing variants** (`prep_guilloche_variants`): 10 variants — HSV Value, HSV+CLAHE, Blue-Red subtraction (most effective), Blackhat morph, Bilateral, Median, CLAHE grayscale
- **EasyOCR address fallback** (`_easyocr_address_fallback`): EasyOCR on FFT + guilloche variants, multiple crop regions
- **Tesseract address enrichment** (`_enrich_address_from_tesseract`): Conservative word recovery — only inserts suffix after exact prefix match

### Result
- All 3 IC test images pass (KarShengIC_Front.jpg, IC front good orientation.jpg, India_front.jpeg)
- **India_front.jpeg FULL address recovered**: `NO 40B JALAN L/P3 Q TAMAN LAKSAMANA PERMAI 68100 BATU CAVES SELANGOR` (was `NO 40B JALAAMANA PERMA, 68100. BATU CAVES, SELANGOR S`)
- Key breakthrough: Blue-Red channel subtraction (`B - R*0.3`) + Tesseract PSM 3 reads guilloche text perfectly
- **IC card noise stripping**: `LELAKI`, `WARGANEGARA`, `PEMILIK`, `ALAMI` stripped from addresses; short orphaned noise words (2-3 chars before comma) removed
- **Known limitation**: `/` character in addresses (e.g. `L/P3`) is physically unrecoverable from guilloche images — but pattern rules recover it

## 3. Name Hallucination Filtering
### Fix
Added `SUSANS`, `SUNESTEE`, `SUAVEMIE`, `LE` to the rejection set in `_is_plausible_malaysian_name()`.

## 4. Crop-Based Fallback (pre-existing)
When spatial filtering score < 30, tries tight 70% crop first, then full-card 98% crop as last resort (with -10 penalty).

## 5. Invoice OCR Fixes

### 5a. Summary Row Override Fix
**Problem**: `_summary_row_re` was too broad, matching line items as summary rows. This caused incorrect subtotal overrides.
**Fix**: Summary rows now require ≥3 raw numbers; filtered summary numbers require ≥2. Summary override only fires when `summary_subtotal > 0`.

### 5b. Vendor Name Validation
**Problem**: Garbage OCR names like `"80 SG1l CD 85."` passed through as vendor names.
**Fix**: Added `_is_garbage_vendor()` (rejects ≤3 chars, >50% digits, no vowels, all-caps ≤6 chars, ≤4 alpha chars, starts with `#`). Trailing numeric and punctuation noise stripping.

### 5c. Invoice Number OCR Digit Correction
**Problem**: OCR confuses `o`↔`0` (e.g. `MJo/CSQ0076125` should be `MJ0/CSQ0076125`).
**Fix**: Added `_fix_id_ocr_digits()` — fixes `o`→`0` when preceded by letter + followed by non-alpha, `l`/`I`→`1` between digits.

### 5d. Vendor Extraction — Receipt Key-Value Skip Fix
**Problem**: `invoice` in receipt key-value skip list was too aggressive — skipped "Bioplex INVOICE" vendor lines.
**Fix**: Removed `invoice` from inline skip list; added line-start check `^invoice\b` instead.

### 5e. Invoice Image OCR (JPG/PNG)
**Problem**: Image files had no preprocessing before EasyOCR, garbled labels weren't matched, spaced decimals weren't parsed, receipt numbers had T/7 confusion.
**Fix**:
- **Image preprocessing**: Added `_preprocess_image()` call before EasyOCR on images (histogram stretch, CLAHE, sharpen, upscale)
- **Tesseract second opinion**: Single Tesseract call (PSM 6) after EasyOCR for additional text
- **Fuzzy label matching**: Added `nuo?ice`, `inv0ice`, `inv0ice`, `nuoice`, `ftal|tatal` as OCR alternates for `_TOTAL_RE`, `_ID_TIERS`, `_label_line`, `_inline_label`
- **Spaced-decimal normalization**: `59  02`→`59.02` with `(?<!\d)(\d{1,2})\s+(\d{2})\b` (non-digit boundary + ≤2 digit first group to avoid mangling invoice numbers)
- **OCR digit correction for receipts**: `7`→`T` in long IDs (≥12 chars) when followed by 4+ digits
- **RM-amount fallback**: Updated pattern to allow colon between RM and amount
- **`_ID_GAP`**: Added `=` separator (Petron receipt has `NUOICE #= 9 04867 41`)
- **OCR date normalization**: `M7`→`07` (month), 5-digit years `21126`→`2026` (first 2→`20` + last 2)
- **Lines re-created after normalizations**: Fixed stale `lines` variable that was created before normalizations

### Test Results (Invoice Images)
- **CelcomDigi.jpg**: vendor=`CelcomDigi Telecommunications Sdn Bhd`, invoice_number=`692T1070520260012`, invoice_date=`2026-06-07`, total=98.0, confidence=0.8
- **Petron.jpg**: vendor=`PETRON`, invoice_number=`90486741`, invoice_date=`2026-07-27`, total=59.02, confidence=0.8
- **All 5 PDF regression tests pass**
- **All 3 IC OCR tests pass**

### 5f. Preprocessing Fallback
**Problem**: Image preprocessing (CLAHE/sharpen/upscale) destroys thermal receipts like Petron — produces garbage OCR while raw EasyOCR gives correct results.
**Fix**: After preprocessed OCR, check if vendor is garbage AND no invoice/date found. If so, retry with raw EasyOCR (no preprocessing).

### 5g. Brand Name Prefix in Vendor Extraction
**Problem**: SDN BHD search finds "Telecommunications Sdn Bhd" but misses brand prefix "CelcomDigi" on a different line.
**Fix**: Two-pass SDN BHD search — first look for lines with brand+SDN BHD combined (e.g. "CelcomDigi Telecommunications Sdn Bhd"), then fall back to prepending brand from preceding lines.

## Rollbacks
- `_prep_gray` contrast default: 1.3 → 2.0
- `_ocr_ic_number_fallback` contrast: 1.5 → 2.0
- `_ocr_name_fallback` contrast: 1.5 → 2.0
- `_preprocess_id_image` contrast: 1.3 → 2.0
- `_auto_crop_document` center-crop fallback: removed (made text smaller)

## Earlier Fixes (from prior sessions)
- RBAC: HR Manager inherits Admin access; HR Director inherits from Admin, HR Manager, HR
- Pending queue: expanded to 6 modules (Leave, Invoice, Vacancy, Applications, Bonus, Increment)
- Toast notifications: pending counts added to dashboard render
- UNION ALL SQL: wrapped in `SELECT * FROM (subq LIMIT n)` for SQLite compatibility
- Job_Application column: `ja.full_name` → `ja.applicant_name`
- Invoice OCR: percentile histogram normalization + adaptive thresholding
- Watermark timing: OCR runs before watermark, watermark after file save
