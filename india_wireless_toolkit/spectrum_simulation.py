"""
6 GHz / 6G Spectrum Simulator - India (v2.1)
---------------------------------------------
Features:
  1. Channel capacity modeling for India vs a fully delicensed market.
  2. Path-loss / SINR model estimating signal degradation with distance
     and walls (log-distance path loss, referenced to TRUE free-space loss
     at 1 m).
  3. Monte Carlo simulation: randomly places APs in a floorplan and reports
     average SINR / co-channel interference under a constrained channel count.
  4. 6G terahertz (THz) band illustrative modeling (free-space loss plus a
     per-band molecular-absorption table).
  5. "What-if" mode: pass a custom MHz allocation and get instant results.

Design: every analysis has a pure function that RETURNS data
(`scenario_capacity`, `sinr_vs_distance_rows`, `thz_path_loss_rows`,
`what_if_analysis`, ...) and a `format_*` function that turns the data into
text. The `*_report` / `simulate_scenario` helpers print that text and still
return the data, so the CLI, report generator, agent and APIs all share one
implementation (nothing needs to capture stdout any more).

Modeling limits (read before quoting numbers):
  * Not a substitute for an RF site survey. Indoor log-distance model, fixed
    wall loss, single 20 dBm transmit power by default.
  * The Monte Carlo counts co-channel interference only (no adjacent-channel
    leakage, no clients roaming, no airtime sharing / CSMA).
  * PHY rates are single-stream peak rates, i.e. an upper bound, not user
    throughput.

Caches expensive Monte Carlo runs in Redis when available
(india_wireless_toolkit.db).
"""

import argparse
import math
import random
from dataclasses import dataclass

from .config_loader import CONFIG
from .db import cached

_SPEC_CFG = CONFIG.get("spectrum", {})
INDIA_DELICENSED_MHZ = int(_SPEC_CFG.get("india_delicensed_mhz", 500))
FULL_BAND_MHZ = int(_SPEC_CFG.get("full_band_mhz", 1200))
US_DELICENSED_MHZ = int(_SPEC_CFG.get("us_delicensed_mhz", 1200))
THZ_FREQ_RANGE_GHZ = tuple(_SPEC_CFG.get("thz_band_ghz_range", [100, 300]))

SPEED_OF_LIGHT_M_S = 299_792_458.0
CHANNEL_WIDTHS = [20, 40, 80, 160, 320]

# Data subcarriers per channel width (802.11ax/be tone plans) and the PHY
# parameters for the top single-stream rate: MCS 13 = 4096-QAM (12 bits per
# subcarrier), coding rate 5/6, 12.8 us symbol + 0.8 us guard interval.
_DATA_TONES = {20: 234, 40: 468, 80: 980, 160: 1960, 320: 3920}


def phy_rate_mbps(width_mhz: int, bits_per_symbol: int = 12, coding_rate: float = 5 / 6,
                  symbol_time_us: float = 13.6, streams: int = 1) -> float:
    """Peak PHY rate for one channel: tones x bits x coding / symbol time."""
    return _DATA_TONES[width_mhz] * bits_per_symbol * coding_rate * streams / symbol_time_us


# Derived rather than hand-typed (the old table understated 80/160 MHz by ~4.5%).
PHY_RATE_MBPS = {w: round(phy_rate_mbps(w), 1) for w in CHANNEL_WIDTHS}


@dataclass
class SpectrumScenario:
    name: str
    total_mhz: int


def default_scenarios():
    """India (current) and US (fully delicensed) scenarios from config.yaml."""
    return [
        SpectrumScenario(f"India (current, {INDIA_DELICENSED_MHZ} MHz delicensed)", INDIA_DELICENSED_MHZ),
        SpectrumScenario(f"US (full band delicensed, {US_DELICENSED_MHZ} MHz)", US_DELICENSED_MHZ),
    ]


# The 6 GHz band plan reserves the lowest 20 MHz (5925-5945 MHz) as a guard band, so
# 1200 MHz gives 59 x 20 / 29 x 40 / 14 x 80 / 7 x 160 / 3 x 320 MHz channels and the
# 500 MHz lower band gives 24 / 12 / 6 / 3 / 1 (not 60/30/15 and 25 as plain division says).
GUARD_BAND_MHZ = 20


def channels_available(total_mhz: int, width: int, guard_mhz: int = GUARD_BAND_MHZ) -> int:
    """Number of non-overlapping `width` MHz channels in `total_mhz` of contiguous
    spectrum, after the band-edge guard."""
    return max(0, total_mhz - guard_mhz) // width


# --- Feature 1: capacity --------------------------------------------------------

def scenario_capacity(scenario: SpectrumScenario):
    """Pure: [(width_mhz, n_channels, aggregate_phy_mbps), ...] for widths that fit."""
    rows = []
    for width in CHANNEL_WIDTHS:
        n_channels = channels_available(scenario.total_mhz, width)
        if n_channels == 0:
            continue
        rows.append((width, n_channels, n_channels * PHY_RATE_MBPS[width]))
    return rows


def format_scenario(scenario: SpectrumScenario, rows) -> str:
    lines = [f"\n=== {scenario.name}: {scenario.total_mhz} MHz available ==="]
    for width, n_channels, total_phy_mbps in rows:
        lines.append(
            f"  {width:>3} MHz channels: {n_channels:>2} channels available "
            f"| aggregate PHY capacity ~ {total_phy_mbps:,.0f} Mbps "
            f"({total_phy_mbps / 1000:.1f} Gbps)"
        )
    return "\n".join(lines)


def simulate_scenario(scenario: SpectrumScenario):
    """Prints the capacity table and returns the rows (see scenario_capacity)."""
    rows = scenario_capacity(scenario)
    print(format_scenario(scenario, rows))
    return rows


def dense_deployment_reuse(scenario: SpectrumScenario, aps_per_building: int = 20, width: int = 160):
    """Pure: channel-reuse factor for N APs that each need a `width` MHz channel
    (None if no such channel fits)."""
    n_channels = channels_available(scenario.total_mhz, width)
    if n_channels == 0:
        return None
    return max(1, -(-aps_per_building // n_channels))


def format_dense(scenario: SpectrumScenario, aps_per_building: int, reuse, width: int = 160) -> str:
    if reuse is None:
        return f"  No {width} MHz channel fits in {scenario.total_mhz} MHz."
    n_channels = channels_available(scenario.total_mhz, width)
    return (
        f"  With {aps_per_building} APs needing {width} MHz channels: "
        f"{n_channels} clean channels -> each channel reused by ~{reuse} AP(s), "
        f"{'no' if reuse == 1 else 'some'} co-channel interference expected."
    )


def dense_deployment_estimate(scenario: SpectrumScenario, aps_per_building: int = 20):
    reuse = dense_deployment_reuse(scenario, aps_per_building)
    print(format_dense(scenario, aps_per_building, reuse))
    return reuse


# --- Feature 2: Path-loss / SINR model --------------------------------------------

def free_space_path_loss_db(distance_m: float, freq_ghz: float) -> float:
    """Friis free-space path loss: 20*log10(4*pi*d*f/c).
    At 1 m and 6 GHz this is ~48 dB (the previous code used the
    kilometre/MHz constant with a metre distance, which overstated it by 60 dB)."""
    if distance_m <= 0 or freq_ghz <= 0:
        raise ValueError("distance_m and freq_ghz must be positive.")
    return 20 * math.log10(4 * math.pi * distance_m * freq_ghz * 1e9 / SPEED_OF_LIGHT_M_S)


def log_distance_path_loss_db(distance_m: float, freq_ghz: float = 6.0,
                              path_loss_exponent: float = 3.0,
                              wall_count: int = 0, wall_loss_db: float = 5.0) -> float:
    """
    Log-distance path loss:
        PL(d) = FSPL(d0) + 10*n*log10(d/d0) + wall attenuation
    d0 = 1 m reference distance. Reasonable indoor approximation, not a
    substitute for a real RF site survey.
    """
    d0 = 1.0
    pl = free_space_path_loss_db(d0, freq_ghz) + \
        10 * path_loss_exponent * math.log10(max(distance_m, d0) / d0)
    return pl + wall_count * wall_loss_db


def estimate_sinr_db(tx_power_dbm: float, distance_m: float, interference_dbm: float = -85.0,
                     noise_floor_dbm: float = -95.0, freq_ghz: float = 6.0,
                     wall_count: int = 1) -> float:
    path_loss = log_distance_path_loss_db(distance_m, freq_ghz=freq_ghz, wall_count=wall_count)
    rx_power_dbm = tx_power_dbm - path_loss
    interference_plus_noise = 10 * math.log10(
        10 ** (interference_dbm / 10) + 10 ** (noise_floor_dbm / 10)
    )
    return rx_power_dbm - interference_plus_noise


def sinr_quality(sinr_db: float) -> str:
    return (
        "Excellent" if sinr_db > 30 else
        "Good" if sinr_db > 20 else
        "Fair" if sinr_db > 10 else
        "Poor"
    )


def sinr_vs_distance_rows(distances=(2, 5, 10, 15, 20, 30), tx_power_dbm: float = 20.0,
                          metres_per_wall: float = 8.0):
    """Pure: [{distance_m, walls, sinr_db, quality}, ...]."""
    rows = []
    for d in distances:
        walls = max(0, int(d // metres_per_wall))
        sinr = estimate_sinr_db(tx_power_dbm, d, wall_count=walls)
        rows.append({"distance_m": d, "walls": walls,
                     "sinr_db": round(sinr, 1), "quality": sinr_quality(sinr)})
    return rows


def format_sinr_rows(rows, tx_power_dbm: float) -> str:
    lines = [f"\n=== SINR vs distance (6 GHz, tx_power={tx_power_dbm} dBm) ==="]
    for r in rows:
        lines.append(f"  {r['distance_m']:>3}m ({r['walls']} walls): "
                     f"SINR ~ {r['sinr_db']:5.1f} dB -> {r['quality']}")
    return "\n".join(lines)


def sinr_vs_distance_report(distances=(2, 5, 10, 15, 20, 30), tx_power_dbm: float = 20.0):
    rows = sinr_vs_distance_rows(distances, tx_power_dbm)
    print(format_sinr_rows(rows, tx_power_dbm))
    return rows


# --- Feature 3: Monte Carlo AP placement simulation --------------------------------

@cached("spectrum:montecarlo")
def monte_carlo_ap_simulation(total_mhz: int, floor_width_m: float = 40, floor_height_m: float = 40,
                              n_aps: int = 20, n_trials: int = 500, seed: int = 42,
                              tx_power_dbm: float = 20.0):
    if total_mhz <= 0 or n_aps < 1 or n_trials < 1:
        raise ValueError("total_mhz, n_aps and n_trials must be positive.")
    rng = random.Random(seed)       # local RNG: no global state, deterministic per call
    n_channels = max(1, channels_available(total_mhz, 160))

    avg_sinrs = []
    for _ in range(n_trials):
        positions = [(rng.uniform(0, floor_width_m), rng.uniform(0, floor_height_m))
                     for _ in range(n_aps)]
        channels = [rng.randint(0, n_channels - 1) for _ in range(n_aps)]

        trial_sinrs = []
        for i in range(n_aps):
            client_distance = rng.uniform(2, 10)
            interference_mw = 0.0
            for j in range(n_aps):
                if j == i or channels[j] != channels[i]:
                    continue
                dx = positions[i][0] - positions[j][0]
                dy = positions[i][1] - positions[j][1]
                dist = max(1.0, math.hypot(dx, dy))
                pl = log_distance_path_loss_db(dist, wall_count=2)
                interference_mw += 10 ** ((tx_power_dbm - pl) / 10)

            interference_dbm = 10 * math.log10(interference_mw) if interference_mw > 0 else -100.0
            trial_sinrs.append(
                estimate_sinr_db(tx_power_dbm, client_distance,
                                 interference_dbm=interference_dbm, wall_count=1)
            )
        avg_sinrs.append(sum(trial_sinrs) / len(trial_sinrs))

    return {
        "total_mhz": total_mhz,
        "n_channels_160mhz": n_channels,
        "n_aps": n_aps,
        "n_trials": n_trials,
        "seed": seed,
        "mean_sinr_db": sum(avg_sinrs) / len(avg_sinrs),
        "min_sinr_db": min(avg_sinrs),
        "max_sinr_db": max(avg_sinrs),
    }


def format_monte_carlo(result: dict, label: str) -> str:
    return (
        f"\n=== Monte Carlo AP simulation: {label} ===\n"
        f"  {result['n_channels_160mhz']} x 160MHz channels for {result['n_aps']} APs "
        f"across {result['n_trials']} random trials\n"
        f"  Mean SINR: {result['mean_sinr_db']:.1f} dB "
        f"(range {result['min_sinr_db']:.1f} to {result['max_sinr_db']:.1f} dB)"
    )


def print_monte_carlo_result(result: dict, label: str):
    print(format_monte_carlo(result, label))


# --- Feature 4: 6G terahertz band (illustrative) -----------------------------------

# Illustrative clear-air molecular absorption at sea level (dB per KILOMETRE),
# order-of-magnitude values following the shape of ITU-R P.676 (windows near
# 140 and 220 GHz, water-vapour lines near 183 and 325 GHz). Use the full ITU
# model for real link budgets. (The previous model used 0.05*f(GHz) dB per
# METRE - about three orders of magnitude too high.)
_THZ_ABSORPTION_DB_PER_KM = [
    (100, 0.5), (140, 1.5), (183, 30.0), (220, 5.0), (300, 12.0), (325, 40.0),
]
_THZ_SWEEP_EXTRAS_GHZ = (140, 183, 220)   # window / absorption-peak frequencies worth showing


def thz_absorption_db_per_km(freq_ghz: float) -> float:
    pts = _THZ_ABSORPTION_DB_PER_KM
    if freq_ghz <= pts[0][0]:
        return pts[0][1]
    if freq_ghz >= pts[-1][0]:
        return pts[-1][1]
    for (f0, a0), (f1, a1) in zip(pts, pts[1:]):
        if f0 <= freq_ghz <= f1:
            return a0 + (a1 - a0) * (freq_ghz - f0) / (f1 - f0)
    return pts[-1][1]


def thz_atmospheric_loss_db(freq_ghz: float, distance_m: float) -> float:
    return thz_absorption_db_per_km(freq_ghz) * distance_m / 1000.0


def thz_path_loss_rows(freq_ghz_range=None, distance_m=(1, 5, 10, 20, 100),
                       path_loss_exponent: float = 2.0):
    """Pure: free-space (exponent 2 = line of sight) + molecular absorption
    for the band edges plus the 140/183/220 GHz window/peak frequencies."""
    lo, hi = freq_ghz_range or THZ_FREQ_RANGE_GHZ
    freqs = sorted({lo, hi, *[f for f in _THZ_SWEEP_EXTRAS_GHZ if lo <= f <= hi]})
    rows = []
    for freq in freqs:
        for d in distance_m:
            pl = log_distance_path_loss_db(d, freq_ghz=freq, path_loss_exponent=path_loss_exponent,
                                           wall_count=0)
            atm = thz_atmospheric_loss_db(freq, d)
            rows.append({
                "freq_ghz": freq, "distance_m": d,
                "path_loss_db": round(pl, 1),
                "atmospheric_loss_db": round(atm, 2),
                "total_path_loss_db": round(pl + atm, 1),
            })
    return rows


THZ_TAKEAWAY = (
    "Takeaway: at 100-300 GHz free-space loss alone is ~70-110 dB at 1-20 m, about 25-35 dB "
    "worse than 6 GHz at the same distance (loss grows with frequency squared). Molecular "
    "absorption is negligible at room scale but grows with range, especially near the "
    "183/325 GHz water-vapour lines. Practical 6G THz use therefore needs high-gain "
    "directional antennas and line of sight - short-range 'innovation zone' pilots, not "
    "wide-area coverage."
)


def format_thz(rows, freq_range) -> str:
    lines = [f"\n=== 6G Terahertz band illustrative model ({freq_range[0]}-{freq_range[1]} GHz) ==="]
    current = None
    for r in rows:
        if r["freq_ghz"] != current:
            current = r["freq_ghz"]
            lines.append(f"\n  At {current} GHz:")
        lines.append(f"    {r['distance_m']:>4}m: path loss ~ {r['total_path_loss_db']:6.1f} dB "
                     f"(free-space {r['path_loss_db']:.1f} dB + atmospheric {r['atmospheric_loss_db']:.2f} dB)")
    lines.append("\n  " + THZ_TAKEAWAY)
    return "\n".join(lines)


def thz_band_report(freq_ghz_range=None, distance_m=(1, 5, 10, 20, 100)):
    freq_range = tuple(freq_ghz_range or THZ_FREQ_RANGE_GHZ)
    rows = thz_path_loss_rows(freq_range, distance_m)
    print(format_thz(rows, freq_range))
    return rows


# --- Feature 5: What-if mode ----------------------------------------------------------

def what_if_analysis(custom_mhz: int, aps_per_building: int = 20, n_trials: int = 200):
    """Pure: capacity table, reuse factor and Monte Carlo for a custom allocation."""
    scenario = SpectrumScenario(f"What-if: {custom_mhz} MHz delicensed", custom_mhz)
    rows = scenario_capacity(scenario)
    return {
        "scenario": scenario,
        "capacity_rows": rows,
        "dense_reuse_factor": dense_deployment_reuse(scenario, aps_per_building),
        "monte_carlo": monte_carlo_ap_simulation(custom_mhz, n_aps=aps_per_building, n_trials=n_trials),
    }


def what_if(custom_mhz: int, aps_per_building: int = 20):
    result = what_if_analysis(custom_mhz, aps_per_building)
    scenario = result["scenario"]
    print(format_scenario(scenario, result["capacity_rows"]))
    print(format_dense(scenario, aps_per_building, result["dense_reuse_factor"]))
    print_monte_carlo_result(result["monte_carlo"], scenario.name)
    return result


# --- Full text report (shared by the CLI and the HTML report generator) -------------------

def spectrum_text_report(aps: int = 20, skip_montecarlo: bool = False) -> str:
    india, us = default_scenarios()
    parts = []
    for scenario in (india, us):
        parts.append(format_scenario(scenario, scenario_capacity(scenario)))

    parts.append(f"\n--- Dense deployment stress test ({aps} APs in one building) ---")
    for scenario in (india, SpectrumScenario(f"India (hypothetical full band, {FULL_BAND_MHZ} MHz)", FULL_BAND_MHZ)):
        parts.append(f"\n{scenario.name}:")
        parts.append(format_dense(scenario, aps, dense_deployment_reuse(scenario, aps)))

    parts.append(format_sinr_rows(sinr_vs_distance_rows(), 20.0))

    if not skip_montecarlo:
        for scenario in (india, us):
            result = monte_carlo_ap_simulation(scenario.total_mhz, n_aps=aps, n_trials=300)
            parts.append(format_monte_carlo(result, scenario.name))

    parts.append(format_thz(thz_path_loss_rows(), THZ_FREQ_RANGE_GHZ))
    parts.append(
        f"\nTakeaway: India's {INDIA_DELICENSED_MHZ} MHz supports "
        f"{channels_available(INDIA_DELICENSED_MHZ, 320)} x 320 MHz OR "
        f"{channels_available(INDIA_DELICENSED_MHZ, 160)} x 160 MHz clean channels - enough for "
        "gigabit-class home/office Wi-Fi, but dense multi-AP buildings see more co-channel reuse "
        "and lower average SINR than fully delicensed markets such as the US "
        "(compare the Monte Carlo means above)."
    )
    return "\n".join(parts)


def main(argv=None):
    from .logging_config import setup_logging
    setup_logging(cli=True)
    parser = argparse.ArgumentParser(description="6 GHz / 6G spectrum simulation")
    parser.add_argument("--whatif-mhz", type=int, default=None,
                        help="Run a custom scenario with this many MHz delicensed")
    parser.add_argument("--aps", type=int, default=20, help="APs per building for stress tests")
    parser.add_argument("--skip-montecarlo", action="store_true",
                        help="Skip the slower Monte Carlo simulation")
    args, _ = parser.parse_known_args(argv)

    if args.aps < 1:
        parser.error("--aps must be at least 1")
    if args.whatif_mhz is not None:
        if args.whatif_mhz < 20:
            parser.error("--whatif-mhz must be at least 20")
        what_if(args.whatif_mhz, aps_per_building=args.aps)
        return

    print(spectrum_text_report(aps=args.aps, skip_montecarlo=args.skip_montecarlo))


if __name__ == "__main__":
    main()
