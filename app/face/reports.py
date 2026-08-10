"""
Attendance Reports & Analytics Module
- Attendance records with filtering
- Statistics & KPIs
- Charts & trends
- PDF/CSV export
"""
import csv
import io
import json
from datetime import datetime, timedelta
from flask import render_template, request, jsonify, session, send_file
from app.face import face_bp
from app.database import get_db
from functools import wraps

# ─────────────────────────────────────────────────────────────────────────────
# DECORATORS
# ─────────────────────────────────────────────────────────────────────────────

def login_required(f):
    """Check if user is logged in"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            from flask import redirect, url_for
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    """Check if user has required role.
    HR Manager inherits all HR permissions automatically."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                from flask import redirect, url_for
                return redirect(url_for('auth.login'))
            
            conn = get_db()
            user = conn.execute(
                "SELECT role_id FROM Employee WHERE employee_id = ?",
                (session['user_id'],)
            ).fetchone()
            
            if not user:
                from flask import redirect, url_for
                return redirect(url_for('auth.login'))
            
            conn = get_db()
            role = conn.execute(
                "SELECT role_name FROM Role WHERE role_id = ?",
                (user['role_id'],)
            ).fetchone()
            
            role_name = role['role_name']
            check_roles = list(roles)
            if role_name == 'HR Manager' and 'HR' in roles:
                check_roles.append('HR Manager')
            
            if role_name not in check_roles:
                return jsonify({'success': False, 'msg': 'Access denied. Admin or HR role required.'}), 403
            
            return f(*args, **kwargs)
        return decorated
    return decorator

# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def get_user_role():
    """Get logged-in user's role"""
    if 'user_id' not in session:
        return None
    
    conn = get_db()
    user = conn.execute(
        "SELECT role_id FROM Employee WHERE employee_id = ?",
        (session['user_id'],)
    ).fetchone()
    
    if not user:
        return None
    
    role = conn.execute(
        "SELECT role_name FROM Role WHERE role_id = ?",
        (user['role_id'],)
    ).fetchone()
    
    return role['role_name'] if role else None

def get_attendance_records(filters=None):
    """
    Get attendance records with optional filtering
    
    Args:
        filters: dict with keys:
            - from_date: YYYY-MM-DD
            - to_date: YYYY-MM-DD
            - employee_id: int
            - branch_id: int
            - department_id: int
            - status: 'Pending'/'Approved'
            - manual_only: bool (true for manual entries only)
    
    Returns:
        List of attendance records
    """
    if filters is None:
        filters = {}
    
    query = """
        SELECT 
            a.attendance_id,
            a.employee_id,
            e.full_name,
            e.position,
            a.branch_id,
            b.name as branch_name,
            d.department_name,
            a.check_in,
            a.check_out,
            a.hours_worked,
            a.overtime_hours,
            a.status,
            a.is_manual_entry
        FROM Attendance a
        JOIN Employee e ON a.employee_id = e.employee_id
        JOIN Branch b ON a.branch_id = b.branch_id
        JOIN Department d ON e.department_id = d.department_id
        WHERE 1=1
    """
    
    params = []
    
    # Date range
    if filters.get('from_date'):
        query += " AND date(a.check_in) >= ?"
        params.append(filters['from_date'])
    
    if filters.get('to_date'):
        query += " AND date(a.check_in) <= ?"
        params.append(filters['to_date'])
    
    # Employee filter
    if filters.get('employee_id'):
        query += " AND a.employee_id = ?"
        params.append(filters['employee_id'])
    
    # Branch filter
    if filters.get('branch_id'):
        query += " AND a.branch_id = ?"
        params.append(filters['branch_id'])
    
    # Department filter
    if filters.get('department_id'):
        query += " AND d.department_id = ?"
        params.append(filters['department_id'])
    
    # Status filter
    if filters.get('status'):
        query += " AND a.status = ?"
        params.append(filters['status'])
    
    # Manual entry filter
    if filters.get('manual_only'):
        query += " AND a.is_manual_entry = 1"
    
    query += " ORDER BY a.check_in DESC"
    
    conn = get_db()
    records = conn.execute(query, params).fetchall()
    conn.close()
    
    return records

def calculate_statistics(records):
    """Calculate attendance statistics from records"""
    if not records:
        return {
            'total_records': 0,
            'total_hours': 0,
            'total_overtime': 0,
            'avg_hours_per_day': 0,
            'attendance_rate': 0,
            'on_time_count': 0,
            'late_count': 0
        }
    
    total_hours = sum(r['hours_worked'] if r['hours_worked'] else 0 for r in records)
    total_overtime = sum(r['overtime_hours'] if r['overtime_hours'] else 0 for r in records)
    
    # Group by date to get days present
    dates = set()
    for r in records:
        dates.add(r['check_in'].split(' ')[0])
    
    return {
        'total_records': len(records),
        'total_hours': round(total_hours, 2),
        'total_overtime': round(total_overtime, 2),
        'avg_hours_per_day': round(total_hours / len(dates) if dates else 0, 2),
        'days_present': len(dates),
        'on_time_count': len([r for r in records if r['hours_worked'] and r['hours_worked'] >= 8]),
        'late_count': len([r for r in records if r['hours_worked'] and r['hours_worked'] < 8])
    }

def get_daily_stats(records):
    """Get daily breakdown of attendance"""
    daily_stats = {}
    
    for record in records:
        date = record['check_in'].split(' ')[0]
        
        if date not in daily_stats:
            daily_stats[date] = {
                'date': date,
                'present': 0,
                'total_hours': 0,
                'employees': 0
            }
        
        daily_stats[date]['present'] += 1
        if record['hours_worked']:
            daily_stats[date]['total_hours'] += record['hours_worked']
    
    return sorted(daily_stats.values(), key=lambda x: x['date'])

def get_employee_stats(records):
    """Get per-employee statistics"""
    employee_stats = {}
    
    for record in records:
        emp_id = record['employee_id']
        
        if emp_id not in employee_stats:
            employee_stats[emp_id] = {
                'employee_id': emp_id,
                'full_name': record['full_name'],
                'position': record['position'],
                'check_ins': 0,
                'total_hours': 0,
                'avg_hours': 0
            }
        
        employee_stats[emp_id]['check_ins'] += 1
        if record['hours_worked']:
            employee_stats[emp_id]['total_hours'] += record['hours_worked']
    
    # Calculate averages
    for emp_id in employee_stats:
        emp = employee_stats[emp_id]
        emp['avg_hours'] = round(emp['total_hours'] / emp['check_ins'] if emp['check_ins'] > 0 else 0, 2)
        emp['total_hours'] = round(emp['total_hours'], 2)
    
    return sorted(employee_stats.values(), key=lambda x: x['full_name'])

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@face_bp.route('/reports', methods=['GET'])
@login_required
def attendance_report():
    """Attendance report viewer with filters"""
    user_role = get_user_role()
    emp_id = session.get('user_id')
    
    conn = get_db()
    
    # Get branches and departments for filters
    branches = conn.execute("SELECT * FROM Branch ORDER BY name").fetchall()
    departments = conn.execute("SELECT * FROM Department ORDER BY department_name").fetchall()
    
    # For non-HR users, show only their own data
    if user_role == 'Employee':
        employee = conn.execute(
            "SELECT * FROM Employee WHERE employee_id = ?",
            (emp_id,)
        ).fetchone()
        conn.close()
        
        return render_template('face/attendance_report.html',
                              user_role=user_role,
                              own_employee=employee,
                              branches=branches,
                              departments=departments,
                              show_all_employees=False)
    else:
        # HR/Admin can see all employees
        employees = conn.execute(
            "SELECT employee_id, full_name, position FROM Employee WHERE employment_status = 'Active' ORDER BY full_name"
        ).fetchall()
        conn.close()
        
        return render_template('face/attendance_report.html',
                              user_role=user_role,
                              employees=employees,
                              branches=branches,
                              departments=departments,
                              show_all_employees=True)

@face_bp.route('/api/report_data', methods=['GET'])
@login_required
def api_get_report_data():
    """API endpoint to get filtered attendance data"""
    try:
        user_role = get_user_role()
        emp_id = session.get('user_id')
        
        # Build filters from query params
        filters = {
            'from_date': request.args.get('from_date'),
            'to_date': request.args.get('to_date'),
            'branch_id': request.args.get('branch_id', type=int),
            'department_id': request.args.get('department_id', type=int),
            'status': request.args.get('status'),
            'manual_only': request.args.get('manual_only', 'false').lower() == 'true'
        }
        
        # Add employee filter
        req_emp_id = request.args.get('employee_id', type=int)
        if user_role == 'Employee':
            # Employees can only see their own data
            filters['employee_id'] = emp_id
        elif req_emp_id:
            filters['employee_id'] = req_emp_id
        
        # Get records
        records = get_attendance_records(filters)
        
        # Convert records to JSON
        records_json = []
        for rec in records:
            records_json.append({
                'attendance_id': rec['attendance_id'],
                'employee_id': rec['employee_id'],
                'full_name': rec['full_name'],
                'position': rec['position'],
                'branch_name': rec['branch_name'],
                'department_name': rec['department_name'],
                'check_in': rec['check_in'],
                'check_out': rec['check_out'],
                'hours_worked': rec['hours_worked'],
                'overtime_hours': rec['overtime_hours'],
                'status': rec['status'],
                'manual': bool(rec['is_manual_entry'])
            })
        
        # Calculate statistics
        stats = calculate_statistics(records)
        daily_breakdown = get_daily_stats(records)
        employee_breakdown = get_employee_stats(records)
        
        return jsonify({
            'success': True,
            'records': records_json,
            'statistics': stats,
            'daily_breakdown': daily_breakdown,
            'employee_breakdown': employee_breakdown
        })
    
    except Exception as e:
        print(f"Report data error: {e}")
        return jsonify({
            'success': False,
            'msg': f'Error: {str(e)}'
        }), 500

@face_bp.route('/export/csv', methods=['GET'])
@login_required
def export_csv():
    """Export attendance report as CSV"""
    try:
        user_role = get_user_role()
        emp_id = session.get('user_id')
        
        # Build filters
        filters = {
            'from_date': request.args.get('from_date'),
            'to_date': request.args.get('to_date'),
            'branch_id': request.args.get('branch_id', type=int),
            'department_id': request.args.get('department_id', type=int),
            'status': request.args.get('status'),
        }
        
        if user_role == 'Employee':
            filters['employee_id'] = emp_id
        elif request.args.get('employee_id'):
            filters['employee_id'] = int(request.args.get('employee_id'))
        
        records = get_attendance_records(filters)
        
        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Header
        writer.writerow([
            'Employee ID', 'Full Name', 'Position', 'Branch', 'Department',
            'Date', 'Check In', 'Check Out', 'Hours Worked', 'Overtime', 'Status', 'Manual Entry'
        ])
        
        # Data rows
        for rec in records:
            check_in_date = rec['check_in'].split(' ')[0] if rec['check_in'] else ''
            check_in_time = rec['check_in'].split(' ')[1] if rec['check_in'] and ' ' in rec['check_in'] else ''
            check_out_time = rec['check_out'].split(' ')[1] if rec['check_out'] and ' ' in rec['check_out'] else ''
            
            writer.writerow([
                rec['employee_id'],
                rec['full_name'],
                rec['position'],
                rec['branch_name'],
                rec['department_name'],
                check_in_date,
                check_in_time,
                check_out_time,
                rec['hours_worked'] or '',
                rec['overtime_hours'] or '',
                rec['status'],
                'Yes' if rec['is_manual_entry'] else 'No'
            ])
        
        # Return CSV file
        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode('utf-8')),
            mimetype='text/csv',
            as_attachment=True,
            download_name=f'attendance_report_{datetime.now().strftime("%Y%m%d")}.csv'
        )
    
    except Exception as e:
        print(f"CSV export error: {e}")
        return jsonify({'success': False, 'msg': str(e)}), 500

@face_bp.route('/analytics', methods=['GET'])
@login_required
@role_required('Admin', 'HR', 'Manager')
def attendance_analytics():
    """Analytics dashboard for HR/Managers"""
    user_role = get_user_role()
    
    if user_role not in ['Admin', 'HR', 'HR Manager', 'Manager']:
        from flask import redirect, url_for
        return redirect(url_for('main.dashboard'))
    
    conn = get_db()
    branches = conn.execute("SELECT * FROM Branch ORDER BY name").fetchall()
    departments = conn.execute("SELECT * FROM Department ORDER BY department_name").fetchall()
    conn.close()
    
    return render_template('face/attendance_analytics.html',
                          user_role=user_role,
                          branches=branches,
                          departments=departments)

@face_bp.route('/api/analytics_data', methods=['GET'])
@login_required
@role_required('Admin', 'HR', 'Manager')
def api_get_analytics_data():
    """API endpoint for analytics dashboard data"""
    try:
        filters = {
            'from_date': request.args.get('from_date'),
            'to_date': request.args.get('to_date'),
            'branch_id': request.args.get('branch_id', type=int),
            'department_id': request.args.get('department_id', type=int),
        }
        
        records = get_attendance_records(filters)
        
        # Get statistics
        stats = calculate_statistics(records)
        daily_breakdown = get_daily_stats(records)
        employee_breakdown = get_employee_stats(records)
        
        # Get department-wise breakdown
        dept_stats = {}
        for rec in records:
            dept = rec['department_name']
            if dept not in dept_stats:
                dept_stats[dept] = {
                    'department': dept,
                    'employees': set(),
                    'total_records': 0,
                    'total_hours': 0
                }
            
            dept_stats[dept]['employees'].add(rec['employee_id'])
            dept_stats[dept]['total_records'] += 1
            if rec['hours_worked']:
                dept_stats[dept]['total_hours'] += rec['hours_worked']
        
        # Convert to list
        dept_breakdown = []
        for dept, data in dept_stats.items():
            dept_breakdown.append({
                'department': dept,
                'employee_count': len(data['employees']),
                'records': data['total_records'],
                'total_hours': round(data['total_hours'], 2)
            })
        
        dept_breakdown.sort(key=lambda x: x['total_hours'], reverse=True)
        
        return jsonify({
            'success': True,
            'statistics': stats,
            'daily_breakdown': daily_breakdown,
            'employee_breakdown': employee_breakdown,
            'department_breakdown': dept_breakdown
        })
    
    except Exception as e:
        print(f"Analytics data error: {e}")
        return jsonify({
            'success': False,
            'msg': f'Error: {str(e)}'
        }), 500
