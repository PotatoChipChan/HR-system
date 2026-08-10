"""app/rate_limiter.py – Sliding-window rate limiter with X-Forwarded-For IP extraction"""
import os
import time
import uuid
from collections import defaultdict, deque
from functools import wraps
from flask import request, jsonify, current_app


_LOCAL_IPS = {"::1", "127.0.0.1", "localhost"}


def _is_dev():
    return os.environ.get("FLASK_ENV") == "development"


class RateLimiter:
    def __init__(self):
        self._buckets = defaultdict(lambda: defaultdict(lambda: deque()))

    def _get_client_ip(self):
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            ip = xff.split(",")[0].strip()
        else:
            ip = request.remote_addr or "unknown"
        # Bypass rate limiting for localhost in dev mode by randomising the key
        if ip in _LOCAL_IPS and _is_dev():
            return f"{ip}-{uuid.uuid4().hex[:8]}"
        return ip

    def _clean(self, ip, endpoint, window):
        bucket = self._buckets[ip][endpoint]
        now = time.time()
        while bucket and bucket[0] < now - window:
            bucket.popleft()

    def is_allowed(self, limit=100, window=60, endpoint=None):
        ip = self._get_client_ip()
        if endpoint is None:
            endpoint = request.endpoint or "unknown"
        self._clean(ip, endpoint, window)
        bucket = self._buckets[ip][endpoint]
        if len(bucket) >= limit:
            return False
        bucket.append(time.time())
        return True

    def get_remaining(self, limit=100, window=60, endpoint=None):
        ip = self._get_client_ip()
        if endpoint is None:
            endpoint = request.endpoint or "unknown"
        self._clean(ip, endpoint, window)
        return max(0, limit - len(self._buckets[ip][endpoint]))


limiter = RateLimiter()


def rate_limit(limit=100, window=60):
    """Decorator: apply per-route rate limiting."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not limiter.is_allowed(limit=limit, window=window):
                ip = limiter._get_client_ip()
                current_app.logger.warning("Rate limit exceeded for %s on %s", ip, request.endpoint)
                return jsonify({"error": "Too many requests. Please try again later."}), 429
            return f(*args, **kwargs)
        return wrapped
    return decorator
