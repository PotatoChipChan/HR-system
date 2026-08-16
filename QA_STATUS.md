# SmartHR QA Checkpoint

## Purpose

This file records the last completed QA stage so an interrupted Codex session
can resume the test run without repeating destructive work or asking for a
decision that has already been authorised.

## Current Run

- Started: 13 August 2026
- Status: Completed
- Scope: all role-accessible sidebar modules, core workflows, server and browser
  validation, permissions/scoping, visual rendering and responsive checks.
- Excluded: face recognition and IC/OCR workflows, as requested.
- Safety: state-changing QA uses a temporary SQLite snapshot and stubbed email.
  The live database is used only for read-only visual checks and explicitly
  requested cleanup.

## Completed Before This Run

- All 26 non-face Admin sidebar destinations rendered without a server-error
  page or desktop overflow.
- Recruitment QA and the B1-B23 regression suite passed (254 checks).
- Explicit QA records were removed from the development database. A recovery
  snapshot exists at `instance/backups/qa_cleanup_20260813_125825_191797.db`.

## Completed in This Run

- Added B24 broad module QA (41 assertions): every non-face Admin sidebar
  destination, malformed URL/form fields, employee/manager role boundaries,
  notification isolation, and branch-scoped record access.
- Added B25 operational workflow QA (11 assertions): profile validation, claim
  validation and approval, payroll finalisation, leave approval/balances,
  monthly performance generation, increment/bonus approvals, CSV export, and
  notification calls. Both blocks use their own temporary SQLite copy; email
  delivery is captured locally.
- Repaired confirmed defects: unscoped performance-score API access; a crafted
  Manager employee-edit request could change hidden branch/department/role
  fields; malformed numeric filters and period fields could raise 500 errors;
  a blank profile name and negative claim amounts were accepted.
- Browser smoke pass: all Admin non-face sidebar links rendered without a
  traceback, server-error page, or desktop overflow. Mobile checks at 390 px
  covered Employees, Payroll, Applications, Interviews, Attendance Logs,
  Claims, Year-End Review, and Reports with no page-level horizontal overflow.
- Final full regression: `TEST_FOCUS` unset and `PYTHONPATH` empty — **306
  passed, 0 failed**. The test runner was executed with the project virtual
  environment and left no QA data in the live database.

## Resume Point

The 13 August 2026 QA run is complete. If a new QA run is requested, start a
new dated section below and keep the temporary-database / captured-email safety
rules above.

## Findings Log

- No unresolved defect is currently recorded for this run.
- Historical interviews with past datetimes can still be marked `Scheduled`
  until an HR user explicitly selects **Mark Completed**. This is currently
  treated as the documented HR workflow, not an automatic-state transition.
