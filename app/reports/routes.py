from flask import Blueprint, render_template, request, session, make_response, flash, redirect, url_for
from app.database import query, log_audit
from app.auth.routes import login_required, role_required
import csv
import io
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

rep_bp = Blueprint('reports', __name__, url_prefix='/reports')


def _filter_int(raw, default, *, minimum=None, maximum=None):
    """Parse an optional report filter without turning malformed URLs into 500s."""
    if raw in (None, ''):
        return default, True
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default, False
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        return default, False
    return value, True


@rep_bp.route('/')
@role_required('Admin', 'HR', 'Manager')
def index():
    co    = session['company_id']
    rtype = request.args.get('type', 'headcount')
    month, valid_month = _filter_int(request.args.get('month', ''), 0, minimum=0, maximum=12)
    year, valid_year = _filter_int(request.args.get('year', ''), 2026, minimum=1, maximum=9999)
    dept_id, valid_dept = _filter_int(request.args.get('dept', ''), None, minimum=1)
    branch_id, valid_branch = _filter_int(request.args.get('branch', ''), None, minimum=1)
    if not all((valid_month, valid_year, valid_dept, valid_branch)):
        flash('Invalid report filter ignored.', 'warning')
    dept = str(dept_id) if dept_id is not None else ''
    branch = str(branch_id) if branch_id is not None else ''
    role  = session['user_role']
    bid   = session.get('branch_id')

    if role == 'Manager' and rtype in ('invoice', 'payroll'):
        flash('Access denied to financial/payroll reports.', 'danger')
        return redirect(url_for('reports.index', type='headcount'))

    data = {}
    if rtype == 'headcount':
        if role == 'Manager':
            data['rows'] = query("""
                SELECT d.department_name, b.name as branch_name,
                       COUNT(e.employee_id) as total,
                       SUM(CASE WHEN e.employment_type='Full-Time' THEN 1 ELSE 0 END) as ft,
                       SUM(CASE WHEN e.employment_type='Part-Time' THEN 1 ELSE 0 END) as pt,
                       SUM(CASE WHEN e.employment_type='Contract'  THEN 1 ELSE 0 END) as ct
                FROM Employee e
                JOIN Department d ON e.department_id=d.department_id
                JOIN Branch b ON e.branch_id=b.branch_id
                WHERE e.company_id=? AND e.branch_id=? AND e.is_active=1
                GROUP BY d.department_id ORDER BY d.department_name
            """, (co, bid))
        else:
            data['rows'] = query("""
                SELECT d.department_name, b.name as branch_name,
                       COUNT(e.employee_id) as total,
                       SUM(CASE WHEN e.employment_type='Full-Time' THEN 1 ELSE 0 END) as ft,
                       SUM(CASE WHEN e.employment_type='Part-Time' THEN 1 ELSE 0 END) as pt,
                       SUM(CASE WHEN e.employment_type='Contract'  THEN 1 ELSE 0 END) as ct
                FROM Employee e
                JOIN Department d ON e.department_id=d.department_id
                JOIN Branch b ON e.branch_id=b.branch_id
                WHERE e.company_id=? AND e.is_active=1
                GROUP BY d.department_id ORDER BY d.department_name
            """, (co,))

    elif rtype == 'attendance':
        sql = """SELECT e.full_name, d.department_name, b.name as branch_name,
                        COUNT(a.attendance_id) as days_attended,
                        ROUND(SUM(a.hours_worked),2) as total_hours,
                        ROUND(SUM(a.overtime_hours),2) as ot_hours,
                        SUM(CASE WHEN a.is_manual_entry=0 THEN 1 ELSE 0 END) as biometric_entries,
                        SUM(CASE WHEN a.is_manual_entry=1 THEN 1 ELSE 0 END) as manual_entries,
                        ROUND(AVG(a.confidence_score),1) as avg_confidence
                 FROM Attendance a JOIN Employee e ON a.employee_id=e.employee_id
                 JOIN Department d ON e.department_id=d.department_id
                 JOIN Branch b ON e.branch_id=b.branch_id
                 WHERE e.company_id=?
                 AND strftime('%Y',a.check_in)=?"""
        args = [co, str(year)]
        if month:
            sql += " AND strftime('%m',a.check_in)=?"
            args.append(f'{month:02d}')
        if role == 'Manager':
            sql += " AND e.branch_id=?"; args.append(bid)
        if dept_id is not None:
            sql += " AND e.department_id=?"; args.append(int(dept))
        if branch_id is not None and role != 'Manager':
            sql += " AND e.branch_id=?"; args.append(int(branch))
        sql += " GROUP BY e.employee_id ORDER BY e.full_name"
        data['rows'] = query(sql, args)

    elif rtype == 'attendance_detail':
        sql = """SELECT e.full_name, d.department_name, b.name as branch_name,
                        date(a.check_in) as att_date,
                        a.check_in, a.check_out,
                        ROUND(a.hours_worked,2) as hours_worked,
                        ROUND(a.overtime_hours,2) as overtime_hours,
                        CASE WHEN a.is_manual_entry=1 THEN 'Manual' ELSE 'Biometric' END as entry_method,
                        a.confidence_score, a.status
                 FROM Attendance a JOIN Employee e ON a.employee_id=e.employee_id
                 JOIN Department d ON e.department_id=d.department_id
                 JOIN Branch b ON e.branch_id=b.branch_id
                 WHERE e.company_id=?
                 AND strftime('%Y',a.check_in)=?"""
        args = [co, str(year)]
        if month:
            sql += " AND strftime('%m',a.check_in)=?"
            args.append(f'{month:02d}')
        if role == 'Manager':
            sql += " AND e.branch_id=?"; args.append(bid)
        if dept_id is not None:
            sql += " AND e.department_id=?"; args.append(int(dept))
        if branch_id is not None and role != 'Manager':
            sql += " AND e.branch_id=?"; args.append(int(branch))
        sql += " ORDER BY a.check_in DESC"
        data['rows'] = query(sql, args)

    elif rtype == 'leave':
        if role == 'Manager':
            data['rows'] = query("""
                SELECT e.full_name, lt.type_name,
                       SUM(la.total_days) as total_days, la.status
                FROM Leave_Application la
                JOIN Employee e ON la.employee_id=e.employee_id
                JOIN Leave_Type lt ON la.leave_type_id=lt.leave_type_id
                WHERE e.company_id=? AND e.branch_id=? AND strftime('%Y',la.applied_at)=?
                GROUP BY e.employee_id, la.leave_type_id, la.status
                ORDER BY e.full_name
            """, (co, bid, str(year)))
        else:
            data['rows'] = query("""
                SELECT e.full_name, lt.type_name,
                       SUM(la.total_days) as total_days, la.status
                FROM Leave_Application la
                JOIN Employee e ON la.employee_id=e.employee_id
                JOIN Leave_Type lt ON la.leave_type_id=lt.leave_type_id
                WHERE e.company_id=? AND strftime('%Y',la.applied_at)=?
                GROUP BY e.employee_id, la.leave_type_id, la.status
                ORDER BY e.full_name
            """, (co, str(year)))

    elif rtype == 'invoice':
        inv_sql = """SELECT e.full_name, i.vendor_name, i.category,
                   i.total_amount, i.status, i.submitted_at
            FROM Invoice i JOIN Employee e ON i.employee_id=e.employee_id
            WHERE e.company_id=?
            AND strftime('%Y',i.submitted_at)=?"""
        inv_args = [co, str(year)]
        if month:
            inv_sql += " AND strftime('%m',i.submitted_at)=?"
            inv_args.append(f'{month:02d}')
        inv_sql += " ORDER BY i.submitted_at DESC"
        data['rows'] = query(inv_sql, inv_args)

    elif rtype == 'payroll':
        pr_sql = """SELECT e.full_name, d.department_name,
                   p.base_salary, p.gross_pay, p.total_deductions, p.net_pay, p.status
            FROM Payroll p JOIN Employee e ON p.employee_id=e.employee_id
            JOIN Department d ON e.department_id=d.department_id
            WHERE e.company_id=? AND p.pay_period_year=?"""
        pr_args = [co, str(year)]
        if month:
            pr_sql += " AND p.pay_period_month=?"
            pr_args.append(month)
        pr_sql += " ORDER BY e.full_name"
        data['rows'] = query(pr_sql, pr_args)

    if role == 'Manager':
        departments = query("SELECT d.* FROM Department d JOIN Branch b ON d.branch_id=b.branch_id WHERE b.company_id=? AND d.branch_id=? ORDER BY d.department_name", (co, bid))
        branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? AND branch_id=? ORDER BY name", (co, bid))
        branch = str(bid)
    else:
        departments = query("SELECT d.* FROM Department d JOIN Branch b ON d.branch_id=b.branch_id WHERE b.company_id=? ORDER BY d.department_name", (co,))
        branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? ORDER BY name", (co,))
        branch = str(branch_id) if branch_id is not None else ''
        
    log_audit('GENERATE_REPORT', 'Reports', f'Generated {rtype} report', action_details={'type': rtype, 'month': month, 'year': year})

    export = request.args.get('export')
    if export == 'csv' and data.get('rows'):
        output = io.StringIO()
        writer = csv.writer(output)
        # Header
        if data['rows']:
            writer.writerow(data['rows'][0].keys())
        # Rows
        for row in data['rows']:
            writer.writerow(dict(row).values())
        
        response = make_response(output.getvalue())
        response.headers["Content-Disposition"] = f"attachment; filename={rtype}_report_{year}_{month}.csv"
        response.headers["Content-type"] = "text/csv"
        return response

    elif export == 'pdf' and data.get('rows'):
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(letter))
        elements = []
        styles = getSampleStyleSheet()
        
        title = f"{rtype.capitalize()} Report - {month}/{year}"
        elements.append(Paragraph(title, styles['Title']))
        elements.append(Spacer(1, 12))
        
        if data['rows']:
            row_dict = dict(data['rows'][0])
            headers = list(row_dict.keys())
            table_data = [headers]
            for row in data['rows']:
                row_dict = dict(row)
                table_data.append([str(v) for v in row_dict.values()])
            
            t = Table(table_data)
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                ('GRID', (0, 0), (-1, -1), 1, colors.black)
            ]))
            elements.append(t)
        
        doc.build(elements)
        response = make_response(buffer.getvalue())
        response.headers["Content-Disposition"] = f"attachment; filename={rtype}_report_{year}_{month}.pdf"
        response.headers["Content-type"] = "application/pdf"
        return response

    return render_template('reports.html', data=data, rtype=rtype,
                           month=month, year=year, dept=dept, branch=branch,
                           departments=departments, branches=branches)
