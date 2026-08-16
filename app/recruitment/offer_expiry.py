"""app/recruitment/offer_expiry.py – Server-side offer-expiry processor.

A background daemon periodically marks Sent offers past their token expiry
as 'Expired', flips the application to 'Offer Expired', releases the reserved
opening and notifies HR/Admin + HR Manager. Replacement offers are never sent
automatically.

The sweep interval is short (30s) in development/test so a QA 1-minute
expiry is processed quickly, and 5 minutes in production. Expiry is always
computed server-side from Contract.token_expires_at; the system/browser
clock is never consulted.
"""

import os
import threading
import time

_INTERVAL_DEV = 30
_INTERVAL_PROD = 300
_INITIAL_DELAY = 30


def _is_dev():
    return (os.environ.get('FLASK_ENV', '') == 'development'
            or os.environ.get('FLASK_DEBUG', '').lower() in ('true', '1', 'yes'))


def _should_start(app):
    """Start only in the real server process (mirrors payroll/autogen.py)."""
    if app.debug:
        return os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    return True


def _sweep(app):
    try:
        with app.app_context():
            from app.recruitment.routes import process_expired_offers
            n = process_expired_offers()
            if n:
                print(f"[OFFER EXPIRY] Processed {n} expired offer(s)")
    except Exception as e:
        print(f"[OFFER EXPIRY] Sweep failed: {e}")


def _loop(app, interval):
    time.sleep(_INITIAL_DELAY)
    while True:
        _sweep(app)
        time.sleep(interval)


def start_offer_expiry_scheduler(app):
    """Start the offer-expiry daemon thread (idempotent)."""
    if not _should_start(app):
        return
    if getattr(app, '_offer_expiry_started', False):
        return
    app._offer_expiry_started = True
    interval = _INTERVAL_DEV if _is_dev() else _INTERVAL_PROD
    t = threading.Thread(target=_loop, args=(app, interval),
                         daemon=True, name='offer-expiry')
    t.start()
    print(f'[OFFER EXPIRY] Scheduler started (every {interval}s, '
          f'first run in {_INITIAL_DELAY}s).')
