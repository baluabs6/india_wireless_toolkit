"""
Redis-backed cache for expensive computations (simulations, scraped news,
report generation). Fails gracefully: if Redis is unreachable or disabled
in config.yaml, everything falls back to computing fresh with a printed
warning instead of crashing.

Usage:
    from india_wireless_toolkit.db import cached

    @cached("spectrum:india:500mhz", ttl=3600)
    def expensive_simulation():
        ...

Or use RedisCache directly for manual get/set/flush.
"""

import json
import functools
import hashlib

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

from .config_loader import CONFIG


class RedisCache:
    def __init__(self, host=None, port=None, db=None, ttl=None, enabled=None,
                 password=None, ssl=None):
        import os
        redis_cfg = CONFIG.get("redis", {})
        # Environment variables (e.g. set by docker-compose or Azure App
        # Settings) take precedence over config.yaml. Azure Cache for Redis
        # requires TLS (port 6380) and an access-key password, so both are
        # first-class here rather than bolted on.
        self.host = host or os.environ.get("REDIS_HOST") or redis_cfg.get("host", "localhost")
        self.port = int(port or os.environ.get("REDIS_PORT") or redis_cfg.get("port", 6379))
        self.db = db if db is not None else redis_cfg.get("db", 0)
        self.ttl = ttl or redis_cfg.get("ttl_seconds", 3600)
        self.enabled = enabled if enabled is not None else redis_cfg.get("enabled", True)
        self.password = password or os.environ.get("REDIS_PASSWORD") or redis_cfg.get("password") or None
        env_ssl = os.environ.get("REDIS_SSL")
        self.ssl = (
            ssl if ssl is not None
            else (env_ssl.lower() in ("1", "true", "yes") if env_ssl is not None
                  else redis_cfg.get("ssl", False))
        )
        self._client = None
        self._connection_checked = False
        self._connection_ok = False

    def _get_client(self):
        if not REDIS_AVAILABLE or not self.enabled:
            return None
        if self._client is None:
            self._client = redis.Redis(
                host=self.host, port=self.port, db=self.db,
                password=self.password, ssl=self.ssl,
                socket_connect_timeout=2, socket_timeout=2,
                decode_responses=True,
            )
        if not self._connection_checked:
            try:
                self._client.ping()
                self._connection_ok = True
            except Exception as e:
                print(f"[redis] Not available ({e}). Falling back to no-cache mode.")
                self._connection_ok = False
            self._connection_checked = True
        return self._client if self._connection_ok else None

    def get(self, key: str):
        client = self._get_client()
        if client is None:
            return None
        try:
            value = client.get(key)
            return json.loads(value) if value is not None else None
        except Exception as e:
            print(f"[redis] get() failed: {e}")
            return None

    def set(self, key: str, value, ttl: int = None):
        client = self._get_client()
        if client is None:
            return False
        try:
            client.setex(key, ttl or self.ttl, json.dumps(value))
            return True
        except Exception as e:
            print(f"[redis] set() failed: {e}")
            return False

    def delete(self, key: str):
        client = self._get_client()
        if client is None:
            return False
        try:
            client.delete(key)
            return True
        except Exception:
            return False

    def flush_all(self):
        client = self._get_client()
        if client is None:
            return False
        try:
            client.flushdb()
            return True
        except Exception:
            return False


_default_cache = RedisCache()


def cached(key_prefix: str, ttl: int = None):
    """
    Decorator that caches a function's JSON-serializable return value in
    Redis, keyed on key_prefix + a hash of the args/kwargs. Falls back to
    calling the function directly if Redis is unavailable.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            arg_hash = hashlib.md5(
                json.dumps([args, kwargs], default=str, sort_keys=True).encode()
            ).hexdigest()[:10]
            cache_key = f"{key_prefix}:{arg_hash}"

            cached_value = _default_cache.get(cache_key)
            if cached_value is not None:
                print(f"[redis] cache hit: {cache_key}")
                return cached_value

            result = func(*args, **kwargs)
            _default_cache.set(cache_key, result, ttl)
            return result
        return wrapper
    return decorator
