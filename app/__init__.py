"""
app/__init__.py  –  Flask application factory
"""
import os
from flask import Flask
from flask_mail import Mail

mail = Mail()

def create_app():
    # Since templates and static are outside the app/ module folder, specify their paths
    app = Flask(__name__, instance_relative_config=True,
                template_folder='../templates',
                static_folder='../static')
    # Cloudflare Quick Tunnels may forward a blueprint root URL without its
    # final slash. Accept both forms so Flask does not issue a canonical-slash
    # redirect that a proxy can loop back to the same request.
    app.url_map.strict_slashes = False

    # ── Secret key ────────────────────────────────────────────────────────
    # Production startup fails hard when SECRET_KEY is missing or still the
    # development fallback. The fallback is only accepted for local dev
    # (FLASK_DEBUG=true / FLASK_ENV=development).
    DEV_SECRET = 'smarthr-dev-secret-2026-change-in-prod'
    app.secret_key = os.environ.get('SECRET_KEY', DEV_SECRET)
    dev_mode = (os.environ.get('FLASK_DEBUG', '').lower() in ('true', '1', 'yes') or
                os.environ.get('FLASK_ENV', '').lower() == 'development')
    if app.secret_key == DEV_SECRET and not dev_mode:
        raise RuntimeError(
            "SECRET_KEY must be set in .env for production use. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )

    from datetime import timedelta
    # Security: Standard session timeout (inactivity)
    app.config['SESSION_PERMANENT'] = True
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=2)

    # Upload folder
    app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(app.root_path), 'uploads')
    app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'leave'), exist_ok=True)
    os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'resumes'), exist_ok=True)
    os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'contracts'), exist_ok=True)
    os.makedirs(app.instance_path, exist_ok=True)

    # ── Mail Configuration ─────────────────────────────────────────────────
    app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
    app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'
    app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
    app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
    app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@smarthr.my')
    app.config['MAIL_TIMEOUT'] = 15
    mail.init_app(app)

    # ── Session cookie hardening ───────────────────────────────────────────
    # HttpOnly + SameSite=Lax are ALWAYS on (defence-in-depth against XSS
    # cookie theft and cross-site request forgery at the cookie layer).
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
    )
    # SESSION_COOKIE_SECURE is opt-in: set FORCE_HTTPS_SESSION=true when the
    # app is served over HTTPS (TLS terminates at this process or at a proxy
    # with ProxyFix configured below).
    if os.environ.get('FORCE_HTTPS_SESSION', '').lower() in ('true', '1', 'yes'):
        app.config['SESSION_COOKIE_SECURE'] = True

    # ── Trusted proxy (optional) ───────────────────────────────────────────
    # When running behind a reverse proxy that sets X-Forwarded-For/Proto,
    # set TRUSTED_PROXY=true. The rate limiter then (and only then) honours
    # X-Forwarded-For; without it the header is ignored to prevent IP spoofing.
    trusted_proxy = os.environ.get('TRUSTED_PROXY', '').lower() in ('true', '1', 'yes')
    if trusted_proxy:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app,
                                x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # ── Register Blueprints ────────────────────────────────────────────────
    from app.auth.routes         import auth_bp
    from app.employees.routes    import emp_bp
    from app.organization.routes import org_bp
    from app.leave.routes        import leave_bp
    from app.attendance.routes   import att_bp
    from app.invoice.routes      import inv_bp
    from app.payroll.routes      import pay_bp
    from app.reports.routes      import rep_bp
    from app.audit.routes        import audit_bp
    from app.main.routes         import main_bp
    from app.settings.routes     import settings_bp
    from app.face.routes         import face_bp
    from app.notifications.routes import notif_bp
    from app.performance import perf_bp as performance_bp
    from app.recruitment.routes import recruit_bp
    from app.increment import increment_bp
    from app.bonus import bonus_bp
    from app.year_end import year_end_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(emp_bp)
    app.register_blueprint(org_bp)
    app.register_blueprint(leave_bp)
    app.register_blueprint(att_bp)
    app.register_blueprint(inv_bp)
    app.register_blueprint(pay_bp)
    app.register_blueprint(rep_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(face_bp)
    app.register_blueprint(notif_bp)
    app.register_blueprint(performance_bp)
    app.register_blueprint(recruit_bp)
    app.register_blueprint(increment_bp)
    app.register_blueprint(bonus_bp)
    app.register_blueprint(year_end_bp)

    from app.database import close_db
    app.teardown_appcontext(close_db)

    # ── Auto Payroll Scheduler (daily refresh of Draft payrolls) ───────────
    from app.payroll.autogen import start_payroll_scheduler
    start_payroll_scheduler(app)

    # ── Offer Expiry Scheduler (server-side, dev/test-fast interval) ───────
    from app.recruitment.offer_expiry import start_offer_expiry_scheduler
    start_offer_expiry_scheduler(app)

    # ── Rate Limiting ───────────────────────────────────────────────────────
    # Applied globally via before_request. Earlier this list mistakenly
    # SKIPPED auth.login / auth.forgot_password / auth.reset_password, leaving
    # the most brute-force-sensitive endpoints unprotected. They now get the
    # strictest limits of all.
    from app.rate_limiter import limiter

    _AUTH_LIMITS = {
        'auth.login':             (10, 60),    # 10 POSTs / minute / IP
        'auth.forgot_password':   (5, 60),     # 5  POSTs / minute / IP
        'auth.reset_password':    (5, 60),     # 5  POSTs / minute / IP
    }

    @app.before_request
    def apply_rate_limit():
        from flask import request, jsonify, current_app
        # Skip static files and the health endpoint only
        if request.endpoint in (None, 'static', 'health'):
            return
        if request.method == 'POST' and request.endpoint in _AUTH_LIMITS:
            limit, window = _AUTH_LIMITS[request.endpoint]
        else:
            limit, window = 200, 60
        allowed = limiter.is_allowed(limit=limit, window=window)
        if not allowed:
            current_app.logger.warning("Rate limit exceeded for %s on %s",
                                       limiter._get_client_ip(), request.endpoint)
            return jsonify({"error": "Too many requests. Please try again later."}), 429

    # ── CSRF Protection ─────────────────────────────────────────────────────
    # Every state-changing request is validated against the session token.
    # Set app.config['CSRF_ENABLED'] = False only in test fixtures that
    # deliberately exercise routes without CSRF.
    app.config.setdefault('CSRF_ENABLED', True)

    @app.before_request
    def apply_csrf_check():
        from flask import current_app, jsonify, request
        if not current_app.config.get('CSRF_ENABLED', True):
            return
        if request.endpoint in (None, 'static', 'health'):
            return
        from app.csrf import validate_csrf
        if not validate_csrf():
            return jsonify({"error": "CSRF token missing or invalid."}), 400

    # ── CSRF token available to every template (and minted in the session) ──
    from app.csrf import get_csrf_token

    @app.context_processor
    def inject_csrf_token():
        return dict(csrf_token=get_csrf_token())

    @app.context_processor
    def inject_notifications():
        from app.database import query
        from flask import session
        from app.recruitment.scoping import count_visible_new_applications
        if 'user_id' in session and 'company_id' in session:
            co = session['company_id']
            user_id = session['user_id']
            try:
                # Get pending leaves and invoices (for HR/Admin/Managers)
                pending_notifs = []
                user_role = session.get('user_role')
                branch_id = session.get('branch_id')
                if user_role in ['Admin','HR','HR Manager','Manager']:
                    leave_where = "la.status='Pending' AND e.company_id=?"
                    leave_params = [co]
                    invoice_where = "i.status='Pending' AND e.company_id=?"
                    invoice_params = [co]
                    if user_role == 'Manager' and branch_id:
                        leave_where += " AND e.branch_id=?"
                        leave_params.append(branch_id)
                        invoice_where += " AND e.branch_id=?"
                        invoice_params.append(branch_id)
                    pending_notifs_data = query("""
                        SELECT 'Leave' as type, 'LV-'||la.leave_id as ref, la.applied_at as dt,
                               e.full_name || ' requested leave' as message
                        FROM Leave_Application la
                        JOIN Employee e ON la.employee_id=e.employee_id
                        WHERE """ + leave_where + """
                        UNION ALL
                        SELECT 'Invoice' as type, COALESCE(i.invoice_number,'#'||i.invoice_id) as ref, i.submitted_at as dt,
                               e.full_name || ' submitted invoice' as message
                        FROM Invoice i
                        JOIN Employee e ON i.employee_id=e.employee_id
                        WHERE """ + invoice_where + """
                        ORDER BY dt DESC LIMIT 3
                    """, tuple(leave_params + invoice_params))
                    pending_notifs = [
                        {'type': r['type'], 'ref': r['ref'], 'dt': r['dt'], 'message': r['message']}
                        for r in pending_notifs_data
                    ]
                
                # Get unread user notifications from new Notification table
                unread_notif_rows = query("""
                    SELECT 'System' as type, title as ref, created_at as dt, 
                           type as notif_type, message, related_url, notification_id
                    FROM Notification 
                    WHERE employee_id=? AND is_read=0
                    ORDER BY created_at DESC LIMIT 5
                """, (user_id,))
                unread_system_notifs = [dict(r) for r in unread_notif_rows]
                
                # Pending approval notifications for Admin/HR/HR Manager
                approval_notifs = []
                user_role = session.get('user_role')
                if user_role in ('Admin', 'HR', 'HR Manager'):
                    approval_rows = query("""
                        SELECT 'Increment' as type, COUNT(*) as cnt,
                               MIN(COALESCE(si.reviewed_at, si.proposed_at)) as dt
                        FROM Salary_Increment si
                        JOIN Employee e ON si.employee_id=e.employee_id
                        WHERE si.status='Pending' AND e.company_id=?
                        UNION ALL
                        SELECT 'Bonus' as type, COUNT(*) as cnt,
                               MIN(COALESCE(bp.reviewed_at, bp.proposed_at)) as dt
                        FROM Bonus_Proposal bp
                        JOIN Employee e ON bp.employee_id=e.employee_id
                        WHERE bp.status='Pending' AND e.company_id=?
                        ORDER BY dt ASC
                    """, (co, co))
                    approval_notifs = [
                        {'type': 'Approval', 'ref': row['type'], 'dt': row['dt'],
                         'message': f"{row['cnt']} pending {row['type'].lower()}{'s' if row['cnt'] != 1 else ''} awaiting review"}
                        for row in approval_rows if row['cnt'] > 0
                    ][:3]
                    # Add pending applications
                    visible_new_apps = count_visible_new_applications(session)
                    if visible_new_apps > 0:
                        approval_notifs.append({
                            'type': 'Approval', 'ref': 'Application', 'dt': '',
                            'message': f"{visible_new_apps} pending application{'s' if visible_new_apps != 1 else ''} awaiting review"
                        })
                    # Add pending offers (sent contracts not yet accepted)
                    offers_row = query("""
                        SELECT COUNT(*) as cnt FROM Contract c
                        JOIN Job_Application ja ON c.application_id=ja.application_id
                        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
                        LEFT JOIN Branch b ON jp.branch_id=b.branch_id
                        WHERE c.status='Sent' AND (b.company_id=? OR jp.branch_id IS NULL)
                    """, (co,), one=True)
                    if offers_row and offers_row['cnt'] > 0:
                        approval_notifs.append({
                            'type': 'Approval', 'ref': 'Offer', 'dt': '',
                            'message': f"{offers_row['cnt']} pending offer{'s' if offers_row['cnt'] != 1 else ''} awaiting response"
                        })
                    # Add pending job posting requests
                    vac_row = query("""
                        SELECT COUNT(*) as cnt FROM Vacancy_Request vr
                        JOIN Employee e ON vr.requested_by=e.employee_id
                        WHERE vr.status='Pending' AND e.company_id=?
                    """, (co,), one=True)
                    if vac_row and vac_row['cnt'] > 0:
                        approval_notifs.append({
                            'type': 'Approval', 'ref': 'Job Posting Request', 'dt': '',
                            'message': f"{vac_row['cnt']} pending job posting request{'s' if vac_row['cnt'] != 1 else ''} awaiting review"
                        })
                
                # Combine them: approval/pending items first (pinned), then system notifications
                header_notifs = list(approval_notifs) + list(pending_notifs) + list(unread_system_notifs)
                header_notifs = header_notifs[:8]
                
                # Count pending items for Admin/HR/HR Manager
                pending_inc = 0
                pending_bonus = 0
                pending_applications = 0
                user_role = session.get('user_role')
                pending_leave = 0
                pending_invoice = 0
                pending_vacancy = 0
                pending_manual = 0
                # Dept manager: count pending vacancies in managed department
                managed_dept_id = session.get('managed_dept_id')
                is_dept_mgr = session.get('is_dept_manager', False)
                if is_dept_mgr and managed_dept_id:
                    vac_row = query("SELECT COUNT(*) as c FROM Vacancy_Request WHERE status='Pending' AND department_id=?", (managed_dept_id,), one=True)
                    pending_vacancy = vac_row['c'] if vac_row else 0
                if user_role in ('Admin', 'HR', 'HR Manager'):
                    inc_row = query("""
                        SELECT COUNT(*) as c FROM Salary_Increment si
                        JOIN Employee e ON si.employee_id=e.employee_id
                        WHERE si.status='Pending' AND e.company_id=?
                    """, (co,), one=True)
                    pending_inc = inc_row['c'] if inc_row else 0
                    vac_row = query("""
                        SELECT COUNT(*) as c FROM Vacancy_Request vr
                        JOIN Employee e ON vr.requested_by=e.employee_id
                        WHERE vr.status='Pending' AND e.company_id=?
                    """, (co,), one=True)
                    pending_vacancy = vac_row['c'] if vac_row else 0
                    bonus_row = query("""
                        SELECT COUNT(*) as c FROM Bonus_Proposal bp
                        JOIN Employee e ON bp.employee_id=e.employee_id
                        WHERE bp.status='Pending' AND e.company_id=?
                    """, (co,), one=True)
                    pending_bonus = bonus_row['c'] if bonus_row else 0
                    pending_applications = count_visible_new_applications(session)
                    leave_row = query("""
                        SELECT COUNT(*) as c FROM Leave_Application la
                        JOIN Employee e ON la.employee_id=e.employee_id
                        WHERE la.status='Pending' AND e.company_id=?
                    """, (co,), one=True)
                    pending_leave = leave_row['c'] if leave_row else 0
                    invoice_row = query("""
                        SELECT COUNT(*) as c FROM Invoice i
                        JOIN Employee e ON i.employee_id=e.employee_id
                        WHERE i.status='Pending' AND e.company_id=?
                    """, (co,), one=True)
                    pending_invoice = invoice_row['c'] if invoice_row else 0
                    manual_row = query("""
                        SELECT COUNT(*) as c FROM Attendance a
                        JOIN Employee e ON a.employee_id=e.employee_id
                        WHERE a.is_manual_entry=1 AND a.status='Pending' AND e.company_id=?
                    """, (co,), one=True)
                    pending_manual = manual_row['c'] if manual_row else 0
                elif user_role == 'Manager' and branch_id:
                    leave_row = query("""
                        SELECT COUNT(*) as c FROM Leave_Application la
                        JOIN Employee e ON la.employee_id=e.employee_id
                        WHERE la.status='Pending' AND e.company_id=? AND e.branch_id=?
                    """, (co, branch_id), one=True)
                    pending_leave = leave_row['c'] if leave_row else 0
                    invoice_row = query("""
                        SELECT COUNT(*) as c FROM Invoice i
                        JOIN Employee e ON i.employee_id=e.employee_id
                        WHERE i.status='Pending' AND e.company_id=? AND e.branch_id=?
                    """, (co, branch_id), one=True)
                    pending_invoice = invoice_row['c'] if invoice_row else 0
                    pending_applications = count_visible_new_applications(session)
                    
                return dict(
                    header_notifications=header_notifs,
                    has_unread_notifications=len(header_notifs) > 0,
                    pending_increment_count=pending_inc,
                    pending_bonus_count=pending_bonus,
                    pending_applications_count=pending_applications,
                    pending_leave_count=pending_leave,
                    pending_invoice_count=pending_invoice,
                    pending_vacancy_count=pending_vacancy,
                    pending_manual_count=pending_manual
                )
            except Exception as e:
                print(f"Error loading notifications: {e}")
                return dict(header_notifications=[], has_unread_notifications=False, pending_increment_count=0, pending_bonus_count=0, pending_applications_count=0, pending_leave_count=0, pending_invoice_count=0, pending_vacancy_count=0, pending_manual_count=0)
        return dict(header_notifications=[], has_unread_notifications=False, pending_increment_count=0, pending_bonus_count=0, pending_applications_count=0, pending_leave_count=0, pending_invoice_count=0, pending_vacancy_count=0, pending_manual_count=0)

    @app.route('/health')
    def health():
        try:
            from app.database import get_db
            db = get_db()
            db.execute('SELECT 1')
            return {'status': 'ok', 'database': 'connected'}
        except Exception as e:
            return {'status': 'error', 'detail': str(e)}, 500

    return app
