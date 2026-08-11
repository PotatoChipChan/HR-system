from flask import Blueprint, jsonify, session, request, render_template, flash, redirect, url_for
from app.database import query, execute, log_audit
from app.auth.routes import login_required, role_required

notif_bp = Blueprint('notifications', __name__, url_prefix='/notifications')

@notif_bp.route('/')
@login_required
def index():
    uid = session['user_id']
    notifs = query("""
        SELECT * FROM Notification
        WHERE employee_id = ?
        ORDER BY created_at DESC
        LIMIT 20
    """, (uid,))
    return jsonify(notifs)

@notif_bp.route('/unread-count')
@login_required
def unread_count():
    uid = session['user_id']
    count = query("""
        SELECT COUNT(*) as cnt FROM Notification
        WHERE employee_id = ? AND is_read = 0
    """, (uid,), one=True)
    return jsonify({"count": count['cnt']})

@notif_bp.route('/mark-read/<int:notif_id>', methods=['POST'])
@login_required
def mark_read(notif_id):
    uid = session['user_id']
    execute("""
        UPDATE Notification
        SET is_read = 1
        WHERE notification_id = ? AND employee_id = ?
    """, (notif_id, uid))
    log_audit('MARK_NOTIF_READ', 'Notifications', f"Marked notification {notif_id} as read")
    return jsonify({"success": True})

@notif_bp.route('/mark-all-read', methods=['POST'])
@login_required
def mark_all_read():
    uid = session['user_id']
    execute("""
        UPDATE Notification
        SET is_read = 1
        WHERE employee_id = ? AND is_read = 0
    """, (uid,))
    log_audit('MARK_ALL_NOTIFS_READ', 'Notifications', "Marked all notifications as read")
    return jsonify({"success": True})

@notif_bp.route('/email-config', methods=['GET', 'POST'])
@role_required('Admin', 'HR')
def email_config():
    if request.method == 'POST':
        f = request.form
        cfg = query("SELECT config_id FROM Email_Config WHERE is_active=1 LIMIT 1", one=True)
        if cfg:
            execute("""UPDATE Email_Config SET host=?, port=?, username=?, email=?
                       WHERE config_id=?""",
                    (f['host'], int(f['port']), f['username'], f['email'], cfg['config_id']))
        else:
            execute("""INSERT INTO Email_Config (email, provider, host, port, username, is_active)
                       VALUES (?, 'IMAP', ?, ?, ?, 1)""",
                    (f['email'], f['host'], int(f['port']), f['username']))
        if f.get('password'):
            import os
            env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), '.env')
            try:
                with open(env_path, 'r') as fh:
                    lines = fh.readlines()
                with open(env_path, 'w') as fh:
                    for line in lines:
                        if line.startswith('MAIL_PASSWORD='):
                            fh.write(f"MAIL_PASSWORD={f['password']}\n")
                        else:
                            fh.write(line)
                os.environ['MAIL_PASSWORD'] = f['password']
            except Exception as e:
                print(f"[EMAIL CONFIG] Failed to update .env: {e}")
        flash('Email configuration saved.', 'success')
        return redirect(url_for('notifications.email_config'))

    import os
    config = {
        'email': os.environ.get('MAIL_USERNAME', ''),
        'host': os.environ.get('IMAP_HOST', 'imap.gmail.com'),
        'port': os.environ.get('IMAP_PORT', '993'),
        'username': os.environ.get('MAIL_USERNAME', ''),
        'password': os.environ.get('MAIL_PASSWORD', ''),
    }
    return render_template('notifications/email_config.html', config=config)


def send_notification(employee_id, title, message, type='Info', related_url=None, extra_context=None):
    """Helper function to send a notification to an employee (in-app + email)"""
    execute("""
        INSERT INTO Notification (employee_id, title, message, type, related_url)
        VALUES (?, ?, ?, ?, ?)
    """, (employee_id, title, message, type, related_url))
    try:
        from app.notifications.email_service import send_email_notification
        send_email_notification(employee_id, title, message, extra_context)
    except Exception as e:
        print(f"[EMAIL] Failed to send email notification: {e}")


def send_in_app_notification(employee_id, title, message, type='Info', related_url=None):
    """In-app notification only (no email). Intentionally separate from
    send_notification so recruitment flows never trigger email side-effects."""
    execute("""
        INSERT INTO Notification (employee_id, title, message, type, related_url)
        VALUES (?, ?, ?, ?, ?)
    """, (employee_id, title, message, type, related_url))


def send_in_app_to_company(company_id, roles, title, message, type='Info', related_url=None,
                           exclude_employee_id=None):
    """Send an in-app notification to every active employee of the given company
    whose role is in `roles`. Failures for one recipient never block the others
    and never raise -- a notification DB error must not fail the caller."""
    placeholders = ','.join('?' for _ in roles)
    params = list(roles) + [company_id]
    filter_excl = ' AND e.employee_id != ?' if exclude_employee_id else ''
    if exclude_employee_id:
        params.append(exclude_employee_id)
    employees = query(
        f"""SELECT e.employee_id FROM Employee e
             JOIN Role r ON e.role_id = r.role_id
             WHERE r.role_name IN ({placeholders})
               AND e.is_active = 1
               AND e.company_id = ?
               {filter_excl}""",
        params
    )
    for emp in employees:
        try:
            send_in_app_notification(emp['employee_id'], title, message, type, related_url)
        except Exception as ex:
            print(f"[NOTIFY] Failed for employee {emp['employee_id']}: {ex}")


def send_notification_to_role(roles, title, message, type='Info', related_url=None):
    """Send notification to all employees with specified role(s)."""
    placeholders = ','.join('?' for _ in roles)
    employees = query(
        f"""SELECT e.employee_id FROM Employee e
             JOIN Role r ON e.role_id = r.role_id
             WHERE r.role_name IN ({placeholders}) AND e.is_active = 1""",
        roles
    )
    for emp in employees:
        send_notification(emp['employee_id'], title, message, type, related_url)
