"""
app/csrf.py  –  Lightweight CSRF protection (no external dependencies).

A single unpredictable token is minted per session and injected into every
template via the `csrf_token` context processor. State-changing requests
(POST/PUT/PATCH/DELETE) must echo the token back – either as a hidden form
field (`csrf_token`) or as the `X-CSRF-Token` header for AJAX/JSON calls.
"""
import hmac
import secrets
from functools import wraps
from flask import request, session, current_app, jsonify


def get_csrf_token():
    """Return the session's CSRF token, minting one on first use."""
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_hex(32)
        session['csrf_token'] = token
    return token


def validate_csrf():
    """Return True when the current request carries a valid CSRF token.

    For state-changing requests the token must match the session value.
    Form bodies, parsed JSON bodies and the X-CSRF-Token header are all
    accepted so that fetch()/AJAX callers can be protected too.
    """
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return True

    token = request.form.get('csrf_token')
    if not token:
        token = request.headers.get('X-CSRF-Token')
    if not token:
        # JSON payloads (e.g. fetch(url, {method:'POST', body: JSON.stringify(...)}))
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            token = data.get('csrf_token')
    if not token:
        return False

    expected = session.get('csrf_token')
    if not expected:
        return False
    return hmac.compare_digest(str(token), str(expected))


def csrf_required(f):
    """Decorator: reject state-changing requests without a valid CSRF token."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not validate_csrf():
            current_app.logger.warning(
                'CSRF validation failed for %s (%s %s)',
                request.endpoint, request.method, request.path)
            # AJAX/JSON callers get a JSON 400, form posts get a page
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"error": "CSRF token missing or invalid."}), 400
            from flask import flash, redirect, url_for
            flash('Your session token expired or is invalid. Please try again.', 'danger')
            return redirect(request.referrer or url_for('main.dashboard'))
        return f(*args, **kwargs)
    return decorated