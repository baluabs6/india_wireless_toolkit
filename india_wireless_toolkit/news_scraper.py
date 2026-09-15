"""
India Telecom News/Press-Release Scraper (v2)
------------------------------------------------
Features:
  1. Original TRAI/DoT scraper (needs live internet; not reachable from
     network-restricted sandboxes — see README).
  2. SQLite storage with dedup, so results persist and can be queried/trended.
  3. RSS feed support (feedparser) as an alternative to HTML scraping.
  4. Alert integration: Slack webhook and/or email when new matches appear.
  5. Keyword trend tracker: counts keyword mentions over time from the
     SQLite archive and plots a trend chart.
  6. Dummy dataset fallback (data/press_releases_sample.json) so the whole
     pipeline (storage, trend chart, alerts) can be exercised offline.

Usage:
    python -m india_wireless_toolkit.cli news             # live scrape (needs internet)
    python -m india_wireless_toolkit.cli news --dummy      # load bundled dummy dataset
    python -m india_wireless_toolkit.cli news --trend      # plot keyword trend from archive
"""

import os
import json
import time
import sqlite3
import hashlib
import argparse
import smtplib
from email.mime.text import MIMEText
from collections import Counter

import requests
from bs4 import BeautifulSoup
import matplotlib.pyplot as plt

from .config_loader import CONFIG

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


def _root_path(*parts):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, *parts)


DB_PATH = _root_path(_CFG.get("sqlite_db_path", "news_archive.db"))
DUMMY_PATH = _root_path(_CFG.get("dummy_data_path", "data/press_releases_sample.json"))


# --- Feature 2: SQLite storage ----------------------------------------------

def init_db(db_path: str = None):
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS press_releases (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            url TEXT,
            date TEXT,
            keywords_matched TEXT,
            fetched_at TEXT
        )
    """)
    conn.commit()
    return conn


def _record_id(title: str, url: str) -> str:
    return hashlib.md5(f"{title}|{url}".encode()).hexdigest()


def store_items(items: list, db_path: str = None):
    conn = init_db(db_path)
    new_count = 0
    for item in items:
        rid = _record_id(item["title"], item["url"])
        try:
            conn.execute(
                "INSERT INTO press_releases (id, source, title, url, date, keywords_matched, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rid, item.get("source", ""), item["title"], item["url"], item.get("date", ""),
                 json.dumps(item.get("keywords_matched", [])), time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            new_count += 1
        except sqlite3.IntegrityError:
            pass  # already stored, skip (dedup)
    conn.commit()
    conn.close()
    return new_count


def query_archive(limit: int = 50, db_path: str = None):
    conn = init_db(db_path)
    cur = conn.execute("SELECT source, title, url, date, keywords_matched FROM press_releases "
                        "ORDER BY date DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


# --- Feature 1: Live HTML scraper (needs real internet) ---------------------

def fetch_page(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def extract_links(soup: BeautifulSoup, base_url: str):
    items = []
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        href = a["href"]
        if not title or len(title) < 15:
            continue
        if href.startswith("/"):
            href = base_url.rstrip("/") + href
        items.append((title, href))
    return items


def filter_by_keywords(items, keywords):
    matched = []
    for title, href in items:
        lower_title = title.lower()
        hits = [kw for kw in keywords if kw in lower_title]
        if hits:
            matched.append((title, href, hits))
    return matched


def run_live_scraper():
    all_matches = []
    for source_name, url in SOURCES.items():
        print(f"\nFetching {source_name}: {url}")
        try:
            soup = fetch_page(url)
        except requests.RequestException as e:
            print(f"  Failed to fetch {source_name}: {e}")
            continue

        base = "/".join(url.split("/")[:3])
        items = extract_links(soup, base)
        matches = filter_by_keywords(items, KEYWORDS)
        print(f"  Found {len(items)} links total, {len(matches)} matched wireless-related keywords.")
        for title, href, hits in matches[:15]:
            all_matches.append({
                "source": source_name, "title": title, "url": href,
                "date": time.strftime("%Y-%m-%d"), "keywords_matched": hits,
            })
        time.sleep(1)
    return all_matches


# --- Feature 6: Dummy dataset fallback --------------------------------------

def load_dummy_dataset(path: str = None):
    path = path or DUMMY_PATH
    with open(path, "r") as f:
        return json.load(f)


# --- Feature 3: RSS feed support ---------------------------------------------

def run_rss_scraper(feed_urls: list):
    """Requires `feedparser`. Falls back gracefully if unavailable/unreachable."""
    try:
        import feedparser
    except ImportError:
        print("  feedparser not installed. Run: pip install feedparser")
        return []

    all_matches = []
    for feed_url in feed_urls:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"  Failed to parse feed {feed_url}: {e}")
            continue
        for entry in getattr(feed, "entries", []):
            title = entry.get("title", "")
            hits = [kw for kw in KEYWORDS if kw in title.lower()]
            if hits:
                all_matches.append({
                    "source": feed_url, "title": title, "url": entry.get("link", ""),
                    "date": entry.get("published", time.strftime("%Y-%m-%d")),
                    "keywords_matched": hits,
                })
    return all_matches


# --- Feature 4: Alerts (Slack webhook + email) ------------------------------

def send_slack_alert(items: list, webhook_url: str = None):
    webhook_url = webhook_url or _CFG.get("alert_webhook_url", "")
    if not webhook_url:
        print("  [alerts] No Slack webhook configured (config.yaml -> news_scraper.alert_webhook_url). Skipping.")
        return False
    text = "*New India wireless/telecom press releases:*\n" + "\n".join(
        f"- <{i['url']}|{i['title']}> ({i.get('source','')})" for i in items
    )
    try:
        resp = requests.post(webhook_url, json={"text": text}, timeout=10)
        resp.raise_for_status()
        print(f"  [alerts] Slack alert sent for {len(items)} item(s).")
        return True
    except Exception as e:
        print(f"  [alerts] Slack alert failed: {e}")
        return False


def send_email_alert(items: list):
    email_cfg = _CFG.get("alert_email", {})
    if not email_cfg.get("enabled"):
        print("  [alerts] Email alerts disabled in config.yaml. Skipping.")
        return False
    body = "\n".join(f"- {i['title']}\n  {i['url']}" for i in items)
    msg = MIMEText(body)
    msg["Subject"] = f"India Wireless Toolkit: {len(items)} new press release(s)"
    msg["From"] = email_cfg.get("from_addr", "")
    msg["To"] = email_cfg.get("to_addr", "")
    try:
        with smtplib.SMTP(email_cfg["smtp_host"], email_cfg.get("smtp_port", 587), timeout=10) as server:
            server.starttls()
            server.login(email_cfg["smtp_user"], email_cfg["smtp_password"])
            server.send_message(msg)
        print(f"  [alerts] Email alert sent for {len(items)} item(s).")
        return True
    except Exception as e:
        print(f"  [alerts] Email alert failed: {e}")
        return False


# --- Feature 5: Keyword trend tracker ---------------------------------------

def keyword_trend_report(db_path: str = None, out_path: str = None):
    _paths_cfg = CONFIG.get("paths", {})
    out_path = out_path or f"{_paths_cfg.get('charts_dir', 'charts')}/keyword_trend.png"
    rows = query_archive(limit=1000, db_path=db_path)

    if not rows:
        print("  No archived data yet. Run the scraper (or --dummy) first to populate the archive.")
        return None

    counter = Counter()
    for _, _, _, _, kw_json in rows:
        for kw in json.loads(kw_json):
            counter[kw] += 1

    if not counter:
        print("  No keyword hits recorded yet.")
        return None

    labels, counts = zip(*counter.most_common(10))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.barh(labels, counts, color="#0ea5e9")
    ax.set_xlabel("Mentions in archived press releases")
    ax.set_title("Top Keywords in India Wireless/Telecom News Archive")
    ax.invert_yaxis()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"  Keyword trend chart saved to {out_path}")
    return dict(counter)


def main():
    parser = argparse.ArgumentParser(description="India telecom news scraper")
    parser.add_argument("--dummy", action="store_true",
                         help="Load the bundled dummy dataset instead of live scraping")
    parser.add_argument("--rss", nargs="*", default=[], help="RSS feed URLs to scrape")
    parser.add_argument("--trend", action="store_true", help="Plot keyword trend from the archive")
    parser.add_argument("--alert", action="store_true", help="Send Slack/email alerts for new matches")
    args, _ = parser.parse_known_args()

    if args.trend:
        keyword_trend_report()
        return

    if args.dummy:
        print(f"Loading dummy dataset from {DUMMY_PATH} ...")
        items = load_dummy_dataset()
    elif args.rss:
        items = run_rss_scraper(args.rss)
    else:
        items = run_live_scraper()
        if not items:
            print("\nNo live results (likely no internet access in this environment). "
                  "Falling back to bundled dummy dataset so the rest of the pipeline still works.")
            items = load_dummy_dataset()

    new_count = store_items(items)
    print(f"\nStored {new_count} new item(s) (of {len(items)} fetched) into {DB_PATH}.")

    if args.alert and new_count > 0:
        new_items = items[:new_count]
        send_slack_alert(new_items)
        send_email_alert(new_items)

    print(f"\nTotal matching wireless/telecom items this run: {len(items)}")
    print("Tip: run with --trend to visualize keyword frequency across the archive, "
          "or schedule this with cron for daily monitoring.")


if __name__ == "__main__":
    main()
