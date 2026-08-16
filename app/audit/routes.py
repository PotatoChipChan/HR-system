"""app/audit/routes.py – Audit log viewer"""
from flask import Blueprint, render_template, request, session
from app.database import query, execute
from app.auth.routes import role_required

audit_bp = Blueprint('audit', __name__, url_prefix='/audit')


@audit_bp.route('/')
@role_required('Admin', 'HR')
def index():
    module  = request.args.get('module', '')
    action  = request.args.get('action', '')
    status  = request.args.get('status', '')
    date_from = request.args.get('from', '')
    date_to   = request.args.get('to', '')
    search    = request.args.get('q', '')
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    per_page  = 20

    sql  = """SELECT al.*, e.full_name FROM AuditLog al
              LEFT JOIN Employee e ON al.employee_id=e.employee_id WHERE 1=1"""
    args = []

    if module:
        sql += " AND al.module_name=?"; args.append(module)
    if action:
        sql += " AND al.action=?"; args.append(action)
    if status:
        sql += " AND al.action_status=?"; args.append(status)
    if date_from:
        sql += " AND date(al.created_at)>=?"; args.append(date_from)
    if date_to:
        sql += " AND date(al.created_at)<=?"; args.append(date_to)
    if search:
        sql += " AND (e.full_name LIKE ? OR al.description LIKE ? OR al.target_record_id LIKE ?)"
        args += [f'%{search}%', f'%{search}%', f'%{search}%']

    total = query(f"SELECT COUNT(*) as c FROM ({sql})", args, one=True)['c']
    total_pages = (total + per_page - 1) // per_page
    if total_pages:
        page = min(page, total_pages)
    offset = (page - 1) * per_page
    sql  += " ORDER BY al.created_at DESC LIMIT ? OFFSET ?"
    args += [per_page, offset]
    logs  = query(sql, args)

    # Stats for today
    today_stats = query("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN action_status='Failed' THEN 1 ELSE 0 END) as failed,
               SUM(CASE WHEN is_archived=1 THEN 1 ELSE 0 END) as archived
        FROM AuditLog WHERE date(created_at)=date('now')
    """, one=True)

    modules = query("SELECT DISTINCT module_name FROM AuditLog ORDER BY module_name")
    actions = query("SELECT DISTINCT action FROM AuditLog ORDER BY action")
    page_numbers = []
    if total_pages:
        start = max(1, page - 2)
        end = min(total_pages, page + 2)
        if start > 1:
            page_numbers.append(1)
            if start > 2:
                page_numbers.append(None)
        page_numbers.extend(range(start, end + 1))
        if end < total_pages:
            if end < total_pages - 1:
                page_numbers.append(None)
            page_numbers.append(total_pages)

    return render_template('audit_log.html',
                           logs=logs, today_stats=today_stats,
                           modules=modules, actions=actions,
                           module=module, action=action, status=status,
                           date_from=date_from, date_to=date_to, search=search,
                           page=page, total_pages=total_pages, total=total,
                           page_numbers=page_numbers)
