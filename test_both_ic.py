"""Test full OCR pipeline on IC test images."""
import sys, re; sys.path.insert(0, '.')
from PIL import Image
from app.employees.routes import (
    _ocr_mykad_front, _extract_id_info, _run_easyocr,
    _extract_address, _extract_malaysian_name,
    _clean_malaysian_address,
    _ocr_address_fallback
)

def _addr_quality(addr):
    """Score an address string: higher is better."""
    if not addr:
        return (-1, -1, 0)
    has_pc = bool(re.search(r'\b\d{5}\b', addr))
    stripped = re.sub(r'[^A-Za-z\s]', ' ', addr)
    alpha_cnt = sum(1 for w in stripped.split() if w.isalpha() and len(w) >= 3)
    addr_markers = re.findall(
        r'\b(JALAN|LORONG|TAMAN|PERSIARAN|LEBUH|NO|L/P|APARTMENT|BLOCK)\b',
        addr, re.I)
    alpha_cnt += len(addr_markers) * 2
    return (has_pc, alpha_cnt, len(addr))

test_cases = [
    ('test_IC/KarShengIC_Front.jpg', {
        'ic': '050112-14-0311',
        'name': 'YAP KAR SHENG',
        'address_parts': ['NO 39', 'JALAN BIDARA', '46', 'KEPONG BARU', '52100', 'KUALA LUMPUR']
    }),
    ('test_IC/IC front good orientation.jpg', {
        'ic': '040630-10-1049',
        'name': 'CHAN HAN YUE',
        'address_parts': ['NO 67', 'JALAN 1/37B', 'TAMAN BUKIT MALURI', 'KEPONG', '52100', 'KUALA LUMPUR']
    }),
    ('test_IC/India_front.jpeg', {
        'ic': '070707-10-2515',
        'name': 'SARVIEN AL RAMOO',
        'address_parts': ['40B', 'JALAN', 'PERMAI', '68100']
    }),
]

all_ok = True
for path, expected in test_cases:
    print(f'=== {path} ===')
    img = Image.open(path).convert('RGB')
    raw_text, ocr_score, corrected = _ocr_mykad_front(img)
    extracted = _extract_id_info(raw_text, side='front', doc_type='ic')
    print(f'  Tesseract IC: {extracted.get("ic_number", "")}')
    print(f'  Tesseract Name: {extracted.get("full_name", "")}')
    print(f'  Tesseract Addr: {extracted.get("address", "")}')

    # Pipeline: EasyOCR on original
    easyocr_text = _run_easyocr(corrected)

    # Pipeline: EasyOCR on FFT-guilloche-removed image (separately for best address)
    addr_easy = None
    addr_fft = None
    try:
        from app.employees.guilloche_removal import remove_guilloche
        guilloche_img = remove_guilloche(corrected)
        guilloche_easyocr = _run_easyocr(guilloche_img)
        if guilloche_easyocr:
            if easyocr_text:
                easyocr_text = easyocr_text + '\n' + guilloche_easyocr
            else:
                easyocr_text = guilloche_easyocr
            print(f'  FFT+EasyOCR added')
            # Extract FFT address separately
            addr_fft = _extract_address(guilloche_easyocr, full_name=extracted.get('full_name', ''))
            if not addr_fft:
                addr_fft = _extract_address(guilloche_easyocr)
    except Exception as e:
        print(f'  FFT skip: {e}')

    # Pipeline: EasyOCR name override
    lines = [l.strip() for l in easyocr_text.split('\n') if l.strip()]
    name_easy = _extract_malaysian_name(lines)
    if name_easy and name_easy.upper() != extracted.get('full_name', '').upper():
        extracted['full_name'] = name_easy.title()
        print(f'  Override Name: {extracted["full_name"]}')

    # Pipeline: EasyOCR address from combined text
    addr_easy = _extract_address(easyocr_text, full_name=name_easy or '')
    if not addr_easy:
        addr_easy = _extract_address(easyocr_text)
    print(f'  addr_easy raw: {addr_easy!r}')

    # Pipeline: Tesseract address fallback (guilloche-specific preprocessing)
    addr_tess_fb = None
    try:
        tess_fb_text = _ocr_address_fallback(corrected)
        if tess_fb_text:
            addr_tess_fb = _extract_address(tess_fb_text, full_name=extracted.get('full_name', ''))
            if not addr_tess_fb:
                addr_tess_fb = _extract_address(tess_fb_text)
            if addr_tess_fb:
                print(f'  addr_tess_fb: {addr_tess_fb!r}')
    except Exception as e:
        print(f'  Tesseract addr fallback skip: {e}')

    # Compare all address sources using quality scoring
    current = extracted.get('address', '')
    candidates = []
    for addr, label in [(current, 'tess'), (addr_easy, 'easy'), (addr_fft, 'fft'), (addr_tess_fb, 'tess_fb')]:
        if addr:
            has_pc, alpha_cnt, length = _addr_quality(addr)
            candidates.append((addr, has_pc, -alpha_cnt, length, label))
            print(f'    {label}: {addr!r} -> quality {has_pc}, {-alpha_cnt}, {length}')

    def _best_postcode_city(*addrs):
        best = ''
        for a in addrs:
            if not a: continue
            norm = re.sub(r'\s+', ' ', a.upper().replace('.', ' '))
            m = re.search(r'\b(\d{5})\s+([A-Z][A-Z\s]+?)(?:\s*,|\s*$)', norm)
            if m:
                chunk = re.sub(r'\s+', ' ', m.group(0).rstrip(',').strip())
                if len(chunk) > len(best):
                    best = chunk
        return best

    def _merge_address(addr_best, *others):
        if not addr_best: return addr_best
        has_jalan = bool(re.search(r'\b(JALAN|LORONG)\b', addr_best, re.I))
        pc_match = re.search(r'\b(\d{5})\b', addr_best)
        if not has_jalan or not pc_match: return addr_best
        postcode = pc_match.group(1)
        pc_inline = re.search(r'\b' + postcode + r'\s+[A-Z]{3,}', addr_best.upper())
        if pc_inline: return addr_best
        borrowed = _best_postcode_city(*others)
        if borrowed and postcode in borrowed:
            state_m = re.search(r',?\s*\b(SELANGOR|KUALA LUMPUR|PENANG|JOHOR|PERAK|KEDAH|'
                                r'NEGERI SEMBILAN|PAHANG|MELAKA|TERENGGANU|SABAH|SARAWAK)\b.*$',
                                addr_best, re.I)
            if state_m:
                state = state_m.group(1)
                before = addr_best[:state_m.start()].rstrip(', ')
            else:
                state = ''
                before = addr_best.rstrip(', ')
            before = re.sub(r',\s*$', '', before).strip()
            before_parts = [p.strip() for p in before.split(',')]
            if before_parts:
                last_part = before_parts[-1].strip()
                last_words = last_part.split()
                if (len(last_words) == 1 and last_words[0].isalpha()
                        and len(last_words[0]) >= 3
                        and last_words[0].upper() not in borrowed.upper()):
                    extended = borrowed + ' ' + last_words[0]
                    for o in others:
                        if o and extended.upper() in re.sub(r'\s+', ' ', o.upper().replace('.', ' ')):
                            borrowed = extended
                            before_parts = before_parts[:-1]
                            break
            before = ', '.join(before_parts)
            parts = [before, borrowed] if before else [borrowed]
            if state and state not in borrowed:
                parts.append(state)
            return ', '.join(p for p in parts if p)
        return addr_best

    if candidates:
        candidates.sort(key=lambda x: (not x[1], x[2], x[3]))
        best = candidates[0][0]
        best = _merge_address(best, current, addr_easy, addr_fft)
        if best != current:
            extracted['address'] = best
            print(f'  Override Addr: {best}')

    # Verify
    ic_ok = expected['ic'] in extracted.get('ic_number', '')
    name_ok = expected['name'] in extracted.get('full_name', '').upper()
    addr_ok = all(p in extracted.get('address', '').upper() for p in expected['address_parts'])

    print(f'  Final IC: {extracted.get("ic_number", "")} {"OK" if ic_ok else "FAIL"}')
    print(f'  Final Name: {extracted.get("full_name", "")} {"OK" if name_ok else "FAIL"}')
    print(f'  Final Addr: {extracted.get("address", "")} {"OK" if addr_ok else "FAIL"}')
    all_ok = all_ok and ic_ok and name_ok and addr_ok
    print()

print(f'{"ALL PASS" if all_ok else "SOME FAILED"}')
