"""
Combined HTML Report Generator
---------------------------------
Stitches together the static charts, key simulation/economics figures, and
the latest archived news items into a single shareable HTML report.

Security: everything that comes from outside the code (scraped titles, URLs,
sources) is HTML-escaped and links are limited to http(s), so a malicious
press-release title cannot inject markup or script into the report.
Provenance: sample (non-real) news rows are labelled in the table and a banner
appears whenever any are shown.
"""

import base64
import html
import logging
import os
from datetime import datetime
from urllib.parse import urlsplit

from .config_loader import CONFIG, resolve_path
from . import _plotting
from . import data_visualization, spectrum_simulation, infra_economics, news_scraper

logger = logging.getLogger(__name__)

_PATHS = CONFIG.get("paths", {})
CHARTS_DIR = resolve_path(_PATHS.get("charts_dir", "charts"))
REPORTS_DIR = resolve_path(_PATHS.get("reports_dir", "reports"))

CHART_FILES = [
    "broadband_split.png", "teledensity_gap.png", "6ghz_split.png",
    "6ghz_india_vs_us.png", "public_wifi_gap.png", "subscriber_trend.png",
]


def _img_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _safe_href(url: str) -> str:
    try:
        scheme = urlsplit((url or "").strip()).scheme.lower()
    except ValueError:
        return "#"
    return html.escape(url.strip(), quote=True) if scheme in ("http", "https") else "#"


def generate_report(out_path: str = None):
    out_path = out_path or os.path.join(REPORTS_DIR, "india_wireless_report.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # Charts are rebuilt only when missing or their inputs changed.
    for name in data_visualization.CHART_BUILDERS:
        data_visualization.ensure_chart(name)

    spectrum_text = spectrum_simulation.spectrum_text_report()
    p = infra_economics.Params()
    summary = infra_economics.summary_report(p)
    npv_result = infra_economics.npv_irr_analysis(p)
    breakeven = infra_economics.breakeven_analysis(p)

    try:
        news_rows = news_scraper.query_archive_records(limit=10)
    except Exception as e:
        logger.warning("Could not read the news archive: %s", e)
        news_rows = []

    fmt = infra_economics.format_inr_crore
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>India Wireless Toolkit Report</title>",
        "<style>body{font-family:sans-serif;max-width:900px;margin:40px auto;padding:0 20px;}",
        "img{max-width:100%;margin:10px 0;} pre{background:#f4f4f4;padding:15px;overflow-x:auto;}",
        "table{border-collapse:collapse;width:100%;} td,th{border:1px solid #ddd;padding:8px;text-align:left;}",
        ".note{background:#fff7e0;border:1px solid #f0d58c;padding:10px;margin:12px 0;}",
        "</style></head><body>",
        "<h1>India Wireless Telecom \u2014 Combined Report</h1>",
        f"<p><small>Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}. All figures are illustrative "
        "planning assumptions from config.yaml and the bundled datasets, not official TRAI/ISP data.</small></p>",
        "<h2>Charts</h2>",
    ]

    for chart in CHART_FILES:
        chart_path = os.path.join(CHARTS_DIR, chart)
        if os.path.exists(chart_path):
            title = chart.replace("_", " ").replace(".png", "").title()
            parts.append(f"<h3>{_e(title)}</h3>")
            parts.append(f"<img alt='{_e(title)}' src='data:image/png;base64,{_img_to_base64(chart_path)}'/>")

    parts += [
        "<h2>Infrastructure Economics Summary</h2>",
        "<table><tr><th>Metric</th><th>Value</th></tr>",
        f"<tr><td>Duplicate deployment total cost</td><td>{_e(fmt(summary['duplicate_total']))}</td></tr>",
        f"<tr><td>Shared-neutral total resource cost (one network)</td><td>{_e(fmt(summary['shared_total']))}</td></tr>",
        f"<tr><td>Savings from shared model</td><td>{_e(fmt(summary['savings']))} ({summary['pct_savings']:.1f}%)</td></tr>",
        f"<tr><td>ISP lease fees (transfer to the neutral operator)</td><td>{_e(fmt(summary['isp_leasing_cost_total']))}</td></tr>",
        f"<tr><td>Neutral operator NPV @ {p.discount_rate * 100:.0f}%</td><td>{_e(fmt(npv_result['npv_inr']))}"
        + (f" (IRR {npv_result['irr'] * 100:.1f}%)" if npv_result["irr"] is not None else "") + "</td></tr>",
        f"<tr><td>Break-even number of ISPs</td><td>{_e(breakeven['breakeven_n_isps'] or 'none within range')}</td></tr>",
        "</table>",
        "<h2>Spectrum Simulation Output</h2>",
        f"<pre>{_e(spectrum_text)}</pre>",
        "<h2>Recent Archived Press Releases</h2>",
    ]

    if news_rows:
        if any(r["origin"] == news_scraper.ORIGIN_SAMPLE for r in news_rows):
            parts.append("<div class='note'><strong>Sample data:</strong> rows marked <em>sample</em> come from "
                         "the bundled demo dataset, not from TRAI/DoT/PIB.</div>")
        parts.append("<table><tr><th>Source</th><th>Title</th><th>Date</th><th>Origin</th></tr>")
        for r in news_rows:
            parts.append(
                f"<tr><td>{_e(r['source'])}</td>"
                f"<td><a href='{_safe_href(r['url'])}' rel='noopener noreferrer'>{_e(r['title'])}</a></td>"
                f"<td>{_e(r['date'])}</td><td>{_e(r['origin'])}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append("<p>No archived news yet \u2014 run the news scraper module first "
                     "(<code>--dummy</code> works offline).</p>")

    parts.append("</body></html>")
    _plotting.write_text_atomic(out_path, "\n".join(parts))
    logger.info("Report generated: %s", out_path)
    return out_path


def main(argv=None):
    from .logging_config import setup_logging
    setup_logging(cli=True)
    generate_report()          # logs "Report generated: <path>"


if __name__ == "__main__":
    main()
