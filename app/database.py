"""
app/database.py  –  SQLite connection helpers and audit logging
"""
import sqlite3
import os
import json
from flask import g, request, session

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                       'instance', 'smarthr.db')


def get_db():
    """Return a per-request SQLite connection stored on Flask's g object."""
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def as_dict(row):
    """Convert sqlite3.Row to a plain dict (Row has no .get() method)."""
    return dict(row) if row is not None else None


def is_leave_eligible(leave_type, employee_gender, employee_marital_status):
    """Check if an employee is eligible for a leave type based on gender and marital status."""
    # Check gender eligibility
    eligible = True
    if leave_type.get('eligible_genders'):
        eligible_genders = [g.strip() for g in leave_type['eligible_genders'].split(',') if g.strip()]
        if employee_gender not in eligible_genders:
            eligible = False

    # Check marital status eligibility (only if still eligible and restriction exists)
    if eligible and leave_type.get('eligible_marital_status'):
        eligible_marital = [m.strip() for m in leave_type['eligible_marital_status'].split(',') if m.strip()]
        if employee_marital_status not in eligible_marital:
            eligible = False

    return eligible


def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def query(sql, args=(), one=False):
    """Execute a SELECT and return all rows (or one row)."""
    cur = get_db().execute(sql, args)
    rv = cur.fetchall()
    return (rv[0] if rv else None) if one else rv


def execute(sql, args=()):
    """Execute INSERT/UPDATE/DELETE; returns the lastrowid."""
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def close_job_posting_for_application(application_id):
    """Mark the related job posting as filled once a candidate is hired."""
    posting = query("""
        SELECT ja.posting_id
        FROM Job_Application ja
        WHERE ja.application_id=?
    """, (application_id,), one=True)
    if not posting or not posting['posting_id']:
        return False

    execute("""
        UPDATE Job_Posting
        SET status='Filled', closed_at=datetime('now')
        WHERE posting_id=? AND status != 'Filled'
    """, (posting['posting_id'],))
    return True


def log_audit(action, module_name, description,
              target_table=None, target_record_id=None,
              action_status='Success', action_details=None):
    """Write one row to AuditLog. Call after every important operation."""
    try:
        employee_id = session.get('user_id')
    except Exception:
        employee_id = None
    try:
        ip = request.remote_addr if request else None
    except Exception:
        ip = None
    try:
        ua = request.headers.get('User-Agent') if request else None
    except Exception:
        ua = None
    details_str = json.dumps(action_details) if action_details else None

    execute("""
        INSERT INTO AuditLog
        (employee_id, action, module_name, description,
         target_table, target_record_id,
         action_status, action_details, ip_address, user_agent)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (employee_id, action, module_name, description,
          target_table, str(target_record_id) if target_record_id else None,
          action_status, details_str, ip, ua))


# =============================================================================
# Permission Management Functions
# =============================================================================

def get_role_permissions(role_id):
    """Get all permissions for a given role."""
    return query("""
        SELECT p.permission_id, p.permission_name, p.description, p.module_name
        FROM Role_Permission rp
        JOIN Permission p ON rp.permission_id = p.permission_id
        WHERE rp.role_id = ?
        ORDER BY p.module_name, p.permission_name
    """, (role_id,))


def assign_role_permissions(employee_id, role_id, current_user_id=None):
    """
    Assign all permissions for a role to an employee.
    Revokes old permissions and grants new ones based on the role.
    """
    db = get_db()
    
    # Get all permissions for this role
    role_perms = query("""
        SELECT permission_id FROM Role_Permission WHERE role_id = ?
    """, (role_id,))
    
    role_perm_ids = [p['permission_id'] for p in role_perms]
    
    # Revoke all currently active permissions for this employee
    execute("""
        UPDATE Employee_Permission
        SET is_active = 0, revoked_at = datetime('now')
        WHERE employee_id = ? AND is_active = 1
    """, (employee_id,))
    
    # Grant all permissions for the new role
    for perm_id in role_perm_ids:
        execute("""
            INSERT INTO Employee_Permission
            (employee_id, permission_id, is_active, granted_by, reason)
            VALUES (?, ?, 1, ?, 'role_assignment')
        """, (employee_id, perm_id, current_user_id))
    
    db.commit()


def has_permission(employee_id, permission_name):
    """Check if an employee has a specific permission."""
    result = query("""
        SELECT 1 FROM Employee_Permission ep
        JOIN Permission p ON ep.permission_id = p.permission_id
        WHERE ep.employee_id = ? AND p.permission_name = ? AND ep.is_active = 1
        LIMIT 1
    """, (employee_id, permission_name), one=True)
    return result is not None


def get_employee_permissions(employee_id):
    """Get all active permissions for an employee."""
    return query("""
        SELECT p.permission_id, p.permission_name, p.description, p.module_name
        FROM Employee_Permission ep
        JOIN Permission p ON ep.permission_id = p.permission_id
        WHERE ep.employee_id = ? AND ep.is_active = 1
        ORDER BY p.module_name, p.permission_name
    """, (employee_id,))
