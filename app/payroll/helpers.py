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


def increment_effective_period(inc, company_id):
    """Return the (month, year) when an approved increment takes effect.

    Uses the company increment policy effective_month/effective_year when
    configured, otherwise January of the year after the increment's
    period_year (year-end increments pay out the following January).
    """
    eff_month = 1
    eff_year = (inc['period_year'] or 0) + 1
    policy = query("""
        SELECT effective_month, effective_year
        FROM Increment_Policy WHERE company_id=?
    """, (company_id,), one=True)
    if policy:
        if policy['effective_month']:
            try:
                eff_month = int(policy['effective_month'])
            except (TypeError, ValueError):
                pass
        if policy['effective_year']:
            try:
                eff_year = int(policy['effective_year'])
            except (TypeError, ValueError):
                pass
    return eff_month, eff_year


def increment_landing_map(company_id, employees, today):
    """Map employee_id -> (landing_month, landing_year, increment_row).

    The landing month is where an approved increment actually takes effect:
    its effective period (policy) when that period has a Draft payroll,
    otherwise the most recent Draft payroll (the effective January has
    already passed), otherwise the current month. Computed BEFORE the sweep
    deletes any Draft rows so it stays stable across the sequential
    per-month regeneration.
    """
    landing = {}
    for emp in employees:
        eid = emp['employee_id']
        inc = query("""
            SELECT * FROM Salary_Increment
            WHERE employee_id=? AND status='Approved'
            ORDER BY period_year DESC, increment_id DESC
            LIMIT 1
        """, (eid,), one=True)
        if not inc:
            continue

        eff_m, eff_y = increment_effective_period(inc, company_id)
        eff_row = query("""
            SELECT status FROM Payroll
            WHERE employee_id=? AND pay_period_month=? AND pay_period_year=?
            LIMIT 1
        """, (eid, eff_m, eff_y), one=True)
        if eff_row and eff_row['status'] == 'Draft':
            landing[eid] = (eff_m, eff_y, inc)
            continue

        latest = query("""
            SELECT pay_period_month AS m, pay_period_year AS y FROM Payroll
            WHERE employee_id=? AND status='Draft'
              AND (pay_period_year < ? OR
                   (pay_period_year = ? AND pay_period_month <= ?))
            ORDER BY pay_period_year DESC, pay_period_month DESC
            LIMIT 1
        """, (eid, today.year, today.year, today.month), one=True)
        if latest:
            landing[eid] = (latest['m'], latest['y'], inc)
        else:
            landing[eid] = (today.month, today.year, inc)
    return landing


def apply_increment_to_payroll(employee_id, increment_amount, new_salary,
                                prefer_month=None, prefer_year=None,
                                user_id=None):
    """Apply a salary increment to the Draft payroll where it takes effect.

    Target period = the increment's effective period (policy). If that
    period's payroll is missing or not a Draft (e.g. January already
    finalised), the most recent Draft payroll is used instead.
    """
    emp = query("SELECT company_id FROM Employee WHERE employee_id=?",
                (employee_id,), one=True)
    if not emp:
        return False, 'Employee not found'

    inc = query("""
        SELECT * FROM Salary_Increment
        WHERE employee_id=? AND status='Approved'
        ORDER BY period_year DESC, increment_id DESC
        LIMIT 1
    """, (employee_id,), one=True)

    rec = None
    if inc:
        eff_m, eff_y = increment_effective_period(inc, emp['company_id'])
        rec = query("""
            SELECT * FROM Payroll
            WHERE employee_id=? AND pay_period_month=? AND pay_period_year=?
              AND status='Draft'
        """, (employee_id, eff_m, eff_y), one=True)

    if not rec:
        today = date.today()
        rec = query("""
            SELECT * FROM Payroll
            WHERE employee_id=? AND status='Draft'
              AND (pay_period_year < ? OR
                   (pay_period_year = ? AND pay_period_month <= ?))
            ORDER BY pay_period_year DESC, pay_period_month DESC
            LIMIT 1
        """, (employee_id, today.year, today.year, today.month), one=True)

    if not rec:
        # No draft up to the current month (e.g. it is already Finalised):
        # fall back to the most recent Draft of any period.
        rec = query("""
            SELECT * FROM Payroll
            WHERE employee_id=? AND status='Draft'
            ORDER BY pay_period_year DESC, pay_period_month DESC
            LIMIT 1
        """, (employee_id,), one=True)

    # Whether an existing Draft row was found (vs. creating one below).
    found_existing = rec is not None

    if not rec:
        rec, _m, _y = find_or_create_draft_payroll(
            employee_id, emp['company_id'], prefer_month, prefer_year,
            created_by=user_id)
        if not rec:
            return False, 'No available payroll period found'

    old_notes = rec['notes'] or ''
    note_line = f"Salary increment +RM {increment_amount:,.2f} approved on {date.today().isoformat()}"
    new_notes = (old_notes + f'; {note_line}') if old_notes else note_line
    execute("UPDATE Payroll SET notes=? WHERE payroll_id=?",
            (new_notes, rec['payroll_id']))

    # A freshly created payroll already carries the updated employee salary,
    # so only bump existing rows (pre-approval drafts) by the increment amount.
    if found_existing:
        execute("""
            UPDATE Payroll SET
                base_salary = base_salary + ?,
                salary_increment = COALESCE(salary_increment, 0) + ?,
                generated_at = datetime('now')
            WHERE payroll_id=?
        """, (increment_amount, increment_amount, rec['payroll_id']))

    _recalculate_payroll(rec['payroll_id'])
    m, y = rec['pay_period_month'], rec['pay_period_year']
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
