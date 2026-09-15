import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from india_wireless_toolkit.spectrum_simulation import (
    channels_available, log_distance_path_loss_db, estimate_sinr_db,
)


def test_channels_available_exact_division():
    assert channels_available(500, 160) == 3
    assert channels_available(1200, 160) == 7


def test_channels_available_zero_when_too_narrow():
    assert channels_available(100, 160) == 0


def test_path_loss_increases_with_distance():
    pl_near = log_distance_path_loss_db(2)
    pl_far = log_distance_path_loss_db(20)
    assert pl_far > pl_near


def test_sinr_decreases_with_distance():
    sinr_near = estimate_sinr_db(20.0, 2)
    sinr_far = estimate_sinr_db(20.0, 30)
    assert sinr_near > sinr_far
