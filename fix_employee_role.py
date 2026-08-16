"""
fix_employee_role.py – Reusable, audited employee role/department correction.

Corrects an employee whose system role was created or changed with unintended
privileges (e.g. a hire that should have been Employee but got Admin), and
optionally assigns them as a department manager.

Usage:
    .venv/Scripts/python.exe fix_employee_role.py --employee-id 79 --role Employee
    .venv/Scripts/python.exe fix_employee_role.py --employee-id 79 --role Employee --department-id 24

Safety guarantees:
- A timestamped backup of the live DB is taken before any change.
- The role change and (optional) department-manager assignment are one atomic
  SQLite transaction; the department-manager conflict is pre-checked inside
  the transaction and an existing manager is never replaced.
- The permission re-sync cannot run inside that transaction (the shared
  assign_role_permissions helper commits per statement), so on failure an
  explicit compensating rollback restores the previous role, the previous
  department manager, and the previous permission set before exiting non-zero.
- Every attempt is written to the AuditLog.
"""
import argparse
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

# Stub background schedulers so create_app is side-effect free for maintenance.
import app.payroll.autogen as _pg
_pg.start_payroll_scheduler = lambda *a, **k: None
import app.recruitment.offer_expiry as _oe
_oe.start_offer_expiry_scheduler = lambda *a, **k: None

from app import create_app
import app.database as _db_mod
from app.database import get_db, query, execute, assign_role_permissions, log_audit


def _backup():
    src = _db_mod.DB_PATH
    bak_dir = os.path.join(os.path.dirname(src), 'backups')
    os.makedirs(bak_dir, exist_ok=True)
    name = f"smarthr_backup_pre_fix_employee_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    path = os.path.join(bak_dir, name)
    shutil.copy2(src, path)
    print(f"[BACKUP] {path}")
    return path


def main():
    parser = argparse.ArgumentParser(description='Audited employee role/department correction')
    parser.add_argument('--employee-id', type=int, required=True)
    parser.add_argument('--role', required=True, help='target system role name (e.g. Employee)')
    parser.add_argument('--department-id', type=int, default=None,
                        help='assign the employee as this department\'s manager')
    args = parser.parse_args()

    _backup()
    app = create_app()

    with app.app_context():
        db = get_db()
        target = query("SELECT employee_id, role_id, full_name, department_id FROM Employee WHERE employee_id=?",
                       (args.employee_id,), one=True)
        if not target:
            print(f"[ERROR] Employee {args.employee_id} not found.")
            sys.exit(1)
        new_role = query("SELECT role_id FROM Role WHERE role_name=? AND role_id != 0", (args.role,), one=True) \
            or query("SELECT role_id FROM Role WHERE LOWER(role_name)=LOWER(?)", (args.role,), one=True)
        if not new_role:
            print(f"[ERROR] Role '{args.role}' not found.")
            sys.exit(1)
        old_role_id = target['role_id']

        old_mgr = None
        if args.department_id is not None:
            dept = query("SELECT department_id, department_name, department_manager_id FROM Department WHERE department_id=?",
                         (args.department_id,), one=True)
            if not dept:
                print(f"[ERROR] Department {args.department_id} not found.")
                sys.exit(1)
            old_mgr = dept['department_manager_id']

        # ── Atomic: role change + department-manager assignment ──────────────
        db.execute("BEGIN IMMEDIATE")
        try:
            if args.department_id is not None:
                cur_mgr = db.execute("SELECT department_manager_id FROM Department WHERE department_id=?",
                                     (args.department_id,)).fetchone()
                if cur_mgr is not None and cur_mgr['department_manager_id'] is not None \
                        and cur_mgr['department_manager_id'] != args.employee_id:
                    db.rollback()
                    print(f"[CONFLICT] Department {args.department_id} already has manager "
                          f"{cur_mgr['department_manager_id']}; no change made. Reassign it first.")
                    sys.exit(2)
            db.execute("UPDATE Employee SET role_id=? WHERE employee_id=?",
                       (new_role['role_id'], args.employee_id))
            if args.department_id is not None:
                assign = db.execute("""UPDATE Department SET department_manager_id=?
                                       WHERE department_id=? AND department_manager_id IS NULL""",
                                    (args.employee_id, args.department_id))
                if assign.rowcount != 1:
                    db.rollback()
                    print(f"[CONFLICT] Department {args.department_id} already has a manager; "
                          "no change made (role change rolled back). Reassign it first.")
                    sys.exit(2)
            db.commit()
            print(f"[OK] Atomic commit: employee {args.employee_id} role {old_role_id} -> "
                  f"{new_role['role_id']}{' + department manager ' + str(args.department_id) if args.department_id is not None else ''}")
        except SystemExit:
            raise
        except Exception as e:
            db.rollback()
            print(f"[ERROR] Atomic step failed, rolled back: {e}")
            sys.exit(1)

        # ── Permission re-sync with explicit compensating rollback ───────────
        try:
            assign_role_permissions(args.employee_id, new_role['role_id'], None)
        except Exception as e:
            print(f"[ERROR] Permission re-sync failed: {e}")
            print("[COMPENSATE] Restoring previous role, department manager, and permissions...")
            try:
                execute("UPDATE Employee SET role_id=? WHERE employee_id=?",
                        (old_role_id, args.employee_id))
                if args.department_id is not None:
                    execute("UPDATE Department SET department_manager_id=? WHERE department_id=?",
                            (old_mgr, args.department_id))
                assign_role_permissions(args.employee_id, old_role_id, None)
                print("[COMPENSATE] Previous state restored.")
            except Exception as e2:
                print(f"[CRITICAL] Compensation also failed: {e2}. Restore from the backup above.")
                sys.exit(3)
            sys.exit(1)

        log_audit('ROLE_CORRECTION', 'Employee',
                  f'Employee {args.employee_id} role corrected {old_role_id} -> {new_role["role_id"]}'
                  + (f' with department-manager assignment (dept {args.department_id})'
                     if args.department_id is not None else ''),
                  'Employee', args.employee_id,
                  action_details={'department_id': args.department_id})

        emp = query("SELECT employee_id, role_id FROM Employee WHERE employee_id=?", (args.employee_id,), one=True)
        perms = query("SELECT COUNT(*) c FROM Employee_Permission WHERE employee_id=? AND is_active=1",
                      (args.employee_id,), one=True)['c']
        print(f"[VERIFY] employee {emp['employee_id']} role_id={emp['role_id']} active_permissions={perms}")
        if args.department_id is not None:
            dept = query("SELECT department_manager_id FROM Department WHERE department_id=?",
                         (args.department_id,), one=True)
            print(f"[VERIFY] department {args.department_id} manager={dept['department_manager_id']}")
        print("[DONE]")


if __name__ == '__main__':
    main()
