"""
Last-mile Infrastructure Economics (v2.1)
-------------------------------------------
Features:
  1. Duplicate (every ISP lays its own fibre) vs shared-neutral deployment.
  2. NPV / IRR of the neutral operator's investment.
  3. Sensitivity analysis (tornado chart) over several decision metrics.
  4. Three-way comparison: FTTH vs fixed wireless (FWA) vs satellite.
  5. Break-even ISP count.

Accounting conventions (these changed in v2.1 - see CHANGELOG.md)
-----------------------------------------------------------------
* RESOURCE cost of the shared model = the neutral operator's capex + opex for
  ONE network. The lease fees ISPs pay are a TRANSFER to the neutral
  operator (its revenue), not an additional cost, so they are reported
  separately and never added on top of the capex they repay. (v2.0 added
  capex + lease fees and ignored the neutral operator's opex.)
* The NPV treats the neutral operator as the investor: capex at t=0, then
  lease revenue minus opex each year, plus (optionally) the residual
  value of the fibre at the end of the horizon, because fibre outlives a
  5-year window. Adoption can ramp up over time (Params.adoption_ramp).
* Break-even requires ALL of: the shared network is cheaper in resource
  terms, the neutral operator's NPV is >= 0, and an ISP pays less in lease
  fees than it would spend building its own network.
* Cash totals ignore asset life, which penalises long-lived assets inside a
  short horizon. The three-way comparison therefore also reports an
  asset-life-adjusted "horizon cost" (capex consumed during the horizon + opex).
* Cost only: service quality (speed, latency, capacity per user) is not modeled.

Every analysis has a pure function returning data and a `*_report` function
that prints it, so APIs and the agent never need to capture stdout.
"""

import argparse
import csv
import dataclasses
import math
from dataclasses import dataclass, replace

from .config_loader import CONFIG, resolve_path
from .db import cached
from . import _plotting

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
    # Economic life of each technology's capex, for residual value / horizon cost.
    fibre_asset_life_years: int = _CFG.get("fibre_asset_life_years", 20)
    fwa_asset_life_years: int = _CFG.get("fwa_asset_life_years", 7)
    satellite_asset_life_years: int = _CFG.get("satellite_asset_life_years", 5)
    include_terminal_value: bool = _CFG.get("include_terminal_value", True)
    # Fraction of the lease revenue earned in year 1, 2, ...; empty = full from year 1.
    adoption_ramp: tuple = tuple(_CFG.get("adoption_ramp", []) or [])

    def __post_init__(self):
        if self.n_buildings < 1 or self.n_isps < 1 or self.years < 1:
            raise ValueError("n_buildings, n_isps and years must be at least 1.")
        if not (0 <= self.discount_rate < 5) or not math.isfinite(self.discount_rate):
            raise ValueError("discount_rate must be a finite fraction in [0, 5).")
        for name in ("fibre_capex_per_building_inr", "fwa_capex_per_building_inr",
                     "satellite_capex_per_building_inr",
                     "neutral_access_fee_per_building_per_year_inr",
                     "opex_per_building_per_year_inr"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a non-negative number.")
        for name in ("fibre_asset_life_years", "fwa_asset_life_years", "satellite_asset_life_years"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1.")
        self.adoption_ramp = tuple(float(x) for x in self.adoption_ramp)
        if any(not (0 <= x <= 1) for x in self.adoption_ramp):
            raise ValueError("adoption_ramp values must be fractions between 0 and 1.")


# Single source of truth for user-adjustable parameters: (type, min, max).
# Used by the CLI, the HTTP API and the agent, so all three clamp identically.
PARAM_SPECS = {
    "n_buildings": (int, 1, 1_000_000),
    "n_isps": (int, 1, 100),
    "years": (int, 1, 30),
    "discount_rate": (float, 0.0, 0.5),
    "fibre_capex_per_building_inr": (float, 0.0, 1e7),
    "fwa_capex_per_building_inr": (float, 0.0, 1e7),
    "satellite_capex_per_building_inr": (float, 0.0, 1e7),
    "neutral_access_fee_per_building_per_year_inr": (float, 0.0, 1e6),
    "opex_per_building_per_year_inr": (float, 0.0, 1e6),
}


def params_from_mapping(mapping, base: Params = None) -> Params:
    """Build Params from untrusted input (query string, model tool arguments).
    Unknown keys and None values are ignored; numbers are clamped to
    PARAM_SPECS; non-numeric values raise ValueError."""
    overrides = {}
    for name, (kind, lo, hi) in PARAM_SPECS.items():
        raw = (mapping or {}).get(name)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"'{name}' must be a number.")
        if not math.isfinite(value):
            raise ValueError(f"'{name}' must be a finite number.")
        value = max(lo, min(hi, value))
        overrides[name] = int(round(value)) if kind is int else value
    return replace(base or Params(), **overrides)


def format_inr_crore(value: float) -> str:
    return f"\u20b9{value / 1e7:,.2f} crore"


# --- Feature 1: Duplicate vs shared model -----------------------------------------

def duplicate_model(p: Params):
    total_capex = p.n_buildings * p.n_isps * p.fibre_capex_per_building_inr
    total_opex = p.n_buildings * p.n_isps * p.opex_per_building_per_year_inr * p.years
    return total_capex, total_opex


def shared_neutral_model(p: Params):
    """Returns (neutral_capex, isp_leasing_cost, total_resource_cost).

    total_resource_cost = neutral capex + neutral opex (one network).
    isp_leasing_cost is the transfer ISPs pay the neutral operator; it is
    returned for information and is NOT part of total_resource_cost."""
    neutral_capex = p.n_buildings * p.fibre_capex_per_building_inr
    neutral_opex = p.n_buildings * p.opex_per_building_per_year_inr * p.years
    isp_leasing_cost = (
        p.n_buildings * p.n_isps * p.neutral_access_fee_per_building_per_year_inr * p.years
    )
    return neutral_capex, isp_leasing_cost, neutral_capex + neutral_opex


def shared_breakdown(p: Params) -> dict:
    neutral_capex, leasing, resource_total = shared_neutral_model(p)
    neutral_opex = resource_total - neutral_capex
    own_network_cost_per_isp = (
        p.n_buildings * (p.fibre_capex_per_building_inr
                         + p.opex_per_building_per_year_inr * p.years)
    )
    return {
        "neutral_capex": neutral_capex,
        "neutral_opex": neutral_opex,
        "resource_total": resource_total,
        "isp_leasing_cost_total": leasing,
        "leasing_cost_per_isp": leasing / p.n_isps,
        "own_network_cost_per_isp": own_network_cost_per_isp,
        "neutral_operator_margin": leasing - resource_total,   # undiscounted
    }


def fwa_alternative_model(p: Params):
    total_capex = p.n_buildings * p.fwa_capex_per_building_inr
    total_opex = p.n_buildings * p.opex_per_building_per_year_inr * p.years
    return total_capex, total_opex


def satellite_alternative_model(p: Params):
    total_capex = p.n_buildings * p.satellite_capex_per_building_inr
    # Satellite opex tends to run higher per building (terminal maintenance, subscription)
    total_opex = p.n_buildings * (p.opex_per_building_per_year_inr * 1.4) * p.years
    return total_capex, total_opex


@cached("infra:summary")
def summary_report(p: Params = None):
    p = p or Params()
    dup_capex, dup_opex = duplicate_model(p)
    dup_total = dup_capex + dup_opex
    breakdown = shared_breakdown(p)
    shared_total = breakdown["resource_total"]
    savings = dup_total - shared_total
    return {
        "duplicate_total": dup_total,
        "shared_total": shared_total,
        "savings": savings,
        "pct_savings": savings / dup_total * 100 if dup_total else 0.0,
        "neutral_capex": breakdown["neutral_capex"],
        "neutral_opex": breakdown["neutral_opex"],
        "isp_leasing_cost_total": breakdown["isp_leasing_cost_total"],
        "per_isp_duplicate_cost": breakdown["own_network_cost_per_isp"],
        "per_isp_shared_cost": breakdown["leasing_cost_per_isp"],
        "n_isps": p.n_isps,
    }


def format_summary(p: Params) -> str:
    dup_capex, dup_opex = duplicate_model(p)
    b = shared_breakdown(p)
    savings = (dup_capex + dup_opex) - b["resource_total"]
    pct = savings / (dup_capex + dup_opex) * 100
    return "\n".join([
        f"Scenario: {p.n_buildings} buildings, {p.n_isps} competing ISPs, {p.years}-year horizon\n",
        "1) Duplicate FTTH deployment (status quo):",
        f"   Total capex (all ISPs combined): {format_inr_crore(dup_capex)}",
        f"   Total opex over {p.years} yrs:        {format_inr_crore(dup_opex)}",
        f"   Total cost:                     {format_inr_crore(dup_capex + dup_opex)}",
        "\n2) Shared neutral last-mile infrastructure (one network):",
        f"   Neutral operator capex (laid once): {format_inr_crore(b['neutral_capex'])}",
        f"   Neutral operator opex over {p.years} yrs: {format_inr_crore(b['neutral_opex'])}",
        f"   Total resource cost:                {format_inr_crore(b['resource_total'])}",
        f"   (ISP lease fees {format_inr_crore(b['isp_leasing_cost_total'])} are a transfer to the "
        f"neutral operator, not an extra cost)",
        f"\n   >> System-wide savings vs duplicate model: {format_inr_crore(savings)} ({pct:.1f}%)",
    ])


# --- Feature 2: NPV / IRR ------------------------------------------------------------

def npv(rate: float, cashflows: list) -> float:
    """cashflows[0] is at t=0 (typically negative, the initial capex)."""
    return sum(cf / (1 + rate) ** t for t, cf in enumerate(cashflows))


def irr(cashflows: list, guess: float = 0.1, tol: float = 1e-6, max_iter: int = 1000):
    """IRR via Newton's method with a bisection fallback. Returns None when no
    IRR exists (no sign change) or neither method converges."""
    if not (any(cf < 0 for cf in cashflows) and any(cf > 0 for cf in cashflows)):
        return None

    rate = guess
    for _ in range(max_iter):
        npv_val = npv(rate, cashflows)
        d_rate = 1e-6
        derivative = (npv(rate + d_rate, cashflows) - npv_val) / d_rate
        if abs(derivative) < 1e-12:
            break
        new_rate = rate - npv_val / derivative
        if not math.isfinite(new_rate) or new_rate <= -0.999:
            break
        if abs(new_rate - rate) < tol:
            return new_rate
        rate = new_rate

    lo, hi = -0.99, 10.0
    f_lo, f_hi = npv(lo, cashflows), npv(hi, cashflows)
    if f_lo * f_hi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        f_mid = npv(mid, cashflows)
        if abs(f_mid) < 1e-9 or (hi - lo) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def _ramp_fraction(p: Params, year: int) -> float:
    if not p.adoption_ramp:
        return 1.0
    return p.adoption_ramp[min(year, len(p.adoption_ramp)) - 1]


def build_shared_model_cashflows(p: Params, annual_revenue_per_building_inr: float = None):
    """
    Cashflows for the neutral operator: capex at t=0 (negative), then each year
    lease revenue - opex, with the residual value of the fibre added to the last
    year when `include_terminal_value` is set.

    annual_revenue_per_building_inr: optional override of the total yearly
    revenue per building; by default it is n_isps x neutral_access_fee.
    """
    capex = p.n_buildings * p.fibre_capex_per_building_inr
    revenue_per_building = (
        annual_revenue_per_building_inr if annual_revenue_per_building_inr is not None
        else p.n_isps * p.neutral_access_fee_per_building_per_year_inr
    )
    annual_opex = p.n_buildings * p.opex_per_building_per_year_inr
    cashflows = [-capex]
    for year in range(1, p.years + 1):
        revenue = p.n_buildings * revenue_per_building * _ramp_fraction(p, year)
        cashflows.append(revenue - annual_opex)
    if p.include_terminal_value:
        residual = capex * max(0.0, 1 - p.years / p.fibre_asset_life_years)
        cashflows[-1] += residual
    return cashflows


def npv_irr_analysis(p: Params, annual_revenue_per_building_inr: float = None) -> dict:
    cashflows = build_shared_model_cashflows(p, annual_revenue_per_building_inr)
    project_npv = npv(p.discount_rate, cashflows)
    return {
        "discount_rate": p.discount_rate,
        "cashflows": cashflows,
        "npv_inr": project_npv,
        "irr": irr(cashflows),
        "includes_terminal_value": p.include_terminal_value,
        "verdict": "financially attractive" if project_npv > 0 else "not attractive at this discount rate",
    }


def format_npv(a: dict) -> str:
    lines = [
        "\n=== NPV / IRR: Neutral Operator Investment ===",
        f"  Discount rate: {a['discount_rate'] * 100:.1f}%",
        f"  Cashflows (\u20b9 crore): {[round(cf / 1e7, 2) for cf in a['cashflows']]}",
        f"  Includes residual (terminal) value of fibre: {a['includes_terminal_value']}",
        f"  NPV: {format_inr_crore(a['npv_inr'])}",
        f"  IRR: {a['irr'] * 100:.1f}%" if a["irr"] is not None else "  IRR: not defined / did not converge",
        f"  Verdict: the neutral-operator investment is {a['verdict']} "
        f"(NPV {'>' if a['npv_inr'] > 0 else '<='} 0).",
    ]
    return "\n".join(lines)


def npv_irr_report(p: Params):
    analysis = npv_irr_analysis(p)
    print(format_npv(analysis))
    return analysis["npv_inr"], analysis["irr"]


# --- Feature 3: Sensitivity analysis / tornado chart -----------------------------------

def load_variable_ranges(csv_path: str = None):
    if csv_path is None:
        csv_path = resolve_path(CONFIG.get("paths", {}).get("data_dir", "data"),
                                "infra_cost_variables.csv")
    ranges = {}
    with open(csv_path, "r", newline="") as f:
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


# metric key -> (label, function(Params) -> INR)
SENSITIVITY_METRICS = {
    "savings": ("Savings: shared network vs duplicate fibre", lambda p: total_system_cost(p) - shared_neutral_model(p)[2]),
    "duplicate_total_cost": ("Total cost: duplicate fibre", total_system_cost),
    "shared_total_cost": ("Total resource cost: shared network", lambda p: shared_neutral_model(p)[2]),
    "neutral_npv": ("Neutral operator NPV", lambda p: npv(p.discount_rate, build_shared_model_cashflows(p))),
    "fwa_total_cost": ("Total cost: fixed wireless (per ISP)", lambda p: sum(fwa_alternative_model(p))),
    "satellite_total_cost": ("Total cost: satellite (per ISP)", lambda p: sum(satellite_alternative_model(p))),
}


def sensitivity_analysis(p: Params, ranges: dict = None, metric: str = "savings"):
    """Vary each variable between its low and high estimate (others fixed) and
    measure the change in `metric`. Every row also carries `swings_by_metric`
    so a variable that does not move THIS metric (e.g. FWA capex vs the
    shared-vs-duplicate savings) is still shown to drive another one.

    Returns (results sorted by swing, base_value_of_metric)."""
    if metric not in SENSITIVITY_METRICS:
        raise ValueError(f"Unknown metric '{metric}'. Valid: {sorted(SENSITIVITY_METRICS)}")
    if ranges is None:
        ranges = load_variable_ranges()
    field_names = {f.name for f in dataclasses.fields(Params)}
    metric_fn = SENSITIVITY_METRICS[metric][1]

    base_value = metric_fn(p)
    results = []
    for name, bounds in ranges.items():
        if name not in field_names:
            continue
        kind = PARAM_SPECS.get(name, (float,))[0]
        low_p = replace(p, **{name: kind(round(bounds["low"])) if kind is int else float(bounds["low"])})
        high_p = replace(p, **{name: kind(round(bounds["high"])) if kind is int else float(bounds["high"])})

        swings = {key: abs(fn(high_p) - fn(low_p)) for key, (_, fn) in SENSITIVITY_METRICS.items()}
        results.append({
            "parameter": name,
            "low_value": getattr(low_p, name),
            "high_value": getattr(high_p, name),
            "low_cost": metric_fn(low_p),
            "high_cost": metric_fn(high_p),
            "swing": abs(metric_fn(high_p) - metric_fn(low_p)),
            "swings_by_metric": swings,
            "influences": sorted(k for k, v in swings.items() if v > 0),
        })
    results.sort(key=lambda r: (-r["swing"], r["parameter"]))
    return results, base_value


def format_sensitivity(results, base_value, metric="savings") -> str:
    label = SENSITIVITY_METRICS[metric][0]
    lines = [f"\n=== Sensitivity Analysis - {label} (base: {format_inr_crore(base_value)}) ==="]
    for r in results:
        if r["swing"] > 0:
            lines.append(f"  {r['parameter']:<45} swing: {format_inr_crore(r['swing'])} "
                         f"(low={format_inr_crore(r['low_cost'])}, high={format_inr_crore(r['high_cost'])})")
        else:
            lines.append(f"  {r['parameter']:<45} no effect on this metric "
                         f"(drives: {', '.join(r['influences']) or 'nothing'})")
    return "\n".join(lines)


def plot_tornado_chart(results: list, base_cost: float, out_path: str = None, metric: str = "savings"):
    out_path = out_path or resolve_path(CONFIG.get("paths", {}).get("charts_dir", "charts"),
                                        "tornado_sensitivity.png")
    label = SENSITIVITY_METRICS[metric][0]
    active = [r for r in results if r["swing"] > 0]
    inactive = [r["parameter"] for r in results if r["swing"] <= 0]
    active = list(reversed(active))      # biggest driver on top

    fig = _plotting.new_figure((9, 5.5))
    ax = fig.subplots()
    if active:
        y_pos = range(len(active))
        lows = [(r["low_cost"] - base_cost) / 1e7 for r in active]
        highs = [(r["high_cost"] - base_cost) / 1e7 for r in active]
        ax.barh(y_pos, highs, color="#dc2626", label="High estimate")
        ax.barh(y_pos, lows, color="#2563eb", label="Low estimate")
        ax.axvline(0, color="black", linewidth=0.8)
        ax.set_yticks(list(y_pos))
        ax.set_yticklabels([r["parameter"] for r in active], fontsize=9)
        ax.legend()
    else:
        ax.text(0.5, 0.5, "No variable moves this metric", ha="center", va="center")
    ax.set_xlabel("Change in metric (\u20b9 crore)")
    ax.set_title(f"Tornado chart - {label}", fontsize=11)
    if inactive:
        fig.text(0.01, 0.01, "No effect on this metric: " + ", ".join(inactive), fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    _plotting.save_figure(fig, out_path)
    return out_path


def sensitivity_report(p: Params, metric: str = "savings", chart: bool = True):
    results, base = sensitivity_analysis(p, metric=metric)
    print(format_sensitivity(results, base, metric))
    if chart:
        path = plot_tornado_chart(results, base, metric=metric)
        print(f"  Tornado chart saved to {path}")
    return results, base


# --- Feature 4: Three-way comparison ------------------------------------------------------

def three_way_comparison_data(p: Params) -> dict:
    """Per-ISP cost of serving the buildings with each technology.

    Legacy keys 'ftth'/'fwa'/'satellite' keep the (capex, opex) pairs; 'detail'
    adds the asset-life-adjusted horizon cost (capex consumed during the
    horizon + opex), which is the fairer comparison across technologies with
    different lifetimes."""
    techs = {
        "ftth": (p.n_buildings * p.fibre_capex_per_building_inr,
                 p.n_buildings * p.opex_per_building_per_year_inr * p.years,
                 p.fibre_asset_life_years),
        "fwa": (*fwa_alternative_model(p), p.fwa_asset_life_years),
        "satellite": (*satellite_alternative_model(p), p.satellite_asset_life_years),
    }
    detail = {}
    for name, (capex, opex, life) in techs.items():
        consumed = capex * min(1.0, p.years / life)
        detail[name] = {
            "capex": capex, "opex": opex, "cash_total": capex + opex,
            "asset_life_years": life,
            "capex_consumed_in_horizon": consumed,
            "horizon_cost": consumed + opex,
        }
    return {
        "ftth": (techs["ftth"][0], techs["ftth"][1]),
        "fwa": (techs["fwa"][0], techs["fwa"][1]),
        "satellite": (techs["satellite"][0], techs["satellite"][1]),
        "detail": detail,
        "cheapest_cash": min(detail, key=lambda k: detail[k]["cash_total"]),
        "cheapest_horizon_cost": min(detail, key=lambda k: detail[k]["horizon_cost"]),
    }


def format_three_way(data: dict, p: Params) -> str:
    names = {"ftth": "FTTH", "fwa": "FWA", "satellite": "Satellite"}
    lines = [f"\n=== Three-way comparison (per ISP, {p.n_buildings} buildings, {p.years} yrs) ==="]
    for key, label in names.items():
        d = data["detail"][key]
        lines.append(
            f"  {label + ':':<10} capex {format_inr_crore(d['capex'])} + opex {format_inr_crore(d['opex'])} "
            f"= {format_inr_crore(d['cash_total'])}  |  asset-life-adjusted "
            f"{format_inr_crore(d['horizon_cost'])} ({d['asset_life_years']}-yr life)"
        )
    lines.append(f"  Cheapest on cash outlay:            {names[data['cheapest_cash']]}")
    lines.append(f"  Cheapest on asset-life-adjusted cost: {names[data['cheapest_horizon_cost']]}")
    lines.append("  (Cost only - speed, latency and capacity per user are not modeled.)")
    return "\n".join(lines)


def three_way_comparison(p: Params):
    data = three_way_comparison_data(p)
    print(format_three_way(data, p))
    return data


# --- Feature 5: Break-even calculator -------------------------------------------------------

def _first_n_isps(p: Params, predicate, max_isps: int):
    for n in range(1, max_isps + 1):
        if predicate(replace(p, n_isps=n)):
            return n
    return None


def breakeven_analysis(p: Params, max_isps: int = 50) -> dict:
    """Smallest ISP count at which shared infrastructure makes sense, with the
    three component conditions reported separately."""
    isps_prefer = (p.neutral_access_fee_per_building_per_year_inr * p.years
                   < p.fibre_capex_per_building_inr + p.opex_per_building_per_year_inr * p.years)
    cheaper = lambda q: shared_neutral_model(q)[2] < total_system_cost(q)
    viable = lambda q: npv(q.discount_rate, build_shared_model_cashflows(q)) >= 0
    return {
        "breakeven_n_isps": _first_n_isps(
            p, lambda q: cheaper(q) and viable(q) and isps_prefer, max_isps),
        "resource_cost_breakeven": _first_n_isps(p, cheaper, max_isps),
        "neutral_operator_viable_from": _first_n_isps(p, viable, max_isps),
        "isps_prefer_shared_over_own_network": isps_prefer,
        "max_isps_tested": max_isps,
    }


def find_breakeven_n_isps(p: Params, max_isps: int = 50):
    return breakeven_analysis(p, max_isps)["breakeven_n_isps"]


def format_breakeven(a: dict) -> str:
    lines = ["\n=== Break-even analysis ==="]
    if a["breakeven_n_isps"]:
        lines.append(f"  Shared-neutral infrastructure works once there are {a['breakeven_n_isps']}+ "
                     "competing ISPs (cheaper in resource terms AND viable for the neutral operator "
                     "AND cheaper for each ISP than building its own network).")
    else:
        lines.append(f"  No break-even found within {a['max_isps_tested']} ISPs.")
    lines.append(f"  - cheaper than duplication (resource cost) from: {a['resource_cost_breakeven']} ISPs")
    lines.append(f"  - neutral operator NPV >= 0 from:               {a['neutral_operator_viable_from']} ISPs")
    lines.append(f"  - ISPs prefer leasing to building their own:    {a['isps_prefer_shared_over_own_network']}")
    return "\n".join(lines)


def breakeven_report(p: Params):
    analysis = breakeven_analysis(p)
    print(format_breakeven(analysis))
    return analysis["breakeven_n_isps"]


# --- CLI -----------------------------------------------------------------------------------------

def main(argv=None):
    from .logging_config import setup_logging
    setup_logging(cli=True)
    parser = argparse.ArgumentParser(description="Last-mile infra economics")
    parser.add_argument("--sensitivity", action="store_true", help="Run sensitivity/tornado analysis")
    parser.add_argument("--metric", default="savings", choices=sorted(SENSITIVITY_METRICS),
                        help="Metric for the sensitivity analysis (default: savings)")
    parser.add_argument("--npv", action="store_true", help="Run NPV/IRR analysis")
    parser.add_argument("--compare", action="store_true", help="Run FTTH/FWA/Satellite comparison")
    parser.add_argument("--breakeven", action="store_true", help="Run break-even ISP count analysis")
    parser.add_argument("--all-features", action="store_true", help="Run every analysis")
    parser.add_argument("--isps", type=int, help="Override number of competing ISPs")
    parser.add_argument("--buildings", type=int, help="Override number of buildings")
    parser.add_argument("--years", type=int, help="Override horizon in years")
    parser.add_argument("--discount-rate", type=float, help="Override discount rate (0.10 = 10%%)")
    args, _ = parser.parse_known_args(argv)

    try:
        p = params_from_mapping({
            "n_isps": args.isps, "n_buildings": args.buildings,
            "years": args.years, "discount_rate": args.discount_rate,
        })
    except ValueError as e:
        parser.error(str(e))

    print(format_summary(p))

    run_all = args.all_features
    if args.npv or run_all:
        npv_irr_report(p)
    if args.sensitivity or run_all:
        sensitivity_report(p, metric=args.metric)
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
