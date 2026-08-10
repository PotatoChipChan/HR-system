"""
Face Registration Module
- Admin/HR captures employee faces
- Encodings stored in Face_Encoding table as encrypted BLOB (AES-256-GCM)
"""
import cv2
import numpy as np
import base64
from io import BytesIO

try:
    import face_recognition
except (ImportError, SystemExit):
    face_recognition = None
from app.crypto_utils import encrypt_face_encoding, decrypt_face_encoding, is_encrypted
from datetime import datetime
from flask import render_template, request, jsonify, session, redirect, url_for
from app.face import face_bp
from app.database import get_db
from functools import wraps
from app.notifications.routes import send_notification_to_role

# ─────────────────────────────────────────────────────────────────────────────
# DECORATORS
# ─────────────────────────────────────────────────────────────────────────────

def login_required(f):
    """Check if user is logged in"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    """Check if user has required role.
    HR Manager inherits all Admin & HR permissions.
    HR Director inherits all Admin & HR Manager & HR permissions."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('auth.login'))
            
            conn = get_db()
            user = conn.execute(
                "SELECT role_id FROM Employee WHERE employee_id = ?",
                (session['user_id'],)
            ).fetchone()
            
            if not user:
                return redirect(url_for('auth.login'))
            
            conn = get_db()
            role = conn.execute(
                "SELECT role_name FROM Role WHERE role_id = ?",
                (user['role_id'],)
            ).fetchone()
            
            role_name = role['role_name']
            check_roles = list(roles)
            if role_name == 'HR Manager' and ('Admin' in roles or 'HR' in roles):
                check_roles.append('HR Manager')
            if role_name == 'HR Director' and ('Admin' in roles or 'HR Manager' in roles or 'HR' in roles):
                check_roles.append('HR Director')
            
            if role_name not in check_roles:
                return jsonify({'success': False, 'msg': 'Access denied. Admin or HR role required.'}), 403
            
            return f(*args, **kwargs)
        return decorated
    return decorator

# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def encoding_to_blob(face_encoding):
    """Convert numpy array face encoding to BLOB (bytes)"""
    return face_encoding.astype(np.float64).tobytes()

def blob_to_encoding(blob):
    """Convert BLOB (bytes) back to numpy array face encoding"""
    return np.frombuffer(blob, dtype=np.float64)

def process_base64_image(image_data):
    """
    Decode base64 image to OpenCV format (BGR)
    
    Args:
        image_data: "data:image/png;base64,..." format string
    
    Returns:
        BGR image (numpy array) or None if decode fails
    """
    try:
        # Remove data URI prefix if present
        if ',' in image_data:
            image_data = image_data.split(',')[1]
        
        # Decode base64
        img_bytes = base64.b64decode(image_data)
        np_arr = np.frombuffer(img_bytes, np.uint8)
        
        # Decode as image
        img_bgr = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        return img_bgr
    except Exception as e:
        print(f"Error processing image: {e}")
        return None

# ─────────────────────────────────────────────────────────────────────────────
# FACE SYSTEM HEALTH MONITOR
# ─────────────────────────────────────────────────────────────────────────────

_system_healthy = True
_consecutive_errors = 0
_ERROR_THRESHOLD = 5

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@face_bp.route('/registration-list', methods=['GET'])
@login_required
@role_required('Admin', 'HR')
def registration_list():
    """Show list of all employees with face registration status"""
    conn = get_db()
    
    employees = conn.execute("""
        SELECT
            e.employee_id,
            e.full_name,
            e.email,
            e.employment_status,
            d.department_name,
            b.name AS branch_name,
            CASE WHEN fe.encoding_id IS NOT NULL THEN 1 ELSE 0 END AS face_registered,
            fe.updated_at AS face_updated_at
        FROM Employee e
        JOIN Department d ON e.department_id = d.department_id
        JOIN Branch b ON e.branch_id = b.branch_id
        LEFT JOIN Face_Encoding fe ON e.employee_id = fe.employee_id
        ORDER BY e.full_name
    """).fetchall()
    
    return render_template('face/registration_list.html', employees=employees)


@face_bp.route('/register/<int:emp_id>', methods=['GET'])
@login_required
@role_required('Admin', 'HR')
def register_face_page(emp_id):
    """Show face registration page for an employee"""
    REG_DURATION = 5  # seconds to capture
    conn = get_db()
    
    # Get employee details
    employee = conn.execute(
        "SELECT employee_id, full_name, email FROM Employee WHERE employee_id = ?",
        (emp_id,)
    ).fetchone()
    
    # Check if face already registered
    existing = conn.execute(
        "SELECT face_encoding_blob FROM Face_Encoding WHERE employee_id = ?",
        (emp_id,)
    ).fetchone()
    
    if not employee:
        return jsonify({'success': False, 'msg': 'Employee not found'}), 404
    
    return render_template('face/register_face.html', 
                          employee=employee,
                          already_registered=bool(existing),
                          REG_DURATION=REG_DURATION)

@face_bp.route('/api/register', methods=['POST'])
@login_required
@role_required('Admin', 'HR')
def api_register_face():
    """
    Capture and register a face encoding.
    
    Accepts either a single image or multiple frames for consistency verification.
    
    Expected JSON:
    {
        "employee_id": 4,
        "image": "data:image/jpeg;base64,...",           // single frame (legacy)
        "frames": ["data:image/jpeg;base64,...", ...]     // multiple frames (preferred)
    }
    """
    try:
        if face_recognition is None:
            return jsonify({'success': False, 'msg': 'Face recognition library is not installed or failed to load on the server.'}), 503

        data = request.get_json()
        emp_id = data.get('employee_id')
        image_data = data.get('image')
        frames_data = data.get('frames', [])
        
        # Normalize: if single image provided, wrap in a list
        if image_data and not frames_data:
            frames_data = [image_data]
        
        if not emp_id or not frames_data:
            return jsonify({'success': False, 'msg': 'Missing employee_id or image(s)'}), 400
        
        # Extract encodings from all frames
        frame_encodings = []
        for frame in frames_data:
            img_bgr = process_base64_image(frame)
            if img_bgr is None:
                continue
            img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            face_locations = face_recognition.face_locations(img_rgb, model='hog')
            if len(face_locations) == 1:
                encs = face_recognition.face_encodings(img_rgb, face_locations)
                if encs:
                    frame_encodings.append(encs[0])
        
        if len(frame_encodings) == 0:
            return jsonify({
                'success': False, 
                'msg': 'No valid face detected in any frame. Please ensure your face is clearly visible.'
            }), 400
        
        # Intra-frame consistency check: verify all frames contain the same face
        if len(frame_encodings) >= 2:
            reference = frame_encodings[0]
            all_consistent = True
            for i, enc in enumerate(frame_encodings[1:], 2):
                dist = face_recognition.face_distance([reference], enc)[0]
                if dist > 0.5:
                    all_consistent = False
                    print(f"[REG CONSISTENCY] Frame 1 vs Frame {i}: distance={dist:.4f} (>0.5 = different faces)")
                    break
            
            if not all_consistent:
                return jsonify({
                    'success': False,
                    'msg': 'Face inconsistency detected across frames. Please ensure only your face is visible and try again.'
                }), 400
            
            # Use the frame with highest quality (largest face = closest to camera = best quality)
            # Sort by face area to pick the best encoding
            best_encoding = frame_encodings[0]
            best_area = 0
            for frame in frames_data:
                img_bgr = process_base64_image(frame)
                if img_bgr is None:
                    continue
                img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
                locations = face_recognition.face_locations(img_rgb, model='hog')
                if len(locations) == 1:
                    top, right, bottom, left = locations[0]
                    area = (right - left) * (bottom - top)
                    encs = face_recognition.face_encodings(img_rgb, locations)
                    if encs and area > best_area:
                        best_area = area
                        best_encoding = encs[0]
            
            new_encoding = best_encoding
        else:
            # Single frame - extract as before
            img_bgr = process_base64_image(frames_data[0])
            if img_bgr is None:
                return jsonify({'success': False, 'msg': 'Failed to decode image'}), 400
            img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            face_locations = face_recognition.face_locations(img_rgb, model='hog')
            if len(face_locations) == 0:
                return jsonify({'success': False, 'msg': 'No face detected'}), 400
            if len(face_locations) > 1:
                return jsonify({'success': False, 'msg': f'Multiple faces detected ({len(face_locations)})'}), 400
            encs = face_recognition.face_encodings(img_rgb, face_locations)
            if not encs:
                return jsonify({'success': False, 'msg': 'Could not extract face encoding'}), 400
            new_encoding = encs[0]
        
        # Validate: check if this face already belongs to another employee
        conn = get_db()
        other_faces = conn.execute("""
            SELECT f.employee_id, f.face_encoding_blob, e.full_name
            FROM Face_Encoding f
            JOIN Employee e ON f.employee_id = e.employee_id
            WHERE f.employee_id != ? AND e.employment_status = 'Active'
        """, (emp_id,)).fetchall()
        
        for row in other_faces:
            try:
                stored_blob = row['face_encoding_blob']
                stored_encoding = None
                if isinstance(stored_blob, str):
                    try:
                        raw = decrypt_face_encoding(stored_blob)
                        stored_encoding = np.frombuffer(raw, dtype=np.float64)
                    except Exception:
                        try:
                            raw = base64.b64decode(stored_blob)
                            stored_encoding = np.frombuffer(raw, dtype=np.float64)
                        except Exception:
                            continue
                elif isinstance(stored_blob, (bytes, bytearray)):
                    stored_encoding = np.frombuffer(stored_blob, dtype=np.float64)
                else:
                    continue
                if stored_encoding is None or len(stored_encoding) != 128:
                    continue
                distance = face_recognition.face_distance([stored_encoding], new_encoding)[0]
                if distance < 0.5:
                    return jsonify({
                        'success': False,
                        'msg': f'This face is already registered to {row["full_name"]} (match confidence: {(1 - distance) * 100:.1f}%). Cannot register the same face to multiple employees.'
                    }), 400
            except Exception as e:
                print(f"[DEBUG face validation] Error checking emp {row['employee_id']}: {e}")
                continue
        
        encoding_blob = encoding_to_blob(new_encoding)
        encrypted_blob = encrypt_face_encoding(encoding_blob)
        
        conn = get_db()
        emp_check = conn.execute(
            "SELECT employee_id FROM Employee WHERE employee_id = ?",
            (emp_id,)
        ).fetchone()
        
        if not emp_check:
            return jsonify({'success': False, 'msg': 'Employee not found'}), 404
        
        existing = conn.execute(
            "SELECT encoding_id FROM Face_Encoding WHERE employee_id = ?",
            (emp_id,)
        ).fetchone()
        
        if existing:
            conn.execute("""
                UPDATE Face_Encoding
                SET face_encoding_blob = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    registered_by = ?
                WHERE employee_id = ?
            """, (encrypted_blob, session['user_id'], emp_id))
            msg = 'Face updated successfully'
        else:
            conn.execute("""
                INSERT INTO Face_Encoding (employee_id, face_encoding_blob, registered_by, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """, (emp_id, encrypted_blob, session['user_id']))
            msg = 'Face registered successfully'
        
        conn.commit()
        
        return jsonify({
            'success': True,
            'msg': msg,
            'frames_verified': len(frame_encodings)
        })
    
    except Exception as e:
        print(f"Face registration error: {e}")
        return jsonify({
            'success': False,
            'msg': f'Error: {str(e)}'
        }), 500


@face_bp.route('/api/analyze_frame', methods=['POST'])
@login_required
def api_analyze_frame():
    """
    Analyze a single frame for face quality metrics.
    Used during registration to provide real-time feedback.
    
    Expected JSON: { "image": "data:image/jpeg;base64,..." }
    Returns: { face_detected, centered, good_size, good_lighting, face_box, quality_score }
    """
    try:
        if face_recognition is None:
            return jsonify({'success': False, 'error': 'Face recognition not available'}), 503
        
        data = request.get_json()
        image_data = data.get('image')
        
        if not image_data:
            return jsonify({'success': False, 'error': 'No image provided'}), 400
        
        img_bgr = process_base64_image(image_data)
        if img_bgr is None:
            return jsonify({'success': False, 'error': 'Failed to decode image'}), 400
        
        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        h, w = img_rgb.shape[:2]
        
        # Detect faces
        face_locations = face_recognition.face_locations(img_rgb, model='hog')
        
        print(f"[DEBUG analyze_frame] Image: {w}x{h}, faces found: {len(face_locations)}")
        
        if len(face_locations) == 0:
            return jsonify({
                'success': True,
                'face_detected': False,
                'centered': False,
                'good_size': False,
                'good_lighting': False,
                'quality_score': 0,
                'face_box': None
            })
        
        if len(face_locations) > 1:
            # Multiple faces - use the largest one
            face_locations = [max(face_locations, key=lambda loc: (loc[2]-loc[0])*(loc[1]-loc[3]))]
        
        top, right, bottom, left = face_locations[0]
        face_w = right - left
        face_h = bottom - top
        face_area = face_w * face_h
        frame_area = w * h
        
        # Face center
        face_cx = (left + right) / 2
        face_cy = (top + bottom) / 2
        frame_cx = w / 2
        frame_cy = h / 2
        
        # Centered: face center within 20% of frame center
        center_threshold_x = w * 0.2
        center_threshold_y = h * 0.2
        centered = (abs(face_cx - frame_cx) < center_threshold_x) and (abs(face_cy - frame_cy) < center_threshold_y)
        
        # Good size: face occupies 8-60% of frame
        size_ratio = face_area / frame_area
        good_size = 0.08 <= size_ratio <= 0.60
        
        # Good lighting: analyze face region brightness and contrast
        face_region = img_rgb[top:bottom, left:right]
        if face_region.size > 0:
            # Convert to grayscale for lighting analysis
            gray = cv2.cvtColor(face_region, cv2.COLOR_RGB2GRAY)
            mean_brightness = np.mean(gray)
            std_brightness = np.std(gray)
            # Good lighting: brightness 40-220 (out of 255), contrast (std) > 15
            good_lighting = 40 <= mean_brightness <= 220 and std_brightness > 15
        else:
            good_lighting = False
        
        # Overall quality score (0-100)
        quality_score = 0
        if centered: quality_score += 25
        if good_size: quality_score += 25
        if good_lighting: quality_score += 25
        if face_area > 0: quality_score += 25  # face detected
        
        return jsonify({
            'success': True,
            'face_detected': True,
            'centered': bool(centered),
            'good_size': bool(good_size),
            'good_lighting': bool(good_lighting),
            'quality_score': int(quality_score),
            'face_box': {
                'top': int(top), 'right': int(right),
                'bottom': int(bottom), 'left': int(left)
            },
            'face_center': {'x': float(face_cx), 'y': float(face_cy)},
            'frame_center': {'x': float(frame_cx), 'y': float(frame_cy)},
            'size_ratio': round(float(size_ratio), 3),
            'brightness': round(float(mean_brightness), 1) if 'mean_brightness' in locals() else 0
        })
        
    except Exception as e:
        print(f"Frame analysis error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@face_bp.route('/api/status/<int:emp_id>', methods=['GET'])
@login_required
@role_required('Admin', 'HR')
def get_face_status(emp_id):
    """Check if employee has a registered face"""
    conn = get_db()
    
    face = conn.execute("""
        SELECT encoding_id, updated_at
        FROM Face_Encoding
        WHERE employee_id = ?
    """, (emp_id,)).fetchone()
    
    if face:
        return jsonify({
            'has_face': True,
            'updated_at': face['updated_at']
        })
    else:
        return jsonify({'has_face': False})

# ═════════════════════════════════════════════════════════════════════════════
# FACE ATTENDANCE — CHECK IN / CHECK OUT
# ═════════════════════════════════════════════════════════════════════════════

@face_bp.route('/attendance', methods=['GET'])
@login_required
def face_attendance_page():
    """Combined face attendance page - auto-detects check-in vs check-out"""
    emp_id = session.get('user_id')
    conn = get_db()
    employee = conn.execute(
        "SELECT employee_id, full_name, branch_id FROM Employee WHERE employee_id = ?",
        (emp_id,)
    ).fetchone()
    face_registered = conn.execute(
        "SELECT encoding_id FROM Face_Encoding WHERE employee_id = ?",
        (emp_id,)
    ).fetchone()

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today_record = conn.execute("""
        SELECT check_in, check_out
        FROM Attendance
        WHERE employee_id = ? AND date(check_in) = date(?)
        ORDER BY check_in DESC LIMIT 1
    """, (emp_id, now_str)).fetchone()

    is_checked_in = today_record is not None and today_record['check_out'] is None
    last_check_in = today_record['check_in'] if today_record else None

    if not employee:
        return redirect(url_for('auth.login'))
    if not face_registered:
        return render_template('face/no_face_registered.html', employee=employee)

    manual_mode = request.args.get('manual') == '1' or session.get('biometric_checkin_failures', 0) >= 3
    failed_attempts = session.get('biometric_checkin_failures', 0)
    if not manual_mode and failed_attempts > 0 and failed_attempts < 3:
        pass

    return render_template('face/face_action.html',
                           employee=employee,
                           is_checked_in=is_checked_in,
                           last_check_in=last_check_in,
                           manual_mode=manual_mode,
                           failed_attempts=failed_attempts)

def _render_face_action_page(action):
    emp_id = session.get('user_id')
    conn = get_db()
    employee = conn.execute(
        "SELECT employee_id, full_name, branch_id FROM Employee WHERE employee_id = ?",
        (emp_id,)
    ).fetchone()
    face_registered = conn.execute(
        "SELECT encoding_id FROM Face_Encoding WHERE employee_id = ?",
        (emp_id,)
    ).fetchone()

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today_record = conn.execute("""
        SELECT check_in, check_out
        FROM Attendance
        WHERE employee_id = ? AND date(check_in) = date(?)
        ORDER BY check_in DESC LIMIT 1
    """, (emp_id, now_str)).fetchone()

    is_checked_in = today_record is not None and today_record['check_out'] is None
    last_check_in = today_record['check_in'] if today_record else None

    if not employee:
        return redirect(url_for('auth.login'))
    if not face_registered:
        return render_template('face/no_face_registered.html', employee=employee)
    return render_template('face/face_action.html',
                           employee=employee, action=action,
                           is_checked_in=is_checked_in,
                           last_check_in=last_check_in)

@face_bp.route('/api/health', methods=['GET'])
@login_required
def api_health():
    global _system_healthy, _consecutive_errors, _ERROR_THRESHOLD
    fr_ok = face_recognition is not None
    db_ok = False
    try:
        c = get_db()
        c.execute("SELECT 1").fetchone()
        c.close()
        db_ok = True
    except Exception:
        pass
    all_ok = fr_ok and db_ok and _consecutive_errors < _ERROR_THRESHOLD
    if not all_ok and _system_healthy:
        _system_healthy = False
        _consecutive_errors += 1
        if _consecutive_errors >= _ERROR_THRESHOLD:
            send_notification_to_role(
                ['Admin', 'HR Director', 'HR Manager', 'HR'],
                'Face Recognition System Error',
                f'Health check failed. face_recognition: {fr_ok}, DB: {db_ok}, consecutive errors: {_consecutive_errors}',
                type='Error'
            )
    elif all_ok and not _system_healthy:
        _system_healthy = True
        _consecutive_errors = 0
        send_notification_to_role(
            ['Admin', 'HR Director', 'HR Manager', 'HR'],
            'Face Recognition System Restored',
            'The system is healthy again.',
            type='Success'
        )
    return jsonify({
        'status': 'healthy' if all_ok else 'unhealthy',
        'face_recognition': fr_ok,
        'database': db_ok,
        'consecutive_errors': _consecutive_errors,
        'threshold': _ERROR_THRESHOLD
    })

def _increment_failures():
    count = session.get('biometric_checkin_failures', 0) + 1
    session['biometric_checkin_failures'] = count
    return count

def _reset_failures():
    session['biometric_checkin_failures'] = 0


@face_bp.route('/api/match_and_record', methods=['POST'])
@login_required
def api_match_and_record():
    global _consecutive_errors, _system_healthy
    from app.face.matcher import match_face, extract_face_encoding, refresh_face_cache
    
    try:
        data = request.get_json()
        image_data = data.get('image')
        requested_action = data.get('action', '')
        
        if not image_data:
            return jsonify({'success': False, 'msg': 'No image provided'}), 400
        
        if requested_action not in ('check_in', 'check_out'):
            return jsonify({'success': False, 'msg': 'Invalid action'}), 400
        
        emp_id = session.get('user_id')
        
        conn = get_db()
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        today_record = conn.execute("""
            SELECT attendance_id, check_in, check_out
            FROM Attendance
            WHERE employee_id = ? AND date(check_in) = date(?)
            ORDER BY check_in DESC LIMIT 1
        """, (emp_id, now_str)).fetchone()
        
        is_checked_in = today_record is not None and today_record['check_out'] is None
        
        # For check-out, also look for an open check-in from yesterday (cross-midnight)
        if requested_action == 'check_out' and not is_checked_in:
            cross_record = conn.execute("""
                SELECT attendance_id, check_in, check_out
                FROM Attendance
                WHERE employee_id = ? AND date(check_in) = date(?, '-1 day') AND check_out IS NULL
                ORDER BY check_in DESC LIMIT 1
            """, (emp_id, now_str)).fetchone()
            if cross_record:
                is_checked_in = True
                today_record = cross_record
        
        if requested_action == 'check_in' and is_checked_in:
            return jsonify({
                'success': False,
                'msg': '⚠️ You are already checked in today. Please check out first before checking in again.'
            }), 400
        
        if requested_action == 'check_out' and not is_checked_in:
            return jsonify({
                'success': False,
                'msg': '⚠️ You have not checked in today yet. Please check in first.'
            }), 400
        
        # Decode image to RGB
        img_bgr = process_base64_image(image_data)
        if img_bgr is None:
            return jsonify({'success': False, 'msg': 'Failed to decode image'}), 400
        
        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        
        # Extract face encoding
        face_encoding, face_count = extract_face_encoding(img_rgb)
        
        if face_encoding is None:
            count = _increment_failures()
            exceeded = count >= 3
            payload = {'success': False, 'failed_attempts': count, 'manual_allowed': exceeded}
            if exceeded:
                payload['redirect_url'] = url_for('face.face_attendance_page', manual=1)
            if face_count == 0:
                payload['msg'] = 'No face detected. Please position your face in the camera.'
                return jsonify(payload), 400
            elif face_count > 1:
                payload['msg'] = f'Multiple faces detected ({face_count}). Only you should be in frame.'
                return jsonify(payload), 400
        
        # Refresh cache if first time
        refresh_face_cache()
        
        # Match against registered faces
        match_result = match_face(face_encoding, tolerance=0.4)  # Strict matching
        
        if not match_result['matched']:
            count = _increment_failures()
            exceeded = count >= 3
            # Face not recognized - offer manual entry
            payload = {
                'success': False,
                'msg': match_result.get('error', 'Face not recognized'),
                'confidence': match_result.get('confidence', 0),
                'failed_attempts': count,
                'manual_allowed': exceeded,
            }
            if exceeded:
                payload['redirect_url'] = url_for('face.face_attendance_page', manual=1)
            return jsonify(payload), 401
        
        # Verify matched employee is logged-in employee (security)
        if match_result['employee_id'] != emp_id:
            count = _increment_failures()
            exceeded = count >= 3
            payload = {
                'success': False,
                'msg': 'Face does not match logged-in employee',
                'failed_attempts': count,
                'manual_allowed': exceeded,
            }
            if exceeded:
                payload['redirect_url'] = url_for('face.face_attendance_page', manual=1)
            return jsonify(payload), 403
        
        # Record attendance with confidence
        _reset_failures()
        confidence = match_result.get('confidence', 0)
        attendance_record = record_attendance(emp_id, confidence, requested_action)
        
        if not attendance_record:
            _consecutive_errors += 1
            if _consecutive_errors >= _ERROR_THRESHOLD and _system_healthy:
                _system_healthy = False
                send_notification_to_role(
                    ['Admin', 'HR Director', 'HR Manager', 'HR'],
                    'Face Recognition System Error',
                    f'Failed to record attendance after face match. consecutive errors: {_consecutive_errors}',
                    type='Error'
                )
            return jsonify({'success': False, 'msg': 'Failed to record attendance'}), 500
        
        _consecutive_errors = 0
        if not _system_healthy:
            _system_healthy = True
            send_notification_to_role(
                ['Admin', 'HR Director', 'HR Manager', 'HR'],
                'Face Recognition System Restored',
                'Attendance recorded successfully after recovery.',
                type='Success'
            )
        
        return jsonify({
            'success': True,
            'msg': attendance_record['msg'],
            'attendance': attendance_record,
            'confidence': confidence
        })
    
    except Exception as e:
        print(f"Match and record error: {e}")
        _consecutive_errors += 1
        if _consecutive_errors >= _ERROR_THRESHOLD and _system_healthy:
            _system_healthy = False
            send_notification_to_role(
                ['Admin', 'HR Director', 'HR Manager', 'HR'],
                'Face Recognition System Error',
                f'Exception in match_and_record: {str(e)}',
                type='Error'
            )
        return jsonify({
            'success': False,
            'msg': f'Error: {str(e)}'
        }), 500

def record_attendance(emp_id, confidence_score=0, action='check_in'):
    """Record face-verified check-in or check-out with confidence score."""
    try:
        now = datetime.now()
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')
        conn = get_db()
        
        last_record = conn.execute("""
            SELECT attendance_id, check_in, check_out
            FROM Attendance
            WHERE employee_id = ? AND date(check_in) = date(?)
            ORDER BY check_in DESC LIMIT 1
        """, (emp_id, now_str)).fetchone()
        
        if action == 'check_out' and (not last_record or last_record['check_out'] is not None):
            cross_record = conn.execute("""
                SELECT attendance_id, check_in, check_out
                FROM Attendance
                WHERE employee_id = ? AND date(check_in) = date(?, '-1 day') AND check_out IS NULL
                ORDER BY check_in DESC LIMIT 1
            """, (emp_id, now_str)).fetchone()
            if cross_record:
                last_record = cross_record
        
        if action == 'check_in':
            if last_record and last_record['check_out'] is None:
                return None
            conn.execute("""
                INSERT INTO Attendance 
                (employee_id, branch_id, check_in, confidence_score, status, is_manual_entry)
                VALUES (?, (SELECT branch_id FROM Employee WHERE employee_id = ?), ?, ?, 'Approved', 0)
            """, (emp_id, emp_id, now_str, confidence_score))
            conn.commit()
            return {
                'action': 'check_in',
                'time': now.strftime('%H:%M:%S'),
                'msg': f'✅ Checked in at {now.strftime("%H:%M:%S")} — Confidence: {confidence_score*100:.1f}%'
            }
        else:
            if not last_record or last_record['check_out'] is not None:
                return None
            check_in_dt = datetime.fromisoformat(last_record['check_in'])
            hours_worked = round((now - check_in_dt).total_seconds() / 3600, 2)
            emp_sched = conn.execute(
                "SELECT work_start_time, work_end_time FROM Employee WHERE employee_id = ?",
                (emp_id,)
            ).fetchone()
            s_start = emp_sched['work_start_time'] or '09:00'
            s_end = emp_sched['work_end_time'] or '18:00'
            sh, sm = map(int, s_start.split(':'))
            eh, em = map(int, s_end.split(':'))
            sched_hours = (eh + em/60) - (sh + sm/60)
            ot = round(max(0, hours_worked - sched_hours), 2)
            
            conn.execute("""
                UPDATE Attendance
                SET check_out = ?, hours_worked = ?, overtime_hours = ?,
                    confidence_score = ?, status = 'Approved'
                WHERE attendance_id = ?
            """, (now_str, hours_worked, ot, confidence_score, last_record['attendance_id']))
            conn.commit()
            return {
                'action': 'check_out',
                'time': now.strftime('%H:%M:%S'),
                'hours_worked': hours_worked,
                'overtime_hours': ot,
                'msg': f'✅ Checked out at {now.strftime("%H:%M:%S")} ({hours_worked}h) — Confidence: {confidence_score*100:.1f}%'
            }
    except Exception as e:
        import traceback
        print(f"Error recording attendance: {e}")
        traceback.print_exc()
        return None

@face_bp.route('/api/record_manual', methods=['POST'])
@login_required
def api_record_manual():
    """
    Manual attendance entry (fallback if face recognition fails)
    
    Expected JSON:
    {
        "check_in_time": "09:00",  // HH:MM format
        "check_out_time": "18:00", // HH:MM format (optional)
        "reason": "Face recognition failed"
    }
    """
    try:
        data = request.get_json()
        check_in_time = data.get('check_in_time')
        check_out_time = data.get('check_out_time')
        reason = data.get('reason', '')
        
        if not check_in_time:
            return jsonify({'success': False, 'msg': 'Check-in time required'}), 400
        
        emp_id = session.get('user_id')
        now = datetime.now()
        
        try:
            # Parse times
            check_in_parts = check_in_time.split(':')
            check_in_dt = now.replace(hour=int(check_in_parts[0]), minute=int(check_in_parts[1]), second=0)
            
            conn = get_db()
            
            # Check if manual entry already exists for today
            existing = conn.execute("""
                SELECT attendance_id FROM Attendance
                WHERE employee_id = ? AND date(check_in) = date(?)
                AND is_manual_entry = 1
            """, (emp_id, now)).fetchone()
            
            if existing:
                return jsonify({'success': False, 'msg': 'Manual entry already exists for today'}), 400
            
            # Insert manual check-in
            conn.execute("""
                INSERT INTO Attendance
                (employee_id, branch_id, check_in, status, is_manual_entry)
                VALUES (?, (SELECT branch_id FROM Employee WHERE employee_id = ?), ?, 'Pending', 1)
            """, (emp_id, emp_id, check_in_dt))
            
            # If check-out provided, add hours
            if check_out_time:
                check_out_parts = check_out_time.split(':')
                check_out_dt = now.replace(hour=int(check_out_parts[0]), minute=int(check_out_parts[1]), second=0)
                hours_worked = (check_out_dt - check_in_dt).total_seconds() / 3600
                
                # Update the attendance record
                conn.execute("""
                    UPDATE Attendance
                    SET check_out = ?, hours_worked = ?
                    WHERE employee_id = ? AND date(check_in) = date(?)
                    AND is_manual_entry = 1
                """, (check_out_dt, round(hours_worked, 2), emp_id, now))
            
            conn.commit()
            
            return jsonify({
                'success': True,
                'msg': 'Manual entry recorded (pending HR approval)'
            })
        
        except ValueError:
            return jsonify({'success': False, 'msg': 'Invalid time format. Use HH:MM'}), 400
    
    except Exception as e:
        print(f"Manual entry error: {e}")
        return jsonify({'success': False, 'msg': f'Error: {str(e)}'}), 500

@face_bp.route('/api/today_attendance', methods=['GET'])
@login_required
def get_today_attendance():
    """Get today's attendance records for logged-in employee"""
    try:
        emp_id = session.get('user_id')
        now = datetime.now()
        today = now.date()
        
        conn = get_db()
        
        records = conn.execute("""
            SELECT check_in, check_out, hours_worked, status, is_manual_entry
            FROM Attendance
            WHERE employee_id = ? AND (date(check_in) = ? OR (date(check_in) = date(?, '-1 day') AND check_out IS NULL))
            ORDER BY check_in ASC
        """, (emp_id, today, today)).fetchall()
        
        attendance_list = []
        for rec in records:
            check_in = datetime.fromisoformat(rec['check_in'])
            check_out = None
            if rec['check_out']:
                check_out = datetime.fromisoformat(rec['check_out'])
            
            attendance_list.append({
                'check_in': check_in.strftime('%H:%M:%S'),
                'check_out': check_out.strftime('%H:%M:%S') if check_out else 'Still logged in',
                'hours_worked': rec['hours_worked'],
                'status': rec['status'],
                'manual': rec['is_manual_entry'] == 1
            })
        
        return jsonify({
            'success': True,
            'date': today.isoformat(),
            'records': attendance_list
        })
    
    except Exception as e:
        print(f"Get attendance error: {e}")
        return jsonify({
            'success': False,
            'msg': f'Error: {str(e)}'
        }), 500


@face_bp.route('/api/report_failure', methods=['POST'])
@login_required
def api_report_failure():
    """Lightweight endpoint to increment biometric failure counter from client-side."""
    action = request.json.get('action', 'check_in') if request.is_json else 'check_in'
    count = _increment_failures()
    exceeded = count >= 3
    payload = {'failed_attempts': count, 'manual_allowed': exceeded}
    if exceeded:
        payload['redirect_url'] = url_for('face.face_attendance_page', manual=1)
    return jsonify(payload)
