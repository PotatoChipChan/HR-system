"""app/attendance/routes.py – Manual entry, biometric verification, and attendance logs"""
import datetime
import csv
import io
import base64
import numpy as np
from PIL import Image
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, jsonify, make_response)
from app.database import query, execute, log_audit
from app.auth.routes import login_required, role_required
from app.crypto_utils import decrypt_face_encoding, is_encrypted
from app.notifications.routes import send_notification_to_role

att_bp = Blueprint('attendance', __name__, url_prefix='/attendance')

VERIFY_THRESHOLD = 65


def _decode_image(image_b64):
    """Decode a base64 data-URL image into an RGB numpy array."""
    if ',' in image_b64:
        image_b64 = image_b64.split(',', 1)[1]
    img_bytes = base64.b64decode(image_b64)
    img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
    return np.array(img)


def _get_stored_encoding(employee_id):
    row = query("SELECT face_encoding_blob FROM Face_Encoding WHERE employee_id=?", (employee_id,), one=True)
    if not row or not row['face_encoding_blob']:
        return None
    blob = row['face_encoding_blob']
    try:
        if is_encrypted(blob):
            blob = decrypt_face_encoding(blob)
        return np.frombuffer(blob, dtype=np.float64).copy()
    except Exception as e:
        print(f"Error decrypting face encoding for employee {employee_id}: {e}")
        return None


def _verify_face_for_user(img_np, employee_id):
    """Verify webcam face against the logged-in employee only."""
    try:
        import face_recognition
    except (ImportError, SystemExit):
        return {'matched': False, 'confidence': 0, 'error': 'Face recognition module is not properly installed on the server.'}

    stored = _get_stored_encoding(employee_id)
    if stored is None:
        return {'matched': False, 'confidence': 0, 'error': 'No face detected. Please position your face in the frame.', 'no_face': True}

    try:
        face_encodings = face_recognition.face_encodings(img_np)
    except Exception:
        return {'matched': False, 'confidence': 0, 'error': 'No face detected. Please position your face in the frame.', 'no_face': True}

    if not face_encodings:
        return {'matched': False, 'confidence': 0, 'error': 'No face detected. Please position your face in the frame.', 'no_face': True}

    live_encoding = face_encodings[0]
    matches = face_recognition.face_distance([stored], live_encoding)
    dist = matches[0]
    confidence = max(0, min(100, round((1 - dist) * 100, 1)))

    if dist < 0.4:
        return {'matched': True, 'confidence': confidence, 'error': None}
    else:
        others = query("""
            SELECT f.employee_id, e.full_name, f.face_encoding_blob
            FROM Face_Encoding f
            JOIN Employee e ON f.employee_id = e.employee_id
            WHERE f.employee_id != ? AND e.is_active = 1
        """, (employee_id,))
        for other in others:
            try:
                ob = other['face_encoding_blob']
                if is_encrypted(ob):
                    ob = decrypt_face_encoding(ob)
                other_enc = np.frombuffer(ob, dtype=np.float64).copy()
                other_dist = face_recognition.face_distance([other_enc], live_encoding)[0]
                if other_dist < 0.4:
                    return {
                        'matched': False, 'confidence': confidence, 'error': f'This face belongs to {other["full_name"]}. You can only check in with your own registered face.',
                        'wrong_person': True, 'wrong_person_id': other['employee_id'], 'wrong_person_name': other['full_name']
                    }
            except Exception as e:
                print(f"Error decrypting face for employee {other['employee_id']}: {e}")
                continue
        return {'matched': False, 'confidence': confidence, 'error': f'Face does not match your registered profile (confidence: {confidence}%). Only your own face can be used for attendance.'}


def _today_open_checkin(employee_id):
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    return query("""
        SELECT * FROM Attendance
        WHERE employee_id=? AND date(check_in)=? AND check_out IS NULL
        ORDER BY check_in DESC LIMIT 1
    """, (employee_id, today), one=True)


def _increment_checkin_failures():
    count = session.get('biometric_checkin_failures', 0) + 1
    session['biometric_checkin_failures'] = count
    return count


def _reset_checkin_failures():
    session['biometric_checkin_failures'] = 0
    return 0


def _failure_payload(action, count):
    """Extra JSON fields when biometric check-in fails."""
    if action != 'check_in':
        return {}
    return {
        'failed_attempts': count,
        'max_attempts': 3,
        'show_manual_fallback': count >= 3,
    }


def _has_checkin_today(employee_id):
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    return query("""
        SELECT attendance_id FROM Attendance
        WHERE employee_id=? AND date(check_in)=?
        ORDER BY check_in DESC LIMIT 1
    """, (employee_id, today), one=True)


@att_bp.route('/')
@login_required
def time_tracking():
    uid = session['user_id']
    role = session['user_role']
    co = session['company_id']
    bid = session.get('branch_id')

    recent = query("""
        SELECT a.*, b.name as branch_name FROM Attendance a
        JOIN Branch b ON a.branch_id=b.branch_id
        WHERE a.employee_id=?
        ORDER BY a.check_in DESC LIMIT 14
    """, (uid,))

    if role == 'Manager':
        employees = query("""
            SELECT e.full_name, e.position, d.department_name,
                   SUM(CASE WHEN date(a.check_in)=date('now') THEN 1 ELSE 0 END) as present_today,
                   SUM(a.hours_worked) as total_hours,
                   SUM(a.overtime_hours) as total_ot,
                   MAX(a.status) as status
            FROM Employee e
            LEFT JOIN Attendance a ON e.employee_id=a.employee_id
                AND date(a.check_in) >= date('now','-6 days')
            JOIN Department d ON e.department_id=d.department_id
            WHERE e.company_id=? AND e.branch_id=? AND e.is_active=1
            GROUP BY e.employee_id
            ORDER BY e.full_name
        """, (co, bid))
    elif role in ('Admin', 'HR Director', 'HR Manager', 'HR'):
        employees = query("""
            SELECT e.full_name, e.position, d.department_name,
                   SUM(CASE WHEN date(a.check_in)=date('now') THEN 1 ELSE 0 END) as present_today,
                   SUM(a.hours_worked) as total_hours,
                   SUM(a.overtime_hours) as total_ot,
                   MAX(a.status) as status
            FROM Employee e
            LEFT JOIN Attendance a ON e.employee_id=a.employee_id
                AND date(a.check_in) >= date('now','-6 days')
            JOIN Department d ON e.department_id=d.department_id
            WHERE e.company_id=? AND e.is_active=1
            GROUP BY e.employee_id
            ORDER BY e.full_name
        """, (co,))
    else:
        employees = []

    stats = query("""
        SELECT SUM(hours_worked) as hrs, SUM(overtime_hours) as ot
        FROM Attendance
        WHERE employee_id=? AND date(check_in) >= date('now','-6 days')
    """, (uid,), one=True)

    return render_template('attendance/time_tracking.html',
                           recent=recent, employees=employees, stats=stats)


@att_bp.route('/manual', methods=['GET', 'POST'])
@role_required('Admin', 'HR')
def manual():
    co = session['company_id']
    uid = session['user_id']
    branches  = query("SELECT * FROM Branch WHERE company_id=?", (co,))

    if request.method == 'POST':
        f         = request.form
        emp_id    = uid
        att_date  = f['att_date']
        att_time  = f['att_time']
        att_type  = f['att_type']
        reason    = f.get('reason', '').strip()
        branch_id = int(f.get('branch_id', session['branch_id']))

        if not reason:
            flash('Override reason is mandatory. Please select a reason.', 'danger')
            return redirect(url_for('attendance.manual'))

        if att_type == 'Check In' and _today_open_checkin(emp_id):
            flash('You already have an open check-in today. Check out first or use a different date.', 'danger')
            return redirect(url_for('attendance.manual'))

        dt_str = f"{att_date} {att_time}:00"

        if att_type == 'Check In':
            aid = execute("""
                INSERT INTO Attendance
                (employee_id, branch_id, check_in, is_manual_entry, manual_reason,
                 corrected_by, corrected_at, status)
                VALUES(?,?,?,1,?,?,datetime('now'),'Pending')
            """, (emp_id, branch_id, dt_str, reason, uid))
            log_audit('MANUAL_CHECKIN', 'Attendance',
                      f'Manual check-in for employee_id={emp_id}',
                      'Attendance', aid, 'Success', {'datetime': dt_str, 'reason': reason})
            flash('Manual check-in recorded (pending approval).', 'success')

        elif att_type == 'Check Out':
            open_att = query("""
                SELECT * FROM Attendance
                WHERE employee_id=? AND date(check_in)=? AND check_out IS NULL
                ORDER BY check_in DESC LIMIT 1
            """, (emp_id, att_date), one=True)

            if not open_att:
                flash('No open check-in found for this employee on that date.', 'danger')
                return redirect(url_for('attendance.manual'))

            ci = datetime.datetime.fromisoformat(open_att['check_in'])
            co_dt = datetime.datetime.fromisoformat(dt_str)
            diff_h = round((co_dt - ci).total_seconds() / 3600, 2)
            emp_sched = query("SELECT work_start_time, work_end_time FROM Employee WHERE employee_id=?", (emp_id,), one=True)
            s_start = emp_sched['work_start_time'] or '09:00'
            s_end = emp_sched['work_end_time'] or '18:00'
            sh, sm = map(int, s_start.split(':'))
            eh, em = map(int, s_end.split(':'))
            sched_hours = (eh + em/60) - (sh + sm/60)
            ot = round(max(0, diff_h - sched_hours), 2)

            execute("""
                UPDATE Attendance SET check_out=?, hours_worked=?, overtime_hours=?,
                       corrected_by=?, corrected_at=datetime('now'), is_manual_entry=1,
                       manual_reason=?, status='Pending'
                WHERE attendance_id=?
            """, (dt_str, diff_h, ot, uid, reason, open_att['attendance_id']))
            log_audit('MANUAL_CHECKOUT', 'Attendance',
                      f'Manual check-out for employee_id={emp_id}',
                      'Attendance', open_att['attendance_id'], 'Success',
                      {'datetime': dt_str, 'hours': diff_h})
            flash(f'Manual check-out recorded (pending approval). Hours: {diff_h}h.', 'success')

        return redirect(url_for('attendance.manual'))

    recent = query("""
        SELECT a.*, e.full_name, b.name as branch_name
        FROM Attendance a
        JOIN Employee e ON a.employee_id=e.employee_id
        JOIN Branch b ON a.branch_id=b.branch_id
        WHERE e.company_id=? AND a.is_manual_entry=1
        ORDER BY a.created_at DESC LIMIT 20
    """, (co,))

    open_checkin = _today_open_checkin(uid)
    return render_template('attendance/manual.html',
                           branches=branches, recent=recent,
                           is_checked_in=open_checkin is not None,
                           failed_attempts=session.get('biometric_checkin_failures', 0))


@att_bp.route('/biometric')
@login_required
def biometric():
    """Page for face recognition."""
    uid = session['user_id']
    role = session['user_role']

    face_registered = query("SELECT encoding_id FROM Face_Encoding WHERE employee_id=?", (uid,), one=True)
    open_att = _today_open_checkin(uid)

    if role in ('Admin', 'HR Director', 'HR Manager', 'HR'):
        employees = query("SELECT employee_id, full_name FROM Employee WHERE company_id=? AND is_active=1 ORDER BY full_name", (session['company_id'],))
        branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? ORDER BY name", (session['company_id'],))
    else:
        employees = []
        branches = []

    return render_template('attendance/biometric.html',
                           face_registered=face_registered is not None,
                           is_checked_in=open_att is not None,
                           employees=employees, branches=branches,
                           failed_attempts=session.get('biometric_checkin_failures', 0))


@att_bp.route('/status')
@login_required
def checkin_status():
    """Return current check-in/out eligibility without face verification."""
    uid = session['user_id']
    open_att = _today_open_checkin(uid)
    return jsonify({
        'checked_in': open_att is not None,
        'check_in_time': open_att['check_in'] if open_att else None,
    })


@att_bp.route('/register_face', methods=['POST'])
@login_required
def register_face():
    if session.get('user_role') not in ('Admin', 'HR Director', 'HR Manager', 'HR'):
        return jsonify({'error': 'Only Admin or HR can register faces.'}), 403

    data = request.get_json()
    if not data or 'image' not in data:
        return jsonify({'error': 'No image data'}), 400

    try:
        img_np = _decode_image(data['image'])
    except Exception:
        return jsonify({'error': 'No face detected. Please try again.'}), 400

    employee_id = int(data.get('employee_id', session['user_id']))
    uid = session['user_id']

    try:
        import face_recognition
        face_encodings = face_recognition.face_encodings(img_np)
        if not face_encodings:
            return jsonify({'error': 'No face detected. Please try again.'}), 400
        encoding_bytes = face_encodings[0].tobytes()
    except Exception as e:
        return jsonify({'error': f'Face detection error: {str(e)}'}), 400

    others = query("""
        SELECT f.employee_id, e.full_name FROM Face_Encoding f
        JOIN Employee e ON f.employee_id=e.employee_id
        WHERE f.employee_id != ? AND e.is_active=1
    """, (employee_id,))
    for other in others:
        try:
            ob = query("SELECT face_encoding_blob FROM Face_Encoding WHERE employee_id=?", (other['employee_id'],), one=True)
            if ob and ob['face_encoding_blob']:
                blob = ob['face_encoding_blob']
                if is_encrypted(blob):
                    blob = decrypt_face_encoding(blob)
                other_enc = np.frombuffer(blob, dtype=np.float64)
                if face_recognition.face_distance([other_enc], face_encodings[0])[0] < 0.4:
                    log_audit('REGISTER_FACE', 'Face',
                              f'Blocked: face already registered to {other["full_name"]}',
                              'Employee', uid, 'Failed',
                              {'other_employee': other['employee_id']})
                    return jsonify({
                        'error': f'This face is already registered to {other["full_name"]}. Each employee must use their own face.'
                    }), 409
        except Exception:
            continue

    existing = query("SELECT encoding_id FROM Face_Encoding WHERE employee_id=?", (employee_id,), one=True)
    if existing:
        execute("UPDATE Face_Encoding SET face_encoding_blob=?, updated_at=datetime('now') WHERE employee_id=?", (encoding_bytes, employee_id))
    else:
        execute("INSERT INTO Face_Encoding (employee_id, face_encoding_blob, registered_by) VALUES (?,?,?)", (employee_id, encoding_bytes, uid))

    log_audit('REGISTER_FACE', 'Face', 'Registered face biometric', 'Employee', employee_id, 'Success')
    return jsonify({'success': True, 'msg': 'Face registered successfully!'})


@att_bp.route('/preview_verify', methods=['GET'])
@login_required
def preview_verify():
    """Real-time face verification preview — no attendance recorded."""
    uid = session['user_id']
    open_att = _today_open_checkin(uid)
    face_registered = query("SELECT encoding_id FROM Face_Encoding WHERE employee_id=?", (uid,), one=True)
    return render_template('attendance/biometric.html',
                           face_registered=face_registered is not None,
                           is_checked_in=open_att is not None,
                           preview_mode=True,
                           failed_attempts=session.get('biometric_checkin_failures', 0))


@att_bp.route('/verify_face', methods=['POST'])
@login_required
def verify_face():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'}), 400

    uid = session['user_id']
    stored_encoding = _get_stored_encoding(uid)
    if stored_encoding is None:
        return jsonify({'error': 'Face not registered. Please register your face first.'}), 400

    try:
        img_np = _decode_image(data['image'])
        result = _verify_face_for_user(img_np, uid)

        face_detected = result.get('error') != 'No face detected. Please position your face in the frame.'

        if result.get('error') and not result.get('matched'):
            payload = {
                'registered': True,
                'face_detected': face_detected,
                'matched': False,
                'confidence': result.get('confidence', 0),
                'can_check_in': _today_open_checkin(uid) is None,
                'can_check_out': _today_open_checkin(uid) is not None,
                'wrong_person': result.get('wrong_person', False),
                'message': result['error'],
            }
            return jsonify(payload)

        return jsonify({
            'registered': True,
            'face_detected': True,
            'matched': result['matched'],
            'confidence': result['confidence'],
            'can_check_in': _today_open_checkin(uid) is None,
            'can_check_out': _today_open_checkin(uid) is not None,
            'message': f"Verified ({result['confidence']}%)" if result['matched'] else 'Face mismatch',
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@att_bp.route('/verify_face_and_record', methods=['POST'])
@login_required
def verify_face_and_record():
    data = request.get_json()
    if not data or 'action' not in data:
        return jsonify({'error': 'Invalid request'}), 400

    uid = session['user_id']
    action = data['action']

    stored_encoding = _get_stored_encoding(uid)
    if stored_encoding is None:
        return jsonify({'error': 'Face not registered. Please register your face first.'}), 400

    try:
        img_np = _decode_image(data['image'])
        result = _verify_face_for_user(img_np, uid)

        if result.get('error') and not result.get('matched'):
            details = {'action': action, 'confidence': result.get('confidence', 0)}
            if result.get('wrong_person'):
                details['wrong_person_id'] = result.get('wrong_person_id')
                details['wrong_person_name'] = result.get('wrong_person_name')
                log_audit('BIOMETRIC_WRONG_PERSON', 'Attendance', result['error'],
                          'Employee', uid, 'Failed', details)
                return jsonify({'error': result['error'], 'wrong_person': True}), 403

            if action == 'check_in':
                count = _increment_checkin_failures()
                details['attempt'] = count
                details['reason'] = 'no_face' if not result.get('confidence') else 'mismatch'
            log_audit('BIOMETRIC_VERIFY_FAILED', 'Attendance', result['error'],
                      'Employee', uid, 'Failed', details)
            return jsonify({
                'error': result['error'],
                **_failure_payload(action, session.get('biometric_checkin_failures', 0)),
            }), 400 if 'No face detected' in result['error'] else 401

        confidence = result['confidence']
        bid = session['branch_id']
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        today = datetime.datetime.now().strftime('%Y-%m-%d')

        if action == 'check_in':
            if _today_open_checkin(uid):
                return jsonify({'error': 'You are already checked in. Use Check Out instead.'}), 400

            aid = execute("""
                INSERT INTO Attendance (employee_id, branch_id, check_in, confidence_score, status)
                VALUES (?,?,?,?,'Approved')
            """, (uid, bid, now, confidence))
            _reset_checkin_failures()
            msg = f"Check-in successful! Confidence: {confidence}%"
            log_audit('BIOMETRIC_CHECKIN', 'Attendance', 'Face verified check-in',
                      'Attendance', aid, 'Success', {'confidence': confidence})
        else:
            open_att = _today_open_checkin(uid)
            if not open_att:
                return jsonify({'error': 'No open check-in found for today.'}), 400

            ci = datetime.datetime.fromisoformat(open_att['check_in'])
            co_dt = datetime.datetime.now()
            diff_h = round((co_dt - ci).total_seconds() / 3600, 2)
            emp_sched = query("SELECT work_start_time, work_end_time FROM Employee WHERE employee_id=?", (uid,), one=True)
            s_start = emp_sched['work_start_time'] or '09:00'
            s_end = emp_sched['work_end_time'] or '18:00'
            sh, sm = map(int, s_start.split(':'))
            eh, em = map(int, s_end.split(':'))
            sched_hours = (eh + em/60) - (sh + sm/60)
            ot = round(max(0, diff_h - sched_hours), 2)

            execute("""
                UPDATE Attendance SET check_out=?, hours_worked=?, overtime_hours=?,
                       confidence_score=?, status='Approved'
                WHERE attendance_id=?
            """, (co_dt.strftime('%Y-%m-%d %H:%M:%S'), diff_h, ot, confidence, open_att['attendance_id']))
            _reset_checkin_failures()
            msg = f"Check-out successful! Hours: {diff_h}h. Confidence: {confidence}%"
            log_audit('BIOMETRIC_CHECKOUT', 'Attendance', 'Face verified check-out',
                      'Attendance', open_att['attendance_id'], 'Success',
                      {'hours': diff_h, 'confidence': confidence})

        return jsonify({'success': True, 'message': msg, 'confidence': confidence})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@att_bp.route('/manual_self', methods=['POST'])
@login_required
def manual_self():
    """Employee self-service manual check-in/out after biometric failures."""
    uid = session['user_id']
    bid = session['branch_id']
    data = request.json or {}
    action = data.get('action', 'check_in')
    reason = data.get('reason', 'Biometric failed (3 attempts)').strip()
    att_time = data.get('time')

    if session.get('user_role') in ('Admin', 'HR Director', 'HR Manager', 'HR', 'Manager'):
        return jsonify({'error': 'Manual self-entry is for employees only. Use Manual Attendance page instead.'}), 403

    if not reason:
        return jsonify({'error': 'Manual entry reason is required.'}), 400

    if session.get('biometric_checkin_failures', 0) < 3:
        return jsonify({'error': 'Manual entry is available after 3 failed biometric attempts.'}), 403

    now = datetime.datetime.now()
    if att_time:
        try:
            parts = att_time.split(':')
            now = now.replace(hour=int(parts[0]), minute=int(parts[1]), second=0, microsecond=0)
        except (ValueError, IndexError):
            return jsonify({'error': 'Invalid time format. Use HH:MM'}), 400

    if action == 'check_out':
        open_att = _today_open_checkin(uid)
        if not open_att:
            return jsonify({'error': 'No open check-in found for today.'}), 400
        ci = datetime.datetime.fromisoformat(open_att['check_in'])
        co_dt = now
        diff_h = round((co_dt - ci).total_seconds() / 3600, 2)
        emp_sched = query("SELECT work_start_time, work_end_time FROM Employee WHERE employee_id=?", (uid,), one=True)
        s_start = emp_sched['work_start_time'] or '09:00'
        s_end = emp_sched['work_end_time'] or '18:00'
        sh, sm = map(int, s_start.split(':'))
        eh, em = map(int, s_end.split(':'))
        sched_hours = (eh + em/60) - (sh + sm/60)
        ot = round(max(0, diff_h - sched_hours), 2)
        co_str = co_dt.strftime('%Y-%m-%d %H:%M:%S')
        execute("""
            UPDATE Attendance SET check_out=?, hours_worked=?, overtime_hours=?,
                   is_manual_entry=1, manual_reason=?, status='Pending'
            WHERE attendance_id=?
        """, (co_str, diff_h, ot, reason, open_att['attendance_id']))
        _reset_checkin_failures()
        log_audit('MANUAL_CHECKOUT', 'Attendance',
                  'Self-service manual check-out after biometric failure',
                  'Attendance', open_att['attendance_id'], 'Success',
                  {'datetime': co_str, 'hours': diff_h, 'self_service': True})
        return jsonify({
            'success': True,
            'message': f'Manual check-out recorded at {co_str[11:16]} (pending approval). Hours: {diff_h}h.',
        })
    else:
        if _today_open_checkin(uid):
            return jsonify({'error': 'You are already checked in today.'}), 400
        dt_str = now.strftime('%Y-%m-%d %H:%M:%S')
        aid = execute("""
            INSERT INTO Attendance
            (employee_id, branch_id, check_in, is_manual_entry, manual_reason, status)
            VALUES (?,?,?,1,?,'Pending')
        """, (uid, bid, dt_str, reason))
        _reset_checkin_failures()
        log_audit('MANUAL_CHECKIN', 'Attendance',
                  'Self-service manual check-in after biometric failure',
                  'Attendance', aid, 'Success',
                  {'datetime': dt_str, 'reason': reason, 'self_service': True})
        return jsonify({
            'success': True,
            'message': f'Manual check-in recorded at {dt_str[11:16]} (pending approval).',
        })


@att_bp.route('/manual-pending', methods=['GET', 'POST'])
@role_required('Admin', 'HR', 'HR Manager', 'HR Director')
def manual_pending_review():
    """HR reviews pending self-service manual attendance entries (from biometric failure)."""
    uid = session['user_id']
    co = session['company_id']

    if request.method == 'POST':
        aid = request.form.get('attendance_id')
        action = request.form.get('action')
        if aid and action in ('approve', 'reject'):
            rec = query("SELECT * FROM Attendance WHERE attendance_id=?", (aid,), one=True)
            if not rec or rec['status'] != 'Pending':
                flash('Entry not found or already processed.', 'danger')
                return redirect(url_for('attendance.manual_pending_review'))

            if rec['employee_id'] == uid:
                flash('You cannot approve or reject your own manual entry. Another Admin/HR must review it.', 'danger')
                return redirect(url_for('attendance.manual_pending_review'))

            if action == 'approve':
                new_status = 'Approved'
                if rec['check_in'] and not rec['check_out']:
                    execute("UPDATE Attendance SET status=?, corrected_by=?, corrected_at=datetime('now') WHERE attendance_id=?", (new_status, uid, aid))
                elif rec['check_in'] and rec['check_out']:
                    ci = datetime.datetime.fromisoformat(rec['check_in'])
                    co_dt = datetime.datetime.fromisoformat(rec['check_out'])
                    diff_h = round((co_dt - ci).total_seconds() / 3600, 2)
                    execute("UPDATE Attendance SET status=?, hours_worked=?, corrected_by=?, corrected_at=datetime('now') WHERE attendance_id=?", (new_status, diff_h, uid, aid))
                flash('Manual entry approved.', 'success')
            else:
                new_status = 'Rejected'
                execute("UPDATE Attendance SET status=?, corrected_by=?, corrected_at=datetime('now') WHERE attendance_id=?", (new_status, uid, aid))
                flash('Manual entry rejected.', 'info')

            log_audit('MANUAL_PENDING_REVIEW', 'Attendance',
                      f'{action}d manual attendance #{aid}',
                      'Attendance', aid, 'Success')

            emp = query("SELECT full_name FROM Employee WHERE employee_id=?", (rec['employee_id'],), one=True)
            try:
                from app.notifications.routes import send_notification
                send_notification(
                    rec['employee_id'],
                    f'Manual Check-In {new_status}',
                    f'Your manual check-in on {rec["check_in"][:10] if rec["check_in"] else "unknown date"} was {new_status.lower()}.',
                    type='Success' if new_status == 'Approved' else 'Error'
                )
            except Exception as e:
                print(f"[MANUAL_PENDING] Notification error: {e}")

        return redirect(url_for('attendance.manual_pending_review'))

    pending = query("""
        SELECT a.*, e.full_name, e.position, e.employee_id as emp_id
        FROM Attendance a
        JOIN Employee e ON a.employee_id=e.employee_id
        WHERE a.is_manual_entry=1 AND a.status='Pending' AND e.company_id=?
        ORDER BY a.created_at DESC
    """, (co,))
    return render_template('attendance/manual_pending.html', pending=pending)


@att_bp.route('/manual-pending-count')
@role_required('Admin', 'HR', 'HR Manager', 'HR Director')
def manual_pending_count():
    """Return count of pending manual entries (for nav badge)."""
    co = session['company_id']
    count = query("""
        SELECT COUNT(*) as cnt FROM Attendance a
        JOIN Employee e ON a.employee_id=e.employee_id
        WHERE a.is_manual_entry=1 AND a.status='Pending' AND e.company_id=?
    """, (co,), one=True)
    return jsonify({'count': count['cnt'] if count else 0})


@att_bp.route('/logs')
@login_required
def attendance_logs():
    """View filterable attendance logs."""
    co = session['company_id']
    uid = session['user_id']
    role = session['user_role']

    date_from = request.args.get('from', datetime.date.today().replace(day=1).isoformat())
    date_to = request.args.get('to', datetime.date.today().isoformat())
    emp_filter = request.args.get('employee', '')
    method = request.args.get('method', '')
    branch_filter = request.args.get('branch', '')
    export_csv = request.args.get('export', '')

    if role in ('Admin', 'HR Director', 'HR Manager', 'HR'):
        if branch_filter:
            employees = query("SELECT employee_id, full_name FROM Employee WHERE company_id=? AND branch_id=? AND is_active=1 ORDER BY full_name", (co, branch_filter))
        else:
            employees = query("SELECT employee_id, full_name FROM Employee WHERE company_id=? AND is_active=1 ORDER BY full_name", (co,))
        branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? ORDER BY name", (co,))
    elif role == 'Manager':
        bid = session.get('branch_id')
        employees = query("SELECT employee_id, full_name FROM Employee WHERE company_id=? AND branch_id=? AND is_active=1 ORDER BY full_name", (co, bid))
        branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? AND branch_id=? ORDER BY name", (co, bid))
        # Managers may only view their own branch's logs – ignore any cross-branch filter
        if branch_filter and branch_filter != str(bid):
            branch_filter = ''
        if emp_filter:
            own_emp = query("SELECT 1 FROM Employee WHERE employee_id=? AND company_id=? AND branch_id=?",
                            (emp_filter, co, bid), one=True)
            if not own_emp:
                emp_filter = ''
        branch_filter = str(bid) if bid else ''
    else:
        employees = []
        branches = []

    sql = """
        SELECT a.*, e.full_name, d.department_name, b.name as branch_name,
               CASE WHEN a.is_manual_entry=1 THEN 'Manual' ELSE 'Biometric' END as entry_method
        FROM Attendance a
        JOIN Employee e ON a.employee_id=e.employee_id
        JOIN Department d ON e.department_id=d.department_id
        JOIN Branch b ON a.branch_id=b.branch_id
        WHERE e.company_id=?
          AND date(a.check_in) >= ? AND date(a.check_in) <= ?
    """
    args = [co, date_from, date_to]

    if branch_filter:
        sql += " AND e.branch_id=?"
        args.append(branch_filter)
    if emp_filter:
        sql += " AND a.employee_id=?"
        args.append(emp_filter)
    if method == 'manual':
        sql += " AND a.is_manual_entry=1"
    elif method == 'biometric':
        sql += " AND a.is_manual_entry=0"
    if branch_filter:
        sql += " AND a.branch_id=?"
        args.append(branch_filter)

    sql += " ORDER BY a.check_in DESC LIMIT 500"
    records = query(sql, args)

    if export_csv == '1':
        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(['Date', 'Employee', 'Department', 'Branch', 'Check In', 'Check Out', 'Hours', 'Overtime', 'Method', 'Confidence', 'Status'])
        for r in records:
            cw.writerow([
                r['check_in'][:10] if r['check_in'] else '',
                r['full_name'], r['department_name'], r['branch_name'],
                r['check_in'][11:19] if r['check_in'] else '',
                r['check_out'][11:19] if r['check_out'] else '',
                r['hours_worked'], r['overtime_hours'],
                r['entry_method'], r['confidence_score'], r['status']
            ])
        out = make_response(si.getvalue())
        out.headers['Content-Disposition'] = f'attachment; filename=attendance_logs_{date_from}_{date_to}.csv'
        out.headers['Content-type'] = 'text/csv'
        return out

    if role in ('Admin', 'HR Director', 'HR Manager', 'HR'):
        if branch_filter:
            stats = query("""
                SELECT COUNT(*) as total_records,
                       SUM(CASE WHEN a.is_manual_entry=0 THEN 1 ELSE 0 END) as biometric_count,
                       SUM(CASE WHEN a.is_manual_entry=1 THEN 1 ELSE 0 END) as manual_count,
                       ROUND(AVG(a.confidence_score), 1) as avg_confidence,
                       ROUND(SUM(a.hours_worked), 1) as total_hours
                FROM Attendance a
                JOIN Employee e ON a.employee_id=e.employee_id
                WHERE e.company_id=? AND e.branch_id=?
                  AND date(a.check_in) >= ? AND date(a.check_in) <= ?
            """, (co, branch_filter, date_from, date_to), one=True)
        elif emp_filter:
            stats = query("""
                SELECT COUNT(*) as total_records,
                       SUM(CASE WHEN a.is_manual_entry=0 THEN 1 ELSE 0 END) as biometric_count,
                       SUM(CASE WHEN a.is_manual_entry=1 THEN 1 ELSE 0 END) as manual_count,
                       ROUND(AVG(a.confidence_score), 1) as avg_confidence,
                       ROUND(SUM(a.hours_worked), 1) as total_hours
                FROM Attendance a
                JOIN Employee e ON a.employee_id=e.employee_id
                WHERE e.company_id=? AND a.employee_id=?
                  AND date(a.check_in) >= ? AND date(a.check_in) <= ?
            """, (co, emp_filter, date_from, date_to), one=True)
        else:
            stats = query("""
                SELECT COUNT(*) as total_records,
                       SUM(CASE WHEN a.is_manual_entry=0 THEN 1 ELSE 0 END) as biometric_count,
                       SUM(CASE WHEN a.is_manual_entry=1 THEN 1 ELSE 0 END) as manual_count,
                       ROUND(AVG(a.confidence_score), 1) as avg_confidence,
                       ROUND(SUM(a.hours_worked), 1) as total_hours
                FROM Attendance a
                JOIN Employee e ON a.employee_id=e.employee_id
                WHERE e.company_id=?
                  AND date(a.check_in) >= ? AND date(a.check_in) <= ?
            """, (co, date_from, date_to), one=True)
    else:
        stats = None

    return render_template('attendance/logs.html',
                           records=records, employees=employees, branches=branches,
                           date_from=date_from, date_to=date_to,
                           selected_employee=emp_filter, selected_method=method,
                           selected_branch=branch_filter, stats=stats)



