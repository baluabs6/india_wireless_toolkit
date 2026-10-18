"""
India Telecom News/Press-Release Scraper (v2.1)
--------------------------------------------------
Features:
  1. TRAI/DoT/PIB HTML scraper (needs live internet; SSRF-safe, size/time capped).
  2. SQLite archive with URL-canonicalising dedup, record provenance
     (`origin`: live | rss | sample), real publication dates and alert state.
  3. RSS feed support (feedparser) fetched through the same safe client.
  4. Alerts (Slack webhook / email) that only ever send items not yet alerted.
  5. Keyword statistics and a keyword trend chart (totals + monthly series).
  6. Bundled sample dataset fallback so the pipeline works offline - always
     labelled origin='sample' so it can never be mistaken for real news.

Usage:
    python -m india_wireless_toolkit.cli news             # live scrape (needs internet)
    python -m india_wireless_toolkit.cli news --dummy     # load bundled sample dataset
    python -m india_wireless_toolkit.cli news --trend     # plot keyword trend from archive
"""

import os
import re
import json
import time
import ssl
import sqlite3
import hashlib
import logging
import argparse
import smtplib
import unicodedata
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, date as date_cls
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup

from .config_loader import CONFIG, resolve_path
from . import _plotting
from .security import safe_get, UnsafeURLError, ResponseTooLarge

logger = logging.getLogger(__name__)

_CFG = CONFIG.get("news_scraper", {})
_PATHS = CONFIG.get("paths", {})

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

SOURCES = _CFG.get("sources", {
    "TRAI": "https://www.trai.gov.in/notifications/press-release",
    "DoT": "https://dot.gov.in/relatedlinks/press-release",
})
KEYWORDS = _CFG.get("keywords", ["spectrum", "wi-fi", "5g", "6g", "satcom"])
MAX_ITEMS_PER_SOURCE = int(_CFG.get("max_items_per_source", 15))
FETCH_TIMEOUT = float(_CFG.get("fetch_timeout_seconds", 15))
FETCH_MAX_BYTES = int(_CFG.get("fetch_max_bytes", 3_000_000))

DB_PATH = os.environ.get("AZURE_NEWS_DB_PATH") or resolve_path(_CFG.get("sqlite_db_path", "news_archive.db"))
DUMMY_PATH = resolve_path(_CFG.get("dummy_data_path", "data/press_releases_sample.json"))

ORIGIN_LIVE, ORIGIN_RSS, ORIGIN_SAMPLE = "live", "rss", "sample"


def clean_limit(value, default: int = 50, lo: int = 1, hi: int = 500) -> int:
    """Untrusted `limit` -> safe int in [lo, hi] (SQLite treats LIMIT -1 as 'all rows')."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


# --- URL / title / date normalisation -------------------------------------------------

_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "source", "igshid"}


def canonicalize_url(url: str) -> str:
    """Stable form of a URL for dedup: https scheme, lower-case host without
    'www.', default port dropped, duplicate slashes and trailing slash removed,
    tracking parameters and fragment stripped, query sorted."""
    if not url:
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return url
    if not parts.netloc:
        return url.split("#")[0]
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")
    )
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def _normalize_title(title: str) -> str:
    title = unicodedata.normalize("NFKC", title or "").casefold()
    return re.sub(r"\s+", " ", title).strip(" .:-\u2013\u2014")


def record_id(title: str, url: str) -> str:
    key = f"{canonicalize_url(url)}|{_normalize_title(title)}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


_DATE_FORMATS = (
    "%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%d/%m/%Y",
    "%d-%m-%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %B, %Y", "%d %b, %Y",
)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _valid_iso(year: int, month: int, day: int):
    try:
        d = date_cls(year, month, day)
    except ValueError:
        return None
    return d.isoformat() if 2000 <= d.year <= 2100 else None


def normalize_date(value):
    """Best-effort conversion to an ISO 'YYYY-MM-DD' string; None if unparseable.
    Accepts ISO strings, RFC-822 (RSS) dates, common Indian formats
    (dd/mm/yyyy, '3 June 2026') and time.struct_time."""
    if value is None or value == "":
        return None
    if isinstance(value, time.struct_time):
        return _valid_iso(value.tm_year, value.tm_mon, value.tm_mday)
    if isinstance(value, (datetime, date_cls)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    text = str(value).strip()
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        return _valid_iso(*map(int, iso.groups()))
    try:
        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return extract_date_from_text(text)


def extract_date_from_text(text: str):
    """Find the first plausible date inside free text (e.g. a listing row)."""
    if not text:
        return None
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if m:
        return _valid_iso(*map(int, m.groups()))
    m = re.search(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](20\d{2})\b", text)      # day first (India)
    if m:
        d, mo, y = map(int, m.groups())
        return _valid_iso(y, mo, d)
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-]+([A-Za-z]{3,9})\.?,?[\s\-]+(20\d{2})\b", text)
    if m and m.group(2)[:3].lower() in _MONTHS:
        return _valid_iso(int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", text)
    if m and m.group(1)[:3].lower() in _MONTHS:
        return _valid_iso(int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
    return None


# --- Keyword matching ---------------------------------------------------------------------

# Short acronyms that are ordinary English words in lower case.
_CASE_SENSITIVE = {"row": r"(?<![A-Za-z0-9])(?:ROW|RoW)(?![A-Za-z0-9])"}


@lru_cache(maxsize=512)
def _keyword_regex(keyword: str):
    kw = keyword.strip().lower()
    if kw in _CASE_SENSITIVE:
        return re.compile(_CASE_SENSITIVE[kw])
    tokens = [re.escape(t) for t in re.split(r"[\s\-]+", kw) if t]
    body = r"[\s\-]*".join(tokens)             # 'wi-fi' also matches 'wi fi' and 'wifi'
    plural = "s?" if len(kw) >= 4 and kw[-1].isalpha() else ""
    return re.compile(rf"(?<![a-z0-9]){body}{plural}(?![a-z0-9])", re.IGNORECASE)


def find_keywords(text: str, keywords=None):
    """Keywords (as configured) that occur in `text` as whole words/phrases."""
    keywords = KEYWORDS if keywords is None else keywords
    return [kw for kw in keywords if _keyword_regex(kw).search(text or "")]


# --- SQLite archive -------------------------------------------------------------------------

SCHEMA_VERSION = 2
_NEW_COLUMNS = [
    ("origin", "origin TEXT NOT NULL DEFAULT 'unknown'"),
    ("date_source", "date_source TEXT NOT NULL DEFAULT 'unknown'"),
    ("alerted_at", "alerted_at TEXT"),
    ("canonical_url", "canonical_url TEXT"),
]
_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS press_releases (
        id TEXT PRIMARY KEY,
        source TEXT,
        title TEXT,
        url TEXT,
        date TEXT,
        keywords_matched TEXT,
        fetched_at TEXT,
        origin TEXT NOT NULL DEFAULT 'unknown',
        date_source TEXT NOT NULL DEFAULT 'unknown',
        alerted_at TEXT,
        canonical_url TEXT
    )
"""


def _migrate(conn: sqlite3.Connection):
    """Upgrade an older archive in place: add provenance/alert columns, label rows
    that came from the bundled sample file, and re-key ids with the canonical-URL
    hash (collapsing duplicates the old md5(title|url) key could not see)."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        conn.commit()
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(press_releases)")}
    for name, ddl in _NEW_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE press_releases ADD COLUMN {ddl}")

    try:
        with open(DUMMY_PATH, "r") as f:
            sample_urls = {canonicalize_url(i.get("url", "")) for i in json.load(f)}
    except (OSError, ValueError):
        sample_urls = set()

    rows = conn.execute(
        "SELECT id, source, title, url, date, keywords_matched, fetched_at, origin, date_source, alerted_at "
        "FROM press_releases ORDER BY fetched_at, rowid"
    ).fetchall()
    conn.execute("DELETE FROM press_releases")
    seen = set()
    for (_, source, title, url, date, kws, fetched_at, origin, date_source, alerted_at) in rows:
        new_id = record_id(title or "", url or "")
        if new_id in seen:
            continue
        seen.add(new_id)
        canon = canonicalize_url(url or "")
        if origin == "unknown" and canon in sample_urls:
            origin = ORIGIN_SAMPLE
        conn.execute(
            "INSERT INTO press_releases (id, source, title, url, date, keywords_matched, fetched_at, "
            "origin, date_source, alerted_at, canonical_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (new_id, source, title, url, date, kws, fetched_at, origin, date_source, alerted_at, canon),
        )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def init_db(db_path: str = None):
    """Open the archive (creating / migrating it as needed). Caller closes the connection."""
    db_path = db_path or DB_PATH
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute(_CREATE_SQL)
        conn.commit()
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            _migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def _connect(db_path: str = None):
    conn = init_db(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _resolve_date(item: dict):
    parsed = normalize_date(item.get("date"))
    if parsed:
        return parsed, item.get("date_source") or "published"
    return time.strftime("%Y-%m-%d"), "fetched"      # unknown publication date: say so


def store_items(items: list, db_path: str = None, origin: str = None):
    """Insert items, skipping duplicates (same canonical URL + normalised title).
    Returns the items that were NEWLY inserted, each enriched with its `id`,
    resolved `date`/`date_source` and `origin`, so callers (alerting) act on the
    right subset."""
    newly_stored = []
    with _connect(db_path) as conn:
        for item in items:
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            if not title:
                logger.warning("Skipping item without a title: %r", item)
                continue
            rid = record_id(title, url)
            date, date_source = _resolve_date(item)
            item_origin = item.get("origin") or origin or "unknown"
            try:
                conn.execute(
                    "INSERT INTO press_releases (id, source, title, url, date, keywords_matched, fetched_at, "
                    "origin, date_source, canonical_url) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rid, item.get("source", ""), title, url, date,
                     json.dumps(item.get("keywords_matched", [])),
                     time.strftime("%Y-%m-%d %H:%M:%S"), item_origin, date_source,
                     canonicalize_url(url)),
                )
                newly_stored.append({**item, "title": title, "url": url, "id": rid, "date": date,
                                     "date_source": date_source, "origin": item_origin})
            except sqlite3.IntegrityError:
                pass          # already stored (dedup)
    return newly_stored


_RECORD_COLUMNS = "id, source, title, url, date, keywords_matched, origin, date_source, alerted_at, fetched_at"


def _decode_keywords(raw):
    try:
        value = json.loads(raw or "[]")
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def query_archive_records(limit: int = 50, db_path: str = None, include_sample: bool = True,
                          only_unalerted: bool = False):
    limit = clean_limit(limit)
    where, args = [], []
    if not include_sample:
        where.append("origin != ?")
        args.append(ORIGIN_SAMPLE)
    if only_unalerted:
        where.append("alerted_at IS NULL")
    sql = f"SELECT {_RECORD_COLUMNS} FROM press_releases"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date DESC, fetched_at DESC, id LIMIT ?"
    with _connect(db_path) as conn:
        rows = conn.execute(sql, (*args, limit)).fetchall()
    keys = ["id", "source", "title", "url", "date", "keywords_matched", "origin",
            "date_source", "alerted_at", "fetched_at"]
    records = []
    for row in rows:
        rec = dict(zip(keys, row))
        rec["keywords_matched"] = _decode_keywords(rec["keywords_matched"])
        records.append(rec)
    return records


def query_archive(limit: int = 50, db_path: str = None, include_sample: bool = True):
    """Compatibility view: [(source, title, url, date, keywords_json), ...]."""
    return [(r["source"], r["title"], r["url"], r["date"], json.dumps(r["keywords_matched"]))
            for r in query_archive_records(limit, db_path, include_sample)]


def archive_origin_summary(db_path: str = None) -> dict:
    with _connect(db_path) as conn:
        return dict(conn.execute("SELECT origin, COUNT(*) FROM press_releases GROUP BY origin").fetchall())


def mark_alerted(ids, db_path: str = None) -> int:
    ids = list(ids)
    if not ids:
        return 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _connect(db_path) as conn:
        cur = conn.executemany("UPDATE press_releases SET alerted_at = ? WHERE id = ?",
                               [(now, i) for i in ids])
        return cur.rowcount


# --- Feature 1: Live HTML scraper (needs real internet) ----------------------------------------

def fetch_page(url: str) -> BeautifulSoup:
    final_url, body, _ = safe_get(url, headers=HEADERS, timeout=FETCH_TIMEOUT, max_bytes=FETCH_MAX_BYTES)
    return BeautifulSoup(body, "html.parser")


_SKIP_HREF = ("#", "javascript:", "mailto:", "tel:")


def _row_text(anchor, max_chars: int = 600) -> str:
    """Text of the listing row an anchor sits in (tr/li, else a nearby ancestor)."""
    node = anchor.find_parent(["tr", "li"]) or anchor.parent
    for _ in range(3):
        if node is None:
            break
        text = node.get_text(" ", strip=True)
        if len(text) <= max_chars:
            if extract_date_from_text(text):
                return text
        node = node.parent
    return anchor.get_text(" ", strip=True)


def extract_links(soup: BeautifulSoup, page_url: str):
    """[(title, absolute_url, iso_date_or_None), ...] for links with a long-enough title."""
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        href = a["href"].strip()
        if not title or len(title) < 15 or href.lower().startswith(_SKIP_HREF):
            continue
        absolute = urljoin(page_url, href)
        if urlsplit(absolute).scheme not in ("http", "https"):
            continue
        key = (absolute, title)
        if key in seen:
            continue
        seen.add(key)
        items.append((title, absolute, extract_date_from_text(_row_text(a))))
    return items


def filter_by_keywords(items, keywords):
    """items: (title, href[, date]) tuples -> [(title, href, hits), ...]."""
    matched = []
    for title, href, *_ in items:
        hits = find_keywords(title, keywords)
        if hits:
            matched.append((title, href, hits))
    return matched


def run_live_scraper():
    all_matches = []
    for source_name, url in SOURCES.items():
        logger.info("Fetching %s: %s", source_name, url)
        try:
            soup = fetch_page(url)
        except (requests.RequestException, UnsafeURLError, ResponseTooLarge) as e:
            logger.warning("  Failed to fetch %s: %s", source_name, e)
            continue

        items = extract_links(soup, url)
        matches = [(t, h, d, find_keywords(t)) for t, h, d in items]
        matches = [m for m in matches if m[3]]
        logger.info("  Found %d links total, %d matched wireless-related keywords.", len(items), len(matches))
        for title, href, pub_date, hits in matches[:MAX_ITEMS_PER_SOURCE]:
            all_matches.append({
                "source": source_name, "title": title, "url": href,
                "date": pub_date, "date_source": "published" if pub_date else None,
                "keywords_matched": hits, "origin": ORIGIN_LIVE,
            })
        time.sleep(1)
    return all_matches


# --- Feature 6: sample dataset fallback ------------------------------------------------------------

def load_dummy_dataset(path: str = None):
    """Bundled sample press releases, each labelled origin='sample'."""
    path = path or DUMMY_PATH
    with open(path, "r") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list of press-release objects.")
    return [{**item, "origin": ORIGIN_SAMPLE} for item in data if isinstance(item, dict)]


def scrape_with_fallback():
    """Live scrape; if nothing comes back (e.g. no internet) use the sample data.
    Returns (items, used_sample_fallback)."""
    items = run_live_scraper()
    if items:
        return items, False
    return load_dummy_dataset(), True


# --- Feature 3: RSS feed support ----------------------------------------------------------------------

def run_rss_scraper(feed_urls: list):
    """Requires `feedparser`. Feeds are downloaded through the SSRF-safe client
    (public hosts only, timeouts, size cap) and then parsed from memory."""
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser not installed. Run: pip install feedparser")
        return []

    all_matches = []
    for feed_url in feed_urls:
        try:
            _, body, _ = safe_get(feed_url, headers=HEADERS, timeout=FETCH_TIMEOUT,
                                  max_bytes=FETCH_MAX_BYTES)
            feed = feedparser.parse(body)
        except (requests.RequestException, UnsafeURLError, ResponseTooLarge) as e:
            logger.warning("  Failed to fetch feed %s: %s", feed_url, e)
            continue
        except Exception as e:
            logger.warning("  Failed to parse feed %s: %s", feed_url, e)
            continue
        for entry in getattr(feed, "entries", []):
            title = entry.get("title", "")
            hits = find_keywords(title)
            if not hits:
                continue
            pub = normalize_date(entry.get("published_parsed") or entry.get("updated_parsed")
                                 or entry.get("published") or entry.get("updated"))
            all_matches.append({
                "source": feed_url, "title": title, "url": entry.get("link", ""),
                "date": pub, "date_source": "published" if pub else None,
                "keywords_matched": hits, "origin": ORIGIN_RSS,
            })
    return all_matches


# --- Feature 4: Alerts (Slack webhook + email) --------------------------------------------------------

_SLACK_BATCH = 20


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_url(url: str) -> str:
    return url.replace(" ", "%20").replace(">", "%3E").replace("|", "%7C")


def send_slack_alert(items: list, webhook_url: str = None):
    """True = every batch sent, False = configured but failed, None = not configured."""
    webhook_url = webhook_url or _CFG.get("alert_webhook_url", "")
    if not webhook_url:
        logger.info("[alerts] No Slack webhook configured (ALERT_WEBHOOK_URL). Skipping.")
        return None
    if urlsplit(webhook_url).scheme != "https":
        logger.warning("[alerts] Slack webhook must be an https URL. Skipping.")
        return False
    ok = True
    for start in range(0, len(items), _SLACK_BATCH):
        batch = items[start:start + _SLACK_BATCH]
        text = "*New India wireless/telecom press releases:*\n" + "\n".join(
            f"- <{_slack_url(i['url'])}|{_slack_escape(i['title'])}> ({_slack_escape(i.get('source', ''))})"
            for i in batch
        )
        try:
            resp = requests.post(webhook_url, json={"text": text}, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            logger.warning("[alerts] Slack alert failed: %s", e)
            ok = False
            break
    if ok:
        logger.info("[alerts] Slack alert sent for %d item(s).", len(items))
    return ok


def send_email_alert(items: list):
    """True = sent, False = configured but failed, None = disabled / not configured."""
    email_cfg = _CFG.get("alert_email", {})
    if not email_cfg.get("enabled"):
        logger.info("[alerts] Email alerts disabled in config.yaml. Skipping.")
        return None
    missing = [k for k in ("smtp_host", "from_addr", "to_addr") if not email_cfg.get(k)]
    if missing:
        logger.warning("[alerts] Email alerts enabled but missing: %s", ", ".join(missing))
        return False
    body = "\n".join(f"- {i['title']}\n  {i['url']}" for i in items)
    msg = MIMEText(body)
    msg["Subject"] = f"India Wireless Toolkit: {len(items)} new press release(s)"
    msg["From"] = email_cfg["from_addr"]
    msg["To"] = email_cfg["to_addr"]
    try:
        with smtplib.SMTP(email_cfg["smtp_host"], int(email_cfg.get("smtp_port", 587)), timeout=10) as server:
            server.starttls(context=ssl.create_default_context())
            if email_cfg.get("smtp_user"):
                server.login(email_cfg["smtp_user"], email_cfg.get("smtp_password", ""))
            server.send_message(msg)
        logger.info("[alerts] Email alert sent for %d item(s).", len(items))
        return True
    except Exception as e:
        logger.warning("[alerts] Email alert failed: %s", e)
        return False


def alert_new_items(items: list = None, *, limit: int = 50, include_sample: bool = False,
                    resend: bool = False, db_path: str = None) -> dict:
    """Alert on items that have not been alerted yet.

    items=None  -> the newest un-alerted archive records (or, with resend=True,
                   simply the newest records).
    items given -> those items (e.g. what store_items just inserted).
    Sample data is never alerted unless include_sample=True. Items are marked
    alerted only when every CONFIGURED channel delivered them."""
    skipped_sample = 0
    if items is None:
        # filter in SQL so sample rows can never crowd real items out of the `limit`
        items = query_archive_records(limit, db_path, include_sample=include_sample,
                                      only_unalerted=not resend)
    else:
        items = [dict(i, id=i.get("id") or record_id(i.get("title", ""), i.get("url", ""))) for i in items]
        if not include_sample:
            kept = [i for i in items if i.get("origin") != ORIGIN_SAMPLE]
            skipped_sample = len(items) - len(kept)
            items = kept
    result = {"items_considered": len(items), "skipped_sample_items": skipped_sample,
              "slack_sent": False, "email_sent": False, "marked_alerted": 0}
    if not items:
        return result

    slack, email = send_slack_alert(items), send_email_alert(items)
    result["slack_sent"], result["email_sent"] = slack is True, email is True
    configured = [r for r in (slack, email) if r is not None]
    if configured and all(configured) and not resend:
        result["marked_alerted"] = mark_alerted([i["id"] for i in items], db_path)
    return result


# --- Feature 5: Keyword statistics / trend ---------------------------------------------------------------

def keyword_counts(db_path: str = None, include_sample: bool = True) -> dict:
    counter = Counter()
    for rec in query_archive_records(1000, db_path, include_sample):
        counter.update(rec["keywords_matched"])
    return dict(counter)


def keyword_trend_series(db_path: str = None, include_sample: bool = True, top_n: int = 5) -> dict:
    """Monthly mention counts for the top keywords: {'months': [...], 'series': {kw: [counts]}}.
    Only records with a real or estimated ISO date are bucketed; each month is the
    record's `date` (publication date when known - see `date_source`)."""
    records = query_archive_records(1000, db_path, include_sample)
    totals, by_month = Counter(), {}
    for rec in records:
        month = (rec["date"] or "")[:7]
        if not re.match(r"^\d{4}-\d{2}$", month):
            continue
        for kw in rec["keywords_matched"]:
            totals[kw] += 1
            by_month.setdefault(kw, Counter())[month] += 1
    months = sorted({m for c in by_month.values() for m in c})
    top = [kw for kw, _ in totals.most_common(top_n)]
    return {"months": months, "series": {kw: [by_month[kw].get(m, 0) for m in months] for kw in top}}


def keyword_trend_report(db_path: str = None, out_path: str = None):
    out_path = out_path or resolve_path(_PATHS.get("charts_dir", "charts"), "keyword_trend.png")
    counter = Counter(keyword_counts(db_path))
    if not query_archive_records(1, db_path):
        logger.info("  No archived data yet. Run the scraper (or --dummy) first to populate the archive.")
        return None
    if not counter:
        logger.info("  No keyword hits recorded yet.")
        return None

    labels, counts = zip(*counter.most_common(10))
    trend = keyword_trend_series(db_path)

    fig = _plotting.new_figure((12, 5))
    ax1, ax2 = fig.subplots(1, 2)
    ax1.barh(labels, counts, color="#0ea5e9")
    ax1.set_xlabel("Mentions in archived press releases")
    ax1.set_title("Top keywords (all time)")
    ax1.invert_yaxis()
    if len(trend["months"]) >= 2:
        positions = list(range(len(trend["months"])))
        for kw, values in trend["series"].items():
            ax2.plot(positions, values, marker="o", label=kw)
        ax2.set_ylabel("Mentions per month")
        ax2.set_title("Monthly trend (top keywords)")
        ax2.set_xticks(positions)
        ax2.set_xticklabels(trend["months"], rotation=45, ha="right")
        ax2.legend(fontsize=8)
    else:
        ax2.text(0.5, 0.5, "Need press releases from at least\ntwo different months for a trend",
                 ha="center", va="center")
        ax2.set_axis_off()
    fig.suptitle("India Wireless/Telecom News Archive", fontsize=12)
    fig.tight_layout()
    _plotting.save_figure(fig, out_path)
    logger.info("  Keyword trend chart saved to %s", out_path)
    return dict(counter)


def main(argv=None):
    from .logging_config import setup_logging
    setup_logging(cli=True)
    parser = argparse.ArgumentParser(description="India telecom news scraper")
    parser.add_argument("--dummy", action="store_true",
                        help="Load the bundled sample dataset instead of live scraping")
    parser.add_argument("--rss", nargs="*", default=[], help="RSS feed URLs to scrape")
    parser.add_argument("--trend", action="store_true", help="Plot keyword trend from the archive")
    parser.add_argument("--alert", action="store_true", help="Send Slack/email alerts for new matches")
    parser.add_argument("--alert-sample", action="store_true",
                        help="Allow alerts for sample (non-real) items, e.g. to test Slack/SMTP wiring")
    args, _ = parser.parse_known_args(argv)

    if args.trend:
        if args.dummy or args.rss or args.alert:
            print("  Note: --trend plots the existing archive and ignores --dummy/--rss/--alert this run.")
        keyword_trend_report()
        return

    used_fallback = False
    if args.dummy:
        print(f"Loading sample dataset from {DUMMY_PATH} ...")
        items = load_dummy_dataset()
    elif args.rss:
        items = run_rss_scraper(args.rss)
    else:
        items, used_fallback = scrape_with_fallback()
        if used_fallback:
            print("\nNo live results (likely no internet access in this environment). "
                  "Loaded the bundled SAMPLE dataset instead - records are labelled origin='sample'.")

    new_items = store_items(items)
    print(f"\nStored {len(new_items)} new item(s) (of {len(items)} fetched) into {DB_PATH}.")

    if args.alert:
        outcome = alert_new_items(new_items, include_sample=args.alert_sample)
        if outcome["skipped_sample_items"]:
            print(f"  Skipped {outcome['skipped_sample_items']} sample item(s); "
                  "use --alert-sample to alert on them anyway.")
        print(f"  Alerts: slack_sent={outcome['slack_sent']} email_sent={outcome['email_sent']}")

    print(f"\nTotal matching wireless/telecom items this run: {len(items)}")
    print("Tip: run with --trend to visualize keyword frequency across the archive, "
          "or schedule this with cron / an Azure timer for daily monitoring.")


if __name__ == "__main__":
    main()
