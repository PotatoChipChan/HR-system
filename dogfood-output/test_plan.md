# SmartHR Recruitment E2E Test Plan (frontend-driven)

Date: 2026-08-11
App: http://127.0.0.1:5000 (Flask, PID 19440, project: smarthr_app_V12)
Method: Drive the real Brave browser via computer_use (background/foreground
        as needed). NO Python test scripts. Evidence = screenshots + AX tree
        + server-side state via DB/HTTP verification.

## Goal flow (per user brief)
Vacancy request (done, #73 DevOps Senior, Penang, Pending)
  -> post the requested posting (HR approves)
  -> 2 internal staff apply
  -> 5 external applications
  -> shortlisting works
  -> auto-assign interviews + HR approves
  -> email functions verified (Sent-folder IMAP check at each mailing step)
  -> after interview choose best performer (score-based)
  -> send offer letter
  -> end

## Accounts (README)
  HR Manager      hr@smarthr.my      Hr@123
  Penang Mgr      cheeseng@smarthr.my Manager@123   (requester of #73)
  Employee        elizabeth@smarthr.my Employee@123
  Employee        ryan@smarthr.my     Employee@123
  (backup: nurul@, priya@)

## Phase 1 - Approve vacancy request #73 (HR Manager)
  1. Login as hr@smarthr.my
  2. Recruitment -> Vacancy Requests -> #73 DevOps Senior (Pending)
  3. Approve with branch = Penang
  4. Verify: Job_Posting created & status Open; in-app notification to
     requester; audit log entry APPROVE_VACANCY
  NOTE (finding B): approve path has NO email; reject path does. Verify live.

## Phase 2 - Applications (2 internal + 5 external)
  Internal (use apply page as employee accounts or plain form):
    - Elizabeth (elizabeth@smarthr.my)
    - Ryan (ryan@smarthr.my)
  External (crafted cover letters; aim: 3 strong >60, 2 weak <60):
    - E1: strong DevOps resume (docker, kubernetes, ci/cd, aws, python)
    - E2: strong DevOps resume (jenkins, terraform, linux, monitoring)
    - E3: strong/mid DevOps resume (git, ci/cd, azure)
    - E4: weak (generic, short cover letter)
    - E5: weak (unrelated field, short)
  Each gets a resume file (txt/pdf) + cover letter. Public apply route:
  /recruitment/apply/<pid> (no login). Score threshold: >60 = Shortlisted,
  else New. NOTE (finding C): scoring only runs if cover letter > 100 chars.

## Phase 3 - Shortlisting
  1. Verify 7 applications on posting page, ai_score + summary shown
  2. Confirm auto-shortlist statuses (>60 Shortlisted, <60 New)
  3. Click "Reject non-shortlisted" -> verify statuses -> Rejected +
     rejection emails attempted (check Sent folder)

## Phase 4 - Interviews (HR)
  1. Recruitment -> interviews/bulk/auto-assign; select shortlisted
  2. auto-assign preview -> confirm
  3. Verify: Interview rows (scheduled_at >= tomorrow, 09:00+, weekday,
     max 3/day per policy: 60min/30gap/In-Person), app status=Interview,
     interviewer assigned (Penang manager + HR pool), invite emails sent
  FINDING A (expected): results cannot be recorded same day because
  auto-assign schedules from tomorrow. DECISION (default chosen): run
  auto-assign as-is to prove it works, then manually re-schedule the
  interviews to today (earlier time) so Pass/Fail can be recorded and the
  E2E completes. Document the guard as a UX finding.

## Phase 5 - Interview results + best performer
  1. Record Pass for top scorer(s), Fail for the rest (view application ->
     interview -> result). Verify Fail auto-rejects others? NO - Fail only
     rejects that candidate; Pass auto-rejects the rest (code confirms).
  2. Best performer = highest ai_score among Passed. Verify view shows
     AI score + interview Pass.

## Phase 6 - Contract + Offer (HR)
  1. Create contract draft (position DevOps Senior, start date, salary)
  2. Send offer -> verify offer email + contract PDF attachment in Sent
     folder; contract status Sent; app status Offered

## Phase 7 - Wrap-up (optional per user: ends at offer)
  Offer accept (HR or public link) -> app Hired -> posting closes ->
  other candidates auto-rejected. Hire -> add employee pre-filled.
  DEFAULT: stop after offer letter sent (per brief "send offer letter ->
  end"); offer acceptance can be a follow-up run.

## Verification tooling
  - Screenshots: computer_use capture (som mode); note: vision_analysis
    unavailable (no vision provider) - rely on AX tree + DB/HTTP checks.
  - Server state: curl + sqlite3 reads (read-only) after each phase.
  - Email: python imaplib read-only check of Sent folder for each mailing
    step (interview invites xN, rejections, offer). Credentials from .env
    (never printed).

## Pre-existing findings to validate live
  A. Interview result guard blocks same-day results (auto-assign = tomorrow)
  B. Approve-vacancy sends no email; reject does (asymmetry)
  C. Public apply auto-scores only when cover letter > 100 chars
  D. /dashboard returns 404 (check nav)
