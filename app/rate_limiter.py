"""app/rate_limiter.py – Sliding-window rate limiter with proxy-aware IP extraction"""
import os
import time
import uuid
from collections import defaultdict, deque
from flask import request


_LOCAL_IPS = {"::1", "127.0.0.1", "localhost"}


def _is_dev():
    return os.environ.get("FLASK_ENV") == "development"


def _trusts_proxy():
    return os.environ.get("TRUSTED_PROXY", "").lower() in ("true", "1", "yes")


class RateLimiter:
    def __init__(self):
        self._buckets = defaultdict(lambda: defaultdict(lambda: deque()))

    def _get_client_ip(self):
        # Only honour X-Forwarded-For when the deployment explicitly declares
        # a trusted reverse proxy. Otherwise a spoofed header would let an
        # attacker bypass per-client limits.
        if _trusts_proxy():
            xff = request.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[0].strip() or request.remote_addr or "unknown"
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
