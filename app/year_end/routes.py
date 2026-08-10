from flask import (render_template, request, session, flash, redirect,
                   url_for, jsonify)
from app.database import query, execute, log_audit
from app.auth.routes import login_required, role_required
from app.year_end import year_end_bp
from app.notifications.routes import send_notification
from datetime import datetime, date


GRADE_ORDER = ['A', 'B', 'C', 'D']


# ── helpers ────────────────────────────────────────────────────────────────

def _months_worked_yr(employee_id, year):
    emp = query("SELECT hire_date FROM Employee WHERE employee_id=?", (employee_id,), one=True)
    if not emp or not emp['hire_date']:
        return 12
    hire = datetime.strptime(emp['hire_date'], '%Y-%m-%d').date()
    start = max(hire, date(year, 1, 1))
    end = date(year, 12, 31)
    if start > end:
        return 0
    return min(12, end.month - start.month + 1)


def _years_served(employee_id, year):
    emp = query("SELECT hire_date FROM Employee WHERE employee_id=?", (employee_id,), one=True)
    if not emp or not emp['hire_date']:
        return 0
    hire = datetime.strptime(emp['hire_date'], '%Y-%m-%d').date()
    yrs = year - hire.year
    if hire.month > 1 or hire.day > 1:
        pass
    return max(0, yrs)


def _get_inc_policy(co):
    p = query("SELECT * FROM Increment_Policy WHERE company_id=?", (co,), one=True)
    if not p:
        execute("""INSERT INTO Increment_Policy (company_id, increment_pct, tenure_threshold_years, effective_month)
                   VALUES(?, 5.0, 1, 1)""", (co,))
        p = query("SELECT * FROM Increment_Policy WHERE company_id=?", (co,), one=True)
    return p


def _get_bonus_policy(co, year):
    p = query("SELECT * FROM Bonus_Policy WHERE company_id=? AND year=?", (co, year), one=True)
    if not p:
        execute("""INSERT INTO Bonus_Policy (company_id, year, grade_A_months, grade_B_months, grade_C_months, grade_D_months,
                    tenure_threshold_months, payout_month, auto_propose) VALUES(?,?,3.0,2.0,1.0,0.5,3,1,1)""", (co, year))
        p = query("SELECT * FROM Bonus_Policy WHERE company_id=? AND year=?", (co, year), one=True)
    return p


def _auto_generate_increments(co, year):
    policy = _get_inc_policy(co)
    if not policy['auto_propose']:
        return 0
    existing = query("SELECT employee_id FROM Salary_Increment WHERE period_year=?", (year,))
    existing_ids = {r['employee_id'] for r in existing}
    pct = float(policy['increment_pct'])
    thresh = int(policy['tenure_threshold_years'])

    emps = query("SELECT * FROM Employee WHERE company_id=? AND is_active=1", (co,))
    count = 0
    for e in emps:
        eid = e['employee_id']
        if eid in existing_ids:
            continue
        yrs = _years_served(eid, year)
        if yrs < thresh:
            continue
        new_sal = round(e['base_salary'] * (1 + pct / 100.0), 2)
        perf = query("SELECT composite_score, grade FROM Performance_Review WHERE employee_id=? AND period_year=?", (eid, year), one=True)
        execute("""INSERT INTO Salary_Increment (employee_id, period_year, old_salary, new_salary, increment_pct,
                    performance_score, performance_grade, proposed_by, status)
                   VALUES(?,?,?,?,?,?,?,1,'Pending')""",
                (eid, year, e['base_salary'], new_sal, pct,
                 perf['composite_score'] if perf else None,
                 perf['grade'] if perf else None))
        count += 1
    return count


def _auto_generate_bonuses(co, year):
    policy = _get_bonus_policy(co, year)
    if not policy['auto_propose']:
        return 0
    existing = query("SELECT employee_id FROM Bonus_Proposal WHERE period_year=?", (year,))
    existing_ids = {r['employee_id'] for r in existing}

    grade_months = {g: float(dict(policy).get(f'grade_{g}_months', 0) or 0) for g in GRADE_ORDER}
    thresh = int(policy['tenure_threshold_months'])

    emps = query("""
        SELECT e.*, pr.composite_score, pr.grade
        FROM Employee e
        JOIN Performance_Review pr ON e.employee_id=pr.employee_id AND pr.period_year=?
        WHERE e.company_id=? AND e.is_active=1
        ORDER BY e.employee_id
    """, (year, co))

    count = 0
    for e in emps:
        eid = e['employee_id']
        if eid in existing_ids:
            continue
        mw = _months_worked_yr(eid, year)
        if mw < thresh:
            continue
        grade = e['grade'] or 'D'
        months = grade_months.get(grade, 0.5)
        full = round(e['base_salary'] * months, 2)
        prorated = round(full * (mw / 12.0), 2)
        execute("""INSERT INTO Bonus_Proposal (employee_id, period_year, composite_score, grade,
                    full_bonus_amount, bonus_amount, months_worked, proposed_by, status)
                   VALUES(?,?,?,?,?,?,?,1,'Pending')""",
                (eid, year, e['composite_score'], grade, full, prorated, mw))
        count += 1
    return count


# ── year-end review page ───────────────────────────────────────────────────

@year_end_bp.route('/year-end-review')
@login_required
@role_required('Admin', 'HR Manager', 'HR Director')
def review():
    co = session['company_id']
    year = int(request.args.get('year', datetime.now().year))

    inc_count = _auto_generate_increments(co, year)
    bonus_count = _auto_generate_bonuses(co, year)
    if inc_count or bonus_count:
        log_audit('AUTO_GENERATE_YEAR_END', 'YearEnd',
                  f'Auto-generated {inc_count} increments, {bonus_count} bonuses for {year}',
                  action_details={'year': year, 'increments': inc_count, 'bonuses': bonus_count})

    # Load existing proposals
    increments = query("""
        SELECT si.*, e.full_name, e.base_salary, e.hire_date,
               d.department_name, b.name as branch_name
        FROM Salary_Increment si
        JOIN Employee e ON si.employee_id=e.employee_id
        JOIN Department d ON e.department_id=d.department_id
        JOIN Branch b ON e.branch_id=b.branch_id
        WHERE e.company_id=? AND si.period_year=?
        ORDER BY si.status, e.full_name
    """, (co, year))

    bonuses = query("""
        SELECT bp.*, e.full_name, e.base_salary,
               d.department_name, b.name as branch_name
        FROM Bonus_Proposal bp
        JOIN Employee e ON bp.employee_id=e.employee_id
        JOIN Department d ON e.department_id=d.department_id
        JOIN Branch b ON e.branch_id=b.branch_id
        WHERE e.company_id=? AND bp.period_year=?
        ORDER BY bp.status, e.full_name
    """, (co, year))

    inc_policy = _get_inc_policy(co)
    bonus_policy = _get_bonus_policy(co, year)

    inc_pending = sum(1 for i in increments if i['status'] == 'Pending')
    bonus_pending = sum(1 for b in bonuses if b['status'] == 'Pending')
    tab = request.args.get('tab', 'increment')

    return render_template('year_end/review.html',
                           year=year, increments=increments, bonuses=bonuses,
                           inc_policy=inc_policy, bonus_policy=bonus_policy,
                           inc_pending=inc_pending, bonus_pending=bonus_pending,
                           tab=tab)


@year_end_bp.route('/year-end-review/approve-increment', methods=['POST'])
@login_required
@role_required('Admin', 'HR Manager', 'HR Director')
def approve_increment():
    ids = request.form.getlist('increment_ids')
    year = request.form.get('year', str(datetime.now().year))
    uid = session['user_id']
    for iid in ids:
        inc = query("SELECT * FROM Salary_Increment WHERE increment_id=?", (iid,), one=True)
        if not inc:
            continue
        execute("""UPDATE Salary_Increment SET status='Approved', reviewed_by=?, reviewed_at=datetime('now')
                   WHERE increment_id=?""", (uid, iid))
        execute("UPDATE Employee SET base_salary=? WHERE employee_id=?", (inc['new_salary'], inc['employee_id']))
        try:
            send_notification(inc['employee_id'], 'Salary Increment Approved',
                f'Your salary has been increased to RM {inc["new_salary"]:,.2f} (+{inc["increment_pct"]}%).', 'Success')
        except Exception:
            pass
    log_audit('APPROVE_INCREMENTS', 'YearEnd', f'Approved {len(ids)} increments for {year}')
    flash(f'Approved {len(ids)} increment(s). Base salaries updated.', 'success')
    return redirect(url_for('year_end.review', year=year, tab='increment'))


@year_end_bp.route('/year-end-review/reject-increment', methods=['POST'])
@login_required
@role_required('Admin', 'HR Manager', 'HR Director')
def reject_increment():
    ids = request.form.getlist('increment_ids')
    year = request.form.get('year', str(datetime.now().year))
    uid = session['user_id']
    reason = request.form.get('rejection_reason', '').strip() or 'No reason provided'
    for iid in ids:
        execute("""UPDATE Salary_Increment SET status='Rejected', reviewed_by=?, reviewed_at=datetime('now'),
                   rejection_reason=? WHERE increment_id=?""", (uid, reason, iid))
    log_audit('REJECT_INCREMENTS', 'YearEnd', f'Rejected {len(ids)} increments for {year}')
    flash(f'Rejected {len(ids)} increment(s).', 'info')
    return redirect(url_for('year_end.review', year=year, tab='increment'))


@year_end_bp.route('/year-end-review/approve-bonus', methods=['POST'])
@login_required
@role_required('Admin', 'HR Manager', 'HR Director')
def approve_bonus():
    ids = request.form.getlist('bonus_ids')
    year = request.form.get('year', str(datetime.now().year))
    uid = session['user_id']
    for bid in ids:
        bp = query("SELECT * FROM Bonus_Proposal WHERE proposal_id=?", (bid,), one=True)
        if not bp:
            continue
        execute("""UPDATE Bonus_Proposal SET status='Approved', reviewed_by=?, reviewed_at=datetime('now')
                   WHERE proposal_id=?""", (uid, bid))
        try:
            send_notification(bp['employee_id'], 'Bonus Approved',
                f'Your {year} bonus of RM {bp["bonus_amount"]:,.2f} has been approved.', 'Success')
        except Exception:
            pass
    log_audit('APPROVE_BONUSES', 'YearEnd', f'Approved {len(ids)} bonuses for {year}')
    flash(f'Approved {len(ids)} bonus(es). Will be paid in the configured payout month.', 'success')
    return redirect(url_for('year_end.review', year=year, tab='bonus'))


@year_end_bp.route('/year-end-review/reject-bonus', methods=['POST'])
@login_required
@role_required('Admin', 'HR Manager', 'HR Director')
def reject_bonus():
    ids = request.form.getlist('bonus_ids')
    year = request.form.get('year', str(datetime.now().year))
    uid = session['user_id']
    reason = request.form.get('rejection_reason', '').strip() or 'No reason provided'
    for bid in ids:
        execute("""UPDATE Bonus_Proposal SET status='Rejected', reviewed_by=?, reviewed_at=datetime('now'),
                   rejection_reason=? WHERE proposal_id=?""", (uid, reason, bid))
    log_audit('REJECT_BONUSES', 'YearEnd', f'Rejected {len(ids)} bonuses for {year}')
    flash(f'Rejected {len(ids)} bonus(es).', 'info')
    return redirect(url_for('year_end.review', year=year, tab='bonus'))


# ── compensation policy page ───────────────────────────────────────────────

@year_end_bp.route('/compensation/policy', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'HR Manager', 'HR Director')
def policy():
    co = session['company_id']
    year = int(request.args.get('year', datetime.now().year))
    inc_policy = _get_inc_policy(co)
    bonus_policy = _get_bonus_policy(co, year)

    if request.method == 'POST':
        # Save increment policy
        execute("""UPDATE Increment_Policy SET increment_pct=?, tenure_threshold_years=?, effective_month=?,
                   effective_year=?, auto_propose=?, updated_at=datetime('now') WHERE policy_id=?""",
                (float(request.form.get('inc_pct', 5.0)),
                 int(request.form.get('inc_tenure', 1)),
                 int(request.form.get('inc_eff_month', 1)),
                 int(request.form.get('inc_eff_year', year)),
                 1 if request.form.get('inc_auto') else 0,
                 inc_policy['policy_id']))

        # Save bonus policy
        execute("""UPDATE Bonus_Policy SET grade_A_months=?, grade_B_months=?, grade_C_months=?, grade_D_months=?,
                   tenure_threshold_months=?, payout_month=?, auto_propose=?, updated_at=datetime('now')
                   WHERE policy_id=?""",
                (float(request.form.get('grade_A_months', 3.0)),
                 float(request.form.get('grade_B_months', 2.0)),
                 float(request.form.get('grade_C_months', 1.0)),
                 float(request.form.get('grade_D_months', 0.5)),
                 int(request.form.get('bonus_tenure', 3)),
                 int(request.form.get('payout_month', 1)),
                 1 if request.form.get('bonus_auto') else 0,
                 bonus_policy['policy_id']))

        log_audit('UPDATE_COMPENSATION_POLICY', 'YearEnd', f'Updated policies for {year}')
        flash('Compensation policies updated.', 'success')
        return redirect(url_for('year_end.policy', year=year))

    return render_template('year_end/policy.html',
                           year=year, inc_policy=inc_policy, bonus_policy=bonus_policy)
