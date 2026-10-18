"""
India Wireless Telecom - Data Visualization (v2.1)
-----------------------------------------------------
Features:
  1. Static snapshot charts (broadband split, teledensity gap, 6 GHz split,
     public Wi-Fi gap) driven by data/national_stats.csv and config.yaml
     instead of numbers hard-coded in the plotting code.
  2. Time-series trend chart from data/subscriber_trends.csv.
  3. Interactive Plotly dashboard (single HTML file) with a 6 GHz what-if slider.
  4. State-wise bubble map (needs plotly) from data/state_teledensity.csv.
  5. Optional live-fetch hook for TRAI open data (SSRF-safe client).

All charts are built on independent matplotlib `Figure` objects and written
atomically (see _plotting.py), so they are safe to generate from the web APIs'
worker threads. plotly is imported lazily - only the dashboard and map need it.
"""

import argparse
import csv
import logging
import os

from .config_loader import CONFIG, resolve_path
from . import _plotting
from .spectrum_simulation import (
    INDIA_DELICENSED_MHZ, FULL_BAND_MHZ, US_DELICENSED_MHZ, channels_available,
)

logger = logging.getLogger(__name__)

_PATHS = CONFIG.get("paths", {})
OUT_DIR = resolve_path(_PATHS.get("charts_dir", "charts"))
DATA_DIR = resolve_path(_PATHS.get("data_dir", "data"))
_SELF = os.path.abspath(__file__)


def _data_path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


def _out(filename: str) -> str:
    return os.path.join(OUT_DIR, filename)


# --- Data loading ----------------------------------------------------------------

def load_national_stats(csv_path: str = None) -> dict:
    """{key: {'value': float, 'unit', 'as_of', 'source'}} from data/national_stats.csv."""
    csv_path = csv_path or _data_path("national_stats.csv")
    stats = {}
    with open(csv_path, "r", newline="") as f:
        for row in csv.DictReader(f):
            stats[row["key"]] = {"value": float(row["value"]), "unit": row.get("unit", ""),
                                 "as_of": row.get("as_of", ""), "source": row.get("source", "")}
    return stats


def _stat(stats: dict, key: str) -> float:
    if key not in stats:
        raise KeyError(f"'{key}' is missing from data/national_stats.csv")
    return stats[key]["value"]


def _as_of(stats: dict, key: str) -> str:
    return stats[key]["as_of"] if key in stats else ""


def _to_float(value):
    """CSV cell -> float, or None when blank/non-numeric (unknown, not zero)."""
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def load_subscriber_trends(csv_path: str = None):
    csv_path = csv_path or _data_path("subscriber_trends.csv")
    with open(csv_path, "r", newline="") as f:
        return list(csv.DictReader(f))


def load_state_data(csv_path: str = None):
    csv_path = csv_path or _data_path("state_teledensity.csv")
    with open(csv_path, "r", newline="") as f:
        return list(csv.DictReader(f))


# --- Feature 1: static charts ----------------------------------------------------------

def chart_broadband_split():
    stats = load_national_stats()
    values = [_stat(stats, "wireless_broadband_millions"), _stat(stats, "wireline_broadband_millions")]
    labels = ["Wireless Broadband", "Wireline Broadband"]

    fig = _plotting.new_figure((6, 5))
    ax = fig.subplots()
    ax.bar(labels, values, color=["#2563eb", "#93c5fd"])
    for i, v in enumerate(values):
        ax.text(i, v + max(values) * 0.015, f"{v:.1f}M", ha="center", fontweight="bold")
    ax.set_ylabel("Subscribers (millions)")
    ax.set_title(f"India Broadband Subscribers: Wireless vs Wireline "
                 f"({_as_of(stats, 'wireless_broadband_millions')})")
    fig.tight_layout()
    return _plotting.save_figure(fig, _out("broadband_split.png"))


def chart_teledensity_gap():
    stats = load_national_stats()
    categories = ["Urban Teledensity", "Rural Teledensity", "National Avg"]
    values = [_stat(stats, "urban_teledensity_pct"), _stat(stats, "rural_teledensity_pct"),
              _stat(stats, "national_teledensity_pct")]

    fig = _plotting.new_figure((6, 5))
    ax = fig.subplots()
    ax.bar(categories, values, color=["#16a34a", "#dc2626", "#6b7280"])
    ax.axhline(100, linestyle="--", color="black", linewidth=1)
    ax.text(2.4, 102, "100% (1 connection/person)", fontsize=8, ha="right")
    for i, v in enumerate(values):
        ax.text(i, v + 3, f"{v:.1f}%", ha="center", fontweight="bold")
    ax.set_ylabel("Teledensity (%)")
    ax.set_title(f"Urban-Rural Teledensity Gap in India ({_as_of(stats, 'urban_teledensity_pct')})")
    fig.tight_layout()
    return _plotting.save_figure(fig, _out("teledensity_gap.png"))


def chart_6ghz_split():
    upper = max(0, FULL_BAND_MHZ - INDIA_DELICENSED_MHZ)
    labels = ["Lower 6 GHz\n(Delicensed for Wi-Fi 6E/7)", "Upper 6 GHz\n(Reserved for licensed 5G/6G)"]

    fig = _plotting.new_figure((6, 5))
    ax = fig.subplots()
    ax.pie([INDIA_DELICENSED_MHZ, upper], labels=labels,
           autopct=lambda p: f"{p:.0f}%\n({p / 100 * FULL_BAND_MHZ:.0f} MHz)",
           colors=["#0ea5e9", "#f97316"], startangle=90)
    ax.set_title(f"India's 6 GHz Band Split (Total: {FULL_BAND_MHZ} MHz)")
    fig.tight_layout()
    primary = _plotting.save_figure(fig, _out("6ghz_split.png"))

    mhz = [INDIA_DELICENSED_MHZ, US_DELICENSED_MHZ]
    countries = [f"India\n({mhz[0]} MHz)", f"US\n({mhz[1]} MHz, full band)"]
    fig = _plotting.new_figure((6, 5))
    ax = fig.subplots()
    ax.bar(countries, mhz, color=["#0ea5e9", "#22c55e"])
    for i, m in enumerate(mhz):
        ax.text(i, m + max(mhz) * 0.02, f"{m} MHz\n({channels_available(m, 160)}x 160MHz channels)",
                ha="center", fontsize=9)
    ax.set_ylabel("Delicensed spectrum for Wi-Fi (MHz)")
    ax.set_title("India vs US: Wi-Fi 6 GHz Spectrum Available")
    fig.tight_layout()
    _plotting.save_figure(fig, _out("6ghz_india_vs_us.png"))
    return primary


def chart_public_wifi_gap():
    stats = load_national_stats()
    labels = [f"Current PM-WANI\nHotspots ({_as_of(stats, 'pm_wani_hotspots_millions')})",
              f"Bharat 6G Vision\nTarget ({_as_of(stats, 'bharat_6g_hotspot_target_millions')})"]
    values = [_stat(stats, "pm_wani_hotspots_millions"), _stat(stats, "bharat_6g_hotspot_target_millions")]

    fig = _plotting.new_figure((6, 5))
    ax = fig.subplots()
    ax.bar(labels, values, color=["#eab308", "#7c3aed"])
    ax.set_yscale("log")
    for i, v in enumerate(values):
        ax.text(i, v * 1.3, f"{v:g}M", ha="center", fontweight="bold")
    ax.set_ylabel("Public Wi-Fi Hotspots (millions, log scale)")
    ax.set_title("India's Public Wi-Fi Gap: Current vs Target")
    fig.tight_layout()
    return _plotting.save_figure(fig, _out("public_wifi_gap.png"))


# --- Feature 2: time-series trend chart ---------------------------------------------------

def chart_subscriber_trend(csv_path: str = None):
    rows = load_subscriber_trends(csv_path)
    if not rows:
        raise ValueError("subscriber_trends.csv has no rows.")
    months = [r["month"] for r in rows]
    wireless = [float(r["wireless_broadband_millions"]) for r in rows]
    wireline = [float(r["wireline_broadband_millions"]) for r in rows]
    teledensity = [float(r["teledensity_pct"]) for r in rows]

    fig = _plotting.new_figure((9, 5))
    ax1 = fig.subplots()
    x = list(range(len(months)))        # positions + labels: month strings would be guessed as dates/floats
    ax1.plot(x, wireless, marker="o", color="#2563eb", label="Wireless Broadband (M)")
    ax1.plot(x, wireline, marker="o", color="#93c5fd", label="Wireline Broadband (M)")
    ax1.set_ylabel("Subscribers (millions)")
    ax1.set_xlabel("Month")
    ax1.set_xticks(x)
    ax1.set_xticklabels(months, rotation=45, ha="right")

    ax2 = ax1.twinx()
    ax2.plot(x, teledensity, marker="s", color="#dc2626", linestyle="--", label="Teledensity (%)")
    ax2.set_ylabel("Teledensity (%)")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=8)

    years = sorted({m[:4] for m in months if m[:4].isdigit()})
    span = f"{years[0]}\u2013{years[-1]}" if len(years) > 1 else (years[0] if years else "")
    ax1.set_title(f"India Broadband Subscriber Growth ({span})")
    fig.tight_layout()
    path = _plotting.save_figure(fig, _out("subscriber_trend.png"))
    logger.info("  Trend chart saved to %s", path)
    return path


# name -> (builder, primary output file, data files whose change should trigger a rebuild)
CHART_BUILDERS = {
    "broadband_split": (chart_broadband_split, "broadband_split.png", ["national_stats.csv"]),
    "teledensity_gap": (chart_teledensity_gap, "teledensity_gap.png", ["national_stats.csv"]),
    "6ghz_split": (chart_6ghz_split, "6ghz_split.png", []),
    "public_wifi_gap": (chart_public_wifi_gap, "public_wifi_gap.png", ["national_stats.csv"]),
    "subscriber_trend": (chart_subscriber_trend, "subscriber_trend.png", ["subscriber_trends.csv"]),
}
ALLOWED_CHARTS = tuple(CHART_BUILDERS)


def chart_path(name: str) -> str:
    return _out(CHART_BUILDERS[name][1])


def ensure_chart(name: str, force: bool = False) -> str:
    """Build the chart only if it is missing or its inputs changed; return its path."""
    if name not in CHART_BUILDERS:
        raise KeyError(f"Unknown chart '{name}'. Valid names: {sorted(CHART_BUILDERS)}")
    builder, filename, data_files = CHART_BUILDERS[name]
    sources = [_SELF, resolve_path("config.yaml")] + [_data_path(f) for f in data_files]
    out = _out(filename)
    if force or _plotting.needs_refresh(out, *sources):
        builder()
    return out


# --- Feature 3: interactive Plotly dashboard -------------------------------------------------

def _plotly():
    try:
        import plotly.graph_objects as go
    except ImportError as e:
        raise RuntimeError("plotly is required for the dashboard and map: pip install plotly") from e
    return go


def _plotlyjs_mode():
    return "inline" if CONFIG.get("visualization", {}).get("plotly_js", "cdn") == "inline" else "cdn"


def build_interactive_dashboard(out_path: str = None):
    go = _plotly()
    out_path = out_path or _out("dashboard.html")

    rows = load_subscriber_trends()
    months = [r["month"] for r in rows]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=months, y=[float(r["wireless_broadband_millions"]) for r in rows],
                             mode="lines+markers", name="Wireless Broadband (M)"))
    fig.add_trace(go.Scatter(x=months, y=[float(r["wireline_broadband_millions"]) for r in rows],
                             mode="lines+markers", name="Wireline Broadband (M)"))

    # "What-if" 6 GHz allocation slider: 160 MHz channels at each hypothetical allocation.
    mhz_options = list(range(100, 1300, 100))
    channel_counts = [channels_available(m, 160) for m in mhz_options]
    default_idx = min(range(len(mhz_options)), key=lambda i: abs(mhz_options[i] - INDIA_DELICENSED_MHZ))

    fig2 = go.Figure()
    for i, mhz in enumerate(mhz_options):
        fig2.add_trace(go.Bar(x=["160 MHz channels available"], y=[channel_counts[i]],
                              visible=(i == default_idx), name=f"{mhz} MHz"))
    steps = [dict(method="update",
                  args=[{"visible": [j == i for j in range(len(mhz_options))]},
                        {"title": f"6 GHz What-If: {mhz} MHz delicensed -> {channel_counts[i]} x 160MHz channels"}],
                  label=f"{mhz}") for i, mhz in enumerate(mhz_options)]
    fig2.update_layout(sliders=[dict(active=default_idx, currentvalue={"prefix": "MHz delicensed: "}, steps=steps)],
                       title="6 GHz What-If Slider", yaxis_title="160 MHz channels available")

    js_first = True if _plotlyjs_mode() == "inline" else "cdn"
    page = "".join([
        "<html><head><meta charset='utf-8'><title>India Wireless Dashboard</title></head><body>",
        "<h1>India Wireless Telecom Dashboard</h1><h2>Broadband Subscriber Trend</h2>",
        fig.to_html(full_html=False, include_plotlyjs=js_first),
        "<h2>6 GHz Spectrum What-If Slider</h2>",
        fig2.to_html(full_html=False, include_plotlyjs=False),
        "</body></html>",
    ])
    _plotting.write_text_atomic(out_path, page)
    logger.info("  Interactive dashboard saved to %s (open in a browser)", out_path)
    return out_path


# --- Feature 4: state-wise bubble map ------------------------------------------------------------

MAP_METRICS = ("urban_teledensity_pct", "rural_teledensity_pct", "coverage_5g_pct")


def build_state_bubble_map(out_path: str = None, metric: str = "coverage_5g_pct"):
    if metric not in MAP_METRICS:
        raise ValueError(f"Unknown metric '{metric}'. Valid: {list(MAP_METRICS)}")
    go = _plotly()
    out_path = out_path or _out("state_bubble_map.html")

    # States with an unknown value (blank in the CSV) are left off the map, not plotted as 0.
    rows = [(r, _to_float(r.get(metric))) for r in load_state_data()]
    rows = [(r, v) for r, v in rows if v is not None]
    if not rows:
        raise ValueError(f"No state has a value for '{metric}'.")

    fig = go.Figure(go.Scattergeo(
        lon=[float(r["lon"]) for r, _ in rows], lat=[float(r["lat"]) for r, _ in rows],
        text=[f"{r['state']}: {v}" for r, v in rows],
        marker=dict(size=[max(v, 1) / 2 for _, v in rows], color=[v for _, v in rows],
                    colorscale="Viridis", showscale=True, colorbar_title=metric),
    ))
    fig.update_geos(scope="asia", center=dict(lat=22, lon=79), projection_scale=4,
                    showcountries=True, showland=True)
    fig.update_layout(title=f"State-wise {metric} (bundled illustrative dataset)")
    _plotting.write_text_atomic(out_path, fig.to_html(include_plotlyjs=_plotlyjs_mode() == "inline" or "cdn"))
    logger.info("  State bubble map saved to %s (open in a browser)", out_path)
    return out_path


# --- Feature 5: live-fetch hook (network permitting) ------------------------------------------------

def fetch_live_trai_data(url: str, save_as: str = None):
    """Fetch a live CSV/JSON dataset (public http(s) hosts only, size-capped) and save it
    next to the bundled data. Returns the saved path, or None on failure."""
    from .security import safe_get, UnsafeURLError, ResponseTooLarge
    import requests
    try:
        _, body, _ = safe_get(url, timeout=15, max_bytes=20_000_000)
    except (requests.RequestException, UnsafeURLError, ResponseTooLarge) as e:
        logger.warning("  Live fetch failed (%s). Falling back to bundled dataset.", e)
        return None
    save_as = save_as or _data_path("live_fetch_cache.csv")
    with open(save_as, "wb") as f:
        f.write(body)
    logger.info("  Live data fetched and saved to %s", save_as)
    return save_as


def main(argv=None):
    from .logging_config import setup_logging
    setup_logging(cli=True)
    parser = argparse.ArgumentParser(description="India wireless telecom data visualization")
    parser.add_argument("--dashboard", action="store_true", help="Build the interactive Plotly dashboard")
    parser.add_argument("--map", action="store_true", help="Build the state-wise bubble map")
    parser.add_argument("--all-features", action="store_true", help="Run every visualization")
    args, _ = parser.parse_known_args(argv)

    for name in CHART_BUILDERS:
        ensure_chart(name, force=True)

    if args.dashboard or args.all_features:
        build_interactive_dashboard()
    if args.map or args.all_features:
        build_state_bubble_map()

    print(f"Done. Static charts saved in {OUT_DIR}/")


if __name__ == "__main__":
    main()
