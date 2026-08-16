# SmartHR V12

Before any non-trivial analysis, code change, review, debugging, or UI work, read `ARCHITECTURE.md` and `DECISIONS.md`. Treat their confirmed content as mandatory project context. Read only the relevant sections needed for the task.

## Verify
- Test runner: `PYTHONPATH= .venv/Scripts/python.exe test_phase2_fixes.py`
- Never run `pytest` directly — the global PYTHONPATH pointing at the Hermes venv breaks PIL imports.
- The project venv is at `.venv/`; use `.venv/Scripts/python.exe` for all Python commands.
- Always `unset PYTHONPATH` (or set to empty) before running the project Python.

## Server
- `PYTHONPATH= .venv/Scripts/python.exe run.py`
- Runs on http://127.0.0.1:5000

## Style
- Test files use `app.test_client()` with manual `check()` assertions, not pytest fixtures.

## Purpose
SmartHR's purpose and scope.

## Repository-First Workflow
Before non-trivial changes, agents must inspect: relevant implementation, call sites, interfaces, related templates/UI, routes/controllers, session/state usage, service/helper functions, API contracts, database/storage interactions, tests, documentation.
Never make architectural assumptions from one file. Never rewrite working code because another implementation looks cleaner.

## Scope Discipline
Smallest coherent change. No unrelated refactors, renames, reformatting, architecture changes, or mixed cleanup with features. Report unrelated issues separately.

## Protected Behavior
Preserve: business logic, UI logic, user journeys, state transitions, validation, permissions, API behavior, database behavior, error handling, compatibility, project style — unless explicitly told to change.

## STOP AND ASK Rules
High-priority rule. If making a product, business, architecture, logic, data, security, or UX decision not clearly determined by the user's request or confirmed existing behavior — STOP AND ASK.
Specific list of things to STOP AND ASK before changing. A visual redesign does not grant permission to change UI logic.

## Business Logic Rules
Never change: role-based scoping, manager branch/dept filtering, leave balance calculations, payroll computations, AI scoring thresholds, approval workflows, notification routing, or position catalog behavior without explicit approval.

## UI Logic Rules
Preserve: sidebar navigation order, filter behavior, tab behavior, form defaults, button placement, responsive layout, toast notifications, badge counts. Improvements to spacing/typography/visual hierarchy OK only when behavior unchanged.

## Data and Migration Rules
No destructive migrations, dropped tables/columns, bulk deletions, cascades, or irreversible operations. No altering database initialization. No reseeding. No resetting.

## Architecture Rules
Preserve: Flask app factory, SQLite, session auth, decorator RBAC, per-request DB on Flask g, context processor notification injection, rate limiter, CSRF. No new major frameworks without approval.

## Dependency Rules
Check existing codebase first. Evaluate maintenance and security impact. Major dependencies require user approval.

## Debugging Workflow
Reproduce → Gather evidence → Trace flow → Form hypothesis → Test hypothesis → Fix root cause → Verify → Regression test. Never change code randomly. Never disable tests or swallow errors.

## Senior Code Review Checklist
Correctness, edge cases, business-rule regressions, UI regressions, state/session issues, race conditions, error handling, security, role/permission leakage, data corruption, API compatibility, performance, dead code, maintainability, test coverage.

## UI/UX Review Checklist
Clear hierarchy, consistent spacing, accessibility, responsive behavior, predictable controls, clear feedback, loading/empty/error states, consistent components, existing design language. No excessive cards, gradients, random shadows, decorative elements.

## Visual Testing Rules
Implement → Render → Inspect → Interact → Test workflow → Fix → Re-render → Verify. Check desktop and mobile layouts, overflow, spacing, alignment, typography, navigation, forms, validation, hover/focus/pressed/disabled states, role-specific interfaces.

## Testing Rules
Use appropriate unit, integration, route, database, regression, UI, end-to-end, build, lint, or type checks. Bug fixes get regression coverage where practical. Never claim a check passed unless it ran.

## Documentation Rules
After approved implementation: update README.md, CHANGELOG.md, ARCHITECTURE.md, DECISIONS.md as appropriate. Don't turn README/CHANGELOG into trivial-edit logs.

## Security Rules
Protect secrets, access controls, authorization boundaries, session behavior, sensitive HR data, database integrity, and auditability. Never document secrets or private values.

## Final Response Expectations
Agents must state: what changed, what was preserved, files changed, verification run, assumptions/risks/unknowns, whether docs were updated.

## Priority Hierarchy
1. Prevent data loss and security problems
2. User's explicit current instruction
3. Frozen/Approved decisions
4. DECISIONS.md
5. ARCHITECTURE.md
6. Existing documented behavior
7. Tests representing intended behavior
8. Existing implementation patterns
9. AGENTS.md process rules
10. Agent preference

If sources 3-8 conflict, STOP AND ASK.
