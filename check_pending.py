import sqlite3, os
from collections import Counter

db_path = os.path.join(os.path.dirname(__file__), 'instance', 'smarthr.db')
db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

# Check what the UNION query returns for Admin/HR
co = 1
user_role = 'HR Manager'

union_parts = []
union_params = []

# Leave
union_parts.append("SELECT * FROM (SELECT 'Leave' as type, 'LV-'||la.leave_id as ref, e.full_name as owner, la.status FROM Leave_Application la JOIN Employee e ON la.employee_id=e.employee_id WHERE la.status='Pending' AND e.company_id=? LIMIT 5)")
union_params.append(co)

# Invoice
union_parts.append("SELECT * FROM (SELECT 'Invoice' as type, i.invoice_number as ref, e.full_name as owner, i.status FROM Invoice i JOIN Employee e ON i.employee_id=e.employee_id WHERE i.status='Pending' AND e.company_id=? LIMIT 5)")
union_params.append(co)

# Vacancy
union_parts.append("SELECT * FROM (SELECT 'Vacancy' as type, 'VR-'||vr.request_id as ref, e.full_name as owner, vr.status FROM Vacancy_Request vr JOIN Department d ON vr.department_id=d.department_id JOIN Branch b ON d.branch_id=b.branch_id JOIN Employee e ON vr.requested_by=e.employee_id WHERE vr.status='Pending' AND b.company_id=? LIMIT 5)")
union_params.append(co)

# Applications
union_parts.append("SELECT * FROM (SELECT 'Application' as type, 'A-'||ja.application_id as ref, ja.applicant_name as owner, ja.status FROM Job_Application ja JOIN Job_Posting jp ON ja.posting_id=jp.posting_id JOIN Branch b ON jp.branch_id=b.branch_id WHERE ja.status='New' AND b.company_id=? LIMIT 5)")
union_params.append(co)

# Bonus + Increment (Admin/HR Manager only)
if user_role in ('Admin', 'HR Manager', 'HR Director'):
    union_parts.append("SELECT * FROM (SELECT 'Bonus' as type, 'BP-'||bp.proposal_id as ref, e.full_name as owner, bp.status FROM Bonus_Proposal bp JOIN Employee e ON bp.employee_id=e.employee_id WHERE bp.status='Pending' AND e.company_id=? LIMIT 5)")
    union_params.append(co)
    union_parts.append("SELECT * FROM (SELECT 'Increment' as type, 'SI-'||si.increment_id as ref, e.full_name as owner, si.status FROM Salary_Increment si JOIN Employee e ON si.employee_id=e.employee_id WHERE si.status='Pending' AND e.company_id=? LIMIT 5)")
    union_params.append(co)

union_sql = ' UNION ALL '.join(union_parts)
print("SQL:", union_sql[:200])

try:
    rows = db.execute(union_sql, union_params).fetchall()
    print(f"\nRows returned: {len(rows)}")
    for r in rows:
        print(f"  type={r['type']} ref={r['ref']} owner={r['owner']} status={r['status']}")
    
    counts = Counter(r['type'] for r in rows)
    print(f"\nCounts: {dict(counts)}")
    print(f"pending_vacancy_count={counts.get('Vacancy', 0)}")
    print(f"pending_increment_count={counts.get('Increment', 0)}")
    print(f"pending_bonus_count={counts.get('Bonus', 0)}")
    print(f"pending_applications_count={counts.get('Application', 0)}")
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()
