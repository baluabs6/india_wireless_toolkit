"""
Redis-backed cache for expensive computations (simulations, report
generation). Fails gracefully: if Redis is unreachable or disabled in
config.yaml, everything falls back to computing fresh.

Behaviour notes
---------------
* Keys are namespaced (`iwt:<CACHE_VERSION>:<prefix>:<hash>`). Bump
  CACHE_VERSION whenever a cached function's output changes meaning, so
  stale results from an older model are never served. `flush_all()` only
  deletes this namespace - never the whole Redis database.
* A failed connection is retried after RETRY_AFTER_SECONDS instead of
  disabling the cache for the lifetime of the process.
* Values are normalised through JSON on every call (cache hit, miss, or
  Redis disabled), so callers always see the same types (e.g. tuples are
  always lists).
* Results that are not JSON-serialisable are returned but not cached.

Usage:
    from india_wireless_toolkit.db import cached

    @cached("spectrum:india:500mhz", ttl=3600)
    def expensive_simulation():
        ...
"""

import json
import time
import hashlib
import logging
import functools
import threading

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:  # redis is optional at runtime
    redis = None
    REDIS_AVAILABLE = False

from .config_loader import CONFIG

logger = logging.getLogger(__name__)

CACHE_NAMESPACE = "iwt"
CACHE_VERSION = "v2"           # bump when model outputs change (v2: path-loss / economics fixes)
RETRY_AFTER_SECONDS = 30


class RedisCache:
    def __init__(self, host=None, port=None, db=None, ttl=None, enabled=None,
                 password=None, ssl=None, client=None, retry_after=None):
        import os
        redis_cfg = CONFIG.get("redis", {})
        # Environment variables (e.g. set by docker-compose or Azure App
        # Settings) take precedence over config.yaml. Azure Cache for Redis
        # requires TLS (port 6380) and an access-key password.
        self.host = host or os.environ.get("REDIS_HOST") or redis_cfg.get("host", "localhost")
        self.port = int(port or os.environ.get("REDIS_PORT") or redis_cfg.get("port", 6379))
        self.db = db if db is not None else redis_cfg.get("db", 0)
        self.ttl = ttl or redis_cfg.get("ttl_seconds", 3600)
        self.enabled = enabled if enabled is not None else redis_cfg.get("enabled", True)
        self.password = password or os.environ.get("REDIS_PASSWORD") or redis_cfg.get("password") or None
        env_ssl = os.environ.get("REDIS_SSL")
        if ssl is not None:
            self.ssl = bool(ssl)
        elif env_ssl is not None:
            self.ssl = env_ssl.strip().lower() in ("1", "true", "yes")
        else:
            self.ssl = str(redis_cfg.get("ssl", False)).strip().lower() in ("1", "true", "yes")
        self.retry_after = RETRY_AFTER_SECONDS if retry_after is None else retry_after

        self._lock = threading.Lock()
        self._client = client
        self._injected = client is not None   # tests / callers can supply a client
        self._connection_ok = client is not None
        self._last_failure = 0.0

    # -- connection management ------------------------------------------------

    def _mark_failed(self, why):
        self._connection_ok = False
        self._last_failure = time.monotonic()
        logger.warning("[redis] unavailable (%s); running without cache, will retry in %ss.",
                       why, self.retry_after)

    def _get_client(self):
        if not self.enabled:
            return None
        if not self._injected and not REDIS_AVAILABLE:
            return None
        if self._connection_ok:
            return self._client
        if self._last_failure and (time.monotonic() - self._last_failure) < self.retry_after:
            return None
        with self._lock:
            if self._connection_ok:
                return self._client
            try:
                if self._client is None:
                    self._client = redis.Redis(
                        host=self.host, port=self.port, db=self.db,
                        password=self.password, ssl=self.ssl,
                        socket_connect_timeout=2, socket_timeout=2,
                        decode_responses=True,
                    )
                self._client.ping()
                self._connection_ok = True
                logger.info("[redis] connected to %s:%s", self.host, self.port)
            except Exception as e:
                self._mark_failed(e)
        return self._client if self._connection_ok else None

    # -- operations -----------------------------------------------------------

    def get(self, key: str):
        client = self._get_client()
        if client is None:
            return None
        try:
            value = client.get(key)
            return json.loads(value) if value is not None else None
        except Exception as e:
            self._mark_failed(f"get failed: {e}")
            return None

    def set(self, key: str, value, ttl: int = None):
        client = self._get_client()
        if client is None:
            return False
        try:
            client.setex(key, ttl or self.ttl, json.dumps(value))
            return True
        except Exception as e:
            self._mark_failed(f"set failed: {e}")
            return False

    def delete(self, key: str):
        client = self._get_client()
        if client is None:
            return False
        try:
            client.delete(key)
            return True
        except Exception as e:
            self._mark_failed(f"delete failed: {e}")
            return False

    def flush_namespace(self) -> int:
        """Delete only this toolkit's keys (iwt:*); never touches other data
        that may share the Redis instance. Returns the number of keys removed."""
        client = self._get_client()
        if client is None:
            return 0
        removed = 0
        try:
            batch = []
            for key in client.scan_iter(match=f"{CACHE_NAMESPACE}:*", count=500):
                batch.append(key)
                if len(batch) >= 500:
                    removed += client.delete(*batch)
                    batch = []
            if batch:
                removed += client.delete(*batch)
        except Exception as e:
            self._mark_failed(f"flush failed: {e}")
        return removed

    # Backwards-compatible name; now namespace-scoped (was FLUSHDB).
    def flush_all(self) -> int:
        return self.flush_namespace()


_default_cache = RedisCache()


def make_cache_key(key_prefix: str, args, kwargs) -> str:
    payload = json.dumps([args, kwargs], default=repr, sort_keys=True)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"{CACHE_NAMESPACE}:{CACHE_VERSION}:{key_prefix}:{digest}"


def cached(key_prefix: str, ttl: int = None):
    """
    Decorator that caches a function's JSON-serializable return value in
    Redis, keyed on key_prefix + a hash of the args/kwargs. Falls back to
    calling the function directly if Redis is unavailable.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            cache_key = make_cache_key(key_prefix, args, kwargs)

            cached_value = _default_cache.get(cache_key)
            if cached_value is not None:
                logger.debug("[redis] cache hit: %s", cache_key)
                return cached_value

            result = func(*args, **kwargs)
            try:
                payload = json.dumps(result)
            except (TypeError, ValueError):
                return result  # not JSON-serialisable: skip the cache entirely
            normalised = json.loads(payload)
            _default_cache.set(cache_key, normalised, ttl)
            return normalised
        wrapper.__wrapped__ = func
        return wrapper
    return decorator
