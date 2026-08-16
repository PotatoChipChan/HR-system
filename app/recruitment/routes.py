"""app/recruitment/routes.py – Job Application & Recruitment Module."""

import os
import secrets
from flask import (Blueprint, render_template, request, session,
                   flash, redirect, url_for, jsonify, send_from_directory,
                   current_app)
from app.database import query, execute, log_audit, close_job_posting_for_application
from app.recruitment.scoping import application_visibility_scope
from app.auth.routes import login_required, role_required
from app.notifications.email_service import send_email
from datetime import datetime

recruit_bp = Blueprint('recruitment', __name__, url_prefix='/recruitment')


# ── Record-Level Authorization Helpers ────────────────────────────────────────
# Every helper resolves the record's owner scope and compares it against the
# session. The dept-manager flag (session.is_dept_manager) is evaluated
# independently of the role name. Employees never pass these checks.

AUDIENCE_VALUES = ('Internal', 'External', 'Both')


def _approved_selection_for_application(aid):
    """Return the approved selection decision for an application, if any.

    The scorecard ranking identifies eligible candidates; the approved
    decision records the HR Manager's confirmation for the offer workflow.
    """
    return query("""SELECT r.* FROM Candidate_Recommendation r
                    JOIN Job_Application ja ON ja.application_id=r.application_id
                    WHERE r.application_id=? AND r.posting_id=ja.posting_id
                      AND r.status='Approved'
                    LIMIT 1""", (aid,), one=True)


def _is_confirmable_candidate(aid, pid):
    """Return whether an application is within the posting's confirmable
    candidate slots.

    The scorecard ranking orders candidates by completed-scorecard total.
    A candidate is confirmable while the number of candidates strictly above
    them in the ranking is smaller than the posting's unfilled approved
    openings, so the HR Manager can confirm the top N candidates of an
    N-opening posting (ties at the cutoff stay eligible — the offer-send
    reservation gate still caps pending offers at unfilled openings).
    """
    row = query("""WITH scores AS (
                       SELECT ja.application_id,
                              SUM(isc.technical + isc.communication + isc.fit) AS total
                       FROM Job_Application ja
                       JOIN Interview i ON i.application_id=ja.application_id
                       JOIN Interview_Scorecard isc ON isc.interview_id=i.interview_id
                       WHERE ja.posting_id=? AND i.status='Completed'
                       GROUP BY ja.application_id
                   )
                   SELECT (SELECT COUNT(*) FROM scores s2 WHERE s2.total > s.total) AS strictly_above
                   FROM scores s
                   WHERE s.application_id=?""",
                 (pid, aid), one=True)
    if not row:
        return False
    jp = query("SELECT approved_openings, filled_openings FROM Job_Posting WHERE posting_id=?",
               (pid,), one=True)
    if not jp:
        return False
    slots = max(0, int(jp['approved_openings'] or 1) - int(jp['filled_openings'] or 0))
    return int(row['strictly_above']) < slots


def _can_access_application(aid):
    scope_sql, scope_params = application_visibility_scope(session)
    app = query(f"""
        SELECT 1
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        LEFT JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE ja.application_id=? AND ({scope_sql})
    """, [aid] + scope_params, one=True)
    return bool(app)


def _can_access_posting(pid):
    jp = query("""SELECT jp.department_id, jp.branch_id, b.company_id
                  FROM Job_Posting jp
                  JOIN Branch b ON jp.branch_id=b.branch_id
                  WHERE jp.posting_id=?""", (pid,), one=True)
    if not jp:
        return False
    role = session.get('user_role')
    if session.get('is_dept_manager') and session.get('managed_dept_id'):
        return jp['department_id'] == session.get('managed_dept_id')
    if role == 'Manager':
        return jp['branch_id'] == session.get('branch_id')
    if role in ('Admin', 'HR', 'HR Manager'):
        return jp['company_id'] == session.get('company_id')
    return False


def _can_access_interview(iid):
    iv = query("SELECT application_id FROM Interview WHERE interview_id=?", (iid,), one=True)
    if not iv:
        return False
    return _can_access_application(iv['application_id'])


def _can_access_contract(cid):
    c = query("SELECT application_id FROM Contract WHERE contract_id=?", (cid,), one=True)
    if not c:
        return False
    return _can_access_application(c['application_id'])


def _can_access_vacancy_request(rid):
    req = query("""SELECT vr.department_id, d.branch_id, b.company_id
                   FROM Vacancy_Request vr
                   JOIN Department d ON vr.department_id=d.department_id
                   JOIN Branch b ON d.branch_id=b.branch_id
                   WHERE vr.request_id=?""", (rid,), one=True)
    if not req:
        return False
    role = session.get('user_role')
    if session.get('is_dept_manager') and session.get('managed_dept_id'):
        return req['department_id'] == session.get('managed_dept_id')
    if role == 'Manager':
        return req['branch_id'] == session.get('branch_id')
    if role in ('Admin', 'HR', 'HR Manager'):
        return req['company_id'] == session.get('company_id')
    return False


def _deny_access():
    flash('Access denied.', 'danger')
    return redirect(url_for('main.dashboard'))


def _is_valid_audience(value):
    return value in AUDIENCE_VALUES


def _mykad_birth_and_gender(ic_number):
    """Return DOB and gender encoded in a valid Malaysian MyKad number.

    The first six digits are YYMMDD and the final digit is odd for Male,
    even for Female.  Do not infer either value from incomplete or malformed
    candidate-supplied numbers.
    """
    digits = (ic_number or '').replace('-', '').replace(' ', '')
    if not digits.isdigit() or len(digits) != 12:
        return '', ''
    try:
        yy = int(digits[:2])
        century = 2000 if yy <= datetime.now().year % 100 else 1900
        parsed_birth_date = datetime.strptime(
            f'{century + yy}{digits[2:6]}', '%Y%m%d').date()
        if parsed_birth_date > datetime.now().date():
            return '', ''
        birth_date = parsed_birth_date.isoformat()
    except ValueError:
        return '', ''
    return birth_date, 'Male' if int(digits[-1]) % 2 else 'Female'


# ── Public Careers Page ─────────────────────────────────────────────────────

@recruit_bp.route('/careers')
def careers():
    """Public career page — no login required. Standalone design."""
    from datetime import datetime
    company = query("SELECT * FROM Company ORDER BY company_id LIMIT 1", one=True)
    postings = query("""
        SELECT jp.*, d.department_name, b.name as branch_name
        FROM Job_Posting jp
        JOIN Department d ON jp.department_id = d.department_id
        JOIN Branch b ON jp.branch_id = b.branch_id
        WHERE jp.status IN ('Open','Partially Filled')
          AND jp.target_audience IN ('External','Both')
        ORDER BY jp.created_at DESC
    """)
    apply_email = 'careers@smarthr.my'
    email_cfg = query("SELECT username FROM Email_Config WHERE is_active=1 ORDER BY config_id DESC LIMIT 1", one=True)
    if email_cfg and email_cfg['username']:
        apply_email = email_cfg['username']
    return render_template('recruitment/careers.html',
                           postings=postings,
                           open_count=len(postings),
                           company=dict(company) if company else None,
                           apply_email=apply_email,
                           current_year=datetime.now().year)


# ── Job Postings ─────────────────────────────────────────────────────────────

@recruit_bp.route('/postings/add', methods=['GET', 'POST'])
@role_required('Admin', 'HR Manager')
def add_posting():
    co = session.get('company_id')
    if request.method == 'POST':
        f = request.form
        emp_type = f.get('employment_type', '')
        norm = {'Full-time': 'Full-Time', 'Part-time': 'Part-Time',
                'Full Time': 'Full-Time', 'Part Time': 'Part-Time',
                'fulltime': 'Full-Time', 'parttime': 'Part-Time'}
        emp_type = norm.get(emp_type, emp_type)
        if emp_type not in ('Full-Time', 'Part-Time', 'Contract'):
            flash('Select a valid employment type.', 'danger')
            return redirect(url_for('recruitment.add_posting'))

        audience = f.get('target_audience', 'Both')
        if not _is_valid_audience(audience):
            flash('Invalid audience selection.', 'danger')
            return redirect(url_for('recruitment.add_posting'))

        try:
            dept_id = int(f['department_id'])
            branch_id = int(f['branch_id'])
        except (TypeError, ValueError):
            flash('Please select a department and branch.', 'danger')
            return redirect(url_for('recruitment.add_posting'))

        # Server-side validation: department, branch and position must belong
        # to the session company and agree with each other (C20).
        dept = query("""SELECT d.department_id, d.branch_id, b.company_id
                        FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
                        WHERE d.department_id=?""", (dept_id,), one=True)
        if not dept or dept['company_id'] != co:
            flash('Selected department does not belong to your company.', 'danger')
            return redirect(url_for('recruitment.add_posting'))
        if branch_id != dept['branch_id']:
            flash('Selected branch does not match the department.', 'danger')
            return redirect(url_for('recruitment.add_posting'))

        # Catalog-only selection: new titles must first be added by HR/Admin
        try:
            position_id = int(f.get('position_id', ''))
        except (TypeError, ValueError):
            position_id = None
        pos = query("SELECT * FROM Position WHERE position_id=? AND is_active=1",
                    (position_id,), one=True) if position_id else None
        if not pos:
            flash('Please select a position from the catalog (add new titles under Roles & Permissions).', 'danger')
            return redirect(url_for('recruitment.add_posting'))
        if pos['department_id'] != dept_id:
            flash('Selected position does not belong to the chosen department.', 'danger')
            return redirect(url_for('recruitment.add_posting'))

        try:
            approved_openings = int(f.get('openings') or 1)
        except (TypeError, ValueError):
            approved_openings = 0
        if approved_openings < 1 or approved_openings > 50:
            flash('Number of openings must be between 1 and 50.', 'danger')
            return redirect(url_for('recruitment.add_posting'))

        try:
            min_salary = float(f['min_salary']) if f.get('min_salary') else None
            max_salary = float(f['max_salary']) if f.get('max_salary') else None
        except (TypeError, ValueError):
            flash('Salary values must be valid numbers.', 'danger')
            return redirect(url_for('recruitment.add_posting'))
        if ((min_salary is not None and min_salary < 0)
                or (max_salary is not None and max_salary < 0)):
            flash('Salary values cannot be negative.', 'danger')
            return redirect(url_for('recruitment.add_posting'))
        if min_salary is not None and max_salary is not None and min_salary > max_salary:
            flash('Minimum salary cannot exceed maximum salary.', 'danger')
            return redirect(url_for('recruitment.add_posting'))

        pid = execute("""INSERT INTO Job_Posting
            (title, department_id, branch_id, employment_type, position_id,
             min_salary, max_salary, description, requirements,
             target_audience, posted_by, status, approved_openings)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'Open',?)""",
            (pos['position_name'], dept_id, branch_id,
             emp_type, pos['position_id'],
             min_salary, max_salary,
             f.get('description', ''), f.get('requirements', ''),
             audience, session['user_id'], approved_openings))
        log_audit('CREATE_POSTING', 'Recruitment',
                  f'Direct posting created: {pos["position_name"]}',
                  action_details={'posting_id': pid, 'title': pos['position_name'],
                                  'position_id': pos['position_id'],
                                  'target_audience': audience,
                                  'approved_openings': approved_openings})
        flash(f'Job posting "{pos["position_name"]}" created!', 'success')
        return redirect(url_for('recruitment.view_posting', pid=pid))

    departments = query("""
        SELECT d.*, b.name as branch_name
        FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
        WHERE b.company_id=?
        ORDER BY b.name, d.department_name
    """, (co,))
    branches = query("SELECT * FROM Branch WHERE company_id=? ORDER BY name", (co,))
    positions = query("""SELECT p.* FROM Position p
                         JOIN Department d ON p.department_id=d.department_id
                         JOIN Branch b ON d.branch_id=b.branch_id
                         WHERE p.is_active=1 AND b.company_id=?
                         ORDER BY p.position_name""", (co,))
    return render_template('recruitment/add_posting.html',
                           departments=departments, branches=branches, positions=positions,
                           audience_values=AUDIENCE_VALUES,
                           prefill_branch=request.args.get('branch_id', ''),
                           prefill_department=request.args.get('department_id', ''),
                           prefill_position=request.args.get('position_id', ''))


@recruit_bp.route('/postings')
@login_required
def list_postings():
    role = session.get('user_role')
    co = session.get('company_id')
    if role == 'Employee' and not session.get('is_dept_manager'):
        return _deny_access()
    search = request.args.get('q', '')
    dept = request.args.get('dept', '')
    branch_filter = request.args.get('branch', '')

    show = request.args.get('show', 'active')
    if show == 'closed':
        conditions = ["jp.status IN ('Filled','Closed')"]
    else:
        conditions = ["jp.status IN ('Open','Partially Filled')"]
    args = []
    if session.get('is_dept_manager') and session.get('managed_dept_id'):
        conditions.append("jp.department_id=?")
        args.append(session.get('managed_dept_id'))
    elif role == 'Manager':
        conditions.append("jp.branch_id=?")
        args.append(session.get('branch_id'))
    elif role in ('Admin', 'HR', 'HR Manager'):
        conditions.append("b.company_id=?")
        args.append(co)

    sql = """
        SELECT jp.*, d.department_name, b.name as branch_name,
               (SELECT COUNT(*) FROM Job_Application WHERE posting_id=jp.posting_id) as app_count
        FROM Job_Posting jp
        JOIN Department d ON jp.department_id=d.department_id
        JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE """ + " AND ".join(conditions)

    if search:
        sql += " AND jp.title LIKE ?"
        args.append(f'%{search}%')
    if dept:
        sql += " AND jp.department_id=?"
        args.append(dept)
    if branch_filter:
        sql += " AND jp.branch_id=?"
        args.append(branch_filter)

    sql += " ORDER BY jp.created_at DESC"
    postings = query(sql, args) if args else query(sql)
    departments = query("""SELECT d.* FROM Department d
                           JOIN Branch b ON d.branch_id=b.branch_id
                           WHERE b.company_id=? ORDER BY d.department_name""", (co,))
    branches = query("SELECT * FROM Branch WHERE company_id=? ORDER BY name", (co,))
    return render_template('recruitment/list_postings.html',
                           postings=postings, departments=departments, branches=branches, show=show)


@recruit_bp.route('/postings/<int:pid>')
@login_required
def view_posting(pid):
    if not _can_access_posting(pid):
        return _deny_access()
    posting = query("""
        SELECT jp.*, d.department_name, b.name as branch_name, e.full_name as posted_by_name
        FROM Job_Posting jp
        JOIN Department d ON jp.department_id=d.department_id
        JOIN Branch b ON jp.branch_id=b.branch_id
        LEFT JOIN Employee e ON jp.posted_by=e.employee_id
        WHERE jp.posting_id=?
    """, (pid,), one=True)
    if not posting:
        flash('Posting not found.', 'danger')
        return redirect(url_for('recruitment.list_postings'))

    applications = query("""
        SELECT ja.* FROM Job_Application ja
        WHERE ja.posting_id=?
        ORDER BY ja.ai_score DESC, ja.applied_at DESC
    """, (pid,))
    new_count = sum(1 for a in applications if a['status'] == 'New')

    # Scorecard-based ranking within this posting, from completed interviews only.
    ranking = query("""
        SELECT ja.application_id, ja.applicant_name, ja.status,
               COUNT(isc.scorecard_id) as scorecard_count,
               SUM(isc.technical + isc.communication + isc.fit) as total
        FROM Job_Application ja
        JOIN Interview i ON i.application_id = ja.application_id
        JOIN Interview_Scorecard isc ON isc.interview_id = i.interview_id
        WHERE ja.posting_id=? AND i.status='Completed'
        GROUP BY ja.application_id
        ORDER BY total DESC, ja.applicant_name ASC
    """, (pid,))

    recommendations = query("""
        SELECT r.*, ja.applicant_name FROM Candidate_Recommendation r
        JOIN Job_Application ja ON ja.application_id=r.application_id
        WHERE r.posting_id=?
        ORDER BY r.created_at DESC
    """, (pid,))

    return render_template('recruitment/view_posting.html',
                           posting=posting, applications=applications, new_count=new_count,
                           ranking=ranking, recommendations=recommendations)


# ── Public Apply (No Login Required) ─────────────────────────────────────────

@recruit_bp.route('/apply/<int:pid>', methods=['GET', 'POST'])
def public_apply(pid):
    posting = query("""
        SELECT jp.*, d.department_name, b.name as branch_name, b.company_id
        FROM Job_Posting jp
        JOIN Department d ON jp.department_id=d.department_id
        JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE jp.posting_id=?
    """, (pid,), one=True)
    if not posting:
        return '<h2>Job not found.</h2>', 404

    if posting['status'] not in ('Open', 'Partially Filled'):
        return '<h2>This job posting is no longer accepting applications.</h2>', 404

    # Logged-in employees are steered to the internal apply flow. Redirect to
    # the GET detail route (never the POST-only apply endpoint).
    if session.get('user_id'):
        return redirect(url_for('recruitment.internal_job_detail', pid=pid))

    if posting['target_audience'] == 'Internal':
        return '<h2>Job not found.</h2>', 404

    if request.method == 'POST':
        f = request.form
        resume_file = request.files.get('resume')
        resume_path = None
        if resume_file and resume_file.filename:
            import os, uuid
            from flask import current_app
            ext = os.path.splitext(resume_file.filename)[1] or '.pdf'
            filename = f"resume_{uuid.uuid4().hex}{ext}"
            resume_dir = os.path.join(current_app.root_path, '..', 'uploads', 'resumes')
            os.makedirs(resume_dir, exist_ok=True)
            resume_file.save(os.path.join(resume_dir, filename))
            resume_path = filename

        app_id = execute("""
            INSERT INTO Job_Application
            (posting_id, company_id, applicant_name, applicant_email, applicant_phone, resume_path, cover_letter, source, status)
            VALUES (?,?,?,?,?,?,?,'Portal','New')
        """, (pid, posting['company_id'], f['applicant_name'], f['applicant_email'], f.get('applicant_phone', ''),
              resume_path, f.get('cover_letter', '')))

        # AI scoring and screening
        try:
            posting_data = query("""
                SELECT title, description, requirements FROM Job_Posting WHERE posting_id=?
            """, (pid,), one=True)
            if posting_data:
                from flask import current_app
                app_data = query("""
                    SELECT application_id, applicant_name, cover_letter, resume_path
                    FROM Job_Application WHERE application_id=?
                """, (app_id,), one=True)
                if app_data:
                    from app.recruitment.scorer import score_and_persist
                    score_and_persist(app_id, dict(posting_data), dict(app_data),
                                      app_root=current_app.root_path)
        except Exception as e:
            print(f"[PUBLIC APPLY] AI scoring failed: {e}")

        return render_template('recruitment/apply_thanks.html', posting=posting)

    return render_template('recruitment/apply.html', posting=posting)


# ── Job Applications ─────────────────────────────────────────────────────────

@recruit_bp.route('/applications')
@login_required
def list_applications():
    role = session['user_role']
    if role == 'Employee' and not session.get('is_dept_manager'):
        return _deny_access()
    type_filter = request.args.get('type', '')

    # Default view shows Shortlisted applications; user filters by status via dropdown.
    status_filter = request.args.get('status', 'Shortlisted')

    valid_statuses = ('New', 'Shortlisted', 'Interview', 'Rejected')
    if status_filter in valid_statuses:
        status_condition = "ja.status=?"
        status_params = [status_filter]
        if status_filter == 'Shortlisted':
            order = "ja.ai_score DESC, ja.applied_at DESC"
        elif status_filter == 'Rejected':
            order = "ja.reviewed_at DESC"
        else:
            order = "ja.applied_at DESC"
    else:
        status_filter = ''
        status_condition = "1=1"
        status_params = []
        order = "ja.applied_at DESC"

    branch_filter = request.args.get('branch', '')
    dept_filter = request.args.get('dept', '')
    job_filter = request.args.get('job', '')

    sql = f"""
        SELECT ja.*, jp.title as job_title, jp.posting_id, d.department_name, b.name as branch_name
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        LEFT JOIN Department d ON jp.department_id=d.department_id
        LEFT JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE {status_condition}
    """
    args = list(status_params)
    co = session.get('company_id')
    scope_sql, scope_params = application_visibility_scope(session)
    sql += " AND (" + scope_sql + ")"
    args.extend(scope_params)
    if type_filter in ('Internal', 'External'):
        sql += " AND ja.applicant_type=?"
        args.append(type_filter)
    if branch_filter:
        sql += " AND jp.branch_id=?"
        args.append(branch_filter)
    if dept_filter:
        sql += " AND jp.department_id=?"
        args.append(dept_filter)
    if job_filter:
        sql += " AND jp.posting_id=?"
        args.append(job_filter)
    sql += f" ORDER BY {order}"

    applications = query(sql, args) if args else query(sql)
    # Keep filter choices within the same branch/department scope as the
    # application results; otherwise managers see filters that can never
    # return a record and reveal unrelated organisation structure.
    if session.get('is_dept_manager') and session.get('managed_dept_id'):
        scope_value = session['managed_dept_id']
        branches = query("""SELECT b.* FROM Branch b
                            JOIN Department d ON d.branch_id=b.branch_id
                            WHERE d.department_id=? ORDER BY b.name""", (scope_value,))
        departments = query("""
            SELECT d.department_id, d.department_name, d.branch_id, b.name AS branch_name
            FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
            WHERE d.department_id=? ORDER BY d.department_name
        """, (scope_value,))
        job_postings = query("""SELECT jp.posting_id, jp.title, jp.branch_id, jp.department_id
                                FROM Job_Posting jp
                                WHERE jp.department_id=? ORDER BY jp.title""", (scope_value,))
    elif role == 'Manager':
        scope_value = session.get('branch_id')
        branches = query("SELECT * FROM Branch WHERE branch_id=? ORDER BY name", (scope_value,))
        departments = query("""
            SELECT d.department_id, d.department_name, d.branch_id, b.name AS branch_name
            FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
            WHERE b.branch_id=? ORDER BY d.department_name
        """, (scope_value,))
        job_postings = query("""SELECT jp.posting_id, jp.title, jp.branch_id, jp.department_id
                                FROM Job_Posting jp
                                WHERE jp.branch_id=? ORDER BY jp.title""", (scope_value,))
    else:
        branches = query("SELECT * FROM Branch WHERE company_id=? ORDER BY name", (co,))
        departments = query("""
            SELECT d.department_id, d.department_name, d.branch_id, b.name AS branch_name
            FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
            WHERE b.company_id=? ORDER BY d.department_name
        """, (co,))
        job_postings = query("""SELECT jp.posting_id, jp.title, jp.branch_id, jp.department_id
                                FROM Job_Posting jp
                                JOIN Branch b ON jp.branch_id=b.branch_id
                                WHERE b.company_id=? ORDER BY jp.title""", (co,))
    from app.recruitment.scorer import screening_rule_info
    return render_template('recruitment/list_applications.html',
                           applications=applications, status_filter=status_filter,
                           branches=branches, departments=departments, job_postings=job_postings,
                           type_filter=type_filter,
                           screening_rule=screening_rule_info())


@recruit_bp.route('/applications/<int:aid>')
@login_required
def view_application(aid):
    if not _can_access_application(aid):
        return _deny_access()
    app = query("""
        SELECT ja.*, jp.title as job_title, jp.branch_id as posting_branch,
               d.department_name, b.name as branch_name
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        LEFT JOIN Department d ON jp.department_id=d.department_id
        LEFT JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE ja.application_id=?
    """, (aid,), one=True)
    if not app:
        flash('Application not found.', 'danger')
        return redirect(url_for('recruitment.list_applications'))

    interviews = query("""
        SELECT * FROM Interview WHERE application_id=? ORDER BY scheduled_at DESC
    """, (aid,))
    # Resolve interviewer names for display (convert to dicts for mutability)
    interview_list = []
    for iv in interviews:
        d = dict(iv)
        ids_str = iv['interviewer_ids'] or ''
        if ids_str:
            ids = [x.strip() for x in ids_str.split(',') if x.strip()]
            names = []
            for eid in ids:
                e = query("SELECT full_name FROM Employee WHERE employee_id=?", (int(eid),), one=True)
                names.append(e['full_name'] if e else eid)
            d['interviewer_names'] = ', '.join(names)
        else:
            d['interviewer_names'] = ''
        d['scorecard'] = query(
            "SELECT * FROM Interview_Scorecard WHERE interview_id=?", (iv['interview_id'],), one=True)
        interview_list.append(d)
    interviews = interview_list

    selection_approval = _approved_selection_for_application(aid)
    selection_record = query("""SELECT * FROM Candidate_Recommendation
                                WHERE application_id=? AND posting_id=?
                                ORDER BY created_at DESC LIMIT 1""",
                             (aid, app['posting_id'])) if app['posting_id'] else None
    has_completed_scorecard = any(
        iv['status'] == 'Completed' and iv['scorecard'] for iv in interviews)
    is_confirmable_candidate = bool(
        app['posting_id'] and _is_confirmable_candidate(aid, app['posting_id']))

    contract = query("""
        SELECT * FROM Contract WHERE application_id=?
    """, (aid,), one=True)

# Eligible interviewers: Virtual uses the existing roster (branch Managers
    # + Admin/HR/HR Manager company-wide) with the vacancy requester first;
    # Physical restricts every role to the posting branch. The manager who
    # requested this position is listed first with a "(Requester)" label when
    # active and eligible for the chosen format. Job_Posting.branch_id is the
    # authoritative posting branch.
    co = session.get('company_id')
    posting_branch = app['posting_branch']
    requester = _requester_for_posting(app['posting_id'], co)
    eligible = _get_eligible_interviewers(
        co, posting_branch,
        requester_id=requester['employee_id'] if requester else None)
    branch_has_local = any(e['branch_id'] == (posting_branch or 0) for e in eligible)
    for e in eligible:
        e['is_requester'] = bool(requester and e['employee_id'] == requester['employee_id'])

    offer_approval = None
    delivery_log = None
    if contract:
        offer_approval = query("SELECT * FROM Offer_Approval WHERE contract_id=?",
                               (contract['contract_id'],), one=True)
        delivery_log = query("""SELECT * FROM Email_Delivery_Log
                                WHERE related_type='offer' AND related_id=?
                                ORDER BY delivery_id DESC LIMIT 3""",
                             (contract['contract_id'],))

    return render_template('recruitment/view_application.html',
                           app=app, interviews=interviews, contract=contract,
                           eligible_interviewers=eligible,
                           posting_branch=posting_branch,
                           branch_has_local_interviewers=branch_has_local,
                           selection_approval=selection_approval,
                           selection_record=selection_record,
                           has_completed_scorecard=has_completed_scorecard,
                           is_confirmable_candidate=is_confirmable_candidate,
                           offer_approval=offer_approval, delivery_log=delivery_log)


@recruit_bp.route('/applications/<int:aid>/status', methods=['POST'])
@role_required('Admin', 'HR')
def update_application_status(aid):
    if not _can_access_application(aid):
        return _deny_access()
    new_status = request.form.get('status')
    valid = ['New', 'Shortlisted', 'Interview', 'Offered', 'Hired', 'Rejected']
    if new_status not in valid:
        flash('Invalid status.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    current = query("SELECT status FROM Job_Application WHERE application_id=?", (aid,), one=True)
    prev_status = current['status'] if current else None
    manual_transitions = {
        'New': ('Shortlisted', 'Rejected'),
        'Shortlisted': ('New', 'Rejected'),
        'Interview': ('Rejected',),
    }
    if new_status not in manual_transitions.get(prev_status, ()):
        if new_status in ('Interview', 'Offered', 'Hired'):
            flash(f'{new_status} is controlled by the interview, offer, and hire workflow and cannot be set directly.', 'danger')
        else:
            flash(f'Cannot change an application from {prev_status or "its current status"} to {new_status}.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    execute("""
        UPDATE Job_Application SET status=?, reviewed_by=?, reviewed_at=datetime('now')
        WHERE application_id=?
    """, (new_status, session['user_id'], aid))

    # Manual screening override: shortlisting a candidate, or reversing an
    # existing shortlist, overrides the AI screening recommendation. The
    # acting user, reason and timestamp are recorded for auditability.
    if new_status == 'Shortlisted' or (prev_status == 'Shortlisted' and new_status != 'Shortlisted'):
        override_reason = request.form.get('override_reason', '').strip() or 'Manual override'
        execute("""UPDATE Job_Application
                   SET shortlist_override_by=?, shortlist_override_reason=?,
                       shortlist_override_at=datetime('now')
                   WHERE application_id=?""",
                (session['user_id'], override_reason, aid))
        log_audit('SHORTLIST_OVERRIDE', 'Recruitment',
                  f'Application {aid} shortlist overridden: {prev_status} -> {new_status}',
                  action_details={'application_id': aid, 'from_status': prev_status,
                                  'to_status': new_status, 'reason': override_reason})

    # Send rejection email
    if new_status == 'Rejected':
        app_data = query("""
            SELECT ja.*, jp.title as job_title FROM Job_Application ja
            LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
            WHERE ja.application_id=?
        """, (aid,), one=True)
        if app_data and app_data['applicant_email']:
            html = render_template('emails/application_rejected.html',
                employee_name=app_data['applicant_name'],
                title='Application Update',
                job_title=app_data['job_title'])
            send_email(f'Application Status – {app_data["job_title"]}', app_data['applicant_email'], html)

    log_audit('UPDATE_APP_STATUS', 'Recruitment',
              f'Application {aid} status changed to {new_status}',
              action_details={'new_status': new_status})
    flash(f'Status updated to {new_status}.', 'success')
    return redirect(url_for('recruitment.view_application', aid=aid))


# ── Add Application (Manual) ──────────────────────────────────────────────────

@recruit_bp.route('/postings/<int:pid>/add-application', methods=['POST'])
@role_required('Admin', 'HR')
def add_application(pid):
    if not _can_access_posting(pid):
        return _deny_access()
    f = request.form
    posting = query("""SELECT jp.*, b.company_id FROM Job_Posting jp
                       JOIN Branch b ON jp.branch_id=b.branch_id
                       WHERE jp.posting_id=?""", (pid,), one=True)
    if not posting:
        flash('Posting not found.', 'danger')
        return redirect(url_for('recruitment.list_postings'))
    applicant_type = 'External'
    internal_employee_id = None
    if f.get('internal_employee_id'):
        try:
            internal_employee_id = int(f['internal_employee_id'])
        except (TypeError, ValueError):
            flash('Select a valid employee for an internal application.', 'danger')
            return redirect(url_for('recruitment.view_posting', pid=pid))
        emp = query("""SELECT e.employee_id FROM Employee e
                       JOIN Branch b ON e.branch_id=b.branch_id
                       WHERE e.employee_id=? AND e.is_active=1 AND b.company_id=?""",
                    (internal_employee_id, posting['company_id']), one=True)
        if not emp:
            flash('Selected employee is not an active employee of this company.', 'danger')
            return redirect(url_for('recruitment.view_posting', pid=pid))
        internal_employee_id = emp['employee_id']
        applicant_type = 'Internal'
        f = dict(f)
        f['applicant_name'] = f.get('applicant_name') or ''
        f['applicant_email'] = f.get('applicant_email') or ''
    execute("""
        INSERT INTO Job_Application (posting_id, company_id, applicant_name, applicant_email, applicant_phone, cover_letter, applicant_type, internal_employee_id, status)
        VALUES (?,?,?,?,?,?,?,?,'New')
    """, (pid, posting['company_id'], f['applicant_name'], f['applicant_email'],
          f.get('applicant_phone', ''), f.get('cover_letter', ''),
          applicant_type, internal_employee_id))
    flash(f'Application added for {f["applicant_name"]}.', 'success')
    return redirect(url_for('recruitment.view_posting', pid=pid))


# ── Reject Non-Shortlisted Candidates ──────────────────────────────────────────

@recruit_bp.route('/postings/<int:pid>/reject-non-shortlisted', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager')
def reject_non_shortlisted(pid):
    if not _can_access_posting(pid):
        return _deny_access()
    posting = query("SELECT title FROM Job_Posting WHERE posting_id=?", (pid,), one=True)
    if not posting:
        flash('Posting not found.', 'danger')
        return redirect(url_for('recruitment.list_postings'))

    others = query("""
        SELECT application_id, applicant_name, applicant_email
        FROM Job_Application
        WHERE posting_id=? AND status='New'
    """, (pid,))
    if not others:
        flash('No non-shortlisted candidates to reject.', 'info')
        return redirect(url_for('recruitment.view_posting', pid=pid))

    rejected = 0
    failed = 0
    for o in others:
        execute("""
            UPDATE Job_Application SET status='Rejected', reviewed_by=?, reviewed_at=datetime('now')
            WHERE application_id=?
        """, (session['user_id'], o['application_id']))
        if o['applicant_email']:
            html = render_template('emails/application_rejected.html',
                employee_name=o['applicant_name'],
                title='Application Update',
                job_title=posting['title'])
            if send_email(f'Application Status – {posting["title"]}', o['applicant_email'], html):
                rejected += 1
            else:
                failed += 1

    log_audit('REJECT_NON_SHORTLISTED', 'Recruitment',
              f'Rejected {len(others)} non-shortlisted candidate(s) for posting #{pid}',
              action_details={'posting_id': pid, 'rejected_count': len(others), 'failed_emails': failed})
    msg = f'Rejected {len(others)} non-shortlisted candidate(s). Emails sent.'
    if failed:
        msg += f' ({failed} email(s) failed.)'
    flash(msg, 'success' if not failed else 'warning')
    return redirect(url_for('recruitment.view_posting', pid=pid))


@recruit_bp.route('/postings/<int:pid>/delete', methods=['POST'])
@role_required('Admin', 'HR Manager')
def delete_posting(pid):
    """Soft-delete (archive) a job posting.

    A posting can only be archived when no interviews have been scheduled for
    any of its applications. The posting is hidden from every posting list but
    its row and all related records are preserved (status='Archived').
    """
    if not _can_access_posting(pid):
        return _deny_access()
    posting = query("SELECT title FROM Job_Posting WHERE posting_id=?", (pid,), one=True)
    if not posting:
        flash('Posting not found.', 'danger')
        return redirect(url_for('recruitment.list_postings'))
    interview_count = query("""
        SELECT COUNT(*) as c FROM Interview i
        JOIN Job_Application ja ON i.application_id=ja.application_id
        WHERE ja.posting_id=?
    """, (pid,), one=True)['c']
    if interview_count > 0:
        flash('This posting cannot be deleted because interviews have already been scheduled for it.', 'danger')
        return redirect(url_for('recruitment.view_posting', pid=pid))

    execute("""
        UPDATE Job_Posting SET status='Archived',
               closed_at=COALESCE(closed_at, datetime('now'))
        WHERE posting_id=?
    """, (pid,))
    log_audit('DELETE_POSTING', 'Recruitment',
              f'Posting #{pid} deleted (archived)', action_details={'posting_id': pid})
    flash('Posting deleted.', 'success')
    return redirect(url_for('recruitment.list_postings'))


# ── Interviews ───────────────────────────────────────────────────────────────

@recruit_bp.route('/interviews')
@login_required
def list_interviews():
    role = session['user_role']
    if role == 'Employee' and not session.get('is_dept_manager'):
        return _deny_access()
    status_filter = request.args.get('status', '')
    sql = """
        SELECT i.*, ja.applicant_name, jp.title as job_title
        FROM Interview i
        JOIN Job_Application ja ON i.application_id=ja.application_id
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        LEFT JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE 1=1
    """
    args = []
    if session.get('is_dept_manager') and session.get('managed_dept_id'):
        sql += " AND jp.department_id=?"
        args.append(session.get('managed_dept_id'))
    elif role == 'Manager':
        sql += " AND jp.branch_id=?"
        args.append(session['branch_id'])
    elif role in ('Admin', 'HR', 'HR Manager'):
        sql += " AND b.company_id=?"
        args.append(session.get('company_id'))
    else:
        return _deny_access()
    if status_filter:
        sql += " AND i.status=?"
        args.append(status_filter)
    sql += " ORDER BY i.scheduled_at DESC"

    interviews = query(sql, args) if args else query(sql)
    # Resolve interviewer names (convert to list of dicts for mutability)
    interview_list = []
    for iv in interviews:
        d = dict(iv)
        ids_str = iv['interviewer_ids'] or ''
        if ids_str:
            ids = [x.strip() for x in ids_str.split(',') if x.strip()]
            names = []
            for eid in ids:
                e = query("SELECT full_name FROM Employee WHERE employee_id=?", (int(eid),), one=True)
                names.append(e['full_name'] if e else eid)
            d['interviewer_name'] = ', '.join(names)
        else:
            d['interviewer_name'] = ''
        interview_list.append(d)
    return render_template('recruitment/interviews.html', interviews=interview_list)


# ── Contract ─────────────────────────────────────────────────────────────────

@recruit_bp.route('/contract/<int:aid>', methods=['GET', 'POST'])
@role_required('Admin', 'HR')
def contract(aid):
    if not _can_access_application(aid):
        return _deny_access()

    app = query("""
        SELECT ja.*, jp.title as job_title, jp.department_id, jp.employment_type,
               d.department_name
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        LEFT JOIN Department d ON jp.department_id=d.department_id
        WHERE ja.application_id=?
    """, (aid,), one=True)
    if not app:
        flash('Application not found.', 'danger')
        return redirect(url_for('recruitment.list_applications'))
    if not app['posting_id'] or not app['department_id']:
        flash('A job posting with a department is required before a contract can be created.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    existing_contract = query("SELECT * FROM Contract WHERE application_id=?", (aid,), one=True)
    if request.method == 'POST':
        if not _approved_selection_for_application(aid):
            flash('Candidate selection must be approved before creating an offer contract.', 'danger')
            return redirect(url_for('recruitment.view_application', aid=aid))
        if existing_contract and existing_contract['status'] != 'Draft':
            flash('Only draft contracts can be edited. Sent or accepted offers cannot be changed.', 'danger')
            return redirect(url_for('recruitment.view_application', aid=aid))
        f = request.form
        emp_type = f.get('employment_type', '')
        norm = {'Full-time': 'Full-Time', 'Part-time': 'Part-Time',
                'Full Time': 'Full-Time', 'Part Time': 'Part-Time',
                'fulltime': 'Full-Time', 'parttime': 'Part-Time'}
        emp_type = norm.get(emp_type, emp_type)
        if emp_type not in ('Full-Time', 'Part-Time', 'Contract'):
            flash('Select a valid employment type.', 'danger')
            return redirect(url_for('recruitment.contract', aid=aid))
        try:
            offer_date = datetime.strptime(f.get('offer_date', ''), '%Y-%m-%d').date()
            start_date = datetime.strptime(f.get('start_date', ''), '%Y-%m-%d').date()
            work_start_time = datetime.strptime(f.get('work_start_time', ''), '%H:%M').time()
            work_end_time = datetime.strptime(f.get('work_end_time', ''), '%H:%M').time()
            base_salary = float(f.get('base_salary', ''))
        except (TypeError, ValueError):
            flash('Enter valid contract dates, working times, and base salary.', 'danger')
            return redirect(url_for('recruitment.contract', aid=aid))
        if base_salary < 0:
            flash('Base salary cannot be negative.', 'danger')
            return redirect(url_for('recruitment.contract', aid=aid))
        position = f.get('position', '').strip()
        if not position:
            flash('Enter a contract position.', 'danger')
            return redirect(url_for('recruitment.contract', aid=aid))
        if existing_contract:
            execute("""UPDATE Contract
                       SET offer_date=?, start_date=?, position=?, department_id=?,
                           work_start_time=?, work_end_time=?, base_salary=?, employment_type=?
                       WHERE contract_id=?""",
                    (offer_date.isoformat(), start_date.isoformat(), position, app['department_id'],
                     work_start_time.strftime('%H:%M'), work_end_time.strftime('%H:%M'), base_salary, emp_type,
                     existing_contract['contract_id']))
        else:
            execute("""
                INSERT INTO Contract
                (application_id, offer_date, start_date, position, department_id,
                 work_start_time, work_end_time, base_salary, employment_type, status)
                VALUES(?,?,?,?,?,?,?,?,?,'Draft')
            """, (aid, offer_date.isoformat(), start_date.isoformat(), position,
                  app['department_id'], work_start_time.strftime('%H:%M'), work_end_time.strftime('%H:%M'),
                  base_salary, emp_type))
        flash('Contract draft saved!', 'success')
        return redirect(url_for('recruitment.view_application', aid=aid))
    return render_template('recruitment/contract.html',
                           app=app, contract=existing_contract)


# ── Schedule Interview ────────────────────────────────────────────────────────

INTERVIEW_FORMATS = ('Physical', 'Virtual')


def _branch_address_text(branch):
    """Return a display address for a Branch, preferring the structured fields."""
    branch = dict(branch)
    parts = [branch.get('address_line1') or '', branch.get('address_line2') or '',
             branch.get('city') or '', branch.get('state') or '',
             branch.get('postal_code') or '']
    text = ', '.join(p.strip() for p in parts if p and p.strip()).strip(' ,')
    if not text:
        text = branch.get('address') or ''
    return text


def _resolve_interview_format(form, posting, prefix=''):
    """Validate the Physical/Virtual selection and resolve venue/link.

    Physical: location is always the posting branch's address (snapshot), never
    a manually typed location. An optional free-text venue (e.g. "Block A
    Meeting Room") is accepted from the form; when left blank the venue falls
    back to the branch address. Blocked when the branch or its address is
    missing. Virtual: a meeting link is required.

    Returns (format, meeting_link, location, venue, posting_branch_id) on
    success, or (None, error_message) on failure.
    """
    posting = dict(posting) if posting else None
    fmt = (form.get(prefix + 'format') or '').strip()
    if fmt not in INTERVIEW_FORMATS:
        return None, 'Select an interview format: Physical or Virtual.'

    if fmt == 'Physical':
        if not posting or not posting.get('branch_id'):
            return None, ('The posting has no branch, so a physical venue cannot be '
                          'determined. Use Virtual or attach the posting to a branch.')
        branch = query("SELECT * FROM Branch WHERE branch_id=?", (posting['branch_id'],), one=True)
        if not branch:
            return None, 'The posting branch no longer exists.'
        address = _branch_address_text(branch)
        if not address:
            return None, ('The posting branch has no address on file. Add the branch '
                          'address before scheduling a physical interview.')
        venue = (form.get(prefix + 'venue') or '').strip() or address
        return fmt, '', address, venue, branch['branch_id']

    link = (form.get(prefix + 'meeting_link') or '').strip()
    if not link:
        return None, 'A valid meeting link is required for a virtual interview.'
    return fmt, link, '', '', posting.get('branch_id') if posting else None


def _display_location(fmt, location, venue, meeting_link):
    """Combine location/venue/link into the string shown to candidates."""
    if fmt == 'Physical':
        loc = location or venue or ''
        if venue and venue != loc:
            return f'{loc} — {venue}'
        return loc or 'To be confirmed'
    return meeting_link or 'To be confirmed'


def _posting_for_application(aid):
    return query("""
        SELECT jp.*, b.name as branch_name FROM Job_Posting jp
        JOIN Branch b ON jp.branch_id=b.branch_id
        JOIN Job_Application ja ON ja.posting_id=jp.posting_id
        WHERE ja.application_id=?
    """, (aid,), one=True)

@recruit_bp.route('/application/<int:aid>/schedule-interview', methods=['POST'])
@role_required('Admin', 'HR')
def schedule_interview(aid):
    if session.get('user_role') == 'HR Manager':
        flash('You do not have permission to access that page.', 'danger')
        return redirect(url_for('main.dashboard'))
    if not _can_access_application(aid):
        return _deny_access()
    app_data = query("""
        SELECT ja.*, jp.title as job_title
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE ja.application_id=?
    """, (aid,), one=True)
    if not app_data:
        flash('Application not found.', 'danger')
        return redirect(url_for('recruitment.list_applications'))
    if app_data['status'] not in ('New', 'Shortlisted'):
        flash('Only New or Shortlisted candidates can be scheduled for an interview.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    f = request.form
    try:
        slot_dt = datetime.strptime(f"{f['date']} {f['time']}:00", '%Y-%m-%d %H:%M:%S')
    except (KeyError, ValueError):
        flash('Select a valid interview date and time.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))
    if slot_dt <= datetime.now():
        flash('Interview time must be in the future. The next minute is valid.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))
    if slot_dt.weekday() >= 5:
        flash('Interviews cannot be scheduled on weekends.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    policy = _get_or_create_interview_policy(session.get('company_id'))
    try:
        policy_start = datetime.strptime(policy['day_start_time'], '%H:%M').time()
        policy_end = datetime.strptime(policy['day_end_time'], '%H:%M').time()
    except (TypeError, ValueError):
        flash('Interview policy working hours are invalid. Ask an administrator to correct the policy.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))
    if not (policy_start <= slot_dt.time() <= policy_end):
        flash(f'Interview time must be within working hours ({policy["day_start_time"]}–{policy["day_end_time"]}).', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    try:
        duration = int(f.get('duration', 60))
    except (TypeError, ValueError):
        flash('Interview duration must be a whole number of minutes.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))
    if duration < 1:
        flash('Interview duration must be at least one minute.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))
    scheduled_at = slot_dt.strftime('%Y-%m-%d %H:%M:%S')

    # Resolve the format first: the eligible interviewer pool depends on it.
    posting = _posting_for_application(aid)
    fmt_result = _resolve_interview_format(f, posting)
    if fmt_result[0] is None:
        flash(fmt_result[1], 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))
    fmt, meeting_link, location, venue, posting_branch_id = fmt_result

    raw_interviewer_ids = f.getlist('interviewer_ids') if 'interviewer_ids' in f else []
    try:
        interviewer_id_list = [int(eid) for eid in raw_interviewer_ids if str(eid).strip()]
    except (TypeError, ValueError):
        flash('Select valid interviewers.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    posting_branch = posting['branch_id'] if posting else None
    requester = _requester_for_posting(app_data['posting_id'], session.get('company_id'))
    pool = _get_eligible_interviewers(
        session.get('company_id'), posting_branch, physical_only=(fmt == 'Physical'),
        requester_id=requester['employee_id'] if requester else None)

    if fmt == 'Physical' and not pool:
        flash('This branch has no eligible local interviewers. Schedule as Virtual instead.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    if interviewer_id_list:
        placeholders = ','.join('?' for _ in interviewer_id_list)
        valid_interviewers = query(
            f"""SELECT employee_id FROM Employee
                 WHERE employee_id IN ({placeholders}) AND company_id=? AND is_active=1""",
            tuple(interviewer_id_list) + (session.get('company_id'),))
        if len(valid_interviewers) != len(set(interviewer_id_list)):
            flash('Select active interviewers from your company.', 'danger')
            return redirect(url_for('recruitment.view_application', aid=aid))
        pool_ids = {p['employee_id'] for p in pool}
        if not set(interviewer_id_list) <= pool_ids:
            if fmt == 'Physical':
                flash('For physical interviews, only interviewers from the posting branch can be selected.', 'danger')
            else:
                flash('Select eligible interviewers for this interview.', 'danger')
            return redirect(url_for('recruitment.view_application', aid=aid))
    interviewer_ids = ','.join(str(eid) for eid in interviewer_id_list)

    # Panel availability: every selected interviewer must be free at the slot.
    for eid in interviewer_id_list:
        if _interviewer_busy(eid, slot_dt, duration):
            flash('An assigned interviewer already has an interview at that time. '
                  'Choose another slot.', 'danger')
            return redirect(url_for('recruitment.view_application', aid=aid))

    interview_id = execute("""
        INSERT INTO Interview
        (application_id, scheduled_at, duration_min, location, meeting_link, type,
         format, venue, posting_branch_id, status, interviewer_ids)
        VALUES(?,?,?,?,?,?,?,?,?,'Scheduled',?)
    """, (aid, scheduled_at, duration,
          location, meeting_link, 'In-Person' if fmt == 'Physical' else 'Online',
          fmt, venue, posting_branch_id, interviewer_ids))

    execute("UPDATE Job_Application SET status='Interview' WHERE application_id=?", (aid,))
    log_audit('SCHEDULE_INTERVIEW', 'Recruitment',
              f'Interview scheduled for application {aid} ({fmt})',
              action_details={'application_id': aid, 'format': fmt,
                              'venue': venue, 'meeting_link': meeting_link})

    # Send email to candidate
    from datetime import datetime as dt
    dt_obj = dt.strptime(scheduled_at, '%Y-%m-%d %H:%M:%S')
    display_location = _display_location(fmt, location, venue, meeting_link)
    html = render_template('emails/interview_scheduled.html',
        employee_name=app_data['applicant_name'],
        title='Interview Scheduled',
        job_title=app_data['job_title'],
        interview_date=dt_obj.strftime('%A, %d %B %Y'),
        interview_time=dt_obj.strftime('%I:%M %p'),
        location=display_location,
        interview_type=fmt,
        interview_ref='INT-%d' % interview_id)
    email_ok = send_email(f'Interview Invitation – {app_data["job_title"]}', app_data['applicant_email'], html)

    if email_ok:
        flash('Interview scheduled and email sent to candidate.', 'success')
    else:
        flash('Interview scheduled but email delivery failed. Check the candidate email address.', 'warning')
    return redirect(url_for('recruitment.view_application', aid=aid))


# ── Interview Result ──────────────────────────────────────────────────────────

@recruit_bp.route('/interview/<int:iid>/result', methods=['POST'])
@role_required('Admin', 'HR')
def interview_result(iid):
    """Retired legacy endpoint kept harmless for existing bookmarks."""
    if not _can_access_interview(iid):
        return _deny_access()
    interview = query("SELECT * FROM Interview WHERE interview_id=?", (iid,), one=True)
    if not interview:
        flash('Interview not found.', 'danger')
        return redirect(url_for('recruitment.list_interviews'))

    # Pass/Fail pre-dated scorecards and could bypass ranking, selection
    # approval, and the multi-opening offer workflow. Do not mutate data.
    flash('Pass/Fail has been retired. Complete the interview, record its scorecard, then use ranking and selection approval.', 'info')
    return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    result = request.form.get('result')
    if result not in ('Pass', 'Fail'):
        flash('Invalid result.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    # Do not allow result before the scheduled interview date
    if interview['scheduled_at']:
        from datetime import datetime as dt
        scheduled = dt.strptime(interview['scheduled_at'], '%Y-%m-%d %H:%M:%S')
        if dt.now() < scheduled:
            flash('Cannot set result before the scheduled interview date.', 'danger')
            return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    execute("UPDATE Interview SET result=?, status='Completed' WHERE interview_id=?", (result, iid))
    aid = interview['application_id']

    app_data = query("""
        SELECT ja.*, jp.title as job_title FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE ja.application_id=?
    """, (aid,), one=True)

    if result == 'Pass':
        execute("UPDATE Job_Application SET reviewed_by=?, reviewed_at=datetime('now') WHERE application_id=?", (session['user_id'], aid))
        # Auto-reject all other candidates for the same posting
        if app_data and app_data['posting_id']:
            others = query("""
                SELECT application_id, applicant_name, applicant_email, status
                FROM Job_Application
                WHERE posting_id=? AND application_id!=? AND status IN ('New','Shortlisted','Interview')
            """, (app_data['posting_id'], aid))
            for o in others:
                execute("UPDATE Job_Application SET status='Rejected', reviewed_by=?, reviewed_at=datetime('now') WHERE application_id=?", (session['user_id'], o['application_id']))
                if o['applicant_email']:
                    html = render_template('emails/application_rejected.html',
                        employee_name=o['applicant_name'],
                        title='Application Update',
                        job_title=app_data['job_title'])
                    send_email(f'Application Status – {app_data["job_title"]}', o['applicant_email'], html)
            if others:
                log_audit('AUTO_REJECT_CANDIDATES', 'Recruitment',
                          f'Auto-rejected {len(others)} candidate(s) for posting #{app_data["posting_id"]}',
                          action_details={'posting_id': app_data['posting_id'], 'passed_aid': aid, 'rejected_count': len(others)})
        flash('Candidate passed! Create a contract and send an offer to proceed.')
    else:
        execute("UPDATE Job_Application SET status='Rejected', reviewed_by=?, reviewed_at=datetime('now') WHERE application_id=?", (session['user_id'], aid))
        # Send rejection email
        if app_data and app_data['applicant_email']:
            html = render_template('emails/application_rejected.html',
                employee_name=app_data['applicant_name'],
                title='Application Update',
                job_title=app_data['job_title'],
                stage='interview')
            send_email(f'Application Status – {app_data["job_title"]}', app_data['applicant_email'], html)
        flash('Candidate failed. Status updated to Rejected.', 'success')

    log_audit('INTERVIEW_RESULT', 'Recruitment', f'Interview {iid} result: {result}')
    return redirect(url_for('recruitment.view_application', aid=aid))


# ── Interview Scorecard (fixed three criteria) ─────────────────────────────────

@recruit_bp.route('/interview/<int:iid>/complete', methods=['POST'])
@role_required('Admin', 'HR')
def complete_interview(iid):
    """Mark a completed interview without making a hiring decision."""
    if not _can_access_interview(iid):
        return _deny_access()
    interview = query("SELECT * FROM Interview WHERE interview_id=?", (iid,), one=True)
    if not interview:
        flash('Interview not found.', 'danger')
        return redirect(url_for('recruitment.list_interviews'))
    if interview['status'] not in ('Scheduled', 'Confirmed'):
        flash('Only Scheduled or Confirmed interviews can be marked completed.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))
    try:
        scheduled_at = datetime.strptime(interview['scheduled_at'], '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        flash('Interview has an invalid scheduled time.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))
    if scheduled_at > datetime.now():
        flash('An interview cannot be marked completed before its scheduled time.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    execute("UPDATE Interview SET status='Completed', result=NULL WHERE interview_id=?", (iid,))
    log_audit('COMPLETE_INTERVIEW', 'Recruitment',
              f'Interview {iid} marked completed',
              action_details={'interview_id': iid,
                              'application_id': interview['application_id']})
    flash('Interview marked completed. Record the scorecard to include it in the candidate ranking.', 'success')
    return redirect(url_for('recruitment.view_application', aid=interview['application_id']))


SCORECARD_CRITERIA = ('technical', 'communication', 'fit')


@recruit_bp.route('/interview/<int:iid>/scorecard', methods=['POST'])
@role_required('Admin', 'HR')
def interview_scorecard(iid):
    """Record the fixed three-criterion scorecard for a completed interview.

    Criteria are non-customisable 1-5 ratings, each requiring an evidence
    note. One scorecard per interview; the first recorded decision is final
    and cannot be updated. Only Admin/HR/HR Manager may record scorecards."""
    if session.get('user_role') not in ('Admin', 'HR', 'HR Manager'):
        flash('Only HR staff or Admin may record scorecards.', 'danger')
        return redirect(url_for('recruitment.list_interviews'))
    if not _can_access_interview(iid):
        return _deny_access()
    interview = query("SELECT * FROM Interview WHERE interview_id=?", (iid,), one=True)
    if not interview:
        flash('Interview not found.', 'danger')
        return redirect(url_for('recruitment.list_interviews'))

    if interview['status'] != 'Completed':
        flash('Scorecards can only be recorded for completed interviews.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    existing = query("SELECT scorecard_id FROM Interview_Scorecard WHERE interview_id=?",
                     (iid,), one=True)
    if existing:
        flash('A scorecard has already been recorded for this interview — the first decision is final.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    try:
        values = {c: int(request.form.get(c)) for c in SCORECARD_CRITERIA}
    except (TypeError, ValueError):
        flash('Each criterion must be a number from 1 to 5.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))
    if any(v < 1 or v > 5 for v in values.values()):
        flash('Each criterion must be between 1 and 5.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))
    notes = {c: (request.form.get('note_' + c) or '').strip() for c in SCORECARD_CRITERIA}
    if not all(notes.values()):
        flash('Every criterion requires an evidence note.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    execute("""INSERT INTO Interview_Scorecard
        (interview_id, technical, communication, fit,
         note_technical, note_communication, note_fit, scored_by, updated_at)
        VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
        (iid, values['technical'], values['communication'], values['fit'],
         notes['technical'], notes['communication'], notes['fit'],
         session['user_id']))
    log_audit('RECORD_SCORECARD', 'Recruitment', f'Scorecard recorded for interview {iid}',
              action_details={'interview_id': iid, 'criteria': values})
    flash('Scorecard saved.', 'success')
    return redirect(url_for('recruitment.view_application', aid=interview['application_id']))


# ── Scorecard-Based Selection Confirmation ─────────────────────────────────────

@recruit_bp.route('/applications/<int:aid>/confirm-selection', methods=['POST'])
@role_required('HR Manager')
def confirm_candidate_selection(aid):
    """Let an HR Manager directly record an approved candidate selection."""
    if not _can_access_application(aid):
        return _deny_access()
    app = query("""SELECT application_id, posting_id FROM Job_Application
                   WHERE application_id=?""", (aid,), one=True)
    if not app or not app['posting_id']:
        flash('A posted application is required before confirming selection.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))
    if not _can_access_posting(app['posting_id']):
        return _deny_access()

    scored_interview = query("""SELECT 1
                                FROM Interview_Scorecard isc
                                JOIN Interview i ON i.interview_id=isc.interview_id
                                WHERE i.application_id=? AND i.status='Completed'
                                LIMIT 1""", (aid,), one=True)
    if not scored_interview:
        flash('A completed interview scorecard is required before confirming selection.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))
    if not _is_confirmable_candidate(aid, app['posting_id']):
        flash('Only the top-scoring candidates within the posting\'s unfilled openings can be confirmed. Review the Candidate Ranking first.', 'warning')
        return redirect(url_for('recruitment.view_application', aid=aid))

    existing = query("""SELECT status FROM Candidate_Recommendation
                        WHERE application_id=? AND posting_id=?""",
                     (aid, app['posting_id']), one=True)
    if existing:
        if existing['status'] == 'Approved':
            flash('Candidate selection is already approved.', 'info')
            return redirect(url_for('recruitment.view_application', aid=aid))
        execute("""UPDATE Candidate_Recommendation
                   SET recommended_by=?, status='Approved', approved_by=?,
                       approved_at=datetime('now'), rejection_reason=NULL
                   WHERE application_id=? AND posting_id=?""",
                (session['user_id'], session['user_id'], aid, app['posting_id']))
    else:
        execute("""INSERT INTO Candidate_Recommendation
                   (posting_id, application_id, recommended_by, status, approved_by, approved_at)
                   VALUES (?,?,?,'Approved',?,datetime('now'))""",
                (app['posting_id'], aid, session['user_id'], session['user_id']))
    log_audit('DIRECT_CONFIRM_SELECTION', 'Recruitment',
              f'HR Manager directly confirmed candidate {aid} for selection',
              action_details={'posting_id': app['posting_id'], 'application_id': aid})
    flash('Candidate selection confirmed. You can now prepare the contract draft.', 'success')
    return redirect(url_for('recruitment.view_application', aid=aid))

@recruit_bp.route('/postings/<int:pid>/recommend', methods=['POST'])
@role_required('Admin', 'HR')
def recommend_candidate(pid):
    """Retired compatibility endpoint for the former manual recommendation flow."""
    if not _can_access_posting(pid):
        return _deny_access()
    flash('Selection is based on completed scorecard ranking. The HR Manager confirms the top-scoring candidate directly.', 'info')
    return redirect(url_for('recruitment.view_posting', pid=pid))


@recruit_bp.route('/postings/<int:pid>/recommendation/<int:rid>/approve', methods=['POST'])
@role_required('Admin', 'HR Manager')
def approve_recommendation(pid, rid):
    """Retired compatibility endpoint for the former approval step."""
    if not _can_access_posting(pid):
        return _deny_access()
    flash('Selection approval is no longer a separate step. The HR Manager confirms the top scorer from the Candidate Ranking.', 'info')
    return redirect(url_for('recruitment.view_posting', pid=pid))


@recruit_bp.route('/postings/<int:pid>/recommendation/<int:rid>/reject', methods=['POST'])
@role_required('Admin', 'HR Manager')
def reject_recommendation(pid, rid):
    """Retired compatibility endpoint for the former approval step."""
    if not _can_access_posting(pid):
        return _deny_access()
    flash('Selection approval is no longer a separate step. The HR Manager confirms the top scorer from the Candidate Ranking.', 'info')
    return redirect(url_for('recruitment.view_posting', pid=pid))


# ── Cancel Interview ──────────────────────────────────────────────────────────

@recruit_bp.route('/interview/<int:iid>/cancel', methods=['POST'])
@role_required('Admin', 'HR')
def cancel_interview(iid):
    if not _can_access_interview(iid):
        return _deny_access()
    interview = query("SELECT * FROM Interview WHERE interview_id=?", (iid,), one=True)
    if not interview:
        flash('Interview not found.', 'danger')
        return redirect(url_for('recruitment.list_interviews'))

    execute("UPDATE Interview SET status='Cancelled' WHERE interview_id=?", (iid,))
    log_audit('CANCEL_INTERVIEW', 'Recruitment', f'Interview {iid} cancelled')
    flash('Interview cancelled.', 'success')
    return redirect(url_for('recruitment.view_application', aid=interview['application_id']))


# ── Reschedule Interview ──────────────────────────────────────────────────────

@recruit_bp.route('/interview/<int:iid>/reschedule', methods=['POST'])
@role_required('Admin', 'HR')
def reschedule_interview(iid):
    """Reschedule a future Scheduled/Confirmed interview.

    Requires a reason; keeps the same interview record and history. The
    branch venue / meeting link are revalidated, the new slot must respect
    policy hours, weekends, interviewer leave and existing interviewer
    bookings. If no valid slot is found the original interview is left
    unchanged. Completed/Cancelled interviews cannot be rescheduled."""
    if not _can_access_interview(iid):
        return _deny_access()
    interview = query("SELECT * FROM Interview WHERE interview_id=?", (iid,), one=True)
    if not interview:
        flash('Interview not found.', 'danger')
        return redirect(url_for('recruitment.list_interviews'))
    interview = dict(interview)
    if interview['status'] not in ('Scheduled', 'Confirmed'):
        flash('Only Scheduled or Confirmed interviews can be rescheduled.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    reason = (request.form.get('reason') or '').strip()
    if not reason:
        flash('A reason is required to reschedule an interview.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    new_date = request.form.get('date', '')
    new_time = request.form.get('time', '')
    if not new_date or not new_time:
        flash('Select a new date and time.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))
    try:
        from datetime import datetime as dt
        new_scheduled_at = dt.strptime(f"{new_date} {new_time}:00", '%Y-%m-%d %H:%M:%S')
    except ValueError:
        flash('Invalid new date or time.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    if new_scheduled_at <= dt.now():
        flash('New interview time must be in the future. The next minute is valid.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    if new_scheduled_at.weekday() >= 5:
        flash('Interviews cannot be scheduled on weekends.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    co = session.get('company_id')
    policy = _get_or_create_interview_policy(co)
    day_start = dt.strptime(policy['day_start_time'], '%H:%M').time()
    day_end = dt.strptime(policy['day_end_time'], '%H:%M').time()
    if not (day_start <= new_scheduled_at.time() <= day_end):
        flash(f'New time must be within working hours ({policy["day_start_time"]}–{policy["day_end_time"]}).', 'danger')
        return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    # Revalidate the interview's venue/link context (format is unchanged).
    posting = query("""
        SELECT jp.*, b.name as branch_name FROM Job_Posting jp
        JOIN Branch b ON jp.branch_id=b.branch_id
        JOIN Job_Application ja ON ja.posting_id=jp.posting_id
        WHERE ja.application_id=?
    """, (interview['application_id'],), one=True)
    posting = dict(posting) if posting else None
    fmt = interview.get('format')
    if fmt == 'Physical':
        if not posting or not posting.get('branch_id'):
            flash('The posting has no branch, so the physical venue cannot be confirmed. Rescheduling blocked.', 'danger')
            return redirect(url_for('recruitment.view_application', aid=interview['application_id']))
        branch = query("SELECT * FROM Branch WHERE branch_id=?", (posting['branch_id'],), one=True)
        if not branch or not _branch_address_text(branch):
            flash('The posting branch has no address on file. Rescheduling blocked until an address is added.', 'danger')
            return redirect(url_for('recruitment.view_application', aid=interview['application_id']))
    elif fmt == 'Virtual':
        if not interview.get('meeting_link'):
            flash('This virtual interview has no meeting link. Rescheduling blocked.', 'danger')
            return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    # Interviewer availability: leave + existing bookings (excluding this interview).
    new_dt_str = new_scheduled_at.strftime('%Y-%m-%d %H:%M:%S')
    new_date_str = new_scheduled_at.strftime('%Y-%m-%d')
    interviewer_ids = [i.strip() for i in (interview.get('interviewer_ids') or '').split(',') if i.strip()]
    for eid in interviewer_ids:
        if _is_interviewer_on_leave(int(eid), new_date_str):
            flash('An assigned interviewer is on leave on the new date. Pick another date.', 'danger')
            return redirect(url_for('recruitment.view_application', aid=interview['application_id']))
        if _interviewer_busy(int(eid), new_scheduled_at,
                             int(interview.get('duration_min') or 60),
                             exclude_interview_id=iid):
            flash('An assigned interviewer already has an interview at the new time.', 'danger')
            return redirect(url_for('recruitment.view_application', aid=interview['application_id']))

    old_dt_str = interview['scheduled_at']
    execute("""INSERT INTO Interview_Reschedule
               (interview_id, old_scheduled_at, new_scheduled_at, reason, rescheduled_by)
               VALUES (?,?,?,?,?)""",
            (iid, old_dt_str, new_dt_str, reason, session['user_id']))
    execute("UPDATE Interview SET scheduled_at=? WHERE interview_id=?", (new_dt_str, iid))
    log_audit('RESCHEDULE_INTERVIEW', 'Recruitment',
              f'Interview {iid} rescheduled from {old_dt_str} to {new_dt_str}',
              action_details={'interview_id': iid, 'old_scheduled_at': old_dt_str,
                              'new_scheduled_at': new_dt_str, 'reason': reason,
                              'rescheduled_by': session['user_id']})

    # Send updated invitation
    app_row = query("""
        SELECT ja.applicant_name, ja.applicant_email, jp.title as job_title
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE ja.application_id=?
    """, (interview['application_id'],), one=True)
    if app_row and app_row['applicant_email']:
        display_location = _display_location(
            fmt, interview.get('location'), interview.get('venue'),
            interview.get('meeting_link'))
        html = render_template('emails/interview_scheduled.html',
            employee_name=app_row['applicant_name'],
            title='Interview Rescheduled',
            job_title=app_row['job_title'],
            interview_date=new_scheduled_at.strftime('%A, %d %B %Y'),
            interview_time=new_scheduled_at.strftime('%I:%M %p'),
            location=display_location,
            interview_type=fmt or interview.get('type') or 'Interview',
            interview_ref='INT-%d' % iid)
        send_email(f'Interview Rescheduled – {app_row["job_title"]}', app_row['applicant_email'], html)

    flash(f'Interview rescheduled to {new_date} {new_time}. History and audit recorded.', 'success')
    return redirect(url_for('recruitment.view_application', aid=interview['application_id']))


# ── Send Offer (Email) ────────────────────────────────────────────────────────


def _offer_validity_minutes():
    """Offer validity in minutes. The OFFER_EXPIRY_MINUTES override (QA, e.g.
    1 minute) is honoured ONLY in development/test environments; production
    always uses the standard 7 days. Expiry is processed server-side."""
    minutes = os.environ.get('OFFER_EXPIRY_MINUTES', '')
    dev_or_test = (os.environ.get('FLASK_ENV', '') == 'development'
                   or os.environ.get('FLASK_DEBUG', '').lower() in ('true', '1', 'yes'))
    if minutes and dev_or_test:
        try:
            v = int(minutes)
            if v >= 1:
                return v
        except (TypeError, ValueError):
            pass
    return 7 * 24 * 60


def _log_delivery(related_type, related_id, recipient, status, error=None):
    execute("""INSERT INTO Email_Delivery_Log (related_type, related_id, recipient, status, last_error)
               VALUES (?,?,?,?,?)""", (related_type, related_id, recipient, status, error))


def _active_reservation_for(application_id):
    return query("""SELECT * FROM Opening_Reservation
                    WHERE application_id=? AND status IN ('Reserved','Filled')
                    LIMIT 1""", (application_id,), one=True)


def _release_reservation(application_id, reason):
    row = _active_reservation_for(application_id)
    if row:
        execute("""UPDATE Opening_Reservation
                   SET status='Released', released_at=datetime('now'), release_reason=?
                   WHERE reservation_id=?""", (reason, row['reservation_id']))
    return row


def _openings_available(posting_id):
    jp = query("""SELECT approved_openings, filled_openings FROM Job_Posting
                  WHERE posting_id=?""", (posting_id,), one=True)
    if not jp:
        return 0
    reserved = query("""SELECT COUNT(*) as c FROM Opening_Reservation
                        WHERE posting_id=? AND status IN ('Reserved','Filled')""",
                     (posting_id,), one=True)['c']
    return max(0, int(jp['approved_openings'] or 1) - int(jp['filled_openings'] or 0) - reserved)


def process_expired_offers():
    """Server-side offer-expiry sweep. Sent contracts past their expiry become
    'Expired', their applications become 'Offer Expired', the reserved opening
    is released and HR/Admin + HR Manager are notified. Failures are isolated
    per contract. Never sends a replacement offer automatically."""
    expired = query("""
        SELECT c.contract_id, c.application_id, c.token_expires_at,
               ja.applicant_name, ja.company_id, jp.title as job_title
        FROM Contract c
        JOIN Job_Application ja ON ja.application_id=c.application_id
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE c.status='Sent' AND c.token_expires_at IS NOT NULL
          AND c.token_expires_at < datetime('now')
    """)
    processed = 0
    for c in expired:
        try:
            execute("UPDATE Contract SET status='Expired' WHERE contract_id=?", (c['contract_id'],))
            execute("""UPDATE Job_Application SET status='Offer Expired'
                       WHERE application_id=?""", (c['application_id'],))
            _release_reservation(c['application_id'], 'offer_expired')
            log_audit('OFFER_EXPIRED', 'Recruitment',
                      f'Offer {c["contract_id"]} expired (application {c["application_id"]})',
                      action_details={'contract_id': c['contract_id'],
                                      'application_id': c['application_id']})
            if c['company_id']:
                from app.notifications.routes import send_in_app_to_company
                send_in_app_to_company(
                    c['company_id'],
                    ('Admin', 'HR', 'HR Manager'),
                    'Offer Expired',
                    'Offer for %s (%s) expired with no response. Reservation released — '
                    'review the next ranked candidate.'
                    % (c['applicant_name'], c['job_title'] or 'position'),
                    type='Warning',
                    related_url='/recruitment/applications')
            processed += 1
        except Exception as e:
            print(f"[OFFER EXPIRY] Failed for contract {c['contract_id']}: {e}")
    return processed

@recruit_bp.route('/application/<int:aid>/send-offer', methods=['POST'])
@role_required('Admin', 'HR')
def send_offer(aid):
    if not _can_access_application(aid):
        return _deny_access()
    if not _approved_selection_for_application(aid):
        flash('Candidate selection must be approved before an offer can be sent.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))
    # Ensure company signature_path column exists
    try:
        execute("ALTER TABLE Company ADD COLUMN signature_path TEXT")
    except Exception:
        pass

    contract = query("""
        SELECT c.*, ja.applicant_name, ja.applicant_email, ja.posting_id, jp.title as job_title
        FROM Contract c
        JOIN Job_Application ja ON c.application_id=ja.application_id
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE c.application_id=?
    """, (aid,), one=True)
    if not contract:
        flash('No contract found. Create a contract first.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    # Normal HR requests an offer approval. Admin and HR Manager may send
    # directly once selection is approved.
    direct_sender = session.get('user_role') in ('Admin', 'HR Manager')
    if not direct_sender:
        approval = query("SELECT * FROM Offer_Approval WHERE contract_id=?",
                         (contract['contract_id'],), one=True)
        if not approval or approval['status'] != 'Approved':
            flash('The offer must be approved by an HR Manager or Admin before it can be sent.', 'danger')
            return redirect(url_for('recruitment.view_application', aid=aid))

    # Openings cap: approved pending offers cannot exceed unfilled openings.
    if contract['posting_id'] and _openings_available(contract['posting_id']) <= 0:
        flash('No openings are available for this posting. Release or fill openings before sending another offer.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    # Generate contract PDF (auto-embedded company signature)
    pdf_path = None
    try:
        from app.recruitment.contract_pdf import generate_contract_pdf
        pdf_path = generate_contract_pdf(contract['contract_id'],
                                          company_id=session.get('company_id'))
        if pdf_path:
            execute("UPDATE Contract SET contract_doc_path=? WHERE contract_id=?",
                    (pdf_path, contract['contract_id']))
    except Exception as e:
        print(f"[SEND OFFER] PDF generation failed: {e}")
        pdf_path = None

    # G26: generate a cryptographically random, expiring, single-use token and
    # persist it. The token is the only authorization for public acceptance.
    accept_token = secrets.token_urlsafe(32)
    from datetime import timedelta
    validity_min = _offer_validity_minutes()
    token_expires_at = (datetime.now() + timedelta(minutes=validity_min)).strftime('%Y-%m-%d %H:%M:%S')
    token_expires_display = (datetime.now() + timedelta(minutes=validity_min)).strftime('%d %b %Y %H:%M')
    execute("""UPDATE Contract
               SET accept_token=?, token_expires_at=?
               WHERE contract_id=?""",
            (accept_token, token_expires_at, contract['contract_id']))
    accept_url = url_for('recruitment.accept_offer', cid=contract['contract_id'],
                         token=accept_token, _external=True)

    from flask import current_app
    hr_email = current_app.config.get('MAIL_USERNAME', 'hr@smarthr.my')
    salary_val = contract['base_salary'] or 0
    html = render_template('emails/offer_letter.html',
        employee_name=contract['applicant_name'],
        title='Offer Letter',
        position=contract['position'],
        start_date=contract['start_date'],
        salary=f"{salary_val:,.2f}",
        employment_type=contract['employment_type'],
        accept_url=accept_url,
        token_expires_display=token_expires_display)

    execute("UPDATE Contract SET status='Sent' WHERE contract_id=?", (contract['contract_id'],))
    execute("UPDATE Job_Application SET status='Offered' WHERE application_id=?", (aid,))
    log_audit('SEND_OFFER', 'Recruitment', f'Offer sent for application {aid}')

    import os
    attachments = [pdf_path] if pdf_path else None
    print(f"[SEND OFFER DEBUG] contract_id={contract['contract_id']}, email={contract['applicant_email']}, "
          f"pdf_path={pdf_path}, position={contract['position']}")

    # Check if PDF file actually exists
    if pdf_path and not os.path.exists(pdf_path):
        print(f"[SEND OFFER DEBUG] PDF file missing at {pdf_path}, sending without attachment")
        pdf_path = None
        attachments = None

    success = send_email(f'Offer #{contract["contract_id"]} – {contract["position"]}',
                         contract['applicant_email'], html, attachments=attachments)
    print(f"[SEND OFFER DEBUG] send_email returned {success}")
    if success:
        # An opening is reserved only when the offer was actually delivered.
        _log_delivery('offer', contract['contract_id'], contract['applicant_email'], 'Sent')
        if not _active_reservation_for(aid) and contract['posting_id']:
            execute("""INSERT INTO Opening_Reservation
                       (posting_id, application_id, contract_id, status)
                       VALUES (?,?,?,'Reserved')""",
                    (contract['posting_id'], aid, contract['contract_id']))
        flash('Offer letter sent to candidate with contract PDF attached.', 'success')
    else:
        _log_delivery('offer', contract['contract_id'], contract['applicant_email'], 'Failed',
                      'SMTP send failed')
        try:
            from app.notifications.routes import send_in_app_to_company
            send_in_app_to_company(
                session.get('company_id'),
                ('Admin', 'HR', 'HR Manager'),
                'Offer Delivery Failed',
                'Offer for %s failed to deliver. Review the delivery log and resend.'
                % contract['applicant_name'],
                type='Warning',
                related_url='/recruitment/applications')
        except Exception as e:
            print(f"[SEND OFFER] Delivery-failure notification error: {e}")
        flash('Offer delivery failed. Check mail configuration and resend; no opening was reserved.', 'warning')

    return redirect(url_for('recruitment.view_application', aid=aid))


# ── Offer Approval (HR Manager/Admin gate before sending) ─────────────────────

@recruit_bp.route('/contract/<int:cid>/offer-approval', methods=['POST'])
@role_required('Admin', 'HR')
def request_offer_approval(cid):
    """HR/Admin/HR Manager requests approval to send an offer. Creates a Pending
    Offer_Approval; a previously Rejected request can be re-requested."""
    if session.get('user_role') not in ('Admin', 'HR', 'HR Manager'):
        flash('Only HR staff or Admin may request offer approval.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=_contract_aid(cid)))
    if not _can_access_contract(cid):
        return _deny_access()
    contract = query("SELECT * FROM Contract WHERE contract_id=?", (cid,), one=True)
    if not contract:
        flash('Contract not found.', 'danger')
        return redirect(url_for('recruitment.list_applications'))
    existing = query("SELECT * FROM Offer_Approval WHERE contract_id=?", (cid,), one=True)
    if existing and existing['status'] == 'Pending':
        flash('Offer approval is already pending review.', 'warning')
    elif existing and existing['status'] == 'Approved':
        flash('This offer is already approved.', 'info')
    else:
        execute("""INSERT INTO Offer_Approval (contract_id, status, requested_by, updated_at)
                   VALUES (?,'Pending',?,datetime('now'))
                   ON CONFLICT(contract_id) DO UPDATE SET
                       status='Pending', requested_by=excluded.requested_by,
                       approved_by=NULL, approved_at=NULL, rejection_reason=NULL,
                       updated_at=datetime('now')""",
                (cid, session['user_id']))
        log_audit('REQUEST_OFFER_APPROVAL', 'Recruitment',
                  f'Offer approval requested for contract {cid}')
        flash('Offer approval requested. HR Manager/Admin must approve before sending.', 'success')
    return redirect(url_for('recruitment.view_application', aid=contract['application_id']))


def _contract_aid(cid):
    row = query("SELECT application_id FROM Contract WHERE contract_id=?", (cid,), one=True)
    return row['application_id'] if row else 0


@recruit_bp.route('/contract/<int:cid>/offer-approval/approve', methods=['POST'])
@role_required('Admin', 'HR Manager')
def approve_offer_approval(cid):
    if not _can_access_contract(cid):
        return _deny_access()
    contract = query("SELECT * FROM Contract WHERE contract_id=?", (cid,), one=True)
    if not contract:
        flash('Contract not found.', 'danger')
        return redirect(url_for('recruitment.list_applications'))
    approval = query("SELECT * FROM Offer_Approval WHERE contract_id=?", (cid,), one=True)
    if not approval or approval['status'] != 'Pending':
        flash('No pending offer approval for this contract.', 'warning')
        return redirect(url_for('recruitment.view_application', aid=contract['application_id']))
    execute("""UPDATE Offer_Approval
               SET status='Approved', approved_by=?, approved_at=datetime('now'),
                   updated_at=datetime('now')
               WHERE approval_id=?""", (session['user_id'], approval['approval_id']))
    log_audit('APPROVE_OFFER', 'Recruitment',
              f'Offer for contract {cid} approved',
              action_details={'contract_id': cid, 'application_id': contract['application_id']})
    flash('Offer approved. HR can now send it.', 'success')
    return redirect(url_for('recruitment.view_application', aid=contract['application_id']))


@recruit_bp.route('/contract/<int:cid>/offer-approval/reject', methods=['POST'])
@role_required('Admin', 'HR Manager')
def reject_offer_approval(cid):
    if not _can_access_contract(cid):
        return _deny_access()
    contract = query("SELECT * FROM Contract WHERE contract_id=?", (cid,), one=True)
    if not contract:
        flash('Contract not found.', 'danger')
        return redirect(url_for('recruitment.list_applications'))
    approval = query("SELECT * FROM Offer_Approval WHERE contract_id=?", (cid,), one=True)
    if not approval or approval['status'] != 'Pending':
        flash('No pending offer approval for this contract.', 'warning')
        return redirect(url_for('recruitment.view_application', aid=contract['application_id']))
    reason = (request.form.get('rejection_reason') or '').strip()
    if not reason:
        flash('A reason is required to reject an offer approval.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=contract['application_id']))
    execute("""UPDATE Offer_Approval
               SET status='Rejected', approved_by=?, approved_at=datetime('now'),
                   rejection_reason=?, updated_at=datetime('now')
               WHERE approval_id=?""", (session['user_id'], reason, approval['approval_id']))
    log_audit('REJECT_OFFER', 'Recruitment',
              f'Offer for contract {cid} rejected',
              action_details={'contract_id': cid, 'reason': reason})
    flash('Offer approval rejected.', 'info')
    return redirect(url_for('recruitment.view_application', aid=contract['application_id']))

# ?? Download Resume ???????????????????????????????????????????????????????????

@recruit_bp.route('/resume/<int:aid>/download')
@login_required
def download_resume(aid):
    if not _can_access_application(aid):
        return _deny_access()
    app = query("SELECT resume_path FROM Job_Application WHERE application_id=?", (aid,), one=True)
    if not app or not app['resume_path']:
        flash('Resume not found.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    import os
    from flask import current_app, send_file
    resume_dir = os.path.join(current_app.root_path, '..', 'uploads', 'resumes')
    path = os.path.join(resume_dir, app['resume_path'])
    if not os.path.exists(path):
        flash('Resume file not found on disk.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))
    return send_file(path, as_attachment=True,
                     download_name=app['resume_path'],
                     mimetype='application/octet-stream')


# ── Download Contract PDF ─────────────────────────────────────────────────────

def _resolve_doc_path(path, *subdirs):
    """Resolve a stored document path to an existing file.

    Older rows store absolute paths (from a previous machine) and newer rows
    store bare filenames; both can break os.path.exists(). Try the stored path
    as-is, then look up the basename in the uploads subdirectories.
    """
    if not path:
        return None
    if os.path.isabs(path) and os.path.exists(path):
        return path
    candidates = [path]
    for sub in subdirs:
        candidates.append(os.path.join(current_app.root_path, '..', 'uploads',
                                        sub, os.path.basename(path)))
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None


@recruit_bp.route('/contract/<int:cid>/download')
@login_required
def download_contract(cid):
    if not _can_access_contract(cid):
        return _deny_access()
    contract = query("SELECT contract_doc_path, application_id FROM Contract WHERE contract_id=?", (cid,), one=True)
    if not contract or not contract['contract_doc_path']:
        flash('Contract PDF not found.', 'danger')
        aid = contract['application_id'] if contract else request.args.get('aid', 0)
        return redirect(url_for('recruitment.view_application', aid=aid))

    from flask import send_file
    path = _resolve_doc_path(contract['contract_doc_path'], 'contracts')
    if not path:
        flash('Contract PDF file not found on disk.', 'danger')
        aid = contract['application_id'] if contract else request.args.get('aid', 0)
        return redirect(url_for('recruitment.view_application', aid=aid))
    return send_file(path, as_attachment=True,
                     download_name=f"contract_{cid}.pdf",
                     mimetype='application/pdf')


@recruit_bp.route('/contract/<int:cid>/download-signed')
@login_required
def download_signed_contract(cid):
    if not _can_access_contract(cid):
        return _deny_access()
    contract = query("SELECT signed_doc_path, application_id FROM Contract WHERE contract_id=?", (cid,), one=True)
    if not contract or not contract['signed_doc_path']:
        flash('Signed contract PDF not found.', 'danger')
        aid = contract['application_id'] if contract else request.args.get('aid', 0)
        return redirect(url_for('recruitment.view_application', aid=aid))

    from flask import send_file
    import os
    path = _resolve_doc_path(contract['signed_doc_path'], 'signed_contracts', 'contracts')
    if not path:
        flash('Signed contract PDF file not found on disk.', 'danger')
        aid = contract['application_id'] if contract else request.args.get('aid', 0)
        return redirect(url_for('recruitment.view_application', aid=aid))
    return send_file(path, as_attachment=True,
                     download_name=f"contract_{cid}_signed.pdf",
                     mimetype='application/pdf')


# ── Accept Offer (Public, token-gated) ────────────────────────────────────────
# GET shows a confirmation page (read-only). POST performs the acceptance and
# requires BOTH the session CSRF token (enforced globally) and the valid,
# unexpired, single-use contract token. Acceptance never marks the application
# Hired and never closes the posting -- the authorized HR `hire` action does
# that after verification.

def _contract_accept_checks(cid, token):
    contract = query("""
        SELECT c.*, ja.applicant_name, ja.applicant_email, jp.title as job_title
        FROM Contract c
        JOIN Job_Application ja ON c.application_id=ja.application_id
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE c.contract_id=?
    """, (cid,), one=True)
    if not contract:
        return None, 'Offer not found.', 404
    if contract['status'] == 'Accepted':
        return None, 'This offer has already been accepted.', 400
    if contract['status'] != 'Sent':
        return None, 'This offer is not yet ready to be accepted.', 400
    if not token or not contract['accept_token'] or not secrets.compare_digest(
            contract['accept_token'], token):
        return None, 'Invalid acceptance link. Please contact HR for a new link.', 400
    if contract['token_expires_at']:
        try:
            expires = datetime.strptime(contract['token_expires_at'], '%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError):
            expires = None
        if expires and datetime.now() > expires:
            return None, 'This acceptance link has expired. Please contact HR for a new link.', 400
    return contract, None, None


@recruit_bp.route('/contract/<int:cid>/accept', methods=['GET', 'POST'])
def accept_offer(cid):
    token = request.args.get('token') or request.form.get('token')
    contract, error, status = _contract_accept_checks(cid, token)
    if error:
        return f'<h2>{error}</h2>', status

    if request.method == 'GET':
        return render_template('recruitment/accept_confirm.html',
                               contract=contract,
                               token=token,
                               expires_display=(datetime.strptime(
                                   contract['token_expires_at'], '%Y-%m-%d %H:%M:%S')
                                   .strftime('%d %b %Y') if contract['token_expires_at'] else None))

    # POST: token re-validated above; CSRF enforced by the global check.
    if request.form.get('action') == 'decline':
        execute("""UPDATE Contract SET status='Declined', accept_token=NULL
                   WHERE contract_id=?""", (cid,))
        _release_reservation(contract['application_id'], 'declined')
        log_audit('DECLINE_OFFER', 'Recruitment',
                  f'Offer {cid} declined by candidate via secure link')
        try:
            from app.notifications.routes import send_in_app_to_company
            company_row = query("""SELECT company_id FROM Job_Application
                                   WHERE application_id=?""",
                                (contract['application_id'],), one=True)
            if company_row and company_row['company_id']:
                send_in_app_to_company(
                    company_row['company_id'],
                    ('Admin', 'HR', 'HR Manager'),
                    'Offer Declined',
                    f'{contract["applicant_name"]} declined the offer for {contract["job_title"]}.',
                    type='Warning',
                    related_url='/recruitment/applications')
        except Exception as e:
            print(f"[ACCEPT OFFER] Decline notification failed: {e}")
        return render_template('recruitment/offer_accepted.html',
                               applicant_name=contract['applicant_name'],
                               job_title=contract['job_title'],
                               application_id=contract['application_id'],
                               declined=True)

    signed_doc_path = None
    signed_file = request.files.get('signed_doc')
    if signed_file and signed_file.filename:
        import uuid
        ext = os.path.splitext(signed_file.filename)[1] or '.pdf'
        filename = f"signed_{uuid.uuid4().hex}{ext}"
        signed_dir = os.path.join(current_app.root_path, '..', 'uploads', 'signed_contracts')
        os.makedirs(signed_dir, exist_ok=True)
        signed_file.save(os.path.join(signed_dir, filename))
        signed_doc_path = filename

    execute("""UPDATE Contract
               SET status='Accepted', accepted_at=datetime('now'), accept_token=NULL,
                   signed_doc_path=COALESCE(?, signed_doc_path),
                   signed_at=COALESCE(?, signed_at)
               WHERE contract_id=?""",
            (signed_doc_path, datetime.now().strftime('%Y-%m-%d %H:%M:%S') if signed_doc_path else None,
             cid))
    log_audit('ACCEPT_OFFER', 'Recruitment',
              f'Offer {cid} accepted by candidate via secure link'
              + (' with signed document attached' if signed_doc_path else ''))

    try:
        from app.notifications.routes import send_in_app_to_company
        company_row = query("""SELECT company_id FROM Job_Application
                               WHERE application_id=?""",
                            (contract['application_id'],), one=True)
        if company_row and company_row['company_id']:
            send_in_app_to_company(
                company_row['company_id'],
                ('Admin', 'HR', 'HR Manager'),
                'Offer Accepted',
                f'{contract["applicant_name"]} accepted the offer for {contract["job_title"]}.',
                type='Offer',
                related_url='/recruitment/applications')
    except Exception as e:
        print(f"[ACCEPT OFFER] Notification failed: {e}")

    return render_template('recruitment/offer_accepted.html',
                           applicant_name=contract['applicant_name'],
                           job_title=contract['job_title'],
                           application_id=contract['application_id'])


# ── Accept Offer (HR) ─────────────────────────────────────────────────────────

@recruit_bp.route('/application/<int:aid>/accept-offer', methods=['POST'])
@role_required('Admin', 'HR')
def accept_offer_hr(aid):
    if not _can_access_application(aid):
        return _deny_access()
    contract = query("SELECT * FROM Contract WHERE application_id=?", (aid,), one=True)
    if not contract:
        flash('No contract found.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    # Acceptance marks the contract accepted and keeps the reserved opening;
    # the candidate is NOT hired until HR Manager/Admin confirms the hire.
    execute("UPDATE Contract SET status='Accepted' WHERE contract_id=?", (contract['contract_id'],))
    if not _active_reservation_for(aid):
        app_row = query("SELECT posting_id, company_id FROM Job_Application WHERE application_id=?", (aid,), one=True)
        if app_row and app_row['posting_id']:
            execute("""INSERT INTO Opening_Reservation (posting_id, application_id, contract_id, status)
                       VALUES (?,?,?,'Reserved')""",
                    (app_row['posting_id'], aid, contract['contract_id']))
    log_audit('ACCEPT_OFFER_HR', 'Recruitment', f'Offer accepted by HR for application {aid}')
    try:
        from app.notifications.routes import send_in_app_to_company
        app_data = query("""SELECT ja.applicant_name, ja.company_id, jp.title as job_title
                            FROM Job_Application ja
                            LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
                            WHERE ja.application_id=?""", (aid,), one=True)
        if app_data and app_data['company_id']:
            send_in_app_to_company(
                app_data['company_id'],
                ('Admin', 'HR', 'HR Manager'),
                'Offer Accepted (HR)',
                f'Offer accepted by HR for {app_data["applicant_name"]} – {app_data["job_title"]}',
                type='Offer',
                related_url='/recruitment/applications')
    except Exception as e:
        print(f"[ACCEPT OFFER HR] Notification failed: {e}")
    flash('Offer accepted. You can now proceed to add the employee.', 'success')
    return redirect(url_for('recruitment.view_application', aid=aid))


# ── Verify Signed Contract (HR checks signed PDF) ────────────────────────────

@recruit_bp.route('/application/<int:aid>/verify-signed-contract', methods=['POST'])
@role_required('Admin', 'HR')
def verify_signed_contract(aid):
    if not _can_access_application(aid):
        return _deny_access()
    contract = query("SELECT * FROM Contract WHERE application_id=?", (aid,), one=True)
    if not contract:
        flash('No contract found.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))
    if contract['status'] != 'Signed':
        flash('Contract must be in Signed status to verify.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))
    if not contract['signed_doc_path']:
        flash('No signed PDF document found. Ask the candidate to re-submit.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    execute("UPDATE Contract SET status='Accepted' WHERE contract_id=?", (contract['contract_id'],))
    if not _active_reservation_for(aid):
        app_row = query("SELECT posting_id FROM Job_Application WHERE application_id=?", (aid,), one=True)
        if app_row and app_row['posting_id']:
            execute("""INSERT INTO Opening_Reservation (posting_id, application_id, contract_id, status)
                       VALUES (?,?,?,'Reserved')""",
                    (app_row['posting_id'], aid, contract['contract_id']))
    log_audit('VERIFY_SIGNED_CONTRACT', 'Recruitment',
              f'Signed contract {contract["contract_id"]} verified and accepted for application {aid}')

    try:
        from app.notifications.routes import send_in_app_to_company
        app_data = query("""SELECT ja.applicant_name, ja.company_id, jp.title as job_title
                            FROM Job_Application ja
                            LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
                            WHERE ja.application_id=?""", (aid,), one=True)
        if app_data and app_data['company_id']:
            send_in_app_to_company(
                app_data['company_id'],
                ('Admin', 'HR', 'HR Manager'),
                'Signed Contract Verified',
                f'Signed contract verified for {app_data["applicant_name"]} – {app_data["job_title"]}',
                type='Offer',
                related_url='/recruitment/applications')
    except Exception as e:
        print(f"[VERIFY SIGNED CONTRACT] Notification failed: {e}")

    # Serve the signed PDF for download as confirmation
    sig_path = _resolve_doc_path(contract['signed_doc_path'], 'signed_contracts', 'contracts')
    if sig_path:
        from flask import send_file
        return send_file(sig_path, as_attachment=True,
                         download_name=f"signed_contract_{contract['contract_id']}.pdf",
                         mimetype='application/pdf')

    flash('Signed contract verified and accepted.', 'success')
    return redirect(url_for('recruitment.view_application', aid=aid))


# ── Hire → Add Employee (Pre-filled) ─────────────────────────────────────────

@recruit_bp.route('/application/<int:aid>/hire')
@role_required('Admin', 'HR Manager')
def hire(aid):
    """HR Manager/Admin confirms the hire: the candidate becomes Hired and
    one approved opening is filled (posting becomes Partially Filled/Filled;
    remaining active candidates are rejected only when all openings are
    filled)."""
    if not _can_access_application(aid):
        return _deny_access()
    app_data = dict(query("""
        SELECT ja.*, jp.branch_id, jp.department_id as posting_dept_id,
               jp.position_id as posting_position_id, jp.title as job_title
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE ja.application_id=?
    """, (aid,), one=True))
    if not app_data:
        flash('Application not found.', 'danger')
        return redirect(url_for('recruitment.list_applications'))

    contract = dict(query("SELECT * FROM Contract WHERE application_id=?", (aid,), one=True) or {})
    if not contract or contract.get('status') not in ('Accepted', 'Signed'):
        flash('Contract must be signed by candidate before hiring.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))
    if contract['status'] == 'Signed':
        execute("UPDATE Contract SET status='Accepted' WHERE contract_id=?", (contract['contract_id'],))
        log_audit('HIRE_AUTO_ACCEPT', 'Recruitment',
                  f'Contract {contract["contract_id"]} auto-accepted on hire for application {aid}')

    # Resolve branch_id from contract's department if posting has none
    branch_id = app_data['branch_id'] or None
    if not branch_id and contract['department_id']:
        dept = query("SELECT branch_id FROM Department WHERE department_id=?", (contract['department_id'],), one=True)
        branch_id = dept['branch_id'] if dept else None

    # Hire confirmation: candidate becomes Hired, one opening is filled.
    execute("UPDATE Job_Application SET status='Hired', reviewed_by=?, reviewed_at=datetime('now') WHERE application_id=?",
            (session['user_id'], aid))
    close_job_posting_for_application(aid)

    import json, re

    # ── Internal application: promote the existing employee (no new record) ──
    if app_data.get('applicant_type') == 'Internal' and app_data.get('internal_employee_id'):
        emp = query("SELECT * FROM Employee WHERE employee_id=? AND company_id=? AND is_active=1",
                    (app_data['internal_employee_id'], app_data['company_id']), one=True)
        if emp:
            contract_position = (contract.get('position') or '').strip()
            job_title = (app_data.get('job_title') or '').strip()
            position_id = (app_data.get('posting_position_id')
                           if not contract_position or contract_position.casefold() == job_title.casefold()
                           else '')
            dept_id = contract['department_id'] or app_data['posting_dept_id']
            execute("""
                UPDATE Employee SET
                    branch_id=?, department_id=?, position=?, position_id=?,
                    base_salary=?, employment_type=?, employment_status='Active'
                WHERE employee_id=?
            """, (branch_id, dept_id, contract_position or job_title,
                  position_id or None,
                  contract['base_salary'] or emp['base_salary'],
                  contract['employment_type'] or emp['employment_type'],
                  emp['employee_id']))
            execute("UPDATE Contract SET employee_id=? WHERE contract_id=?",
                    (emp['employee_id'], contract['contract_id']))
            log_audit('HIRE_INTERNAL', 'Recruitment',
                      f'Internal hire: employee #{emp["employee_id"]} updated with new '
                      f'branch/department/position for application {aid}',
                      action_details={'application_id': aid,
                                      'employee_id': emp['employee_id'],
                                      'branch_id': branch_id, 'department_id': dept_id})
            flash(f'Internal hire: {emp["full_name"]} updated with the new branch, department and position.', 'success')
            return redirect(url_for('employees.view_employee', emp_id=emp['employee_id']))
        flash('Internal applicant employee record not found; adding as a new employee instead.', 'warning')

    sanitized = re.sub(r'[^a-z0-9.]', '', app_data['applicant_name'].lower().replace(' ', '.'))
    smarthr_email = f'{sanitized}@smarthr.my'
    date_of_birth, gender = _mykad_birth_and_gender(app_data.get('applicant_ic'))
    contract_position = (contract.get('position') or '').strip()
    job_title = (app_data.get('job_title') or '').strip()
    position_id = (app_data.get('posting_position_id')
                   if not contract_position or contract_position.casefold() == job_title.casefold()
                   else '')
    session['hire_prefill'] = json.dumps({
        'full_name': app_data['applicant_name'],
        'email': smarthr_email,
        'personal_email': app_data['applicant_email'],
        'ic_number': app_data.get('applicant_ic') or '',
        'date_of_birth': date_of_birth,
        'gender': gender,
        'contact_no': app_data['applicant_phone'] or '',
        'address': app_data.get('applicant_address') or '',
        'emergency_contact_name': app_data.get('emergency_contact_name') or '',
        'emergency_contact_no': app_data.get('emergency_contact_no') or '',
        'position_id': position_id,
        'position': contract_position or job_title,
        'department_id': contract['department_id'] or app_data['posting_dept_id'],
        'branch_id': branch_id,
        'employment_type': contract['employment_type'] or 'Full-Time',
        'base_salary': str(contract['base_salary'] or 0),
        'hire_date': contract['start_date'] or '',
        'work_start_time': contract['work_start_time'] or '09:00',
        'work_end_time': contract['work_end_time'] or '18:00',
        'contract_id': contract['contract_id'],
    })
    # Opening accounting is handled by close_job_posting_for_application above.
    return redirect(url_for('employees.add_employee', from_hire=1))


# ── Vacancy Request (Manager submits) ─────────────────────────────────────────

@recruit_bp.route('/vacancy-request', methods=['GET', 'POST'])
@login_required
def vacancy_request():
    role = session.get('user_role')
    is_dept_mgr = session.get('is_dept_manager', False)
    managed_dept_id = session.get('managed_dept_id')
    if role not in ('Admin', 'Manager', 'HR Manager') and not is_dept_mgr:
        flash('You do not have permission to access that page.', 'danger')
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        f = request.form
        emp_type = f.get('employment_type', '')
        norm = {'Full-time': 'Full-Time', 'Part-time': 'Part-Time',
                'Full Time': 'Full-Time', 'Part Time': 'Part-Time',
                'fulltime': 'Full-Time', 'parttime': 'Part-Time'}
        emp_type = norm.get(emp_type, emp_type)
        if emp_type not in ('Full-Time', 'Part-Time', 'Contract'):
            flash('Select a valid employment type.', 'danger')
            return redirect(url_for('recruitment.vacancy_request'))

        audience = f.get('target_audience', 'Both')
        if not _is_valid_audience(audience):
            flash('Invalid audience selection.', 'danger')
            return redirect(url_for('recruitment.vacancy_request'))

        # ── Server-side guard: never trust the dropdown for authorization ──
        try:
            dept_id = int(f['department_id'])
        except (TypeError, ValueError):
            flash('Please select a department.', 'danger')
            return redirect(url_for('recruitment.vacancy_request'))
        co = session.get('company_id')
        dept_row = query("""SELECT d.department_id, d.branch_id, b.company_id
                            FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
                            WHERE d.department_id=?""", (dept_id,), one=True)
        if role in ('Admin', 'HR', 'HR Manager'):
            # HR roles may pick any department OF THEIR OWN COMPANY only (C20)
            if not dept_row or dept_row['company_id'] != co:
                flash('Selected department does not belong to your company.', 'danger')
                return redirect(url_for('recruitment.vacancy_request'))
        elif is_dept_mgr and managed_dept_id:
            if dept_id != managed_dept_id:
                flash('You may only request positions for your own department.', 'danger')
                return redirect(url_for('recruitment.vacancy_request'))
        else:
            if not dept_row or dept_row['branch_id'] != session.get('branch_id'):
                flash('You may only request positions for your own branch.', 'danger')
                return redirect(url_for('recruitment.vacancy_request'))

        # ── Position: catalog entry OR custom title (HR reviews custom) ──
        position_id = f.get('position_id') or ''
        is_custom = 0
        if position_id and position_id != '__custom__':
            try:
                position_id = int(position_id)
            except (TypeError, ValueError):
                flash('Invalid position selected for this department.', 'danger')
                return redirect(url_for('recruitment.vacancy_request'))
            pos = query("SELECT * FROM Position WHERE position_id=? AND is_active=1",
                        (position_id,), one=True)
            if not pos or pos['department_id'] != dept_id:
                flash('Invalid position selected for this department.', 'danger')
                return redirect(url_for('recruitment.vacancy_request'))
            position_title = pos['position_name']
        else:
            custom_title = f.get('position_title', '').strip()
            if not custom_title:
                flash('Please select a position or enter a custom title.', 'danger')
                return redirect(url_for('recruitment.vacancy_request'))
            position_title = custom_title
            is_custom = 1
            position_id = None

        # ── Number of openings requested ──
        try:
            requested_openings = int(f.get('requested_openings') or 1)
        except (TypeError, ValueError):
            requested_openings = 0
        if requested_openings < 1 or requested_openings > 50:
            flash('Number of openings must be between 1 and 50.', 'danger')
            return redirect(url_for('recruitment.vacancy_request'))

        try:
            min_salary = float(f['min_salary']) if f.get('min_salary') else None
            max_salary = float(f['max_salary']) if f.get('max_salary') else None
        except (TypeError, ValueError):
            flash('Salary values must be valid numbers.', 'danger')
            return redirect(url_for('recruitment.vacancy_request'))
        if ((min_salary is not None and min_salary < 0)
                or (max_salary is not None and max_salary < 0)):
            flash('Salary values cannot be negative.', 'danger')
            return redirect(url_for('recruitment.vacancy_request'))
        if min_salary is not None and max_salary is not None and min_salary > max_salary:
            flash('Minimum salary cannot exceed maximum salary.', 'danger')
            return redirect(url_for('recruitment.vacancy_request'))

        request_id = execute("""INSERT INTO Vacancy_Request
                   (requested_by, department_id, position_title, position_id, is_custom,
                    employment_type, min_salary, max_salary, description, requirements, reason,
                    target_audience, requested_openings, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'Pending')""",
                (session['user_id'], dept_id, position_title, position_id, is_custom,
                emp_type,
                 min_salary, max_salary,
                 f.get('description', ''), f.get('requirements', ''),
                 f.get('reason', ''), audience, requested_openings))
        log_audit('SUBMIT_VACANCY', 'Recruitment', f'Vacancy request submitted: {position_title}',
                  action_details={'department_id': dept_id, 'is_custom': is_custom,
                                  'target_audience': audience, 'request_id': request_id,
                                  'requested_openings': requested_openings})

        # Notify eligible reviewers in-app: same company only, requester
        # excluded, per-recipient failure isolation (P12).
        try:
            from app.notifications.routes import send_in_app_to_company
            dept_name = query("SELECT department_name FROM Department WHERE department_id=?",
                              (dept_id,), one=True)['department_name']
            send_in_app_to_company(
                co,
                ('Admin', 'HR', 'HR Manager'),
                'New Job Posting Request',
                f"{session.get('user_name', 'An employee')} requested a {position_title} job posting for {dept_name}.",
                type='Info',
                related_url=url_for('recruitment.view_vacancy_request', rid=request_id),
                exclude_employee_id=session.get('user_id'))
        except Exception as e:
            print(f"[VACANCY NOTIFY] Failed: {e}")

        flash('Vacancy request submitted for review.', 'success')
        if role in ('Admin', 'HR', 'HR Manager'):
            return redirect(url_for('recruitment.list_vacancy_requests'))
        return redirect(url_for('recruitment.my_vacancy_requests'))

    branch_id = session.get('branch_id')

    # Dept manager: only show their managed department
    if is_dept_mgr and managed_dept_id and role not in ('Admin', 'HR Manager'):
        departments = query("""
            SELECT d.*, b.name as branch_name
            FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
            WHERE d.department_id = ?
        """, (managed_dept_id,))
    elif role in ('Admin', 'HR', 'HR Manager'):
        departments = query("""
            SELECT d.*, b.name as branch_name
            FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
            WHERE b.company_id=?
            ORDER BY d.department_name
        """, (session.get('company_id'),))
    else:
        departments = query("""
            SELECT d.*, b.name as branch_name
            FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
            WHERE d.branch_id=?
            ORDER BY d.department_name
        """, (branch_id,))

    dept_ids = [d['department_id'] for d in departments]
    if dept_ids:
        ph = ','.join('?' * len(dept_ids))
        positions = query(f"""
            SELECT * FROM Position
            WHERE department_id IN ({ph}) AND is_active=1
            ORDER BY position_name
        """, dept_ids)
    else:
        positions = []
    return render_template('recruitment/vacancy_request.html',
                           departments=departments, positions=positions,
                           audience_values=AUDIENCE_VALUES)


@recruit_bp.route('/vacancy-requests')
@login_required
def list_vacancy_requests():
    """Job posting requests review page — Admin/HR only. Managers use my_vacancy_requests."""
    role = session['user_role']
    if role not in ('Admin', 'HR', 'HR Manager'):
        if role == 'Manager' or session.get('is_dept_manager'):
            return redirect(url_for('recruitment.my_vacancy_requests'))
        return _deny_access()

    status_filter = request.args.get('status', 'active')
    if status_filter not in ('active', 'completed', 'all'):
        status_filter = 'active'
    status_clause = {
        'active': "AND vr.status='Pending'",
        'completed': "AND vr.status IN ('Approved','Rejected')",
        'all': '',
    }[status_filter]
    requests = query("""
        SELECT vr.*, d.department_name, e.full_name as requester_name
        FROM Vacancy_Request vr
        JOIN Department d ON vr.department_id=d.department_id
        JOIN Branch b ON d.branch_id=b.branch_id
        JOIN Employee e ON vr.requested_by=e.employee_id
        WHERE b.company_id=?
        %s
        ORDER BY vr.created_at DESC
    """ % status_clause, (session.get('company_id'),))
    return render_template('recruitment/vacancy_requests.html', requests=requests,
                           my_requests=False, status_filter=status_filter)


@recruit_bp.route('/my-requests')
@login_required
def my_vacancy_requests():
    """Job posting requests submitted by the current user (managers/dept managers)."""
    role = session['user_role']
    if role not in ('Admin', 'Manager', 'HR Manager') and not session.get('is_dept_manager'):
        return _deny_access()
    uid = session['user_id']
    status_filter = request.args.get('status', 'active')
    if status_filter not in ('active', 'completed', 'all'):
        status_filter = 'active'
    status_clause = {
        'active': "AND vr.status='Pending'",
        'completed': "AND vr.status IN ('Approved','Rejected')",
        'all': '',
    }[status_filter]
    requests = query("""
        SELECT vr.*, d.department_name, e.full_name as requester_name
        FROM Vacancy_Request vr
        JOIN Department d ON vr.department_id=d.department_id
        JOIN Employee e ON vr.requested_by=e.employee_id
        WHERE vr.requested_by=?
        %s
        ORDER BY vr.created_at DESC
    """ % status_clause, (uid,))
    return render_template('recruitment/vacancy_requests.html', requests=requests,
                           my_requests=True, status_filter=status_filter)


@recruit_bp.route('/vacancy-request/<int:rid>')
@login_required
def view_vacancy_request(rid):
    req = query("""
        SELECT vr.*, d.department_name, e.full_name as requester_name,
               rev.full_name as reviewer_name
        FROM Vacancy_Request vr
        JOIN Department d ON vr.department_id=d.department_id
        JOIN Employee e ON vr.requested_by=e.employee_id
        LEFT JOIN Employee rev ON vr.reviewed_by=rev.employee_id
        WHERE vr.request_id=?
    """, (rid,), one=True)
    if not req:
        flash('Vacancy request not found.', 'danger')
        return redirect(url_for('recruitment.list_vacancy_requests'))

    if req['requested_by'] != session['user_id'] and not _can_access_vacancy_request(rid):
        flash('Access denied.', 'danger')
        return redirect(url_for('recruitment.list_vacancy_requests'))

    return render_template('recruitment/vacancy_request_detail.html', req=req)


@recruit_bp.route('/vacancy-request/<int:rid>/approve', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager')
def approve_vacancy(rid):
    if not _can_access_vacancy_request(rid):
        return _deny_access()
    uid = session['user_id']
    req = query("SELECT * FROM Vacancy_Request WHERE request_id=?", (rid,), one=True)
    if not req:
        flash('Vacancy request not found.', 'danger')
        return redirect(url_for('recruitment.list_vacancy_requests'))
    if req['status'] != 'Pending':
        flash('This request has already been reviewed.', 'warning')
        return redirect(url_for('recruitment.view_vacancy_request', rid=rid))

    # The posting branch is derived automatically from the request's
    # department (a department always belongs to one branch) - HR only
    # approves or rejects, no branch selection needed.
    dept_row = query("SELECT branch_id FROM Department WHERE department_id=?",
                     (req['department_id'],), one=True)
    if not dept_row or not dept_row['branch_id']:
        flash('The request department has no branch. Add the department to a branch first.', 'danger')
        return redirect(url_for('recruitment.view_vacancy_request', rid=rid))
    branch_id = dept_row['branch_id']

    audience = req['target_audience']

    # Custom positions: HR decides whether to promote the title into the catalog
    posting_position_id = req['position_id']
    if req['is_custom'] and request.form.get('add_to_catalog') == '1':
        name = ' '.join(req['position_title'].split())
        before = query("""SELECT position_id FROM Position
                          WHERE department_id=? AND LOWER(position_name)=LOWER(?)""",
                       (req['department_id'], name), one=True)
        execute("INSERT OR IGNORE INTO Position(position_name, department_id) VALUES(?,?)",
                (name, req['department_id']))
        pos = query("""SELECT position_id FROM Position
                       WHERE department_id=? AND LOWER(position_name)=LOWER(?)""",
                    (req['department_id'], name), one=True)
        posting_position_id = pos['position_id'] if pos else None
        execute("""UPDATE Vacancy_Request SET position_id=?, is_custom=0 WHERE request_id=?""",
                (posting_position_id, rid))
        if not before:
            log_audit('CREATE_POSITION', 'Organization',
                      f'Custom title "{name}" promoted into catalog from vacancy request {rid}',
                      target_table='Position', target_record_id=posting_position_id,
                      action_details={'position_name': name,
                                      'department_id': req['department_id'],
                                      'request_id': rid})

    approved_openings = int(req['requested_openings'] or 1)
    posting_id = execute("""INSERT INTO Job_Posting
        (title, department_id, branch_id, employment_type, position_id,
         min_salary, max_salary, description, requirements,
         target_audience, posted_by, status, approved_openings)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,'Open',?)""",
        (req['position_title'], req['department_id'], branch_id,
         req['employment_type'], posting_position_id,
         req['min_salary'], req['max_salary'],
         req['description'], req['requirements'],
         audience, uid, approved_openings))

    execute("""UPDATE Vacancy_Request
               SET status='Approved', reviewed_by=?, reviewed_at=datetime('now'),
                   posting_id=?, approved_openings=?
               WHERE request_id=?""",
            (uid, posting_id, approved_openings, rid))

    log_audit('APPROVE_VACANCY', 'Recruitment',
              f'Vacancy request {rid} approved, posting {posting_id} created',
              action_details={'request_id': rid, 'posting_id': posting_id,
                              'is_custom': req['is_custom'],
                              'position_title': req['position_title'],
                              'approved_openings': approved_openings})

    from app.notifications.routes import send_notification
    send_notification(
        req['requested_by'],
        'Vacancy Request Approved',
        f'Your request for {req["position_title"]} has been approved. The job posting is now live.',
        'Success',
        related_url=url_for('recruitment.view_posting', pid=posting_id)
    )

    flash('Vacancy approved and job posting created!', 'success')
    return redirect(url_for('recruitment.view_posting', pid=posting_id))


@recruit_bp.route('/vacancy-request/<int:rid>/reject', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager')
def reject_vacancy(rid):
    if not _can_access_vacancy_request(rid):
        return _deny_access()
    uid = session['user_id']
    req = query("SELECT * FROM Vacancy_Request WHERE request_id=?", (rid,), one=True)
    if not req:
        flash('Vacancy request not found.', 'danger')
        return redirect(url_for('recruitment.list_vacancy_requests'))
    if req['status'] != 'Pending':
        flash('This request has already been reviewed.', 'warning')
        return redirect(url_for('recruitment.view_vacancy_request', rid=rid))

    rejection_reason = request.form.get('rejection_reason', '').strip()
    if not rejection_reason:
        flash('Please provide a reason for rejection.', 'danger')
        return redirect(url_for('recruitment.view_vacancy_request', rid=rid))

    execute("""UPDATE Vacancy_Request
               SET status='Rejected', reviewed_by=?, reviewed_at=datetime('now'), rejection_reason=?
               WHERE request_id=?""",
            (uid, rejection_reason, rid))

    log_audit('REJECT_VACANCY', 'Recruitment',
              f'Vacancy request {rid} rejected: {rejection_reason[:50]}',
              action_details={'request_id': rid, 'reason': rejection_reason,
                              'is_custom': req['is_custom'],
                              'position_title': req['position_title']})

    from app.notifications.routes import send_notification
    send_notification(
        req['requested_by'],
        'Vacancy Request Rejected',
        f'Your request for {req["position_title"]} was rejected. Reason: {rejection_reason}',
        'Info',
        related_url=url_for('recruitment.view_vacancy_request', rid=rid)
    )

    # Send email to the manager who requested
    manager_info = query(
        "SELECT full_name, personal_email FROM Employee WHERE employee_id=?",
        (req['requested_by'],), one=True
    )
    if manager_info and manager_info['personal_email']:
        email_html = render_template('emails/vacancy_request_rejected.html',
            employee_name=manager_info['full_name'],
            title='Vacancy Request Update',
            position_title=req['position_title'],
            rejection_reason=rejection_reason)
        send_email(f'Vacancy Request Rejected – {req["position_title"]}',
                   manager_info['personal_email'], email_html)

    flash('Vacancy request rejected.', 'info')
    return redirect(url_for('recruitment.list_vacancy_requests'))


@recruit_bp.route('/_positions-for-dept/<int:dept_id>')
@login_required
def _positions_for_dept(dept_id):
    dept = query("""SELECT d.department_id, b.company_id
                    FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
                    WHERE d.department_id=?""", (dept_id,), one=True)
    if not dept or dept['company_id'] != session.get('company_id'):
        return jsonify([])
    positions = query("""
        SELECT position_id, position_name, is_department_manager_position FROM Position
        WHERE department_id=? AND is_active=1
        ORDER BY position_name
    """, (dept_id,))
    return jsonify([dict(p) for p in positions])


# ── Bulk Interview Scheduling ─────────────────────────────────────────────────

@recruit_bp.route('/bulk-schedule', methods=['GET', 'POST'])
@role_required('Admin', 'HR')
def bulk_schedule():
    if session.get('user_role') == 'HR Manager':
        flash('You do not have permission to access that page.', 'danger')
        return redirect(url_for('main.dashboard'))
    if request.method == 'POST':
        f = request.form
        try:
            posting_id = int(f.get('posting_id', ''))
            base_dt = datetime.strptime(
                f"{f.get('date', '')} {f.get('start_time', '')}", '%Y-%m-%d %H:%M')
            duration = int(f.get('duration', 30))
            interviewer_id_list = [int(eid) for eid in f.getlist('interviewer_ids')
                                   if str(eid).strip()]
        except (TypeError, ValueError):
            flash('Select a valid posting, date, start time, duration, and interviewer list.', 'danger')
            return redirect(url_for('recruitment.bulk_schedule'))
        if not _can_access_posting(posting_id):
            return _deny_access()
        if base_dt <= datetime.now():
            flash('Interview time must be in the future. The next minute is valid.', 'danger')
            return redirect(url_for('recruitment.bulk_schedule'))
        if base_dt.weekday() >= 5:
            flash('Interviews cannot be scheduled on weekends.', 'danger')
            return redirect(url_for('recruitment.bulk_schedule'))
        if duration < 1:
            flash('Interview duration must be at least one minute.', 'danger')
            return redirect(url_for('recruitment.bulk_schedule'))
        policy = _get_or_create_interview_policy(session.get('company_id'))
        try:
            policy_start = datetime.strptime(policy['day_start_time'], '%H:%M').time()
            policy_end = datetime.strptime(policy['day_end_time'], '%H:%M').time()
        except (TypeError, ValueError):
            flash('Interview policy working hours are invalid. Ask an administrator to correct the policy.', 'danger')
            return redirect(url_for('recruitment.bulk_schedule'))
        if not (policy_start <= base_dt.time() <= policy_end):
            flash(f'Interview time must be within working hours ({policy["day_start_time"]}â€“{policy["day_end_time"]}).', 'danger')
            return redirect(url_for('recruitment.bulk_schedule'))
# Resolve the format first: the eligible interviewer pool depends on it.
        posting = query("""SELECT jp.*, b.name as branch_name FROM Job_Posting jp
                           JOIN Branch b ON jp.branch_id=b.branch_id
                           WHERE jp.posting_id=?""", (posting_id,), one=True)
        fmt_result = _resolve_interview_format(f, posting)
        if fmt_result[0] is None:
            flash(fmt_result[1], 'danger')
            return redirect(url_for('recruitment.bulk_schedule'))
        fmt, meeting_link, location, venue, posting_branch_id = fmt_result

        posting_branch = posting['branch_id'] if posting else None
        requester = _requester_for_posting(posting_id, session.get('company_id'))
        pool = _get_eligible_interviewers(
            session.get('company_id'), posting_branch, physical_only=(fmt == 'Physical'),
            requester_id=requester['employee_id'] if requester else None)

        if fmt == 'Physical' and not pool:
            flash('This branch has no eligible local interviewers. Schedule as Virtual instead.', 'danger')
            return redirect(url_for('recruitment.bulk_schedule'))

        if interviewer_id_list:
            placeholders = ','.join('?' for _ in interviewer_id_list)
            valid_interviewers = query(
                f"""SELECT employee_id FROM Employee
                     WHERE employee_id IN ({placeholders}) AND company_id=? AND is_active=1""",
                tuple(interviewer_id_list) + (session.get('company_id'),))
            if len(valid_interviewers) != len(set(interviewer_id_list)):
                flash('Select active interviewers from your company.', 'danger')
                return redirect(url_for('recruitment.bulk_schedule'))
            pool_ids = {p['employee_id'] for p in pool}
            if not set(interviewer_id_list) <= pool_ids:
                if fmt == 'Physical':
                    flash('For physical interviews, only interviewers from the posting branch can be selected.', 'danger')
                else:
                    flash('Select eligible interviewers for this interview.', 'danger')
                return redirect(url_for('recruitment.bulk_schedule'))
        date = base_dt.date().isoformat()
        interviewer_ids = ','.join(str(eid) for eid in interviewer_id_list)
        uid = session['user_id']

        candidates = query("""
            SELECT ja.application_id, ja.applicant_name, ja.applicant_email, jp.title as job_title
            FROM Job_Application ja
            LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
            WHERE ja.posting_id=? AND ja.status='Shortlisted'
        """, (posting_id,))

        if not candidates:
            flash('No shortlisted candidates found for this posting.', 'warning')
            return redirect(url_for('recruitment.bulk_schedule'))

        from datetime import timedelta
        scheduled_count = 0
        email_failures = 0
        skipped_conflicts = 0

        for i, c in enumerate(candidates):
            slot = base_dt + timedelta(minutes=i * duration)
            scheduled_at = slot.strftime('%Y-%m-%d %H:%M:%S')

            # Panel availability: every selected interviewer must be free.
            busy = False
            for eid in interviewer_id_list:
                if _interviewer_busy(eid, slot, duration):
                    busy = True
                    break
            if busy:
                skipped_conflicts += 1
                continue

            interview_id = execute("""INSERT INTO Interview
                (application_id, scheduled_at, duration_min, location, meeting_link, type,
                 format, venue, posting_branch_id, status, interviewer_ids)
                VALUES (?,?,?,?,?,?,?,?,?,'Scheduled',?)""",
                (c['application_id'], scheduled_at, duration, location, meeting_link,
                 'In-Person' if fmt == 'Physical' else 'Online',
                 fmt, venue, posting_branch_id, interviewer_ids))

            execute("UPDATE Job_Application SET status='Interview' WHERE application_id=?", (c['application_id'],))

            dt_obj = slot
            html = render_template('emails/interview_scheduled.html',
                employee_name=c['applicant_name'],
                title='Interview Scheduled',
                job_title=c['job_title'],
                interview_date=dt_obj.strftime('%A, %d %B %Y'),
                interview_time=dt_obj.strftime('%I:%M %p'),
                location=_display_location(fmt, location, venue, meeting_link),
                interview_type=fmt,
                interview_ref='INT-%d' % interview_id)
            ok = send_email(f'Interview Invitation – {c["job_title"]}', c['applicant_email'], html)
            if not ok:
                email_failures += 1
            scheduled_count += 1

        log_audit('BULK_SCHEDULE_INTERVIEW', 'Recruitment',
                  f'Scheduled {scheduled_count} interviews for posting #{posting_id} ({fmt})',
                  action_details={'posting_id': posting_id, 'count': scheduled_count,
                                  'date': date, 'format': fmt,
                                  'skipped_conflicts': skipped_conflicts})

        msg = f'Scheduled {scheduled_count} interview(s) and emails sent.'
        if email_failures:
            msg += f' {email_failures} email(s) failed to send.'
        if skipped_conflicts:
            msg += f' {skipped_conflicts} candidate(s) skipped: an interviewer is already booked at their slot.'
        flash(msg, 'success' if not email_failures else 'warning')
        return redirect(url_for('recruitment.list_interviews'))

    postings = query("""
        SELECT jp.posting_id, jp.title, b.name as branch_name,
               (SELECT COUNT(*) FROM Job_Application WHERE posting_id=jp.posting_id AND status='Shortlisted') as candidate_count,
               (SELECT COUNT(*) FROM Employee e
                 JOIN Role r ON e.role_id=r.role_id
                 WHERE e.company_id=? AND e.is_active=1 AND e.branch_id=jp.branch_id
                   AND r.role_name IN ('Manager','Admin','HR','HR Manager')) as local_interviewers,
               (SELECT requested_by FROM Vacancy_Request
                 WHERE posting_id=jp.posting_id AND status='Approved'
                 ORDER BY created_at ASC, request_id ASC LIMIT 1) as requester_id
        FROM Job_Posting jp
        JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE jp.status IN ('Open','Partially Filled') AND b.company_id=?
          AND (SELECT COUNT(*) FROM Job_Application WHERE posting_id=jp.posting_id AND status='Shortlisted') > 0
        ORDER BY jp.title
    """, (session.get('company_id'), session.get('company_id')))
    co = session.get('company_id')
    branch_id = session.get('branch_id', 0)
    interviewers = query("""
        SELECT e.employee_id, e.full_name, e.branch_id, r.role_name, b.name as branch_name
        FROM Employee e
        JOIN Role r ON e.role_id=r.role_id
        JOIN Branch b ON e.branch_id=b.branch_id
        WHERE e.company_id=?
          AND ((r.role_name='Manager' AND e.branch_id=?) OR r.role_name IN ('Admin','HR','HR Manager'))
        ORDER BY r.role_name, e.full_name
    """, (co, branch_id))
    return render_template('recruitment/bulk_schedule.html', postings=postings, interviewers=interviewers)


@recruit_bp.route('/_shortlisted-for-posting/<int:pid>')
@login_required
def _shortlisted_for_posting(pid):
    if not _can_access_posting(pid):
        return jsonify({'error': 'Forbidden'}), 403
    candidates = query("""
        SELECT application_id, applicant_name, applicant_email
        FROM Job_Application
        WHERE posting_id=? AND status='Shortlisted'
        ORDER BY applicant_name
    """, (pid,))
    return jsonify([dict(c) for c in candidates])


# ── Interview Policy (Timeslot Configuration) ─────────────────────────────────

def _get_or_create_interview_policy(company_id):
    policy = query("SELECT * FROM Interview_Policy WHERE company_id=?", (company_id,), one=True)
    if not policy:
        execute("""INSERT INTO Interview_Policy
            (company_id, default_duration_min, default_type, default_location, default_meeting_link,
             day_start_time, day_end_time, slot_gap_min, max_per_day, auto_notify)
            VALUES (?,60,'In-Person','','','09:00','17:00',15,8,1)""", (company_id,))
        policy = query("SELECT * FROM Interview_Policy WHERE company_id=?", (company_id,), one=True)
    return policy


@recruit_bp.route('/interview-policy', methods=['GET', 'POST'])
@login_required
@role_required('Admin', 'HR Manager')
def interview_policy():
    co = session.get('company_id')
    policy = _get_or_create_interview_policy(co)
    if request.method == 'POST':
        f = request.form
        try:
            default_duration = int(f.get('default_duration_min', ''))
            slot_gap = int(f.get('slot_gap_min', ''))
            max_per_day = int(f.get('max_per_day', ''))
            day_start = datetime.strptime(f.get('day_start_time', ''), '%H:%M').time()
            day_end = datetime.strptime(f.get('day_end_time', ''), '%H:%M').time()
        except (TypeError, ValueError):
            flash('Enter valid interview-policy numbers and working times.', 'danger')
            return redirect(url_for('recruitment.interview_policy'))
        if default_duration < 15 or default_duration % 5:
            flash('Default duration must be at least 15 minutes and use five-minute increments.', 'danger')
            return redirect(url_for('recruitment.interview_policy'))
        if slot_gap < 0 or slot_gap > 120:
            flash('Slot gap must be between 0 and 120 minutes.', 'danger')
            return redirect(url_for('recruitment.interview_policy'))
        if max_per_day < 1 or max_per_day > 20:
            flash('Max interviews per day must be between 1 and 20.', 'danger')
            return redirect(url_for('recruitment.interview_policy'))
        if day_start >= day_end:
            flash('Working-day end time must be later than the start time.', 'danger')
            return redirect(url_for('recruitment.interview_policy'))
        default_type = f.get('default_type', 'In-Person')
        if default_type not in ('In-Person', 'Online', 'Phone'):
            flash('Select a valid default interview type.', 'danger')
            return redirect(url_for('recruitment.interview_policy'))
        execute("""UPDATE Interview_Policy SET
            default_duration_min=?, default_type=?, default_location=?, default_meeting_link=?,
            day_start_time=?, day_end_time=?, slot_gap_min=?, max_per_day=?,
            auto_notify=?, updated_at=datetime('now')
            WHERE policy_id=?""",
            (default_duration,
             default_type,
             f.get('default_location', ''),
             f.get('default_meeting_link', ''),
             day_start.strftime('%H:%M'),
             day_end.strftime('%H:%M'),
             slot_gap,
             max_per_day,
             1 if f.get('auto_notify') else 0,
             policy['policy_id']))
        log_audit('UPDATE_INTERVIEW_POLICY', 'Recruitment', 'Interview policy updated')
        flash('Interview policy saved.', 'success')
        return redirect(url_for('recruitment.interview_policy'))
    return render_template('recruitment/interview_policy.html', policy=policy)


# ── Auto-Assign Interviews ────────────────────────────────────────────────────

def _is_interviewer_on_leave(interviewer_id, date_str):
    overlap = query("""
        SELECT COUNT(*) as cnt FROM Leave_Application
        WHERE employee_id=? AND status IN ('Pending','Approved')
          AND start_date <= ? AND end_date >= ?
    """, (interviewer_id, date_str, date_str), one=True)
    return overlap and overlap['cnt'] > 0


def _interviewer_busy(interviewer_id, slot_dt, duration_min, exclude_interview_id=None):
    """True when the interviewer already has a Scheduled/Confirmed interview
    overlapping [slot_dt, slot_dt + duration_min).

    Overlap test: existing.start < candidate.end AND candidate.start < existing.end.
    Optionally excludes one interview (used when rescheduling)."""
    excl = " AND interview_id!=?" if exclude_interview_id else ""
    args = [f'%{interviewer_id}%',
            slot_dt.strftime('%Y-%m-%d %H:%M:%S'), f'+{int(duration_min)} minutes',
            slot_dt.strftime('%Y-%m-%d %H:%M:%S')]
    if exclude_interview_id:
        args.append(exclude_interview_id)
    row = query(f"""
        SELECT COUNT(*) as c FROM Interview
        WHERE interviewer_ids LIKE ?
          AND status IN ('Scheduled','Confirmed')
          AND datetime(scheduled_at) < datetime(?, ?)
          AND datetime(?) < datetime(scheduled_at, '+' || duration_min || ' minutes')
          {excl}
    """, args, one=True)
    return bool(row and row['c'] > 0)


def _requester_for_posting(posting_id, co):
    """Return the manager who requested the posting via the earliest approved
    vacancy request (active employee dict), or None.

    Deterministic: earliest created_at ASC, request_id ASC tie-break. Direct
    postings have no approved vacancy request, so they never have a requester.
    The requester must still be an active employee of the company.
    """
    if not posting_id:
        return None
    req = query("""
        SELECT requested_by FROM Vacancy_Request
        WHERE posting_id=? AND status='Approved'
        ORDER BY created_at ASC, request_id ASC
        LIMIT 1
    """, (posting_id,), one=True)
    if not req:
        return None
    emp = query("""
        SELECT e.employee_id, e.full_name, e.branch_id, r.role_name
        FROM Employee e JOIN Role r ON e.role_id=r.role_id
        WHERE e.employee_id=? AND e.company_id=? AND e.is_active=1
    """, (req['requested_by'], co), one=True)
    return dict(emp) if emp else None


def _get_eligible_interviewers(co, branch_id, physical_only=False, requester_id=None):
    """Eligible active interviewers for an interview on a posting's branch.

    Virtual (physical_only=False) keeps the existing roster: Managers of the
    posting branch plus Admin/HR/HR Manager company-wide. Physical restricts
    every role to employees physically in the posting branch, so the panel
    can actually attend on-site.

    Ordering: the requester (when given and eligible) first, then posting-branch
    interviewers, then remaining company-wide interviewers; each group sorted by
    role_name then full_name. Requester priority never adds an otherwise
    ineligible person: the requester is only included when active (query filters
    is_active) and, for physical_only, when assigned to the posting branch.
    """
    if physical_only:
        rows = query("""
            SELECT e.employee_id, e.full_name, e.branch_id, r.role_name,
                   b.name as branch_name
            FROM Employee e
            JOIN Role r ON e.role_id=r.role_id
            JOIN Branch b ON e.branch_id=b.branch_id
            WHERE e.company_id=? AND e.is_active=1 AND e.branch_id=?
              AND r.role_name IN ('Manager','Admin','HR','HR Manager')
        """, (co, branch_id or 0))
    else:
        rows = query("""
            SELECT e.employee_id, e.full_name, e.branch_id, r.role_name,
                   b.name as branch_name
            FROM Employee e
            JOIN Role r ON e.role_id=r.role_id
            JOIN Branch b ON e.branch_id=b.branch_id
            WHERE e.company_id=? AND e.is_active=1
              AND ((r.role_name='Manager' AND e.branch_id=?) OR r.role_name IN ('Admin','HR','HR Manager'))
        """, (co, branch_id or 0))

    requester = None
    local = []
    others = []
    for row in rows:
        if requester_id is not None and row['employee_id'] == requester_id:
            requester = row
        elif row['branch_id'] == (branch_id or 0):
            local.append(row)
        else:
            others.append(row)
    local.sort(key=lambda r: (r['role_name'], r['full_name']))
    others.sort(key=lambda r: (r['role_name'], r['full_name']))
    rows = ([requester] if requester else []) + local + others
    return [dict(r) for r in rows]


def _find_next_available_slot(policy, interviewer_pool, start_date, existing_slots, co, branch_id):
    from datetime import datetime as dt, timedelta
    from datetime import time

    day_start = dt.strptime(policy['day_start_time'], '%H:%M').time() if ':' in str(policy['day_start_time']) else time(9, 0)
    day_end = dt.strptime(policy['day_end_time'], '%H:%M').time() if ':' in str(policy['day_end_time']) else time(17, 0)
    duration = int(policy['default_duration_min'])
    gap = int(policy['slot_gap_min'])
    max_per_day = int(policy['max_per_day'] or 8)

    current_date = start_date
    max_future = start_date + timedelta(days=30)

    while current_date <= max_future:
        if current_date.weekday() >= 5:
            current_date += timedelta(days=1)
            continue

        date_str = current_date.strftime('%Y-%m-%d')
        count_today = sum(1 for s in existing_slots if s.startswith(date_str))

        available_interviewers = [iv for iv in interviewer_pool
                                  if not _is_interviewer_on_leave(iv['employee_id'], date_str)]
        if not available_interviewers:
            current_date += timedelta(days=1)
            continue

        # Start scanning at the batch's last slot on this day (respecting the
        # gap), or at the day start when this day has no batch slots yet.
        day_slots = [s for s in existing_slots if s.startswith(date_str)]
        if day_slots:
            cursor = dt.strptime(day_slots[-1], '%Y-%m-%d %H:%M:%S') + timedelta(minutes=duration + gap)
        else:
            cursor = dt.combine(current_date, day_start)

        # Scan candidate times within the day; at each time pick the first
        # interviewer who is not on leave AND not already booked in the DB
        # (overlap check against all Scheduled/Confirmed interviews).
        while cursor.time() <= day_end and count_today < max_per_day:
            for iv in available_interviewers:
                if not _interviewer_busy(iv['employee_id'], cursor, duration):
                    return cursor, iv
            cursor += timedelta(minutes=duration + gap)
            count_today += 1

        current_date += timedelta(days=1)

    return None, None


@recruit_bp.route('/auto-assign', methods=['POST'])
@login_required
@role_required('Admin', 'HR', 'HR Manager')
def auto_assign_preview():
    co = session.get('company_id')
    branch_id = session.get('branch_id', 0)
    policy = _get_or_create_interview_policy(co)
    application_ids = request.form.getlist('application_ids')
    if not application_ids:
        return jsonify({'error': 'No candidates selected.'})

    # G24: every candidate must be reachable from this session's scope
    try:
        ids = [int(aid) for aid in application_ids]
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid candidate selection.'}), 400
    if not all(_can_access_application(a) for a in ids):
        return jsonify({'error': 'Forbidden'}), 403
    application_ids = [str(a) for a in ids]

    candidates = query(f"""
        SELECT ja.application_id, ja.applicant_name, ja.applicant_email, ja.posting_id,
               jp.title as job_title
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE ja.application_id IN ({','.join('?' for _ in application_ids)})
          AND ja.status='Shortlisted'
        ORDER BY ja.applied_at ASC
    """, [int(aid) for aid in application_ids])

    if not candidates:
        return jsonify({'error': 'No valid shortlisted candidates found.'})

    # Auto-assign handles candidates from ONE job posting per batch only.
    posting_ids = {c['posting_id'] for c in candidates}
    if len(posting_ids) != 1 or None in posting_ids:
        return jsonify({'error': 'Auto-assign handles candidates from one job posting per batch. '
                                 'Select candidates from a single posting.'})

    first_posting_id = candidates[0]['posting_id']
    posting = query("""
        SELECT jp.*, d.branch_id FROM Job_Posting jp
        LEFT JOIN Department d ON jp.department_id=d.department_id
        WHERE jp.posting_id=?
    """, (first_posting_id,), one=True)
    posting_branch = posting['branch_id'] if posting else branch_id

    fmt_result = _resolve_interview_format(request.form, posting)
    if fmt_result[0] is None:
        return jsonify({'error': fmt_result[1]})
    fmt, meeting_link, location, venue, posting_branch_id = fmt_result

    requester = _requester_for_posting(first_posting_id, co)
    interviewer_pool = _get_eligible_interviewers(
        co, posting_branch, physical_only=(fmt == 'Physical'),
        requester_id=requester['employee_id'] if requester else None)
    if not interviewer_pool:
        if fmt == 'Physical':
            return jsonify({'error': 'This branch has no eligible local interviewers. Schedule as Virtual instead.'})
        return jsonify({'error': 'No eligible interviewers found for this branch.'})

    from datetime import datetime as dt, timedelta
    start_date = dt.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    existing_slots = []
    assignments = []
    warnings = []

    for c in candidates:
        slot, interviewer = _find_next_available_slot(
            policy, interviewer_pool, start_date, existing_slots, co, posting_branch)
        if slot is None:
            warnings.append(f'{c["applicant_name"]}: Could not find an available slot within 30 days.')
            continue
        slot_str = slot.strftime('%Y-%m-%d %H:%M:%S')
        existing_slots.append(slot_str)
        assignments.append({
            'application_id': c['application_id'],
            'applicant_name': c['applicant_name'],
            'date': slot.strftime('%A, %d %b %Y'),
            'time': slot.strftime('%I:%M %p'),
            'scheduled_at': slot_str,
            'interviewer': interviewer['full_name'],
            'interviewer_ids': str(interviewer['employee_id']),
            'duration': int(policy['default_duration_min']),
            'format': fmt,
            'location': location,
            'venue': venue,
            'meeting_link': meeting_link
        })

    return jsonify({'assignments': assignments, 'warnings': warnings, 'format': fmt,
                    'location': location, 'venue': venue, 'meeting_link': meeting_link})


@recruit_bp.route('/auto-assign/confirm', methods=['POST'])
@login_required
@role_required('Admin', 'HR', 'HR Manager')
def auto_assign_confirm():
    application_ids = request.form.getlist('application_ids')
    if not application_ids:
        flash('No candidates selected.', 'danger')
        return redirect(url_for('recruitment.list_applications'))

    # G25: every candidate must be reachable from this session's scope
    try:
        ids = [int(aid) for aid in application_ids]
    except (TypeError, ValueError):
        flash('Invalid candidate selection.', 'danger')
        return redirect(url_for('recruitment.list_applications'))
    if not all(_can_access_application(a) for a in ids):
        flash('Access denied.', 'danger')
        return redirect(url_for('recruitment.list_applications'))
    application_ids = [str(a) for a in ids]

    candidates = query(f"""
        SELECT ja.application_id, ja.applicant_name, ja.applicant_email, ja.posting_id,
               jp.title as job_title
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE ja.application_id IN ({','.join('?' for _ in application_ids)})
          AND ja.status='Shortlisted'
        ORDER BY ja.applied_at ASC
    """, [int(aid) for aid in application_ids])

    if not candidates:
        flash('No valid shortlisted candidates found.', 'danger')
        return redirect(url_for('recruitment.list_applications'))

    # Auto-assign handles candidates from ONE job posting per batch only.
    posting_ids = {c['posting_id'] for c in candidates}
    if len(posting_ids) != 1 or None in posting_ids:
        flash('Auto-assign handles candidates from one job posting per batch. '
              'Select candidates from a single posting.', 'danger')
        return redirect(url_for('recruitment.list_applications'))

    co = session.get('company_id')
    first_posting_id = candidates[0]['posting_id']
    posting = query("""
        SELECT jp.*, d.branch_id FROM Job_Posting jp
        LEFT JOIN Department d ON jp.department_id=d.department_id
        WHERE jp.posting_id=?
    """, (first_posting_id,), one=True)
    posting_branch = posting['branch_id'] if posting else session.get('branch_id', 0)
    policy = _get_or_create_interview_policy(co)

    # Revalidate the selected format, venue and meeting link on confirmation.
    fmt_result = _resolve_interview_format(request.form, posting)
    if fmt_result[0] is None:
        flash(fmt_result[1], 'danger')
        return redirect(url_for('recruitment.list_applications'))
    fmt, meeting_link, location, venue, posting_branch_id = fmt_result

    requester = _requester_for_posting(first_posting_id, co)
    interviewer_pool = _get_eligible_interviewers(
        co, posting_branch, physical_only=(fmt == 'Physical'),
        requester_id=requester['employee_id'] if requester else None)
    if not interviewer_pool:
        if fmt == 'Physical':
            flash('This branch has no eligible local interviewers. Schedule as Virtual instead.', 'danger')
        else:
            flash('No eligible interviewers found for this branch.', 'danger')
        return redirect(url_for('recruitment.list_applications'))

    from datetime import datetime as dt, timedelta
    start_date = dt.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    existing_slots = []
    scheduled = 0
    email_ok = 0
    email_fail = 0
    conflicts = 0

    for c in candidates:
        slot, interviewer = _find_next_available_slot(
            policy, interviewer_pool, start_date, existing_slots, co, posting_branch)
        if slot is None:
            conflicts += 1
            continue
        slot_str = slot.strftime('%Y-%m-%d %H:%M:%S')
        existing_slots.append(slot_str)

        interview_id = execute("""INSERT INTO Interview
            (application_id, scheduled_at, duration_min, location, meeting_link, type,
             format, venue, posting_branch_id, status, interviewer_ids)
            VALUES (?,?,?,?,?,?,?,?,?,'Scheduled',?)""",
            (c['application_id'], slot_str, int(policy['default_duration_min']),
             location, meeting_link, 'In-Person' if fmt == 'Physical' else 'Online',
             fmt, venue, posting_branch_id, str(interviewer['employee_id'])))

        execute("UPDATE Job_Application SET status='Interview' WHERE application_id=?", (c['application_id'],))

        html = render_template('emails/interview_scheduled.html',
            employee_name=c['applicant_name'],
            title='Interview Scheduled',
            job_title=c['job_title'],
            interview_date=slot.strftime('%A, %d %B %Y'),
            interview_time=slot.strftime('%I:%M %p'),
            location=_display_location(fmt, location, venue, meeting_link),
            interview_type=fmt,
            interview_ref='INT-%d' % interview_id)
        if send_email(f'Interview Invitation – {c["job_title"]}', c['applicant_email'], html):
            email_ok += 1
        else:
            email_fail += 1
        scheduled += 1

    log_audit('AUTO_ASSIGN_INTERVIEWS', 'Recruitment',
              f'Auto-assigned {scheduled} interview(s) ({fmt})',
              action_details={'scheduled_count': scheduled, 'email_ok': email_ok,
                              'email_fail': email_fail, 'conflicts': conflicts,
                              'format': fmt})

    if scheduled > 0:
        msg = f'Scheduled {scheduled} interview(s). {email_ok} email(s) sent.'
        if email_fail:
            msg += f' {email_fail} email(s) failed.'
        if conflicts:
            msg += f' {conflicts} candidate(s) skipped: no conflict-free slot within 30 days.'
        flash(msg, 'success' if not email_fail else 'warning')
    else:
        flash('No interview slots could be assigned. Check interviewer availability, '
              'the interview policy, or existing bookings and try again.', 'warning')

    return redirect(url_for('recruitment.list_interviews'))

# -- Internal Job Board (Employees) -------------------------------------------

@recruit_bp.route('/internal-jobs')
@login_required
def internal_jobs():
    co = session.get('company_id')
    postings = query("""
        SELECT jp.*, d.department_name, b.name as branch_name
        FROM Job_Posting jp
        JOIN Department d ON jp.department_id=d.department_id
        JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE jp.status IN ('Open','Partially Filled')
          AND jp.target_audience IN ('Internal','Both')
          AND b.company_id=?
        ORDER BY jp.created_at DESC
    """, (co,))
    return render_template('recruitment/internal_jobs.html', postings=postings)


@recruit_bp.route('/internal-jobs/<int:pid>')
@login_required
def internal_job_detail(pid):
    co = session.get('company_id')
    posting = query("""
        SELECT jp.*, d.department_name, b.name as branch_name
        FROM Job_Posting jp
        JOIN Department d ON jp.department_id=d.department_id
        JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE jp.posting_id=? AND jp.status IN ('Open','Partially Filled')
          AND jp.target_audience IN ('Internal','Both')
          AND b.company_id=?
    """, (pid, co), one=True)
    if not posting:
        flash('Posting not found or not open to internal applications.', 'danger')
        return redirect(url_for('recruitment.internal_jobs'))
    return render_template('recruitment/internal_job_detail.html', posting=posting)


@recruit_bp.route('/internal-jobs/<int:pid>/apply', methods=['POST'])
@login_required
def internal_apply(pid):
    uid = session['user_id']
    posting = query("""
        SELECT jp.*, b.company_id FROM Job_Posting jp
        JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE jp.posting_id=?
    """, (pid,), one=True)
    if not posting:
        flash('Posting not found.', 'danger')
        return redirect(url_for('recruitment.internal_jobs'))
    if posting['status'] not in ('Open', 'Partially Filled'):
        flash('This posting is no longer accepting applications.', 'warning')
        return redirect(url_for('recruitment.internal_job_detail', pid=pid))
    if posting['target_audience'] not in ('Internal', 'Both'):
        flash('This position is only open to external candidates. Internal applications are not accepted.', 'warning')
        return redirect(url_for('recruitment.internal_job_detail', pid=pid))
    if posting['company_id'] != session.get('company_id'):
        flash('This posting belongs to another company.', 'danger')
        return redirect(url_for('recruitment.internal_jobs'))

    emp = query("""SELECT e.employee_id, e.full_name, e.personal_email, e.email, e.is_active
                   FROM Employee e WHERE e.employee_id=?""", (uid,), one=True)
    if not emp or not emp['is_active']:
        flash('Only active employees can apply for internal positions.', 'danger')
        return redirect(url_for('recruitment.internal_jobs'))

    dup = query("""SELECT 1 FROM Job_Application
                   WHERE posting_id=? AND internal_employee_id=?
                   LIMIT 1""", (pid, uid), one=True)
    if dup:
        flash('You have already applied for this position.', 'warning')
        return redirect(url_for('recruitment.my_applications'))

    resume_file = request.files.get('resume')
    resume_path = None
    if resume_file and resume_file.filename:
        import uuid
        ext = os.path.splitext(resume_file.filename)[1] or '.pdf'
        filename = f"resume_{uuid.uuid4().hex}{ext}"
        resume_dir = os.path.join(current_app.root_path, '..', 'uploads', 'resumes')
        os.makedirs(resume_dir, exist_ok=True)
        resume_file.save(os.path.join(resume_dir, filename))
        resume_path = filename

    applicant_email = emp['personal_email'] or emp['email'] or ''
    app_id = execute("""
        INSERT INTO Job_Application
        (posting_id, company_id, applicant_name, applicant_email, applicant_phone, resume_path, cover_letter,
         source, applicant_type, internal_employee_id, status)
        VALUES (?,?,?,?,?,?,?,'Portal','Internal',?,'New')
    """, (pid, posting['company_id'], emp['full_name'], applicant_email,
          '', resume_path, request.form.get('cover_letter', ''),
          uid))
    log_audit('INTERNAL_APPLY', 'Recruitment',
              f'Employee {uid} applied internally for posting #{pid}',
              action_details={'posting_id': pid, 'application_id': app_id})

    # AI scoring — same auto-shortlisting as external/public applications
    try:
        posting_data = query("""
            SELECT title, description, requirements FROM Job_Posting WHERE posting_id=?
        """, (pid,), one=True)
        if posting_data:
            app_data = query("""
                SELECT application_id, applicant_name, cover_letter, resume_path
                FROM Job_Application WHERE application_id=?
            """, (app_id,), one=True)
            if app_data:
                from app.recruitment.scorer import score_and_persist
                score_and_persist(app_id, dict(posting_data), dict(app_data),
                                  app_root=current_app.root_path)
    except Exception as e:
        print(f"[INTERNAL APPLY] AI scoring failed: {e}")

    from app.notifications.routes import send_in_app_notification
    try:
        send_in_app_notification(uid, 'Application Received',
                                 f'Your application for {posting["title"]} has been submitted.',
                                 type='Info',
                                 related_url=url_for('recruitment.my_applications'))
    except Exception as e:
        print(f"[INTERNAL APPLY] Notification failed: {e}")

    return render_template('recruitment/internal_apply_confirm.html',
                           posting=posting, application_id=app_id)


# -- My Applications (Employee self-service) ----------------------------------

@recruit_bp.route('/my-applications')
@login_required
def my_applications():
    uid = session['user_id']
    applications = query("""
        SELECT ja.*, jp.title as job_title, jp.status as posting_status,
               d.department_name, b.name as branch_name
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        LEFT JOIN Department d ON jp.department_id=d.department_id
        LEFT JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE ja.internal_employee_id=?
        ORDER BY ja.applied_at DESC
    """, (uid,))
    return render_template('recruitment/my_applications.html', applications=applications)


@recruit_bp.route('/my-applications/<int:aid>')
@login_required
def my_application_detail(aid):
    uid = session['user_id']
    app = query("""
        SELECT ja.*, jp.title as job_title, jp.status as posting_status,
               d.department_name, b.name as branch_name
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        LEFT JOIN Department d ON jp.department_id=d.department_id
        LEFT JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE ja.application_id=? AND ja.internal_employee_id=?
    """, (aid, uid), one=True)
    if not app:
        return _deny_access()
    interviews = query("""
        SELECT scheduled_at, status, result FROM Interview
        WHERE application_id=? ORDER BY scheduled_at DESC
    """, (aid,))
    contract = query("""
        SELECT status, offer_date, start_date FROM Contract
        WHERE application_id=?
    """, (aid,), one=True)
    return render_template('recruitment/my_application_detail.html',
                           app=app, interviews=interviews, contract=contract)
