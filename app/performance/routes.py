from flask import (render_template, request, session,
                   flash, redirect, url_for, jsonify)
from app.database import query, execute, log_audit
from app.auth.routes import login_required, role_required
from app.performance.calculator import calculate_monthly_score, calculate_yearly_review
from app.performance import perf_bp
from datetime import datetime, date, timedelta


def _period_from_request(month_raw, year_raw, *, allow_empty_month=True):
    """Return a safe reporting period without letting crafted values raise 500s."""
    try:
        month = int(month_raw) if month_raw not in (None, '') else 0
        year = int(year_raw) if year_raw not in (None, '') else datetime.now().year
    except (TypeError, ValueError):
        return None, None
    if year < 1 or year > 9999 or month < 0 or month > 12:
        return None, None
    if not allow_empty_month and month == 0:
        return None, None
    return month, year


def _add_yearly_attendance_evidence(reviews, year):
    """Attach the attendance counts used to explain yearly review scores.

    Performance_Review persists score components, not the underlying day
    counts. The review list still promises that evidence, so derive it from
    the same approved-attendance date range used by the yearly calculator.
    """
    if not reviews:
        return reviews

    review_rows = [dict(review) for review in reviews]
    employee_ids = [review['employee_id'] for review in review_rows]
    placeholders = ','.join('?' for _ in employee_ids)
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    attendance_rows = query(f"""
        SELECT e.employee_id, e.hire_date,
               COUNT(DISTINCT date(a.check_in)) AS days_present
        FROM Employee e
        LEFT JOIN Attendance a ON a.employee_id=e.employee_id
            AND a.status='Approved'
            AND date(a.check_in) >= CASE WHEN e.hire_date > ? THEN e.hire_date ELSE ? END
            AND date(a.check_in) <= ?
        WHERE e.employee_id IN ({placeholders})
        GROUP BY e.employee_id, e.hire_date
    """, [year_start.isoformat(), year_start.isoformat(), year_end.isoformat(), *employee_ids])
    attendance = {row['employee_id']: row for row in attendance_rows}

    for review in review_rows:
        row = attendance.get(review['employee_id'])
        start = year_start
        if row and row['hire_date']:
            try:
                hire_date = datetime.strptime(row['hire_date'], '%Y-%m-%d').date()
                start = max(start, hire_date)
            except ValueError:
                pass
        review['days_present'] = row['days_present'] if row else 0
        review['total_working_days'] = sum(
            1 for day_offset in range((year_end - start).days + 1)
            if (start + timedelta(days=day_offset)).weekday() < 5
        )
    return review_rows


def _yearly_summary(reviews):
    """Summarise the already scope-filtered review rows for the header cards."""
    if not reviews:
        return None
    count = len(reviews)
    return {
        'count': count,
        'avg_score': round(sum(review['composite_score'] for review in reviews) / count, 1),
        'avg_att': round(sum(review['attendance_rate'] for review in reviews) / count, 1),
        'avg_punct': round(sum(review['punctuality'] for review in reviews) / count, 1),
    }


@perf_bp.route('/')
@login_required
def list_scores():
    uid = session['user_id']
    role = session['user_role']
    co = session['company_id']

    month, year = _period_from_request(request.args.get('month', ''),
                                       request.args.get('year', ''))
    if month is None:
        flash('Use a valid month and year filter.', 'danger')
        month, year = 0, datetime.now().year

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

    reviews = _add_yearly_attendance_evidence(reviews, year)

    # `reviews` has already been filtered for the current user's permitted
    # branch/company scope. Derive the cards from those rows so a manager does
    # not see company-wide aggregate data above a branch-only table.
    summary = _yearly_summary(reviews)

    return render_template('performance/admin_list.html',
                           reviews=reviews, year=year, summary=summary, role=role)


@perf_bp.route('/generate', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager')
def generate_scores():
    co = session['company_id']
    month, year = _period_from_request(request.form.get('month', ''),
                                       request.form.get('year', ''))
    if month is None:
        flash('Use a valid month and year.', 'danger')
        return redirect(url_for('performance.list_scores'))

    # If month is provided, generate monthly scores (drill-down); otherwise generate yearly review
    if month:
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
    _, year = _period_from_request('', request.args.get('year', ''))
    if year is None:
        return jsonify({'error': 'Use a valid year.'}), 400

    target = query("SELECT employee_id, branch_id FROM Employee WHERE employee_id=?",
                   (employee_id,), one=True)
    if not target:
        return jsonify({'error': 'Employee not found.'}), 404

    role = session['user_role']
    if role == 'Employee' and target['employee_id'] != session['user_id']:
        return jsonify({'error': 'Access denied.'}), 403
    if role == 'Manager' and target['branch_id'] != session.get('branch_id'):
        return jsonify({'error': 'Access denied.'}), 403

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
