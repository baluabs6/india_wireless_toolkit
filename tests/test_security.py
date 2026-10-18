import socket

import pytest

from india_wireless_toolkit import security as sec


def _resolver(*addresses):
    def resolve(host, port, type=None):
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]
    return resolve


PUBLIC = _resolver("93.184.216.34")


@pytest.mark.parametrize("url", [
    "http://localhost/x", "http://169.254.169.254/latest/meta-data", "http://127.0.0.1:80/",
    "http://10.1.2.3/", "http://[::1]/", "ftp://example.com/x", "file:///etc/passwd", "http:///nohost",
    "https://user:pw@example.com/",  # pragma: allowlist secret
    "https://example.com:8443/x", "http://metadata.internal/",
    "http://printer.local/", "javascript:alert(1)",
])
def test_unsafe_urls_are_rejected(url):
    assert sec.is_safe_public_url(url, resolver=PUBLIC) is False


def test_hostname_resolving_to_private_address_is_rejected():
    # the old guard only inspected IP literals, so DNS rebinding / nip.io style names got through
    for private in ("10.0.0.5", "192.168.1.1", "127.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "::ffff:10.0.0.1", "fd00::1"):
        assert sec.is_safe_public_url("https://evil.example.com/feed", resolver=_resolver(private)) is False, private
    # one bad record among good ones is enough to refuse
    assert sec.is_safe_public_url("https://x.example.com/", resolver=_resolver("93.184.216.34", "10.0.0.5")) is False


def test_public_url_and_resolution_failure():
    assert sec.is_safe_public_url("https://example.com/feed.xml", resolver=PUBLIC) is True
    assert sec.is_safe_public_url("http://example.com:80/feed", resolver=PUBLIC) is True

    def failing(host, port, type=None):
        raise socket.gaierror("nope")
    assert sec.is_safe_public_url("https://no-such-host.example/", resolver=failing) is False


class _Resp:
    def __init__(self, status=200, headers=None, chunks=(b"ok",)):
        self.status_code, self.headers, self._chunks = status, headers or {}, chunks
        self.is_redirect = status in (301, 302, 303, 307, 308)
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def iter_content(self, chunk_size=0):
        yield from self._chunks

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, *responses):
        self.responses, self.requested = list(responses), []

    def get(self, url, **kw):
        assert kw["allow_redirects"] is False and kw["timeout"]
        self.requested.append(url)
        return self.responses.pop(0)


def test_safe_get_returns_body():
    final, body, _ = sec.safe_get("https://example.com/f", resolver=PUBLIC,
                                  session=_Session(_Resp(chunks=(b"he", b"llo"))))
    assert final == "https://example.com/f" and body == b"hello"


def test_safe_get_follows_redirects_but_revalidates_each_hop():
    hosts = {"example.com": "93.184.216.34", "internal.example.org": "10.0.0.9"}

    def resolve(host, port, type=None):
        return _resolver(hosts[host])(host, port)
    session = _Session(_Resp(302, {"Location": "https://internal.example.org/admin"}))
    with pytest.raises(sec.UnsafeURLError):
        sec.safe_get("https://example.com/start", resolver=resolve, session=session)
    assert session.requested == ["https://example.com/start"]               # never contacted the private host

    ok = _Session(_Resp(301, {"Location": "/moved"}), _Resp(chunks=(b"done",)))
    assert sec.safe_get("https://example.com/a", resolver=resolve, session=ok)[:2] == ("https://example.com/moved", b"done")


def test_safe_get_limits_redirects_and_size():
    loop = _Session(*[_Resp(302, {"Location": "https://example.com/n"}) for _ in range(10)])
    with pytest.raises(sec.UnsafeURLError, match="redirects"):
        sec.safe_get("https://example.com/", resolver=PUBLIC, session=loop, max_redirects=3)
    with pytest.raises(sec.ResponseTooLarge):
        sec.safe_get("https://example.com/", resolver=PUBLIC, max_bytes=10,
                     session=_Session(_Resp(chunks=(b"x" * 8, b"y" * 8))))
    with pytest.raises(sec.ResponseTooLarge):
        sec.safe_get("https://example.com/", resolver=PUBLIC, max_bytes=10,
                     session=_Session(_Resp(headers={"Content-Length": "999"})))


# --- auth ------------------------------------------------------------------------------------

def test_auth_policy_modes():
    open_policy = sec.AuthPolicy(keys=[], require=False)
    assert open_policy.check(None)[0] is True
    closed = sec.AuthPolicy(keys=[], require=True)
    assert closed.check("anything")[:2] == (False, 503)
    keyed = sec.AuthPolicy(keys=["new", "old"], require=False)
    assert keyed.check("new")[0] and keyed.check("old")[0]                 # rotation: both accepted
    assert keyed.check(None)[:2] == (False, 401) and keyed.check("wrong")[:2] == (False, 401)
    assert keyed.check("")[1] == 401 and keyed.check("ne")[1] == 401


def test_auth_policy_reads_environment(monkeypatch):
    monkeypatch.setenv("API_KEY", " a , b ")  # pragma: allowlist secret
    monkeypatch.setenv("API_REQUIRE_KEY", "true")
    policy = sec.AuthPolicy()
    assert policy.keys == ["a", "b"] and policy.require is True


# --- rate limiting -----------------------------------------------------------------------------

def test_rate_limiter_sliding_window():
    now = [0.0]
    limiter = sec.RateLimiter(clock=lambda: now[0])
    assert all(limiter.allow("k", 3, 60)[0] for _ in range(3))
    allowed, retry = limiter.allow("k", 3, 60)
    assert allowed is False and 1 <= retry <= 61
    assert limiter.allow("other", 3, 60)[0] is True                        # per-key
    now[0] = 61.0
    assert limiter.allow("k", 3, 60)[0] is True                            # window slid


def test_rate_limiter_memory_is_bounded():
    now = [0.0]
    limiter = sec.RateLimiter(max_keys=100, clock=lambda: now[0])
    for i in range(1000):
        now[0] += 0.01
        limiter.allow(f"k{i}", 5, 60)
    assert len(limiter._events) <= 100


def test_limit_for_uses_longest_prefix_and_overrides():
    assert sec.limit_for("/agent/ask") == 10 and sec.limit_for("/infra/npv") == sec.DEFAULT_LIMIT_PER_MINUTE
    assert sec.limit_for("/report/generate") == 5 and sec.limit_for("/report") == sec.DEFAULT_LIMIT_PER_MINUTE
    assert sec.limit_for("/agent/ask", {"/agent/": 2}) == 2
    assert sec.limit_for("/x", {"default": 7}) == 7


def test_client_id_only_trusts_forwarded_for_behind_a_proxy():
    assert sec.client_id(None, "6.6.6.6", "1.2.3.4", trust_proxy=False) == "ip:1.2.3.4"
    assert sec.client_id(None, "6.6.6.6:5555, 10.0.0.1", "10.0.0.1", trust_proxy=True) == "ip:6.6.6.6"
    assert sec.client_id(None, "[2001:db8::1]:443", "x", trust_proxy=True) == "ip:2001:db8::1"
    key_id = sec.client_id("not-a-real-key", None, "1.2.3.4", False)  # pragma: allowlist secret
    assert key_id.startswith("key:") and "not-a-real-key" not in key_id


def test_request_guard_end_to_end():
    guard = sec.RequestGuard(auth=sec.AuthPolicy(keys=["k"], require=False), trust_proxy=False)
    assert guard.evaluate("GET", "/health", None, None, "1.1.1.1") is None            # public
    status, body, headers = guard.evaluate("GET", "/infra/npv", None, None, "1.1.1.1")
    assert status == 401 and "WWW-Authenticate" in headers and "key" not in body["error"].lower().replace("x-api-key", "")
    assert guard.evaluate("GET", "/infra/npv", "k", None, "1.1.1.1") is None
    results = [guard.evaluate("POST", "/news/scrape", "k", None, "1.1.1.1") for _ in range(7)]
    assert results[:5] == [None] * 5 and results[5][0] == 429 and "Retry-After" in results[5][2]
    # a different route has its own bucket
    assert guard.evaluate("GET", "/infra/npv", "k", None, "1.1.1.1") is None


def test_parse_bounded_int():
    assert sec.parse_bounded_int("5", 1, 1, 10) == 5 and sec.parse_bounded_int("500", 1, 1, 10) == 10
    assert sec.parse_bounded_int("-5", 1, 1, 10) == 1 and sec.parse_bounded_int("x", 7, 1, 10) == 7
