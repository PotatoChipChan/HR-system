-- =============================================================================
-- SmartHR – SQLite Schema (converted from hr_system_erd_v3.sql)
-- SQLite differences: no ENUM, no AUTO_INCREMENT, no JSON type, no ON UPDATE
-- =============================================================================
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS Role (
    role_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    role_name TEXT NOT NULL UNIQUE  -- 'Admin', 'HR', 'Manager', 'Employee'
);

-- Predefined job-title catalog, one entry per department.
-- position_name must be stored already-trimmed with collapsed inner whitespace
-- (CHECK backstop; application normalizes before every insert).
-- Duplicates are rejected case-insensitively at the DB level (COLLATE NOCASE).
CREATE TABLE IF NOT EXISTS Position (
    position_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    position_name TEXT NOT NULL CHECK(TRIM(position_name) = position_name
                                      AND position_name NOT LIKE '%  %'
                                      AND length(TRIM(position_name)) > 0),
    department_id INTEGER NOT NULL,
    is_active     INTEGER DEFAULT 1,
    created_at    TEXT DEFAULT (datetime('now')),
    UNIQUE (department_id, position_name COLLATE NOCASE),
    FOREIGN KEY (department_id) REFERENCES Department(department_id)
);

CREATE INDEX IF NOT EXISTS idx_position_active     ON Position(is_active);
CREATE INDEX IF NOT EXISTS idx_position_dept_active ON Position(department_id, is_active);

CREATE TABLE IF NOT EXISTS Company (
    company_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    address     TEXT,
    contact_no  TEXT,
    email       TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS Branch (
    branch_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL,
    name          TEXT NOT NULL,
    address       TEXT,
    address_line1 TEXT,
    address_line2 TEXT,
    city          TEXT,
    state         TEXT,
    postal_code   TEXT,
    contact_no    TEXT,
    hr_manager_id INTEGER,
    parent_branch_id INTEGER,
    FOREIGN KEY (company_id) REFERENCES Company(company_id),
    FOREIGN KEY (parent_branch_id) REFERENCES Branch(branch_id)
);

CREATE TABLE IF NOT EXISTS Department (
    department_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id             INTEGER NOT NULL,
    department_name       TEXT NOT NULL,
    department_manager_id INTEGER,
    FOREIGN KEY (branch_id)             REFERENCES Branch(branch_id),
    FOREIGN KEY (department_manager_id) REFERENCES Employee(employee_id)
);

CREATE TABLE IF NOT EXISTS Employee (
    employee_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id             INTEGER NOT NULL,
    branch_id              INTEGER NOT NULL,
    department_id          INTEGER NOT NULL,
    full_name              TEXT NOT NULL,
    ic_number              TEXT UNIQUE,
    passport_number        TEXT,
    contact_no             TEXT,
    address                TEXT,
    date_of_birth          TEXT,
    gender                 TEXT CHECK(gender IN ('Male','Female','Other')),
    marital_status         TEXT, -- 'Single', 'Married', 'Divorced', 'Widowed'
    emergency_contact_name TEXT,
    emergency_contact_no   TEXT,
    position               TEXT,
    position_id            INTEGER REFERENCES Position(position_id),
    employment_type        TEXT NOT NULL DEFAULT 'Full-Time'
                               CHECK(employment_type IN ('Full-Time','Part-Time','Contract')),
    employment_status      TEXT DEFAULT 'Active'
                               CHECK(employment_status IN ('Active','On Leave','Inactive','Terminated')),
    hire_date              TEXT NOT NULL,
    base_salary            REAL NOT NULL DEFAULT 0.00,
    role_id                INTEGER NOT NULL,
    email                  TEXT NOT NULL UNIQUE,
    personal_email         TEXT,
    password_hash          TEXT NOT NULL,
    is_active              INTEGER DEFAULT 1,
    failed_attempts        INTEGER DEFAULT 0,
    locked_until           TEXT,
    last_login             TEXT,
    id_document_path       TEXT, -- Path to watermarked IC/Passport
    work_start_time        TEXT DEFAULT '09:00',
    work_end_time          TEXT DEFAULT '18:00',
    created_at             TEXT DEFAULT (datetime('now')),
    updated_at             TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (company_id)    REFERENCES Company(company_id),
    FOREIGN KEY (branch_id)     REFERENCES Branch(branch_id),
    FOREIGN KEY (department_id) REFERENCES Department(department_id),
    FOREIGN KEY (role_id)       REFERENCES Role(role_id)
);

CREATE TABLE IF NOT EXISTS Face_Encoding (
    encoding_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id        INTEGER NOT NULL UNIQUE,
    face_encoding_blob BLOB NOT NULL,
    registered_at      TEXT DEFAULT (datetime('now')),
    updated_at         TEXT DEFAULT (datetime('now')),
    registered_by      INTEGER,
    FOREIGN KEY (employee_id)   REFERENCES Employee(employee_id) ON DELETE CASCADE,
    FOREIGN KEY (registered_by) REFERENCES Employee(employee_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS Attendance (
    attendance_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id      INTEGER NOT NULL,
    branch_id        INTEGER NOT NULL,
    check_in         TEXT NOT NULL,
    check_out        TEXT,
    hours_worked     REAL,
    overtime_hours   REAL DEFAULT 0.00,
    confidence_score REAL,
    status           TEXT DEFAULT 'Pending' CHECK(status IN ('Pending','Approved','Flagged')),
    is_manual_entry  INTEGER DEFAULT 0,
    manual_reason    TEXT,
    corrected_by     INTEGER,
    corrected_at     TEXT,
    created_at       TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (employee_id)  REFERENCES Employee(employee_id),
    FOREIGN KEY (branch_id)    REFERENCES Branch(branch_id),
    FOREIGN KEY (corrected_by) REFERENCES Employee(employee_id)
);

CREATE TABLE IF NOT EXISTS Attendance_Request (
    request_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL,
    request_date    TEXT NOT NULL,
    check_in_time   TEXT NOT NULL,
    check_out_time  TEXT,
    reason          TEXT NOT NULL,
    system_evidence TEXT,
    status          TEXT DEFAULT 'Pending' CHECK(status IN ('Pending','Approved','Rejected')),
    reviewed_by     INTEGER,
    reviewed_at     TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (employee_id) REFERENCES Employee(employee_id),
    FOREIGN KEY (reviewed_by) REFERENCES Employee(employee_id)
);

CREATE TABLE IF NOT EXISTS Invoice (
  invoice_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id    INTEGER NOT NULL,
  filename       TEXT NOT NULL,
  original_name  TEXT,
  file_type      TEXT NOT NULL CHECK(file_type IN ('image','pdf')),
  vendor_name    TEXT,
  invoice_number TEXT,
  invoice_date   TEXT,
  due_date       TEXT,
  currency       TEXT DEFAULT 'MYR',
  exchange_rate  REAL,
  subtotal       REAL,
  tax_amount     REAL DEFAULT 0.00,
  total_amount   REAL,
  total_amount_myr REAL,
  category       TEXT,
  description    TEXT,
  status         TEXT DEFAULT 'Pending' CHECK(status IN ('Pending','Approved','Rejected','Paid')),
  submitted_at   TEXT DEFAULT (datetime('now')),
  approved_by    INTEGER,
  approved_at    TEXT,
  rejection_reason TEXT,
  payroll_id       INTEGER,
  FOREIGN KEY (employee_id) REFERENCES Employee(employee_id),
  FOREIGN KEY (approved_by) REFERENCES Employee(employee_id),
  FOREIGN KEY (payroll_id)  REFERENCES Payroll(payroll_id)
);

CREATE TABLE IF NOT EXISTS OCR_Result (
    result_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id       INTEGER NOT NULL UNIQUE,
    raw_text         TEXT,
    extracted_data   TEXT,   -- JSON stored as text
    confidence_score REAL,
    ocr_engine       TEXT DEFAULT 'Tesseract',
    is_manual_review INTEGER DEFAULT 0,
    reviewed_by      INTEGER,
    reviewed_at      TEXT,
    created_at       TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (invoice_id)  REFERENCES Invoice(invoice_id) ON DELETE CASCADE,
    FOREIGN KEY (reviewed_by) REFERENCES Employee(employee_id)
);

-- Learned patterns for specific vendors to improve OCR over time
CREATE TABLE IF NOT EXISTS Vendor_Pattern (
    pattern_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_name    TEXT NOT NULL UNIQUE,
    inv_num_anchor TEXT,   -- e.g., "Ref No"
    date_anchor    TEXT,   -- e.g., "Issue Date"
    total_anchor   TEXT,   -- e.g., "Amount Due"
    category_hint  TEXT,
    occurrence_count INTEGER DEFAULT 1,
    last_updated   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS Leave_Type (
    leave_type_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    type_name        TEXT NOT NULL UNIQUE,
    default_days     INTEGER NOT NULL DEFAULT 0,
    is_paid          INTEGER DEFAULT 1,
    requires_document INTEGER DEFAULT 0,
    description      TEXT,
    eligible_genders TEXT, -- Comma-separated list of eligible genders (e.g., 'Male,Female' or 'Female' for maternity),
    eligible_marital_status TEXT -- Comma-separated list of eligible marital statuses (e.g., 'Single,Married' or NULL for any)
);

CREATE TABLE IF NOT EXISTS Leave_Entitlement (
    entitlement_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    leave_type_id    INTEGER NOT NULL,
    employment_type  TEXT NOT NULL CHECK(employment_type IN ('Full-Time','Part-Time','Contract')),
    min_service_years INTEGER NOT NULL DEFAULT 0,
    entitled_days    INTEGER NOT NULL DEFAULT 0,
    effective_year   INTEGER NOT NULL,
    UNIQUE (leave_type_id, employment_type, min_service_years, effective_year),
    FOREIGN KEY (leave_type_id) REFERENCES Leave_Type(leave_type_id)
);

CREATE TABLE IF NOT EXISTS Leave_Balance (
    balance_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id   INTEGER NOT NULL,
    leave_type_id INTEGER NOT NULL,
    year          INTEGER NOT NULL,
    entitled_days REAL NOT NULL DEFAULT 0.0,
    used_days     REAL NOT NULL DEFAULT 0.0,
    pending_days  REAL NOT NULL DEFAULT 0.0,
    UNIQUE (employee_id, leave_type_id, year),
    FOREIGN KEY (employee_id)   REFERENCES Employee(employee_id) ON DELETE CASCADE,
    FOREIGN KEY (leave_type_id) REFERENCES Leave_Type(leave_type_id)
);

CREATE TABLE IF NOT EXISTS Leave_Application (
    leave_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL,
    leave_type_id   INTEGER NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    total_days      REAL NOT NULL,
    reason          TEXT,
    supporting_doc  TEXT,
    status          TEXT DEFAULT 'Pending'
                        CHECK(status IN ('Pending','Approved','Rejected','Cancelled')),
    applied_at      TEXT DEFAULT (datetime('now')),
    reviewed_by     INTEGER,
    reviewed_at     TEXT,
    review_comment  TEXT,
    last_updated_by INTEGER,
    last_updated_at TEXT,
    FOREIGN KEY (employee_id)    REFERENCES Employee(employee_id),
    FOREIGN KEY (leave_type_id)  REFERENCES Leave_Type(leave_type_id),
    FOREIGN KEY (reviewed_by)    REFERENCES Employee(employee_id),
    FOREIGN KEY (last_updated_by) REFERENCES Employee(employee_id)
);

CREATE TABLE IF NOT EXISTS Payroll (
    payroll_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id      INTEGER NOT NULL,
    pay_period_month INTEGER NOT NULL,
    pay_period_year  INTEGER NOT NULL,
    base_salary      REAL NOT NULL DEFAULT 0.00,
    overtime_pay     REAL NOT NULL DEFAULT 0.00,
    commission       REAL NOT NULL DEFAULT 0.00,
    bonus            REAL NOT NULL DEFAULT 0.00,
    invoice_claims   REAL NOT NULL DEFAULT 0.00,
    salary_increment REAL NOT NULL DEFAULT 0.00,
    leave_adjustment REAL NOT NULL DEFAULT 0.00,
    gross_pay        REAL NOT NULL DEFAULT 0.00,
    epf_employee     REAL NOT NULL DEFAULT 0.00,
    epf_employer     REAL NOT NULL DEFAULT 0.00,
    socso_employee   REAL NOT NULL DEFAULT 0.00,
    socso_employer   REAL NOT NULL DEFAULT 0.00,
    eis_employee     REAL NOT NULL DEFAULT 0.00,
    eis_employer     REAL NOT NULL DEFAULT 0.00,
    pcb_tax          REAL NOT NULL DEFAULT 0.00,
    total_deductions REAL NOT NULL DEFAULT 0.00,
    net_pay          REAL NOT NULL DEFAULT 0.00,
    status           TEXT DEFAULT 'Draft' CHECK(status IN ('Draft','Finalised','Paid')),
    generated_by     INTEGER,
    generated_at     TEXT DEFAULT (datetime('now')),
    notes            TEXT,
    UNIQUE (employee_id, pay_period_month, pay_period_year),
    FOREIGN KEY (employee_id)  REFERENCES Employee(employee_id),
    FOREIGN KEY (generated_by) REFERENCES Employee(employee_id)
);

-- =============================================================================
-- Performance Review Module (Yearly)
-- =============================================================================

CREATE TABLE IF NOT EXISTS Performance_Review (
    review_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL,
    period_year     INTEGER NOT NULL,
    attendance_rate REAL DEFAULT 0,
    punctuality     REAL DEFAULT 0,
    overtime_score  REAL DEFAULT 0,
    reliability     REAL DEFAULT 0,
    composite_score REAL DEFAULT 0,
    grade           TEXT,
    generated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE (employee_id, period_year),
    FOREIGN KEY (employee_id) REFERENCES Employee(employee_id)
);

CREATE INDEX IF NOT EXISTS idx_perf_review_emp_year ON Performance_Review(employee_id, period_year);

-- =============================================================================
-- Bonus Policy Module (Yearly performance bonus rules)
-- =============================================================================

CREATE TABLE IF NOT EXISTS Bonus_Policy (
    policy_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id              INTEGER NOT NULL,
    year                    INTEGER NOT NULL,
    grade_A_months          REAL NOT NULL DEFAULT 3.0,
    grade_B_months          REAL NOT NULL DEFAULT 2.0,
    grade_C_months          REAL NOT NULL DEFAULT 1.0,
    grade_D_months          REAL NOT NULL DEFAULT 0.5,
    tenure_threshold_months INTEGER NOT NULL DEFAULT 3,
    payout_month            INTEGER NOT NULL DEFAULT 1,
    auto_propose            INTEGER NOT NULL DEFAULT 1,
    created_at              TEXT DEFAULT (datetime('now')),
    updated_at              TEXT DEFAULT (datetime('now')),
    UNIQUE (company_id, year),
    FOREIGN KEY (company_id) REFERENCES Company(company_id)
);

CREATE INDEX IF NOT EXISTS idx_bonus_policy_company_year ON Bonus_Policy(company_id, year);

-- =============================================================================
-- Bonus Proposal Module (Yearly)
-- =============================================================================

CREATE TABLE IF NOT EXISTS Bonus_Proposal (
    proposal_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id       INTEGER NOT NULL,
    period_year       INTEGER NOT NULL,
    composite_score   REAL,
    grade             TEXT,
    full_bonus_amount REAL NOT NULL,
    bonus_amount      REAL NOT NULL,
    months_worked     INTEGER NOT NULL DEFAULT 12,
    proposed_by       INTEGER NOT NULL,
    proposed_at       TEXT DEFAULT (datetime('now')),
    status            TEXT NOT NULL DEFAULT 'Pending'
                      CHECK(status IN ('Pending','Approved','Rejected')),
    reviewed_by       INTEGER,
    reviewed_at       TEXT,
    rejection_reason  TEXT,
    notified_at       TEXT,
    UNIQUE (employee_id, period_year),
    FOREIGN KEY (employee_id) REFERENCES Employee(employee_id),
    FOREIGN KEY (proposed_by) REFERENCES Employee(employee_id),
    FOREIGN KEY (reviewed_by) REFERENCES Employee(employee_id)
);

CREATE INDEX IF NOT EXISTS idx_bonus_emp_year ON Bonus_Proposal(employee_id, period_year);
CREATE INDEX IF NOT EXISTS idx_bonus_status ON Bonus_Proposal(status);

CREATE TABLE IF NOT EXISTS Payslip (
    payslip_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    payroll_id   INTEGER NOT NULL UNIQUE,
    employee_id  INTEGER NOT NULL,
    filename     TEXT NOT NULL,
    generated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (payroll_id)  REFERENCES Payroll(payroll_id) ON DELETE CASCADE,
    FOREIGN KEY (employee_id) REFERENCES Employee(employee_id)
);

CREATE TABLE IF NOT EXISTS Report (
    report_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_by INTEGER NOT NULL,
    report_type  TEXT NOT NULL CHECK(report_type IN ('Attendance','Invoice','Payroll','Leave','Headcount')),
    parameters   TEXT,
    file_path    TEXT,
    generated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (generated_by) REFERENCES Employee(employee_id)
);

CREATE TABLE IF NOT EXISTS AuditLog (
    audit_log_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id      INTEGER,
    action           TEXT NOT NULL,
    module_name      TEXT NOT NULL,
    description      TEXT,
    target_table     TEXT,
    target_record_id TEXT,
    action_status    TEXT NOT NULL DEFAULT 'Success' CHECK(action_status IN ('Success','Failed')),
    action_details   TEXT,   -- JSON stored as text
    ip_address       TEXT,
    user_agent       TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    is_archived      INTEGER NOT NULL DEFAULT 0,
    archived_at      TEXT,
    retention_until  TEXT,
    FOREIGN KEY (employee_id) REFERENCES Employee(employee_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS Notification (
    notification_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id        INTEGER NOT NULL,
    title              TEXT NOT NULL,
    message            TEXT,
    type               TEXT NOT NULL CHECK(type IN ('Info','Success','Warning','Error','Offer','Application')),
    is_read            INTEGER NOT NULL DEFAULT 0,
    related_url        TEXT,
    created_at         TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (employee_id) REFERENCES Employee(employee_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS IC_Access_Request (
    request_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id        INTEGER NOT NULL,
    target_employee_id  INTEGER NOT NULL,
    reason              TEXT,
    status              TEXT DEFAULT 'Pending' CHECK(status IN ('Pending','Approved','Rejected','Expired')),
    requested_at        TEXT DEFAULT (datetime('now')),
    reviewed_by         INTEGER,
    reviewed_at         TEXT,
    expires_at          TEXT,
    FOREIGN KEY (requester_id)      REFERENCES Employee(employee_id) ON DELETE CASCADE,
    FOREIGN KEY (target_employee_id) REFERENCES Employee(employee_id) ON DELETE CASCADE,
    FOREIGN KEY (reviewed_by)       REFERENCES Employee(employee_id)
);

CREATE INDEX IF NOT EXISTS idx_ic_access_request_requester ON IC_Access_Request(requester_id);
CREATE INDEX IF NOT EXISTS idx_ic_access_request_target    ON IC_Access_Request(target_employee_id);
CREATE INDEX IF NOT EXISTS idx_ic_access_request_status    ON IC_Access_Request(status);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_attendance_employee   ON Attendance(employee_id);
CREATE INDEX IF NOT EXISTS idx_attendance_checkin    ON Attendance(check_in);
CREATE INDEX IF NOT EXISTS idx_invoice_employee      ON Invoice(employee_id);
CREATE INDEX IF NOT EXISTS idx_invoice_status        ON Invoice(status);
CREATE INDEX IF NOT EXISTS idx_leave_app_employee    ON Leave_Application(employee_id);
CREATE INDEX IF NOT EXISTS idx_leave_app_status      ON Leave_Application(status);
CREATE INDEX IF NOT EXISTS idx_leave_balance_emp_yr  ON Leave_Balance(employee_id, year);
CREATE INDEX IF NOT EXISTS idx_payroll_employee      ON Payroll(employee_id);
CREATE INDEX IF NOT EXISTS idx_employee_email        ON Employee(email);
CREATE INDEX IF NOT EXISTS idx_audit_log_employee    ON AuditLog(employee_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_module      ON AuditLog(module_name);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at  ON AuditLog(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_archive     ON AuditLog(is_archived, archived_at);
CREATE INDEX IF NOT EXISTS idx_notification_employee ON Notification(employee_id);
CREATE INDEX IF NOT EXISTS idx_notification_read     ON Notification(employee_id, is_read);

-- =============================================================================
-- Permission System – Role-Based Access Control (RBAC)
-- =============================================================================

-- Define all available permissions in the system
CREATE TABLE IF NOT EXISTS Permission (
    permission_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    permission_name TEXT NOT NULL UNIQUE,  -- e.g., 'view_employees', 'approve_leave'
    description     TEXT,
    module_name     TEXT,                  -- e.g., 'employees', 'leave', 'payroll'
    created_at      TEXT DEFAULT (datetime('now'))
);

-- Map roles to permissions (many-to-many)
CREATE TABLE IF NOT EXISTS Role_Permission (
    role_id         INTEGER NOT NULL,
    permission_id   INTEGER NOT NULL,
    granted_at      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (role_id, permission_id),
    FOREIGN KEY (role_id)       REFERENCES Role(role_id) ON DELETE CASCADE,
    FOREIGN KEY (permission_id) REFERENCES Permission(permission_id) ON DELETE CASCADE
);

-- Track individual employee permissions (optional: for audit/override)
CREATE TABLE IF NOT EXISTS Employee_Permission (
    employee_permission_id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL,
    permission_id   INTEGER NOT NULL,
    granted_at      TEXT DEFAULT (datetime('now')),
    revoked_at      TEXT,
    is_active       INTEGER DEFAULT 1,
    granted_by      INTEGER,
    reason          TEXT,  -- e.g., 'promotion', 'role_change', 'manual_override'
    UNIQUE (employee_id, permission_id),
    FOREIGN KEY (employee_id)  REFERENCES Employee(employee_id) ON DELETE CASCADE,
    FOREIGN KEY (permission_id) REFERENCES Permission(permission_id) ON DELETE CASCADE,
    FOREIGN KEY (granted_by)    REFERENCES Employee(employee_id) ON DELETE SET NULL
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_role_permission_role     ON Role_Permission(role_id);
CREATE INDEX IF NOT EXISTS idx_role_permission_perm     ON Role_Permission(permission_id);
CREATE INDEX IF NOT EXISTS idx_employee_permission_emp  ON Employee_Permission(employee_id);
CREATE INDEX IF NOT EXISTS idx_employee_permission_perm ON Employee_Permission(permission_id);
CREATE INDEX IF NOT EXISTS idx_employee_permission_active ON Employee_Permission(employee_id, is_active);

-- =============================================================================
-- Performance Evaluation Module
-- =============================================================================

CREATE TABLE IF NOT EXISTS Performance_Score (
    score_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL,
    period_month    INTEGER NOT NULL,
    period_year     INTEGER NOT NULL,
    attendance_rate REAL DEFAULT 0,
    punctuality     REAL DEFAULT 0,
    overtime_score  REAL DEFAULT 0,
    reliability     REAL DEFAULT 0,
    composite_score REAL DEFAULT 0,
    grade           TEXT,
    generated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE (employee_id, period_month, period_year),
    FOREIGN KEY (employee_id) REFERENCES Employee(employee_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_perf_score_emp_period ON Performance_Score(employee_id, period_month, period_year);

-- =============================================================================
-- Recruitment / Job Application Module
-- =============================================================================

CREATE TABLE IF NOT EXISTS Job_Posting (
    posting_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    position_id     INTEGER REFERENCES Position(position_id),
    department_id   INTEGER,
    branch_id       INTEGER,
    employment_type TEXT CHECK(employment_type IN ('Full-Time','Part-Time','Contract')),
    min_salary      REAL,
    max_salary      REAL,
    description     TEXT,
    requirements    TEXT,
    status          TEXT DEFAULT 'Open' CHECK(status IN ('Open','Closed','Filled')),
    posted_by       INTEGER,
    created_at      TEXT DEFAULT (datetime('now')),
    closed_at       TEXT,
    FOREIGN KEY (department_id) REFERENCES Department(department_id),
    FOREIGN KEY (branch_id)     REFERENCES Branch(branch_id),
    FOREIGN KEY (posted_by)     REFERENCES Employee(employee_id)
);

CREATE TABLE IF NOT EXISTS Job_Application (
    application_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_id      INTEGER NOT NULL,
    applicant_name  TEXT NOT NULL,
    applicant_email TEXT NOT NULL,
    applicant_phone TEXT,
    applicant_ic    TEXT,
    applicant_address TEXT,
    resume_path     TEXT,
    cover_letter    TEXT,
    source          TEXT DEFAULT 'Manual' CHECK(source IN ('Email','Manual','Portal')),
    status          TEXT DEFAULT 'New'
                     CHECK(status IN ('New','Shortlisted','Interview','Offered','Hired','Rejected')),
    ai_score        REAL,
    ai_summary      TEXT,
    applied_at      TEXT DEFAULT (datetime('now')),
    reviewed_by     INTEGER,
    reviewed_at     TEXT,
    FOREIGN KEY (posting_id)   REFERENCES Job_Posting(posting_id) ON DELETE CASCADE,
    FOREIGN KEY (reviewed_by)  REFERENCES Employee(employee_id)
);

CREATE INDEX IF NOT EXISTS idx_job_app_posting   ON Job_Application(posting_id);
CREATE INDEX IF NOT EXISTS idx_job_app_status    ON Job_Application(status);

CREATE TABLE IF NOT EXISTS Interview (
    interview_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id  INTEGER NOT NULL,
    scheduled_at    TEXT NOT NULL,
    duration_min    INTEGER DEFAULT 60,
    interviewer_ids TEXT,
    location        TEXT,
    meeting_link    TEXT,
    type            TEXT DEFAULT 'In-Person' CHECK(type IN ('Online','In-Person','Phone')),
    status          TEXT DEFAULT 'Scheduled'
                     CHECK(status IN ('Scheduled','Confirmed','Completed','Cancelled')),
    feedback        TEXT,
    result          TEXT CHECK(result IN ('Pass','Fail','Pending')),
    created_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (application_id) REFERENCES Job_Application(application_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS Contract (
    contract_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id    INTEGER NOT NULL UNIQUE,
    employee_id       INTEGER,
    offer_date        TEXT,
    start_date        TEXT,
    position          TEXT,
    department_id     INTEGER,
    work_start_time   TEXT,
    work_end_time     TEXT,
    base_salary       REAL,
    employment_type   TEXT,
    contract_doc_path TEXT,
    signed_doc_path   TEXT,
    status            TEXT DEFAULT 'Draft'
                       CHECK(status IN ('Draft','Sent','Signed','Accepted')),
    created_at        TEXT DEFAULT (datetime('now')),
    signed_at         TEXT,
    FOREIGN KEY (application_id)  REFERENCES Job_Application(application_id),
    FOREIGN KEY (employee_id)     REFERENCES Employee(employee_id),
    FOREIGN KEY (department_id)   REFERENCES Department(department_id)
);

CREATE TABLE IF NOT EXISTS Interview_Policy (
    policy_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id            INTEGER NOT NULL UNIQUE,
    default_duration_min  INTEGER DEFAULT 60,
    default_type          TEXT DEFAULT 'In-Person' CHECK(default_type IN ('Online','In-Person','Phone')),
    default_location      TEXT DEFAULT '',
    default_meeting_link  TEXT DEFAULT '',
    day_start_time        TEXT DEFAULT '09:00',
    day_end_time          TEXT DEFAULT '17:00',
    slot_gap_min          INTEGER DEFAULT 15,
    max_per_day           INTEGER DEFAULT 8,
    auto_notify           INTEGER DEFAULT 1,
    updated_at            TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (company_id) REFERENCES Company(company_id)
);

CREATE TABLE IF NOT EXISTS Email_Config (
    config_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL,
    provider    TEXT DEFAULT 'IMAP',
    host        TEXT,
    port        INTEGER,
    username    TEXT,
    password    TEXT,
    last_sync   TEXT,
    is_active   INTEGER DEFAULT 1
);

-- =============================================================================
-- Salary Increment Module
-- =============================================================================

CREATE TABLE IF NOT EXISTS Salary_Increment (
    increment_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id       INTEGER NOT NULL,
    period_year       INTEGER NOT NULL,
    old_salary        REAL NOT NULL,
    new_salary        REAL NOT NULL,
    increment_pct     REAL NOT NULL,
    performance_score REAL,
    performance_grade TEXT,
    proposed_by       INTEGER NOT NULL,
    proposed_at       TEXT DEFAULT (datetime('now')),
    status            TEXT NOT NULL DEFAULT 'Pending'
                      CHECK(status IN ('Pending','Approved','Rejected')),
    reviewed_by       INTEGER,
    reviewed_at       TEXT,
    rejection_reason  TEXT,
    notified_at       TEXT,
    FOREIGN KEY (employee_id) REFERENCES Employee(employee_id),
    FOREIGN KEY (proposed_by) REFERENCES Employee(employee_id),
    FOREIGN KEY (reviewed_by) REFERENCES Employee(employee_id)
);

CREATE INDEX IF NOT EXISTS idx_inc_emp_year ON Salary_Increment(employee_id, period_year);
CREATE INDEX IF NOT EXISTS idx_inc_status ON Salary_Increment(status);

-- =============================================================================
-- Increment Policy Module (same % for all eligible employees)
-- =============================================================================

CREATE TABLE IF NOT EXISTS Increment_Policy (
    policy_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id             INTEGER NOT NULL,
    increment_pct          REAL NOT NULL DEFAULT 5.0,
    tenure_threshold_years INTEGER NOT NULL DEFAULT 1,
    effective_month        INTEGER NOT NULL DEFAULT 1,
    effective_year         INTEGER,
    auto_propose           INTEGER NOT NULL DEFAULT 1,
    created_at             TEXT DEFAULT (datetime('now')),
    updated_at             TEXT DEFAULT (datetime('now')),
    UNIQUE (company_id),
    FOREIGN KEY (company_id) REFERENCES Company(company_id)
);

CREATE TABLE IF NOT EXISTS Vacancy_Request (
    request_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_by    INTEGER NOT NULL,
    department_id   INTEGER NOT NULL,
    position_title  TEXT NOT NULL,
    position_id     INTEGER REFERENCES Position(position_id),
    is_custom       INTEGER DEFAULT 0,
    employment_type TEXT NOT NULL CHECK(employment_type IN ('Full-Time','Part-Time','Contract')),
    min_salary      REAL,
    max_salary      REAL,
    description     TEXT,
    requirements    TEXT,
    reason          TEXT,
    status          TEXT DEFAULT 'Pending' CHECK(status IN ('Pending','Approved','Rejected')),
    rejection_reason TEXT,
    reviewed_by     INTEGER,
    reviewed_at     TEXT,
    posting_id      INTEGER,
    created_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (requested_by)  REFERENCES Employee(employee_id),
    FOREIGN KEY (reviewed_by)   REFERENCES Employee(employee_id),
    FOREIGN KEY (department_id) REFERENCES Department(department_id),
    FOREIGN KEY (posting_id)    REFERENCES Job_Posting(posting_id)
);
