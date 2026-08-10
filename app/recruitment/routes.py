"""app/recruitment/routes.py – Job Application & Recruitment Module."""

import os
from flask import (Blueprint, render_template, request, session,
                   flash, redirect, url_for, jsonify, send_from_directory,
                   current_app)
from app.database import query, execute, log_audit, close_job_posting_for_application
from app.auth.routes import login_required, role_required
from app.notifications.email_service import send_email
from datetime import datetime

recruit_bp = Blueprint('recruitment', __name__, url_prefix='/recruitment')


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
        WHERE jp.status = 'Open'
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
@role_required('Admin', 'HR Manager', 'HR Director')
def add_posting():
    if request.method == 'POST':
        f = request.form
        emp_type = f['employment_type']
        norm = {'Full-time': 'Full-Time', 'Part-time': 'Part-Time',
                'Full Time': 'Full-Time', 'Part Time': 'Part-Time',
                'fulltime': 'Full-Time', 'parttime': 'Part-Time'}
        emp_type = norm.get(emp_type, emp_type)

        # Catalog-only selection: new titles must first be added by HR/Admin
        pos = query("SELECT * FROM Position WHERE position_id=? AND is_active=1",
                    (int(f['position_id']),), one=True) if f.get('position_id') else None
        if not pos:
            flash('Please select a position from the catalog (add new titles under Roles & Permissions).', 'danger')
            return redirect(url_for('recruitment.add_posting'))
        if pos['department_id'] != int(f['department_id']):
            flash('Selected position does not belong to the chosen department.', 'danger')
            return redirect(url_for('recruitment.add_posting'))

        pid = execute("""INSERT INTO Job_Posting
            (title, department_id, branch_id, employment_type, position_id,
             min_salary, max_salary, description, requirements,
             posted_by, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,'Open')""",
            (pos['position_name'], int(f['department_id']), int(f['branch_id']),
             emp_type, pos['position_id'],
             float(f['min_salary']) if f.get('min_salary') else None,
             float(f['max_salary']) if f.get('max_salary') else None,
             f.get('description', ''), f.get('requirements', ''),
             session['user_id']))
        log_audit('CREATE_POSTING', 'Recruitment',
                  f'Direct posting created: {pos["position_name"]}',
                  action_details={'posting_id': pid, 'title': pos['position_name'],
                                  'position_id': pos['position_id']})
        flash(f'Job posting "{pos["position_name"]}" created!', 'success')
        return redirect(url_for('recruitment.view_posting', pid=pid))

    departments = query("""
        SELECT d.*, b.name as branch_name
        FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
        WHERE EXISTS (SELECT 1 FROM Position p
                      WHERE p.department_id=d.department_id AND p.is_active=1)
        ORDER BY d.department_name
    """)
    branches = query("SELECT * FROM Branch ORDER BY name")
    positions = query("SELECT * FROM Position WHERE is_active=1 ORDER BY position_name")
    return render_template('recruitment/add_posting.html',
                           departments=departments, branches=branches, positions=positions)


@recruit_bp.route('/postings')
def list_postings():
    role = session.get('user_role')
    co = session.get('company_id')
    search = request.args.get('q', '')
    dept = request.args.get('dept', '')
    branch_filter = request.args.get('branch', '')

    show = request.args.get('show', 'active')
    if show == 'closed':
        conditions = ["jp.status IN ('Filled','Closed')"]
    else:
        conditions = ["jp.status='Open'"]
    args = []
    if role in ('Admin', 'HR', 'HR Manager', 'HR Director'):
        pass

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
    departments = query("SELECT * FROM Department")
    branches = query("SELECT * FROM Branch ORDER BY name")
    return render_template('recruitment/list_postings.html',
                           postings=postings, departments=departments, branches=branches, show=show)


@recruit_bp.route('/postings/<int:pid>')
def view_posting(pid):
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
    return render_template('recruitment/view_posting.html',
                           posting=posting, applications=applications, new_count=new_count)


# ── Public Apply (No Login Required) ─────────────────────────────────────────

@recruit_bp.route('/apply/<int:pid>', methods=['GET', 'POST'])
def public_apply(pid):
    posting = query("""
        SELECT jp.*, d.department_name, b.name as branch_name
        FROM Job_Posting jp
        JOIN Department d ON jp.department_id=d.department_id
        JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE jp.posting_id=?
    """, (pid,), one=True)
    if not posting:
        return '<h2>Job not found.</h2>', 404

    if posting['status'] != 'Open':
        return '<h2>This job posting is no longer accepting applications.</h2>', 404

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
            (posting_id, applicant_name, applicant_email, applicant_phone, resume_path, cover_letter, source, status)
            VALUES (?,?,?,?,?,?,'Portal','New')
        """, (pid, f['applicant_name'], f['applicant_email'], f.get('applicant_phone', ''),
              resume_path, f.get('cover_letter', '')))

        # AI scoring
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
                    app_data = dict(app_data)
                    if app_data.get('cover_letter') and len(app_data['cover_letter'].strip()) > 100:
                        from app.recruitment.scorer import score_applications
                        results = score_applications(dict(posting_data), [app_data], app_root=current_app.root_path)
                        if results:
                            r = results[0]
                            status = 'Shortlisted' if r['score'] > 60 else 'New'
                            execute("""
                                UPDATE Job_Application
                                SET ai_score=?, ai_summary=?, status=?
                                WHERE application_id=?
                            """, (r['score'], r['summary'], status, app_id))
                        else:
                            execute("UPDATE Job_Application SET ai_score=0 WHERE application_id=?", (app_id,))
        except Exception as e:
            print(f"[PUBLIC APPLY] AI scoring failed: {e}")

        return render_template('recruitment/apply_thanks.html', posting=posting)

    return render_template('recruitment/apply.html', posting=posting)


# ── Job Applications ─────────────────────────────────────────────────────────

@recruit_bp.route('/applications')
@login_required
def list_applications():
    role = session['user_role']
    show = request.args.get('show', 'shortlisted')

    if show == 'shortlisted':
        status_condition = "ja.status='Shortlisted'"
        order = "ja.ai_score DESC, ja.applied_at DESC"
    elif show == 'hired':
        status_condition = "ja.status IN ('Hired','Accepted')"
        order = "ja.reviewed_at DESC"
    elif show == 'rejected':
        status_condition = "ja.status='Rejected'"
        order = "ja.reviewed_at DESC"
    else:
        status_condition = "ja.status NOT IN ('Hired','Rejected')"
        order = "ja.applied_at DESC"

    status_filter = request.args.get('status', '') if show in ('shortlisted', 'active') else ''
    branch_filter = request.args.get('branch', '')
    job_filter = request.args.get('job', '')

    sql = f"""
        SELECT ja.*, jp.title as job_title, jp.posting_id, d.department_name, b.name as branch_name
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        LEFT JOIN Department d ON jp.department_id=d.department_id
        LEFT JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE {status_condition}
    """
    args = []
    if role == 'Manager':
        sql += " AND jp.branch_id=?"
        args.append(session['branch_id'])
    if status_filter:
        sql += " AND ja.status=?"
        args.append(status_filter)
    if branch_filter:
        sql += " AND jp.branch_id=?"
        args.append(branch_filter)
    if job_filter:
        sql += " AND jp.posting_id=?"
        args.append(job_filter)
    sql += f" ORDER BY {order}"

    applications = query(sql, args) if args else query(sql)
    branches = query("SELECT * FROM Branch ORDER BY name")
    job_postings = query("SELECT posting_id, title FROM Job_Posting ORDER BY title")
    return render_template('recruitment/list_applications.html',
                           applications=applications, status_filter=status_filter,
                           branches=branches, job_postings=job_postings, show=show)


@recruit_bp.route('/applications/<int:aid>')
@login_required
def view_application(aid):
    app = query("""
        SELECT ja.*, jp.title as job_title, d.department_name, b.name as branch_name
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
        interview_list.append(d)
    interviews = interview_list

    has_passed_interview = any(iv.get('result') == 'Pass' for iv in interviews)

    contract = query("""
        SELECT * FROM Contract WHERE application_id=?
    """, (aid,), one=True)

    # Eligible interviewers: Managers from same branch + HR/Admin/HR Manager company-wide
    # + the manager who requested this position via vacancy request
    co = session.get('company_id')
    dept_id = app['department_id'] if 'department_id' in app else None
    branch_id = None
    if dept_id:
        dept = query("SELECT branch_id FROM Department WHERE department_id=?", (dept_id,), one=True)
        branch_id = dept['branch_id'] if dept else None
    eligible = query("""
        SELECT e.employee_id, e.full_name, r.role_name
        FROM Employee e
        JOIN Role r ON e.role_id=r.role_id
        WHERE e.company_id=?
          AND ((r.role_name='Manager' AND e.branch_id=?) OR r.role_name IN ('Admin','HR','HR Manager','HR Director'))
        ORDER BY r.role_name, e.full_name
    """, (co, branch_id or 0))

    # Also add the manager who requested this position
    req = query("""
        SELECT vr.requested_by FROM Vacancy_Request vr
        WHERE vr.posting_id=? AND vr.status='Approved'
        LIMIT 1
    """, (app['posting_id'],), one=True)
    if req:
        requester_id = req['requested_by']
        if not any(e['employee_id'] == requester_id for e in eligible):
            req_emp = query("""
                SELECT e.employee_id, e.full_name, r.role_name
                FROM Employee e JOIN Role r ON e.role_id=r.role_id
                WHERE e.employee_id=?
            """, (requester_id,), one=True)
            if req_emp:
                eligible.append(req_emp)

    return render_template('recruitment/view_application.html',
                           app=app, interviews=interviews, contract=contract,
                           eligible_interviewers=eligible,
                           has_passed_interview=has_passed_interview)


@recruit_bp.route('/applications/<int:aid>/status', methods=['POST'])
@role_required('Admin', 'HR')
def update_application_status(aid):
    new_status = request.form.get('status')
    valid = ['New', 'Shortlisted', 'Interview', 'Offered', 'Hired', 'Rejected']
    if new_status not in valid:
        flash('Invalid status.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    execute("""
        UPDATE Job_Application SET status=?, reviewed_by=?, reviewed_at=datetime('now')
        WHERE application_id=?
    """, (new_status, session['user_id'], aid))

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

    # Auto-reject other candidates when one is hired
    if new_status == 'Hired':
        app_data = query("""
            SELECT ja.*, jp.title as job_title FROM Job_Application ja
            LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
            WHERE ja.application_id=?
        """, (aid,), one=True)
        if app_data and app_data['posting_id']:
            others = query("""
                SELECT application_id, applicant_name, applicant_email
                FROM Job_Application
                WHERE posting_id=? AND application_id!=? AND status IN ('New','Shortlisted','Interview','Offered')
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
                          action_details={'posting_id': app_data['posting_id'], 'hired_aid': aid, 'rejected_count': len(others)})
            # Close the job posting since a candidate has been hired
            close_job_posting_for_application(aid)

    log_audit('UPDATE_APP_STATUS', 'Recruitment',
              f'Application {aid} status changed to {new_status}',
              action_details={'new_status': new_status})
    flash(f'Status updated to {new_status}.', 'success')
    return redirect(url_for('recruitment.view_application', aid=aid))


# ── Add Application (Manual) ──────────────────────────────────────────────────

@recruit_bp.route('/postings/<int:pid>/add-application', methods=['POST'])
@role_required('Admin', 'HR')
def add_application(pid):
    f = request.form
    execute("""
        INSERT INTO Job_Application (posting_id, applicant_name, applicant_email, applicant_phone, cover_letter, status)
        VALUES (?,?,?,?,?,'New')                                
    """, (pid, f['applicant_name'], f['applicant_email'], f.get('applicant_phone', ''), f.get('cover_letter', '')))
    flash(f'Application added for {f["applicant_name"]}.', 'success')
    return redirect(url_for('recruitment.view_posting', pid=pid))


# ── Reject Non-Shortlisted Candidates ──────────────────────────────────────────

@recruit_bp.route('/postings/<int:pid>/reject-non-shortlisted', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager')
def reject_non_shortlisted(pid):
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


# ── Interviews ───────────────────────────────────────────────────────────────

@recruit_bp.route('/interviews')
@login_required
def list_interviews():
    role = session['user_role']
    status_filter = request.args.get('status', '')
    sql = """
        SELECT i.*, ja.applicant_name, jp.title as job_title
        FROM Interview i
        JOIN Job_Application ja ON i.application_id=ja.application_id
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE 1=1
    """
    args = []
    if role == 'Manager':
        sql += " AND jp.branch_id=?"
        args.append(session['branch_id'])
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
    if request.method == 'POST':
        f = request.form
        emp_type = f['employment_type']
        norm = {'Full-time': 'Full-Time', 'Part-time': 'Part-Time',
                'Full Time': 'Full-Time', 'Part Time': 'Part-Time',
                'fulltime': 'Full-Time', 'parttime': 'Part-Time'}
        emp_type = norm.get(emp_type, emp_type)
        execute("""
            INSERT OR REPLACE INTO Contract
            (application_id, offer_date, start_date, position, department_id,
             work_start_time, work_end_time, base_salary, employment_type, status)
            VALUES(?,?,?,?,?,?,?,?,?,'Draft')
        """, (aid, f['offer_date'], f['start_date'], f['position'],
              f['department_id'], f['work_start_time'], f['work_end_time'],
              float(f['base_salary']), emp_type))
        flash('Contract draft saved!', 'success')
        return redirect(url_for('recruitment.view_application', aid=aid))

    app = query("""
        SELECT ja.*, jp.title as job_title, jp.department_id, jp.employment_type
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE ja.application_id=?
    """, (aid,), one=True)
    if not app:
        flash('Application not found.', 'danger')
        return redirect(url_for('recruitment.list_applications'))

    contract = query("""
        SELECT * FROM Contract WHERE application_id=?
    """, (aid,), one=True)
    departments = query("SELECT * FROM Department")
    return render_template('recruitment/contract.html',
                           app=app, contract=contract, departments=departments)


# ── Schedule Interview ────────────────────────────────────────────────────────

@recruit_bp.route('/application/<int:aid>/schedule-interview', methods=['POST'])
@role_required('Admin', 'HR')
def schedule_interview(aid):
    app_data = query("""
        SELECT ja.*, jp.title as job_title
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE ja.application_id=?
    """, (aid,), one=True)
    if not app_data:
        flash('Application not found.', 'danger')
        return redirect(url_for('recruitment.list_applications'))

    f = request.form
    scheduled_at = f"{f['date']} {f['time']}:00"
    interviewer_ids = ','.join(f.getlist('interviewer_ids')) if 'interviewer_ids' in f else ''
    execute("""
        INSERT INTO Interview
        (application_id, scheduled_at, duration_min, location, meeting_link, type, status, interviewer_ids)
        VALUES(?,?,?,?,?,?,'Scheduled',?)
    """, (aid, scheduled_at, int(f.get('duration', 60)),
          f.get('location', ''), f.get('meeting_link', ''), f.get('type', 'In-Person'),
          interviewer_ids))

    execute("UPDATE Job_Application SET status='Interview' WHERE application_id=?", (aid,))
    log_audit('SCHEDULE_INTERVIEW', 'Recruitment', f'Interview scheduled for application {aid}')

    # Send email to candidate
    from datetime import datetime as dt
    dt_obj = dt.strptime(scheduled_at, '%Y-%m-%d %H:%M:%S')
    html = render_template('emails/interview_scheduled.html',
        employee_name=app_data['applicant_name'],
        title='Interview Scheduled',
        job_title=app_data['job_title'],
        interview_date=dt_obj.strftime('%A, %d %B %Y'),
        interview_time=dt_obj.strftime('%I:%M %p'),
        location=f.get('location', 'To be confirmed'),
        interview_type=f.get('type', 'In-Person'))
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
    interview = query("SELECT * FROM Interview WHERE interview_id=?", (iid,), one=True)
    if not interview:
        flash('Interview not found.', 'danger')
        return redirect(url_for('recruitment.list_interviews'))

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


# ── Cancel Interview ──────────────────────────────────────────────────────────

@recruit_bp.route('/interview/<int:iid>/cancel', methods=['POST'])
@role_required('Admin', 'HR')
def cancel_interview(iid):
    interview = query("SELECT * FROM Interview WHERE interview_id=?", (iid,), one=True)
    if not interview:
        flash('Interview not found.', 'danger')
        return redirect(url_for('recruitment.list_interviews'))

    execute("UPDATE Interview SET status='Cancelled' WHERE interview_id=?", (iid,))
    log_audit('CANCEL_INTERVIEW', 'Recruitment', f'Interview {iid} cancelled')
    flash('Interview cancelled.', 'success')
    return redirect(url_for('recruitment.view_application', aid=interview['application_id']))


# ── Send Offer (Email) ────────────────────────────────────────────────────────

@recruit_bp.route('/application/<int:aid>/send-offer', methods=['POST'])
@role_required('Admin', 'HR')
def send_offer(aid):
    # Ensure company signature_path column exists
    try:
        execute("ALTER TABLE Company ADD COLUMN signature_path TEXT")
    except Exception:
        pass

    contract = query("""
        SELECT c.*, ja.applicant_name, ja.applicant_email, jp.title as job_title
        FROM Contract c
        JOIN Job_Application ja ON c.application_id=ja.application_id
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE c.application_id=?
    """, (aid,), one=True)
    if not contract:
        flash('No contract found. Create a contract first.', 'danger')
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
        contract_id=contract['contract_id'],
        hr_email=hr_email)

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
        flash('Offer letter sent to candidate with contract PDF attached.', 'success')
    else:
        flash('Offer marked as sent but email delivery failed. Check mail configuration.', 'warning')

    return redirect(url_for('recruitment.view_application', aid=aid))


# ── Download Resume ───────────────────────────────────────────────────────────

@recruit_bp.route('/resume/<int:aid>/download')
@login_required
def download_resume(aid):
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

@recruit_bp.route('/contract/<int:cid>/download')
@login_required
def download_contract(cid):
    contract = query("SELECT contract_doc_path, application_id FROM Contract WHERE contract_id=?", (cid,), one=True)
    if not contract or not contract['contract_doc_path']:
        flash('Contract PDF not found.', 'danger')
        aid = contract['application_id'] if contract else request.args.get('aid', 0)
        return redirect(url_for('recruitment.view_application', aid=aid))

    from flask import send_file
    import os
    path = contract['contract_doc_path']
    if not os.path.exists(path):
        flash('Contract PDF file not found on disk.', 'danger')
        aid = contract['application_id'] if contract else request.args.get('aid', 0)
        return redirect(url_for('recruitment.view_application', aid=aid))
    return send_file(path, as_attachment=True,
                     download_name=f"contract_{cid}.pdf",
                     mimetype='application/pdf')


@recruit_bp.route('/contract/<int:cid>/download-signed')
@login_required
def download_signed_contract(cid):
    contract = query("SELECT signed_doc_path, application_id FROM Contract WHERE contract_id=?", (cid,), one=True)
    if not contract or not contract['signed_doc_path']:
        flash('Signed contract PDF not found.', 'danger')
        aid = contract['application_id'] if contract else request.args.get('aid', 0)
        return redirect(url_for('recruitment.view_application', aid=aid))

    from flask import send_file
    import os
    path = contract['signed_doc_path']
    if not os.path.exists(path):
        flash('Signed contract PDF file not found on disk.', 'danger')
        aid = contract['application_id'] if contract else request.args.get('aid', 0)
        return redirect(url_for('recruitment.view_application', aid=aid))
    return send_file(path, as_attachment=True,
                     download_name=f"contract_{cid}_signed.pdf",
                     mimetype='application/pdf')


# ── Accept Offer (Public) ─────────────────────────────────────────────────────

@recruit_bp.route('/contract/<int:cid>/accept')
def accept_offer(cid):
    contract = query("""
        SELECT c.*, ja.applicant_name, jp.title as job_title
        FROM Contract c
        JOIN Job_Application ja ON c.application_id=ja.application_id
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        WHERE c.contract_id=?
    """, (cid,), one=True)
    if not contract:
        return '<h2>Offer not found.</h2>', 404

    if contract['status'] == 'Accepted':
        return '<h2>This offer has already been accepted.</h2>'

    if contract['status'] != 'Sent':
        return '<h2>This offer is not yet ready to be accepted.</h2>'

    execute("UPDATE Contract SET status='Accepted' WHERE contract_id=?", (cid,))
    execute("UPDATE Job_Application SET status='Hired' WHERE application_id=?", (contract['application_id'],))
    # Close the job posting
    close_job_posting_for_application(contract['application_id'])
    log_audit('ACCEPT_OFFER', 'Recruitment', f'Offer {cid} accepted by candidate')

    # Notify all Admin/HR users
    try:
        from app.notifications.routes import send_notification
        hr_users = query("SELECT employee_id FROM Employee WHERE role_id IN (SELECT role_id FROM Role WHERE role_name IN ('Admin','HR','HR Manager','HR Director')) AND is_active=1")
        for u in hr_users:
            send_notification(
                u['employee_id'],
                'Offer Accepted',
                f'{contract["applicant_name"]} accepted the offer for {contract["job_title"]}',
                type='Offer',
                related_url='/recruitment/applications'
            )
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
    contract = query("SELECT * FROM Contract WHERE application_id=?", (aid,), one=True)
    if not contract:
        flash('No contract found.', 'danger')
        return redirect(url_for('recruitment.view_application', aid=aid))

    execute("UPDATE Contract SET status='Accepted' WHERE contract_id=?", (contract['contract_id'],))
    execute("UPDATE Job_Application SET status='Hired' WHERE application_id=?", (aid,))
    # Close the job posting
    close_job_posting_for_application(aid)
    log_audit('ACCEPT_OFFER_HR', 'Recruitment', f'Offer accepted by HR for application {aid}')
    try:
        from app.notifications.routes import send_notification
        hr_users = query("SELECT employee_id FROM Employee WHERE role_id IN (SELECT role_id FROM Role WHERE role_name IN ('Admin','HR','HR Manager','HR Director')) AND is_active=1")
        app_data = query("SELECT ja.applicant_name, jp.title as job_title FROM Job_Application ja LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id WHERE ja.application_id=?", (aid,), one=True)
        for u in hr_users:
            send_notification(
                u['employee_id'],
                'Offer Accepted (HR)',
                f'Offer accepted by HR for {app_data["applicant_name"]} – {app_data["job_title"]}',
                type='Offer',
                related_url='/recruitment/applications'
            )
    except Exception as e:
        print(f"[ACCEPT OFFER HR] Notification failed: {e}")
    flash('Offer accepted. You can now proceed to add the employee.', 'success')
    return redirect(url_for('recruitment.view_application', aid=aid))


# ── Verify Signed Contract (HR checks signed PDF) ────────────────────────────

@recruit_bp.route('/application/<int:aid>/verify-signed-contract', methods=['POST'])
@role_required('Admin', 'HR')
def verify_signed_contract(aid):
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
    execute("UPDATE Job_Application SET status='Hired' WHERE application_id=?", (aid,))
    # Close the job posting
    close_job_posting_for_application(aid)
    log_audit('VERIFY_SIGNED_CONTRACT', 'Recruitment',
              f'Signed contract {contract["contract_id"]} verified and accepted for application {aid}')

    try:
        from app.notifications.routes import send_notification
        hr_users = query("SELECT employee_id FROM Employee WHERE role_id IN (SELECT role_id FROM Role WHERE role_name IN ('Admin','HR','HR Manager','HR Director')) AND is_active=1")
        app_data = query("SELECT ja.applicant_name, jp.title as job_title FROM Job_Application ja LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id WHERE ja.application_id=?", (aid,), one=True)
        for u in hr_users:
            send_notification(
                u['employee_id'],
                'Signed Contract Verified',
                f'Signed contract verified for {app_data["applicant_name"]} – {app_data["job_title"]}',
                type='Offer',
                related_url='/recruitment/applications'
            )
    except Exception as e:
        print(f"[VERIFY SIGNED CONTRACT] Notification failed: {e}")

    # Serve the signed PDF for download as confirmation
    sig_path = contract['signed_doc_path']
    if os.path.exists(sig_path):
        from flask import send_file
        return send_file(sig_path, as_attachment=True,
                         download_name=f"signed_contract_{contract['contract_id']}.pdf",
                         mimetype='application/pdf')

    flash('Signed contract verified and accepted.', 'success')
    return redirect(url_for('recruitment.view_application', aid=aid))


# ── Hire → Add Employee (Pre-filled) ─────────────────────────────────────────

@recruit_bp.route('/application/<int:aid>/hire')
@role_required('Admin', 'HR')
def hire(aid):
    app_data = dict(query("""
        SELECT ja.*, jp.branch_id, jp.department_id as posting_dept_id, jp.title as job_title
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

    import json, re
    sanitized = re.sub(r'[^a-z0-9.]', '', app_data['applicant_name'].lower().replace(' ', '.'))
    smarthr_email = f'{sanitized}@smarthr.my'
    session['hire_prefill'] = json.dumps({
        'full_name': app_data['applicant_name'],
        'email': smarthr_email,
        'personal_email': app_data['applicant_email'],
        'ic_number': app_data.get('applicant_ic') or '',
        'contact_no': app_data['applicant_phone'] or '',
        'address': app_data.get('applicant_address') or '',
        'position': contract['position'] or app_data['job_title'],
        'department_id': contract['department_id'] or app_data['posting_dept_id'],
        'branch_id': branch_id,
        'employment_type': contract['employment_type'] or 'Full-Time',
        'base_salary': str(contract['base_salary'] or 0),
        'hire_date': contract['start_date'] or '',
        'work_start_time': contract['work_start_time'] or '09:00',
        'work_end_time': contract['work_end_time'] or '18:00',
        'contract_id': contract['contract_id'],
    })
    # Close the job posting
    close_job_posting_for_application(aid)
    return redirect(url_for('employees.add_employee', from_hire=1))


# ── Vacancy Request (Manager submits) ─────────────────────────────────────────

@recruit_bp.route('/vacancy-request', methods=['GET', 'POST'])
@login_required
def vacancy_request():
    role = session.get('user_role')
    is_dept_mgr = session.get('is_dept_manager', False)
    managed_dept_id = session.get('managed_dept_id')
    if role not in ('Admin', 'Manager', 'HR Manager', 'HR Director') and not is_dept_mgr:
        flash('You do not have permission to access that page.', 'danger')
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        f = request.form
        emp_type = f['employment_type']
        norm = {'Full-time': 'Full-Time', 'Part-time': 'Part-Time',
                'Full Time': 'Full-Time', 'Part Time': 'Part-Time',
                'fulltime': 'Full-Time', 'parttime': 'Part-Time'}
        emp_type = norm.get(emp_type, emp_type)

        # ── Server-side guard: never trust the dropdown for authorization ──
        try:
            dept_id = int(f['department_id'])
        except (TypeError, ValueError):
            flash('Please select a department.', 'danger')
            return redirect(url_for('recruitment.vacancy_request'))
        if role in ('Admin', 'HR', 'HR Manager', 'HR Director'):
            pass
        elif is_dept_mgr and managed_dept_id:
            if dept_id != managed_dept_id:
                flash('You may only request positions for your own department.', 'danger')
                return redirect(url_for('recruitment.vacancy_request'))
        else:
            dept = query("SELECT branch_id FROM Department WHERE department_id=?", (dept_id,), one=True)
            if not dept or dept['branch_id'] != session.get('branch_id'):
                flash('You may only request positions for your own branch.', 'danger')
                return redirect(url_for('recruitment.vacancy_request'))

        # ── Position: catalog entry OR custom title (HR reviews custom) ──
        position_id = f.get('position_id') or ''
        is_custom = 0
        if position_id and position_id != '__custom__':
            pos = query("SELECT * FROM Position WHERE position_id=? AND is_active=1",
                        (int(position_id),), one=True)
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

        execute("""INSERT INTO Vacancy_Request
                   (requested_by, department_id, position_title, position_id, is_custom,
                    employment_type, min_salary, max_salary, description, requirements, reason, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'Pending')""",
                (session['user_id'], dept_id, position_title, position_id, is_custom,
                 emp_type,
                 float(f['min_salary']) if f.get('min_salary') else None,
                 float(f['max_salary']) if f.get('max_salary') else None,
                 f.get('description', ''), f.get('requirements', ''),
                 f.get('reason', '')))
        log_audit('SUBMIT_VACANCY', 'Recruitment', f'Vacancy request submitted: {position_title}',
                  action_details={'department_id': dept_id, 'is_custom': is_custom})
        flash('Vacancy request submitted for review.', 'success')
        return redirect(url_for('recruitment.list_vacancy_requests'))

    branch_id = session.get('branch_id')

    # Dept manager: only show their managed department
    if is_dept_mgr and managed_dept_id and role not in ('Admin', 'HR Manager', 'HR Director'):
        departments = query("""
            SELECT d.*, b.name as branch_name
            FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
            WHERE d.department_id = ?
        """, (managed_dept_id,))
    elif role in ('Admin', 'HR', 'HR Manager', 'HR Director'):
        departments = query("""
            SELECT d.*, b.name as branch_name
            FROM Department d JOIN Branch b ON d.branch_id=b.branch_id
            ORDER BY d.department_name
        """)
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
                           departments=departments, positions=positions)


@recruit_bp.route('/vacancy-requests')
@login_required
def list_vacancy_requests():
    role = session['user_role']
    user_id = session['user_id']
    is_dept_mgr = session.get('is_dept_manager', False)
    managed_dept_id = session.get('managed_dept_id')

    if role in ('Admin', 'HR', 'HR Manager', 'HR Director'):
        requests = query("""
            SELECT vr.*, d.department_name, e.full_name as requester_name
            FROM Vacancy_Request vr
            JOIN Department d ON vr.department_id=d.department_id
            JOIN Employee e ON vr.requested_by=e.employee_id
            ORDER BY vr.created_at DESC
        """)
    # Dept manager: see all requests for their managed department
    elif is_dept_mgr and managed_dept_id:
        requests = query("""
            SELECT vr.*, d.department_name, e.full_name as requester_name
            FROM Vacancy_Request vr
            JOIN Department d ON vr.department_id=d.department_id
            JOIN Employee e ON vr.requested_by=e.employee_id
            WHERE vr.department_id = ?
            ORDER BY vr.created_at DESC
        """, (managed_dept_id,))
    else:
        requests = query("""
            SELECT vr.*, d.department_name, e.full_name as requester_name
            FROM Vacancy_Request vr
            JOIN Department d ON vr.department_id=d.department_id
            JOIN Employee e ON vr.requested_by=e.employee_id
            WHERE vr.requested_by=?
            ORDER BY vr.created_at DESC
        """, (user_id,))
    return render_template('recruitment/vacancy_requests.html', requests=requests)


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

    role = session['user_role']
    user_id = session['user_id']
    managed_dept_id = session.get('managed_dept_id')
    if role not in ('Admin', 'HR', 'HR Manager', 'HR Director') and req['requested_by'] != user_id:
        # Also allow department managers to view requests for their department
        if not (session.get('is_dept_manager') and managed_dept_id == req['department_id']):
            flash('Access denied.', 'danger')
            return redirect(url_for('recruitment.list_vacancy_requests'))

    return render_template('recruitment/vacancy_request_detail.html', req=req)


@recruit_bp.route('/vacancy-request/<int:rid>/approve', methods=['POST'])
@role_required('Admin', 'HR', 'HR Manager')
def approve_vacancy(rid):
    uid = session['user_id']
    req = query("SELECT * FROM Vacancy_Request WHERE request_id=?", (rid,), one=True)
    if not req:
        flash('Vacancy request not found.', 'danger')
        return redirect(url_for('recruitment.list_vacancy_requests'))
    if req['status'] != 'Pending':
        flash('This request has already been reviewed.', 'warning')
        return redirect(url_for('recruitment.view_vacancy_request', rid=rid))

    branch_id = request.form.get('branch_id')
    if not branch_id:
        flash('Please select a branch for this position.', 'danger')
        return redirect(url_for('recruitment.view_vacancy_request', rid=rid))

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

    posting_id = execute("""INSERT INTO Job_Posting
        (title, department_id, branch_id, employment_type, position_id,
         min_salary, max_salary, description, requirements,
         posted_by, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,'Open')""",
        (req['position_title'], req['department_id'], int(branch_id),
         req['employment_type'], posting_position_id,
         req['min_salary'], req['max_salary'],
         req['description'], req['requirements'], uid))

    execute("""UPDATE Vacancy_Request
               SET status='Approved', reviewed_by=?, reviewed_at=datetime('now'), posting_id=?
               WHERE request_id=?""",
            (uid, posting_id, rid))

    log_audit('APPROVE_VACANCY', 'Recruitment',
              f'Vacancy request {rid} approved, posting {posting_id} created',
              action_details={'request_id': rid, 'posting_id': posting_id,
                              'is_custom': req['is_custom'],
                              'position_title': req['position_title']})

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


@recruit_bp.route('/_branches-for-dept/<int:dept_id>')
@login_required
def _branches_for_dept(dept_id):
    dept = query("SELECT branch_id FROM Department WHERE department_id=?", (dept_id,), one=True)
    if not dept:
        return jsonify([])
    branches = query("SELECT branch_id, name FROM Branch WHERE branch_id=?", (dept['branch_id'],))
    return jsonify([dict(b) for b in branches])


# ── Bulk Interview Scheduling ─────────────────────────────────────────────────

@recruit_bp.route('/bulk-schedule', methods=['GET', 'POST'])
@role_required('Admin', 'HR')
def bulk_schedule():
    if request.method == 'POST':
        f = request.form
        posting_id = int(f['posting_id'])
        date = f['date']
        start_time = f['start_time']
        duration = int(f.get('duration', 30))
        interview_type = f.get('type', 'In-Person')
        location = f.get('location', '')
        meeting_link = f.get('meeting_link', '')
        interviewer_ids = ','.join(f.getlist('interviewer_ids')) if 'interviewer_ids' in f else ''
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

        from datetime import datetime as dt, timedelta
        base_dt = dt.strptime(f"{date} {start_time}", '%Y-%m-%d %H:%M')
        scheduled_count = 0
        email_failures = 0

        for i, c in enumerate(candidates):
            slot = base_dt + timedelta(minutes=i * duration)
            scheduled_at = slot.strftime('%Y-%m-%d %H:%M:%S')

            execute("""INSERT INTO Interview
                (application_id, scheduled_at, duration_min, location, meeting_link, type, status, interviewer_ids)
                VALUES (?,?,?,?,?,?,'Scheduled',?)""",
                (c['application_id'], scheduled_at, duration, location, meeting_link, interview_type, interviewer_ids))

            execute("UPDATE Job_Application SET status='Interview' WHERE application_id=?", (c['application_id'],))

            dt_obj = slot
            html = render_template('emails/interview_scheduled.html',
                employee_name=c['applicant_name'],
                title='Interview Scheduled',
                job_title=c['job_title'],
                interview_date=dt_obj.strftime('%A, %d %B %Y'),
                interview_time=dt_obj.strftime('%I:%M %p'),
                location=location or 'To be confirmed',
                interview_type=interview_type)
            ok = send_email(f'Interview Invitation – {c["job_title"]}', c['applicant_email'], html)
            if not ok:
                email_failures += 1
            scheduled_count += 1

        log_audit('BULK_SCHEDULE_INTERVIEW', 'Recruitment',
                  f'Scheduled {scheduled_count} interviews for posting #{posting_id}',
                  action_details={'posting_id': posting_id, 'count': scheduled_count, 'date': date})

        msg = f'Scheduled {scheduled_count} interview(s) and emails sent.'
        if email_failures:
            msg += f' {email_failures} email(s) failed to send.'
        flash(msg, 'success' if not email_failures else 'warning')
        return redirect(url_for('recruitment.list_interviews'))

    postings = query("""
        SELECT jp.posting_id, jp.title,
               (SELECT COUNT(*) FROM Job_Application WHERE posting_id=jp.posting_id AND status='Shortlisted') as candidate_count
        FROM Job_Posting jp
        WHERE jp.status='Open'
          AND (SELECT COUNT(*) FROM Job_Application WHERE posting_id=jp.posting_id AND status='Shortlisted') > 0
        ORDER BY jp.title
    """)
    co = session.get('company_id')
    branch_id = session.get('branch_id', 0)
    interviewers = query("""
        SELECT e.employee_id, e.full_name, r.role_name
        FROM Employee e
        JOIN Role r ON e.role_id=r.role_id
        WHERE e.company_id=?
          AND ((r.role_name='Manager' AND e.branch_id=?) OR r.role_name IN ('Admin','HR','HR Manager','HR Director'))
        ORDER BY r.role_name, e.full_name
    """, (co, branch_id))
    return render_template('recruitment/bulk_schedule.html', postings=postings, interviewers=interviewers)


@recruit_bp.route('/_shortlisted-for-posting/<int:pid>')
@login_required
def _shortlisted_for_posting(pid):
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
@role_required('Admin', 'HR Manager', 'HR Director')
def interview_policy():
    co = session.get('company_id')
    policy = _get_or_create_interview_policy(co)
    if request.method == 'POST':
        f = request.form
        execute("""UPDATE Interview_Policy SET
            default_duration_min=?, default_type=?, default_location=?, default_meeting_link=?,
            day_start_time=?, day_end_time=?, slot_gap_min=?, max_per_day=?,
            auto_notify=?, updated_at=datetime('now')
            WHERE policy_id=?""",
            (int(f.get('default_duration_min', 60)),
             f.get('default_type', 'In-Person'),
             f.get('default_location', ''),
             f.get('default_meeting_link', ''),
             f.get('day_start_time', '09:00'),
             f.get('day_end_time', '17:00'),
             int(f.get('slot_gap_min', 15)),
             int(f.get('max_per_day', 8)),
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


def _get_eligible_interviewers(co, branch_id):
    eligible = query("""
        SELECT e.employee_id, e.full_name, r.role_name
        FROM Employee e
        JOIN Role r ON e.role_id=r.role_id
        WHERE e.company_id=? AND e.is_active=1
          AND ((r.role_name='Manager' AND e.branch_id=?) OR r.role_name IN ('Admin','HR','HR Manager','HR Director'))
        ORDER BY r.role_name, e.full_name
    """, (co, branch_id or 0))
    return eligible


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

        if count_today >= max_per_day:
            current_date += timedelta(days=1)
            continue

        available_interviewers = []
        for iv in interviewer_pool:
            if not _is_interviewer_on_leave(iv['employee_id'], date_str):
                available_interviewers.append(iv)

        if not available_interviewers:
            current_date += timedelta(days=1)
            continue

        if existing_slots:
            last_slot = dt.strptime(existing_slots[-1], '%Y-%m-%d %H:%M:%S')
        else:
            last_slot = dt.combine(current_date, day_start)

        proposed = last_slot + timedelta(minutes=duration + gap)
        if proposed.time() > day_end:
            current_date += timedelta(days=1)
            continue

        interviewer = available_interviewers[count_today % len(available_interviewers)]
        return proposed, interviewer

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

    first_posting_id = candidates[0]['posting_id']
    posting = query("""
        SELECT jp.*, d.branch_id FROM Job_Posting jp
        LEFT JOIN Department d ON jp.department_id=d.department_id
        WHERE jp.posting_id=?
    """, (first_posting_id,), one=True)
    posting_branch = posting['branch_id'] if posting else branch_id

    interviewer_pool = _get_eligible_interviewers(co, posting_branch)
    if not interviewer_pool:
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
            'type': policy['default_type'],
            'location': policy['default_location'] or '',
            'meeting_link': policy['default_meeting_link'] or ''
        })

    return jsonify({'assignments': assignments, 'warnings': warnings})


@recruit_bp.route('/auto-assign/confirm', methods=['POST'])
@login_required
@role_required('Admin', 'HR', 'HR Manager')
def auto_assign_confirm():
    application_ids = request.form.getlist('application_ids')
    if not application_ids:
        flash('No candidates selected.', 'danger')
        return redirect(url_for('recruitment.list_applications'))

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

    co = session.get('company_id')
    first_posting_id = candidates[0]['posting_id']
    posting = query("""
        SELECT jp.*, d.branch_id FROM Job_Posting jp
        LEFT JOIN Department d ON jp.department_id=d.department_id
        WHERE jp.posting_id=?
    """, (first_posting_id,), one=True)
    posting_branch = posting['branch_id'] if posting else session.get('branch_id', 0)
    policy = _get_or_create_interview_policy(co)
    interviewer_pool = _get_eligible_interviewers(co, posting_branch)

    from datetime import datetime as dt, timedelta
    start_date = dt.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    existing_slots = []
    scheduled = 0
    email_ok = 0
    email_fail = 0

    for c in candidates:
        slot, interviewer = _find_next_available_slot(
            policy, interviewer_pool, start_date, existing_slots, co, posting_branch)
        if slot is None:
            continue
        slot_str = slot.strftime('%Y-%m-%d %H:%M:%S')
        existing_slots.append(slot_str)

        execute("""INSERT INTO Interview
            (application_id, scheduled_at, duration_min, location, meeting_link, type, status, interviewer_ids)
            VALUES (?,?,?,?,?,?,'Scheduled',?)""",
            (c['application_id'], slot_str, int(policy['default_duration_min']),
             policy['default_location'] or '', policy['default_meeting_link'] or '',
             policy['default_type'], str(interviewer['employee_id'])))

        execute("UPDATE Job_Application SET status='Interview' WHERE application_id=?", (c['application_id'],))

        html = render_template('emails/interview_scheduled.html',
            employee_name=c['applicant_name'],
            title='Interview Scheduled',
            job_title=c['job_title'],
            interview_date=slot.strftime('%A, %d %B %Y'),
            interview_time=slot.strftime('%I:%M %p'),
            location=policy['default_location'] or 'To be confirmed',
            interview_type=policy['default_type'])
        if send_email(f'Interview Invitation – {c["job_title"]}', c['applicant_email'], html):
            email_ok += 1
        else:
            email_fail += 1
        scheduled += 1

    log_audit('AUTO_ASSIGN_INTERVIEWS', 'Recruitment',
              f'Auto-assigned {scheduled} interview(s)',
              action_details={'scheduled_count': scheduled, 'email_ok': email_ok, 'email_fail': email_fail})

    if scheduled > 0:
        msg = f'Scheduled {scheduled} interview(s). {email_ok} email(s) sent.'
        if email_fail:
            msg += f' {email_fail} email(s) failed.'
        flash(msg, 'success' if not email_fail else 'warning')
    else:
        flash('No interview slots could be assigned. Check the interview policy and try again.', 'warning')

    return redirect(url_for('recruitment.list_interviews'))
