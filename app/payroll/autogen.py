"""
app/payroll/autogen.py  –  Automatic payroll generation.

Refreshes Draft payroll records (attendance / bonus / increments / claims)
without requiring an active HTTP session. Runs on a background thread once
per day; idempotent and safe – Finalised/Paid payrolls are never touched.
"""
from datetime import date, datetime

from app.database import query, execute
from app.payroll.calculator import (calculate_proration, calculate_epf,
                                     calculate_socso, calculate_eis,
                                     calculate_pcb, calculate_ot_or_leave)
from app.payroll.helpers import increment_landing_map


def _months_to_refresh(company_id):
    """Return [(month, year), ...] to regenerate for a company.

    Always includes the current month, plus any month in the last 4 months
    that still has a Draft payroll (i.e. not yet finalised/paid).
    """
    today = date.today()
    pairs = {(today.month, today.year)}

    recent = query("""
        SELECT DISTINCT pay_period_month, pay_period_year
        FROM Payroll p
        JOIN Employee e ON p.employee_id=e.employee_id
        WHERE e.company_id=?
          AND p.status='Draft'
          AND (p.pay_period_year > ? OR
               (p.pay_period_year = ? AND p.pay_period_month >= ?))
    """, (company_id, today.year - 1, today.year, today.month - 3))
    for r in recent or []:
        pairs.add((r['pay_period_month'], r['pay_period_year']))
    return sorted(pairs, key=lambda x: (x[1], x[0]))


def generate_payroll_for_company(company_id, month, year, generated_by=None):
    """Regenerate Draft payrolls for one company for one month.

    Deletes existing Draft rows (unlinking their invoices first), then
    recreates them from current attendance, approved increments, approved
    bonuses and outstanding approved claims. Finalised/Paid records are
    preserved. Returns the number of payrolls created.
    """
    # Employees who already have Finalised or Paid payrolls for this month
    finalised = query("""
        SELECT p.employee_id FROM Payroll p
        JOIN Employee e ON p.employee_id=e.employee_id
        WHERE e.company_id=? AND p.pay_period_month=? AND p.pay_period_year=?
          AND p.status IN ('Finalised','Paid')
    """, (company_id, month, year))
    finalised_eids = {r['employee_id'] for r in finalised} if finalised else set()

    employees = query("SELECT * FROM Employee WHERE company_id=? AND is_active=1",
                      (company_id,))
    today = date.today()
    landing_map = increment_landing_map(company_id, employees, today)

    # Unlink invoices that pointed at Draft payrolls about to be deleted
    execute("""UPDATE Invoice SET payroll_id = NULL
               WHERE payroll_id IN (
                   SELECT payroll_id FROM Payroll
                   WHERE pay_period_month=? AND pay_period_year=?
                     AND status='Draft'
                     AND employee_id IN (SELECT employee_id FROM Employee WHERE company_id=?)
               )""", (month, year, company_id))

    # Delete existing Drafts so they are regenerated fresh
    execute("""DELETE FROM Payroll
               WHERE pay_period_month=? AND pay_period_year=? AND status='Draft'
                 AND employee_id IN (SELECT employee_id FROM Employee WHERE company_id=?)""",
            (month, year, company_id))

    count = 0
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    for emp in employees:
        eid = emp['employee_id']
        if eid in finalised_eids:
            continue

        # Approved increment: new salary applies from the landing month; the
        # increment amount line shows only on the landing month itself.
        land = landing_map.get(eid)
        if land:
            inc = land[2]
            if (year, month) == (land[1], land[0]):
                effective_salary = inc['new_salary']
                salary_inc_amt = inc['new_salary'] - inc['old_salary']
            elif (year, month) > (land[1], land[0]):
                effective_salary = inc['new_salary']
                salary_inc_amt = 0.0
            else:
                effective_salary = inc['old_salary']
                salary_inc_amt = 0.0
        else:
            effective_salary = emp['base_salary']
            salary_inc_amt = 0.0

        base = calculate_proration(effective_salary, emp['hire_date'], month, year)

        # Approved attendance overtime for the month
        att = query("""SELECT SUM(overtime_hours) as ot
                       FROM Attendance
                       WHERE employee_id=?
                         AND strftime('%m', check_in)=? AND strftime('%Y', check_in)=?
                         AND status='Approved'""",
                    (eid, f"{month:02d}", str(year)), one=True)
        ot_hours = att['ot'] if att and att['ot'] else 0

        # Approved invoice claims not yet linked to a payroll
        claims = query("""SELECT SUM(COALESCE(total_amount_myr, total_amount)) as total
                          FROM Invoice
                          WHERE employee_id=? AND status='Approved' AND payroll_id IS NULL""",
                       (eid,), one=True)
        invoice_claims = claims['total'] if claims and claims['total'] else 0.0

        ot_pay, ot_type = calculate_ot_or_leave(base, ot_hours)

        # Approved bonus for this payout month (from Bonus_Policy year + payout_month)
        bonus_prop = query("""
            SELECT bp.bonus_amount FROM Bonus_Proposal bp
            JOIN Bonus_Policy pol ON pol.company_id=? AND pol.year=bp.period_year
            WHERE bp.employee_id=? AND bp.status='Approved'
              AND pol.payout_month=?
            ORDER BY bp.period_year DESC
            LIMIT 1
        """, (company_id, eid, month), one=True)
        bonus = round(bonus_prop['bonus_amount'], 2) if bonus_prop else 0.0

        # Unused leave conversion (RM 200/day)
        leave_days = query(
            "SELECT SUM(pending_days) as pending FROM Leave_Balance WHERE employee_id=? AND year=?",
            (eid, year), one=True)
        pending_leave = leave_days['pending'] if leave_days and leave_days['pending'] else 0.0
        leave_adjustment = round(pending_leave * 200, 2)

        gross = base + ot_pay + invoice_claims + bonus

        epf_e, epf_er     = calculate_epf(gross)
        socso_e, socso_er = calculate_socso(gross)
        eis_e, eis_er     = calculate_eis(gross)
        pcb               = calculate_pcb(gross)
        total_ded = epf_e + socso_e + eis_e + pcb
        net = round(gross - total_ded, 2)

        note = f"OT calculated as {ot_type}"
        if ot_type == "REPLACEMENT_LEAVE":
            note = f"OT of {ot_hours} hrs converted to Replacement Leave"
        if salary_inc_amt > 0:
            note += f"; Salary increment: RM {salary_inc_amt:,.2f}"
        if bonus > 0:
            note += f"; Bonus: {bonus}"
        if leave_adjustment > 0:
            note += f"; Leave adjustment: {leave_adjustment}"

        pid = execute("""
            INSERT INTO Payroll
            (employee_id, pay_period_month, pay_period_year, base_salary, overtime_pay,
             commission, bonus, invoice_claims, leave_adjustment, gross_pay,
             epf_employee, epf_employer, socso_employee, socso_employer,
             eis_employee, eis_employer, pcb_tax, total_deductions, net_pay,
             status, generated_by, generated_at, notes, salary_increment)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (eid, month, year, base, ot_pay, 0, bonus, invoice_claims, leave_adjustment, gross,
              epf_e, epf_er, socso_e, socso_er, eis_e, eis_er, pcb, total_ded, net,
              'Draft', generated_by, now_str, note, salary_inc_amt))

        if invoice_claims > 0:
            execute("""UPDATE Invoice SET payroll_id=?
                       WHERE employee_id=? AND status='Approved' AND payroll_id IS NULL""",
                    (pid, eid))
        count += 1

    return count


def auto_generate_payroll(generated_by=None):
    """Regenerate Draft payrolls for every company.

    Called from the background scheduler. Runs inside an app context.
    Returns a list of result strings for logging.
    """
    companies = query("SELECT company_id FROM Company ORDER BY company_id")
    if not companies:
        return ['No companies found']

    results = []
    for comp in companies:
        cid = comp['company_id']
        for month, year in _months_to_refresh(cid):
            try:
                cnt = generate_payroll_for_company(cid, month, year,
                                                   generated_by=generated_by)
                results.append(f"Company {cid}: {month}/{year} -> {cnt} payrolls")
            except Exception as e:
                results.append(f"Company {cid}: {month}/{year} -> ERROR: {e}")
    return results


# =============================================================================
# Background scheduler thread
# =============================================================================

import os
import threading
import time

_INTERVAL_SECONDS = 24 * 60 * 60   # daily
_INITIAL_DELAY_SECONDS = 30        # let the server finish booting first


def _scheduler_loop(app, interval, initial_delay):
    time.sleep(initial_delay)
    while True:
        try:
            with app.app_context():
                results = auto_generate_payroll()
                print('[PayrollScheduler] Auto payroll run:')
                for r in results:
                    print('  -', r)
        except Exception as e:
            print(f'[PayrollScheduler] Error during auto payroll: {e}')
        time.sleep(interval)


def _should_start(app):
    """Only start the scheduler in the real server process.

    When the Flask debug reloader is active, two processes are spawned;
    the reloader child (which actually serves requests) sets
    WERKZEUG_RUN_MAIN=true, so we start only there to avoid duplicates.
    """
    if app.debug:
        return os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    return True


def start_payroll_scheduler(app, interval=_INTERVAL_SECONDS,
                            initial_delay=_INITIAL_DELAY_SECONDS):
    """Start the daily auto-payroll background thread (idempotent)."""
    if not _should_start(app):
        return
    # Guard against double registration
    if getattr(app, '_payroll_scheduler_started', False):
        return
    app._payroll_scheduler_started = True
    t = threading.Thread(target=_scheduler_loop, args=(app, interval, initial_delay),
                         daemon=True, name='payroll-autogen')
    t.start()
    print(f'[PayrollScheduler] Auto payroll generation started '
          f'(every {interval // 3600}h, first run in {initial_delay}s).')
