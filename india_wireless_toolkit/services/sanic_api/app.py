"""
Sanic analytics API
--------------------
Async JSON/image API over the toolkit's "compute" modules: charts,
spectrum simulation, and infra economics. Kept separate from the
Blacksheep news/report service so each can be scaled/deployed
independently (e.g. as two Azure Web Apps for Containers).

Security: every route except /health requires the X-API-Key header when
API_KEY is set (see security.py), and callers are rate limited per route.
All query parameters are bounded, so a request's cost is predictable.

Run locally:
    python -m india_wireless_toolkit.services.sanic_api.app
    # or
    sanic india_wireless_toolkit.services.sanic_api.app:app --host 0.0.0.0 --port 8001
"""

import os
import asyncio
from functools import partial

from sanic import Sanic
from sanic.response import json as json_response, file as file_response
from sanic.exceptions import NotFound, BadRequest, ServiceUnavailable

from india_wireless_toolkit import __version__
from india_wireless_toolkit import data_visualization as dv
from india_wireless_toolkit import spectrum_simulation as spec
from india_wireless_toolkit import infra_economics as infra
from india_wireless_toolkit import _plotting
from india_wireless_toolkit.config_loader import CONFIG
from india_wireless_toolkit.logging_config import setup_logging
from india_wireless_toolkit.security import RequestGuard, parse_bounded_int

setup_logging()

app = Sanic("india-wireless-analytics-api")

CHARTS_DIR = dv.OUT_DIR
_guard = RequestGuard(rate_overrides=CONFIG.get("api", {}).get("rate_limits"))


@app.on_request
async def security_middleware(request):
    """API-key auth + rate limiting for every route except /health."""
    blocked = _guard.evaluate(
        request.method, request.path,
        request.headers.get("x-api-key"), request.headers.get("x-forwarded-for"), request.ip,
    )
    if blocked:
        status, body, headers = blocked
        return json_response(body, status=status, headers=headers)


async def _run_blocking(func, *args, **kwargs):
    """Offload CPU-bound / blocking toolkit calls to a worker thread so they
    don't block Sanic's event loop."""
    return await asyncio.to_thread(partial(func, *args, **kwargs))


def _bounded_int(request, name: str, default: int, lo: int, hi: int) -> int:
    """Parse an integer query param and clamp it to [lo, hi]. The Monte Carlo
    endpoints are CPU-bound (O(n_aps^2 * n_trials)), so unbounded values would
    be a resource-exhaustion risk; non-integers are rejected with a 400."""
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        int(raw)
    except (TypeError, ValueError):
        raise BadRequest(f"'{name}' must be an integer.")
    return parse_bounded_int(raw, default, lo, hi)


def _infra_params(request):
    """Infra scenario from query parameters (n_isps, years, discount_rate, ...);
    omitted parameters keep their config.yaml defaults. Values are clamped."""
    try:
        return infra.params_from_mapping({k: request.args.get(k) for k in infra.PARAM_SPECS})
    except ValueError as e:
        raise BadRequest(str(e))


@app.get("/health")
async def health(request):
    return json_response({"status": "ok", "service": "sanic-analytics-api", "version": __version__})


# --- Charts -------------------------------------------------------------

@app.get("/charts")
async def list_charts(request):
    return json_response({"available": sorted(dv.CHART_BUILDERS)})


@app.get("/charts/<name:str>")
async def get_chart(request, name: str):
    if name not in dv.CHART_BUILDERS:
        raise NotFound(f"Unknown chart '{name}'. See GET /charts for valid names.")
    chart_path = await _run_blocking(dv.ensure_chart, name)   # rebuilt only when inputs changed
    return await file_response(chart_path, mime_type="image/png")


@app.get("/charts/dashboard")
async def dashboard(request):
    out_path = os.path.join(CHARTS_DIR, "dashboard.html")
    try:
        if _plotting.needs_refresh(out_path, dv._SELF, dv._data_path("subscriber_trends.csv")):
            await _run_blocking(dv.build_interactive_dashboard, out_path)
    except RuntimeError as e:                      # plotly missing
        raise ServiceUnavailable(str(e))
    return await file_response(out_path, mime_type="text/html")


@app.get("/charts/map")
async def state_map(request):
    metric = request.args.get("metric", "coverage_5g_pct")
    if metric not in dv.MAP_METRICS:
        raise BadRequest(f"'metric' must be one of {list(dv.MAP_METRICS)}.")
    out_path = os.path.join(CHARTS_DIR, f"state_bubble_map_{metric}.html")
    try:
        if _plotting.needs_refresh(out_path, dv._SELF, dv._data_path("state_teledensity.csv")):
            await _run_blocking(dv.build_state_bubble_map, out_path, metric)
    except RuntimeError as e:
        raise ServiceUnavailable(str(e))
    return await file_response(out_path, mime_type="text/html")


# --- Spectrum simulation ------------------------------------------------

@app.get("/spectrum/capacity")
async def spectrum_capacity(request):
    result = {}
    for scenario in spec.default_scenarios():
        result[scenario.name] = [
            {"channel_width_mhz": w, "channels": n, "aggregate_phy_mbps": mbps}
            for w, n, mbps in spec.scenario_capacity(scenario)
        ]
    return json_response(result)


@app.get("/spectrum/sinr")
async def spectrum_sinr(request):
    try:
        tx_power = float(request.args.get("tx_power_dbm", 20.0))
    except (TypeError, ValueError):
        raise BadRequest("'tx_power_dbm' must be a number.")
    tx_power = max(-10.0, min(40.0, tx_power))      # realistic Wi-Fi tx-power range
    return json_response({"tx_power_dbm": tx_power,
                          "results": spec.sinr_vs_distance_rows(tx_power_dbm=tx_power)})


@app.get("/spectrum/montecarlo")
async def spectrum_montecarlo(request):
    mhz = _bounded_int(request, "mhz", spec.INDIA_DELICENSED_MHZ, 20, 2000)
    aps = _bounded_int(request, "aps", 20, 1, 100)
    trials = _bounded_int(request, "trials", 300, 1, 2000)
    result = await _run_blocking(spec.monte_carlo_ap_simulation, mhz, n_aps=aps, n_trials=trials)
    return json_response(result)


@app.get("/spectrum/thz")
async def spectrum_thz(request):
    rows = [
        {"freq_ghz": r["freq_ghz"], "distance_m": r["distance_m"],
         "free_space_path_loss_db": r["path_loss_db"],
         "atmospheric_loss_db": r["atmospheric_loss_db"],
         "total_path_loss_db": r["total_path_loss_db"]}
        for r in spec.thz_path_loss_rows()
    ]
    return json_response({"freq_range_ghz": list(spec.THZ_FREQ_RANGE_GHZ), "results": rows,
                          "takeaway": spec.THZ_TAKEAWAY})


@app.get("/spectrum/whatif")
async def spectrum_whatif(request):
    mhz = _bounded_int(request, "mhz", 800, 20, 2000)
    aps = _bounded_int(request, "aps", 20, 1, 100)
    result = await _run_blocking(spec.what_if_analysis, mhz, aps)
    return json_response({
        "scenario_mhz": mhz,
        "aps_per_building": aps,
        "capacity": [
            {"channel_width_mhz": w, "channels": n, "aggregate_phy_mbps": m}
            for w, n, m in result["capacity_rows"]
        ],
        "dense_reuse_factor": result["dense_reuse_factor"],
        "monte_carlo": result["monte_carlo"],
    })


# --- Infra economics -------------------------------------------------------
# All endpoints accept the scenario as query parameters, e.g.
#   /infra/npv?n_isps=3&discount_rate=0.12&years=10
# (see infra_economics.PARAM_SPECS for the names and allowed ranges).

@app.get("/infra/summary")
async def infra_summary(request):
    result = await _run_blocking(infra.summary_report, _infra_params(request))
    return json_response(result)


@app.get("/infra/npv")
async def infra_npv(request):
    p = _infra_params(request)
    result = await _run_blocking(infra.npv_irr_analysis, p)
    return json_response({
        "discount_rate": p.discount_rate,
        "npv_inr": result["npv_inr"],
        "irr": result["irr"],
        "cashflows_inr": result["cashflows"],
        "includes_terminal_value": result["includes_terminal_value"],
        "verdict": result["verdict"],
    })


@app.get("/infra/sensitivity")
async def infra_sensitivity(request):
    metric = request.args.get("metric", "savings")
    if metric not in infra.SENSITIVITY_METRICS:
        raise BadRequest(f"'metric' must be one of {sorted(infra.SENSITIVITY_METRICS)}.")
    p = _infra_params(request)
    results, base = await _run_blocking(infra.sensitivity_analysis, p, None, metric)
    return json_response({"metric": metric, "base_value_inr": base, "base_cost_inr": base, "results": results})


@app.get("/infra/compare")
async def infra_compare(request):
    result = await _run_blocking(infra.three_way_comparison_data, _infra_params(request))
    return json_response(result)


@app.get("/infra/breakeven")
async def infra_breakeven(request):
    result = await _run_blocking(infra.breakeven_analysis, _infra_params(request))
    return json_response(result)


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8001)),
        workers=int(os.environ.get("SANIC_WORKERS", 1)),
    )
