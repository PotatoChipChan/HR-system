# SmartHR Migration Runbook

This runbook governs schema migrations. SmartHR uses SQLite with no versioned
migration tool; all migrations are idempotent functions in `init_db.py`
(or modules it imports) executed in order by `init_db()`.

## Core rules

1. **Back up before you migrate.** Never run a rebuild migration against a
   database with real data without first creating a snapshot
   (`migration_framework.backup_before_migration`). Both rebuild migrations
   in `init_db.py` do this automatically before any pending rebuild runs.
2. **Never destroy data.** Migrations may add columns/tables or rebuild a
   table, but must preserve every existing row, ID, foreign-key relationship,
   index, and trigger.
3. **Idempotent by design.** Every migration must detect "already applied"
   state and no-op safely on re-runs.
4. **Verified or it did not happen.** A migration is not complete until the
   assertions and the copy-of-database test pass (see below).

## The framework

`migration_framework.py` provides the reusable, assertion-guarded helpers:

- `backup_database(db_path, backup_path)` — consistent online snapshot
  (sqlite3 online-backup API; safe even under concurrent writes).
- `get_table_info(con, table)` — captures source columns, index DDL, and
  trigger DDL for replay after a rebuild.
- `rebuild_table(con, table, new_ddl)` — transactional
  `copy -> assert -> drop -> rename`, recreating indexes/triggers.

`rebuild_table` raises `RuntimeError` (and rolls back) when any of these
fail:

- source/destination row-count parity
- full-row preservation (set comparison of the common columns, which —
  combined with row-count parity — also catches lost duplicates)
- `PRAGMA foreign_key_check` reports violations
- an index or trigger that existed on the source is missing after the rebuild

`new_ddl` must create a table literally named `<table>_new`.

## Reference migrations

Both rebuild migrations are reference implementations of the framework:

- `init_db.migrate_attendance_status()`:
  1. Reads the current `Attendance` DDL from `sqlite_master`.
  2. If `'Rejected'` is already in the status CHECK, it only ensures the
     standard indexes exist (`_ensure_attendance_indexes`) and returns.
  3. Otherwise it creates a timestamped backup
     (`backup_before_migration(DB_PATH)`), calls `rebuild_table(...)` with
     the new DDL, then re-ensures the standard indexes and commits.
- `init_db.migrate_contract_security()`: same pattern for `Contract`
  (adds `accept_token`/`token_expires_at`/`accepted_at` and the `Declined`
  status); no-op branch checks for `accept_token` and creates no backup.
- `init_db.migrate_vacancy_openings()`: rebuilds `Job_Posting` (status CHECK
  gains `Partially Filled`), then adds opening-count columns to
  `Job_Posting` (`approved_openings`/`reserved_openings`/`filled_openings`)
  and `Vacancy_Request` (`requested_openings`/`approved_openings`), creates
  the `Opening_Reservation` ledger, and backfills historical rows
  (postings 1/1 with `filled=1` where `Filled`; requests 1/1 where
  Approved; `Filled` reservations for existing Hired applications).
  No-op branch checks for `Partially Filled` in the `Job_Posting` DDL and
  creates no backup.
- `init_db.migrate_interview_format()`: additive-only (no rebuild, no
  backup) — adds `format`/`venue`/`posting_branch_id` to `Interview` and
  creates the `Interview_Reschedule` history table. Legacy interviews keep
  their existing `type`/`location`; `format` stays NULL. No-op branch
  checks for the `format` column.
- `init_db.migrate_reschedule_dedup()`: additive-only (no rebuild, no
  backup) — creates `Reschedule_Email_Processed` (Message-ID primary key)
  for persistent reschedule-email deduplication. Idempotent.
- `init_db.migrate_scorecard_recommendation()`: additive-only (no rebuild,
  no backup) — creates `Interview_Scorecard` (fixed 1-5 criteria with
  evidence notes, one per interview) and `Candidate_Recommendation`
  (Pending/Approved/Rejected, one per application per posting).
  Idempotent.
- `init_db.migrate_offer_lifecycle()`: verified rebuilds of `Contract`
  (status CHECK gains `Expired`) and `Job_Application` (status CHECK gains
  `Offer Expired`), with DDL derived from the live table so column parity is
  guaranteed (backup created before the rebuilds), then additive
  `Offer_Approval` and `Email_Delivery_Log` tables. Idempotent.
- `init_db.migrate_job_posting_archive()`: rebuilds `Job_Posting` (status
  CHECK gains `Archived`; DDL includes the columns introduced by
  `migrate_vacancy_openings`) with a timestamped backup, then backfills
  `Filled` postings to `Archived` with `closed_at` preserved/defaulted.
  No-op branch checks for `Archived` in the `Job_Posting` DDL.
- `init_db.migrate_position_manager_flag()`: additive-only (no rebuild, no
  backup) — adds `is_department_manager_position INTEGER NOT NULL DEFAULT 0`
  to the `Position` catalog. Existing positions default to 0, so no existing
  title ever auto-assigns a department manager. Idempotent.

## Adding a new migration

1. Write the migration function in `init_db.py` following the reference:
   - idempotency guard at the top (e.g. check for a new column name or a
     marker string in the table DDL)
   - call `rebuild_table` (or `ALTER TABLE ADD COLUMN` for simple additive
     changes) inside the function
   - register the call in `init_db()` in dependency order
2. Add a regression test block in `test_phase2_fixes.py` (B7-style) that
   exercises the migration against a **synthetic old-schema database on a
   temporary copy** — never against the shared development DB.
3. Run the verification steps below.

## Verification steps (before considering a migration done)

- Run the migration against a **copy of the legacy database** and confirm:
  - row count before == row count after
  - every preserved column is identical before/after (full-row comparison,
    not a column subset)
  - IDs (primary keys) unchanged
  - `PRAGMA foreign_key_check` returns zero rows
  - all source indexes and triggers exist after the rebuild
  - re-running the migration is a safe no-op (idempotency)
- Confirm the new schema accepts the new values (e.g. the new CHECK allows
  `'Rejected'` when that is the point of the migration).
- Confirm foreign keys are still enforced after the rebuild (bogus child
  row raises `IntegrityError`).
- Confirm a **failing** migration rolls back: force a failure (e.g. a CHECK
  the copied rows violate) and verify rows, indexes, and triggers are
  untouched afterwards.

## Rollback procedure

- Every rebuild runs inside a transaction; on any assertion failure the
  transaction is rolled back automatically and the source table is left
  intact (verified by test).
- If a migration fails despite the guards, restore the pre-migration backup
  taken with `backup_before_migration` (or the file copy), fix the migration,
  and re-run — never hand-edit the database to "un-do" a migration.

## Pre-migration backups and retention

- Every pending rebuild migration (`migrate_attendance_status`,
  `migrate_contract_security`) calls `backup_before_migration(DB_PATH)`
  **before** the rebuild starts. A backup failure aborts the migration, so a
  rebuild never runs without a snapshot.
- Backups are written to `<database parent>/backups/` (e.g.
  `instance/backups/` for `instance/smarthr.db`) — always outside the active
  database file path.
- Names are timestamped (`smarthr_backup_YYYYMMDD_HHMMSS_ffffff.db`). An
  existing file is never overwritten; a numeric suffix is appended instead.
- The migration reports the backup path on success (`[BACKUP] Created backup
  at <path>`).
- Already-applied (no-op) migration runs do **not** create backups.

### Retention / cleanup policy

- SmartHR does not delete backups automatically. The operator is responsible
  for cleanup.
- Recommended retention: keep the **five most recent** backups per database
  and delete older ones (typically after confirming the migrated database is
  healthy for a full pay period or release cycle).
- Cleanup is a plain file deletion; there is no index that must stay in sync.
- To restore: stop the server, copy the chosen backup over
  `instance/smarthr.db`, and restart. Verify with the migration checks above
  before resuming normal use.

## Testing guidance

- Attendance migration tests (B7/B8) in `test_phase2_fixes.py` run against
  throwaway copies (`tempfile` + `shutil.copy2`); fixture creation/deletion
  never touches real development data.
- Tests use the migration framework directly (B8) plus the reference
  migration (B7) so both the framework and its real consumer stay verified.
