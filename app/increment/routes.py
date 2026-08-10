from flask import (render_template, request, session, flash, redirect,
                   url_for, jsonify)
from app.database import query, execute, log_audit
from app.auth.routes import login_required, role_required
from app.increment import increment_bp
from datetime import datetime


def get_policy(company_id):
    """Return the increment policy for a company, creating a default if absent."""
    policy = query("SELECT * FROM Increment_Policy WHERE company_id=?",
                   (company_id,), one=True)
    if not policy:
        execute("""
            INSERT INTO Increment_Policy
            (company_id, increment_pct, tenure_threshold_years, effective_month)
            VALUES(?, 5.0, 1, 1)
        """, (company_id,))
        policy = query("SELECT * FROM Increment_Policy WHERE company_id=?",
                       (company_id,), one=True)
    return policy


@increment_bp.route('/')
@login_required
@role_required('Admin', 'HR Manager')
def list_increments():
    year = int(request.args.get('year') or datetime.now().year)
    grade_f = request.args.get('grade', '')
    status_f = request.args.get('status', '')
    branch_f = request.args.get('branch_id', '')
    dept_f = request.args.get('department_id', '')
    search = request.args.get('search', '')

    co = session['company_id']
    where = ["si.period_year=? AND e.company_id=?"]
    params = [year, co]

    if grade_f:
        where.append("si.performance_grade=?")
        params.append(grade_f)
    if status_f:
        where.append("si.status=?")
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

    increments = query(f"""
        SELECT si.*, e.full_name, e.base_salary as current_salary,
               e.hire_date,
               d.department_name, b.name as branch_name,
               p.full_name as proposer_name,
               ROUND((julianday('now') - julianday(e.hire_date)) / 365.25, 1) as years_served
        FROM Salary_Increment si
        JOIN Employee e ON si.employee_id=e.employee_id
        JOIN Department d ON e.department_id=d.department_id
        JOIN Branch b ON e.branch_id=b.branch_id
        LEFT JOIN Employee p ON si.proposed_by=p.employee_id
        WHERE {' AND '.join(where)}
        ORDER BY si.status, e.full_name
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

    policy = get_policy(co)

    return render_template('increment/list.html',
                           increments=increments, year=year,
                           grade_f=grade_f, status_f=status_f,
                           branch_f=branch_f, dept_f=dept_f,
                           search=search, branches=branches,
                           departments=departments, policy=policy)


@increment_bp.route('/policy', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'HR Manager')
def policy():
    co = session['company_id']
    policy = get_policy(co)

    if request.method == 'POST':
        pct = request.form.get('increment_pct', type=float)
        tenure = request.form.get('tenure_threshold_years', type=int)
        eff_month = request.form.get('effective_month', type=int)
        eff_year = request.form.get('effective_year', type=int)
        auto_propose = 1 if request.form.get('auto_propose') else 0

        if not pct or pct < 0 or pct > 100:
            flash('Increment percentage must be between 0 and 100.', 'danger')
            return redirect(url_for('increment.policy'))
        if not tenure or tenure < 0:
            tenure = 0
        if not eff_month or eff_month < 1 or eff_month > 12:
            eff_month = 1

        execute("""
            UPDATE Increment_Policy
            SET increment_pct=?, tenure_threshold_years=?,
                effective_month=?, effective_year=?, auto_propose=?,
                updated_at=datetime('now')
            WHERE policy_id=?
        """, (pct, tenure, eff_month, eff_year, auto_propose, policy['policy_id']))

        log_audit('UPDATE_INCREMENT_POLICY', 'Increment_Policy',
                  f'Updated increment policy: {pct}% after {tenure} years, effective {eff_month}/{eff_year}',
                  target_table='Increment_Policy', target_record_id=policy['policy_id'],
                  action_details={'increment_pct': pct, 'tenure_threshold_years': tenure,
                                  'effective_month': eff_month, 'effective_year': eff_year})
        flash('Increment policy updated.', 'success')
        return redirect(url_for('increment.policy'))

    active_count = query("""
        SELECT COUNT(*) as c FROM Employee
        WHERE company_id=? AND is_active=1
          AND (julianday('now') - julianday(hire_date)) / 365.25 >= ?
    """, (co, policy['tenure_threshold_years']), one=True)

    return render_template('increment/policy.html', policy=policy,
                           active_count=active_count['c'] if active_count else 0)


@increment_bp.route('/propose', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'HR Manager')
def propose_increments():
    co = session['company_id']
    year = int(request.form.get('year', datetime.now().year))
    policy = get_policy(co)

    if request.method == 'POST':
        selected = request.form.getlist('selected')
        counts = 0
        for emp_id in selected:
            emp = query("SELECT employee_id, base_salary FROM Employee WHERE employee_id=?",
                        (emp_id,), one=True)
            if not emp:
                continue
            pct = request.form.get(f'pct_{emp_id}', type=float)
            if not pct or pct < 0 or pct > 100:
                pct = policy['increment_pct']
            new_salary = round(emp['base_salary'] * (1 + pct / 100))

            execute("""
                INSERT OR IGNORE INTO Salary_Increment
                (employee_id, period_year, old_salary, new_salary, increment_pct,
                 proposed_by, status)
                VALUES(?,?,?,?,?,?,'Pending')
            """, (emp_id, year, emp['base_salary'], new_salary, pct,
                  session['user_id']))
            counts += 1

        log_audit('PROPOSE_INCREMENT', 'Salary_Increment',
                  f'Proposed {counts} salary increments for {year}',
                  action_details={'year': year, 'count': counts})
        flash(f'Proposed {counts} salary increment(s) for {year} '
              f'at {policy["increment_pct"]}% (policy).', 'success')
        return redirect(url_for('increment.list_increments', year=year))

    tenure_years = policy['tenure_threshold_years']
    employees = query("""
        SELECT e.employee_id, e.full_name, e.base_salary,
               e.hire_date,
               d.department_name, b.name as branch_name,
               ROUND((julianday('now') - julianday(e.hire_date)) / 365.25, 1) as years_served
        FROM Employee e
        JOIN Department d ON e.department_id=d.department_id
        JOIN Branch b ON e.branch_id=b.branch_id
        WHERE e.company_id=? AND e.is_active=1
          AND (julianday('now') - julianday(e.hire_date)) / 365.25 >= ?
          AND e.employee_id NOT IN (
              SELECT employee_id FROM Salary_Increment
              WHERE period_year=? AND status IN ('Pending','Approved')
          )
        ORDER BY e.full_name
    """, (co, tenure_years, year))

    return render_template('increment/propose.html',
                           employees=employees, year=year,
                           policy=policy)


@increment_bp.route('/bulk-action', methods=['POST'])
@login_required
@role_required('Admin', 'HR Manager')
def bulk_action():
    ids = request.form.getlist('increment_ids')
    action = request.form.get('action')
    uid = session['user_id']

    if not ids or action not in ('approve', 'reject'):
        flash('Select increments and choose an action.', 'warning')
        return redirect(url_for('increment.list_increments'))

    rejection_reason = request.form.get('rejection_reason', '').strip() if action == 'reject' else None

    for inc_id in ids:
        inc = query("""
            SELECT si.*, e.base_salary as cur_salary
            FROM Salary_Increment si
            JOIN Employee e ON si.employee_id=e.employee_id
            WHERE si.increment_id=?
        """, (inc_id,), one=True)

        if not inc:
            continue

        if action == 'approve':
            execute("""
                UPDATE Salary_Increment
                SET status='Approved', reviewed_by=?, reviewed_at=datetime('now')
                WHERE increment_id=?
            """, (uid, inc_id))

            # Update Employee.base_salary immediately
            execute("UPDATE Employee SET base_salary=? WHERE employee_id=?",
                    (inc['new_salary'], inc['employee_id']))

            # Apply to the next available Draft payroll immediately
            try:
                from app.payroll.helpers import apply_increment_to_payroll
                inc_amt = inc['new_salary'] - inc['old_salary']
                apply_increment_to_payroll(inc['employee_id'], inc_amt,
                                           inc['new_salary'], user_id=uid)
            except Exception as e:
                print(f"Failed to apply increment to payroll: {e}")

            from app.notifications.routes import send_notification
            send_notification(
                inc['employee_id'],
                'Salary Increment Approved',
                f'Your salary has been increased from RM {inc["old_salary"]:,.2f} to RM {inc["new_salary"]:,.2f}.',
                'Success'
            )
            execute("UPDATE Salary_Increment SET notified_at=datetime('now') WHERE increment_id=?",
                    (inc_id,))
        else:
            reason = rejection_reason or 'No reason provided'
            execute("""
                UPDATE Salary_Increment
                SET status='Rejected', reviewed_by=?, reviewed_at=datetime('now'),
                    rejection_reason=?
                WHERE increment_id=?
            """, (uid, reason, inc_id))

    label = 'approved' if action == 'approve' else 'rejected'
    log_audit(f'BULK_{action.upper()}_INCREMENT', 'Salary_Increment',
              f'{label.title()} {len(ids)} salary increment(s)',
              action_details={'count': len(ids), 'ids': ids})
    msg = f'{label.title()} {len(ids)} salary increment(s).'
    if action == 'approve':
        msg += ' Base salary updated and applied to the next payroll period.'
    flash(msg, 'success')
    return redirect(url_for('increment.list_increments'))


@increment_bp.route('/<int:inc_id>/approve', methods=['POST'])
@login_required
@role_required('Admin', 'HR Manager')
def approve_increment(inc_id):
    uid = session['user_id']
    inc = query("""
        SELECT si.*, e.base_salary as cur_salary
        FROM Salary_Increment si
        JOIN Employee e ON si.employee_id=e.employee_id
        WHERE si.increment_id=?
    """, (inc_id,), one=True)

    if not inc:
        flash('Increment not found.', 'danger')
        return redirect(url_for('increment.list_increments'))

    execute("""
        UPDATE Salary_Increment
        SET status='Approved', reviewed_by=?, reviewed_at=datetime('now')
        WHERE increment_id=?
    """, (uid, inc_id))

    # Update Employee.base_salary immediately
    execute("UPDATE Employee SET base_salary=? WHERE employee_id=?",
            (inc['new_salary'], inc['employee_id']))

    # Apply to the next available Draft payroll immediately
    try:
        from app.payroll.helpers import apply_increment_to_payroll
        inc_amt = inc['new_salary'] - inc['old_salary']
        success, msg = apply_increment_to_payroll(inc['employee_id'], inc_amt,
                                                   inc['new_salary'], user_id=uid)
        if success:
            flash(f'Increment approved. {msg}.', 'success')
        else:
            flash('Increment approved but could not be applied to payroll: ' + msg, 'warning')
    except Exception as e:
        print(f"Failed to apply increment to payroll: {e}")
        flash('Increment approved.', 'success')

    from app.notifications.routes import send_notification
    send_notification(
        inc['employee_id'],
        'Salary Increment Approved',
        f'Your salary has been increased from RM {inc["old_salary"]:,.2f} to RM {inc["new_salary"]:,.2f}.',
        'Success'
    )
    execute("UPDATE Salary_Increment SET notified_at=datetime('now') WHERE increment_id=?",
            (inc_id,))

    log_audit('APPROVE_INCREMENT', 'Salary_Increment',
              f'Approved increment for employee #{inc["employee_id"]} from RM {inc["old_salary"]} to RM {inc["new_salary"]}',
              action_details={'increment_id': inc_id, 'new_salary': inc['new_salary']})
    return redirect(url_for('increment.list_increments'))


@increment_bp.route('/<int:inc_id>/reject', methods=['POST'])
@login_required
@role_required('Admin', 'HR Manager')
def reject_increment(inc_id):
    uid = session['user_id']
    reason = request.form.get('rejection_reason', '').strip() or 'No reason provided'

    inc = query("SELECT * FROM Salary_Increment WHERE increment_id=?", (inc_id,), one=True)
    if not inc:
        flash('Increment not found.', 'danger')
        return redirect(url_for('increment.list_increments'))

    execute("""
        UPDATE Salary_Increment
        SET status='Rejected', reviewed_by=?, reviewed_at=datetime('now'),
            rejection_reason=?
        WHERE increment_id=?
    """, (uid, reason, inc_id))

    log_audit('REJECT_INCREMENT', 'Salary_Increment',
              f'Rejected increment for employee #{inc["employee_id"]}: {reason[:50]}',
              action_details={'increment_id': inc_id, 'reason': reason})
    flash('Increment rejected.', 'info')
    return redirect(url_for('increment.list_increments'))


@increment_bp.route('/api/pending-count')
@login_required
def api_pending_count():
    role = session.get('user_role')
    if role not in ('Admin', 'HR Manager'):
        return jsonify({'count': 0})
    co = session['company_id']
    cnt = query("""
        SELECT COUNT(*) as c FROM Salary_Increment si
        JOIN Employee e ON si.employee_id=e.employee_id
        WHERE si.status='Pending' AND e.company_id=?
    """, (co,), one=True)
    return jsonify({'count': cnt['c'] if cnt else 0})
