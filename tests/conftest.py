import os
import sys

import pytest

# Make the package importable when running `pytest` from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _no_real_redis(monkeypatch):
    """Tests must never talk to a real Redis (or depend on one being absent)."""
    from india_wireless_toolkit import db
    monkeypatch.setattr(db, "_default_cache", db.RedisCache(enabled=False))
