"""
Sanic analytics API
--------------------
Async JSON/image API over the toolkit's "compute" modules: charts,
spectrum simulation, and infra economics. Kept separate from the
Blacksheep news/report service so each can be scaled/deployed
independently (e.g. as two Azure Web Apps for Containers).

Run locally:
    python -m india_wireless_toolkit.services.sanic_api.app
    # or
    sanic india_wireless_toolkit.services.sanic_api.app:app --host 0.0.0.0 --port 8001
"""

import os
import math
import asyncio
from functools import partial

from sanic import Sanic
from sanic.response import json as json_response, file as file_response
from sanic.exceptions import NotFound

from india_wireless_toolkit import data_visualization as dv
from india_wireless_toolkit import spectrum_simulation as spec
from india_wireless_toolkit import infra_economics as infra
from india_wireless_toolkit.config_loader import CONFIG

app = Sanic("india-wireless-analytics-api")

_PATHS = CONFIG.get("paths", {})
CHARTS_DIR = os.path.abspath(_PATHS.get("charts_dir", "charts"))

_ALLOWED_CHARTS = {
    "broadband_split": dv.chart_broadband_split,
    "teledensity_gap": dv.chart_teledensity_gap,
    "6ghz_split": dv.chart_6ghz_split,
    "public_wifi_gap": dv.chart_public_wifi_gap,
    "subscriber_trend": dv.chart_subscriber_trend,
}


async def _run_blocking(func, *args, **kwargs):
    """Offload CPU-bound toolkit calls to a worker thread so they don't
    block Sanic's event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


@app.get("/health")
async def health(request):
    return json_response({"status": "ok", "service": "sanic-analytics-api"})


# --- Charts -------------------------------------------------------------

@app.get("/charts")
async def list_charts(request):
    return json_response({"available": sorted(_ALLOWED_CHARTS.keys())})


@app.get("/charts/<name:str>")
async def get_chart(request, name: str):
    if name not in _ALLOWED_CHARTS:
        raise NotFound(f"Unknown chart '{name}'. See GET /charts for valid names.")
    await _run_blocking(_ALLOWED_CHARTS[name])
    chart_path = os.path.join(CHARTS_DIR, f"{name}.png")
    if not os.path.exists(chart_path):
        raise NotFound("Chart was generated but the file could not be found.")
    return await file_response(chart_path, mime_type="image/png")


@app.get("/charts/dashboard")
async def dashboard(request):
    out_path = os.path.join(CHARTS_DIR, "dashboard.html")
    await _run_blocking(dv.build_interactive_dashboard, out_path)
    return await file_response(out_path, mime_type="text/html")


@app.get("/charts/map")
async def state_map(request):
    metric = request.args.get("metric", "coverage_5g_pct")
    out_path = os.path.join(CHARTS_DIR, "state_map.html")
    await _run_blocking(dv.build_state_bubble_map, out_path, metric)
    return await file_response(out_path, mime_type="text/html")


# --- Spectrum simulation --------------------------------------------------

@app.get("/spectrum/scenarios")
async def spectrum_scenarios(request):
    india = spec.SpectrumScenario("India (current, Jan 2026 gazette)", 500)
    us = spec.SpectrumScenario("US (full band delicensed)", 1200)
    result = {}
    for scenario in (india, us):
        rows = await _run_blocking(spec.simulate_scenario, scenario)
        result[scenario.name] = [
            {"channel_width_mhz": w, "channels": n, "aggregate_phy_mbps": mbps}
            for w, n, mbps in rows
        ]
    return json_response(result)


@app.get("/spectrum/sinr")
async def spectrum_sinr(request):
    tx_power = float(request.args.get("tx_power_dbm", 20.0))
    distances = [2, 5, 10, 15, 20, 30]
    rows = []
    for d in distances:
        walls = max(0, int(d // 8))
        sinr = spec.estimate_sinr_db(tx_power, d, wall_count=walls)
        quality = (
            "Excellent" if sinr > 30 else
            "Good" if sinr > 20 else
            "Fair" if sinr > 10 else
            "Poor"
        )
        rows.append({"distance_m": d, "walls": walls, "sinr_db": round(sinr, 1), "quality": quality})
    return json_response({"tx_power_dbm": tx_power, "results": rows})


@app.get("/spectrum/montecarlo")
async def spectrum_montecarlo(request):
    mhz = int(request.args.get("mhz", 500))
    aps = int(request.args.get("aps", 20))
    trials = int(request.args.get("trials", 300))
    result = await _run_blocking(spec.monte_carlo_ap_simulation, mhz, n_aps=aps, n_trials=trials)
    return json_response(result)


@app.get("/spectrum/thz")
async def spectrum_thz(request):
    freq_range = (100, 300)
    distances = (1, 5, 10, 20)
    rows = []
    for freq in freq_range:
        for d in distances:
            pl = spec.log_distance_path_loss_db(d, freq_ghz=freq, path_loss_exponent=2.5, wall_count=0)
            atmospheric_loss_db = 0.05 * freq * d
            rows.append({
                "freq_ghz": freq, "distance_m": d,
                "free_space_path_loss_db": round(pl, 1),
                "atmospheric_loss_db": round(atmospheric_loss_db, 1),
                "total_path_loss_db": round(pl + atmospheric_loss_db, 1),
            })
    return json_response({"freq_range_ghz": freq_range, "results": rows})


@app.get("/spectrum/whatif")
async def spectrum_whatif(request):
    mhz = int(request.args.get("mhz", 800))
    aps = int(request.args.get("aps", 20))
    scenario = spec.SpectrumScenario(f"What-if: {mhz} MHz delicensed", mhz)
    capacity_rows = await _run_blocking(spec.simulate_scenario, scenario)
    monte_carlo = await _run_blocking(spec.monte_carlo_ap_simulation, mhz, n_aps=aps, n_trials=200)
    return json_response({
        "scenario_mhz": mhz,
        "aps_per_building": aps,
        "capacity": [
            {"channel_width_mhz": w, "channels": n, "aggregate_phy_mbps": m}
            for w, n, m in capacity_rows
        ],
        "monte_carlo": monte_carlo,
    })


# --- Infra economics -------------------------------------------------------

@app.get("/infra/summary")
async def infra_summary(request):
    result = await _run_blocking(infra.summary_report)
    return json_response(result)


@app.get("/infra/npv")
async def infra_npv(request):
    p = infra.Params()
    project_npv, project_irr = await _run_blocking(infra.npv_irr_report, p)
    return json_response({
        "discount_rate": p.discount_rate,
        "npv_inr": project_npv,
        "irr": project_irr,
        "verdict": "financially attractive" if project_npv > 0 else "not attractive at this discount rate",
    })


@app.get("/infra/sensitivity")
async def infra_sensitivity(request):
    p = infra.Params()
    results, base_cost = await _run_blocking(infra.sensitivity_analysis, p)
    return json_response({"base_cost_inr": base_cost, "results": results})


@app.get("/infra/compare")
async def infra_compare(request):
    p = infra.Params()
    result = await _run_blocking(infra.three_way_comparison, p)
    return json_response(result)


@app.get("/infra/breakeven")
async def infra_breakeven(request):
    p = infra.Params()
    breakeven = await _run_blocking(infra.breakeven_report, p)
    return json_response({"breakeven_n_isps": breakeven})


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8001)),
        workers=int(os.environ.get("SANIC_WORKERS", 1)),
    )
