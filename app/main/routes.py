"""app/main/routes.py – Dashboard"""
from flask import Blueprint, render_template, session, jsonify
from app.database import query, as_dict, is_leave_eligible
from app.auth.routes import login_required

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
@login_required
def dashboard():
    uid  = session['user_id']
    role = session['user_role']
    co   = session['company_id']
    bid  = session.get('branch_id')

    if role == 'Employee':
        # ── Employee Self-Service (ESS) Dashboard Metrics ──
        # Get employee's own data
        emp = as_dict(query("SELECT * FROM Employee WHERE employee_id=?", (uid,), one=True))
        
        dept = None
        branch = None
        if emp:
            if emp.get('department_id'):
                dept = query("SELECT department_name FROM Department WHERE department_id=?", (emp['department_id'],), one=True)
            if emp.get('branch_id'):
                branch = query("SELECT name FROM Branch WHERE branch_id=?", (emp['branch_id'],), one=True)
        
        # Get upcoming leave balance (filtered by eligibility)
        all_leave_balances = query("""
            SELECT lt.type_name, lt.eligible_genders, lt.eligible_marital_status,
                   lb.entitled_days, lb.used_days, lb.pending_days, lb.leave_type_id,
                   (lb.entitled_days - lb.used_days - lb.pending_days) as available_days
            FROM Leave_Balance lb
            JOIN Leave_Type lt ON lb.leave_type_id = lt.leave_type_id
            WHERE lb.employee_id=? AND lb.year = strftime('%Y', 'now')
        """, (uid,))
        
        emp_gender = emp.get('gender')
        emp_marital = emp.get('marital_status')
        
        leave_balances = []
        for bal in all_leave_balances:
            lt_dict = as_dict(bal)
            if is_leave_eligible(lt_dict, emp_gender, emp_marital):
                leave_balances.append(bal)
        
        # Get recent payslips
        recent_payslips = query("""
            SELECT p.pay_period_month, p.pay_period_year, ps.filename, p.net_pay, p.status
            FROM Payslip ps
            JOIN Payroll p ON ps.payroll_id = p.payroll_id
            WHERE ps.employee_id=?
            ORDER BY p.pay_period_year DESC, p.pay_period_month DESC
            LIMIT 5
        """, (uid,))
        
        # Get recent attendance
        recent_attendance = query("""
            SELECT check_in, check_out, hours_worked, status
            FROM Attendance
            WHERE employee_id=?
            ORDER BY check_in DESC
            LIMIT 7
        """, (uid,))
        
        # Get own pending leave & invoice applications
        own_pending = query("""
            SELECT * FROM (
                SELECT 'Leave' as type, 
                       'LV-'||leave_id as ref, 
                       status, 
                       applied_at as created_at
                FROM Leave_Application
                WHERE employee_id=? AND status='Pending'
                LIMIT 3
            )
            UNION ALL
            SELECT * FROM (
                SELECT 'Invoice' as type,
                       invoice_number as ref,
                       status,
                       submitted_at as created_at
                FROM Invoice
                WHERE employee_id=? AND status='Pending'
                LIMIT 3
            )
            ORDER BY created_at DESC
        """, (uid, uid))
        
        return render_template('dashboard.html',
                               emp=emp,
                               dept=dept,
                               branch=branch,
                               leave_balances=leave_balances,
                               recent_payslips=recent_payslips,
                               recent_attendance=recent_attendance,
                               own_pending=own_pending)

    elif role == 'Manager':
        # ── Metrics for Manager's Branch ──
        total_emp = query(
            "SELECT COUNT(*) as c FROM Employee WHERE company_id=? AND branch_id=? AND employment_status='Active'",
            (co, bid), one=True)['c']

        on_leave = query(
            "SELECT COUNT(*) as c FROM Employee WHERE company_id=? AND branch_id=? AND employment_status='On Leave'",
            (co, bid), one=True)['c']

        pending_leaves = query(
            "SELECT COUNT(*) as c FROM Leave_Application la "
            "JOIN Employee e ON la.employee_id=e.employee_id "
            "WHERE e.company_id=? AND e.branch_id=? AND la.status='Pending'",
            (co, bid), one=True)['c']

        pending_invoices = query(
            "SELECT COUNT(*) as c FROM Invoice i "
            "JOIN Employee e ON i.employee_id=e.employee_id "
            "WHERE e.company_id=? AND e.branch_id=? AND i.status='Pending'",
            (co, bid), one=True)['c']

        # ── Recent Activity for Manager's Branch ──
        activity = query("""
            SELECT al.*, e.full_name
            FROM AuditLog al
            JOIN Employee e ON al.employee_id = e.employee_id
            WHERE e.company_id=? AND e.branch_id=?
            ORDER BY al.created_at DESC LIMIT 8
        """, (co, bid))

        # ── Pending Approvals for Manager's Branch (Leaves only) ──
        pending_q = query("""
            SELECT 'Leave' as type,
                   'LV-'||la.leave_id as ref,
                   e.full_name as owner,
                   la.status
            FROM Leave_Application la
            JOIN Employee e ON la.employee_id=e.employee_id
            WHERE la.status='Pending' AND e.company_id=? AND e.branch_id=?
            LIMIT 5
        """, (co, bid))

        # ── Dept distribution for Manager's Branch ──
        dept_dist = query("""
            SELECT d.department_name, COUNT(e.employee_id) as cnt
            FROM Employee e
            JOIN Department d ON e.department_id=d.department_id
            WHERE e.company_id=? AND e.branch_id=? AND e.employment_status='Active'
            GROUP BY d.department_name
        """, (co, bid))

    else:
        # ── Metrics for Admin/HR ──
        total_emp = query(
            "SELECT COUNT(*) as c FROM Employee WHERE company_id=? AND employment_status='Active'",
            (co,), one=True)['c']

        on_leave = query(
            "SELECT COUNT(*) as c FROM Employee WHERE company_id=? AND employment_status='On Leave'",
            (co,), one=True)['c']

        pending_leaves = query(
            "SELECT COUNT(*) as c FROM Leave_Application la "
            "JOIN Employee e ON la.employee_id=e.employee_id "
            "WHERE e.company_id=? AND la.status='Pending'",
            (co,), one=True)['c']

        pending_invoices = query(
            "SELECT COUNT(*) as c FROM Invoice i "
            "JOIN Employee e ON i.employee_id=e.employee_id "
            "WHERE e.company_id=? AND i.status='Pending'",
            (co,), one=True)['c']

        # ── Recent Activity ──
        activity = query("""
            SELECT al.*, e.full_name
            FROM AuditLog al
            LEFT JOIN Employee e ON al.employee_id = e.employee_id
            ORDER BY al.created_at DESC LIMIT 8
        """)

        # ── Pending Approvals (all modules) ──
        union_parts = []
        union_params = []

        # Leave (all admin roles)
        union_parts.append("""
            SELECT * FROM (
                SELECT 'Leave' as type, 'LV-'||la.leave_id as ref,
                       e.full_name as owner, la.status
                FROM Leave_Application la
                JOIN Employee e ON la.employee_id=e.employee_id
                WHERE la.status='Pending' AND e.company_id=?
                LIMIT 5
            )
        """)
        union_params.append(co)

        # Invoice (all admin roles)
        union_parts.append("""
            SELECT * FROM (
                SELECT 'Invoice' as type, i.invoice_number as ref,
                       e.full_name as owner, i.status
                FROM Invoice i
                JOIN Employee e ON i.employee_id=e.employee_id
                WHERE i.status='Pending' AND e.company_id=?
                LIMIT 5
            )
        """)
        union_params.append(co)

        # Job Applications (all admin roles)
        union_parts.append("""
            SELECT * FROM (
                SELECT 'Application' as type, 'A-'||ja.application_id as ref,
                       ja.applicant_name as owner, ja.status
                FROM Job_Application ja
                LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
                LEFT JOIN Branch b ON jp.branch_id=b.branch_id
                WHERE ja.status='New' AND (b.company_id=? OR ja.posting_id IS NULL)
                LIMIT 5
            )
        """)
        union_params.append(co)

        # Bonus + Increment (Admin/HR Manager only)
        if role in ('Admin', 'HR Manager', 'HR Director'):
            union_parts.append("""
                SELECT * FROM (
                    SELECT 'Bonus' as type, 'BP-'||bp.proposal_id as ref,
                           e.full_name as owner, bp.status
                    FROM Bonus_Proposal bp
                    JOIN Employee e ON bp.employee_id=e.employee_id
                    WHERE bp.status='Pending' AND e.company_id=?
                    LIMIT 5
                )
            """)
            union_params.append(co)
            union_parts.append("""
                SELECT * FROM (
                    SELECT 'Increment' as type, 'SI-'||si.increment_id as ref,
                           e.full_name as owner, si.status
                    FROM Salary_Increment si
                    JOIN Employee e ON si.employee_id=e.employee_id
                    WHERE si.status='Pending' AND e.company_id=?
                    LIMIT 5
                )
            """)
            union_params.append(co)

        union_sql = ' UNION ALL '.join(union_parts)
        pending_q = query(union_sql, union_params)

        # ── Dept distribution ──
        dept_dist = query("""
            SELECT d.department_name, COUNT(e.employee_id) as cnt
            FROM Employee e
            JOIN Department d ON e.department_id=d.department_id
            WHERE e.company_id=? AND e.employment_status='Active'
            GROUP BY d.department_name
        """, (co,))

    # Per-type pending counts for toast notifications (direct COUNT queries)
    if role == 'Manager':
        pending_applications_count = query("""
            SELECT COUNT(*) as c FROM Job_Application ja
            JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
            WHERE ja.status='New' AND jp.branch_id=?
        """, (bid,), one=True)['c']
    else:
        pending_applications_count = query("""
            SELECT COUNT(*) as c FROM Job_Application ja
            LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
            LEFT JOIN Branch b ON jp.branch_id=b.branch_id
            WHERE ja.status='New' AND (b.company_id=? OR ja.posting_id IS NULL)
        """, (co,), one=True)['c']
    if role in ('Admin', 'HR Manager', 'HR Director'):
        pending_increment_count = query("""
            SELECT COUNT(*) as c FROM Salary_Increment si
            JOIN Employee e ON si.employee_id=e.employee_id
            WHERE si.status='Pending' AND e.company_id=?
        """, (co,), one=True)['c']
        pending_bonus_count = query("""
            SELECT COUNT(*) as c FROM Bonus_Proposal bp
            JOIN Employee e ON bp.employee_id=e.employee_id
            WHERE bp.status='Pending' AND e.company_id=?
        """, (co,), one=True)['c']
    else:
        pending_increment_count = 0
        pending_bonus_count = 0
    pending_leave_count = pending_leaves
    pending_invoice_count = pending_invoices

    return render_template('dashboard.html',
                           total_emp=total_emp,
                           on_leave=on_leave,
                           pending_leaves=pending_leaves,
                           pending_invoices=pending_invoices,
                           pending_increment_count=pending_increment_count,
                           pending_bonus_count=pending_bonus_count,
                           pending_applications_count=pending_applications_count,
                            activity=activity,
                            pending_q=pending_q,
                            dept_dist=dept_dist)


@main_bp.route('/api/check-email')
@login_required
def check_email():
    role = session.get('user_role')
    if role not in ('Admin', 'HR', 'HR Manager', 'HR Director'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 403

    try:
        from app.notifications.email_monitor import poll_inbox
        new_apps, new_accepts, new_declines = poll_inbox()

        co = session['company_id']
        total_apps = query(
            "SELECT COUNT(*) as c FROM Job_Application ja "
            "LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id "
            "LEFT JOIN Branch b ON jp.branch_id=b.branch_id "
            "WHERE ja.status='New' AND (b.company_id=? OR ja.posting_id IS NULL)",
            (co,), one=True)['c']

        return jsonify({
            'ok': True,
            'new_apps': new_apps,
            'new_accepts': new_accepts,
            'new_declines': new_declines,
            'total_apps': total_apps,
        })
    except Exception as e:
        print(f"[CHECK EMAIL] Error: {e}")
        return jsonify({'ok': False, 'error': str(e)})
