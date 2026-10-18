import pytest

from india_wireless_toolkit import infra_economics as ie
from india_wireless_toolkit.infra_economics import (
    Params, duplicate_model, shared_neutral_model, npv, irr, find_breakeven_n_isps,
)


def test_duplicate_more_expensive_than_shared_at_default_params():
    p = Params()
    dup_capex, dup_opex = duplicate_model(p)
    _, _, shared_total = shared_neutral_model(p)
    assert (dup_capex + dup_opex) > shared_total


def test_shared_resource_cost_does_not_double_count_lease_fees():
    p = Params()
    capex, leasing, total = shared_neutral_model(p)
    opex = p.n_buildings * p.opex_per_building_per_year_inr * p.years
    assert total == capex + opex                    # one network: capex + its opex
    assert leasing == p.n_buildings * p.n_isps * p.neutral_access_fee_per_building_per_year_inr * p.years
    assert total < capex + leasing + opex           # lease fees are a transfer, not extra cost


def test_resource_cost_independent_of_lease_fee():
    low = ie.replace(Params(), neutral_access_fee_per_building_per_year_inr=0.0)
    high = ie.replace(Params(), neutral_access_fee_per_building_per_year_inr=9999.0)
    assert shared_neutral_model(low)[2] == shared_neutral_model(high)[2]


def test_npv_zero_rate_equals_sum_of_cashflows():
    cashflows = [-100, 50, 50, 50]
    assert abs(npv(0.0, cashflows) - sum(cashflows)) < 1e-9


def test_irr_recovers_reasonable_rate():
    rate = irr([-100, 60, 60])
    assert rate == pytest.approx(0.1307, abs=1e-3)


def test_irr_none_without_sign_change_and_bisection_fallback():
    assert irr([100, 50, 50]) is None
    assert irr([-100, -10]) is None
    # Newton from guess=0.1 diverges on this profile; bisection still finds the root
    flows = [-1000, 100, 100, 100, 100, 5000]
    r = irr(flows, guess=5.0)
    assert r is not None and npv(r, flows) == pytest.approx(0, abs=1e-3)


def test_cashflows_use_fee_revenue_and_terminal_value():
    p = Params()
    flows = ie.build_shared_model_cashflows(p)
    annual = p.n_buildings * (p.n_isps * p.neutral_access_fee_per_building_per_year_inr
                              - p.opex_per_building_per_year_inr)
    capex = p.n_buildings * p.fibre_capex_per_building_inr
    assert flows[0] == -capex and flows[1] == annual
    assert flows[-1] == pytest.approx(annual + capex * (1 - p.years / p.fibre_asset_life_years))
    no_tv = ie.build_shared_model_cashflows(ie.replace(p, include_terminal_value=False))
    assert no_tv[-1] == annual and npv(p.discount_rate, flows) > npv(p.discount_rate, no_tv)


def test_revenue_override_and_adoption_ramp_are_honoured():
    p = ie.replace(Params(), include_terminal_value=False, adoption_ramp=(0.5, 1.0))
    flows = ie.build_shared_model_cashflows(p)
    full = p.n_buildings * (p.n_isps * p.neutral_access_fee_per_building_per_year_inr
                            - p.opex_per_building_per_year_inr)
    assert flows[1] < full and flows[2] == full and flows[5] == full
    override = ie.build_shared_model_cashflows(ie.replace(Params(), include_terminal_value=False),
                                               annual_revenue_per_building_inr=4000)
    assert override[1] == Params().n_buildings * (4000 - Params().opex_per_building_per_year_inr)


def test_sensitivity_has_no_dead_variables_across_metrics():
    results, base = ie.sensitivity_analysis(Params())
    assert base == pytest.approx(ie.summary_report.__wrapped__(Params())["savings"])
    for r in results:
        assert r["influences"], f"{r['parameter']} moves no metric at all"
    by_name = {r["parameter"]: r for r in results}
    assert "neutral_npv" in by_name["neutral_access_fee_per_building_per_year_inr"]["influences"]
    assert "fwa_total_cost" in by_name["fwa_capex_per_building_inr"]["influences"]


def test_sensitivity_metric_selection_and_validation():
    results, _ = ie.sensitivity_analysis(Params(), metric="neutral_npv")
    top = {r["parameter"]: r["swing"] for r in results}
    assert top["neutral_access_fee_per_building_per_year_inr"] > 0
    with pytest.raises(ValueError):
        ie.sensitivity_analysis(Params(), metric="nonsense")


def test_sensitivity_does_not_mutate_params():
    p = Params()
    before = ie.dataclasses.asdict(p)
    ie.sensitivity_analysis(p)
    assert ie.dataclasses.asdict(p) == before


def test_breakeven_requires_neutral_operator_viability():
    p = Params()
    a = ie.breakeven_analysis(p)
    assert a["resource_cost_breakeven"] == 2
    assert a["neutral_operator_viable_from"] >= a["resource_cost_breakeven"]
    assert a["breakeven_n_isps"] == max(a["resource_cost_breakeven"], a["neutral_operator_viable_from"])
    assert find_breakeven_n_isps(p) == a["breakeven_n_isps"]
    # npv really is >= 0 at the break-even and < 0 just below it
    at = ie.replace(p, n_isps=a["breakeven_n_isps"])
    below = ie.replace(p, n_isps=a["breakeven_n_isps"] - 1)
    assert npv(at.discount_rate, ie.build_shared_model_cashflows(at)) >= 0
    assert npv(below.discount_rate, ie.build_shared_model_cashflows(below)) < 0


def test_breakeven_none_when_fee_too_high_for_isps():
    p = ie.replace(Params(), neutral_access_fee_per_building_per_year_inr=100000.0)
    assert ie.breakeven_analysis(p)["breakeven_n_isps"] is None


def test_three_way_has_legacy_keys_and_asset_life_adjustment():
    p = Params()
    data = ie.three_way_comparison_data(p)
    assert set(("ftth", "fwa", "satellite")) <= set(data)
    ftth = data["detail"]["ftth"]
    assert ftth["horizon_cost"] == pytest.approx(ftth["capex"] * p.years / p.fibre_asset_life_years + ftth["opex"])
    assert data["detail"]["satellite"]["horizon_cost"] == pytest.approx(data["detail"]["satellite"]["cash_total"])


def test_params_validation():
    for bad in (dict(n_isps=0), dict(years=0), dict(discount_rate=-0.1), dict(discount_rate=float("nan")),
                dict(fibre_capex_per_building_inr=-1), dict(adoption_ramp=(1.5,))):
        with pytest.raises(ValueError):
            Params(**bad)


def test_params_from_mapping_clamps_and_rejects_garbage():
    p = ie.params_from_mapping({"n_isps": "500", "years": "3.6", "discount_rate": "0.12",
                                "unknown": "x", "n_buildings": None})
    assert p.n_isps == 100 and p.years == 4 and p.discount_rate == 0.12
    assert p.n_buildings == Params().n_buildings
    with pytest.raises(ValueError):
        ie.params_from_mapping({"n_isps": "abc"})
    with pytest.raises(ValueError):
        ie.params_from_mapping({"discount_rate": "nan"})


def test_summary_has_consistent_totals():
    s = ie.summary_report.__wrapped__(Params())
    assert s["savings"] == s["duplicate_total"] - s["shared_total"]
    assert s["pct_savings"] == pytest.approx(s["savings"] / s["duplicate_total"] * 100)
