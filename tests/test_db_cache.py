import json

import pytest

from india_wireless_toolkit import db


class FakeRedis:
    def __init__(self):
        self.store, self.fail, self.pings = {}, False, 0

    def ping(self):
        self.pings += 1
        if self.fail:
            raise ConnectionError("down")

    def get(self, key):
        if self.fail:
            raise ConnectionError("down")
        return self.store.get(key)

    def setex(self, key, ttl, value):
        if self.fail:
            raise ConnectionError("down")
        self.store[key] = value

    def delete(self, *keys):
        return sum(self.store.pop(k, None) is not None for k in keys)

    def scan_iter(self, match=None, count=None):
        prefix = match.rstrip("*")
        return iter([k for k in list(self.store) if k.startswith(prefix)])


def _cache(fake, **kw):
    return db.RedisCache(client=fake, enabled=True, **kw)


def test_cached_function_hits_cache_and_normalises_types(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(db, "_default_cache", _cache(fake))
    calls = []

    @db.cached("t:pair")
    def pair(x):
        calls.append(x)
        return {"values": (x, x * 2)}

    first, second = pair(3), pair(3)
    assert first == second == {"values": [3, 6]}            # tuple -> list on a miss AND on a hit
    assert calls == [3]
    assert pair(4) == {"values": [4, 8]} and calls == [3, 4]
    assert all(k.startswith(f"iwt:{db.CACHE_VERSION}:t:pair:") for k in fake.store)


def test_types_are_consistent_even_when_redis_is_disabled():
    @db.cached("t:off")
    def f():
        return {"t": (1, 2)}
    assert f() == {"t": [1, 2]}


def test_non_json_results_are_returned_but_not_cached(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(db, "_default_cache", _cache(fake))

    @db.cached("t:obj")
    def f():
        return {"x": object()}
    assert "x" in f() and fake.store == {}


def test_cache_key_changes_with_arguments_and_version(monkeypatch):
    k1 = db.make_cache_key("p", (1,), {})
    assert k1 == db.make_cache_key("p", (1,), {}) and k1 != db.make_cache_key("p", (2,), {})
    monkeypatch.setattr(db, "CACHE_VERSION", "v99")
    assert db.make_cache_key("p", (1,), {}) != k1 and ":v99:" in db.make_cache_key("p", (1,), {})


def test_flush_only_removes_the_toolkit_namespace():
    fake = FakeRedis()
    fake.store.update({"iwt:v2:a:1": "1", "iwt:v2:b:2": "2", "someone-elses-key": "keep", "session:9": "keep"})
    assert _cache(fake).flush_all() == 2
    assert set(fake.store) == {"someone-elses-key", "session:9"}


def test_connection_failures_are_retried_not_permanent():
    now = [100.0]
    fake = FakeRedis()
    cache = db.RedisCache(client=None, enabled=True, retry_after=30)
    cache._injected = True                     # use the fake instead of building a real client
    cache._client = fake
    fake.fail = True
    cache._connection_ok = False
    import time as _t
    real = _t.monotonic
    db.time.monotonic = lambda: now[0]
    try:
        assert cache.get("k") is None and fake.pings == 1           # first attempt fails
        now[0] += 10
        assert cache.get("k") is None and fake.pings == 1           # inside the back-off window: no hammering
        fake.fail = False
        now[0] += 31
        cache.set("k", {"a": 1})
        assert fake.pings == 2 and cache.get("k") == {"a": 1}       # recovered without a restart
    finally:
        db.time.monotonic = real


def test_runtime_errors_mark_the_connection_for_retry():
    fake = FakeRedis()
    cache = _cache(fake, retry_after=0)
    assert cache.set("k", 1) is True
    fake.fail = True
    assert cache.get("k") is None and cache._connection_ok is False
    fake.fail = False
    assert cache.get("k") == 1 and cache._connection_ok is True


def test_ssl_setting_parses_strings_not_truthiness(monkeypatch):
    monkeypatch.setenv("REDIS_SSL", "false")
    assert db.RedisCache(enabled=False).ssl is False
    monkeypatch.setenv("REDIS_SSL", "true")
    assert db.RedisCache(enabled=False).ssl is True
