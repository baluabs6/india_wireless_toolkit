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
from india_wireless_toolkit import agent
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
    new_items = await _run_blocking(news.store_items, items)
    return json_response({"fetched": len(items), "newly_stored": len(new_items)})


@post("/news/scrape")
async def news_scrape():
    """Live scrape of TRAI/DoT/PIB. Falls back to the dummy dataset if
    nothing came back (e.g. no outbound internet)."""
    items = await _run_blocking(news.run_live_scraper)
    if not items:
        items = await _run_blocking(news.load_dummy_dataset)
    new_items = await _run_blocking(news.store_items, items)
    return json_response({"fetched": len(items), "newly_stored": len(new_items)})


@post("/news/rss")
async def news_rss(request):
    body = await request.json()
    feed_urls = (body or {}).get("feeds", [])
    if not feed_urls:
        return json_response({"error": "Provide a JSON body: {\"feeds\": [\"<rss url>\", ...]}"}, status=400)

    # --- Secure coding: this endpoint makes server-side HTTP requests to
    # caller-supplied URLs, which is an SSRF vector if left unchecked.
    # Enforce an allowlisted scheme, cap the batch size, and reject
    # obvious loopback/link-local/internal hostnames before fetching.
    if len(feed_urls) > 10:
        return json_response({"error": "Too many feeds in one request (max 10)."}, status=400)

    safe_urls, rejected = [], []
    for url in feed_urls:
        if _is_safe_feed_url(url):
            safe_urls.append(url)
        else:
            rejected.append(url)

    if not safe_urls:
        return json_response({"error": "No valid feed URLs. Must be http(s) URLs to a public host.",
                               "rejected": rejected}, status=400)

    items = await _run_blocking(news.run_rss_scraper, safe_urls)
    new_items = await _run_blocking(news.store_items, items)
    response = {"fetched": len(items), "newly_stored": len(new_items)}
    if rejected:
        response["rejected_urls"] = rejected
    return json_response(response)


def _is_safe_feed_url(url: str) -> bool:
    """Best-effort SSRF guard for the /news/rss endpoint: only allow
    http(s) URLs with a real hostname that isn't localhost/loopback,
    link-local, or a bare IP literal (which are the common ways to reach
    internal services from a server-side fetch)."""
    import ipaddress
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False

    host = parsed.hostname.lower()
    if host in ("localhost",) or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    except ValueError:
        pass  # not an IP literal, it's a hostname — fine
    return True


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


# --- Agentic AI assistant ----------------------------------------------------
# See india_wireless_toolkit/agent.py for the tool allowlist, argument
# clamping, and turn-limit safeguards. Requires ANTHROPIC_API_KEY to be set
# in the environment (never accepted from the request body).

_AGENT_MAX_TURNS_CEILING = 10  # hard server-side ceiling, independent of what a caller requests


@post("/agent/ask")
async def agent_ask(request):
    """Body: {"question": "...", "max_turns": 6 (optional, capped at 10)}."""
    body = await request.json() if request.content else None
    question = (body or {}).get("question", "").strip()
    if not question:
        return json_response({"error": "Provide a JSON body: {\"question\": \"...\"}"}, status=400)
    if len(question) > 2000:
        return json_response({"error": "Question too long (max 2000 characters)."}, status=400)

    max_turns = (body or {}).get("max_turns")
    try:
        max_turns = min(_AGENT_MAX_TURNS_CEILING, max(1, int(max_turns))) if max_turns is not None else None
    except (TypeError, ValueError):
        max_turns = None

    try:
        result = await _run_blocking(agent.run_agent, question, max_turns=max_turns, verbose=False)
    except RuntimeError as e:
        # Missing API key / missing `anthropic` package — a config problem,
        # not a caller error, but still safe to surface directly since the
        # message never contains the key itself.
        return json_response({"error": str(e)}, status=503)

    return json_response(result)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8002)),
    )
