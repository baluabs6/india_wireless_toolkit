"""
Security helpers shared by both web APIs and the scraper.

1. SSRF-safe outbound fetching (`validate_public_url`, `safe_get`)
   The previous guard only rejected `localhost` and IP *literals*. A
   hostname that RESOLVES to a private address (DNS rebinding, `*.nip.io`,
   an internal name) or a redirect into the private network got through,
   and feedparser fetched with no timeout. Here every hostname is resolved
   and every address must be globally routable, redirects are followed
   manually with the same check at each hop, and size/time are capped.
   Residual risk: a DNS answer can still change between our check and the
   socket connect (TOCTOU). Keep an egress firewall / NSG rule as defence
   in depth.

2. API-key authentication (`AuthPolicy`) - constant-time comparison, multiple
   keys accepted for rotation (API_KEY="new,old").

3. Rate limiting (`RateLimiter`) - thread-safe sliding window, stricter
   limits for expensive endpoints. State is per-process; with several
   workers/instances each enforces its own window (put Azure Front Door /
   API Management in front for a global limit).

`evaluate_request()` is framework-neutral so the Sanic and Blacksheep apps
share identical behaviour.
"""

import hashlib
import hmac
import ipaddress
import logging
import os
import socket
import threading
import time
from collections import deque
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# SSRF-safe outbound requests
# --------------------------------------------------------------------------

class UnsafeURLError(ValueError):
    """The URL is not an acceptable public http(s) target."""


class ResponseTooLarge(Exception):
    pass


_ALLOWED_PORTS = {None, 80, 443}


def _ip_is_public(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_global and not ip.is_multicast


def validate_public_url(url: str, resolver=socket.getaddrinfo) -> str:
    """Return the url if it is an http(s) URL whose host resolves ONLY to
    public addresses; raise UnsafeURLError otherwise."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise UnsafeURLError(f"Malformed URL: {e}")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURLError("Only http(s) URLs with a hostname are allowed.")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed.")
    if port not in _ALLOWED_PORTS:
        raise UnsafeURLError(f"Port {port} is not allowed (use 80 or 443).")

    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise UnsafeURLError("Internal hostnames are not allowed.")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:                       # IP literal: judge it directly, no DNS involved
        if not _ip_is_public(str(literal)):
            raise UnsafeURLError("Non-public IP addresses are not allowed.")
        return url
    try:
        infos = resolver(host, port or (443 if parts.scheme == "https" else 80),
                         type=socket.SOCK_STREAM)
    except OSError as e:
        raise UnsafeURLError(f"Could not resolve host '{host}': {e}")
    if not infos:
        raise UnsafeURLError(f"Host '{host}' did not resolve.")
    for info in infos:
        address = info[4][0]
        if not _ip_is_public(address):
            raise UnsafeURLError(f"Host '{host}' resolves to a non-public address.")
    return url


def is_safe_public_url(url: str, resolver=socket.getaddrinfo) -> bool:
    try:
        validate_public_url(url, resolver=resolver)
        return True
    except UnsafeURLError:
        return False


def safe_get(url: str, *, headers: dict = None, timeout: float = 10.0,
             max_bytes: int = 2_000_000, max_redirects: int = 3,
             resolver=socket.getaddrinfo, session=None):
    """GET `url` with SSRF protection. Returns (final_url, body_bytes, response_headers).

    Raises UnsafeURLError, ResponseTooLarge, or requests exceptions."""
    import requests

    getter = (session or requests).get
    current = url
    for _ in range(max_redirects + 1):
        validate_public_url(current, resolver=resolver)
        resp = getter(current, headers=headers, timeout=timeout,
                      allow_redirects=False, stream=True)
        try:
            if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    raise UnsafeURLError("Redirect without a Location header.")
                current = urljoin(current, location)
                continue
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ResponseTooLarge(f"Response is {declared} bytes (limit {max_bytes}).")
            chunks, total = [], 0
            for chunk in resp.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > max_bytes:
                    raise ResponseTooLarge(f"Response exceeded {max_bytes} bytes.")
                chunks.append(chunk)
            return current, b"".join(chunks), dict(resp.headers)
        finally:
            resp.close()
    raise UnsafeURLError(f"Too many redirects (>{max_redirects}).")


# --------------------------------------------------------------------------
# API-key authentication
# --------------------------------------------------------------------------

class AuthPolicy:
    """Reads API_KEY (comma-separated for rotation). If no key is configured
    the API is open, unless API_REQUIRE_KEY=true, in which case every
    protected request is refused (fail closed)."""

    def __init__(self, keys=None, require=None):
        if keys is None:
            raw = os.environ.get("API_KEY", "")
            keys = [k.strip() for k in raw.split(",") if k.strip()]
        self.keys = list(keys)
        if require is None:
            require = os.environ.get("API_REQUIRE_KEY", "").strip().lower() in ("1", "true", "yes")
        self.require = require
        self._warned = False

    def check(self, provided: str):
        """Return (allowed, status, message)."""
        if not self.keys:
            if self.require:
                return False, 503, "API key authentication is required but no API_KEY is configured."
            if not self._warned:
                logger.warning("API_KEY is not set: the API is running WITHOUT authentication. "
                               "Set API_KEY (and API_REQUIRE_KEY=true) outside local development.")
                self._warned = True
            return True, 200, ""
        if not provided:
            return False, 401, "Missing X-API-Key header."
        candidate = provided.encode()
        ok = False
        for key in self.keys:
            # compare against every key so timing does not reveal which matched
            ok |= hmac.compare_digest(candidate, key.encode())
        if not ok:
            return False, 401, "Invalid API key."
        return True, 200, ""


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window limiter. allow(key, limit, window) -> (allowed, retry_after_seconds)."""

    def __init__(self, max_keys: int = 10_000, clock=time.monotonic):
        self._events = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys
        self._clock = clock

    def allow(self, key: str, limit: int, window_seconds: float = 60.0):
        now = self._clock()
        cutoff = now - window_seconds
        with self._lock:
            dq = self._events.get(key)
            if dq is None:
                if len(self._events) >= self._max_keys:
                    self._prune(cutoff)
                dq = self._events.setdefault(key, deque())
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                retry_after = max(1, int(dq[0] + window_seconds - now) + 1)
                return False, retry_after
            dq.append(now)
            return True, 0

    def _prune(self, cutoff):
        for k in [k for k, dq in self._events.items() if not dq or dq[-1] <= cutoff]:
            del self._events[k]
        if len(self._events) >= self._max_keys:      # still full: drop oldest quarter
            for k in list(self._events)[: self._max_keys // 4]:
                del self._events[k]


DEFAULT_RATE_LIMITS = {          # requests per minute, longest matching prefix wins
    "/agent/": 10,               # spends Anthropic credits
    "/report/generate": 5,
    "/news/scrape": 5,
    "/news/alert": 5,
    "/news/rss": 10,
    "/spectrum/montecarlo": 30,
    "/spectrum/whatif": 30,
    "/charts": 60,
}
DEFAULT_LIMIT_PER_MINUTE = 120
PUBLIC_PATHS = {"/health"}


def limit_for(path: str, overrides: dict = None) -> int:
    limits = dict(DEFAULT_RATE_LIMITS)
    if overrides:
        limits.update({str(k): int(v) for k, v in overrides.items() if k != "default"})
    best = None
    for prefix, value in limits.items():
        if path.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, value)
    if best:
        return best[1]
    return int((overrides or {}).get("default", DEFAULT_LIMIT_PER_MINUTE))


def client_id(api_key: str, forwarded_for: str, peer_ip: str, trust_proxy: bool) -> str:
    """Identify the caller for rate limiting: API key hash if present, else IP.
    X-Forwarded-For is only honoured when TRUST_PROXY=true (e.g. behind Azure
    App Service) because it is otherwise client-controlled."""
    if api_key:
        return "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:16]
    ip = peer_ip or "unknown"
    if trust_proxy and forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first.startswith("[") and "]" in first:      # [::1]:1234
            first = first[1:first.index("]")]
        elif first.count(":") == 1:                      # 1.2.3.4:5678
            first = first.split(":")[0]
        if first:
            ip = first
    return "ip:" + ip


class RequestGuard:
    """Bundles auth + rate limiting. One instance per process."""

    def __init__(self, auth: AuthPolicy = None, limiter: RateLimiter = None,
                 rate_overrides: dict = None, trust_proxy: bool = None):
        self.auth = auth or AuthPolicy()
        self.limiter = limiter or RateLimiter()
        self.rate_overrides = rate_overrides
        if trust_proxy is None:
            trust_proxy = os.environ.get("TRUST_PROXY", "").strip().lower() in ("1", "true", "yes")
        self.trust_proxy = trust_proxy

    def evaluate(self, method: str, path: str, api_key: str, forwarded_for: str, peer_ip: str):
        """Return None when the request may proceed, otherwise
        (status, json_body_dict, extra_headers_dict)."""
        if path in PUBLIC_PATHS:
            return None
        allowed, status, message = self.auth.check(api_key)
        if not allowed:
            headers = {"WWW-Authenticate": "ApiKey"} if status == 401 else {}
            return status, {"error": message}, headers
        who = client_id(api_key, forwarded_for, peer_ip, self.trust_proxy)
        limit = limit_for(path, self.rate_overrides)
        bucket = f"{who}|{path.split('/')[1] if path.count('/') >= 1 else path}"
        ok, retry_after = self.limiter.allow(bucket, limit)
        if not ok:
            return 429, {"error": "Rate limit exceeded.", "retry_after_seconds": retry_after}, \
                {"Retry-After": str(retry_after)}
        return None


def parse_bounded_int(raw, default: int, lo: int, hi: int) -> int:
    """Parse an int from untrusted input and clamp to [lo, hi]. Non-numeric
    input falls back to `default` rather than raising."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))
