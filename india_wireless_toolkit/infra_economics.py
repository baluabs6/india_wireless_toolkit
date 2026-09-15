"""
Last-Mile Infrastructure Economics — India (v2)
--------------------------------------------------
Features:
  1. Duplicate vs shared-neutral last-mile deployment cost model (original).
  2. NPV / IRR analysis over the deployment horizon.
  3. Sensitivity analysis (tornado chart data) — which variable swings cost most.
  4. Three-way comparison: FTTH vs FWA vs Satellite.
  5. Break-even calculator: at what ISP count does shared infra beat duplication?

Reads default parameters from config.yaml (section: infra_economics), with
dummy per-variable ranges from data/infra_cost_variables.csv used in the
sensitivity analysis.
"""

import os
import csv
import argparse
from dataclasses import dataclass, fields

import matplotlib.pyplot as plt

from .config_loader import CONFIG
from .db import cached

_CFG = CONFIG.get("infra_economics", {})


@dataclass
class Params:
    n_buildings: int = _CFG.get("n_buildings", 1000)
    n_isps: int = _CFG.get("n_isps", 5)
    fibre_capex_per_building_inr: float = _CFG.get("fibre_capex_per_building_inr", 25000)
    fwa_capex_per_building_inr: float = _CFG.get("fwa_capex_per_building_inr", 6000)
    satellite_capex_per_building_inr: float = _CFG.get("satellite_capex_per_building_inr", 15000)
    neutral_access_fee_per_building_per_year_inr: float = _CFG.get(
        "neutral_access_fee_per_building_per_year_inr", 2000)
    opex_per_building_per_year_inr: float = _CFG.get("opex_per_building_per_year_inr", 1500)
    years: int = _CFG.get("years", 5)
    discount_rate: float = _CFG.get("discount_rate", 0.10)


def format_inr_crore(value: float) -> str:
    return f"₹{value / 1e7:,.2f} crore"


# --- Feature 1: Duplicate vs shared model -----------------------------------

def duplicate_model(p: Params):
    total_capex = p.n_buildings * p.n_isps * p.fibre_capex_per_building_inr
    total_opex = p.n_buildings * p.n_isps * p.opex_per_building_per_year_inr * p.years
    return total_capex, total_opex


def shared_neutral_model(p: Params):
    neutral_capex = p.n_buildings * p.fibre_capex_per_building_inr
    isp_leasing_cost = (
        p.n_buildings * p.n_isps * p.neutral_access_fee_per_building_per_year_inr * p.years
    )
    total_cost_all_parties = neutral_capex + isp_leasing_cost
    return neutral_capex, isp_leasing_cost, total_cost_all_parties


def fwa_alternative_model(p: Params):
    total_capex = p.n_buildings * p.fwa_capex_per_building_inr
    total_opex = p.n_buildings * p.opex_per_building_per_year_inr * p.years
    return total_capex, total_opex


def satellite_alternative_model(p: Params):
    total_capex = p.n_buildings * p.satellite_capex_per_building_inr
    # Satellite opex tends to run higher per building (terminal maintenance, subscription)
    total_opex = p.n_buildings * (p.opex_per_building_per_year_inr * 1.4) * p.years
    return total_capex, total_opex


# --- Feature 2: NPV / IRR ----------------------------------------------------

def npv(rate: float, cashflows: list) -> float:
    """cashflows[0] is at t=0 (typically negative, the initial capex)."""
    return sum(cf / (1 + rate) ** t for t, cf in enumerate(cashflows))


def irr(cashflows: list, guess: float = 0.1, tol: float = 1e-6, max_iter: int = 1000) -> float:
    """Simple Newton's-method IRR solver. Returns None if it doesn't converge."""
    rate = guess
    for _ in range(max_iter):
        npv_val = npv(rate, cashflows)
        # numerical derivative
        d_rate = 1e-6
        npv_delta = npv(rate + d_rate, cashflows)
        derivative = (npv_delta - npv_val) / d_rate
        if abs(derivative) < 1e-12:
            return None
        new_rate = rate - npv_val / derivative
        if abs(new_rate - rate) < tol:
            return new_rate
        rate = new_rate
    return None


def build_shared_model_cashflows(p: Params, annual_revenue_per_building_inr: float = 3000):
    """
    Illustrative cashflow stream for the neutral operator: capex upfront (t=0,
    negative), then annual net cashflow = leasing revenue - opex, for `years`.
    """
    capex = p.n_buildings * p.fibre_capex_per_building_inr
    cashflows = [-capex]
    for _ in range(p.years):
        annual_revenue = p.n_buildings * p.n_isps * p.neutral_access_fee_per_building_per_year_inr
        annual_opex = p.n_buildings * p.opex_per_building_per_year_inr
        cashflows.append(annual_revenue - annual_opex)
    return cashflows


def npv_irr_report(p: Params):
    cashflows = build_shared_model_cashflows(p)
    project_npv = npv(p.discount_rate, cashflows)
    project_irr = irr(cashflows)

    print(f"\n=== NPV / IRR: Neutral Operator Investment ===")
    print(f"  Discount rate: {p.discount_rate*100:.1f}%")
    print(f"  Cashflows (₹ crore): {[round(cf/1e7, 2) for cf in cashflows]}")
    print(f"  NPV: {format_inr_crore(project_npv)}")
    if project_irr is not None:
        print(f"  IRR: {project_irr*100:.1f}%")
    else:
        print("  IRR: did not converge")
    verdict = "financially attractive" if project_npv > 0 else "not attractive at this discount rate"
    print(f"  Verdict: the neutral-operator investment is {verdict} "
          f"(NPV {'>' if project_npv > 0 else '<='} 0).")
    return project_npv, project_irr


# --- Feature 3: Sensitivity analysis / tornado chart ------------------------

def load_variable_ranges(csv_path: str = None):
    if csv_path is None:
        csv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "data", "infra_cost_variables.csv")
    ranges = {}
    with open(csv_path, "r") as f:
        for row in csv.DictReader(f):
            ranges[row["parameter"]] = {
                "base": float(row["base_value"]),
                "low": float(row["low_estimate"]),
                "high": float(row["high_estimate"]),
            }
    return ranges


def total_system_cost(p: Params) -> float:
    dup_capex, dup_opex = duplicate_model(p)
    return dup_capex + dup_opex


def sensitivity_analysis(p: Params, ranges: dict = None):
    if ranges is None:
        ranges = load_variable_ranges()

    base_cost = total_system_cost(p)
    results = []
    for param_name, bounds in ranges.items():
        if not hasattr(p, param_name):
            continue
        original_value = getattr(p, param_name)

        setattr(p, param_name, bounds["low"])
        low_cost = total_system_cost(p)

        setattr(p, param_name, bounds["high"])
        high_cost = total_system_cost(p)

        setattr(p, param_name, original_value)  # restore

        swing = abs(high_cost - low_cost)
        results.append({
            "parameter": param_name,
            "low_cost": low_cost,
            "high_cost": high_cost,
            "swing": swing,
        })

    results.sort(key=lambda r: r["swing"], reverse=True)

    print(f"\n=== Sensitivity Analysis (base total cost: {format_inr_crore(base_cost)}) ===")
    for r in results:
        print(f"  {r['parameter']:<45} swing: {format_inr_crore(r['swing'])} "
              f"(low={format_inr_crore(r['low_cost'])}, high={format_inr_crore(r['high_cost'])})")
    return results, base_cost


def plot_tornado_chart(results: list, base_cost: float, out_path: str = "charts/tornado_sensitivity.png"):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    labels = [r["parameter"] for r in results]
    lows = [(r["low_cost"] - base_cost) / 1e7 for r in results]
    highs = [(r["high_cost"] - base_cost) / 1e7 for r in results]

    fig, ax = plt.subplots(figsize=(8, 5))
    y_pos = range(len(labels))
    ax.barh(y_pos, highs, color="#dc2626", label="High estimate")
    ax.barh(y_pos, lows, color="#2563eb", label="Low estimate")
    ax.axvline(0, color="black", linewidth=0.8)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("Change in total system cost (₹ crore)")
    ax.set_title("Sensitivity / Tornado Chart: Last-Mile Deployment Cost Drivers")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"  Tornado chart saved to {out_path}")


# --- Feature 4: Three-way comparison ----------------------------------------

def three_way_comparison(p: Params):
    ftth_capex, ftth_opex = duplicate_model(p)  # per-market duplicate cost, for comparison baseline
    ftth_capex_single, ftth_opex_single = (
        p.n_buildings * p.fibre_capex_per_building_inr,
        p.n_buildings * p.opex_per_building_per_year_inr * p.years,
    )
    fwa_capex, fwa_opex = fwa_alternative_model(p)
    sat_capex, sat_opex = satellite_alternative_model(p)

    print(f"\n=== Three-way comparison (per ISP, {p.n_buildings} buildings, {p.years} yrs) ===")
    print(f"  FTTH:      capex {format_inr_crore(ftth_capex_single)} + opex {format_inr_crore(ftth_opex_single)} "
          f"= {format_inr_crore(ftth_capex_single + ftth_opex_single)}")
    print(f"  FWA:       capex {format_inr_crore(fwa_capex)} + opex {format_inr_crore(fwa_opex)} "
          f"= {format_inr_crore(fwa_capex + fwa_opex)}")
    print(f"  Satellite: capex {format_inr_crore(sat_capex)} + opex {format_inr_crore(sat_opex)} "
          f"= {format_inr_crore(sat_capex + sat_opex)}")

    cheapest = min(
        [("FTTH", ftth_capex_single + ftth_opex_single),
         ("FWA", fwa_capex + fwa_opex),
         ("Satellite", sat_capex + sat_opex)],
        key=lambda x: x[1],
    )
    print(f"  Cheapest option at this scale: {cheapest[0]} ({format_inr_crore(cheapest[1])})")
    return {
        "ftth": (ftth_capex_single, ftth_opex_single),
        "fwa": (fwa_capex, fwa_opex),
        "satellite": (sat_capex, sat_opex),
    }


# --- Feature 5: Break-even calculator ---------------------------------------

def find_breakeven_n_isps(p: Params, max_isps: int = 50):
    """
    Finds the smallest n_isps at which shared-neutral total cost becomes
    cheaper than duplicate-deployment total cost (holding other params fixed).
    """
    original_n = p.n_isps
    breakeven = None
    for n in range(1, max_isps + 1):
        p.n_isps = n
        dup_capex, dup_opex = duplicate_model(p)
        _, _, shared_total = shared_neutral_model(p)
        if shared_total < (dup_capex + dup_opex):
            breakeven = n
            break
    p.n_isps = original_n
    return breakeven


def breakeven_report(p: Params):
    breakeven = find_breakeven_n_isps(p)
    print(f"\n=== Break-even analysis ===")
    if breakeven:
        print(f"  Shared-neutral infrastructure becomes cheaper than duplicate "
              f"deployment once there are {breakeven}+ competing ISPs in the market.")
    else:
        print("  No break-even found within the tested range — shared infra "
              "is cheaper (or more expensive) across the whole range checked.")
    return breakeven


# --- Original summary report --------------------------------------------------

@cached("infra:summary")
def summary_report(p: Params = None):
    p = p or Params()
    dup_capex, dup_opex = duplicate_model(p)
    neutral_capex, leasing_cost, shared_total = shared_neutral_model(p)
    savings = (dup_capex + dup_opex) - shared_total
    pct_savings = savings / (dup_capex + dup_opex) * 100
    return {
        "duplicate_total": dup_capex + dup_opex,
        "shared_total": shared_total,
        "savings": savings,
        "pct_savings": pct_savings,
    }


def main():
    parser = argparse.ArgumentParser(description="Last-mile infra economics")
    parser.add_argument("--sensitivity", action="store_true", help="Run sensitivity/tornado analysis")
    parser.add_argument("--npv", action="store_true", help="Run NPV/IRR analysis")
    parser.add_argument("--compare", action="store_true", help="Run FTTH/FWA/Satellite comparison")
    parser.add_argument("--breakeven", action="store_true", help="Run break-even ISP count analysis")
    parser.add_argument("--all-features", action="store_true", help="Run every analysis")
    args, _ = parser.parse_known_args()

    p = Params()
    print(f"Scenario: {p.n_buildings} buildings, {p.n_isps} competing ISPs, {p.years}-year horizon\n")

    dup_capex, dup_opex = duplicate_model(p)
    print("1) Duplicate FTTH deployment (status quo):")
    print(f"   Total capex (all ISPs combined): {format_inr_crore(dup_capex)}")
    print(f"   Total opex over {p.years} yrs:        {format_inr_crore(dup_opex)}")
    print(f"   Total cost:                     {format_inr_crore(dup_capex + dup_opex)}")

    neutral_capex, leasing_cost, shared_total = shared_neutral_model(p)
    print("\n2) Shared neutral last-mile infrastructure:")
    print(f"   Neutral operator capex (laid once): {format_inr_crore(neutral_capex)}")
    print(f"   ISP leasing fees over {p.years} yrs:      {format_inr_crore(leasing_cost)}")
    print(f"   Total cost (all parties combined):  {format_inr_crore(shared_total)}")

    savings = (dup_capex + dup_opex) - shared_total
    pct_savings = savings / (dup_capex + dup_opex) * 100
    print(f"\n   >> System-wide savings vs duplicate model: {format_inr_crore(savings)} ({pct_savings:.1f}%)")

    run_all = args.all_features
    if args.npv or run_all:
        npv_irr_report(p)
    if args.sensitivity or run_all:
        results, base_cost = sensitivity_analysis(p)
        plot_tornado_chart(results, base_cost)
    if args.compare or run_all:
        three_way_comparison(p)
    if args.breakeven or run_all:
        breakeven_report(p)

    if not any([args.npv, args.sensitivity, args.compare, args.breakeven, run_all]):
        print("\n(tip: pass --npv, --sensitivity, --compare, --breakeven, or --all-features "
              "for deeper analysis)")

    print(
        "\nNote: figures are illustrative planning assumptions from config.yaml / "
        "data/infra_cost_variables.csv, not official TRAI/ISP cost data."
    )


if __name__ == "__main__":
    main()
