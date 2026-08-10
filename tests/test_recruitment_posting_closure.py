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
            CREATE TABLE IF NOT EXISTS Job_Posting (
                posting_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                status TEXT DEFAULT 'Open',
                closed_at TEXT
            )
        """)
        db.get_db().execute("""
            CREATE TABLE IF NOT EXISTS Job_Application (
                application_id INTEGER PRIMARY KEY AUTOINCREMENT,
                posting_id INTEGER NOT NULL,
                applicant_name TEXT NOT NULL,
                applicant_email TEXT NOT NULL
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


def test_close_job_posting_for_application_marks_posting_as_filled(temp_db):
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
            "SELECT status, closed_at FROM Job_Posting WHERE posting_id=?",
            (posting_id,),
            one=True,
        )
        assert posting['status'] == 'Filled'
        assert posting['closed_at'] is not None
