"""
Blacksheep news/report API
----------------------------
ASGI API over the toolkit's regulatory-news pipeline and combined HTML
report generator: scraping/archiving TRAI/DoT/PIB press releases, keyword
trend charts, Slack/email alerting, the stitched HTML report and the Claude
research agent. Kept separate from the Sanic analytics API so each can be
deployed/scaled independently (e.g. as two Azure Web Apps for Containers).

Security: every route except /health requires the X-API-Key header when
API_KEY is set, callers are rate limited per route (strictest for the routes
that cost money or send messages: /agent/ask, /news/alert, /news/scrape,
/report/generate), outbound feed fetches are SSRF-checked (security.py),
and request parameters are validated.

Run locally:
    uvicorn india_wireless_toolkit.services.blacksheep_api.app:app --host 0.0.0.0 --port 8002
"""

import os
import asyncio
from functools import partial

from blacksheep import Application, Content, Response, get, post, json as json_response

from india_wireless_toolkit import __version__
from india_wireless_toolkit import news_scraper as news
from india_wireless_toolkit import report_generator as report
from india_wireless_toolkit import agent
from india_wireless_toolkit import _plotting
from india_wireless_toolkit.config_loader import CONFIG, resolve_path
from india_wireless_toolkit.logging_config import setup_logging
from india_wireless_toolkit.security import RequestGuard, is_safe_public_url

setup_logging()

app = Application()

_PATHS = CONFIG.get("paths", {})
REPORTS_DIR = resolve_path(_PATHS.get("reports_dir", "reports"))
CHARTS_DIR = resolve_path(_PATHS.get("charts_dir", "charts"))

_guard = RequestGuard(rate_overrides=CONFIG.get("api", {}).get("rate_limits"))
_agent_slots = asyncio.Semaphore(3)          # at most 3 concurrent (billable) agent runs per process
_AGENT_MAX_TURNS_CEILING = 10                # hard server-side ceiling, independent of what a caller requests


# --- helpers --------------------------------------------------------------------------

async def _run_blocking(func, *args, **kwargs):
    """Offload blocking I/O (SQLite, requests, smtplib, matplotlib) to a worker thread."""
    return await asyncio.to_thread(partial(func, *args, **kwargs))


def _header(request, name: bytes):
    value = request.headers.get_first(name)
    return value.decode("latin-1") if value else None


def _peer_ip(request):
    try:
        return request.client_ip
    except Exception:
        client = (request.scope or {}).get("client")
        return client[0] if client else None


def _query(request, name: str, default=None):
    values = request.query.get(name)
    return values[0] if values else default


def _query_flag(request, name: str, default: bool) -> bool:
    raw = _query(request, name)
    return default if raw is None else str(raw).strip().lower() in ("1", "true", "yes", "on")


def _bytes_response(data: bytes, content_type: str, status: int = 200, headers: dict = None) -> Response:
    response = Response(status, None, Content(content_type.encode(), data))
    response.add_header(b"X-Content-Type-Options", b"nosniff")
    for key, value in (headers or {}).items():
        response.add_header(key.encode(), value.encode())
    return response


def _json(data, status: int = 200, headers: dict = None) -> Response:
    response = json_response(data, status=status)
    for key, value in (headers or {}).items():
        response.add_header(key.encode(), value.encode())
    return response


def _error(message: str, status: int = 400, **extra) -> Response:
    return _json({"error": message, **extra}, status=status)


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def _json_body(request) -> dict:
    """Parsed JSON object body ({} when empty). Raises ValueError for bad input."""
    if not request.content:
        return {}
    try:
        body = await request.json()
    except Exception:
        raise ValueError("Request body must be valid JSON.")
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")
    return body


async def security_middleware(request, handler):
    """API-key auth + rate limiting for every route except /health."""
    blocked = _guard.evaluate(
        request.method, request.url.path.decode(),
        _header(request, b"x-api-key"), _header(request, b"x-forwarded-for"), _peer_ip(request),
    )
    if blocked:
        status, body, headers = blocked
        return _json(body, status=status, headers=headers)
    return await handler(request)


app.middlewares.append(security_middleware)


# --- health ---------------------------------------------------------------------------

@get("/health")
async def health():
    return json_response({"status": "ok", "service": "blacksheep-news-api", "version": __version__})


# --- News archive ---------------------------------------------------------------------

@get("/news/archive")
async def news_archive(request):
    limit = news.clean_limit(_query(request, "limit"), default=50)
    include_sample = _query_flag(request, "include_sample", True)
    records = await _run_blocking(news.query_archive_records, limit, include_sample=include_sample)
    origins = await _run_blocking(news.archive_origin_summary)
    return _json({"count": len(records), "origins_in_archive": origins, "items": records})


@post("/news/dummy")
async def news_load_dummy():
    """Loads the bundled SAMPLE dataset (labelled origin='sample') - the reliable
    path when live scraping isn't reachable (e.g. network-restricted containers)."""
    items = await _run_blocking(news.load_dummy_dataset)
    new_items = await _run_blocking(news.store_items, items)
    return _json({"fetched": len(items), "newly_stored": len(new_items), "origin": "sample"})


@post("/news/scrape")
async def news_scrape():
    """Live scrape of TRAI/DoT/PIB. If nothing came back (e.g. no outbound
    internet) the bundled sample data is loaded instead and the response says so."""
    items, used_fallback = await _run_blocking(news.scrape_with_fallback)
    new_items = await _run_blocking(news.store_items, items)
    return _json({"fetched": len(items), "newly_stored": len(new_items),
                  "used_sample_fallback": used_fallback})


def _split_feeds(feed_urls):
    safe, rejected = [], []
    for url in feed_urls:
        (safe if isinstance(url, str) and is_safe_public_url(url) else rejected).append(url)
    return safe, rejected


@post("/news/rss")
async def news_rss(request):
    try:
        body = await _json_body(request)
    except ValueError as e:
        return _error(str(e))
    feed_urls = body.get("feeds", [])
    if not isinstance(feed_urls, list) or not feed_urls:
        return _error('Provide a JSON body: {"feeds": ["<rss url>", ...]}')
    if len(feed_urls) > 10:
        return _error("Too many feeds in one request (max 10).")

    # This endpoint fetches caller-supplied URLs server-side (SSRF vector). Every
    # hostname is resolved and must be public, and the fetch itself re-validates each
    # redirect and caps time and size (security.safe_get).
    safe_urls, rejected = await _run_blocking(_split_feeds, feed_urls)
    if not safe_urls:
        return _error("No valid feed URLs. Must be http(s) URLs to a public host.",
                      rejected=[str(u)[:200] for u in rejected])

    items = await _run_blocking(news.run_rss_scraper, safe_urls)
    new_items = await _run_blocking(news.store_items, items)
    response = {"fetched": len(items), "newly_stored": len(new_items)}
    if rejected:
        response["rejected_urls"] = [str(u)[:200] for u in rejected]
    return _json(response)


@get("/news/trend")
async def news_trend(request):
    include_sample = _query_flag(request, "include_sample", True)
    counts = await _run_blocking(news.keyword_counts, include_sample=include_sample)
    if not counts:
        return _error("No archived data yet. POST /news/dummy or /news/scrape first.", status=404)
    monthly = await _run_blocking(news.keyword_trend_series, include_sample=include_sample)
    origins = await _run_blocking(news.archive_origin_summary)
    return _json({"keyword_counts": counts, "monthly": monthly, "origins_in_archive": origins})


@get("/news/trend/chart")
async def news_trend_chart():
    chart_path = os.path.join(CHARTS_DIR, "keyword_trend.png")
    if _plotting.needs_refresh(chart_path, news.DB_PATH):
        counts = await _run_blocking(news.keyword_trend_report)
        if counts is None:
            return _error("No archived data yet.", status=404)
    return _bytes_response(await _run_blocking(_read_file, chart_path), "image/png")


@post("/news/alert")
async def news_alert(request):
    """Alerts on archived items that have NOT been alerted yet (sample data is skipped).
    Body (all optional): {"limit": 50, "include_sample": false, "resend": false}.
    Requires ALERT_WEBHOOK_URL / SMTP_* env vars - see .env.example."""
    try:
        body = await _json_body(request)
    except ValueError as e:
        return _error(str(e))
    limit = news.clean_limit(body.get("limit"), default=50)
    result = await _run_blocking(
        news.alert_new_items, None, limit=limit,
        include_sample=bool(body.get("include_sample", False)),
        resend=bool(body.get("resend", False)),
    )
    if result["items_considered"] == 0:
        return _json({**result, "note": "Nothing new to alert on."}, status=200)
    return _json(result)


# --- Combined report -------------------------------------------------------------------

_REPORT_CSP = {"Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'"}


@post("/report/generate")
async def report_generate():
    out_path = await _run_blocking(report.generate_report)
    return _json({"report_path": os.path.relpath(out_path, resolve_path("."))})


@get("/report")
async def report_get():
    out_path = os.path.join(REPORTS_DIR, "india_wireless_report.html")
    if not os.path.exists(out_path):
        out_path = await _run_blocking(report.generate_report)
    return _bytes_response(await _run_blocking(_read_file, out_path), "text/html; charset=utf-8",
                           headers=_REPORT_CSP)


# --- Agentic AI assistant ----------------------------------------------------------------
# See india_wireless_toolkit/agent.py for the tool allowlist, argument clamping and
# turn-limit safeguards. Requires ANTHROPIC_API_KEY in the environment (never accepted
# from the request body).

@post("/agent/ask")
async def agent_ask(request):
    """Body: {"question": "...", "max_turns": 6 (optional, capped at 10)}."""
    try:
        body = await _json_body(request)
    except ValueError as e:
        return _error(str(e))
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        return _error('Provide a JSON body: {"question": "..."}')
    question = question.strip()
    if len(question) > 2000:
        return _error("Question too long (max 2000 characters).")

    max_turns = body.get("max_turns")
    try:
        max_turns = min(_AGENT_MAX_TURNS_CEILING, max(1, int(max_turns))) if max_turns is not None else None
    except (TypeError, ValueError):
        max_turns = None

    if _agent_slots.locked():
        return _error("The assistant is busy; try again shortly.", status=429, retry_after_seconds=5)
    async with _agent_slots:
        try:
            result = await _run_blocking(agent.run_agent, question, max_turns=max_turns, verbose=False)
        except RuntimeError as e:
            # Missing API key / missing `anthropic` package: a config problem, not a caller
            # error. The message never contains the key itself.
            return _error(str(e), status=503)
    return _json(result)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8002)),
    )
