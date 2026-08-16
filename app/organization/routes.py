"""app/organization/routes.py – Company / Branch / Department / Role management"""
import os
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, send_from_directory, current_app
)
from werkzeug.utils import secure_filename
from app.database import query, execute, log_audit
from app.auth.routes import login_required, role_required

org_bp = Blueprint('organization', __name__, url_prefix='/organization')

# ----------------------------------------------------------------------
# Helper to get company_id from session (used for HR views)
# ----------------------------------------------------------------------
def _get_company_id():
    return session.get('company_id')


_WORKFLOW_RETURN_PATHS = {
    '/recruitment/postings/add',
    '/organization/roles/positions/add',
}


def _workflow_return_url(raw_url, **prefill):
    """Return an internal, approved setup destination with new selections.

    Organisation setup forms can call one another (for example, a posting
    needs a new department, then a catalog position).  Only these two known
    destinations are accepted, so form-controlled query parameters cannot
    create an open redirect.
    """
    if not raw_url:
        return None
    parsed = urlsplit(raw_url)
    if parsed.scheme or parsed.netloc or parsed.path not in _WORKFLOW_RETURN_PATHS:
        return None
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params.update({key: str(value) for key, value in prefill.items()
                   if value not in (None, '')})
    return urlunsplit(('', '', parsed.path, urlencode(params), ''))

def _get_dept_manager_candidates(company_id):
    """Active employees eligible to be assigned as a department manager.

    Any active employee of the company may hold department-manager
    responsibility (least-privilege: the system role stays whatever it is —
    department-manager authority comes from Department.department_manager_id).
    """
    return query("""
        SELECT e.employee_id, e.full_name, e.branch_id, b.name AS branch_name,
               r.role_name,
               COALESCE(p.position_name, e.position) AS position_name
        FROM Employee e
        JOIN Role r ON e.role_id = r.role_id
        JOIN Branch b ON e.branch_id = b.branch_id
        LEFT JOIN Position p ON e.position_id = p.position_id
        WHERE e.is_active = 1 AND b.company_id = ?
        ORDER BY b.name, e.full_name
    """, (company_id,))

# ----------------------------------------------------------------------
# Redirect old index to company list
# ----------------------------------------------------------------------
@org_bp.route('/')
@login_required
def index():
    return redirect(url_for('organization.companies'))

# ======================================================================
# COMPANIES
# ======================================================================
@org_bp.route('/companies')
@login_required
@role_required('Admin', 'HR')
def companies():
    """List companies with branch and employee counts."""
    # Admin and HR Manager see all companies; HR sees only their own company.
    if session.get('user_role') in ('Admin', 'HR Manager'):
        rows = query("""
            SELECT
                c.*,
                COUNT(DISTINCT b.branch_id) AS branch_count,
                COUNT(DISTINCT e.employee_id) AS employee_count
            FROM Company c
            LEFT JOIN Branch b ON b.company_id = c.company_id
            LEFT JOIN Employee e ON e.company_id = c.company_id
                AND e.employment_status != 'Terminated'
            GROUP BY c.company_id
            ORDER BY c.name
        """)
    else:
        co = _get_company_id()
        rows = query("""
            SELECT
                c.*,
                COUNT(DISTINCT b.branch_id) AS branch_count,
                COUNT(DISTINCT e.employee_id) AS employee_count
            FROM Company c
            LEFT JOIN Branch b ON b.company_id = c.company_id
            LEFT JOIN Employee e ON e.company_id = c.company_id
                AND e.employment_status != 'Terminated'
            WHERE c.company_id = ?
            GROUP BY c.company_id
        """, (co,))
    companies = [dict(row) for row in rows]
    return render_template('organization/company_list.html', companies=companies)


@org_bp.route('/company/<int:cid>/edit', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'HR')
def edit_company(cid):
    # HR can only edit their own company; Admin can edit any
    if session.get('user_role') == 'HR' and cid != _get_company_id():
        flash('You are not authorised to edit this company.', 'danger')
        return redirect(url_for('organization.companies'))

    company = query("SELECT * FROM Company WHERE company_id=?", (cid,), one=True)
    if not company:
        flash('Company not found.', 'danger')
        return redirect(url_for('organization.companies'))

    if request.method == 'POST':
        f = request.form
        name = f.get('name', '').strip()
        if not name:
            flash('Company name is required.', 'danger')
            return render_template('organization/company_form.html', company=company)
        if len(name) < 2 or len(name) > 150:
            flash('Company name must be 2-150 characters.', 'danger')
            return render_template('organization/company_form.html', company=company)
            
        address = f.get('address', '').strip()
        contact = f.get('contact_no', '').strip()
        email = f.get('email', '').strip()
        
        # Validate email format if provided
        if email and '@' not in email:
            flash('Please enter a valid email address.', 'danger')
            return render_template('organization/company_form.html', company=company)

        # Ensure signature_path column exists
        try:
            execute("ALTER TABLE Company ADD COLUMN signature_path TEXT")
        except Exception:
            pass

        # Handle signature upload (Admin/HR Manager only)
        signature_updated = False
        if session.get('user_role') in ('Admin', 'HR Manager'):
            file = request.files.get('signature')
            if file and file.filename and file.filename.strip():
                if not file.filename.lower().endswith('.png'):
                    flash('Company signature must be a PNG file.', 'danger')
                    return render_template('organization/company_form.html', company=company)
                sig_dir = os.path.join(current_app.root_path, '..', 'uploads', 'signatures')
                os.makedirs(sig_dir, exist_ok=True)
                sig_filename = f"company_{cid}_signature.png"
                sig_path = os.path.join(sig_dir, sig_filename)
                file.save(sig_path)
                execute("UPDATE Company SET signature_path=? WHERE company_id=?", (sig_filename, cid))
                signature_updated = True

        execute(
            "UPDATE Company SET name=?, address=?, contact_no=?, email=? WHERE company_id=?",
            (name, address, contact, email, cid)
        )
        log_audit('UPDATE', 'Organization', f'Updated company id={cid}', 'Company', cid)
        flash('Company updated.' + (' Signature saved.' if signature_updated else ''), 'success')
        return redirect(url_for('organization.companies'))

    return render_template('organization/company_form.html', company=company)


@org_bp.route('/company/<int:cid>/signature')
@login_required
def company_signature(cid):
    company = query("SELECT signature_path FROM Company WHERE company_id=?", (cid,), one=True)
    if not company or not company['signature_path']:
        return '', 404
    sig_dir = os.path.join(current_app.root_path, '..', 'uploads', 'signatures')
    return send_from_directory(sig_dir, company['signature_path'])


@org_bp.route('/company/<int:cid>/delete', methods=['POST'])
@login_required
@role_required('Admin')   # Only Admin can delete companies
def delete_company(cid):
    # Check for dependent branches
    branches = query("SELECT COUNT(*) as count FROM Branch WHERE company_id=?", (cid,), one=True)
    if branches['count'] > 0:
        flash('Cannot delete company: it has branches. Delete branches first.', 'danger')
        return redirect(url_for('organization.companies'))
    # Check for employees (even if no branches, but employees are linked to branches)
    employees = query("SELECT COUNT(*) as count FROM Employee WHERE company_id=?", (cid,), one=True)
    if employees['count'] > 0:
        flash('Cannot delete company: it has employees. Move them first.', 'danger')
        return redirect(url_for('organization.companies'))

    execute("DELETE FROM Company WHERE company_id=?", (cid,))
    log_audit('DELETE', 'Organization', f'Deleted company id={cid}', 'Company', cid)
    flash('Company deleted.', 'warning')
    return redirect(url_for('organization.companies'))


# ======================================================================
# BRANCHES
# ======================================================================
@org_bp.route('/branches')
@login_required
@role_required('Admin', 'HR')
def branches():
    """List branches, optionally filtered by company (Admin only)."""
    company_id = request.args.get('company_id', type=int)

    # If not Admin/HR Manager, force to own company.
    if session.get('user_role') not in ('Admin', 'HR Manager'):
        company_id = _get_company_id()

    if company_id:
        rows = query("""
            SELECT
                b.*,
                c.name AS company_name,
                e.full_name AS manager_name
            FROM Branch b
            JOIN Company c ON c.company_id = b.company_id
            LEFT JOIN Employee e ON e.employee_id = b.hr_manager_id
            WHERE b.company_id = ?
            ORDER BY b.name
        """, (company_id,))
    else:
        rows = query("""
            SELECT
                b.*,
                c.name AS company_name,
                e.full_name AS manager_name
            FROM Branch b
            JOIN Company c ON c.company_id = b.company_id
            LEFT JOIN Employee e ON e.employee_id = b.hr_manager_id
            ORDER BY c.name, b.name
        """)

    branches = [dict(row) for row in rows]

    # For Admin filter dropdown: all companies
    companies = []
    if session.get('user_role') in ('Admin', 'HR Manager'):
        companies = query("SELECT company_id, name FROM Company ORDER BY name")

    return render_template('organization/branch_list.html',
                           branches=branches,
                           companies=companies,
                           current_company=company_id)


@org_bp.route('/branch/add', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'HR')
def add_branch():
    if request.method == 'POST':
        f = request.form
        name = f.get('name', '').strip()
        address_line1 = f.get('address_line1', '').strip()
        address_line2 = f.get('address_line2', '').strip()
        city = f.get('city', '').strip()
        state = f.get('state', '').strip()
        postal_code = f.get('postal_code', '').strip()
        
        # Validate required fields
        if not name:
            flash('Branch Name is required.', 'danger')
            return redirect(url_for('organization.add_branch'))
        if not address_line1:
            flash('Address Line 1 is required.', 'danger')
            return redirect(url_for('organization.add_branch'))
        if not city:
            flash('City/Region is required.', 'danger')
            return redirect(url_for('organization.add_branch'))
        if not state:
            flash('State is required.', 'danger')
            return redirect(url_for('organization.add_branch'))
        if not postal_code:
            flash('Postal Code is required.', 'danger')
            return redirect(url_for('organization.add_branch'))
        
        # Validate postal code format (5 digits)
        if not postal_code.isdigit() or len(postal_code) != 5:
            flash('Postal Code must be 5 digits.', 'danger')
            return redirect(url_for('organization.add_branch'))
        
        # Generate combined address for backward compatibility
        address_parts = [address_line1]
        if address_line2:
            address_parts.append(address_line2)
        address_parts.extend([postal_code, city, state])
        combined_address = ', '.join(address_parts)
        
        company_id = f.get('company_id')
        # HR can only add to their own company
        if session.get('user_role') == 'HR':
            company_id = _get_company_id()
        else:
            if not company_id:
                flash('Company selection is required.', 'danger')
                return redirect(url_for('organization.add_branch'))
        
        contact = f.get('contact_no', '').strip()
        hr_manager = f.get('hr_manager_id') or None
        parent = f.get('parent_branch_id') or None
        
        bid = execute(
            "INSERT INTO Branch (company_id, name, address, address_line1, address_line2, city, state, postal_code, contact_no, hr_manager_id, parent_branch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (company_id, name, combined_address, address_line1, address_line2, city, state, postal_code, contact, hr_manager, parent)
        )
        log_audit('CREATE', 'Organization', f'Added branch "{name}"', 'Branch', bid)
        flash(f'Branch "{name}" added successfully.', 'success')
        return_to = _workflow_return_url(f.get('return_to'), branch_id=bid)
        if return_to:
            return redirect(return_to)
        if f.get('continue_setup') == 'department':
            return redirect(url_for('organization.add_department', branch_id=bid,
                                    continue_setup='1'))
        return redirect(url_for('organization.branches'))

    # GET: populate dropdowns
    if session.get('user_role') in ('Admin', 'HR Manager'):
        companies = query("SELECT company_id, name FROM Company ORDER BY name")
    else:
        co = _get_company_id()
        companies = query("SELECT company_id, name FROM Company WHERE company_id=?", (co,))

    employees = query("SELECT employee_id, full_name FROM Employee WHERE is_active=1 ORDER BY full_name")
    parent_branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? ORDER BY name", (_get_company_id(),))
    return render_template('organization/branch_form.html',
                           branch=None,
                           companies=companies,
                           employees=employees,
                           parent_branches=parent_branches,
                           return_to=_workflow_return_url(request.args.get('return_to', '')) or '')


@org_bp.route('/branch/<int:bid>/edit', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'HR')
def edit_branch(bid):
    branch = query("SELECT * FROM Branch WHERE branch_id=?", (bid,), one=True)
    if not branch:
        flash('Branch not found.', 'danger')
        return redirect(url_for('organization.branches'))
    # HR can only edit branches in their own company
    if session.get('user_role') == 'HR' and branch['company_id'] != _get_company_id():
        flash('You are not authorised to edit this branch.', 'danger')
        return redirect(url_for('organization.branches'))

    if request.method == 'POST':
        f = request.form
        name = f.get('name', '').strip()
        address_line1 = f.get('address_line1', '').strip()
        address_line2 = f.get('address_line2', '').strip()
        city = f.get('city', '').strip()
        state = f.get('state', '').strip()
        postal_code = f.get('postal_code', '').strip()
        
        # Validate required fields
        if not name or not address_line1 or not city or not state or not postal_code:
            flash('Branch Name, Address Line 1, City, State and Postal Code are required.', 'danger')
            companies = query("SELECT company_id, name FROM Company WHERE company_id=?", (branch['company_id'],))
            employees = query("SELECT employee_id, full_name FROM Employee WHERE is_active=1 ORDER BY full_name")
            parent_branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? AND branch_id!=? ORDER BY name",
                                    (branch['company_id'], bid))
            return render_template('organization/branch_form.html',
                                   branch=branch,
                                   companies=companies,
                                   employees=employees,
                                   parent_branches=parent_branches)
        
        # Validate postal code format (5 digits)
        if not postal_code.isdigit() or len(postal_code) != 5:
            flash('Postal Code must be 5 digits.', 'danger')
            companies = query("SELECT company_id, name FROM Company WHERE company_id=?", (branch['company_id'],))
            employees = query("SELECT employee_id, full_name FROM Employee WHERE is_active=1 ORDER BY full_name")
            parent_branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? AND branch_id!=? ORDER BY name",
                                    (branch['company_id'], bid))
            return render_template('organization/branch_form.html',
                                   branch=branch,
                                   companies=companies,
                                   employees=employees,
                                   parent_branches=parent_branches)
        
        # Generate combined address for backward compatibility
        address_parts = [address_line1]
        if address_line2:
            address_parts.append(address_line2)
        address_parts.extend([postal_code, city, state])
        combined_address = ', '.join(address_parts)
        
        contact = f.get('contact_no', '').strip()
        hr_manager = f.get('hr_manager_id') or None
        parent = f.get('parent_branch_id') or None
        execute(
            "UPDATE Branch SET name=?, address=?, address_line1=?, address_line2=?, city=?, state=?, postal_code=?, contact_no=?, hr_manager_id=?, parent_branch_id=? WHERE branch_id=?",
            (name, combined_address, address_line1, address_line2, city, state, postal_code, contact, hr_manager, parent, bid)
        )
        log_audit('UPDATE', 'Organization', f'Updated branch id={bid}', 'Branch', bid)
        flash('Branch updated successfully.', 'success')
        return redirect(url_for('organization.branches'))

    # GET: populate dropdowns
    companies = query("SELECT company_id, name FROM Company WHERE company_id=?", (branch['company_id'],))
    employees = query("SELECT employee_id, full_name FROM Employee WHERE is_active=1 ORDER BY full_name")
    parent_branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? AND branch_id!=? ORDER BY name",
                            (branch['company_id'], bid))
    return render_template('organization/branch_form.html',
                           branch=branch,
                           companies=companies,
                           employees=employees,
                           parent_branches=parent_branches)


@org_bp.route('/branch/<int:bid>/delete', methods=['POST'])
@login_required
@role_required('Admin', 'HR')
def delete_branch(bid):
    # Get branch info
    branch = query("SELECT * FROM Branch WHERE branch_id=?", (bid,), one=True)
    if not branch:
        flash('Branch not found.', 'danger')
        return redirect(url_for('organization.branches'))
    
    # HR can only delete branches in their own company
    if session.get('user_role') == 'HR' and branch['company_id'] != _get_company_id():
        flash('You are not authorised to delete this branch.', 'danger')
        return redirect(url_for('organization.branches'))
    
    # Check for sub-branches
    sub = query("SELECT COUNT(*) as count FROM Branch WHERE parent_branch_id=?", (bid,), one=True)
    if sub['count'] > 0:
        flash("Cannot delete branch: It has sub-branches.", "danger")
        return redirect(url_for('organization.branches'))
    # Check for departments
    depts = query("SELECT COUNT(*) as count FROM Department WHERE branch_id=?", (bid,), one=True)
    if depts['count'] > 0:
        flash("Cannot delete branch: It has associated departments.", "danger")
        return redirect(url_for('organization.branches'))
    # Check for active employees
    emps = query("SELECT COUNT(*) as count FROM Employee WHERE branch_id=? AND is_active=1", (bid,), one=True)
    if emps['count'] > 0:
        flash("Cannot delete branch: It has active employees.", "danger")
        return redirect(url_for('organization.branches'))
    execute("DELETE FROM Branch WHERE branch_id=?", (bid,))
    log_audit('DELETE', 'Organization', f'Deleted branch id={bid}', 'Branch', bid)
    flash("Branch deleted.", "warning")
    return redirect(url_for('organization.branches'))


# ======================================================================
# DEPARTMENTS
# ======================================================================
@org_bp.route('/departments')
@login_required
@role_required('Admin', 'HR')
def departments():
    """List departments, optionally filtered by branch."""
    branch_id = request.args.get('branch_id', type=int)

    # If no filter, show all departments under the user's company (for HR)
    if session.get('user_role') == 'HR' and not branch_id:
        # Get all branches for the HR's company
        co = _get_company_id()
        rows = query("""
            SELECT
                d.*,
                b.name AS branch_name,
                m.full_name AS manager_name,
                COUNT(e.employee_id) AS emp_count
            FROM Department d
            JOIN Branch b ON b.branch_id = d.branch_id
            LEFT JOIN Employee m ON m.employee_id = d.department_manager_id
            LEFT JOIN Employee e ON e.department_id = d.department_id
                AND e.employment_status != 'Terminated'
            WHERE b.company_id = ?
            GROUP BY d.department_id
            ORDER BY b.name, d.department_name
        """, (co,))
    else:
        if branch_id:
            rows = query("""
                SELECT
                    d.*,
                    b.name AS branch_name,
                    m.full_name AS manager_name,
                    COUNT(e.employee_id) AS emp_count
                FROM Department d
                JOIN Branch b ON b.branch_id = d.branch_id
                LEFT JOIN Employee m ON m.employee_id = d.department_manager_id
                LEFT JOIN Employee e ON e.department_id = d.department_id
                    AND e.employment_status != 'Terminated'
                WHERE d.branch_id = ?
                GROUP BY d.department_id
                ORDER BY d.department_name
            """, (branch_id,))
        else:
            # Admin viewing all departments without filter
            rows = query("""
                SELECT
                    d.*,
                    b.name AS branch_name,
                    m.full_name AS manager_name,
                    COUNT(e.employee_id) AS emp_count
                FROM Department d
                JOIN Branch b ON b.branch_id = d.branch_id
                LEFT JOIN Employee m ON m.employee_id = d.department_manager_id
                LEFT JOIN Employee e ON e.department_id = d.department_id
                    AND e.employment_status != 'Terminated'
                GROUP BY d.department_id
                ORDER BY b.name, d.department_name
            """)

    departments = [dict(row) for row in rows]

    # For filter dropdown: Admin/HR Manager see all, HR sees only own company's branches.
    if session.get('user_role') in ('Admin', 'HR Manager'):
        branches = query("SELECT branch_id, name FROM Branch ORDER BY name")
    else:
        co = _get_company_id()
        branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? ORDER BY name", (co,))

    return render_template('organization/department_list.html',
                           departments=departments,
                           branches=branches,
                           current_branch=branch_id)


@org_bp.route('/department/add', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'HR')
def add_department():
    if request.method == 'POST':
        f = request.form
        branch_id = f.get('branch_id')
        dept_name = f.get('department_name', '').strip()
        return_to = f.get('return_to', '')
        continue_setup = f.get('continue_setup') == '1'
        back = url_for('organization.add_department', branch_id=branch_id or '',
                       return_to=return_to or None,
                       continue_setup='1' if continue_setup else None)
        if not branch_id or not dept_name:
            flash('Branch and Department Name are required.', 'danger')
            return redirect(back)
        if len(dept_name) < 2 or len(dept_name) > 100:
            flash('Department name must be 2-100 characters.', 'danger')
            return redirect(back)
            
        # Check that the branch belongs to the HR's company (or admin)
        branch = query("SELECT company_id FROM Branch WHERE branch_id=?", (branch_id,), one=True)
        if not branch:
            flash('Invalid branch.', 'danger')
            return redirect(back)
        if session.get('user_role') == 'HR' and branch['company_id'] != _get_company_id():
            flash('You cannot add a department to a branch outside your company.', 'danger')
            return redirect(back)

        dept_mgr = f.get('department_manager_id')
        dept_mgr = int(dept_mgr) if dept_mgr else None
        if dept_mgr:
            mgr = query("""SELECT e.branch_id, b.company_id
                           FROM Employee e JOIN Branch b ON e.branch_id=b.branch_id
                           WHERE e.employee_id=? AND e.is_active=1""",
                        (dept_mgr,), one=True)
            if not mgr or mgr['branch_id'] != int(branch_id) or mgr['company_id'] != branch['company_id']:
                flash('The selected department manager must be an active employee of this branch in the same company.', 'danger')
                return redirect(back)
        did = execute("INSERT INTO Department (branch_id, department_name, department_manager_id) VALUES (?,?,?)",
                      (branch_id, dept_name, dept_mgr))
        log_audit('CREATE', 'Organization', f'Added department "{dept_name}"', 'Department', did)
        flash(f'Department "{dept_name}" added.', 'success')
        workflow_return = _workflow_return_url(return_to, branch_id=branch_id,
                                                department_id=did)
        if workflow_return:
            return redirect(workflow_return)
        if continue_setup:
            return redirect(url_for('organization.add_position', branch_id=branch_id,
                                    department_id=did, continue_setup='1'))
        return redirect(url_for('organization.departments'))

    # GET: list branches and managers for the company
    co = _get_company_id()
    branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? ORDER BY name", (co,))
    managers = _get_dept_manager_candidates(co)
    return render_template('organization/department_form.html', department=None, branches=branches, managers=managers,
                           prefill_branch=request.args.get('branch_id', ''),
                           return_to=_workflow_return_url(request.args.get('return_to', '')) or '',
                           continue_setup=request.args.get('continue_setup') == '1')


@org_bp.route('/department/<int:did>/edit', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'HR')
def edit_department(did):
    dept = query("""
        SELECT d.*, b.company_id
        FROM Department d
        JOIN Branch b ON d.branch_id = b.branch_id
        WHERE d.department_id = ?
    """, (did,), one=True)
    if not dept:
        flash('Department not found.', 'danger')
        return redirect(url_for('organization.departments'))
    # HR can only edit departments in their company
    if session.get('user_role') == 'HR' and dept['company_id'] != _get_company_id():
        flash('You are not authorised to edit this department.', 'danger')
        return redirect(url_for('organization.departments'))

    if request.method == 'POST':
        f = request.form
        new_name = f.get('department_name', '').strip()
        if not new_name:
            flash('Department name is required.', 'danger')
            branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? ORDER BY name", (dept['company_id'],))
            managers = _get_dept_manager_candidates(dept['company_id'])
            return render_template('organization/department_form.html', department=dept, branches=branches, managers=managers)
        if len(new_name) < 2 or len(new_name) > 100:
            flash('Department name must be 2-100 characters.', 'danger')
            branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? ORDER BY name", (dept['company_id'],))
            managers = _get_dept_manager_candidates(dept['company_id'])
            return render_template('organization/department_form.html', department=dept, branches=branches, managers=managers)
        dept_mgr = f.get('department_manager_id')
        dept_mgr = int(dept_mgr) if dept_mgr else None
        if dept_mgr:
            mgr = query("""SELECT e.branch_id, b.company_id
                           FROM Employee e JOIN Branch b ON e.branch_id=b.branch_id
                           WHERE e.employee_id=? AND e.is_active=1""",
                        (dept_mgr,), one=True)
            if not mgr or mgr['branch_id'] != dept['branch_id'] or mgr['company_id'] != dept['company_id']:
                flash('The selected department manager must be an active employee of this branch in the same company.', 'danger')
                branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? ORDER BY name", (dept['company_id'],))
                managers = _get_dept_manager_candidates(dept['company_id'])
                return render_template('organization/department_form.html', department=dept, branches=branches, managers=managers)
        execute("UPDATE Department SET department_name=?, department_manager_id=? WHERE department_id=?", (new_name, dept_mgr, did))
        log_audit('UPDATE', 'Organization', f'Updated department id={did}', 'Department', did)
        flash('Department updated.', 'success')
        return redirect(url_for('organization.departments'))

    # GET: branches and managers for the same company
    branches = query("SELECT branch_id, name FROM Branch WHERE company_id=? ORDER BY name", (dept['company_id'],))
    managers = _get_dept_manager_candidates(dept['company_id'])
    return render_template('organization/department_form.html', department=dept, branches=branches, managers=managers)


@org_bp.route('/department/<int:did>/delete', methods=['POST'])
@login_required
@role_required('Admin')
def delete_department(did):
    # Check for active employees
    emps = query("SELECT COUNT(*) as count FROM Employee WHERE department_id=? AND is_active=1", (did,), one=True)
    if emps['count'] > 0:
        flash("Cannot delete department: It has active employees.", "danger")
        return redirect(url_for('organization.departments'))
    execute("DELETE FROM Department WHERE department_id=?", (did,))
    log_audit('DELETE', 'Organization', f'Deleted department id={did}', 'Department', did)
    flash("Department deleted.", "warning")
    return redirect(url_for('organization.departments'))


# ======================================================================
# ROLES + POSITION CATALOG
# ======================================================================
@org_bp.route('/roles')
@login_required
@role_required('Admin', 'HR', 'HR Manager')
def roles():
    """List roles with employee counts, dept manager assignments and the
    position catalog (predefined job titles per department)."""
    rows = query("""
        SELECT r.role_id, r.role_name,
               COUNT(e.employee_id) as employee_count
        FROM Role r
        LEFT JOIN Employee e ON r.role_id = e.role_id AND e.is_active=1
        WHERE r.role_name != 'HR Director'
        GROUP BY r.role_id
        ORDER BY r.role_id
    """)
    roles = [dict(row) for row in rows]

    dept_managers = query("""
        SELECT d.department_id, d.department_name, b.name AS branch_name,
               e.employee_id, e.full_name AS manager_name,
               COALESCE(p.position_name, e.position) AS position_name,
               r.role_name
        FROM Department d
        JOIN Branch b ON d.branch_id = b.branch_id
        LEFT JOIN Employee e ON d.department_manager_id = e.employee_id
        LEFT JOIN Role r ON e.role_id = r.role_id
        LEFT JOIN Position p ON e.position_id = p.position_id
        ORDER BY b.name, d.department_name
    """)

    empty_departments = query("""
        SELECT d.department_id, d.department_name, b.name AS branch_name
        FROM Department d
        JOIN Branch b ON d.branch_id = b.branch_id
        LEFT JOIN Position p ON p.department_id = d.department_id AND p.is_active = 1
        GROUP BY d.department_id
        HAVING COUNT(p.position_id) = 0
        ORDER BY b.name, d.department_name
    """)

    positions = query("""
        SELECT p.*, d.department_name, b.branch_id AS branch_id, b.name AS branch_name,
               (SELECT COUNT(*) FROM Employee e WHERE e.position_id=p.position_id) AS emp_count,
               (SELECT COUNT(*) FROM Job_Posting jp WHERE jp.position_id=p.position_id) AS posting_count
        FROM Position p
        JOIN Department d ON p.department_id=d.department_id
        JOIN Branch b ON d.branch_id=b.branch_id
        ORDER BY b.name, d.department_name, LOWER(p.position_name)
    """)
    departments = query("""
        SELECT d.*, b.name AS branch_name
        FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
        ORDER BY d.department_name
    """)
    position_branches = query("SELECT branch_id, name FROM Branch ORDER BY name")

    return render_template('organization/role_list.html',
                           roles=roles, dept_managers=dept_managers,
                           positions=positions, departments=departments,
                           position_branches=position_branches,
                           empty_departments=empty_departments)


def _normalize_position_name(name):
    """Trim + collapse inner whitespace."""
    return ' '.join(name.split())


@org_bp.route('/roles/positions/add', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'HR', 'HR Manager')
def add_position():
    if request.method == 'GET':
        branches = query("SELECT branch_id, name FROM Branch ORDER BY name")
        departments = query("""
            SELECT d.*, b.name AS branch_name
            FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
            ORDER BY b.name, d.department_name
        """)
        return render_template('organization/add_position.html',
                               branches=branches, departments=departments,
                               prefill_branch=request.args.get('branch_id', ''),
                               prefill_department=request.args.get('department_id', ''),
                               prefill_name=request.args.get('position_name', ''),
                               return_to=_workflow_return_url(request.args.get('return_to', '')) or '',
                               continue_setup=request.args.get('continue_setup') == '1')

    name = _normalize_position_name(request.form.get('position_name', ''))
    dept_id = request.form.get('department_id')
    back = url_for('organization.add_position',
                   position_name=name,
                   department_id=dept_id or '',
                   branch_id=request.form.get('branch_id', ''),
                   return_to=request.form.get('return_to', '') or None,
                   continue_setup='1' if request.form.get('continue_setup') == '1' else None)
    if not name or not dept_id:
        flash('Position name and department are required.', 'danger')
        return redirect(back)
    try:
        dept_id = int(dept_id)
    except (TypeError, ValueError):
        flash('Invalid department.', 'danger')
        return redirect(back)

    exists = query("""SELECT position_id FROM Position
                      WHERE department_id=? AND LOWER(position_name)=LOWER(?)""",
                   (dept_id, name), one=True)
    if exists:
        flash(f'Position "{name}" already exists in this department.', 'warning')
        return redirect(back)

    is_dept_mgr_position = 1 if request.form.get('is_department_manager_position') == '1' else 0
    pid = execute("INSERT INTO Position(position_name, department_id, is_department_manager_position) VALUES(?,?,?)",
                  (name, dept_id, is_dept_mgr_position))
    log_audit('CREATE_POSITION', 'Organization',
              f'Added position "{name}" to department {dept_id}',
              'Position', None, action_details={'position_name': name, 'department_id': dept_id,
                                                'is_department_manager_position': is_dept_mgr_position})
    flash(f'Position "{name}" added to the catalog.', 'success')
    workflow_return = _workflow_return_url(request.form.get('return_to', ''),
                                            branch_id=request.form.get('branch_id', ''),
                                            department_id=dept_id, position_id=pid)
    if workflow_return:
        return redirect(workflow_return)
    if request.form.get('continue_setup') == '1':
        return redirect(url_for('employees.add_employee',
                                branch_id=request.form.get('branch_id', ''),
                                department_id=dept_id, position_id=pid,
                                setup_branch_manager='1'))
    return redirect(url_for('organization.roles'))


@org_bp.route('/roles/positions/<int:pid>/rename', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager')
def rename_position(pid):
    name = _normalize_position_name(request.form.get('position_name', ''))
    pos = query("SELECT * FROM Position WHERE position_id=?", (pid,), one=True)
    if not pos:
        flash('Position not found.', 'danger')
        return redirect(url_for('organization.roles'))
    if not name:
        flash('Position name cannot be empty.', 'danger')
        return redirect(url_for('organization.roles'))
    dup = query("""SELECT position_id FROM Position
                   WHERE department_id=? AND LOWER(position_name)=LOWER(?) AND position_id!=?""",
                (pos['department_id'], name, pid), one=True)
    if dup:
        flash(f'Position "{name}" already exists in this department.', 'warning')
        return redirect(url_for('organization.roles'))
    is_dept_mgr_position = 1 if request.form.get('is_department_manager_position') == '1' else 0
    execute("UPDATE Position SET position_name=?, is_department_manager_position=? WHERE position_id=?",
            (name, is_dept_mgr_position, pid))
    log_audit('RENAME_POSITION', 'Organization',
              f'Renamed position {pid} to "{name}"',
              'Position', pid, action_details={'is_department_manager_position': is_dept_mgr_position})
    flash('Position updated.', 'success')
    return redirect(url_for('organization.roles'))
