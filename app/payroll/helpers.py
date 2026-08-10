"""
Payroll helpers — shared functions for directly applying
transactions (increment, bonus, claims) to the next available Draft payroll.
"""
from datetime import date, datetime
from app.database import query, execute
from app.payroll.calculator import (calculate_proration, calculate_epf,
                                     calculate_socso, calculate_eis, calculate_pcb)


def _recalculate_payroll(pid):
    """Recalculate gross/net/deductions for a Draft payroll record."""
    p = query("SELECT * FROM Payroll WHERE payroll_id=?", (pid,), one=True)
    if not p or p['status'] != 'Draft':
        return

    gross = (p['base_salary'] + p['overtime_pay'] + p['commission'] +
             p['bonus'] + p['invoice_claims'] + p['leave_adjustment'])
    epf_e, epf_er = calculate_epf(gross)
    socso_e, socso_er = calculate_socso(gross)
    eis_e, eis_er = calculate_eis(gross)
    pcb = calculate_pcb(gross)
    total_ded = epf_e + socso_e + eis_e + pcb
    net = round(gross - total_ded, 2)

    execute("""
        UPDATE Payroll SET
            gross_pay=?, epf_employee=?, epf_employer=?,
            socso_employee=?, socso_employer=?,
            eis_employee=?, eis_employer=?, pcb_tax=?,
            total_deductions=?, net_pay=?
        WHERE payroll_id=?
    """, (gross, epf_e, epf_er, socso_e, socso_er,
          eis_e, eis_er, pcb, total_ded, net, pid))


def find_or_create_draft_payroll(employee_id, company_id,
                                  prefer_month=None, prefer_year=None,
                                  created_by=None):
    """
    Find the next available Draft payroll for an employee.
    Starts from prefer_month/prefer_year (default current month),
    looks forward month by month (up to 12 months).
    Skips Finalised/Paid months. Returns (record_dict, month, year) or (None,None,None).
    """
    today = date.today()
    sm = prefer_month if prefer_month is not None else today.month
    sy = prefer_year if prefer_year is not None else today.year

    for i in range(12):
        m = sm + i
        y = sy
        while m > 12:
            m -= 12
            y += 1

        existing = query("""
            SELECT * FROM Payroll
            WHERE employee_id=? AND pay_period_month=? AND pay_period_year=?
        """, (employee_id, m, y), one=True)

        if existing:
            if existing['status'] == 'Draft':
                return existing, m, y
            # Finalised/Paid — skip
            continue

        emp = query("""SELECT base_salary, hire_date FROM Employee
                        WHERE employee_id=?""", (employee_id,), one=True)
        if not emp:
            return None, None, None

        base = calculate_proration(emp['base_salary'], emp['hire_date'], m, y)
        gross = base
        epf_e, epf_er = calculate_epf(gross)
        socso_e, socso_er = calculate_socso(gross)
        eis_e, eis_er = calculate_eis(gross)
        pcb = calculate_pcb(gross)
        total_ded = epf_e + socso_e + eis_e + pcb
        net = round(gross - total_ded, 2)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        pid = execute("""
            INSERT INTO Payroll
                (employee_id, pay_period_month, pay_period_year, base_salary,
                 overtime_pay, commission, bonus, invoice_claims, leave_adjustment,
                 gross_pay, epf_employee, epf_employer, socso_employee, socso_employer,
                 eis_employee, eis_employer, pcb_tax, total_deductions, net_pay,
                 status, generated_by, generated_at, notes)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (employee_id, m, y, gross, 0, 0, 0, 0, 0,
              gross, epf_e, epf_er, socso_e, socso_er,
              eis_e, eis_er, pcb, total_ded, net,
              'Draft', created_by or employee_id, now_str, 'Auto-created'))

        new_rec = query("SELECT * FROM Payroll WHERE payroll_id=?", (pid,), one=True)
        return new_rec, m, y

    return None, None, None


def apply_bonus_to_payroll(employee_id, bonus_amount,
                            prefer_month=None, prefer_year=None,
                            user_id=None):
    """Add bonus amount to the next available Draft payroll for an employee."""
    emp = query("SELECT company_id FROM Employee WHERE employee_id=?",
                (employee_id,), one=True)
    if not emp:
        return False, 'Employee not found'

    rec, m, y = find_or_create_draft_payroll(
        employee_id, emp['company_id'], prefer_month, prefer_year,
        created_by=user_id)
    if not rec:
        return False, 'No available payroll period found'

    # Update bonus and add note
    old_notes = rec['notes'] or ''
    note_line = f"Bonus of RM {bonus_amount:,.2f} approved on {date.today().isoformat()}"
    new_notes = (old_notes + f'; {note_line}') if old_notes else note_line

    execute("""
        UPDATE Payroll SET bonus=bonus+?, notes=?, generated_at=datetime('now')
        WHERE payroll_id=?
    """, (bonus_amount, new_notes, rec['payroll_id']))
    _recalculate_payroll(rec['payroll_id'])
    return True, f'Applied bonus to {m}/{y}'


def apply_increment_to_payroll(employee_id, increment_amount, new_salary,
                                prefer_month=None, prefer_year=None,
                                user_id=None):
    """Update base_salary and record increment in the next Draft payroll."""
    emp = query("SELECT company_id FROM Employee WHERE employee_id=?",
                (employee_id,), one=True)
    if not emp:
        return False, 'Employee not found'

    rec, m, y = find_or_create_draft_payroll(
        employee_id, emp['company_id'], prefer_month, prefer_year,
        created_by=user_id)
    if not rec:
        return False, 'No available payroll period found'

    old_salary_in_payroll = rec['base_salary']
    old_increment = rec['salary_increment'] if rec['salary_increment'] else 0

    # Update base_salary and salary_increment in the payroll record
    # base_salary = effective salary including all increments
    execute("""
        UPDATE Payroll SET
            base_salary = base_salary + ?,
            salary_increment = COALESCE(salary_increment, 0) + ?,
            generated_at = datetime('now')
        WHERE payroll_id=?
    """, (increment_amount, increment_amount, rec['payroll_id']))

    old_notes = rec['notes'] or ''
    note_line = f"Salary increment +RM {increment_amount:,.2f} approved on {date.today().isoformat()}"
    new_notes = (old_notes + f'; {note_line}') if old_notes else note_line
    execute("UPDATE Payroll SET notes=? WHERE payroll_id=?",
            (new_notes, rec['payroll_id']))

    _recalculate_payroll(rec['payroll_id'])
    return True, f'Applied increment to {m}/{y}'


def apply_claim_to_payroll(employee_id, claim_amount, invoice_id,
                            prefer_month=None, prefer_year=None,
                            user_id=None):
    """Add invoice claim to the next available Draft payroll."""
    emp = query("SELECT company_id FROM Employee WHERE employee_id=?",
                (employee_id,), one=True)
    if not emp:
        return False, 'Employee not found'

    rec, m, y = find_or_create_draft_payroll(
        employee_id, emp['company_id'], prefer_month, prefer_year,
        created_by=user_id)
    if not rec:
        return False, 'No available payroll period found'

    execute("""
        UPDATE Payroll SET invoice_claims=invoice_claims+?, generated_at=datetime('now')
        WHERE payroll_id=?
    """, (claim_amount, rec['payroll_id']))

    old_notes = rec['notes'] or ''
    note_line = (f"Claim INV-{invoice_id} RM {claim_amount:,.2f} "
                 f"approved on {date.today().isoformat()}")
    new_notes = (old_notes + f'; {note_line}') if old_notes else note_line
    execute("UPDATE Payroll SET notes=? WHERE payroll_id=?",
            (new_notes, rec['payroll_id']))

    # Link invoice to payroll
    execute("""
        UPDATE Invoice SET payroll_id=?
        WHERE invoice_id=? AND payroll_id IS NULL
    """, (rec['payroll_id'], invoice_id))

    _recalculate_payroll(rec['payroll_id'])
    return True, f'Applied claim to {m}/{y}'
