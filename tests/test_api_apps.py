"""Smoke tests for the two web apps. They are skipped when the frameworks are not
installed (the pure logic they delegate to is covered by the other test modules)."""
import pytest


def test_sanic_app_imports_and_guards_requests():
    pytest.importorskip("sanic")
    from india_wireless_toolkit.services.sanic_api import app as module
    assert module.app.name == "india-wireless-analytics-api"
    assert module._guard.evaluate("GET", "/health", None, None, "1.1.1.1") is None


def test_blacksheep_app_imports_and_installs_the_security_middleware():
    pytest.importorskip("blacksheep")
    from india_wireless_toolkit.services.blacksheep_api import app as module
    assert module.security_middleware in module.app.middlewares
    assert module._split_feeds(["http://127.0.0.1/x", "ftp://x/y", 5])[0] == []
