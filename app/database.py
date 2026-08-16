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


def _reject_active_candidates(posting_id, exclude_application_id, job_title):
    """Reject the remaining active candidates of a posting.

    Called when all approved openings are filled. Rejection emails are sent
    per candidate; failures are isolated. Audit event is written once."""
    others = query("""
        SELECT application_id, applicant_name, applicant_email
        FROM Job_Application
        WHERE posting_id=? AND application_id!=?
          AND status IN ('New','Shortlisted','Interview','Offered')
    """, (posting_id, exclude_application_id))
    if not others:
        return

    try:
        uid = session.get('user_id')
    except Exception:
        uid = None
    from flask import render_template
    for o in others:
        execute("""
            UPDATE Job_Application SET status='Rejected', reviewed_by=?,
                   reviewed_at=datetime('now')
            WHERE application_id=?
        """, (uid, o['application_id']))
        if o['applicant_email']:
            try:
                from app.notifications.email_service import send_email
                html = render_template('emails/application_rejected.html',
                    employee_name=o['applicant_name'],
                    title='Application Update',
                    job_title=job_title)
                send_email(f'Application Status – {job_title}', o['applicant_email'], html)
            except Exception as e:
                print(f"[OPENING FILL] Rejection email failed for {o['application_id']}: {e}")

    try:
        log_audit('AUTO_REJECT_CANDIDATES', 'Recruitment',
                  f'Auto-rejected {len(others)} candidate(s) for posting #{posting_id} '
                  'after all approved openings were filled',
                  action_details={'posting_id': posting_id,
                                  'hired_aid': exclude_application_id,
                                  'rejected_count': len(others)})
    except Exception as e:
        print(f"[OPENING FILL] Audit log failed: {e}")


def close_job_posting_for_application(application_id):
    """Fill one approved opening for the application's posting.

    Backward-compatible name: the function no longer closes a posting on the
    first hire. Instead it performs opening accounting:
      - ensures a Filled Opening_Reservation row exists for the application
      - increments Job_Posting.filled_openings
      - updates the posting status: Open / Partially Filled / Archived
        (a fully filled posting is archived automatically - soft deleted)
      - when filled_openings == approved_openings, the remaining active
        candidates are rejected (emails + audit)

    Returns True when a posting was updated, False otherwise."""
    posting = query("""
        SELECT ja.posting_id FROM Job_Application ja
        WHERE ja.application_id=?
    """, (application_id,), one=True)
    if not posting or not posting['posting_id']:
        return False
    pid = posting['posting_id']

    jp = query("""
        SELECT jp.*, b.company_id FROM Job_Posting jp
        JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE jp.posting_id=?
    """, (pid,), one=True)
    if not jp:
        return False

    approved = int(jp['approved_openings'] or 1)
    filled = int(jp['filled_openings'] or 0)

    active = query("""
        SELECT reservation_id FROM Opening_Reservation
        WHERE application_id=? AND status IN ('Reserved','Filled')
        LIMIT 1
    """, (application_id,), one=True)
    if active:
        execute("UPDATE Opening_Reservation SET status='Filled' WHERE reservation_id=?",
                (active['reservation_id'],))
    else:
        execute("""INSERT INTO Opening_Reservation (posting_id, application_id, status)
                   VALUES (?,?,'Filled')""", (pid, application_id))

    new_filled = min(filled + 1, approved)
    if new_filled >= approved:
        # Fully filled postings are auto-archived (soft deleted): they leave
        # the active and closed lists while every record is preserved.
        new_status = 'Archived'
        execute("""UPDATE Job_Posting SET filled_openings=?, status=?,
                          closed_at=COALESCE(closed_at, datetime('now'))
                   WHERE posting_id=?""", (new_filled, new_status, pid))
    elif new_filled > 0:
        new_status = 'Partially Filled'
        execute("""UPDATE Job_Posting SET filled_openings=?, status=?
                   WHERE posting_id=?""", (new_filled, new_status, pid))
    else:
        new_status = 'Open'
        execute("""UPDATE Job_Posting SET filled_openings=?, status=?
                   WHERE posting_id=?""", (new_filled, new_status, pid))

    if new_filled >= approved:
        _reject_active_candidates(pid, application_id, jp['title'])
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
    
    # Grant all permissions for the new role. Previous grants may already
    # exist as revoked rows (e.g. when demoting from a superset role), so
    # reactivate those instead of duplicating the (employee, permission) pair.
    for perm_id in role_perm_ids:
        execute("""
            INSERT OR IGNORE INTO Employee_Permission
            (employee_id, permission_id, is_active, granted_by, reason)
            VALUES (?, ?, 1, ?, 'role_assignment')
        """, (employee_id, perm_id, current_user_id))
        execute("""
            UPDATE Employee_Permission
            SET is_active = 1, revoked_at = NULL
            WHERE employee_id = ? AND permission_id = ? AND is_active = 0
        """, (employee_id, perm_id))
    
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
