"""
India Wireless Telecom — Data Visualization (v2)
---------------------------------------------------
Features:
  1. Original static snapshot charts (broadband split, teledensity gap,
     6 GHz split, public Wi-Fi gap).
  2. Time-series trend chart from data/subscriber_trends.csv.
  3. Interactive Plotly dashboard (single HTML file, no server needed) with
     a scenario slider for the 6 GHz "what-if" MHz allocation.
  4. State-wise bubble map (teledensity / 5G coverage) using dummy
     data/state_teledensity.csv — avoids needing a GeoJSON/geopandas dependency.
  5. Optional live-fetch hook for TRAI's open data portal (network permitting).
"""

import os
import csv
import argparse

import matplotlib.pyplot as plt
import plotly.graph_objects as go

from .config_loader import CONFIG

_PATHS = CONFIG.get("paths", {})
OUT_DIR = _PATHS.get("charts_dir", "charts")
DATA_DIR = _PATHS.get("data_dir", "data")
os.makedirs(OUT_DIR, exist_ok=True)


def _data_path(filename: str) -> str:
    # Resolve relative to project root regardless of cwd
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, DATA_DIR, filename)


# --- Feature 1: original static charts --------------------------------------

def chart_broadband_split():
    labels = ["Wireless Broadband", "Wireline Broadband"]
    values = [1026.60, 46.84]
    colors = ["#2563eb", "#93c5fd"]

    fig, ax = plt.subplots(figsize=(6, 5))
    ax.bar(labels, values, color=colors)
    for i, v in enumerate(values):
        ax.text(i, v + 15, f"{v:.1f}M", ha="center", fontweight="bold")
    ax.set_ylabel("Subscribers (millions)")
    ax.set_title("India Broadband Subscribers: Wireless vs Wireline (Apr 2026)")
    fig.tight_layout()
    fig.savefig(f"{OUT_DIR}/broadband_split.png", dpi=150)
    plt.close(fig)


def chart_teledensity_gap():
    categories = ["Urban Teledensity", "Rural Teledensity", "National Avg"]
    values = [152.11, 60.74, 90.28]
    colors = ["#16a34a", "#dc2626", "#6b7280"]

    fig, ax = plt.subplots(figsize=(6, 5))
    ax.bar(categories, values, color=colors)
    ax.axhline(100, linestyle="--", color="black", linewidth=1)
    ax.text(2.4, 102, "100% (1 connection/person)", fontsize=8)
    for i, v in enumerate(values):
        ax.text(i, v + 3, f"{v:.1f}%", ha="center", fontweight="bold")
    ax.set_ylabel("Teledensity (%)")
    ax.set_title("Urban–Rural Teledensity Gap in India (Apr 2026)")
    fig.tight_layout()
    fig.savefig(f"{OUT_DIR}/teledensity_gap.png", dpi=150)
    plt.close(fig)


def chart_6ghz_split():
    labels = ["Lower 6 GHz\n(Delicensed for Wi-Fi 6E/7)", "Upper 6 GHz\n(Reserved for licensed 5G/6G)"]
    values_mhz = [500, 700]
    colors = ["#0ea5e9", "#f97316"]

    fig, ax = plt.subplots(figsize=(6, 5))
    ax.pie(values_mhz, labels=labels, autopct=lambda p: f"{p:.0f}%\n({p/100*1200:.0f} MHz)",
           colors=colors, startangle=90)
    ax.set_title("India's 6 GHz Band Split (Total: 1200 MHz)")
    fig.tight_layout()
    fig.savefig(f"{OUT_DIR}/6ghz_split.png", dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 5))
    countries = ["India\n(500 MHz)", "US\n(1200 MHz, full band)"]
    mhz = [500, 1200]
    max_channels_160 = [m // 160 for m in mhz]
    ax.bar(countries, mhz, color=["#0ea5e9", "#22c55e"])
    for i, (m, ch) in enumerate(zip(mhz, max_channels_160)):
        ax.text(i, m + 20, f"{m} MHz\n(~{ch}x 160MHz channels)", ha="center", fontsize=9)
    ax.set_ylabel("Delicensed spectrum for Wi-Fi (MHz)")
    ax.set_title("India vs US: Wi-Fi 6 GHz Spectrum Available")
    fig.tight_layout()
    fig.savefig(f"{OUT_DIR}/6ghz_india_vs_us.png", dpi=150)
    plt.close(fig)


def chart_public_wifi_gap():
    labels = ["Current PM-WANI\nHotspots (2026)", "Bharat 6G Vision\nTarget (2030)"]
    values = [0.4, 50]
    colors = ["#eab308", "#7c3aed"]

    fig, ax = plt.subplots(figsize=(6, 5))
    ax.bar(labels, values, color=colors)
    ax.set_yscale("log")
    for i, v in enumerate(values):
        ax.text(i, v * 1.3, f"{v}M", ha="center", fontweight="bold")
    ax.set_ylabel("Public Wi-Fi Hotspots (millions, log scale)")
    ax.set_title("India's Public Wi-Fi Gap: Current vs 2030 Target")
    fig.tight_layout()
    fig.savefig(f"{OUT_DIR}/public_wifi_gap.png", dpi=150)
    plt.close(fig)


# --- Feature 2: Time-series trend chart -------------------------------------

def load_subscriber_trends(csv_path: str = None):
    csv_path = csv_path or _data_path("subscriber_trends.csv")
    rows = []
    with open(csv_path, "r") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def chart_subscriber_trend(csv_path: str = None):
    rows = load_subscriber_trends(csv_path)
    months = [r["month"] for r in rows]
    wireless = [float(r["wireless_broadband_millions"]) for r in rows]
    wireline = [float(r["wireline_broadband_millions"]) for r in rows]
    teledensity = [float(r["teledensity_pct"]) for r in rows]

    fig, ax1 = plt.subplots(figsize=(9, 5))
    ax1.plot(months, wireless, marker="o", color="#2563eb", label="Wireless Broadband (M)")
    ax1.plot(months, wireline, marker="o", color="#93c5fd", label="Wireline Broadband (M)")
    ax1.set_ylabel("Subscribers (millions)")
    ax1.set_xlabel("Month")
    ax1.tick_params(axis="x", rotation=45)

    ax2 = ax1.twinx()
    ax2.plot(months, teledensity, marker="s", color="#dc2626", linestyle="--", label="Teledensity (%)")
    ax2.set_ylabel("Teledensity (%)")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=8)

    ax1.set_title("India Broadband Subscriber Growth (2024–2026)")
    fig.tight_layout()
    fig.savefig(f"{OUT_DIR}/subscriber_trend.png", dpi=150)
    plt.close(fig)
    print(f"  Trend chart saved to {OUT_DIR}/subscriber_trend.png")


# --- Feature 3: Interactive Plotly dashboard with what-if slider -----------

def build_interactive_dashboard(out_path: str = None):
    out_path = out_path or f"{OUT_DIR}/dashboard.html"

    rows = load_subscriber_trends()
    months = [r["month"] for r in rows]
    wireless = [float(r["wireless_broadband_millions"]) for r in rows]
    wireline = [float(r["wireline_broadband_millions"]) for r in rows]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=months, y=wireless, mode="lines+markers", name="Wireless Broadband (M)"))
    fig.add_trace(go.Scatter(x=months, y=wireline, mode="lines+markers", name="Wireline Broadband (M)"))

    # "What-if" 6 GHz allocation slider: shows how many 160MHz channels
    # would exist at each hypothetical MHz allocation from 100 to 1200.
    mhz_options = list(range(100, 1300, 100))
    channel_counts = [m // 160 for m in mhz_options]

    fig2 = go.Figure()
    for i, mhz in enumerate(mhz_options):
        fig2.add_trace(go.Bar(
            x=["160 MHz channels available"],
            y=[channel_counts[i]],
            visible=(i == 4),  # default to 500 MHz
            name=f"{mhz} MHz",
        ))

    steps = []
    for i, mhz in enumerate(mhz_options):
        step = dict(
            method="update",
            args=[{"visible": [j == i for j in range(len(mhz_options))]},
                  {"title": f"6 GHz What-If: {mhz} MHz delicensed -> {channel_counts[i]} x 160MHz channels"}],
            label=f"{mhz}",
        )
        steps.append(step)

    sliders = [dict(active=4, currentvalue={"prefix": "MHz delicensed: "}, steps=steps)]
    fig2.update_layout(sliders=sliders, title="6 GHz What-If Slider", yaxis_title="160 MHz channels available")

    # Combine both figures into one HTML file
    with open(out_path, "w") as f:
        f.write("<html><head><title>India Wireless Dashboard</title></head><body>")
        f.write("<h1>India Wireless Telecom Dashboard</h1>")
        f.write("<h2>Broadband Subscriber Trend</h2>")
        f.write(fig.to_html(full_html=False, include_plotlyjs="cdn"))
        f.write("<h2>6 GHz Spectrum What-If Slider</h2>")
        f.write(fig2.to_html(full_html=False, include_plotlyjs=False))
        f.write("</body></html>")

    print(f"  Interactive dashboard saved to {out_path} (open in a browser)")
    return out_path


# --- Feature 4: State-wise bubble map ----------------------------------------

def load_state_data(csv_path: str = None):
    csv_path = csv_path or _data_path("state_teledensity.csv")
    rows = []
    with open(csv_path, "r") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def build_state_bubble_map(out_path: str = None, metric: str = "coverage_5g_pct"):
    out_path = out_path or f"{OUT_DIR}/state_bubble_map.html"
    rows = load_state_data()

    fig = go.Figure(go.Scattergeo(
        lon=[float(r["lon"]) for r in rows],
        lat=[float(r["lat"]) for r in rows],
        text=[f"{r['state']}: {r[metric]}" for r in rows],
        marker=dict(
            size=[float(r[metric]) / 2 for r in rows],
            color=[float(r[metric]) for r in rows],
            colorscale="Viridis",
            showscale=True,
            colorbar_title=metric,
        ),
    ))
    fig.update_geos(scope="asia", center=dict(lat=22, lon=79), projection_scale=4,
                     showcountries=True, showland=True)
    fig.update_layout(title=f"State-wise {metric} (dummy dataset)")
    fig.write_html(out_path)
    print(f"  State bubble map saved to {out_path} (open in a browser)")
    return out_path


# --- Feature 5: Live-fetch hook (network permitting) ------------------------

def fetch_live_trai_data(url: str, save_as: str = None):
    """
    Attempts to fetch a live CSV/JSON dataset from TRAI's open data portal.
    Not runnable in network-restricted sandboxes — intended for use in an
    environment with normal internet access.
    """
    import requests
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        save_as = save_as or _data_path("live_fetch_cache.csv")
        with open(save_as, "wb") as f:
            f.write(resp.content)
        print(f"  Live data fetched and saved to {save_as}")
        return save_as
    except Exception as e:
        print(f"  Live fetch failed ({e}). Falling back to bundled dummy dataset.")
        return None


def main():
    parser = argparse.ArgumentParser(description="India wireless telecom data visualization")
    parser.add_argument("--dashboard", action="store_true", help="Build the interactive Plotly dashboard")
    parser.add_argument("--map", action="store_true", help="Build the state-wise bubble map")
    parser.add_argument("--all-features", action="store_true", help="Run every visualization")
    args, _ = parser.parse_known_args()

    chart_broadband_split()
    chart_teledensity_gap()
    chart_6ghz_split()
    chart_public_wifi_gap()
    chart_subscriber_trend()

    if args.dashboard or args.all_features:
        build_interactive_dashboard()
    if args.map or args.all_features:
        build_state_bubble_map()

    print(f"Done. Static charts saved in ./{OUT_DIR}/")


if __name__ == "__main__":
    main()
