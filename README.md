# SmartHR — AI-Powered HR Management System

A Flask-based HR management system with face recognition attendance, leave management, payroll, invoicing, and recruitment modules.

---

## Quick Start (Automated)

1. **Run Setup**: Double-click `Setup_SmartHR.bat`. (Installs libraries + initializes DB)
2. **Launch Server**: Double-click `Start_SmartHR.bat`.
3. **Access**: Visit the URL shown in the console (e.g., `http://192.168.x.x:5000`).

---

## Prerequisites

- Windows with **Python 3.10+** available on `PATH`.
- The **Tesseract OCR executable** available on `PATH`; `pytesseract` is only the Python wrapper.
- A compatible `dlib` / `face_recognition` installation for face attendance. The dependency is included in `requirements.txt`, so machines that cannot install it need compatible build tools or wheels before setup can complete.

The setup script creates a fresh `.venv`, installs `requirements.txt`, applies the schema and runs the supported database migrations. Back up `instance/smarthr.db` before intentionally resetting it.

---

## Default Login Credentials

| Role | Email | Password |
|------|-------|----------|
| System Admin | admin@smarthr.my | Admin@123 |
| HR Manager | hr@smarthr.my | Hr@123 |
| CEO (Top Management, KL) | brian@smarthr.my | Manager@123 |
| COO (Top Management, KL) | coo@smarthr.my | Manager@123 |
| CFO (Top Management, KL) | cfo@smarthr.my | Manager@123 |
| Branch Manager (KL) | weiliang@smarthr.my | Manager@123 |
| Branch Manager (Penang) | cheeseng@smarthr.my | Manager@123 |
| Branch Manager (Johor Bahru) | kevin_loh@smarthr.my | Manager@123 |
| Dept Manager (PG Eng) | hafiz@smarthr.my | Manager@123 |
| Dept Manager (KL Eng) | vincent@smarthr.my | Manager@123 |
| Dept Manager (KL Finance) | shanthi@smarthr.my | Manager@123 |
| Dept Manager (JB Ops) | fauzi@smarthr.my | Manager@123 |
| Employee | elizabeth@smarthr.my | Employee@123 |
| Employee | ryan@smarthr.my | Employee@123 |
| Employee | nurul@smarthr.my | Employee@123 |
| Employee | priya@smarthr.my | Employee@123 |

> **Change all passwords before any production use!**

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.x |
| Framework | Flask 3.x |
| Auth | Werkzeug (included with Flask) |
| Database | SQLite |
| Frontend | Jinja2 + Vanilla CSS/JS |
| OCR | Tesseract + pytesseract + PyPDF2 + EasyOCR |
| Face Recognition | OpenCV + face_recognition (dlib) |
| Notifications | In-app + email via Flask-Mail (SMTP Gmail) |
| Email Polling | IMAP inbox monitoring (recruitment) |
| Contract PDF | ReportLab |

---

## Project Structure

```
smarthr_app/
├── app/
│   ├── __init__.py              # Flask app factory – registers all blueprints + context processor
│   ├── database.py              # SQLite helpers (get_db, query, execute, log_audit)
│   ├── crypto_utils.py          # AES-256-GCM encryption for face encodings
│   ├── rate_limiter.py          # Sliding-window rate limiter with X-Forwarded-For IP extraction
│   ├── auth/routes.py           # Login, logout, login_required, role_required + password reset
│   ├── main/routes.py           # Dashboard
│   ├── employees/routes.py      # Employee CRUD + IC upload/OCR + notifications
│   ├── organization/routes.py   # Company/Branch/Department
│   ├── leave/routes.py          # Apply + approve/reject leave
│   ├── attendance/routes.py     # Manual entry + time tracking + biometric face recog + requests
│   ├── invoice/routes.py        # Upload, list, approve/reject + OCR extraction
│   ├── payroll/
│   │   ├── routes.py            # List payroll + payslip view
│   │   ├── calculator.py        # EPF/SOCSO/EIS/PCB calculation engine
│   │   ├── helpers.py           # Immediate payroll reflection helpers (bonus, increment, claims)
│   │   └── autogen.py           # Daily refresh of Draft payroll records
│   ├── reports/routes.py        # Analytics and reports
│   ├── audit/routes.py          # Audit log viewer
│   ├── settings/routes.py       # Profile + password change
│   ├── notifications/
│   │   ├── routes.py            # In-app notification CRUD + send_notification helper
│   │   ├── email_service.py     # Email sending via Flask-Mail (multipart templates)
│   │   ├── email_monitor.py     # IMAP inbox polling, reply parsing, intent detection
│   │   └── email_parser.py      # Subject regex, body parsing, contract ID extraction
│   ├── face/
│   │   ├── routes.py            # Face recognition attendance (register, check-in/out, analytics, health)
│   │   ├── matcher.py           # Face encoding cache + matching logic
│   │   └── reports.py           # Attendance report generation
│   ├── recruitment/
│   │   ├── __init__.py          # Recruitment blueprint
│   │   ├── routes.py            # Job postings, applications, interviews, contracts, hire flow
│   │   ├── scorer.py            # AI shortlisting (keyword-coverage scoring)
│   │   └── contract_pdf.py      # ReportLab contract PDF generation
│   ├── increment/
│   │   ├── __init__.py          # Salary Increment blueprint
│   │   └── routes.py            # Propose, approve/reject, API
│   ├── bonus/
│   │   ├── __init__.py          # Bonus Proposal blueprint
│   │   └── routes.py            # List, propose, approve/reject, bulk-action, pending-count API
│   ├── performance/
│   │   ├── __init__.py          # Performance blueprint
│   │   ├── calculator.py        # Monthly and yearly performance calculations
│   │   └── routes.py            # Scoring engine, list scores
│   └── year_end/
│       ├── __init__.py          # Year-End Review blueprint
│       └── routes.py            # Unified review and compensation-policy routes
├── templates/
│   ├── base.html                # Master template (sidebar, nav, email polling JS)
│   ├── login.html               # Login page
│   ├── dashboard.html           # Dashboard with metrics + toast notifications
│   ├── audit_log.html           # Audit log viewer
│   ├── reports.html             # Reports & analytics
│   ├── settings.html            # Profile + password change
│   ├── attendance/              # biometric.html, logs.html, manual.html, time_tracking.html, requests.html
│   ├── employees/               # add.html, list.html, view.html, upload_ic.html, notifications.html
│   ├── face/                    # register_face.html, face_action.html, registration_list.html, attendance_report.html, attendance_analytics.html, no_face_registered.html
│   ├── organization/            # company_*.html, branch_*.html, department_*.html, role_list.html
│   ├── leave/                   # apply.html, approve.html
│   ├── invoice/                 # list.html, claims.html
│   ├── payroll/                 # list.html, payslip.html
│   ├── performance/             # admin_list.html, employee_view.html
│   ├── increment/               # list.html, propose.html
│   ├── bonus/                   # list.html, propose.html
│   ├── recruitment/             # list_postings.html, view_posting.html, add_posting.html, list_applications.html, view_application.html, interviews.html, contract.html, apply.html, apply_thanks.html, offer_accepted.html, vacancy_request.html, vacancy_requests.html, vacancy_request_detail.html, careers.html
│   ├── notifications/           # email_config.html
│   └── emails/                  # 17 email templates (see below)
├── static/
│   ├── css/style.css            # Main stylesheet (green + dark SmartHR design)
│   └── favicon.svg              # SmartHR logo
├── uploads/                     # Private invoices, ID/leave files, resumes, signatures and contracts (gitignored)
├── instance/smarthr.db          # SQLite database (auto-created by init_db.py)
├── schema.sql                   # SQLite schema
├── init_db.py                   # DB init + Malaysian demo data seeder
├── seed_performance.py          # Back-calculate performance scores from attendance
├── seed_increment.py            # Backfill approved Jan 2026 salary increments
├── seed_increment_test_data.py  # Add 26 test employees + attendance + performance + increments
├── seed_yearly_attendance.py    # Full-year 2026 attendance + yearly reviews (varied A/B/C/D grades)
├── seed_bonus_test.py           # Seed sample Bonus_Proposal records for testing
├── run.py                       # Entry point (loads .env via python-dotenv)
├── requirements.txt             # Python dependencies
├── .env                         # Mail config (gitignored)
├── .gitignore                   # Excludes .env, __pycache__, instance/, uploads/
├── Setup_SmartHR.bat            # One-click setup script
├── Start_SmartHR.bat            # One-click launch script (menu)
└── _run_server.bat              # Helper: launches server in a separate window
```

### Email Templates

| Template | Purpose |
|----------|---------|
| `emails/base.html` | Email layout wrapper |
| `emails/application_received.html` | Job application confirmation |
| `emails/application_rejected.html` | Application rejection notice |
| `emails/attendance_request.html` | Attendance request notification (Admin/HR) |
| `emails/face_system_alert.html` | Face system error/recovery alert |
| `emails/ic_access_requested.html` | IC access request to employee |
| `emails/ic_access_result.html` | IC access approve/reject result |
| `emails/interview_scheduled.html` | Interview schedule notification |
| `emails/invoice_approved.html` | Invoice claim approved |
| `emails/invoice_rejected.html` | Invoice claim rejected |
| `emails/leave_approved.html` | Leave application approved |
| `emails/leave_rejected.html` | Leave application rejected |
| `emails/offer_accepted_confirmation.html` | Offer acceptance confirmation |
| `emails/offer_letter.html` | Job offer letter |
| `emails/password_reset.html` | Password reset instructions |
| `emails/payslip_ready.html` | Monthly payslip notification |
| `emails/welcome_employee.html` | New employee welcome + credentials |

---

## Database

- **Location:** `instance/smarthr.db`
- **Backup:** Simply copy the `.db` file
- **Reset:** Delete `instance/smarthr.db` and re-run `python init_db.py` (destroys local data)
- **Schema file:** `schema.sql`

### Key Tables

| Table | Purpose |
|-------|---------|
| Employee | Users + auth credentials + employment info |
| Role | Admin / HR Manager / HR / Manager / Employee |
| Company / Branch / Department | Org structure |
| Attendance | Check-in/out records + manual overrides + confidence scores |
| Attendance_Request | Employee attendance request submissions + approval workflow |
| Leave_Application | Leave requests |
| Leave_Balance | Remaining days per employee per year |
| Invoice | Expense invoice submissions |
| OCR_Result | Tesseract OCR output |
| Face_Encoding | AES-256-GCM encrypted face encodings |
| Payroll | Monthly payroll records with EPF/SOCSO/EIS + salary_increment column |
| Salary_Increment | Annual performance-based salary increment approvals |
| Increment_Policy | Company-specific increment rules (%, tenure, effective month/year, auto-propose) |
| Bonus_Proposal | Yearly performance bonus proposals with grade, amount, approval workflow |
| Bonus_Policy | Company-specific bonus rules (grade → months of salary, payout month, auto-propose) |
| Interview_Policy | Company-level interview scheduling defaults (duration, type, location, day times, slot gaps, max/day) |
| Performance_Score | Monthly composite scores and grades from attendance analysis (drill-down only) |
| Performance_Review | Yearly aggregate review: composite score + grade |
| AuditLog | All system actions logged here |
| Notification | In-app notification records |

---

## Role-Based Access

| Feature | Admin | HR Manager | HR | Manager | Employee |
|---------|-------|------------|----|---------|---------|
| Dashboard | Yes | Yes | Yes | Yes | Yes |
| Employee CRUD | Yes | Yes | Yes | No | No |
| View own profile | Yes | Yes | Yes | Yes | Yes |
| Leave apply | Yes | Yes | Yes | Yes | Yes |
| Leave approve | Yes | Yes | Yes | Yes | No |
| Manual attendance | Yes | Yes | Yes | No | No |
| Attendance requests | Yes | Yes | Yes | No | Yes |
| Invoice upload | Yes | Yes | Yes | Yes | Yes |
| Invoice approve | Yes | Yes | Yes | Yes | No |
| Payroll view | Yes | Yes | Yes | No | Own only |
| Salary Increment | Yes | Yes | No | No | No |
| Bonus Proposal | Yes | Yes | No | No | No |
| Reports | Yes | Yes | Yes | Yes | No |
| Audit log | Yes | Yes | Yes | No | No |
| Org management | Yes | Yes | Yes | No | No |
| Position Catalog (add/rename/deactivate) | Yes | Yes | Yes | No | No |
| User deactivate | Yes | Yes | Yes | No | No |
| Face Registration | Yes | Yes | No | No | No |
| Face Attendance | Yes | Yes | Yes | Yes | Yes |

> **Manager branch scoping:** Managers have branch-filtered leave, invoice/expense-claim,
> application and interview list views. Every new recruitment detail or action route must
> apply the same company and branch check before it is exposed. Attendance correction
> requests remain Admin/HR-only.

> **Department managers:** Assign an employee as a department manager via
> **Organization → Departments → edit** — any active employee of the department's branch
> can be assigned (their system role is unchanged; department-manager responsibility is
> separate from role-based access). That user's `is_dept_manager` flag is set on login
> and they get dept-scoped vacancy request management. Demo assignments are seeded in
> `init_db.py`.

> **Position Catalog:** Job titles are managed centrally under **Roles & Permissions →
> Position Catalog** (Admin/HR only). The `Position` table was backfilled from existing
> employee/postings titles at migration time (`init_db.py`). Employee add/edit and vacancy
> request/postings pick titles from the catalog (scoped to the chosen/owned department);
> custom titles are allowed on vacancy requests and flagged `is_custom=1` for HR review,
> then optionally promoted into the catalog at approval time.
> Positions can be marked **Department Manager Position** (explicit checkbox — no title
> inference): hires for that title default to the **Employee** system role and are
> automatically assigned as the posting department's manager. If the department already
> has a different manager, the hire is blocked and HR must reassign the manager first.
>
> **Guided setup:** HR can explicitly continue from a new Branch to its Department, catalog
> Position, a prefilled Manager employee form, and a prefilled Job Posting form. The normal
> save buttons remain available at every step. Role assignments, department-manager
> assignments, and posting validation still require an explicit user action.

---

## Session Variables

| Key | Value |
|-----|-------|
| `session['user_id']` | employee_id |
| `session['user_name']` | Full name |
| `session['user_role']` | Admin / HR Manager / HR / Manager / Employee |
| `session['user_initials']` | e.g. "AZ" |
| `session['user_email']` | Email address |
| `session['company_id']` | company_id |
| `session['branch_id']` | branch_id |
| `session['dept_name']` | Department name |
| `session['is_dept_manager']` | True if user is assigned as dept manager |
| `session['managed_dept_id']` | department_id if user is dept manager, else None |

---

## Configuration

- **Secret Key:** Set a long, unique `SECRET_KEY` in `.env`. The built-in fallback is for local development only.
- **Production session cookies:** Set `FORCE_HTTPS_SESSION=true` when serving the app over HTTPS.
- **Debug mode:** Keep `FLASK_DEBUG` unset or `false` outside local development.
- **Max Upload Size:** 10 MB (configurable in `__init__.py`)
- **Upload Folder:** `uploads/`
- **DB Path:** `instance/smarthr.db`

## Mail Configuration

SmartHR uses **Gmail SMTP** for email notifications (leave approve/reject, invoice approve/reject, IC access, payslip ready, password reset).

1. Enable 2FA on your Google account
2. Generate an App Password at https://myaccount.google.com/apppasswords
3. Create a `.env` file in the project root:
```env
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USE_TLS=true
MAIL_USERNAME=your-email@gmail.com
MAIL_PASSWORD=your-16-char-app-password
MAIL_DEFAULT_SENDER=your-email@gmail.com
SECRET_KEY=replace-with-a-long-random-value
FORCE_HTTPS_SESSION=true
```
4. Emails are sent as **multipart/alternative** (HTML + plain text) for better deliverability
5. All email sends are logged in `AuditLog` (action: `SEND_EMAIL`)

> For development testing, use [Mailtrap](https://mailtrap.io) (port 2525, no real delivery).

## Testing

The repository includes a small `tests/` suite. `pytest` is not part of the runtime requirements, so install it in the virtual environment before running:

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
```

---

## Sprint Status

### Sprint 1 — Core HR System (Completed)

| Module | Status | Notes |
|--------|--------|-------|
| Project Structure | Done | Flask app factory, blueprints |
| SQLite Schema | Done | Converted from hr_system_erd_v3.sql |
| Demo Data | Done | 10 Malaysian employees, leave, payroll, invoices |
| Login / Logout | Done | Session-based, account lock after 5 attempts |
| Dashboard | Done | Live metrics from DB |
| Employee CRUD | Done | List, add, view, edit, deactivate |
| Organization Mgmt | Done | Company, Branch, Department |
| Leave Apply | Done | Working-day calc, balance check |
| Leave Approve/Reject | Done | Balance auto-update |
| Manual Attendance | Done | Audit logged |
| Time Tracking | Done | Weekly summary, team overview |
| Invoice Upload | Done | File upload + metadata form |
| Invoice Approve/Reject | Done | With reason |
| Payroll View | Done | Payslip with EPF/SOCSO/EIS breakdown |
| Reports | Done | Headcount, Attendance, Leave, Invoice, Payroll |
| Audit Log | Done | Paginated, filterable, modal details |
| Settings / Profile | Done | Edit profile + change password |

### Sprint 2 — OCR, Face Recognition, Email (Completed)

| Module | Status | Notes |
|--------|--------|-------|
| Automated Deployment | Done | One-click .bat scripts for Setup & Launch |
| Invoice OCR | Done | Supports PDF & Images with Scoring Engine |
| Malaysian Specifics | Done | SDN BHD detection + SSM/SST handling |
| Payroll Management | Done | Dynamic Engine (EPF/SOCSO/EIS + Proration + OT/Leave rules) |
| Bulk Payslips | Done | ZIP download of all monthly payslips |
| Payroll Claims | Done | Integrated approved invoices into payslips |
| Identity Scanner | Done | Malaysian IC/Passport OCR + Hardcoded Watermarking |
| Face Recognition | Done | Biometric face matching integration |
| Email Notifications | Done | Flask-Mail + Gmail SMTP + password reset + multipart (HTML+text) |
| Leave Duplicate Prevention | Done | Overlapping date check on leave application |
| Payroll Bonus Calc | Done | Performance bonus (0-10% based on composite score), leave adjustment |
| Leave Attachment Upload | Done | Upload medical cert/PDF when applying; viewable by managers on approve page |

### Sprint 3 — Performance, Increment, Bonus, Recruitment (Completed)

| Module | Status | Notes |
|--------|--------|-------|
| Performance Evaluation | Done | Scoring engine (attendance 40%, punctuality 30%, OT 15%, reliability 15%) + grades A-D |
| Salary Increment | Done | Propose, approve/reject (bulk + single), auto-suggest % by grade, base_salary auto-update on approve |
| Bonus Proposal | Done | Propose, approve/reject (bulk + single), auto-calc bonus from performance score, immediate payroll reflection |
| Payroll Helpers | Done | `app/payroll/helpers.py` — immediate payroll reflection for bonus/increment/claims |
| Recruitment Frontend | Done | Job Postings, Applications, Interviews, Contract CRUD |
| HR Manager Role | Done | New role with all Admin-level permissions |
| Toast Notification | Done | Auto-dismiss popup on dashboard for pending increment/bonus reviews |
| Header Notification Dropdown | Done | Approval-type notifications for pending increments and bonuses |
| Pending Count Badges | Done | Nav-badge on sidebar links for Salary Increment and Bonus Proposal |
| Grade Filter | Done | Added performance grade filter dropdown to Bonus Proposal list page |
| Commission Removal | Done | Flat 5% commission replaced by performance-based annual increment |

### Sprint 4 — Recruitment Full Flow, HR Manager Permissions (Completed)

| Module | Status | Notes |
|--------|--------|-------|
| Recruitment Full Flow | Done | Interview scheduling + email, Pass/Fail result, Send Offer + email, Accept Offer (public + HR), Hire -> Add Employee pre-filled |
| AI Shortlisting | Done | Keyword-coverage scoring against job requirements, auto-shortlist top candidates |
| Public Apply Page | Done | No-login-required application form with resume upload |
| Vacancy Request Workflow | Done | Manager submits request -> Admin/HR approves (auto-creates posting) or rejects with reason |
| IC Access Email Notification | Done | Sends email to employee when their IC is requested |
| HR Manager Privileges | Done | HR Manager inherits all Admin privileges — full system access, approvals, organisation settings |
| MyKad OCR - EasyOCR Fallback | Done | Deep-learning fallback for guilloche-pattern ICs |
| Internal Job Board | Done | Employees can browse and apply for open postings across their company |
| Interviewer Selection | Done | Schedule form includes dropdown of department heads + HR staff |

### Sprint 5 — Face Verification Refactor (Completed)

| Module | Status | Notes |
|--------|--------|-------|
| Check-in page | Done | Separate `/face/checkin` — one-button flow, no live preview |
| Check-out page | Done | Separate `/face/checkout` — same simple flow |
| Confidence score | Done | Stores `confidence_score`, sets `status='Approved'`, calculates overtime |
| System health monitor | Done | Passive + active (`GET /face/api/health`). Notifies Admin/HR on threshold exceeded or recovery |
| Attendance Requests | Done | New `/attendance/requests` — employees submit with reason; Admin/HR approve/reject |
| Manual self-serve block | Done | Elevated roles must ask a peer |
| Sidebar updates | Done | "Face Check-In" / "Face Check-Out"; gated "Manual Attendance" + "Requests" |
| Notification helpers | Done | `send_notification_to_role()` broadcasts to Admin/HR |
| Face Registration overhaul | Done | 3-second countdown + multi-frame capture over 5s, real-time quality feedback |
| Registration List page | Done | `/face/registration-list` with per-employee status table |

### Sprint 6 — Face Recognition Stability & Registration Overhaul (Completed)

| Module | Status | Notes |
|--------|--------|-------|
| Camera capture fix | Done | Frame captured before camera stops; prevents empty image crash |
| Confidence display | Done | Shown in success message (e.g. "Confidence: 95.2%") |
| Action validation | Done | Server blocks check-in if already checked in, check-out if not checked in |
| Status banner | Done | Color-coded banner (red = checked in, green = checked out, blue = no attendance) |
| DB error fixes | Done | Removed premature `conn.close()` on shared `g.db`; datetime string conversion |
| Face registration auto-flow | Done | Single-click: Start -> capture 5s -> auto-submit best frame -> redirect on success |
| Frame analysis relaxed | Done | Changed to `@login_required` only; simplified to `face_detected` only |
| Face duplicate validation | Done | Compares new face against all other employees; rejects duplicates |
| Encrypted blob handling | Done | Multi-fallback decoding: decrypt -> base64 -> bytes; converts to numpy float64 |
| PBKDF2 import fix | Done | Updated for newer `cryptography` library versions |
| JSON serialization fix | Done | Cast numpy bool/float to Python native types |
| DB column name fix | Done | `face_encoding_id` -> `encoding_id` (matches schema) |

### Sprint 7 — Unified Attendance & Multi-Frame UX (Completed)

| Module | Status | Notes |
|--------|--------|-------|
| Unified attendance page | Done | Single `/face/attendance` page replaces separate check-in/check-out |
| Auto-detect action | Done | Single "Record Attendance" button auto-detects check-in vs check-out |
| Multi-frame capture | Done | 3-second capture with real-time quality indicators; best frame selected |
| Post-attendance UX | Done | Banner updates client-side (no reload), button resets immediately |
| Registration multi-frame | Done | Sends top 5 frames; server cross-validates intra-frame consistency |
| Dashboard crash fix | Done | Fixed `NoneType` crash in `sum(attribute='hours_worked')` using `rejectattr` |

### Sprint 8 — Yearly Performance Review & Performance-Based Bonus Overhaul (Completed)

| Module | Status | Notes |
|--------|--------|-------|
| Performance_Review table | Done | `period_year`, composite score, grade (A≥85, B≥70, C≥55, D<55) |
| Yearly performance calculator | Done | `calculate_yearly_review()` aggregates 12 months attendance into one review |
| Performance list UI | Done | `/performance/` yearly reviews by default; month filter = monthly drill-down |
| 2025 attendance seeded | Done | 44 employees, 7851 records producing A=18, B=7, C=5, D=8 |
| 2025/2026 yearly reviews | Done | Generated from seeded attendance |
| Bonus_Policy table | Done | Per company/year: A=3m, B=2m, C=1m, D=0.5m; payout month (default Jan/Feb CNY) |
| **Unified Year-End Review** | Done | `/year-end-review?year=X` — two tabs (Increments, Bonuses). Auto-generates on load. Bulk approve/reject. No "Propose New" buttons, no "Proposed by" column. |
| **Unified Compensation Policy** | Done | `/compensation/policy?year=X` — Increment Policy + Bonus Policy sections on one page |
| Sidebar decluttered | Done | Removed duplicate Bonus link from Finance & Payroll. Performance section now: Performance Scores, Year-End Review (combined pending badge), Compensation Policy. Old increment/bonus/propose/policy links removed from sidebar. |
| Payroll integration | Done | `autogen.py` joins `Bonus_Proposal` + `Bonus_Policy` on `payout_month` |
| Old routes note | Superseded | `/increment/`, `/bonus/`, `/increment/policy`, `/bonus/policy`, propose pages still exist but unlinked from sidebar — superseded by Year-End Review page. |

### Sprint 9 — Application Filters & Interview Auto-Assign (Completed)

| Module | Status | Notes |
|--------|--------|-------|
| Shortlisted default view | Done | `/recruitment/applications` defaults to Shortlisted tab; options: Shortlisted / Active / Hired / Rejected |
| Reject Non-Shortlisted | Done | Button on posting view to bulk-reject all New (non-shortlisted) candidates with rejection emails |
| Auto-reject on hire/pass | Enhanced | Already existed; rejection email now generic (works for interview and non-interview stages) |
| Interview Policy table | Done | `Interview_Policy` — company-level config: duration, type, location, meeting link, day hours, slot gaps, max/day |
| Interview Policy page | Done | `/recruitment/interview-policy` (Admin/HR Manager) — config form like Bonus Policy |
| Auto-Assign Interviews | Done | Checkbox-select Shortlisted candidates → preview slots → confirm; leave-aware interviewer assignment |
| Leave-aware scheduling | Done | Interviewers with Approved/Pending leave on a date are excluded from that day's pool |
| Sidebar: Interview Policy | Done | New "Interview Policy" link under Recruitment section |

---

---

## 12-Feature Master Plan (Reference)

> **Process:** Do one plan at a time. After confirmation, implement. Some plans must be finished together (e.g. recruitment overhaul). Always fact-check, be honest, give recommendations (prefer Malaysian HR software). For every big plan, ask for logical flow confirmation before implementing.

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 1 | Bulk Finalise Payroll (checkboxes on payroll list) | **Done** | `/payroll/bulk-finalise`, `/payroll/bulk-pdf-selected`; checkbox column, bulk bar, JS |
| 2 | Increment/Bonus Overhaul | **Done** | **Increment:** `Increment_Policy` table, `/increment/policy`, auto-propose based on tenure, auto-apply. **Bonus:** `Bonus_Policy` + yearly `Performance_Review` grade; grades → months of salary: A=3m, B=2m, C=1m, D=0.5m; configurable payout month (default Jan/Feb for CNY); proration by months worked. |
| 3 | Payroll shows increment for all months after increment month | Done | `autogen.py` already uses latest approved `Salary_Increment` for base salary in every month |
| 4 | Performance by Year (not month) | **Done** | `Performance_Review` table, yearly calculator, `/performance/` defaults to yearly view, month filter only for drill-down |
| 5 | Show calculation logic on pages | **Done** | Grade thresholds, composite formula, increment/bonus formulas shown on policy/propose/list pages; sidebar exposes Increment Policy and Bonus Policy |
| 6 | IC Watermark inside lines (horizontal) | **Done** | Two horizontal lines, text centred between them, 40% opacity, `_apply_watermark()` in `app/employees/routes.py` |
| 7 | Signed contract in employee details | **Done** | Contract section always visible; shows details + "View Contract" / "View Signed Contract" buttons when linked; "No contract found" otherwise. Auto-linked 4 contracts by email match. |
| 8 | Vacancy Request workflow + Manager roles | **Done** | Dept manager assignment (Department.dept_manager_id), manager-only dept scope in vacancy form, HR Manager seeded, role list updated, sidebar renamed |
| 9 | External Application form (email) | **Done** | Public careers page (`/recruitment/careers`) with mailto: links, structured email parser (Name/Position/Email/IC/Phone/Address), posting ref tag for exact matching, HR copy-paste block on posting view |
| 10 | Applications page (shortlisted + filters) | **Done** | Default view = Shortlisted; filter for rejected/non-shortlisted; auto-reject non-shortlisted with emails; manual reject-non-shortlisted button on posting view |
| 11 | Interview page overhaul | **Done** | HR timeslot config (Interview_Policy like Bonus_Policy); checkbox-select candidates; auto-assign to nearest available slots; leave-aware interviewer scheduling; auto-email with policy defaults |
| 12 | Sidebar rename | **Done** | "Vacancy Request" → "Applications (Internal)"; "Applications" → "Applications (External)" |

### Implementation Rules (from user)
- **Do not leave old UI behind.** When logic changes, update/delete the matching templates, sidebar links, and routes.
- **Sidebar:** Performance section now has: Performance Scores → Year-End Review (with combined pending badge) → Compensation Policy. Old increment/bonus/propose/policy links removed. Duplicate Bonus link in Finance & Payroll removed.
- **Yearly performance:** Main grade is yearly. Month filter only shows monthly breakdown.
- **Bonus calculation:** A=3 months salary, B=2, C=1, D=0.5. Prorated by months worked.
- **Year-End Review (`/year-end-review`):** Two tabs (Increments, Bonuses). Auto-generates on load. Bulk approve/reject. No "Propose New", no "Proposed by" column.
- **Compensation Policy (`/compensation/policy`):** Unified page with Increment Policy + Bonus Policy sections.
- **Note everything in README.md** as implementation proceeds.

---

## Calculation Rules

### Performance Score (Composite)

| Component | Weight |
|-----------|--------|
| Attendance Rate | 40% |
| Punctuality | 30% |
| Overtime Score | 15% |
| Reliability | 15% |

**Formula:** `composite = attendance x 0.40 + punctuality x 0.30 + overtime x 0.15 + reliability x 0.15`

### Grade Thresholds

| Grade | Composite Score Range |
|-------|----------------------|
| A | >= 85 |
| B | 70 - 84 |
| C | 55 - 69 |
| D | < 55 |

### Salary Increment by Grade (Suggested %)

| Grade | Min | Max | Default |
|-------|-----|-----|---------|
| A | 8% | 10% | 9% |
| B | 5% | 7% | 6% |
| C | 3% | 4% | 3.5% |
| D | 0% | 2% | 1% |

HR/Admin can override the suggested % when proposing. The increment is applied immediately on approval.

### Bonus Calculation (Yearly, from Bonus_Proposal)

**Eligibility:** Active employee with a `Performance_Review` grade for the bonus year and at least the tenure threshold months worked.

**Formula:**
```
full_bonus      = base_salary × grade_months[grade]            # A=3, B=2, C=1, D=0.5
prorated_bonus  = full_bonus × (months_worked_in_year / 12)    # based on hire_date
```

**Payout:** Approved `Bonus_Proposal` is added to payroll in the configured `payout_month` (default January for CNY).

**Legacy fallback (to be removed):** Old monthly `Performance_Score` auto-calc (`composite × 10%`) is deprecated once Bonus_Policy is live.

### Payroll Deductions (EPF / SOCSO / EIS / PCB)

See `app/payroll/calculator.py` for full implementation.

| Component | Employee | Employer |
|-----------|----------|----------|
| EPF | 11% of gross | 12-13% of gross |
| SOCSO | RM 5.00-19.75 (tiered) | Tiered per category |
| EIS | 0.2% of gross (cap RM 7.75) | 0.2% of gross (cap RM 7.75) |
| PCB | Progressive tax table | - |

**Proration:** New hires in the middle of a month have salary prorated by `remaining_days / total_days`.

**Leave adjustment:** Unused leave days paid at RM 200/day.

---

## QA Verification â€” 13 August 2026

### Scope and outcome

- Browser QA covered every Admin sidebar destination except face-recognition and IC/OCR features, which were intentionally excluded. No page showed a server-error, traceback, or 404 state.
- Responsive checks at 390 px covered Applications, Interviews, Attendance Logs, Compensation Policy, and an Employee profile. The sidebar stayed closed by default and no horizontal page overflow was present.
- Recruitment was verified end-to-end using two controlled mailboxes: a structured email application was received through IMAP, created an application, and auto-shortlisted it at 69.9; an invitation and an updated reschedule invitation arrived at the candidate mailbox; a reply containing the interview reference surfaced in-app HR notifications and did not change the interview automatically. The completed interview then recorded a 14/15 scorecard, displayed first in advisory ranking, and received a recorded selection approval.
- No live offer email was sent during this QA pass. Sending an offer would reserve an opening in the controlled test posting, so it remains a deliberate follow-up action.

### Expanded sidebar verification and test-data cleanup

- A second visual smoke pass loaded all **26 Admin sidebar destinations** except Face Attendance and Face Registration, which remain excluded together with IC/OCR. Every checked page rendered substantive content without a traceback, server-error screen, or desktop-width page overflow. This verifies navigation and rendering; state-changing actions on real payroll, claims, leave, and compensation records were deliberately not submitted.
- The complete regression command, run with an empty `PYTHONPATH`, passed: **254 passed, 0 failed**. Its database and email fixtures use an isolated temporary copy and local capture mode.
- Explicit QA records were removed from the live SmartHR database: 185 duplicate test vacancy requests and postings, 9 labelled test applications, 10 linked interviews, 2 contracts, associated offer/reservation/scorecard/recommendation history, 6 `Alice Systest` employees with 864 attendance and 36 payroll rows, 4 test-only position-catalog entries, and their matching notifications/audit rows. The three linked QA upload files were also removed.
- A recoverable snapshot was created immediately before the cleanup at `instance/backups/qa_cleanup_20260813_125825_191797.db`; `PRAGMA foreign_key_check` was clean after deletion. Legitimate records such as the **QA Engineer** position and the **Hardware Engineer** posting were preserved. The incoming QA application email was archived from the monitored Inbox so the poller cannot recreate its deleted application; the other Gmail messages were preserved as recoverable test evidence.

### Observed legacy-state item

- Several older interview records still show `Scheduled` despite dates in the past. New scheduling and rescheduling are blocked server-side for past datetimes, but the current HR workflow requires a user to mark a past interview complete. Automatically completing overdue interviews would be a product-policy change (for example, it may hide a no-show or cancellation), so it was not changed during QA.

### Validation repairs made during QA

- Interview reschedule date controls now prevent selecting a past calendar day. The server remains authoritative: manual and bulk scheduling, plus rescheduling, reject a datetime at or before server time; weekends, working hours, positive duration, and active same-company interviewers are also enforced on manual and bulk scheduling.
- Job-posting and vacancy-request salary values reject malformed, negative, and inverted ranges. Contract drafts reject malformed dates/times/salary and negative base salary. The browser inputs mirror the non-negative salary rule.
- Interview Policy now validates its UI constraints on the server: 15-minute minimum in five-minute increments, a 0â€“120 minute gap, 1â€“20 interviews per day, valid type, and a working-day end after its start.
- Malformed leave fields and malformed email-server ports return clear validation messages rather than 500 errors. A malformed internal-applicant ID is also rejected safely.
- Regression fixtures for field validation now run only against a disposable database copy. The vacancy-opening migration test removes the later scorecard recommendation child table before rebuilding its legacy fixture, so live approved recommendations cannot cause a false migration failure.

### Extended isolated QA and validation hardening

- B24 broad-module QA adds 41 assertions across all non-face Admin sidebar destinations, malformed URL/form input, Employee/Manager record scoping, notification ownership, and sensitive-page access. B25 adds 11 workflow assertions across leave, expense claims, payroll, performance, increments, bonuses, reports, and notification calls. Both blocks use independent temporary SQLite copies and local notification/email capture.
- The performance-score API now enforces the same self/branch boundaries as the user interface: Employees may read only their own score and Managers only their branch. A crafted Manager edit request can no longer move an employee to another branch or department, or change their role; Manager-visible hidden fields must match the existing assignment.
- Period/filter values in Payroll, Performance, Reports, Increment, Bonus, and Year-End views now reject malformed/out-of-range numeric input without a 500 error. Payroll and Performance generation return a clear validation message for a malformed period; compensation and bonus policies validate numeric ranges before persisting.
- Profile updates require a non-empty full name. Expense-claim uploads reject negative amounts and totals of zero before creating either an invoice record or uploaded file.
- A browser pass at 390 px covered Employees, Payroll, Applications, Interviews, Attendance Logs, Claims Management, Year-End Review, and Reports. Every page stayed within the viewport without page-level horizontal overflow; all Admin non-face sidebar destinations also rendered normally at desktop width.
- Final verification with `TEST_FOCUS` unset and an empty `PYTHONPATH` passed: **306 passed, 0 failed**. The test runner uses the project virtual environment, temporary SQLite copies for all state-changing QA, and captured notification/email calls; it did not create live QA records or send real mail.

### Verification commands

- Focused validation checks: `TEST_FOCUS=B17,B23 .venv/Scripts/python.exe test_phase2_fixes.py` (run with an empty `PYTHONPATH`).
- The B23 validation block contains 20 assertions across manual/bulk interview scheduling, vacancy/direct-posting salaries, internal applications, contract drafts, Interview Policy, email configuration, and leave parsing. Email is stubbed in this block and the database copy is removed afterward.

### Pending policy confirmation

- A requested maximum scheduling horizon of **365 days** has not been applied because it is a business policy choice. Current confirmed behavior allows any future datetime that satisfies the established weekend and working-hours rules; the next minute is valid.

## Team

| Member | Role |
|--------|------|
| Chan Han Yue | Full Stack Development, Database Design, AI Integration |
| Yap Kar Sheng | Co-developer — assign modules as needed |

**Supervisor:** TARUC Faculty of Computing & Information Technology

---

## Bug Fixes

| Issue | Fix |
|-------|-----|
| Sidebar collapse sharing one localStorage key | Changed to DOM index-based keys (`nav-{uid}-sec-{idx}`) |
| Bulk bar overlapping table rows | `updateBulkBar()` adds `padding-bottom: 70px` when bar is shown |
| Individual Approve button submitting bulk form | Replaced nested `<form>` with JS `approveSingle(id)` |
| `ValueError` on empty query string params | Changed `int(request.args.get('key', default))` to `int(request.args.get('key') or default)` |
| Duplicate `year` param in filter form | Removed redundant hidden `<input name="year">` |
| MyKad guilloche hallucination (Tesseract noise) | Added EasyOCR fallback for name + address; overrides Tesseract when they differ |
| Address postcode lost due to `break` on stop_words | Changed `break` → `continue` so postcode after stop-words is captured |
| Suburb name filtered from address | Relaxed `len(words) >= 2` → `len(words) >= 1` |
| Address garbage from EasyOCR trailing line | Added lowercase-first alpha filter + `PERSEKUTUAN` stop-word |
| Tesseract address leading junk not cleaned | Added `^[^A-Za-z0-9]+` leading non-alphanumeric strip |
| Candidate selection preferred shorter noisy Tesseract over clean EasyOCR | Changed tiebreak from `len()` to `-alpha_cnt` |
| EasyOCR `JALAN 1/378` misread (8 vs B) | Added `JALAN 1/378` → `JALAN 1/37B` fix |
| Address trailing junk `.,` and ` 2 222. )` | Added orphaned punctuation removal + expanded trailing regex |
| Dashboard toast only showing for pending leaves | Added missing invoice toast + animation list; extracted counts from UNION query |
| Notification Type constraint failure (`Offer`, `Application`) | Added types to `Notification.type` CHECK constraint |
| `get_notifications` wrong column names (500 error) | Fixed `proposed_pct`→`increment_pct`, `lt.name`→`lt.type_name`, `i.amount`→`i.total_amount` |
| Header dropdown priority (system first) | Pinned approval/pending items above system notifications |
| App 27 appearing as new every poll cycle | Checked `create_application_from_email()` return before `new_apps++` |
| App 30 posting_id=NULL (position match) | Reversed LIKE direction: `WHERE ? LIKE '%' \|\| lower(title) \|\| '%'` |
| Context processor: `Leave_Request` table not found | Renamed to `Leave_Application` |
| Context processor: missing keys on fallback | Added all keys to both fallback dicts |
| Context processor: `i.company_id` missing | Added JOIN Employee for company filter |
| Dashboard `NoneType` crash in attendance sum | Used `rejectattr` for `hours_worked` |
| Face registration: `face_encoding_id` → `encoding_id` | Matched schema column name |
| PBKDF2 import fail on newer `cryptography` | Updated import path |
| numpy bool/float JSON serialization | Cast to Python native types |

---

## Important Notes

1. Set a strong, private `SECRET_KEY` in `.env` before any real deployment; do not rely on the development fallback.
2. The `uploads/` folder should **not** be publicly accessible — serve through Flask with auth check
3. `instance/smarthr.db` should be backed up regularly
4. PDPA compliance: all data stays on local server, no cloud calls are made

---

## Developer Documentation

- `AGENTS.md` — rules for AI coding agents working on SmartHR.
- `ARCHITECTURE.md` — current SmartHR architecture and protected system behavior.
- `DECISIONS.md` — architecture and product decision records.
- `CHANGELOG.md` — meaningful project changes over time.
