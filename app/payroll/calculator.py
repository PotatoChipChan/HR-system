"""
app/payroll/calculator.py – Statutory Deduction Calculation Engine
Implements Malaysian EPF, SOCSO, EIS, and Employment Act compliant payroll rules.
"""
from datetime import datetime, date

def calculate_proration(base_salary, hire_date_str, month, year):
    """
    Prorates base salary based on the date of joining within the month.
    """
    try:
        hire_date = datetime.strptime(hire_date_str, '%Y-%m-%d').date()
    except:
        return float(base_salary)

    # Check if hired in the payroll month and year
    if hire_date.month == month and hire_date.year == year:
        # 30 days in month logic:
        # If hired on the 27th, they work 27, 28, 29, 30 (4 days)
        # Calculation: 30 - 27 + 1 = 4 days
        days_in_month = 30
        worked_days = max(1, days_in_month - hire_date.day + 1)
        return round(base_salary * (worked_days / days_in_month), 2)
    return float(base_salary)

def calculate_ot_or_leave(gross_salary, ot_hours):
    """
    Compliance logic based on Malaysian Employment Act:
    - Employees earning <= RM4,000 (threshold) are generally entitled to OT pay.
    - Employees > RM4,000 may be entitled to Replacement Leave instead of OT.
    """
    threshold = 4000.00
    if gross_salary <= threshold:
        # Standard OT rate: 1.5x (Normal workday)
        hourly_rate = (gross_salary / 160)
        return round(ot_hours * hourly_rate * 1.5, 2), "OT_PAY"
    else:
        # Exceeds threshold: Provide Time Off in Lieu (Replacement Leave)
        return 0.0, "REPLACEMENT_LEAVE"

def calculate_epf(gross_salary):
    # Standard 11% (employee) / 13% (employer)
    emp = round(gross_salary * 0.11, 2)
    er  = round(gross_salary * 0.13, 2)
    return emp, er

def calculate_socso(gross_salary):
    # Capped at RM 6,000
    capped = min(gross_salary, 6000)
    emp = round(capped * 0.005, 2)
    er  = round(capped * 0.0175, 2)
    return emp, er

def calculate_eis(gross_salary):
    capped = min(gross_salary, 6000)
    emp = round(capped * 0.002, 2)
    er  = round(capped * 0.002, 2)
    return emp, er

def calculate_pcb(gross_salary):
    if gross_salary <= 3000:
        return 0.0
    return round(max(0, (gross_salary - 3000) * 0.01), 2)
