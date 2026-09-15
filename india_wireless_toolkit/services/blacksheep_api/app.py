"""
Blacksheep news/report API
----------------------------
ASGI API over the toolkit's regulatory-news pipeline and combined HTML
report generator: scraping/archiving TRAI/DoT/PIB press releases, keyword
trend charts, Slack/email alerting, and the stitched HTML report. Kept
separate from the Sanic analytics API so each can be deployed/scaled
independently (e.g. as two Azure Web Apps for Containers).

Run locally:
    uvicorn india_wireless_toolkit.services.blacksheep_api.app:app --host 0.0.0.0 --port 8002
"""

import os
import asyncio
from functools import partial

from blacksheep import Application, get, post, json as json_response
from blacksheep.server.responses import file as file_response
from blacksheep.exceptions import NotFound

from india_wireless_toolkit import news_scraper as news
from india_wireless_toolkit import report_generator as report
from india_wireless_toolkit.config_loader import CONFIG

app = Application()

_PATHS = CONFIG.get("paths", {})
REPORTS_DIR = os.path.abspath(_PATHS.get("reports_dir", "reports"))
CHARTS_DIR = os.path.abspath(_PATHS.get("charts_dir", "charts"))


async def _run_blocking(func, *args, **kwargs):
    """Offload blocking I/O (SQLite, requests, smtplib) to a worker thread
    so it doesn't block the ASGI event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


@get("/health")
async def health():
    return json_response({"status": "ok", "service": "blacksheep-news-api"})


# --- News archive -----------------------------------------------------------

@get("/news/archive")
async def news_archive(limit: int = 50):
    rows = await _run_blocking(news.query_archive, limit=limit)
    return json_response({
        "count": len(rows),
        "items": [
            {"source": s, "title": t, "url": u, "date": d, "keywords_matched": k}
            for s, t, u, d, k in rows
        ],
    })


@post("/news/dummy")
async def news_load_dummy():
    """Loads the bundled offline dataset and archives new items — the
    reliable path when live scraping isn't reachable (e.g. network-
    restricted containers)."""
    items = await _run_blocking(news.load_dummy_dataset)
    new_count = await _run_blocking(news.store_items, items)
    return json_response({"fetched": len(items), "newly_stored": new_count})


@post("/news/scrape")
async def news_scrape():
    """Live scrape of TRAI/DoT/PIB. Falls back to the dummy dataset if
    nothing came back (e.g. no outbound internet)."""
    items = await _run_blocking(news.run_live_scraper)
    if not items:
        items = await _run_blocking(news.load_dummy_dataset)
    new_count = await _run_blocking(news.store_items, items)
    return json_response({"fetched": len(items), "newly_stored": new_count})


@post("/news/rss")
async def news_rss(request):
    body = await request.json()
    feed_urls = (body or {}).get("feeds", [])
    if not feed_urls:
        return json_response({"error": "Provide a JSON body: {\"feeds\": [\"<rss url>\", ...]}"}, status=400)
    items = await _run_blocking(news.run_rss_scraper, feed_urls)
    new_count = await _run_blocking(news.store_items, items)
    return json_response({"fetched": len(items), "newly_stored": new_count})


@get("/news/trend")
async def news_trend():
    counts = await _run_blocking(news.keyword_trend_report)
    if counts is None:
        return json_response({"error": "No archived data yet. POST /news/dummy or /news/scrape first."}, status=404)
    return json_response({"keyword_counts": counts})


@get("/news/trend/chart")
async def news_trend_chart():
    counts = await _run_blocking(news.keyword_trend_report)
    if counts is None:
        raise NotFound()
    chart_path = os.path.join(CHARTS_DIR, "keyword_trend.png")
    return await file_response(chart_path, "image/png")


@post("/news/alert")
async def news_alert(request):
    """Sends Slack/email alerts for the most recently archived items.
    Requires ALERT_WEBHOOK_URL / SMTP_* env vars (or .env) to be set —
    see .env.example."""
    body = await request.json() if request.content else None
    limit = (body or {}).get("limit", 10)
    rows = await _run_blocking(news.query_archive, limit=limit)
    items = [{"source": s, "title": t, "url": u, "date": d} for s, t, u, d, _ in rows]
    if not items:
        return json_response({"error": "No archived items to alert on."}, status=404)
    slack_sent = await _run_blocking(news.send_slack_alert, items)
    email_sent = await _run_blocking(news.send_email_alert, items)
    return json_response({"items_considered": len(items), "slack_sent": slack_sent, "email_sent": email_sent})


# --- Combined report ---------------------------------------------------------

@post("/report/generate")
async def report_generate():
    out_path = await _run_blocking(report.generate_report)
    return json_response({"report_path": out_path})


@get("/report")
async def report_get():
    out_path = os.path.join(REPORTS_DIR, "india_wireless_report.html")
    if not os.path.exists(out_path):
        out_path = await _run_blocking(report.generate_report)
    return await file_response(out_path, "text/html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8002)),
    )
