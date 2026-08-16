"""app/payroll/routes.py – Payroll list, payslip view, and PDF download"""
from flask import (Blueprint, render_template, request, session,
                   flash, redirect, url_for, make_response, current_app)
from app.database import query, execute, log_audit
from app.auth.routes import login_required, role_required
from app.notifications.routes import send_notification
from app.payroll.calculator import (calculate_epf, calculate_socso, calculate_eis, 
                                     calculate_pcb, calculate_proration, calculate_ot_or_leave)
import datetime
import io
import json

pay_bp = Blueprint('payroll', __name__, url_prefix='/payroll')


def _parse_period(month_raw, year_raw, *, month_required=False):
    """Validate payroll period fields before they reach integer conversion or SQL."""
    try:
        month = int(month_raw) if month_raw not in (None, '') else 0
        year = int(year_raw) if year_raw not in (None, '') else datetime.date.today().year
    except (TypeError, ValueError):
        return None, None
    if year < 1 or year > 9999 or month < 0 or month > 12 or (month_required and not month):
        return None, None
    return month, year


@pay_bp.route('/')
@login_required
def list_payroll():
    uid  = session['user_id']
    role = session['user_role']
    co   = session['company_id']
    if 'month' in request.args:
        month, year = _parse_period(request.args.get('month', ''), request.args.get('year', ''))
    else:
        # Default the list to the current month; "All Year" is opt-in.
        month, year = datetime.date.today().month, datetime.date.today().year
    if month is None:
        flash('Use a valid payroll month and year filter.', 'danger')
        month, year = 0, datetime.date.today().year

    search_name = request.args.get('name', '').strip()
    search_branch = request.args.get('branch', '').strip()
    search_dept = request.args.get('dept', '').strip()
    search_pay = request.args.get('pay', '').strip()

    branches = []
    departments = []

    if role in ('Admin', 'HR', 'HR Manager', 'Manager'):
        if role == 'Manager':
            branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? AND branch_id=?", (co, session['branch_id']))
            departments = query("SELECT department_id, branch_id, department_name FROM Department WHERE branch_id=?", (session['branch_id'],))
        else:
            branches = query("SELECT branch_id, name FROM Branch WHERE company_id=?", (co,))
            departments = query("SELECT department_id, branch_id, department_name FROM Department WHERE branch_id IN (SELECT branch_id FROM Branch WHERE company_id=?)", (co,))

        sql = """SELECT p.*, e.full_name, e.position, d.department_name 
                 FROM Payroll p
                 JOIN Employee e ON p.employee_id=e.employee_id
                 JOIN Department d ON e.department_id=d.department_id
                 LEFT JOIN Branch b ON e.branch_id=b.branch_id
                 WHERE e.company_id=? AND p.pay_period_year=?"""
        args = [co, year]
        if month:
            sql += " AND p.pay_period_month=?"
            args.append(month)
        
        if role == 'Manager':
            sql += " AND e.branch_id = ?"
            args.append(session['branch_id'])

        if search_name:
            sql += " AND e.full_name LIKE ?"
            args.append(f"%{search_name}%")
        if search_branch and role != 'Manager':
            sql += " AND e.branch_id = ?"
            args.append(search_branch)
        if search_dept:
            sql += " AND e.department_id = ?"
            args.append(search_dept)
            
        if search_pay == 'asc':
            sql += " ORDER BY p.net_pay ASC"
        elif search_pay == 'desc':
            sql += " ORDER BY p.net_pay DESC"
        else:
            sql += " ORDER BY p.payroll_id DESC"
            
        payrolls = query(sql, args)
        
        if role == 'Manager':
            s_sql = """SELECT COUNT(*) as count, SUM(gross_pay) as total_gross,
                              SUM(total_deductions) as total_ded, SUM(net_pay) as total_net
                       FROM Payroll p JOIN Employee e ON p.employee_id=e.employee_id
                       WHERE e.company_id=? AND e.branch_id=? AND p.pay_period_year=?"""
            s_args = [co, session['branch_id'], year]
            if month:
                s_sql += " AND p.pay_period_month=?"
                s_args.append(month)
            summary = query(s_sql, s_args, one=True)
        else:
            s_sql = """SELECT COUNT(*) as count, SUM(gross_pay) as total_gross,
                              SUM(total_deductions) as total_ded, SUM(net_pay) as total_net
                       FROM Payroll p JOIN Employee e ON p.employee_id=e.employee_id
                       WHERE e.company_id=? AND p.pay_period_year=?"""
            s_args = [co, year]
            if month:
                s_sql += " AND p.pay_period_month=?"
                s_args.append(month)
            summary = query(s_sql, s_args, one=True)
    else:
        e_sql = """SELECT p.*, e.full_name, e.position, d.department_name
                   FROM Payroll p
                   JOIN Employee e ON p.employee_id=e.employee_id
                   JOIN Department d ON e.department_id=d.department_id
                   WHERE p.employee_id=? AND p.pay_period_year=?"""
        e_args = [uid, year]
        if month:
            e_sql += " AND p.pay_period_month=?"
            e_args.append(month)
        payrolls = query(e_sql, e_args)
        summary = None

    return render_template('payroll/list.html',
                           payrolls=payrolls, summary=summary,
                           month=month, year=year,
                           branches=branches, departments=departments)


@pay_bp.route('/generate', methods=['POST'])
@role_required('Admin', 'HR')
def generate_payroll():
    uid  = session['user_id']
    co   = session['company_id']
    month, year = _parse_period(request.form.get('month'), request.form.get('year'), month_required=True)
    if month is None:
        flash('Use a valid payroll month and year.', 'danger')
        return redirect(url_for('payroll.list_payroll'))

    # Find employees who already have Finalised or Paid payrolls for this month
    finalised_records = query("""SELECT p.employee_id FROM Payroll p
                                 JOIN Employee e ON p.employee_id=e.employee_id
                                 WHERE e.company_id=? AND p.pay_period_month=? AND p.pay_period_year=?
                                 AND p.status IN ('Finalised', 'Paid')""",
                              (co, month, year))
    finalised_eids = {r['employee_id'] for r in finalised_records} if finalised_records else set()

    from app.payroll.helpers import increment_landing_map
    employees = query("SELECT * FROM Employee WHERE company_id=? AND is_active=1", (co,))
    landing_map = increment_landing_map(co, employees, datetime.date.today())

    # Unlink existing Draft invoices
    execute("""UPDATE Invoice SET payroll_id = NULL 
               WHERE payroll_id IN (
                   SELECT payroll_id FROM Payroll 
                   WHERE pay_period_month=? AND pay_period_year=? AND status='Draft'
                   AND employee_id IN (SELECT employee_id FROM Employee WHERE company_id=?)
               )""", (month, year, co))

    # Delete existing Drafts so we can regenerate fresh
    execute("""DELETE FROM Payroll 
               WHERE pay_period_month=? AND pay_period_year=? AND status='Draft'
               AND employee_id IN (SELECT employee_id FROM Employee WHERE company_id=?)""",
            (month, year, co))

    count = 0

    for emp in employees:
        eid = emp['employee_id']
        
        # Skip if employee already has a finalised payroll
        if eid in finalised_eids:
            continue

        # ---- Salary Increment: new salary from the landing month; the
        # increment line shows only on the landing month itself ----
        land = landing_map.get(eid)
        if land:
            inc = land[2]
            if (year, month) == (land[1], land[0]):
                effective_salary = inc['new_salary']
                salary_inc_amt = inc['new_salary'] - inc['old_salary']
            elif (year, month) > (land[1], land[0]):
                effective_salary = inc['new_salary']
                salary_inc_amt = 0.0
            else:
                effective_salary = inc['old_salary']
                salary_inc_amt = 0.0
        else:
            effective_salary = emp['base_salary']
            salary_inc_amt = 0.0

        # Apply Proration
        base = calculate_proration(effective_salary, emp['hire_date'], month, year)

        # Get attendance hours for the month
        att = query("""SELECT SUM(overtime_hours) as ot
                       FROM Attendance
                       WHERE employee_id=? AND strftime('%m', check_in)=? AND strftime('%Y', check_in)=?
                       AND status='Approved'""",
                    (eid, f"{month:02d}", str(year)), one=True)
        ot_hours = att['ot'] or 0
        
        # Get ALL outstanding approved invoice claims for the employee
        claims = query("""SELECT SUM(COALESCE(total_amount_myr, total_amount)) as total
                          FROM Invoice
                          WHERE employee_id=? AND status='Approved' AND payroll_id IS NULL""",
                       (eid,), one=True)
        invoice_claims = claims['total'] or 0.0

        # Calculate OT or Replacement Leave
        ot_pay, ot_type = calculate_ot_or_leave(base, ot_hours)

        # ---- Bonus: use approved Bonus_Proposal if available ----
        bonus_prop = query("""
            SELECT bonus_amount FROM Bonus_Proposal
            WHERE employee_id=? AND period_month=? AND period_year=?
              AND status='Approved'
            LIMIT 1
        """, (eid, month, year), one=True)

        if bonus_prop:
            bonus = round(bonus_prop['bonus_amount'], 2)
        else:
            # Fallback: auto-calculate from Performance_Score
            perf = query("""
                SELECT composite_score FROM Performance_Score
                WHERE employee_id=? AND period_month=? AND period_year=?
            """, (eid, month, year), one=True)
            if perf and perf['composite_score']:
                bonus_rate = min(0.10, max(0.0, (perf['composite_score'] / 100) * 0.10))
                bonus = round(base * bonus_rate, 2)
            else:
                bonus = 0.0
        
        # Leave adjustment: Convert unused leave days to monetary value (RM 200/day)
        leave_days = query("SELECT SUM(pending_days) as pending FROM Leave_Balance WHERE employee_id=? AND year=?", 
                          (eid, year), one=True)
        pending_leave = leave_days['pending'] or 0.0
        leave_adjustment = round(pending_leave * 200, 2)

        # Basic deductions
        gross = base + ot_pay + invoice_claims + bonus
        
        # Statutory
        epf_e, epf_er     = calculate_epf(gross)
        socso_e, socso_er = calculate_socso(gross)
        eis_e, eis_er     = calculate_eis(gross)
        pcb               = calculate_pcb(gross)

        total_ded = epf_e + socso_e + eis_e + pcb
        net = round(gross - total_ded, 2)

        # Append replacement leave note
        note = f"OT calculated as {ot_type}"
        if ot_type == "REPLACEMENT_LEAVE":
            note = f"OT of {ot_hours} hrs converted to Replacement Leave"
        if salary_inc_amt > 0:
            note += f"; Salary increment: RM {salary_inc_amt:,.2f}"
        if bonus > 0:
            note += f"; Bonus: {bonus}"
        if leave_adjustment > 0:
            note += f"; Leave adjustment: {leave_adjustment}"

        pid = execute("""
            INSERT INTO Payroll
            (employee_id, pay_period_month, pay_period_year, base_salary, overtime_pay,
             commission, bonus, invoice_claims, leave_adjustment, gross_pay,
             epf_employee, epf_employer, socso_employee, socso_employer,
             eis_employee, eis_employer, pcb_tax, total_deductions, net_pay,
             status, generated_by, generated_at, notes, salary_increment)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (eid, month, year, base, ot_pay, 0, bonus, invoice_claims, leave_adjustment, gross,
              epf_e, epf_er, socso_e, socso_er, eis_e, eis_er, pcb, total_ded, net,
              'Draft', uid, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), note, salary_inc_amt))
              
        if invoice_claims > 0:
            execute("""UPDATE Invoice SET payroll_id=? 
                       WHERE employee_id=? AND status='Approved' AND payroll_id IS NULL""",
                    (pid, eid))
        
        count += 1

    log_audit('GENERATE_PAYROLL', 'Payroll', f'Generated payroll for {month}/{year}',
              action_details={'month': month, 'year': year, 'count': count})
              
    if len(finalised_eids) > 0:
        flash('finalised record found, non-finalised payroll generated', 'warning')
    else:
        flash(f'Successfully generated {count} payroll records for {month}/{year}.', 'success')
        
    return redirect(url_for('payroll.list_payroll', month=month, year=year))


@pay_bp.route('/finalise/<int:pid>', methods=['POST'])
@role_required('Admin', 'HR')
def finalise(pid):
    # Get payroll info before updating
    p = query("SELECT p.*, e.full_name FROM Payroll p JOIN Employee e ON p.employee_id=e.employee_id WHERE p.payroll_id=?", (pid,), one=True)
    if not p:
        flash('Payroll record not found.', 'danger')
        return redirect(url_for('payroll.list_payroll'))

    # Mark associated invoices as Paid
    execute("UPDATE Invoice SET status='Paid' WHERE payroll_id=?", (pid,))

    execute("UPDATE Payroll SET status='Finalised' WHERE payroll_id=?", (pid,))
    log_audit('FINALISE', 'Payroll', f'Finalised payroll id={pid}', 'Payroll', pid)

    month_names = ['', 'January', 'Febuary', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
    period = f"{month_names[p['pay_period_month']]} {p['pay_period_year']}"
    send_notification(
        p['employee_id'],
        'Payslip Ready',
        f"Your payslip for {period} is ready. Net pay: RM {p['net_pay']:,.2f}",
        'Success',
        url_for('payroll.view_payslip', pid=pid)
    )

    flash('Payroll finalised, invoices marked as Paid, and employee notified.', 'success')
    return redirect(url_for('payroll.list_payroll'))


@pay_bp.route('/bulk-finalise', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager')
def bulk_finalise():
    payroll_ids = request.form.getlist('payroll_ids')
    if not payroll_ids:
        flash('No payroll records selected.', 'warning')
        return redirect(url_for('payroll.list_payroll'))

    month, year = _parse_period(request.form.get('month'), request.form.get('year'), month_required=True)
    if month is None:
        flash('Use a valid payroll month and year.', 'danger')
        return redirect(url_for('payroll.list_payroll'))

    success = 0
    for pid_str in payroll_ids:
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        p = query("SELECT p.*, e.full_name FROM Payroll p JOIN Employee e ON p.employee_id=e.employee_id WHERE p.payroll_id=? AND p.status='Draft'", (pid,), one=True)
        if not p:
            continue

        execute("UPDATE Invoice SET status='Paid' WHERE payroll_id=?", (pid,))
        execute("UPDATE Payroll SET status='Finalised' WHERE payroll_id=?", (pid,))
        log_audit('FINALISE', 'Payroll', f'Finalised payroll id={pid}', 'Payroll', pid)

        month_names = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                       'July', 'August', 'September', 'October', 'November', 'December']
        period = f"{month_names[p['pay_period_month']]} {p['pay_period_year']}"
        send_notification(
            p['employee_id'],
            'Payslip Ready',
            f"Your payslip for {period} is ready. Net pay: RM {p['net_pay']:,.2f}",
            'Success',
            url_for('payroll.view_payslip', pid=pid)
        )
        success += 1

    if success:
        flash(f'Finalised {success} payroll record(s).', 'success')
    else:
        flash('No draft payroll records were finalised.', 'warning')
    return redirect(url_for('payroll.list_payroll', month=month, year=year))


@pay_bp.route('/bulk-pdf-selected', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager')
def bulk_pdf_selected():
    import zipfile
    payroll_ids = request.form.getlist('payroll_ids')
    if not payroll_ids:
        flash('No payroll records selected.', 'warning')
        return redirect(url_for('payroll.list_payroll'))

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for pid_str in payroll_ids:
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            res = _generate_payslip_pdf(pid)
            if res:
                pdf_bytes, fname = res
                zf.writestr(fname, pdf_bytes)

    zip_buf.seek(0)
    log_audit('BULK_DOWNLOAD', 'Payroll', f'Downloaded selected payslips ({len(payroll_ids)} records)', 'Payroll')

    response = make_response(zip_buf.getvalue())
    response.headers['Content-Type'] = 'application/zip'
    response.headers['Content-Disposition'] = f'attachment; filename="Payslips_Selected.zip"'
    return response


@pay_bp.route('/bulk_pdf')
@role_required('Admin', 'HR')
def bulk_download_pdf():
    """Generate a ZIP file containing all payslips for the selected month/year."""
    month, year = _parse_period(request.args.get('month'), request.args.get('year'), month_required=True)
    co    = session['company_id']

    if month is None:
        flash('Invalid month or year for bulk download.', 'danger')
        return redirect(url_for('payroll.list_payroll'))

    payrolls = query("""
        SELECT p.payroll_id FROM Payroll p
        JOIN Employee e ON p.employee_id=e.employee_id
        WHERE e.company_id=? AND p.pay_period_month=? AND p.pay_period_year=?
    """, (co, month, year))

    if not payrolls:
        flash('No payroll records found for this period.', 'warning')
        return redirect(url_for('payroll.list_payroll', month=month, year=year))

    import zipfile
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for p in payrolls:
            res = _generate_payslip_pdf(p['payroll_id'])
            if res:
                pdf_bytes, fname = res
                zf.writestr(fname, pdf_bytes)

    zip_buf.seek(0)
    log_audit('BULK_DOWNLOAD', 'Payroll', f'Downloaded bulk payslips for {month}/{year}', 'Payroll')

    response = make_response(zip_buf.getvalue())
    response.headers['Content-Type']        = 'application/zip'
    response.headers['Content-Disposition'] = f'attachment; filename="Payslips_{month}_{year}.zip"'
    return response


@pay_bp.route('/<int:pid>')
@login_required
def view_payslip(pid):
    uid  = session['user_id']
    role = session['user_role']
    p = query("""
        SELECT p.*, e.branch_id, e.full_name, e.position, e.ic_number, e.email,
               d.department_name, b.name as branch_name, c.name as company_name
        FROM Payroll p
        JOIN Employee e ON p.employee_id=e.employee_id
        JOIN Department d ON e.department_id=d.department_id
        JOIN Branch b ON e.branch_id=b.branch_id
        JOIN Company c ON e.company_id=c.company_id
        WHERE p.payroll_id=?
    """, (pid,), one=True)

    if not p:
        flash('Payslip not found.', 'danger')
        return redirect(url_for('payroll.list_payroll'))
    if role == 'Employee' and p['employee_id'] != uid:
        flash('Access denied.', 'danger')
        return redirect(url_for('payroll.list_payroll'))
    if role == 'Manager' and p['branch_id'] != session['branch_id']:
        flash('Access denied. You can only view payslips of staff from your own branch.', 'danger')
        return redirect(url_for('payroll.list_payroll'))

    return render_template('payroll/payslip.html', p=p)


@pay_bp.route('/<int:pid>/pdf')
@login_required
def download_pdf(pid):
    """Generate and stream a ReportLab PDF payslip."""
    uid  = session['user_id']
    role = session['user_role']

    # Security Check
    p_check = query("SELECT employee_id FROM Payroll WHERE payroll_id=?", (pid,), one=True)
    if not p_check:
        flash('Payslip not found.', 'danger')
        return redirect(url_for('payroll.list_payroll'))
    if role == 'Employee' and p_check['employee_id'] != uid:
        flash('Access denied.', 'danger')
        return redirect(url_for('payroll.list_payroll'))
    if role == 'Manager':
        emp_branch = query("SELECT branch_id FROM Employee WHERE employee_id=?", (p_check['employee_id'],), one=True)
        if not emp_branch or emp_branch['branch_id'] != session['branch_id']:
            flash('Access denied. You can only download payslips of staff from your own branch.', 'danger')
            return redirect(url_for('payroll.list_payroll'))

    res = _generate_payslip_pdf(pid)
    if not res:
        flash('Error generating PDF.', 'danger')
        return redirect(url_for('payroll.list_payroll', pid=pid))

    pdf_bytes, fname = res
    log_audit('DOWNLOAD_PDF', 'Payroll', f'Downloaded payslip PDF for id={pid}', 'Payroll', pid)

    response = make_response(pdf_bytes)
    response.headers['Content-Type']        = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename="{fname}"'
    return response


def _generate_payslip_pdf(payroll_id):
    """Internal helper to generate a PDF for a specific payroll record."""
    p = query("""
        SELECT p.*, e.full_name, e.position, e.ic_number, e.email,
               d.department_name, b.name as branch_name, c.name as company_name
        FROM Payroll p
        JOIN Employee e ON p.employee_id=e.employee_id
        JOIN Department d ON e.department_id=d.department_id
        JOIN Branch b ON e.branch_id=b.branch_id
        JOIN Company c ON e.company_id=c.company_id
        WHERE p.payroll_id=?
    """, (payroll_id,), one=True)

    if not p: return None
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                        Paragraph, Spacer, HRFlowable)
        from reportlab.lib.enums import TA_RIGHT, TA_CENTER
    except ImportError: return None

    month_names = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
    period = f"{month_names[p['pay_period_month']]} {p['pay_period_year']}"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm, topMargin=18*mm, bottomMargin=18*mm)
    elements = []

    # Header
    header_style = ParagraphStyle('hdr', fontSize=20, fontName='Helvetica-Bold', textColor=colors.HexColor('#1a6b3a'))
    right_style  = ParagraphStyle('rt',  fontSize=10, alignment=TA_RIGHT)
    header_data = [[Paragraph('SmartHR', header_style), Paragraph(f'<b>PAYSLIP</b><br/>{period}', right_style)]]
    header_tbl = Table(header_data, colWidths=['60%', '40%'])
    header_tbl.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'TOP')]))
    elements.append(header_tbl)
    elements.append(Paragraph(p['company_name'], ParagraphStyle('sub', fontSize=10, textColor=colors.grey)))
    elements.append(Spacer(1, 4*mm))
    elements.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#1a6b3a')))
    elements.append(Spacer(1, 3*mm))

    # Employee Info
    emp_data = [['Employee', p['full_name'], 'IC Number', p['ic_number'] or '—'],
                ['Position', p['position'] or '—', 'Department', p['department_name']],
                ['Branch', p['branch_name'], 'Email', p['email']]]
    emp_tbl = Table(emp_data, colWidths=['22%', '28%', '22%', '28%'])
    emp_tbl.setStyle(TableStyle([
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
        ('FONTNAME', (2,0), (2,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('TEXTCOLOR', (0,0), (0,-1), colors.grey),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [colors.HexColor('#f4f9f6'), colors.white])
    ]))
    elements.append(emp_tbl); elements.append(Spacer(1, 5*mm))

    # Earnings & Deductions
    def add_sec(title, rows, t_label, t_val, t_col):
        elements.append(Paragraph(title, ParagraphStyle('s', fontSize=9, fontName='Helvetica-Bold', textColor=colors.white, backColor=colors.HexColor('#2d7a4f'), borderPadding=4)))
        data = [[l, f'RM {v:,.2f}'] for l, v in rows]
        data.append([t_label, f'RM {t_val:,.2f}'])
        tbl = Table(data, colWidths=['70%', '30%'])
        tbl.setStyle(TableStyle([
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('ALIGN', (1,0), (1,-1), 'RIGHT'),
            ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
            ('TEXTCOLOR', (1,-1), (1,-1), t_col)
        ]))
        elements.append(tbl); elements.append(Spacer(1, 4*mm))

    add_sec('EARNINGS', [('Base Salary', p['base_salary']), ('Overtime', p['overtime_pay']), ('Claims', p['invoice_claims'])], 'Gross Pay', p['gross_pay'], colors.HexColor('#1a6b3a'))
    add_sec('DEDUCTIONS', [('EPF', p['epf_employee']), ('SOCSO', p['socso_employee']), ('EIS', p['eis_employee']), ('Tax', p['pcb_tax'])], 'Total Deductions', p['total_deductions'], colors.red)

    # Net Pay
    elements.append(Table([['NET PAY', f"RM {p['net_pay']:,.2f}"]], colWidths=['70%', '30%'], 
                          style=TableStyle([('FONTSIZE', (0,0), (-1,-1), 14), ('FONTNAME', (0,0), (-1,-1), 'Helvetica-Bold'), ('TEXTCOLOR', (1,0), (1,0), colors.HexColor('#1a6b3a')), ('ALIGN', (1,0), (1,-1), 'RIGHT')])))
    
    if p['notes']:
        elements.append(Spacer(1, 5*mm))
        elements.append(Paragraph(f"<b>Notes:</b> {p['notes']}", ParagraphStyle('n', fontSize=8)))

    doc.build(elements)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes, f"Payslip_{p['pay_period_month']}_{p['pay_period_year']}_{p['full_name'].replace(' ','_')}.pdf"
