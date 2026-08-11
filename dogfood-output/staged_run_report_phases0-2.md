# SmartHR Vision-Driven UI Test — Staged Run Report (Phases 0–2)

> Date: 2026-08-11 · Driver: cua-driver 0.19.3 via `computer_use` (Brave, background + foreground SendInput)
> App: `http://127.0.0.1:5000` · Server: `run.py` (project `.venv`, PYTHONPATH unset — see env note)
> Run type: STAGED per user decision — Phases 0–2 only, pause for review.

---

## ✅ Phase 0 — Smoke (3/3 PASS)

| # | Action | Result |
|---|--------|--------|
| 0.1 | Login `hr@smarthr.my` (HR) | ✅ Dashboard: 47 active employees, full admin nav (Organisation, User Mgmt, Audit Log) |
| 0.2 | Login `cheeseng@smarthr.my` (Branch Manager) | ✅ Branch-scoped: **8 employees** (Penang only), trimmed nav (no Organisation/Roles/Audit Log) |
| 0.3 | Login `elizabeth@smarthr.my` (Employee) | ✅ Minimal nav (no Organisation/User Mgmt/Interviews), dashboard shows leave balances etc. |
| 0.4 | Logouts (3x) | ✅ "You have been logged out successfully." + audit-logged |

**Role-scoping observations (all correct):**
- Manager sees Employees + Recruitment incl. new v5 links (Internal Job Board, My Applications); no Interview Policy, no Careers link, no Audit Log. ✓
- Employee sees Job Postings / Applications (Internal) / Internal Job Board / My Applications — verify 403-guards in later phase. ⏳
- HR sees everything incl. System Audit Log + Careers Page. ✓

## ✅ Phase 1 — Vacancy request (PASS, DB-verified)

- Filled Request New Position as Ng Chee Seng: **Engineering (Penang Office)** → **Hardware Engineer**, Full-Time, RM4500–6500, description/requirements/reason (IoT expansion).
- Form has **NO audience field** — server defaults to `'Both'` (routes.py:1379). OK by design.
- Flash: *"Vacancy request submitted for review."* → redirected to requests list (2 pending).
- **DB verified** — `Vacancy_Request #74`: Hardware Engineer, dept 6 (Engineering/Penang), Full-Time, 4500–6500, `target_audience='Both'`, `status='Pending'`, requested_by=19, 11:56:41.
- **3 in-app notifications** created (unread) to Goh Sook Ting (HR), Chan Han Yue (Admin), Elizabeth/Admin (#45) — recipients are HR+Admin roles ✓ (Elizabeth #45 is Admin `elizabeth11@...`, distinct from Employee elizabeth).

## 🚨 Phase 2 — HR approve (BLOCKED — BUG FOUND)

**Bug B1 (CRITICAL): Approve & Create Posting → HTTP 500**
- Route: `POST /recruitment/vacancy-request/74/approve`
- Traceback (server log):
  ```
  File "app/recruitment/routes.py", line 1596, in approve_vacancy
      audience = req.get('target_audience', 'Both')
  AttributeError: 'sqlite3.Row' object has no attribute 'get'
  ```
- **Impact: NO vacancy request can ever be approved.** Posting creation, requester notification, and audit-log entry downstream are all unreachable. Blocks the entire recruitment flow in the current uncommitted v5 refactor.
- Root cause: `query()` returns `sqlite3.Row`; `.get()` is a dict method. Fix: `req['target_audience']` (or `as_dict(req)` first).
- Scope check: sibling `.get()` uses in recruitment routes are on form dicts (`f.get`) — safe. `invoice/routes.py:1851` uses `.get()` but only after `as_dict(row)` — safe. **Single occurrence.**

**Bug B2 (MEDIUM, pre-existing): Email monitor crashes every poll cycle**
- Server log, repeated each `/api/check-email`:
  ```
  [EMAIL MONITOR] Error processing message b'3278': NOT NULL constraint failed: Job_Application.applicant_name
  ```
- The email monitor hits a message that produces an application without `applicant_name` → constraint error, caught + logged, but the message is never skipped/consumed → repeats forever.
- Relevant to G31 email-handling surface; needs a guard (skip/normalise message without applicant_name) + likely a repro of message 3278.

## 🔎 Minor observations (non-blocking)

1. Login page demo list labels `hr@smarthr.my` as **"HR Director"** but the app sidebar/DB shows **"HR Manager"** (Amantha Lee Mei Ling, position_id → HR Manager). Cosmetic label mismatch on login page.
2. Eliz... Employee `elizabeth@smarthr.my` first login attempt failed because foreground typing dropped the trailing `.my` — **driver input truncation**, not an app bug (audit log confirmed `email not found: elizabeth@smarthr`). Workaround: ctrl+a before typing. (Tooling note, not app finding.)
3. `set_value` does not work on native `<select>` in Chromium; **foreground click opens the native dropdown** and lets you pick list items. (Tooling note.)

## Environment note
- Global `PYTHONPATH` points at the Hermes venv → corrupts PIL/Flask resolution for the project. Server must run with `unset PYTHONPATH`. (Persisted in memory.)

## Status
- Phase 0 ✅ · Phase 1 ✅ · Phase 2 ⛔ blocked by B1.
- **Awaiting user decision:** fix B1 (+B2) and re-run Phase 2, or stop here?
