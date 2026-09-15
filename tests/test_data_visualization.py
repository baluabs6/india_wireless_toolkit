import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from india_wireless_toolkit.data_visualization import load_subscriber_trends, load_state_data


def test_subscriber_trends_loads_rows():
    rows = load_subscriber_trends()
    assert len(rows) > 0
    assert "wireless_broadband_millions" in rows[0]


def test_state_data_loads_rows():
    rows = load_state_data()
    assert len(rows) > 0
    assert "coverage_5g_pct" in rows[0]
