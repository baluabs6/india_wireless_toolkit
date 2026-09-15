"""
6 GHz / 6G Spectrum Simulator — India (v2)
---------------------------------------------
Features:
  1. Channel capacity modeling (original) for India vs a fully delicensed market.
  2. Path-loss / SINR model estimating real-world signal degradation with
     distance and walls (log-distance path loss model).
  3. Monte Carlo simulation: randomly places APs in a floorplan and reports
     average SINR / interference under India's constrained channel count.
  4. 6G terahertz (THz) band illustrative modeling (India's "innovation zone" pilots).
  5. "What-if" mode: pass a custom MHz allocation and get instant results.

Caches expensive Monte Carlo runs in Redis when available (india_wireless_toolkit.db).
"""

import argparse
import random
import math
from dataclasses import dataclass

from .db import cached

PHY_RATE_MBPS = {20: 172.1, 40: 344.1, 80: 688.1, 160: 1376.1, 320: 2882.4}
CHANNEL_WIDTHS = [20, 40, 80, 160, 320]


@dataclass
class SpectrumScenario:
    name: str
    total_mhz: int


def channels_available(total_mhz: int, width: int) -> int:
    return total_mhz // width


def simulate_scenario(scenario: SpectrumScenario):
    print(f"\n=== {scenario.name}: {scenario.total_mhz} MHz available ===")
    rows = []
    for width in CHANNEL_WIDTHS:
        n_channels = channels_available(scenario.total_mhz, width)
        if n_channels == 0:
            continue
        total_phy_mbps = n_channels * PHY_RATE_MBPS[width]
        rows.append((width, n_channels, total_phy_mbps))
        print(
            f"  {width:>3} MHz channels: {n_channels:>2} channels available "
            f"| aggregate PHY capacity ≈ {total_phy_mbps:,.0f} Mbps "
            f"({total_phy_mbps/1000:.1f} Gbps)"
        )
    return rows


def dense_deployment_estimate(scenario: SpectrumScenario, aps_per_building: int = 20):
    width = 160
    n_channels = channels_available(scenario.total_mhz, width)
    if n_channels == 0:
        print(f"  No {width} MHz channel fits in {scenario.total_mhz} MHz.")
        return None
    reuse_factor = max(1, -(-aps_per_building // n_channels))
    print(
        f"  With {aps_per_building} APs needing 160 MHz channels: "
        f"{n_channels} clean channels -> each channel reused by ~{reuse_factor} AP(s), "
        f"{'no' if reuse_factor == 1 else 'some'} co-channel interference expected."
    )
    return reuse_factor


# --- Feature 2: Path-loss / SINR model -------------------------------------

def log_distance_path_loss_db(distance_m: float, freq_ghz: float = 6.0,
                                path_loss_exponent: float = 3.0,
                                wall_count: int = 0, wall_loss_db: float = 5.0) -> float:
    """
    Simplified log-distance path loss model:
        PL(d) = PL(d0) + 10*n*log10(d/d0) + wall attenuation
    d0 = 1m reference distance. Reasonable indoor approximation, not a
    substitute for a real RF site survey.
    """
    d0 = 1.0
    fspl_d0 = 20 * math.log10(d0) + 20 * math.log10(freq_ghz * 1000) + 32.44
    pl = fspl_d0 + 10 * path_loss_exponent * math.log10(max(distance_m, d0) / d0)
    pl += wall_count * wall_loss_db
    return pl


def estimate_sinr_db(tx_power_dbm: float, distance_m: float, interference_dbm: float = -85.0,
                      noise_floor_dbm: float = -95.0, freq_ghz: float = 6.0,
                      wall_count: int = 1) -> float:
    path_loss = log_distance_path_loss_db(distance_m, freq_ghz=freq_ghz, wall_count=wall_count)
    rx_power_dbm = tx_power_dbm - path_loss
    interference_plus_noise = 10 * math.log10(
        10 ** (interference_dbm / 10) + 10 ** (noise_floor_dbm / 10)
    )
    return rx_power_dbm - interference_plus_noise


def sinr_vs_distance_report(distances=(2, 5, 10, 15, 20, 30), tx_power_dbm: float = 20.0):
    print(f"\n=== SINR vs distance (6 GHz, tx_power={tx_power_dbm} dBm) ===")
    for d in distances:
        walls = max(0, int(d // 8))
        sinr = estimate_sinr_db(tx_power_dbm, d, wall_count=walls)
        quality = (
            "Excellent" if sinr > 30 else
            "Good" if sinr > 20 else
            "Fair" if sinr > 10 else
            "Poor"
        )
        print(f"  {d:>3}m ({walls} walls): SINR ≈ {sinr:5.1f} dB -> {quality}")


# --- Feature 3: Monte Carlo AP placement simulation -------------------------

@cached("spectrum:montecarlo")
def monte_carlo_ap_simulation(total_mhz: int, floor_width_m: float = 40, floor_height_m: float = 40,
                                n_aps: int = 20, n_trials: int = 500, seed: int = 42):
    random.seed(seed)
    n_channels = max(1, channels_available(total_mhz, 160))

    avg_sinrs = []
    for _ in range(n_trials):
        positions = [(random.uniform(0, floor_width_m), random.uniform(0, floor_height_m))
                     for _ in range(n_aps)]
        channels = [random.randint(0, n_channels - 1) for _ in range(n_aps)]

        trial_sinrs = []
        for i in range(n_aps):
            client_distance = random.uniform(2, 10)
            interference_mw = 0.0
            for j in range(n_aps):
                if j == i or channels[j] != channels[i]:
                    continue
                dx = positions[i][0] - positions[j][0]
                dy = positions[i][1] - positions[j][1]
                dist = max(1.0, math.hypot(dx, dy))
                pl = log_distance_path_loss_db(dist, wall_count=2)
                interfering_power_dbm = 20.0 - pl
                interference_mw += 10 ** (interfering_power_dbm / 10)

            interference_dbm = 10 * math.log10(interference_mw) if interference_mw > 0 else -100.0
            sinr = estimate_sinr_db(20.0, client_distance, interference_dbm=interference_dbm, wall_count=1)
            trial_sinrs.append(sinr)

        avg_sinrs.append(sum(trial_sinrs) / len(trial_sinrs))

    result = {
        "total_mhz": total_mhz,
        "n_channels_160mhz": n_channels,
        "n_aps": n_aps,
        "n_trials": n_trials,
        "mean_sinr_db": sum(avg_sinrs) / len(avg_sinrs),
        "min_sinr_db": min(avg_sinrs),
        "max_sinr_db": max(avg_sinrs),
    }
    return result


def print_monte_carlo_result(result: dict, label: str):
    print(f"\n=== Monte Carlo AP simulation: {label} ===")
    print(f"  {result['n_channels_160mhz']} x 160MHz channels for {result['n_aps']} APs "
          f"across {result['n_trials']} random trials")
    print(f"  Mean SINR: {result['mean_sinr_db']:.1f} dB "
          f"(range {result['min_sinr_db']:.1f} to {result['max_sinr_db']:.1f} dB)")


# --- Feature 4: 6G terahertz band (illustrative) ----------------------------

def thz_band_report(freq_ghz_range=(100, 300), distance_m=(1, 5, 10, 20)):
    print(f"\n=== 6G Terahertz band illustrative model ({freq_ghz_range[0]}-{freq_ghz_range[1]} GHz) ===")
    for freq in freq_ghz_range:
        print(f"\n  At {freq} GHz:")
        for d in distance_m:
            pl = log_distance_path_loss_db(d, freq_ghz=freq, path_loss_exponent=2.5, wall_count=0)
            atmospheric_loss_db = 0.05 * freq * d
            total_pl = pl + atmospheric_loss_db
            print(f"    {d:>2}m: path loss ≈ {total_pl:6.1f} dB "
                  f"(free-space {pl:.1f} dB + atmospheric {atmospheric_loss_db:.1f} dB)")
    print(
        "\n  Takeaway: THz path loss grows so fast with distance and frequency "
        "that 6G THz use cases stay confined to very short range, line-of-sight "
        "'innovation zone' pilots rather than wide-area coverage."
    )


# --- Feature 5: What-if mode -------------------------------------------------

def what_if(custom_mhz: int, aps_per_building: int = 20):
    scenario = SpectrumScenario(f"What-if: {custom_mhz} MHz delicensed", custom_mhz)
    simulate_scenario(scenario)
    dense_deployment_estimate(scenario, aps_per_building=aps_per_building)
    result = monte_carlo_ap_simulation(custom_mhz, n_aps=aps_per_building, n_trials=200)
    print_monte_carlo_result(result, scenario.name)


def main():
    parser = argparse.ArgumentParser(description="6 GHz / 6G spectrum simulation")
    parser.add_argument("--whatif-mhz", type=int, default=None,
                         help="Run a custom scenario with this many MHz delicensed")
    parser.add_argument("--aps", type=int, default=20, help="APs per building for stress tests")
    parser.add_argument("--skip-montecarlo", action="store_true",
                         help="Skip the slower Monte Carlo simulation")
    args, _ = parser.parse_known_args()

    if args.whatif_mhz is not None:
        what_if(args.whatif_mhz, aps_per_building=args.aps)
        return

    india_current = SpectrumScenario("India (current, Jan 2026 gazette)", 500)
    us_market = SpectrumScenario("US (full band delicensed)", 1200)

    for scenario in [india_current, us_market]:
        simulate_scenario(scenario)

    print("\n--- Dense deployment stress test (20 APs in one building) ---")
    for scenario in [india_current, SpectrumScenario("India (hypothetical full band)", 1200)]:
        print(f"\n{scenario.name}:")
        dense_deployment_estimate(scenario, aps_per_building=args.aps)

    sinr_vs_distance_report()

    if not args.skip_montecarlo:
        for scenario in [india_current, us_market]:
            result = monte_carlo_ap_simulation(scenario.total_mhz, n_aps=args.aps, n_trials=300)
            print_monte_carlo_result(result, scenario.name)

    thz_band_report()

    print(
        "\nTakeaway: India's 500 MHz still supports one 320 MHz channel OR "
        "three clean 160 MHz channels — enough for gigabit-class home/office "
        "Wi-Fi, but dense multi-AP buildings will see more co-channel reuse "
        "and lower average SINR than in fully-delicensed markets like the US."
    )


if __name__ == "__main__":
    main()
