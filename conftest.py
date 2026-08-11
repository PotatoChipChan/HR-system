"""pytest configuration — load .env, disable CSRF for tests."""
import os
import sys
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(__file__))

import pytest
from app import create_app


@pytest.fixture(scope='session')
def app():
    """Create the Flask app once per test session, CSRF disabled."""
    app = create_app()
    app.config['CSRF_ENABLED'] = False
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['TESTING'] = True
    return app


@pytest.fixture
def client(app):
    """Provide a test client."""
    return app.test_client()
