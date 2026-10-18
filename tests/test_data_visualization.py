import os
import sys

import pytest

from india_wireless_toolkit import data_visualization as dv
from india_wireless_toolkit.data_visualization import load_subscriber_trends, load_state_data


def test_subscriber_trends_loads_rows():
    rows = load_subscriber_trends()
    assert len(rows) > 0
    assert "wireless_broadband_millions" in rows[0]


def test_state_data_loads_rows():
    rows = load_state_data()
    assert len(rows) > 0
    assert "coverage_5g_pct" in rows[0]


def test_national_stats_drive_the_charts():
    stats = dv.load_national_stats()
    assert stats["rural_teledensity_pct"]["value"] < stats["urban_teledensity_pct"]["value"]
    assert stats["urban_teledensity_pct"]["as_of"]


def test_unknown_rural_teledensity_is_blank_not_zero():
    delhi = [r for r in load_state_data() if r["state"] == "Delhi"][0]
    assert delhi["rural_teledensity_pct"] == ""
    assert dv._to_float(delhi["rural_teledensity_pct"]) is None
    assert dv._to_float("64.1") == 64.1 and dv._to_float("abc") is None


def test_package_imports_without_plotly():
    # plotly is only needed by the dashboard/map, which fail with a clear message
    if "plotly.graph_objects" in sys.modules:
        pytest.skip("plotly is installed here")
    with pytest.raises(RuntimeError, match="plotly"):
        dv.build_interactive_dashboard()


def test_map_rejects_unknown_metric():
    with pytest.raises(ValueError):
        dv.build_state_bubble_map(metric="../../etc/passwd")


def test_ensure_chart_builds_once_then_reuses(tmp_path, monkeypatch):
    monkeypatch.setattr(dv, "OUT_DIR", str(tmp_path))
    first = dv.ensure_chart("broadband_split")
    assert os.path.exists(first) and first.startswith(str(tmp_path))
    mtime = os.path.getmtime(first)
    again = dv.ensure_chart("broadband_split")
    assert again == first and os.path.getmtime(first) == mtime        # not regenerated
    assert (os.stat(first).st_mode & 0o044) != 0                      # readable by other users/mounts
    with pytest.raises(KeyError):
        dv.ensure_chart("nope")


def test_all_charts_build(tmp_path, monkeypatch):
    monkeypatch.setattr(dv, "OUT_DIR", str(tmp_path))
    for name in dv.CHART_BUILDERS:
        path = dv.ensure_chart(name, force=True)
        with open(path, "rb") as f:
            assert f.read(8) == b"\x89PNG\r\n\x1a\n"
