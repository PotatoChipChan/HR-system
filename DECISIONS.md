# SmartHR Architecture Decision Records

Lightweight decision log for SmartHR. All entries use **Observed** status since no pre-existing ADRs exist. Changes to security-critical decisions require explicit confirmation.

---

## DEC-001: Flask App Factory Pattern

### Status
Observed

### Context
SmartHR needs a standard Flask application setup that supports testing, configuration flexibility, and proper package organization.

### Decision
The application uses the factory pattern via `create_app()`. Templates and static files live outside the `app/` package (`template_folder='../templates'`). Configuration is instance-relative (`instance_relative_config=True`). Extensions (db, mail, limiter) are initialized in `app/extensions.py` and bound to the app in `create_app()`.

### Rationale
Flask's recommended pattern for testable applications. Allows multiple app instances with different configs. Keeps the package self-contained.

### Consequences
- Easy to test with different configurations.
- Extensions are app-bound, not module-level globals.
- Template paths outside the package require explicit `template_folder` argument.

### Agent Rule
`Agent may modify`

### References
- `app/__init__.py` — `create_app()` factory
- `app/extensions.py` — `db`, `mail`, `limiter`, `csrf` initialization
- `config.py` — configuration classes
- `run.py` — application entry point

---

## DEC-002: SQLite Database

### Status
Observed

### Context
SmartHR needs a lightweight, file-based relational database for a single-tenant HR system.

### Decision
SQLite is the sole database engine. The database file is at `instance/smarthr.db`. Connections are created per-request on `flask.g` and closed at teardown. Helper functions `query()` and `execute()` in `database.py` wrap cursor operations. `PRAGMA foreign_keys=ON` is enforced on every connection. The schema contains 28 tables covering employees, payroll, recruitment, attendance, leave, and more.

### Rationale
Zero-dependency deployment. No separate database server required. Sufficient for single-tenant, low-concurrency HR applications.

### Consequences
- No concurrent write support (SQLite serializes writes).
- No stored procedures or advanced SQL features.
- File-level backup is straightforward.
- Foreign key enforcement requires explicit PRAGMA per connection.

### Agent Rule
`Agent may modify`

### References
- `database.py` — `get_db()`, `query()`, `execute()`, PRAGMA enforcement
- `instance/smarthr.db` — database file
- `schema.sql` — DDL statements (28 tables)
- `app/__init__.py` — per-request connection lifecycle

---

## DEC-003: Session-Based Authentication

### Status
Observed

### Context
SmartHR needs user authentication with session management. JWT was rejected for simplicity and CSRF protection.

### Decision
Authentication uses Flask's built-in session mechanism (server-side cookies). Default session lifetime is 2 hours. "Remember Me" extends lifetime to 7 days. Cookies are `HttpOnly=True` and `SameSite=Lax` always. `Secure` flag is opt-in (enabled in production via `SESSION_COOKIE_SECURE`).

### Rationale
Flask sessions are simple, require no external token store, and integrate with CSRF protection. Server-side sessions avoid token storage complexity. `SameSite=Lax` prevents CSRF without breaking navigation links.

### Consequences
- Session data stored in signed cookie (not encrypted) — no sensitive data in session dict.
- Server-side session stores would require additional infrastructure.
- Session fixation protection handled by `session.clear()` on login.
- No token refresh mechanism — sessions expire or persist via Remember Me.

### Agent Rule
`STOP AND ASK before changing`

### References
- `app/__init__.py` — `app.secret_key`, `SESSION_COOKIE_*` settings
- `auth/routes.py` — login, logout, session lifecycle
- `config.py` — `PERMANENT_SESSION_LIFETIME`, `SESSION_COOKIE_SECURE`

---

## DEC-004: Decorator-Based Role Checks

### Status
Observed

### Context
SmartHR has 5 user roles with hierarchical permissions. Route-level access control must be enforced.

### Decision
Access control uses two decorators: `login_required` (authentication) and `role_required(*roles)` (authorization). Role hierarchy is implicit: HR Manager inherits Admin and HR access. The 5 roles are: Admin, HR, HR Manager, Manager, Employee.

### Rationale
Decorator-based checks are explicit and readable. Implicit hierarchy reduces redundant role lists. No external permission library needed.

### Consequences
- Hierarchy is enforced in the decorator, not the database — changes require code updates.
- New roles must be added to both the database enum and the decorator logic.
- Some routes may need additional business-logic checks beyond role checks.

### Agent Rule
`STOP AND ASK before changing`

### References
- `auth/decorators.py` — `login_required()`, `role_required()`, hierarchy logic
- `auth/routes.py` — route-level decorator usage
- `app/templates/layout.html` — role-based menu rendering

---

## DEC-005: Branch/Department Manager Scoping

### Status
Observed

### Context
Managers and department managers should only see data for their branch or department, not the entire organization.

### Decision
Session variables `session['is_dept_manager']`, `session['managed_dept_id']`, and `session['branch_id']` control data scoping. Every query that returns user-visible data applies scope filters based on these session values. Department managers see only their department. Branch managers see only their branch. Recruitment application badges and pending-approval counts use the same scope as the Applications page.

### Rationale
Data isolation is enforced at the query level, not the UI level. Session variables are set at login and reused across all routes.

### Consequences
- Every data-retrieval query must check session scope — forgetting a filter leaks data.
- Scope is fixed per login session — changing a manager's assignment requires re-login.
- Cross-branch/cross-department data access is impossible for scoped users.

### Agent Rule
`STOP AND ASK before changing`

### References
- `auth/routes.py` — session variable assignment on login
- `database.py` — scope filter patterns
- `app/routes/*.py` — query-level filtering in all data routes
- `app/templates/layout.html` — role-based navigation rendering

---

## DEC-006: CSRF + Rate Limiting

### Status
Observed

### Context
SmartHR needs protection against cross-site request forgery and brute-force attacks without external dependencies.

### Decision
**CSRF**: Session-based token, HMAC timing-safe comparison, no external dependencies. Token is generated per-session and embedded in all forms. A JS interceptor in `templates/base.html` attaches the token to state-changing requests via three mechanisms: a `submit`-event listener (capture phase) for standard form posts, a patched `HTMLFormElement.prototype.submit` for programmatic `form.submit()` calls (which bypass the `submit` event), and `fetch`/`XMLHttpRequest` wrappers that add the `X-CSRF-Token` header. **Rate limiting**: In-memory sliding window, per-IP, per-endpoint. Auth endpoints: 10/min login, 5/min password reset. Global: 100/min for other requests.

### Rationale
In-house CSRF implementation avoids dependency on Flask-WTF for CSRF alone. In-memory rate limiting avoids Redis/Memcached dependency for a single-process app.

### Consequences
- Rate limiting state is lost on server restart (in-memory).
- Multi-process deployments would need shared rate-limit state.
- CSRF token is session-bound — clearing cookies invalidates tokens.
- Timing-safe comparison prevents timing attacks.

### Agent Rule
`STOP AND ASK before changing`

### References
- `app/__init__.py` — CSRF token generation, rate limiter setup
- `auth/routes.py` — rate limit enforcement on login/reset
- `app/templates/*.html` — CSRF token in hidden form fields
- `config.py` — rate limit thresholds

---

## DEC-007: Face Encoding Encryption

### Status
Observed

### Context
Face attendance stores biographic encodings. These are sensitive biometric data requiring encryption at rest.

### Decision
Face encodings are encrypted with AES-256-GCM via the `cryptography` library. A master key is derived from the `FACE_ENCRYPTION_KEY` environment variable using PBKDF2-SHA256 (100,000 iterations). A dev fallback key is used when the env var is not set. An `is_encrypted()` shim detects whether a stored encoding is plaintext or ciphertext for migration compatibility.

### Rationale
AES-256-GCM provides authenticated encryption (confidentiality + integrity). PBKDF2 key derivation slows brute-force attacks. The `is_encrypted()` shim allows gradual migration from plaintext storage.

### Consequences
- Losing the encryption key makes all face encodings unrecoverable.
- Dev fallback key is insecure — must be overridden in production.
- Migration shim adds complexity to all face-related queries.
- Encryption/decryption adds minor CPU overhead per face check.

### Agent Rule
`STOP AND ASK before changing`

### References
- `face_attendance.py` — `encrypt_face_encoding()`, `decrypt_face_encoding()`, `is_encrypted()`
- `config.py` — `FACE_ENCRYPTION_KEY` env var
- `app/routes/face_attendance.py` — encryption/decryption in attendance routes

---

## DEC-008: 3-Failure Face Attendance Fallback

### Status
Observed

### Context
Face recognition can fail due to lighting, angles, or camera issues. Users need a fallback to avoid being unable to check in.

### Decision
A session counter `session['biometric_checkin_failures']` tracks consecutive face recognition failures. After 3 failures, manual attendance (check-in or check-out) becomes available: the active `/face/attendance` flow renders a Manual Check-Out request when the employee has an open check-in (today or the previous calendar day) and a Manual Check-In request otherwise. Manual entries are flagged as `manual=True` and require HR approval before being finalized.

### Rationale
Prevents users from being locked out due to biometric failures. HR approval for manual entries provides audit trail and prevents abuse.

### Consequences
- Counter resets on a successful face check-in, face check-out, or accepted manual entry.
- Manual entries require HR review, adding workflow overhead.
- Counter is session-based — switching browsers resets the count.
- No maximum on manual entries per day — potential for abuse without HR vigilance.

### Agent Rule
`Agent may modify`

### References
- `app/routes/face_attendance.py` — failure counter, manual fallback logic
- `app/routes/hr_dashboard.py` — manual entry approval
- `app/templates/hr_dashboard.html` — pending manual entries UI

---

## DEC-009: Recruitment AI Auto-Shortlisting

### Status
Observed

### Context
Recruitment involves many applications. Manual shortlisting is time-consuming.

### Decision
A 3-factor scoring algorithm auto-shortlists candidates: keyword coverage (50%), skills match (30%), and depth analysis (20%). Threshold is >60 for auto-shortlist. Applied to public_apply, internal_apply, and email-monitor paths. Cover letter must be >100 characters for scoring; shorter letters skip AI scoring.

### Rationale
Automated shortlisting reduces HR workload. Weighted scoring emphasizes keyword coverage (direct job requirements) over depth (experience richness). Minimum cover letter length ensures sufficient text for meaningful analysis.

### Consequences
- Candidates with short cover letters are not scored (may miss qualified candidates).
- Keyword-heavy resumes may score higher than well-written ones.
- Threshold is hardcoded — tuning requires code changes.
- No human-in-the-loop for borderline candidates (55-60 range).

### Agent Rule
`Agent may modify`

### References
- `recruitment.py` — `calculate_score()`, `auto_shortlist()`
- `app/routes/public_careers.py` — public application path
- `app/routes/employee_dashboard.py` — internal application path
- `email_monitor.py` — email application path

---

## DEC-010: Vacancy Request → Posting Approval Flow

### Status
Observed

### Context
Vacancies should not be posted directly. Managers request, and HR/Admin approves before public posting.

### Decision
Managers submit vacancy requests with position details. Admin, HR, or HR Manager approves or rejects. Approved requests automatically create a `Job_Posting` with `target_audience` inherited from the request. Custom positions can optionally be promoted to the Position Catalog on approval.

### Rationale
Ensures vacancies are vetted before public posting. Promotes catalog usage while allowing flexibility for new positions. Maintains audit trail from request to posting.

### Consequences
- Approval is a gate — vacancies cannot be posted without it.
- Catalog promotion is optional — custom titles may persist if not promoted.
- Approval state transitions are linear: Pending → Approved/Rejected.
- No appeal mechanism for rejected requests.

### Agent Rule
`STOP AND ASK before changing`

### References
- `app/routes/hr_dashboard.py` — vacancy request approval, posting creation
- `app/routes/manager_dashboard.py` — vacancy request submission
- `database.py` — `vacancy_requests` and `job_postings` table queries
- `app/templates/hr_dashboard.html` — approval UI

---

## DEC-011: Merged Applications Page

### Status
Observed

### Context
HR needs a unified view of all job applications (internal and external) with filtering and bulk actions.

### Decision
A single page displays all applications with status dropdown (New, Shortlisted, Interview, Rejected, All). Default view is Shortlisted. Type filter (Internal, External, All) and job/branch filters are available. Dropdown changes auto-submit the form (no Filter button). Shortlisted view enables bulk selection and auto-assign interviews.

### Rationale
Single page reduces navigation complexity. Auto-submit provides instant feedback. Defaulting to Shortlisted focuses HR on actionable candidates.

### Consequences
- Auto-submit on dropdown change may cause unintended filter changes.
- Bulk interview assignment requires date/time input — not fully automated.
- No pagination — performance may degrade with many applications.
- Status transitions are one-way in the UI (no undo).

### Agent Rule
`Agent may modify`

### References
- `app/routes/hr_dashboard.py` — applications page route
- `app/templates/hr_dashboard.html` — applications table, filters, bulk actions
- `database.py` — application query helpers

---

## DEC-012: Position Catalog

### Status
Observed

### Context
Free-text job titles lead to inconsistencies (e.g., "Software Dev" vs "Software Developer"). A catalog enforces naming standards.

### Decision
Departments have named positions stored in a catalog. Vacancy requests can reference catalog entries or use custom titles. Custom titles are optionally promoted to the catalog on approval. This prevents arbitrary posting titles.

### Rationale
Catalog-based positions ensure consistency across postings, payroll, and reporting. Custom titles allow flexibility for new or unusual positions.

### Consequences
- Catalog must be maintained — new positions require admin action.
- Custom titles may persist if not promoted, reducing consistency.
- No bulk import of catalog entries — manual creation only.
- Catalog changes affect all future vacancy requests referencing that department.

### Agent Rule
`Agent may modify`

### References
- `database.py` — `position_catalog` table queries
- `app/routes/hr_dashboard.py` — catalog management, vacancy request validation
- `app/templates/hr_dashboard.html` — catalog UI

---

## DEC-013: Notification Architecture

### Status
Observed

### Context
Users need real-time awareness of pending approvals, new applications, and system events.

### Decision
Three notification layers: (1) `Notification` database table for persistence, (2) header dropdown for quick access, (3) sidebar badges and toast popups for urgency. `inject_notifications()` context processor runs on every request, injecting pending counts and recent notifications into all templates. Header dropdown shows approval items, pending items, and system notifications (max 8). Email notifications via Flask-Mail with HTML templates for 13 notification types.

### Rationale
DB-persistent notifications survive page refreshes. Context processor ensures every page has current counts. Email provides out-of-band notification for critical events.

### Consequences
- `inject_notifications()` runs on every request — performance impact on high-traffic pages.
- 13 email templates must be maintained for each notification type.
- No WebSocket/polling for real-time updates — users must refresh.
- Toast notifications are JS-based — no persistence beyond session.

### Agent Rule
`Agent may modify`

### References
- `app/__init__.py` — `inject_notifications()` context processor
- `database.py` — notification query helpers
- `app/templates/layout.html` — header dropdown, sidebar badges
- `email_templates/` — 13 HTML email templates
- `app/routes/hr_dashboard.py` — notification creation triggers

---

## DEC-014: Payroll Scheduler

### Status
Observed

### Context
Payroll must be auto-generated for active employees on a regular schedule. Manual generation is error-prone.

### Decision
A background daemon thread runs on app startup with a 24-hour interval and 30-second initial delay. It auto-generates Draft payrolls for all active employees. Computation includes: base salary, overtime, invoice claims, bonus, leave adjustment, and statutory deductions (EPF, SOCSO, EIS, PCB). Only Draft payrolls are regenerated — Finalised and Paid payrolls are never touched.

### Rationale
Daemon thread avoids external scheduler dependency (Celery, cron). 24-hour interval aligns with monthly payroll cycles. Draft-only regeneration protects finalized data.

### Consequences
- Thread dies on app restart — next startup regenerates Draft payrolls.
- No retry mechanism on computation errors — failures are silent.
- 30-second initial delay may cause blank payroll on first visit after restart.
- Thread-safe SQLite access is handled by per-request connections, not the scheduler.

### Agent Rule
`STOP AND ASK before changing`

### References
- `payroll_scheduler.py` — `start_scheduler()`, `generate_draft_payrolls()`
- `app/__init__.py` — scheduler thread startup
- `payroll.py` — `calculate_payroll()`, deduction logic
- `database.py` — payroll table queries

---

## DEC-015: Test Conventions

### Status
Observed

### Context
SmartHR needs a consistent testing approach. Pytest was rejected due to PIL import conflicts with the global PYTHONPATH.

### Decision
Tests use `app.test_client()` with manual `check()` assertion functions. No pytest fixtures. Regression suite `test_phase2_fixes.py` contains cases B1-B23 and redirects the database path to a temporary snapshot before any fixtures execute. Verification command: `PYTHONPATH= .venv/Scripts/python.exe test_phase2_fixes.py`.

### Rationale
Avoids PIL import conflicts caused by global PYTHONPATH pointing at the Hermes venv. Manual assertions are explicit and debuggable. Test client provides full request/response cycle testing.

### Consequences
- No parallel test execution — tests run sequentially.
- No fixture reuse — setup/teardown is manual in each test.
- Verification requires explicit PYTHONPATH override — easy to forget.
- No coverage reporting — test completeness is manual.

### Agent Rule
`Agent may modify`

### References
- `test_phase2_fixes.py` — B1-B23 regression suite
- `AGENTS.md` — test runner instructions
- `.venv/Scripts/python.exe` — project Python interpreter

---

## DEC-016: Vanilla Design System

### Status
Observed

### Context
SmartHR needs a consistent UI without JavaScript framework overhead. No React, Vue, or Angular dependencies.

### Decision
Design system uses DM Sans and Space Grotesk fonts. CSS custom properties define a green palette (`--g50` to `--g800`). Component-based class naming (`.card`, `.tbl`, `.badge`, `.btn`, `.form-input`). Responsive: mobile sidebar slides off-screen with hamburger toggle. No JavaScript framework — vanilla JS only.

### Rationale
Zero build step. Fast page loads. No framework lock-in. CSS custom properties enable theme customization. Component classes ensure visual consistency.

### Consequences
- No component reuse across pages — copy-paste HTML.
- No reactive UI — form submissions cause full page reloads.
- Mobile responsiveness requires manual CSS media queries.
- No accessibility audit tooling — a11y is manual.

### Agent Rule
`Agent may modify`

### References
- `static/css/` — stylesheet files
- `static/js/` — vanilla JavaScript
- `app/templates/layout.html` — base template, sidebar, responsive toggle
- `app/templates/components/` — reusable HTML snippets (cards, tables, forms)

---

## DEC-017: Email Monitoring for Recruitment

### Status
Observed

### Context
Candidates apply via email. Manual checking is tedious and slow. Automated monitoring is needed.

### Decision
IMAP polling runs every 30 seconds for new application emails. Multi-parser architecture: structured body parser, subject template parser, and regex fallback. Promotional email detection uses multi-signal filtering to reject non-application emails. Auto-creates `Job_Application` records with AI scoring via DEC-009.

### Rationale
Email is a common application channel. Multi-parser approach handles varying email formats. Promotional filtering prevents spam from polluting the application pool.

### Consequences
- IMAP connection may timeout or fail — no automatic reconnection logic documented.
- 30-second polling interval may miss rapid sequences of applications.
- Regex fallback may misparse non-standard email formats.
- Promotional filter may reject legitimate applications with promotional language.

### Agent Rule
`Agent may modify`

### References
- `email_monitor.py` — IMAP polling, parsers, promotional filter
- `recruitment.py` — `calculate_score()`, application creation
- `database.py` — `job_applications` table queries
- `config.py` — IMAP credentials, polling interval

---

## DEC-020: Branch-Dependent Interviewer Assignment

### Status
Approved

### Context
Physical interviews need an on-site panel that can actually attend, but the old eligibility list was company-wide for every format, so a physical interview on one branch could be booked with interviewers assigned to another branch. Vacancy requests also carry the identity of the requesting manager, who was not surfaced in scheduling UIs.

### Decision
- **Physical** interviewers are restricted to active eligible employees physically assigned to the posting branch (`Job_Posting.branch_id` is authoritative): branch Managers plus Admin/HR/HR Manager who are themselves assigned to that branch.
- **Virtual** interviewers keep the company-wide roster: Managers of the posting branch plus Admin/HR/HR Manager company-wide.
- The manager who requested the posting via the **earliest approved vacancy request** (created_at ASC, request_id ASC tie-break; must still be active) is listed first in scheduling UIs with a "(Requester)" label and is prioritised first by auto-assign. Requester priority never adds an otherwise ineligible person: the requester is only included when active and, for Physical, assigned to the posting branch. Direct postings (no approved request) never have a requester.
- A posting whose branch has no local interviewers cannot be scheduled **Physical** — manual, bulk, and auto-assign all block with "This branch has no eligible local interviewers. Schedule as Virtual instead." and the UI disables the Physical option with an inline hint.
- Every scheduling path (manual, bulk, auto-assign preview/confirm) validates the submitted interviewer set against the format-specific pool server-side; forged/inactive IDs still hit the original "active interviewers from your company" check.
- **HR Manager** may use auto-assign but is denied manual and bulk scheduling (route-level guards in `schedule_interview` and `bulk_schedule`; the shared `role_required` decorator intentionally continues to grant HR Manager the Admin/HR role set).
- Interviewers remain optional (existing `Interview.interviewer_ids` nullable TEXT). No schema change.

### Rationale
Format-specific pools make physical panels physically co-located with the interview venue while keeping virtual panels broad; surfacing the requester gives HR the intended panel lead without changing the first-available auto-assign scan.

### Consequences
- Scheduling forms, bulk-schedule page, and auto-assign restrict choices and reject out-of-pool submissions server-side.
- Branches with no local staff can only schedule Virtual interviews until staff are assigned.

### Agent Rule
`STOP AND ASK before changing`

### References
- `app/recruitment/routes.py` — `_get_eligible_interviewers`, `_requester_for_posting`, `schedule_interview`, `bulk_schedule`, `auto_assign_preview/confirm`, `view_application`
- `templates/recruitment/view_application.html`, `templates/recruitment/bulk_schedule.html` — requester label, disabled Physical option, branch-filtered interviewer selects

---

## DEC-021: Posting Soft Delete (Archive)

### Status
Approved

### Context
HR needed to remove a mistakenly created job posting, but hard-deleting rows is unsafe: applications, interviews, scorecards, recommendations, contracts and audit history reference the posting, and SQLite foreign keys with `ON DELETE CASCADE` would silently destroy them. The constraint that makes deletion safe is the presence of scheduled interviews.

### Decision
- Deletion is a **soft delete**: the posting's status becomes `Archived`, the row and all related records are preserved, and the posting is hidden from the active and closed posting lists.
- Postings auto-archive when the last approved opening is filled — `Archived` replaces `Filled` as the terminal state of a fully filled posting (`close_job_posting_for_application`); `closed_at` is kept and backfilled by the migration.
- A posting can be deleted **only when no interviews are scheduled** for any of its applications (server-side count check in the delete route; the UI button is confirm-dialog guarded).
- Delete permission is **Admin and HR Manager only** (route-level `role_required`); normal HR and below cannot delete. Archived postings never show the delete button.
- Archived postings stop accepting public applications (404 "This job posting is no longer accepting applications.") and internal applications; they remain viewable with an `Archived` badge.
- `migrate_job_posting_archive` (backup-guarded `Job_Posting` rebuild via the migration framework, idempotent) adds `Archived` to the status CHECK — including the columns introduced by `migrate_vacancy_openings` — and backfills existing `Filled` postings to `Archived`.
- The public schema (`schema.sql`) status CHECK includes `Archived` for fresh installs.

### Rationale
Soft delete preserves auditability and all downstream records while giving HR the intended "remove from view" behaviour; interviews are the only hard dependency that makes later deletion unsafe, so they are the sole guard.

### Consequences
- `Archived` postings no longer appear in any posting list, careers page, internal job board, bulk-schedule posting dropdown, or email-intake filters (all status filters treat `Archived` as closed).
- Running `init_db.py` once applies the migration to the live database with a timestamped backup.

### Agent Rule
`STOP AND ASK before changing`

### References
- `app/recruitment/routes.py` — `delete_posting`, `public_apply`, `internal_job_detail`
- `app/database.py` — `close_job_posting_for_application`
- `init_db.py` — `migrate_job_posting_archive`
- `templates/recruitment/view_posting.html` — delete form, Archived badge, Apply Now conditional
- `schema.sql` — `Job_Posting` status CHECK
- `test_phase2_fixes.py` — B35 regression coverage

---

## DEC-019: Recruitment Evaluation and Offer Gates

### Status
Approved

### Context
The recruitment workflow now has structured scorecards, score-based ranking, recorded selection confirmation, multi-opening reservations, and offer expiry. The previous Pass/Fail action could bypass those controls, and contract departments or offer permissions were not fully enforced by the server.

### Decision
- Interviews may be scheduled or rescheduled only for a future server-side datetime. The existing weekend, working-hours, leave, and interviewer-conflict rules remain in force. Manual and bulk scheduling enforce the same future/weekend/working-hours and active-interviewer validation server-side; browser date controls mirror the past-date restriction but do not replace it.
- A past scheduled interview is marked `Completed` without a pass/fail outcome. The legacy Pass/Fail endpoint remains only as a harmless compatibility response and never mutates application or interview state.
- A completed, evidence-backed fixed-criterion scorecard is required before a candidate can be selected. The candidate ranking is calculated solely from completed scorecard totals. Normal HR completes scorecards only; the HR Manager directly confirms candidates in ranking order — a candidate is confirmable while the number of candidates strictly above them is smaller than the posting's unfilled approved openings, so the top N candidates of an N-opening posting can be confirmed (ties at the cutoff stay eligible; the offer-send reservation gate still caps pending offers at unfilled openings). There is no manual recommendation or separate selection-approval step.
- A scorecard is recorded once per interview: the first recorded decision is final and cannot be updated (re-submission is rejected and the UI shows the existing scorecard read-only).
- Contract department is derived from `Job_Posting.department_id` on the server and is not user-selectable in the contract form.
- Only a `Draft` contract is editable. All subsequent offer states are displayed read-only in the editor route so the UI reflects the server-side immutability rule.
- Admin and HR Manager can send an offer directly after candidate-selection approval. Normal HR must request and receive a separate offer-send approval.
- `Interview`, `Offered`, and `Hired` are server-controlled states. They must not be accepted by the generic application-status endpoint; they are reached only through their dedicated, validated workflow actions.

### Rationale
These gates preserve a traceable HR decision flow while avoiding automatic rejection or hiring from a single interview action. Server enforcement prevents form tampering and makes role rules consistent across direct requests and UI visibility.

### Consequences
- Completed interviews need scorecards before the candidate can appear in score-based selection ranking.
- Normal HR completes scorecards but cannot manually select a candidate. HR Manager confirmation is bounded by the posting's unfilled approved openings (top N candidates, ties eligible); confirmed candidates proceed to the existing contract and offer controls, where the reservation gate enforces the opening cap.
- Historic Pass/Fail values remain visible as legacy data but do not drive current workflow transitions.
- A crafted status-change request cannot bypass the candidate-selection, offer-approval, reservation, or hire controls.

### Agent Rule
`STOP AND ASK before changing`

### References
- `app/recruitment/routes.py` — scheduling, completion, scorecards, score-based selection, contracts, offers
- `templates/recruitment/view_application.html` — action visibility and offer actions
- `templates/recruitment/contract.html` — posting-derived department display
- `test_phase2_fixes.py` — B17, B20, B21, B22, B23 regression coverage

---

## DEC-018: IC OCR Pipeline

### Status
Observed

### Context
HR needs to extract data from Malaysian IC (identity card) images for employee onboarding. ICs have guilloche patterns, watermarks, and varying orientations.

### Decision
Multi-engine OCR: Tesseract + EasyOCR. Auto-rotation tries 4 orientations × multiple PSM modes. Guilloche removal uses FFT frequency filtering to isolate text from background patterns. Watermark application adds SmartHR watermark to processed images. Vendor pattern learning stores extraction patterns for future reference.

### Rationale
Multi-engine OCR improves accuracy across different IC designs. Auto-rotation handles scanned documents in any orientation. FFT filtering removes decorative patterns that confuse OCR engines. Pattern learning reduces manual intervention over time.

### Consequences
- Tesseract and EasyOCR must be installed as system dependencies.
- FFT processing is CPU-intensive — may slow batch processing.
- Pattern learning requires initial training data — cold-start problem.
- Watermarking is irreversible — original images are modified.

### Agent Rule
`Agent may modify`

### References
- `ic_ocr.py` — OCR pipeline, FFT filtering, rotation, watermark
- `app/routes/hr_dashboard.py` — IC upload routes
- `app/templates/hr_dashboard.html` — IC upload UI
- `vendor_patterns.json` — learned extraction patterns

---

## DEC-020: Direct Posting Form — Branch Filtering and Department Visibility

### Status
Approved

### Context
The direct job-posting form (`/recruitment/postings/add`, Admin/HR Manager) listed only departments that already had an active catalog position, listed them unfiltered across branches, and had no way to set the number of openings — while the vacancy-request → approval path supported openings (1–50). A newly created department (e.g. "Branch Manager" for the Kepong branch) was invisible in the posting form, the Department Manager Assignments container, and the Position Catalog, blocking manual postings for it.

### Decision
1. The posting form's Department dropdown lists **every** company department, including departments with no active catalog positions; selecting one without positions shows an inline hint pointing to the Position Catalog. Server-side rules are unchanged: a valid active catalog position is still required and branch, department, position, and company must agree.
2. Selecting a Branch client-side filters the Department dropdown to that branch's departments (other-branch options are hidden and disabled; a now-mismatched selection is cleared). Field order is Branch + Employment Type, then Department + Job Title.
3. The Department Manager Assignments container lists **all** departments via LEFT JOIN, rendering an `Unassigned` badge when no manager is set.
4. The Position Catalog names departments with zero active positions in a "Departments without catalog positions" hint that directs users to the "+ New Position Title" form.
5. The direct posting form gains a **Number of Openings** field (1–50, validated server-side with the same bounds and message as vacancy requests). The posting stores it in `approved_openings`; when omitted it defaults to 1, and the existing opening-ledger lifecycle (reserve → fill → Partially Filled → Filled) applies unchanged.

### Rationale
- Visibility of empty departments removes a dead end: HR can now select the new department and is guided to add the missing catalog title instead of the dropdown silently omitting it.
- Branch-first dependent filtering mirrors the department form's existing manager filtering pattern (`filterManagers()`) and reduces cross-branch mistakes client-side while the server stays authoritative.
- The `Unassigned` badge and empty-department hint make new org units visible immediately instead of hiding them until data catches up.
- Manual postings should offer the same openings capability as approved vacancy requests, since the opening ledger already drives offers/hires; a default of 1 preserves existing single-opening behavior.

### Consequences
- The posting form now lists departments without positions; submissions for them still fail server-side with the catalog-position flash until a title is added.
- `approved_openings` on manually created postings participates in `_openings_available()` exactly like approval-created postings.
- New B30 regression coverage asserts form attributes, empty-department visibility, Unassigned rendering, the empty-departments hint, openings validation (0/51/non-numeric rejected), default of one opening, multi-opening persistence, and crafted branch/department mismatch rejection.

### Agent Rule
`STOP AND ASK before changing`: opening-ledger accounting, vacancy-request openings bounds, posting creation validation order, or the catalog-only title rule.

### References
- `app/recruitment/routes.py` — `add_posting()` GET/POST, `vacancy_request()` openings bounds, `_openings_available()`
- `templates/recruitment/add_posting.html` — branch/department dependent filtering, openings input
- `app/organization/routes.py` — `roles()` department-manager and empty-department queries
- `templates/organization/role_list.html` — Department Manager Assignments, Position Catalog
- `test_phase2_fixes.py` — B30 regression block
