"""
Combined HTML Report Generator
---------------------------------
Stitches together the static charts, key simulation/economics figures, and
the latest archived news items into a single shareable HTML report.
"""

import os
import io
import base64
import contextlib

from .config_loader import CONFIG
from . import data_visualization, spectrum_simulation, infra_economics, news_scraper

_PATHS = CONFIG.get("paths", {})
CHARTS_DIR = _PATHS.get("charts_dir", "charts")
REPORTS_DIR = _PATHS.get("reports_dir", "reports")


def _img_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _capture_stdout(func, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue()


def generate_report(out_path: str = None):
    out_path = out_path or os.path.join(REPORTS_DIR, "india_wireless_report.html")
    os.makedirs(REPORTS_DIR, exist_ok=True)

    # Ensure charts exist
    data_visualization.chart_broadband_split()
    data_visualization.chart_teledensity_gap()
    data_visualization.chart_6ghz_split()
    data_visualization.chart_public_wifi_gap()
    data_visualization.chart_subscriber_trend()

    # Capture text output from spectrum + infra modules
    spectrum_text = _capture_stdout(spectrum_simulation.main)
    infra_p = infra_economics.Params()
    infra_summary = infra_economics.summary_report(infra_p)

    # News archive snapshot
    try:
        news_rows = news_scraper.query_archive(limit=10)
    except Exception:
        news_rows = []

    chart_files = [
        "broadband_split.png", "teledensity_gap.png", "6ghz_split.png",
        "6ghz_india_vs_us.png", "public_wifi_gap.png", "subscriber_trend.png",
    ]

    html_parts = [
        "<html><head><meta charset='utf-8'><title>India Wireless Toolkit Report</title>",
        "<style>body{font-family:sans-serif;max-width:900px;margin:40px auto;padding:0 20px;}",
        "img{max-width:100%;margin:10px 0;} pre{background:#f4f4f4;padding:15px;overflow-x:auto;}",
        "table{border-collapse:collapse;width:100%;} td,th{border:1px solid #ddd;padding:8px;text-align:left;}",
        "</style></head><body>",
        "<h1>India Wireless Telecom — Combined Report</h1>",
        "<h2>Charts</h2>",
    ]

    for chart in chart_files:
        chart_path = os.path.join(CHARTS_DIR, chart)
        if os.path.exists(chart_path):
            html_parts.append(f"<h3>{chart.replace('_', ' ').replace('.png', '').title()}</h3>")
            html_parts.append(f"<img src='data:image/png;base64,{_img_to_base64(chart_path)}'/>")

    html_parts.append("<h2>Infrastructure Economics Summary</h2>")
    html_parts.append("<table><tr><th>Metric</th><th>Value</th></tr>")
    html_parts.append(f"<tr><td>Duplicate deployment total cost</td>"
                       f"<td>{infra_economics.format_inr_crore(infra_summary['duplicate_total'])}</td></tr>")
    html_parts.append(f"<tr><td>Shared-neutral total cost</td>"
                       f"<td>{infra_economics.format_inr_crore(infra_summary['shared_total'])}</td></tr>")
    html_parts.append(f"<tr><td>Savings from shared model</td>"
                       f"<td>{infra_economics.format_inr_crore(infra_summary['savings'])} "
                       f"({infra_summary['pct_savings']:.1f}%)</td></tr>")
    html_parts.append("</table>")

    html_parts.append("<h2>Spectrum Simulation Output</h2>")
    html_parts.append(f"<pre>{spectrum_text}</pre>")

    html_parts.append("<h2>Recent Archived Press Releases</h2>")
    if news_rows:
        html_parts.append("<table><tr><th>Source</th><th>Title</th><th>Date</th></tr>")
        for source, title, url, date, _ in news_rows:
            html_parts.append(f"<tr><td>{source}</td><td><a href='{url}'>{title}</a></td><td>{date}</td></tr>")
        html_parts.append("</table>")
    else:
        html_parts.append("<p>No archived news yet — run the news scraper module first "
                           "(<code>--dummy</code> works offline).</p>")

    html_parts.append("</body></html>")

    with open(out_path, "w") as f:
        f.write("\n".join(html_parts))

    print(f"Report generated: {out_path}")
    return out_path


def main():
    generate_report()


if __name__ == "__main__":
    main()
