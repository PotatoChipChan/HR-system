from datetime import datetime, date
from collections import defaultdict
from app.database import query


def _grade_for_score(composite):
    """Return A/B/C/D grade for a composite score."""
    if composite >= 85:
        return 'A'
    elif composite >= 70:
        return 'B'
    elif composite >= 55:
        return 'C'
    else:
        return 'D'


def _working_days_in_month(month, year, employee_id=None):
    """Count weekdays (Mon-Fri) in a given month."""
    import calendar
    total = 0
    num_days = calendar.monthrange(year, month)[1]
    for d in range(1, num_days + 1):
        dt = date(year, month, d)
        if dt.weekday() < 5:
            total += 1
    return total


def _get_employee_work_start(employee_id):
    """Get work_start_time for an employee, default 09:00."""
    emp = query("SELECT work_start_time FROM Employee WHERE employee_id=?", (employee_id,), one=True)
    if emp and emp['work_start_time']:
        return emp['work_start_time']
    return '09:00'


def calculate_monthly_score(employee_id, month, year):
    """Calculate performance score for an employee for a given month/year.
    Returns dict with all component scores and composite.
    """
    # Attendance records for the month
    att_records = query("""
        SELECT * FROM Attendance
        WHERE employee_id = ?
          AND strftime('%m', check_in) = ?
          AND strftime('%Y', check_in) = ?
          AND status = 'Approved'
        ORDER BY check_in
    """, (employee_id, f"{month:02d}", str(year)))

    if not att_records:
        return None

    total_working_days = _working_days_in_month(month, year, employee_id)
    if total_working_days == 0:
        return None

    work_start = _get_employee_work_start(employee_id)

    # Build per-day data from earliest check-in per day
    day_data = {}  # day_str -> {'check_in': str, 'hours_worked': float, 'has_manual': bool}
    for rec in att_records:
        ci = rec['check_in']
        day = ci[:10]
        if day not in day_data or ci < day_data[day]['check_in']:
            day_data[day] = {
                'check_in': ci,
                'hours_worked': rec['hours_worked'] or 0.0,
                'has_manual': bool(rec['is_manual_entry']),
            }

    days_present = set(day_data.keys())
    on_time_days = set()
    full_day_days = set()
    total_ot = 0.0

    for day, data in day_data.items():
        ci = data['check_in']
        ci_time = ci[11:16]
        if ci_time <= work_start:
            on_time_days.add(day)
        if data['hours_worked'] >= 6:
            full_day_days.add(day)

    # Overtime (sum across ALL records, not just earliest per day)
    for rec in att_records:
        total_ot += rec['overtime_hours'] or 0.0

    # 1. Attendance Rate (40%)
    attendance_rate = min(100.0, (len(days_present) / total_working_days) * 100)

    # 2. Punctuality (30%) per unique day
    if len(days_present) > 0:
        punctuality = (len(on_time_days) / len(days_present)) * 100
    else:
        punctuality = 0.0

    # 3. Overtime Score (15%) — cap at 40 hours/month
    ot_cap = 40.0
    overtime_score = min(100.0, (total_ot / ot_cap) * 100)

    # 4. Reliability (15%) — % of all working days with >= 6 hours worked
    reliability = (len(full_day_days) / total_working_days) * 100

    # Weighted composite
    composite = (
        attendance_rate * 0.40 +
        punctuality * 0.30 +
        overtime_score * 0.15 +
        reliability * 0.15
    )

    # Grade
    if composite >= 85:
        grade = 'A'
    elif composite >= 70:
        grade = 'B'
    elif composite >= 55:
        grade = 'C'
    else:
        grade = 'D'

    return {
        'employee_id': employee_id,
        'period_month': month,
        'period_year': year,
        'attendance_rate': round(attendance_rate, 2),
        'punctuality': round(punctuality, 2),
        'overtime_score': round(overtime_score, 2),
        'reliability': round(reliability, 2),
        'composite_score': round(composite, 2),
        'grade': _grade_for_score(composite),
        'days_present': len(days_present),
        'total_working_days': total_working_days,
        'total_ot': round(total_ot, 2),
    }


def _working_days_between(start, end):
    """Count weekdays between two dates (inclusive)."""
    from datetime import timedelta
    total = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            total += 1
        cur += timedelta(days=1)
    return total


def calculate_yearly_review(employee_id, year):
    """Calculate a yearly performance review for an employee.

    Aggregates attendance from 1 Jan to 31 Dec of the year.
    For new hires, counts working days from hire_date.
    Returns dict with component scores, composite, grade.
    """
    from datetime import timedelta

    emp = query("SELECT hire_date, work_start_time FROM Employee WHERE employee_id=?", (employee_id,), one=True)
    if not emp:
        return None

    work_start = emp['work_start_time'] or '09:00'

    start_date = date(year, 1, 1)
    end_date = date(year, 12, 31)
    if emp['hire_date']:
        hire = datetime.strptime(emp['hire_date'], '%Y-%m-%d').date()
        if hire > start_date:
            start_date = hire

    if start_date > end_date:
        return None

    total_working_days = _working_days_between(start_date, end_date)
    if total_working_days == 0:
        return None

    att_records = query("""
        SELECT * FROM Attendance
        WHERE employee_id = ?
          AND date(check_in) >= ?
          AND date(check_in) <= ?
          AND status = 'Approved'
        ORDER BY check_in
    """, (employee_id, start_date.isoformat(), end_date.isoformat()))

    day_data = {}
    for rec in att_records:
        ci = rec['check_in']
        day = ci[:10]
        if day not in day_data or ci < day_data[day]['check_in']:
            day_data[day] = {
                'check_in': ci,
                'hours_worked': rec['hours_worked'] or 0.0,
                'has_manual': bool(rec['is_manual_entry']),
            }

    on_time_days = set()
    full_day_days = set()
    total_ot = 0.0
    for day, data in day_data.items():
        ci_time = data['check_in'][11:16]
        if ci_time <= work_start:
            on_time_days.add(day)
        if data['hours_worked'] >= 6:
            full_day_days.add(day)

    for rec in att_records:
        total_ot += rec['overtime_hours'] or 0.0

    days_present = set(day_data.keys())

    attendance_rate = min(100.0, (len(days_present) / total_working_days) * 100)
    punctuality = (len(on_time_days) / len(days_present) * 100) if days_present else 0.0
    ot_cap = 40.0 * 12
    overtime_score = min(100.0, (total_ot / ot_cap) * 100)
    reliability = (len(full_day_days) / total_working_days) * 100

    composite = (
        attendance_rate * 0.40 +
        punctuality * 0.30 +
        overtime_score * 0.15 +
        reliability * 0.15
    )

    return {
        'employee_id': employee_id,
        'period_year': year,
        'attendance_rate': round(attendance_rate, 2),
        'punctuality': round(punctuality, 2),
        'overtime_score': round(overtime_score, 2),
        'reliability': round(reliability, 2),
        'composite_score': round(composite, 2),
        'grade': _grade_for_score(composite),
        'days_present': len(days_present),
        'total_working_days': total_working_days,
        'total_ot': round(total_ot, 2),
    }
