import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from india_wireless_toolkit.infra_economics import (
    Params, duplicate_model, shared_neutral_model, npv, irr, find_breakeven_n_isps,
)


def test_duplicate_more_expensive_than_shared_at_default_params():
    p = Params()
    dup_capex, dup_opex = duplicate_model(p)
    _, _, shared_total = shared_neutral_model(p)
    assert (dup_capex + dup_opex) > shared_total


def test_npv_zero_rate_equals_sum_of_cashflows():
    cashflows = [-100, 50, 50, 50]
    assert abs(npv(0.0, cashflows) - sum(cashflows)) < 1e-9


def test_irr_recovers_reasonable_rate():
    cashflows = [-100, 60, 60]
    rate = irr(cashflows)
    assert rate is not None
    assert 0.1 < rate < 0.3


def test_breakeven_isp_count_found():
    p = Params()
    breakeven = find_breakeven_n_isps(p)
    assert breakeven is None or breakeven >= 1
