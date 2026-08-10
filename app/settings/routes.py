"""app/main/routes.py – Settings & Profile"""
# This is included in main blueprint but kept separate for clarity
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash)
from werkzeug.security import generate_password_hash, check_password_hash
from app.database import query, execute, log_audit
from app.auth.routes import login_required

settings_bp = Blueprint('settings', __name__, url_prefix='/settings')


@settings_bp.route('/')
@login_required
def index():
    uid = session['user_id']
    co  = session['company_id']
    emp = query("""
        SELECT e.*, r.role_name, d.department_name, b.name as branch_name, c.name as company_name
        FROM Employee e
        JOIN Role r ON e.role_id=r.role_id
        JOIN Department d ON e.department_id=d.department_id
        JOIN Branch b ON e.branch_id=b.branch_id
        JOIN Company c ON e.company_id=c.company_id
        WHERE e.employee_id=?
    """, (uid,), one=True)
    company = query("SELECT * FROM Company WHERE company_id=?", (co,), one=True)
    
    # Get pending IC access requests for this user
    pending_ic_requests = query("""
        SELECT r.*, requester.full_name as requester_name
        FROM IC_Access_Request r
        JOIN Employee requester ON r.requester_id = requester.employee_id
        WHERE r.target_employee_id = ? AND r.status = 'Pending'
        ORDER BY r.requested_at DESC
    """, (uid,))
    
    return render_template('settings.html', emp=emp, company=company, pending_ic_requests=pending_ic_requests)


@settings_bp.route('/profile', methods=['POST'])
@login_required
def update_profile():
    uid = session['user_id']
    f   = request.form
    # Get current gender from database to prevent changes
    current_gender = query("SELECT gender FROM Employee WHERE employee_id=?", (uid,), one=True)['gender']
    execute("""UPDATE Employee SET full_name=?, contact_no=?, address=?,
               date_of_birth=?, gender=?, emergency_contact_name=?,
               emergency_contact_no=?, updated_at=datetime('now')
               WHERE employee_id=?""",
            (f['full_name'], f.get('contact_no',''), f.get('address',''),
             f.get('date_of_birth',''), current_gender,
             f.get('emergency_contact_name',''), f.get('emergency_contact_no',''),
             uid))
    session['user_name'] = f['full_name']
    session['user_initials'] = ''.join(p[0].upper() for p in f['full_name'].split()[:2])
    log_audit('UPDATE', 'Settings', 'Updated own profile', 'Employee', uid)
    flash('Profile updated successfully.', 'success')
    return redirect(url_for('settings.index'))


@settings_bp.route('/password', methods=['POST'])
@login_required
def change_password():
    uid     = session['user_id']
    old_pw  = request.form.get('old_password', '')
    new_pw  = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')

    emp = query("SELECT password_hash FROM Employee WHERE employee_id=?", (uid,), one=True)
    if not check_password_hash(emp['password_hash'], old_pw):
        flash('Current password is incorrect.', 'danger')
        return redirect(url_for('settings.index'))
    if new_pw != confirm:
        flash('New passwords do not match.', 'danger')
        return redirect(url_for('settings.index'))
    if len(new_pw) < 6:
        flash('Password must be at least 6 characters.', 'danger')
        return redirect(url_for('settings.index'))

    execute("UPDATE Employee SET password_hash=?, updated_at=datetime('now') WHERE employee_id=?",
            (generate_password_hash(new_pw), uid))
    log_audit('CHANGE_PASSWORD', 'Settings', 'Changed own password', 'Employee', uid)
    flash('Password changed successfully.', 'success')
    return redirect(url_for('settings.index'))
