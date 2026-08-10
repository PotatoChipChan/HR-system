from flask import (render_template, request, session, flash, redirect,
                   url_for, jsonify)
from app.database import query, execute, log_audit
from app.auth.routes import login_required, role_required
from app.bonus import bonus_bp
from app.notifications.routes import send_notification
from datetime import datetime, date


GRADE_ORDER = ['A', 'B', 'C', 'D']


def _get_or_create_bonus_policy(company_id, year):
    """Return Bonus_Policy row for a company/year, creating defaults if missing."""
    policy = query("""
        SELECT * FROM Bonus_Policy WHERE company_id=? AND year=?
    """, (company_id, year), one=True)
    if policy:
        return policy
    execute("""
        INSERT INTO Bonus_Policy
        (company_id, year, grade_A_months, grade_B_months, grade_C_months,
         grade_D_months, tenure_threshold_months, payout_month, auto_propose)
        VALUES (?, ?, 3.0, 2.0, 1.0, 0.5, 3, 1, 1)
    """, (company_id, year))
    return query("""
        SELECT * FROM Bonus_Policy WHERE company_id=? AND year=?
    """, (company_id, year), one=True)


def _months_worked(employee_id, year):
    """Return number of months worked in the given year (capped at 12)."""
    emp = query("SELECT hire_date FROM Employee WHERE employee_id=?", (employee_id,), one=True)
    if not emp or not emp['hire_date']:
        return 12
    hire = datetime.strptime(emp['hire_date'], '%Y-%m-%d').date()
    start = date(year, 1, 1)
    if hire > start:
        start = hire
    end = date(year, 12, 31)
    if start > end:
        return 0
    # Count partial first month as 1, full months in between, partial last month as 1
    months = 1
    if start.year == end.year:
        months = end.month - start.month + 1
    return min(12, max(0, months))


def _grade_months(policy, grade):
    key = f'grade_{grade}_months'
    return float(dict(policy).get(key, 0) or 0)


def _calculate_bonus(base_salary, grade, policy, employee_id, year):
    """Return (full_bonus, prorated_bonus, months_worked)."""
    months = _months_worked(employee_id, year)
    full = base_salary * _grade_months(policy, grade)
    prorated = full * (months / 12.0)
    return round(full, 2), round(prorated, 2), months


@bonus_bp.route('/policy', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'HR Manager', 'HR Director')
def bonus_policy():
    co = session['company_id']
    year = int(request.args.get('year', datetime.now().year))
    policy = _get_or_create_bonus_policy(co, year)

    if request.method == 'POST':
        try:
            execute("""
                UPDATE Bonus_Policy
                SET grade_A_months=?, grade_B_months=?, grade_C_months=?, grade_D_months=?,
                    tenure_threshold_months=?, payout_month=?, auto_propose=?, updated_at=datetime('now')
                WHERE policy_id=?
            """, (
                float(request.form.get('grade_A_months', 3.0)),
                float(request.form.get('grade_B_months', 2.0)),
                float(request.form.get('grade_C_months', 1.0)),
                float(request.form.get('grade_D_months', 0.5)),
                int(request.form.get('tenure_threshold_months', 3)),
                int(request.form.get('payout_month', 1)),
                1 if request.form.get('auto_propose') else 0,
                policy['policy_id']
            ))
            log_audit('UPDATE_BONUS_POLICY', 'Bonus_Policy',
                      f'Updated bonus policy for {year}',
                      action_details={'year': year, 'company_id': co})
            flash(f'Bonus policy for {year} updated.', 'success')
        except Exception as e:
            flash(f'Error updating policy: {e}', 'danger')
        return redirect(url_for('bonus.bonus_policy', year=year))

    return render_template('bonus/policy.html', policy=policy, year=year)


@bonus_bp.route('/')
@login_required
@role_required('Admin', 'HR Manager', 'HR Director')
def list_bonuses():
    co = session['company_id']
    year = int(request.args.get('year', datetime.now().year))
    grade_f = request.args.get('grade', '')
    status_f = request.args.get('status', '')
    branch_f = request.args.get('branch_id', '')
    dept_f = request.args.get('department_id', '')
    search = request.args.get('search', '')

    policy = _get_or_create_bonus_policy(co, year)

    where = ["bp.period_year=? AND e.company_id=?"]
    params = [year, co]

    if grade_f:
        where.append("bp.grade=?")
        params.append(grade_f)
    if status_f:
        where.append("bp.status=?")
        params.append(status_f)
    if branch_f:
        where.append("e.branch_id=?")
        params.append(int(branch_f))
    if dept_f:
        where.append("e.department_id=?")
        params.append(int(dept_f))
    if search:
        where.append("e.full_name LIKE ?")
        params.append(f"%{search}%")

    proposals = query(f"""
        SELECT bp.*, e.full_name, e.base_salary,
               d.department_name, b.name as branch_name,
               p.full_name as proposer_name
        FROM Bonus_Proposal bp
        JOIN Employee e ON bp.employee_id=e.employee_id
        JOIN Department d ON e.department_id=d.department_id
        JOIN Branch b ON e.branch_id=b.branch_id
        LEFT JOIN Employee p ON bp.proposed_by=p.employee_id
        WHERE {' AND '.join(where)}
        ORDER BY bp.status, e.full_name
    """, params)

    branches = query(
        "SELECT branch_id, name FROM Branch WHERE company_id=? ORDER BY name", (co,))
    departments = query("""
        SELECT DISTINCT d.department_id, d.department_name
        FROM Department d
        JOIN Branch b ON d.branch_id = b.branch_id
        WHERE b.company_id = ?
        ORDER BY d.department_name
    """, (co,))

    # Summary counts
    summary = query("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN status='Pending' THEN 1 ELSE 0 END) as pending,
               SUM(CASE WHEN status='Approved' THEN 1 ELSE 0 END) as approved
        FROM Bonus_Proposal bp
        JOIN Employee e ON bp.employee_id=e.employee_id
        WHERE bp.period_year=? AND e.company_id=?
    """, (year, co), one=True)

    return render_template('bonus/list.html',
                           proposals=proposals, year=year,
                           policy=policy,
                           grade_f=grade_f, status_f=status_f,
                           branch_f=branch_f, dept_f=dept_f,
                           search=search, branches=branches,
                           departments=departments, summary=summary)


@bonus_bp.route('/propose', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'HR Manager', 'HR Director')
def propose_bonuses():
    co = session['company_id']
    year = int(request.args.get('year', datetime.now().year))
    policy = _get_or_create_bonus_policy(co, year)
    uid = session['user_id']

    if request.method == 'POST':
        selected = request.form.getlist('selected')
        counts = 0
        for emp_id in selected:
            grade = request.form.get(f'grade_{emp_id}', '')
            bonus_amt = request.form.get(f'bonus_{emp_id}', type=float)
            full_bonus = request.form.get(f'full_bonus_{emp_id}', type=float)
            months = request.form.get(f'months_{emp_id}', type=int)
            if not bonus_amt or bonus_amt < 0 or not grade:
                continue
            execute("""
                INSERT OR REPLACE INTO Bonus_Proposal
                (employee_id, period_year, grade, full_bonus_amount, bonus_amount,
                 months_worked, proposed_by, status)
                VALUES(?,?,?,?,?,?,?,'Pending')
            """, (emp_id, year, grade, full_bonus, bonus_amt, months or 12, uid))
            counts += 1

        log_audit('PROPOSE_BONUS', 'Bonus_Proposal',
                  f'Proposed {counts} bonuses for {year}',
                  action_details={'year': year, 'count': counts})
        flash(f'Proposed {counts} bonus(es) for {year}.', 'success')
        return redirect(url_for('bonus.list_bonuses', year=year))

    # Auto-propose: eligible employees with a Performance_Review grade
    # who do not already have a proposal for the year
    employees = query("""
        SELECT e.employee_id, e.full_name, e.base_salary, e.hire_date,
               d.department_name, b.name as branch_name,
               pr.composite_score, pr.grade
        FROM Employee e
        JOIN Department d ON e.department_id=d.department_id
        JOIN Branch b ON e.branch_id=b.branch_id
        LEFT JOIN Performance_Review pr ON e.employee_id=pr.employee_id AND pr.period_year=?
        WHERE e.company_id=? AND e.is_active=1
          AND e.employee_id NOT IN (
              SELECT employee_id FROM Bonus_Proposal WHERE period_year=?
          )
        ORDER BY e.full_name
    """, (year, co, year))

    # Compute proration for display
    eligible = []
    for emp in employees:
        if not emp['grade']:
            continue
        months = _months_worked(emp['employee_id'], year)
        if months < policy['tenure_threshold_months']:
            continue
        full_bonus, prorated_bonus, _ = _calculate_bonus(
            emp['base_salary'], emp['grade'], policy, emp['employee_id'], year)
        emp = dict(emp)
        emp['months_worked'] = months
        emp['full_bonus'] = full_bonus
        emp['prorated_bonus'] = prorated_bonus
        eligible.append(emp)

    return render_template('bonus/propose.html',
                           employees=eligible, year=year, policy=policy)


@bonus_bp.route('/bulk-action', methods=['POST'])
@login_required
@role_required('Admin', 'HR Manager', 'HR Director')
def bulk_action():
    ids = request.form.getlist('proposal_ids')
    action = request.form.get('action')
    uid = session['user_id']

    if not ids or action not in ('approve', 'reject'):
        flash('Select proposals and choose an action.', 'warning')
        return redirect(url_for('bonus.list_bonuses'))

    rejection_reason = request.form.get('rejection_reason', '').strip() if action == 'reject' else None

    for prop_id in ids:
        prop = query("""
            SELECT bp.*, e.base_salary as cur_salary
            FROM Bonus_Proposal bp
            JOIN Employee e ON bp.employee_id=e.employee_id
            WHERE bp.proposal_id=?
        """, (prop_id,), one=True)

        if not prop:
            continue

        if action == 'approve':
            execute("""
                UPDATE Bonus_Proposal
                SET status='Approved', reviewed_by=?, reviewed_at=datetime('now')
                WHERE proposal_id=?
            """, (uid, prop_id))

            send_notification(
                prop['employee_id'],
                'Bonus Approved',
                f'Your {prop["period_year"]} bonus of RM {prop["bonus_amount"]:,.2f} has been approved.',
                'Success'
            )
            execute("UPDATE Bonus_Proposal SET notified_at=datetime('now') WHERE proposal_id=?",
                    (prop_id,))
        else:
            reason = rejection_reason or 'No reason provided'
            execute("""
                UPDATE Bonus_Proposal
                SET status='Rejected', reviewed_by=?, reviewed_at=datetime('now'),
                    rejection_reason=?
                WHERE proposal_id=?
            """, (uid, reason, prop_id))

    label = 'approved' if action == 'approve' else 'rejected'
    log_audit(f'BULK_{action.upper()}_BONUS', 'Bonus_Proposal',
              f'{label.title()} {len(ids)} bonus proposal(s)',
              action_details={'count': len(ids), 'ids': ids})
    flash(f'{label.title()} {len(ids)} bonus proposal(s).', 'success')
    return redirect(url_for('bonus.list_bonuses'))


@bonus_bp.route('/<int:prop_id>/approve', methods=['POST'])
@login_required
@role_required('Admin', 'HR Manager', 'HR Director')
def approve_bonus(prop_id):
    uid = session['user_id']
    prop = query("""
        SELECT bp.*, e.base_salary as cur_salary
        FROM Bonus_Proposal bp
        JOIN Employee e ON bp.employee_id=e.employee_id
        WHERE bp.proposal_id=?
    """, (prop_id,), one=True)

    if not prop:
        flash('Bonus proposal not found.', 'danger')
        return redirect(url_for('bonus.list_bonuses'))

    execute("""
        UPDATE Bonus_Proposal
        SET status='Approved', reviewed_by=?, reviewed_at=datetime('now')
        WHERE proposal_id=?
    """, (uid, prop_id))

    send_notification(
        prop['employee_id'],
        'Bonus Approved',
        f'Your {prop["period_year"]} bonus of RM {prop["bonus_amount"]:,.2f} has been approved.',
        'Success'
    )
    execute("UPDATE Bonus_Proposal SET notified_at=datetime('now') WHERE proposal_id=?",
            (prop_id,))

    log_audit('APPROVE_BONUS', 'Bonus_Proposal',
              f'Approved bonus for employee #{prop["employee_id"]} of RM {prop["bonus_amount"]}',
              action_details={'proposal_id': prop_id, 'bonus_amount': prop['bonus_amount']})
    flash('Bonus approved. It will be paid in the configured payout month.', 'success')
    return redirect(url_for('bonus.list_bonuses'))


@bonus_bp.route('/<int:prop_id>/reject', methods=['POST'])
@login_required
@role_required('Admin', 'HR Manager', 'HR Director')
def reject_bonus(prop_id):
    uid = session['user_id']
    reason = request.form.get('rejection_reason', '').strip() or 'No reason provided'

    prop = query("SELECT * FROM Bonus_Proposal WHERE proposal_id=?", (prop_id,), one=True)
    if not prop:
        flash('Bonus proposal not found.', 'danger')
        return redirect(url_for('bonus.list_bonuses'))

    execute("""
        UPDATE Bonus_Proposal
        SET status='Rejected', reviewed_by=?, reviewed_at=datetime('now'),
            rejection_reason=?
        WHERE proposal_id=?
    """, (uid, reason, prop_id))

    log_audit('REJECT_BONUS', 'Bonus_Proposal',
              f'Rejected bonus for employee #{prop["employee_id"]}: {reason[:50]}',
              action_details={'proposal_id': prop_id, 'reason': reason})
    flash('Bonus proposal rejected.', 'info')
    return redirect(url_for('bonus.list_bonuses'))


@bonus_bp.route('/api/pending-count')
@login_required
def api_pending_count():
    role = session.get('user_role')
    if role not in ('Admin', 'HR Manager', 'HR Director'):
        return jsonify({'count': 0})
    co = session['company_id']
    year = datetime.now().year
    cnt = query("""
        SELECT COUNT(*) as c FROM Bonus_Proposal bp
        JOIN Employee e ON bp.employee_id=e.employee_id
        WHERE bp.status='Pending' AND e.company_id=? AND bp.period_year=?
    """, (co, year), one=True)
    return jsonify({'count': cnt['c'] if cnt else 0})
