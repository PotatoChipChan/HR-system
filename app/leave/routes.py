"""app/leave/routes.py – Apply, approve, reject leave"""
import datetime
import os
import uuid
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, current_app, send_from_directory, abort)
from app.database import query, execute, log_audit, as_dict, is_leave_eligible
from app.auth.routes import login_required, role_required
from app.notifications.routes import send_notification

LEAVE_ALLOWED_EXT = {'jpg', 'jpeg', 'png', 'pdf'}


def leave_allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in LEAVE_ALLOWED_EXT

leave_bp = Blueprint('leave', __name__, url_prefix='/leave')


@leave_bp.route('/apply', methods=['GET', 'POST'])
@login_required
def apply():
    uid = session['user_id']
    yr  = datetime.date.today().year
    
    # Get employee details for eligibility check
    emp = as_dict(query("SELECT gender, marital_status FROM Employee WHERE employee_id=?", (uid,), one=True))
    emp_gender = emp.get('gender')
    emp_marital = emp.get('marital_status')
    
    # Get all leave types, then filter eligible ones
    all_leave_types = query("SELECT * FROM Leave_Type ORDER BY type_name")
    leave_types = []
    for lt in all_leave_types:
        lt_dict = as_dict(lt)
        if is_leave_eligible(lt_dict, emp_gender, emp_marital):
            leave_types.append(lt)
        
    # Get balances (also filter balances to eligible leave types
    all_balances = query("""
        SELECT lb.*, lt.type_name 
        FROM Leave_Balance lb
        JOIN Leave_Type lt ON lb.leave_type_id=lt.leave_type_id
        WHERE lb.employee_id=? AND lb.year=?
    """, (uid, yr))
    balances = []
    for b in all_balances:
        # Get leave type details for this balance to check eligibility
        lt = as_dict(query("SELECT * FROM Leave_Type WHERE leave_type_id=?", (b['leave_type_id'],), one=True))
        if is_leave_eligible(lt, emp_gender, emp_marital):
            balances.append(b)

    recent = query("""
        SELECT la.*, lt.type_name FROM Leave_Application la
        JOIN Leave_Type lt ON la.leave_type_id=lt.leave_type_id
        WHERE la.employee_id=?
        ORDER BY la.applied_at DESC LIMIT 10
    """, (uid,))

    if request.method == 'POST':
        f   = request.form
        try:
            lt_id = int(f.get('leave_type_id', ''))
            start = f.get('start_date', '')
            end = f.get('end_date', '')
            sd = datetime.date.fromisoformat(start)
            ed = datetime.date.fromisoformat(end)
        except (TypeError, ValueError):
            flash('Select a valid leave type, start date, and end date.', 'danger')
            return redirect(url_for('leave.apply'))
        reason = f.get('reason', '')

        # Recheck that the requested leave type exists and is eligible. This
        # keeps crafted form submissions from reaching the balance/insert path.
        selected_lt = as_dict(query("SELECT * FROM Leave_Type WHERE leave_type_id=?", (lt_id,), one=True))
        if not selected_lt:
            flash('Select a valid leave type.', 'danger')
            return redirect(url_for('leave.apply'))
        if not is_leave_eligible(selected_lt, emp_gender, emp_marital):
            flash('You are not eligible to apply for this leave type.', 'danger')
            return redirect(url_for('leave.apply'))

        # Handle file upload
        attachment = request.files.get('attachment')
        saved_fn = None
        if attachment and attachment.filename:
            if not leave_allowed_file(attachment.filename):
                flash('Only JPG, PNG, PDF files are accepted.', 'danger')
                return redirect(url_for('leave.apply'))
            ext = attachment.filename.rsplit('.', 1)[1].lower()
            saved_fn = f"{uuid.uuid4().hex}.{ext}"
            leave_upload = os.path.join(current_app.config['UPLOAD_FOLDER'], 'leave')
            os.makedirs(leave_upload, exist_ok=True)
            attachment.save(os.path.join(leave_upload, saved_fn))

        # Calculate working days
        if sd > ed:
            flash('End date must be after start date.', 'danger')
            return redirect(url_for('leave.apply'))

        total_days = sum(1 for n in range((ed - sd).days + 1)
                         if (sd + datetime.timedelta(n)).weekday() < 5)
        if total_days == 0:
            flash('Selected dates fall on weekends only.', 'danger')
            return redirect(url_for('leave.apply'))
        
        # Check for overlapping leaves on same date
        overlap = query("""
            SELECT COUNT(*) as cnt FROM Leave_Application
            WHERE employee_id=? AND status IN ('Pending','Approved')
              AND start_date <= ? AND end_date >= ?
        """, (uid, end, start), one=True)
        if overlap and overlap['cnt'] > 0:
            flash('You already have a pending or approved leave that overlaps with these dates.', 'danger')
            return redirect(url_for('leave.apply'))

        # Check balance
        bal = query("SELECT * FROM Leave_Balance WHERE employee_id=? AND leave_type_id=? AND year=?",
                    (uid, lt_id, yr), one=True)
        lt = selected_lt

        if bal:
            available = bal['entitled_days'] - bal['used_days'] - bal['pending_days']
            if lt['is_paid'] and total_days > available:
                flash(f'Insufficient leave balance. Available: {available:.1f} days.', 'danger')
                return redirect(url_for('leave.apply'))

        lid = execute("""
            INSERT INTO Leave_Application
            (employee_id, leave_type_id, start_date, end_date, total_days, reason, supporting_doc, status)
            VALUES (?,?,?,?,?,?,?,'Pending')
        """, (uid, lt_id, start, end, total_days, reason, saved_fn))

        # Update pending days in balance
        if bal:
            execute("UPDATE Leave_Balance SET pending_days=pending_days+? WHERE employee_id=? AND leave_type_id=? AND year=?",
                    (total_days, uid, lt_id, yr))
        else:
            execute("INSERT INTO Leave_Balance(employee_id,leave_type_id,year,entitled_days,used_days,pending_days) VALUES(?,?,?,?,?,?)",
                    (uid, lt_id, yr, lt['default_days'], 0, total_days))

        log_audit('APPLY_LEAVE', 'Leave', f'Leave application submitted for {total_days} day(s)',
                  'Leave_Application', lid, 'Success',
                  {'leave_type': lt['type_name'], 'start': start, 'end': end, 'days': total_days})
        flash(f'Leave request submitted for {total_days} working day(s).', 'success')
        return redirect(url_for('leave.apply'))

    return render_template('leave/apply.html',
                           leave_types=leave_types, balances=balances, recent=recent)


@leave_bp.route('/approve')
@role_required('Admin', 'HR', 'HR Manager', 'Manager')
def approve_list():
    co = session['company_id']
    role = session['user_role']
    bid = session.get('branch_id')

    if role == 'Manager':
        pending = query("""
            SELECT la.*, lt.type_name, e.full_name, e.position, d.department_name,
                   e.employment_status
            FROM Leave_Application la
            JOIN Employee e   ON la.employee_id    = e.employee_id
            JOIN Leave_Type lt ON la.leave_type_id = lt.leave_type_id
            JOIN Department d  ON e.department_id  = d.department_id
            WHERE e.company_id=? AND e.branch_id=? AND la.status='Pending'
            ORDER BY la.applied_at ASC
        """, (co, bid))

        history = query("""
            SELECT la.*, lt.type_name, e.full_name,
                   rev.full_name as reviewer_name
            FROM Leave_Application la
            JOIN Employee e    ON la.employee_id  = e.employee_id
            JOIN Leave_Type lt ON la.leave_type_id= lt.leave_type_id
            LEFT JOIN Employee rev ON la.reviewed_by = rev.employee_id
            WHERE e.company_id=? AND e.branch_id=? AND la.status != 'Pending'
            ORDER BY la.reviewed_at DESC LIMIT 20
        """, (co, bid))
    else:
        pending = query("""
            SELECT la.*, lt.type_name, e.full_name, e.position, d.department_name,
                   e.employment_status
            FROM Leave_Application la
            JOIN Employee e   ON la.employee_id    = e.employee_id
            JOIN Leave_Type lt ON la.leave_type_id = lt.leave_type_id
            JOIN Department d  ON e.department_id  = d.department_id
            WHERE e.company_id=? AND la.status='Pending'
            ORDER BY la.applied_at ASC
        """, (co,))

        history = query("""
            SELECT la.*, lt.type_name, e.full_name,
                   rev.full_name as reviewer_name
            FROM Leave_Application la
            JOIN Employee e    ON la.employee_id  = e.employee_id
            JOIN Leave_Type lt ON la.leave_type_id= lt.leave_type_id
            LEFT JOIN Employee rev ON la.reviewed_by = rev.employee_id
            WHERE e.company_id=? AND la.status != 'Pending'
            ORDER BY la.reviewed_at DESC LIMIT 20
        """, (co,))

    return render_template('leave/approve.html', pending=pending, history=history)


@leave_bp.route('/approve/<int:lid>', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager', 'Manager')
def approve(lid):
    uid = session['user_id']
    la  = query("SELECT * FROM Leave_Application WHERE leave_id=?", (lid,), one=True)
    if not la:
        flash('Leave application not found.', 'danger')
        return redirect(url_for('leave.approve_list'))

    # Prevent self-approval: an approver cannot approve their own leave.
    if la['employee_id'] == uid:
        flash('You cannot approve or reject your own leave application. Another approver must review it.', 'danger')
        return redirect(url_for('leave.approve_list'))

    # Manager branch check
    if session['user_role'] == 'Manager':
        emp_branch = query("""
            SELECT e.branch_id FROM Leave_Application la
            JOIN Employee e ON la.employee_id = e.employee_id
            WHERE la.leave_id = ?
        """, (lid,), one=True)
        if not emp_branch or emp_branch['branch_id'] != session['branch_id']:
            flash('Access denied. You can only approve leave for staff in your branch.', 'danger')
            return redirect(url_for('leave.approve_list'))

    yr  = datetime.date.fromisoformat(la['start_date']).year
    execute("""
        UPDATE Leave_Application SET status='Approved', reviewed_by=?,
               reviewed_at=datetime('now'), last_updated_by=?, last_updated_at=datetime('now')
        WHERE leave_id=?
    """, (uid, uid, lid))

    # Move pending → used.  Employment status is a durable employment state;
    # whether someone is away today is derived from approved leave dates by
    # the dashboard/profile rather than being changed when leave is approved.
    execute("""UPDATE Leave_Balance
               SET used_days=used_days+?, pending_days=MAX(0,pending_days-?)
               WHERE employee_id=? AND leave_type_id=? AND year=?""",
            (la['total_days'], la['total_days'],
             la['employee_id'], la['leave_type_id'], yr))

    log_audit('APPROVE', 'Leave', f'Approved leave application LV-{lid}',
              'Leave_Application', lid, 'Success', {'days': la['total_days']})
    send_notification(la['employee_id'], 
                     'Leave Application Approved', 
                     f"Your leave application LV-{lid} has been approved.",
                     'Success',
                     url_for('leave.apply'))
    flash(f'Leave application LV-{lid} approved.', 'success')
    return redirect(url_for('leave.approve_list'))


@leave_bp.route('/reject/<int:lid>', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager', 'Manager')
def reject(lid):
    uid     = session['user_id']
    comment = request.form.get('comment', '')
    la      = query("SELECT * FROM Leave_Application WHERE leave_id=?", (lid,), one=True)
    if not la:
        flash('Leave application not found.', 'danger')
        return redirect(url_for('leave.approve_list'))

    # Prevent self-rejection: an approver cannot reject their own leave.
    if la['employee_id'] == uid:
        flash('You cannot approve or reject your own leave application. Another approver must review it.', 'danger')
        return redirect(url_for('leave.approve_list'))

    # Manager branch check
    if session['user_role'] == 'Manager':
        emp_branch = query("""
            SELECT e.branch_id FROM Leave_Application la
            JOIN Employee e ON la.employee_id = e.employee_id
            WHERE la.leave_id = ?
        """, (lid,), one=True)
        if not emp_branch or emp_branch['branch_id'] != session['branch_id']:
            flash('Access denied. You can only reject leave for staff in your branch.', 'danger')
            return redirect(url_for('leave.approve_list'))

    yr = datetime.date.fromisoformat(la['start_date']).year
    execute("""
        UPDATE Leave_Application SET status='Rejected', reviewed_by=?,
               reviewed_at=datetime('now'), review_comment=?,
               last_updated_by=?, last_updated_at=datetime('now')
        WHERE leave_id=?
    """, (uid, comment, uid, lid))

    # Release pending days
    execute("UPDATE Leave_Balance SET pending_days=MAX(0,pending_days-?) WHERE employee_id=? AND leave_type_id=? AND year=?",
            (la['total_days'], la['employee_id'], la['leave_type_id'], yr))

    log_audit('REJECT', 'Leave', f'Rejected leave application LV-{lid}',
              'Leave_Application', lid, 'Success', {'reason': comment})
    send_notification(la['employee_id'], 
                     'Leave Application Rejected', 
                     f"Your leave application LV-{lid} has been rejected: {comment if comment else 'No reason provided.'}",
                     'Warning',
                     url_for('leave.apply'))
    flash(f'Leave application LV-{lid} rejected.', 'warning')
    return redirect(url_for('leave.approve_list'))


@leave_bp.route('/cancel/<int:lid>', methods=['POST'])
@login_required
def cancel(lid):
    uid = session['user_id']
    la  = query("SELECT * FROM Leave_Application WHERE leave_id=? AND employee_id=? AND status='Pending'",
                (lid, uid), one=True)
    if not la:
        flash('Cannot cancel this application.', 'danger')
        return redirect(url_for('leave.apply'))

    yr = datetime.date.fromisoformat(la['start_date']).year
    execute("UPDATE Leave_Application SET status='Cancelled', last_updated_at=datetime('now') WHERE leave_id=?", (lid,))
    execute("UPDATE Leave_Balance SET pending_days=MAX(0,pending_days-?) WHERE employee_id=? AND leave_type_id=? AND year=?",
            (la['total_days'], uid, la['leave_type_id'], yr))
    log_audit('CANCEL', 'Leave', f'Cancelled leave application LV-{lid}',
              'Leave_Application', lid, 'Success')
    flash('Leave application cancelled.', 'info')
    return redirect(url_for('leave.apply'))


@leave_bp.route('/attachment/<int:lid>')
@login_required
def download_attachment(lid):
    la = query("SELECT employee_id, supporting_doc FROM Leave_Application WHERE leave_id=?", (lid,), one=True)
    if not la or not la['supporting_doc']:
        abort(404)
    uid = session['user_id']
    role = session['user_role']
    if uid != la['employee_id'] and role not in ('Admin', 'HR', 'HR Manager', 'Manager'):
        abort(403)
    return send_from_directory(
        os.path.join(current_app.config['UPLOAD_FOLDER'], 'leave'),
        la['supporting_doc']
    )
