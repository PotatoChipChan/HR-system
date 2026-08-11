# SmartHR V12

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
