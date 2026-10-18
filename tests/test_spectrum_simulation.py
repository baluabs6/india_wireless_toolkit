import math

import pytest

from india_wireless_toolkit import spectrum_simulation as s


def test_channels_available_matches_the_real_6ghz_band_plan():
    # lowest 20 MHz is a guard band: 500 MHz -> 24/12/6/3/1, 1200 MHz -> 59/29/14/7/3
    assert [s.channels_available(500, w) for w in (20, 40, 80, 160, 320)] == [24, 12, 6, 3, 1]
    assert [s.channels_available(1200, w) for w in (20, 40, 80, 160, 320)] == [59, 29, 14, 7, 3]


def test_channels_available_zero_when_too_narrow():
    assert s.channels_available(100, 160) == 0
    assert s.channels_available(10, 20) == 0


def test_free_space_path_loss_golden_values():
    # Friis: ~48.0 dB at 1 m / 6 GHz (the old code returned 108 dB, a 60 dB unit error)
    assert s.free_space_path_loss_db(1, 6.0) == pytest.approx(48.0, abs=0.1)
    assert s.free_space_path_loss_db(10, 6.0) == pytest.approx(68.0, abs=0.1)
    assert s.free_space_path_loss_db(1, 100.0) == pytest.approx(72.4, abs=0.1)


def test_path_loss_uses_free_space_reference_and_exponent():
    pl = s.log_distance_path_loss_db(10, freq_ghz=6.0, path_loss_exponent=3.0, wall_count=0)
    assert pl == pytest.approx(48.0 + 30.0, abs=0.1)
    assert s.log_distance_path_loss_db(10, 6.0, 3.0, wall_count=2, wall_loss_db=5) == pytest.approx(pl + 10, abs=1e-9)


def test_path_loss_increases_with_distance():
    assert s.log_distance_path_loss_db(20) > s.log_distance_path_loss_db(2)


def test_sinr_decreases_with_distance_and_is_sane_up_close():
    near, far = s.estimate_sinr_db(20.0, 2), s.estimate_sinr_db(20.0, 30)
    assert near > far
    assert near > 30            # a client 2 m from its AP must have a strong signal
    assert s.sinr_quality(near) in ("Good", "Excellent")


def test_sinr_rows_are_not_all_poor():
    rows = s.sinr_vs_distance_rows()
    assert any(r["quality"] in ("Good", "Excellent") for r in rows)
    assert [r["distance_m"] for r in rows] == sorted(r["distance_m"] for r in rows)


def test_phy_rates_are_derived_from_tone_plans():
    assert s.PHY_RATE_MBPS == {20: 172.1, 40: 344.1, 80: 720.6, 160: 1441.2, 320: 2882.4}


def test_more_spectrum_means_better_interference_in_monte_carlo():
    india = s.monte_carlo_ap_simulation.__wrapped__(500, n_aps=20, n_trials=60)
    us = s.monte_carlo_ap_simulation.__wrapped__(1200, n_aps=20, n_trials=60)
    assert us["mean_sinr_db"] > india["mean_sinr_db"] + 3      # the core claim of the toolkit
    assert india["mean_sinr_db"] > -10                          # no longer buried by the 60 dB error


def test_monte_carlo_is_deterministic_and_does_not_touch_global_rng():
    import random
    random.seed(123)
    expected = random.random()
    random.seed(123)
    a = s.monte_carlo_ap_simulation.__wrapped__(500, n_aps=5, n_trials=5, seed=7)
    assert random.random() == expected                          # global RNG untouched
    b = s.monte_carlo_ap_simulation.__wrapped__(500, n_aps=5, n_trials=5, seed=7)
    assert a == b


def test_monte_carlo_validates_input():
    with pytest.raises(ValueError):
        s.monte_carlo_ap_simulation.__wrapped__(500, n_aps=0)


def test_thz_atmospheric_loss_is_realistic_not_15_db_per_metre():
    # old model: 0.05*f dB per metre -> 15 dB at 1 m / 300 GHz. Real clear air is ~0.01 dB there.
    assert s.thz_atmospheric_loss_db(300, 1) < 0.1
    assert s.thz_atmospheric_loss_db(183, 1000) == pytest.approx(30.0)     # water-vapour peak, per km
    assert s.thz_absorption_db_per_km(50) == s.thz_absorption_db_per_km(100)   # clamped below table


def test_thz_rows_cover_band_and_total_is_sum():
    rows = s.thz_path_loss_rows()
    freqs = {r["freq_ghz"] for r in rows}
    assert {100, 300} <= freqs and all(100 <= f <= 300 for f in freqs)
    for r in rows:
        assert r["total_path_loss_db"] == pytest.approx(r["path_loss_db"] + r["atmospheric_loss_db"], abs=0.11)


def test_what_if_and_text_report_are_pure_and_complete():
    result = s.what_if_analysis(700, aps_per_building=10, n_trials=5)
    assert result["capacity_rows"][0][0] == 20 and result["dense_reuse_factor"] >= 1
    text = s.spectrum_text_report(aps=5, skip_montecarlo=True)
    assert "SINR vs distance" in text and "Terahertz" in text


def test_scenario_defaults_come_from_config():
    india, us = s.default_scenarios()
    assert india.total_mhz == s.INDIA_DELICENSED_MHZ and us.total_mhz == s.US_DELICENSED_MHZ
