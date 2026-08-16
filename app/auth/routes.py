"""app/auth/routes.py – Login, logout, password reset, and login_required decorator"""
import os
from functools import wraps
from flask import (Blueprint, render_template, request, redirect,
                   url_for, session, flash, current_app)
from werkzeug.security import check_password_hash, generate_password_hash
from app.database import query, execute, log_audit, get_db
from itsdangerous import URLSafeTimedSerializer

auth_bp = Blueprint('auth', __name__)


def _dev_mode():
    """Match create_app's dev-mode flag (FLASK_DEBUG / FLASK_ENV)."""
    return (os.environ.get('FLASK_DEBUG', '').lower() in ('true', '1', 'yes') or
            os.environ.get('FLASK_ENV', '').lower() == 'development')


# ── Decorator ──────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    """Allow access only to users whose role is in the given list.
    HR Manager inherits all Admin & HR permissions."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('auth.login'))
            user_role = session.get('user_role')
            check_roles = list(roles)
            if user_role == 'HR Manager' and ('Admin' in roles or 'HR' in roles):
                check_roles.append('HR Manager')
            if user_role not in check_roles:
                flash('You do not have permission to access that page.', 'danger')
                return redirect(url_for('main.dashboard'))
            return f(*args, **kwargs)
        return decorated
    return decorator


# ── Login ──────────────────────────────────────────────────────────────────
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        emp = query("""
            SELECT e.*, r.role_name, d.department_name
            FROM Employee e
            JOIN Role r ON e.role_id = r.role_id
            JOIN Department d ON e.department_id = d.department_id
            WHERE LOWER(e.email) = ? AND e.is_active = 1
        """, (email,), one=True)

        if emp is None:
            log_audit('LOGIN', 'Auth', f'Failed login – email not found: {email}',
                      action_status='Failed')
            flash('Invalid email or password.', 'danger')
            return render_template('login.html')

        # Check account lock
        if emp['locked_until']:
            from datetime import datetime
            if datetime.now() < datetime.fromisoformat(emp['locked_until']):
                flash('Account temporarily locked. Please try again later.', 'danger')
                return render_template('login.html')

        if not check_password_hash(emp['password_hash'], password):
            attempts = (emp['failed_attempts'] or 0) + 1
            lock_until = None
            if attempts >= 5:
                from datetime import datetime, timedelta
                lock_until = (datetime.now() + timedelta(minutes=15)).isoformat()
                flash('Too many failed attempts. Account locked for 15 minutes.', 'danger')
            else:
                flash('Invalid email or password.', 'danger')

            execute("UPDATE Employee SET failed_attempts=?, locked_until=? WHERE employee_id=?",
                    (attempts, lock_until, emp['employee_id']))
            log_audit('LOGIN', 'Auth', f'Failed login for {email}',
                      'Employee', emp['employee_id'], 'Failed',
                      {'failed_attempts': attempts})
            return render_template('login.html')

        # Successful login
        execute("UPDATE Employee SET failed_attempts=0, locked_until=NULL, last_login=datetime('now') WHERE employee_id=?",
                (emp['employee_id'],))

        initials = ''.join(p[0].upper() for p in emp['full_name'].split()[:2])
        
        # 'Remember Me' functionality: If checked, make session permanent for 7 days
        if request.form.get('remember'):
            session.permanent = True
            from datetime import timedelta
            session.permanent_session_lifetime = timedelta(days=7)
        else:
            session.permanent = False
            
        session['user_id']       = emp['employee_id']
        session['user_name']     = emp['full_name']
        session['user_role']     = emp['role_name']
        session['user_email']    = emp['email']
        session['user_initials'] = initials
        session['user_position'] = emp['position'] or ''
        session['company_id']    = emp['company_id']
        session['branch_id']     = emp['branch_id']
        session['dept_name']     = emp['department_name']

        # Check if user is a department manager
        # Exemption: the Branch Manager department's manager IS the branch-wide
        # manager - treat them as a plain Manager (no dept scoping), so they
        # keep the Sprint 10 branch-wide behaviour.
        # ORDER BY department_id: the seeded 'Branch Manager' department always
        # carries the lowest id, so an employee who manages several departments
        # (e.g. Branch Manager + a normal dept) deterministically hits the
        # exemption instead of being silently dept-scoped.
        dept_mgr = query("""SELECT department_id, department_name FROM Department
                            WHERE department_manager_id = ?
                            ORDER BY department_id""",
                         (emp['employee_id'],), one=True)
        is_branch_manager_dept = dept_mgr is not None and dept_mgr['department_name'] == 'Branch Manager'
        session['is_dept_manager'] = dept_mgr is not None and not is_branch_manager_dept
        session['managed_dept_id'] = None if (dept_mgr is None or is_branch_manager_dept) else dept_mgr['department_id']

        log_audit('LOGIN', 'Auth', f'{emp["full_name"]} logged in',
                  'Employee', emp['employee_id'], 'Success')
        return redirect(url_for('main.dashboard'))

    return render_template('login.html')


# ── Password Reset ─────────────────────────────────────────────────────────
def get_token_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='password-reset')


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        emp = query("SELECT employee_id, full_name FROM Employee WHERE LOWER(email)=? AND is_active=1",
                    (email,), one=True)

        if emp:
            s = get_token_serializer()
            token = s.dumps(email)
            reset_url = url_for('auth.reset_password', token=token, _external=True)
            try:
                from flask_mail import Message
                from app import mail
                html_body = render_template('emails/password_reset.html',
                                            employee_name=emp['full_name'],
                                            title='Password Reset',
                                            message='Click the button below to reset your password.',
                                            reset_url=reset_url)
                from app.notifications.email_service import strip_html
                msg = Message(subject='SmartHR - Password Reset',
                              recipients=[email])
                msg.body = f"Hi {emp['full_name']},\n\nClick the link below to reset your password:\n{reset_url}\n\nIf you did not request this, please ignore this email."
                msg.html = html_body
                mail.send(msg)
                log_audit('PASSWORD_RESET', 'Auth', f'Reset link sent to {email}',
                          action_status='Success')
                current_app.logger.info('Password reset link sent to %s', email)
                # Development convenience: surface the link so local testing
                # does not depend on inbox delivery.
                if _dev_mode():
                    print(f"[PASSWORD RESET] {email} -> {reset_url}")
            except Exception as e:
                current_app.logger.error('Password reset email failed for %s: %s', email, e)
                print(f"[EMAIL] Failed to send password reset: {e}")
                log_audit('PASSWORD_RESET', 'Auth', f'Failed to send reset link to {email}',
                          action_status='Failed')
        else:
            # Anti-enumeration: the user-facing response stays identical, but
            # every attempt is audited internally with a masked address so a
            # genuinely broken flow is never invisible again. The audit status
            # CHECK only allows Success/Failed, so the skip is described in
            # the message (nothing failed — the lookup simply found no user).
            local, _, domain = email.partition('@')
            masked = (local[:1] + '*' * max(len(local) - 1, 0) + '@' + domain) if domain else '***'
            log_audit('PASSWORD_RESET_ATTEMPT', 'Auth',
                      f'Reset requested for unregistered email {masked} (no email sent)',
                      action_status='Success')

        flash('If that email is registered, a password reset link has been sent.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/forgot_password.html')


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    s = get_token_serializer()
    try:
        email = s.loads(token, max_age=3600)
    except Exception:
        flash('Password reset link is invalid or has expired.', 'danger')
        return redirect(url_for('auth.forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'danger')
            return render_template('auth/reset_password.html', token=token)
        if password != confirm:
            flash('Passwords do not match.', 'danger')
            return render_template('auth/reset_password.html', token=token)

        pw_hash = generate_password_hash(password)
        execute("UPDATE Employee SET password_hash=? WHERE LOWER(email)=? AND is_active=1",
                (pw_hash, email))
        log_audit('PASSWORD_RESET', 'Auth', f'Password reset completed for {email}',
                  action_status='Success')
        flash('Your password has been reset successfully. Please sign in.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/reset_password.html', token=token)


# ── Logout ─────────────────────────────────────────────────────────────────
@auth_bp.route('/logout')
def logout():
    user_name = session.get('user_name', 'Unknown')
    user_id   = session.get('user_id')
    log_audit('LOGOUT', 'Auth', f'{user_name} logged out',
              'Employee', user_id, 'Success')
    session.clear()
    flash('You have been logged out successfully.', 'success')
    return redirect(url_for('auth.login'))
