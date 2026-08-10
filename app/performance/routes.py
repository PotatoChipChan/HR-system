from flask import (render_template, request, session,
                   flash, redirect, url_for, jsonify)
from app.database import query, execute, log_audit
from app.auth.routes import login_required, role_required
from app.performance.calculator import calculate_monthly_score, calculate_yearly_review
from app.performance import perf_bp
from datetime import datetime


@perf_bp.route('/')
@login_required
def list_scores():
    uid = session['user_id']
    role = session['user_role']
    co = session['company_id']

    month_raw = request.args.get('month', '')
    month = int(month_raw) if month_raw else 0
    year = int(request.args.get('year', datetime.now().year))

    # Manager/Employee monthly drill-down view
    if month:
        if role == 'Employee':
            scores = query("""
                SELECT ps.*, e.full_name, d.department_name
                FROM Performance_Score ps
                JOIN Employee e ON ps.employee_id=e.employee_id
                JOIN Department d ON e.department_id=d.department_id
                WHERE ps.employee_id=? AND ps.period_month=? AND ps.period_year=?
            """, (uid, month, year))
            emp = query("SELECT full_name, position FROM Employee WHERE employee_id=?", (uid,), one=True)
            return render_template('performance/employee_view.html',
                                   scores=scores, month=month, year=year, emp=emp)
        elif role == 'Manager':
            scores = query("""
                SELECT ps.*, e.full_name, d.department_name
                FROM Performance_Score ps
                JOIN Employee e ON ps.employee_id=e.employee_id
                JOIN Department d ON e.department_id=d.department_id
                WHERE e.company_id=? AND e.branch_id=? AND ps.period_month=? AND ps.period_year=?
                ORDER BY e.full_name
            """, (co, session['branch_id'], month, year))
        else:
            scores = query("""
                SELECT ps.*, e.full_name, d.department_name, b.name as branch_name
                FROM Performance_Score ps
                JOIN Employee e ON ps.employee_id=e.employee_id
                JOIN Department d ON e.department_id=d.department_id
                JOIN Branch b ON e.branch_id=b.branch_id
                WHERE e.company_id=? AND ps.period_month=? AND ps.period_year=?
                ORDER BY e.full_name
            """, (co, month, year))
        return render_template('performance/monthly_list.html',
                               scores=scores, month=month, year=year)

    # Default: YEARLY view from Performance_Review
    if role == 'Employee':
        reviews = query("""
            SELECT pr.*, e.full_name, d.department_name
            FROM Performance_Review pr
            JOIN Employee e ON pr.employee_id=e.employee_id
            JOIN Department d ON e.department_id=d.department_id
            WHERE pr.employee_id=? AND pr.period_year=?
        """, (uid, year))
        emp = query("SELECT full_name, position FROM Employee WHERE employee_id=?", (uid,), one=True)
        return render_template('performance/employee_view.html',
                               reviews=reviews, year=year, emp=emp)
    elif role == 'Manager':
        reviews = query("""
            SELECT pr.*, e.full_name, d.department_name
            FROM Performance_Review pr
            JOIN Employee e ON pr.employee_id=e.employee_id
            JOIN Department d ON e.department_id=d.department_id
            WHERE e.company_id=? AND e.branch_id=? AND pr.period_year=?
            ORDER BY e.full_name
        """, (co, session['branch_id'], year))
    else:
        reviews = query("""
            SELECT pr.*, e.full_name, d.department_name, b.name as branch_name
            FROM Performance_Review pr
            JOIN Employee e ON pr.employee_id=e.employee_id
            JOIN Department d ON e.department_id=d.department_id
            JOIN Branch b ON e.branch_id=b.branch_id
            WHERE e.company_id=? AND pr.period_year=?
            ORDER BY e.full_name
        """, (co, year))

    summary = query("""
        SELECT COUNT(*) as count,
               ROUND(AVG(composite_score), 1) as avg_score,
               ROUND(AVG(attendance_rate), 1) as avg_att,
               ROUND(AVG(punctuality), 1) as avg_punct
        FROM Performance_Review
        WHERE period_year=?
    """, (year,), one=True) if reviews else None

    return render_template('performance/admin_list.html',
                           reviews=reviews, year=year, summary=summary, role=role)


@perf_bp.route('/generate', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager', 'HR Director')
def generate_scores():
    co = session['company_id']
    month = request.form.get('month', '')
    year = int(request.form.get('year', datetime.now().year))

    # If month is provided, generate monthly scores (drill-down); otherwise generate yearly review
    if month:
        month = int(month)
        att_exists = query("""
            SELECT COUNT(*) as c FROM Attendance
            WHERE strftime('%m', check_in) = ? AND strftime('%Y', check_in) = ?
              AND status = 'Approved'
        """, (f"{month:02d}", str(year)), one=True)
        if not att_exists or att_exists['c'] == 0:
            flash(f'No approved attendance records found for {month}/{year}. Cannot generate scores.', 'danger')
            return redirect(url_for('performance.list_scores', month=month, year=year))

        employees = query("SELECT employee_id FROM Employee WHERE company_id=? AND is_active=1", (co,))
        count = 0
        for emp in employees:
            eid = emp['employee_id']
            result = calculate_monthly_score(eid, month, year)
            if result:
                execute("""
                    INSERT OR REPLACE INTO Performance_Score
                    (employee_id, period_month, period_year, attendance_rate,
                     punctuality, overtime_score, reliability, composite_score, grade)
                    VALUES(?,?,?,?,?,?,?,?,?)
                """, (result['employee_id'], result['period_month'], result['period_year'],
                      result['attendance_rate'], result['punctuality'],
                      result['overtime_score'], result['reliability'],
                      result['composite_score'], result['grade']))
                count += 1

        log_audit('GENERATE_PERFORMANCE_MONTHLY', 'Performance',
                  f'Generated {count} monthly performance scores for {month}/{year}',
                  action_details={'month': month, 'year': year, 'count': count})
        flash(f'Generated {count} monthly performance score(s) for {month}/{year}.', 'success')
        return redirect(url_for('performance.list_scores', month=month, year=year))

    # Generate YEARLY review
    att_exists = query("""
        SELECT COUNT(*) as c FROM Attendance
        WHERE strftime('%Y', check_in) = ? AND status = 'Approved'
    """, (str(year),), one=True)
    if not att_exists or att_exists['c'] == 0:
        flash(f'No approved attendance records found for {year}. Cannot generate yearly review.', 'danger')
        return redirect(url_for('performance.list_scores', year=year))

    employees = query("SELECT employee_id FROM Employee WHERE company_id=? AND is_active=1", (co,))
    count = 0
    for emp in employees:
        eid = emp['employee_id']
        result = calculate_yearly_review(eid, year)
        if result:
            execute("""
                INSERT OR REPLACE INTO Performance_Review
                (employee_id, period_year, attendance_rate, punctuality,
                 overtime_score, reliability, composite_score, grade)
                VALUES(?,?,?,?,?,?,?,?)
            """, (result['employee_id'], result['period_year'],
                  result['attendance_rate'], result['punctuality'],
                  result['overtime_score'], result['reliability'],
                  result['composite_score'], result['grade']))
            count += 1

    log_audit('GENERATE_PERFORMANCE_YEARLY', 'Performance',
              f'Generated {count} yearly performance reviews for {year}',
              action_details={'year': year, 'count': count})
    flash(f'Generated {count} yearly performance review(s) for {year}.', 'success')
    return redirect(url_for('performance.list_scores', year=year))


@perf_bp.route('/api/score/<int:employee_id>')
@login_required
def api_score(employee_id):
    """Return the latest yearly performance review for an employee (for bonus/increment integration)."""
    year = int(request.args.get('year', datetime.now().year))

    score = query("""
        SELECT * FROM Performance_Review
        WHERE employee_id=? AND period_year=?
    """, (employee_id, year), one=True)

    if not score:
        return jsonify({'composite_score': 0, 'grade': 'N/A'})
    return jsonify({
        'composite_score': score['composite_score'],
        'grade': score['grade'],
        'attendance_rate': score['attendance_rate'],
        'punctuality': score['punctuality'],
    })
