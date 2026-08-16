import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest
from flask import Flask


@pytest.fixture
def temp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp.close()

    pkg = types.ModuleType('app')
    pkg.__path__ = [str(Path(__file__).resolve().parents[1] / 'app')]
    monkeypatch.setitem(sys.modules, 'app', pkg)

    spec = importlib.util.spec_from_file_location(
        'app.database',
        Path(__file__).resolve().parents[1] / 'app' / 'database.py',
    )
    db = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'app.database', db)
    spec.loader.exec_module(db)

    monkeypatch.setattr(db, 'DB_PATH', tmp.name)

    app = Flask(__name__)
    with app.app_context():
        db.get_db().execute("""
            CREATE TABLE IF NOT EXISTS Branch (
                branch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL
            )
        """)
        db.get_db().execute(
            "INSERT INTO Branch (branch_id, company_id, name) VALUES (1, 1, 'KL')")
        db.get_db().execute("""
            CREATE TABLE IF NOT EXISTS Job_Posting (
                posting_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title              TEXT NOT NULL,
                branch_id          INTEGER NOT NULL DEFAULT 1,
                status             TEXT DEFAULT 'Open',
                approved_openings  INTEGER NOT NULL DEFAULT 1,
                reserved_openings  INTEGER NOT NULL DEFAULT 0,
                filled_openings    INTEGER NOT NULL DEFAULT 0,
                closed_at          TEXT
            )
        """)
        db.get_db().execute("""
            CREATE TABLE IF NOT EXISTS Job_Application (
                application_id INTEGER PRIMARY KEY AUTOINCREMENT,
                posting_id     INTEGER NOT NULL,
                applicant_name TEXT NOT NULL,
                applicant_email TEXT NOT NULL,
                status         TEXT DEFAULT 'New'
            )
        """)
        db.get_db().execute("""
            CREATE TABLE IF NOT EXISTS Opening_Reservation (
                reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                posting_id     INTEGER NOT NULL,
                application_id INTEGER NOT NULL,
                contract_id    INTEGER,
                status         TEXT NOT NULL DEFAULT 'Reserved',
                created_at     TEXT DEFAULT (datetime('now')),
                released_at    TEXT,
                release_reason TEXT
            )
        """)
        db.get_db().execute("""
            CREATE TABLE IF NOT EXISTS AuditLog (
                audit_log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id       INTEGER,
                action            TEXT,
                module_name       TEXT,
                description       TEXT,
                target_table      TEXT,
                target_record_id  TEXT,
                action_status     TEXT,
                action_details    TEXT,
                ip_address        TEXT,
                user_agent        TEXT,
                created_at        TEXT DEFAULT (datetime('now')),
                is_archived       INTEGER DEFAULT 0,
                archived_at       TEXT,
                retention_until   TEXT
            )
        """)
        db.get_db().commit()
        yield db

    with app.app_context():
        db.close_db()
    try:
        os.remove(tmp.name)
    except PermissionError:
        pass


def test_first_hire_fills_single_opening_posting(temp_db):
    with Flask(__name__).app_context():
        posting_id = temp_db.execute(
            "INSERT INTO Job_Posting (title, status) VALUES (?, ?)",
            ('Software Engineer', 'Open')
        )
        application_id = temp_db.execute(
            "INSERT INTO Job_Application (posting_id, applicant_name, applicant_email) VALUES (?, ?, ?)",
            (posting_id, 'Alice Tan', 'alice@example.com')
        )

        assert temp_db.close_job_posting_for_application(application_id) is True

        posting = temp_db.query(
            "SELECT status, closed_at, filled_openings, approved_openings FROM Job_Posting WHERE posting_id=?",
            (posting_id,),
            one=True,
        )
        assert posting['status'] == 'Filled'
        assert posting['closed_at'] is not None
        assert posting['filled_openings'] == 1
        assert posting['approved_openings'] == 1

        reservation = temp_db.query(
            "SELECT status FROM Opening_Reservation WHERE application_id=?",
            (application_id,),
            one=True,
        )
        assert reservation is not None and reservation['status'] == 'Filled'


def test_multi_opening_posting_stays_open_until_all_filled(temp_db):
    with Flask(__name__).app_context():
        posting_id = temp_db.execute(
            "INSERT INTO Job_Posting (title, status, approved_openings) VALUES (?, ?, ?)",
            ('Engineer', 'Open', 2)
        )
        for name, email in [('Alice', 'a@x.com'), ('Bob', 'b@x.com'), ('Carol', 'c@x.com')]:
            temp_db.execute(
                "INSERT INTO Job_Application (posting_id, applicant_name, applicant_email, status) VALUES (?,?,?, 'Shortlisted')",
                (posting_id, name, email)
            )
        apps = temp_db.query(
            "SELECT application_id FROM Job_Application WHERE posting_id=? ORDER BY application_id",
            (posting_id,),
        )
        a1, a2, a3 = [a['application_id'] for a in apps]

        # First hire: posting is only partially filled; others stay active
        temp_db.close_job_posting_for_application(a1)
        posting = temp_db.query(
            "SELECT status, filled_openings, closed_at FROM Job_Posting WHERE posting_id=?",
            (posting_id,),
            one=True,
        )
        assert posting['status'] == 'Partially Filled'
        assert posting['filled_openings'] == 1
        assert posting['closed_at'] is None

        others = temp_db.query(
            "SELECT status FROM Job_Application WHERE application_id IN (?, ?) ORDER BY application_id",
            (a2, a3),
        )
        assert [o['status'] for o in others] == ['Shortlisted', 'Shortlisted']

        # Second hire fills the posting; the remaining candidate is rejected
        temp_db.close_job_posting_for_application(a2)
        posting = temp_db.query(
            "SELECT status, filled_openings, closed_at FROM Job_Posting WHERE posting_id=?",
            (posting_id,),
            one=True,
        )
        assert posting['status'] == 'Filled'
        assert posting['filled_openings'] == 2
        assert posting['closed_at'] is not None

        rejected = temp_db.query(
            "SELECT status FROM Job_Application WHERE application_id=?",
            (a3,),
            one=True,
        )
        assert rejected['status'] == 'Rejected'
