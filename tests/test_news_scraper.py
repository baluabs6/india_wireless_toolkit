import json
import sqlite3

import pytest
from bs4 import BeautifulSoup

from india_wireless_toolkit import news_scraper as n


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "archive.db")


def _item(title, url, **kw):
    return {"source": "TRAI", "title": title, "url": url, "date": "2026-05-01",
            "keywords_matched": ["spectrum"], **kw}


# --- normalisation -----------------------------------------------------------------

def test_canonicalize_url_collapses_equivalent_urls():
    a = n.canonicalize_url("http://www.TRAI.gov.in/notifications/press-release/x/?utm_source=mail&b=2&a=1#top")
    b = n.canonicalize_url("https://trai.gov.in/notifications//press-release/x?a=1&b=2")
    assert a == b == "https://trai.gov.in/notifications/press-release/x?a=1&b=2"
    assert n.canonicalize_url("https://pib.gov.in/x?PRID=5") != n.canonicalize_url("https://pib.gov.in/x?PRID=6")
    assert n.canonicalize_url("") == ""


def test_record_id_ignores_tracking_params_and_title_case():
    assert n.record_id("5G  Rollout", "https://a.in/p?utm_campaign=z") == n.record_id("5g rollout", "http://a.in/p")
    assert n.record_id("5G rollout", "https://a.in/p") != n.record_id("6G rollout", "https://a.in/p")


@pytest.mark.parametrize("raw,expected", [
    ("2026-06-03", "2026-06-03"), ("03 June 2026", "2026-06-03"), ("3rd Jun 2026", "2026-06-03"),
    ("Tue, 03 Jun 2026 10:00:00 +0530", "2026-06-03"), ("12/06/2026", "2026-06-12"),
    ("June 5, 2026", "2026-06-05"), ("2026-02-30", None), ("garbage", None), ("", None), (None, None),
])
def test_normalize_date(raw, expected):
    assert n.normalize_date(raw) == expected


def test_extract_date_from_listing_text():
    assert n.extract_date_from_text("TRAI releases paper | Date: 15-03-2026 | PDF") == "2026-03-15"
    assert n.extract_date_from_text("no date here") is None


# --- keyword matching ---------------------------------------------------------------

def test_keywords_match_whole_words_only():
    kws = ["row", "5g", "wi-fi", "tower", "satellite", "6 ghz"]
    assert n.find_keywords("Growth in arrow towers and rowing", kws) == ["tower"]   # plural ok, substrings not
    assert n.find_keywords("Right of Way (ROW) rules for 5G", kws) == ["row", "5g"]
    assert n.find_keywords("Wifi and Wi-Fi 6GHz and 6 GHz", kws) == ["wi-fi", "6 ghz"]
    assert n.find_keywords("Satellites for 15G", kws) == ["satellite"]
    assert n.find_keywords("the row of houses", kws) == []                          # lowercase 'row' is not ROW


# --- link extraction ---------------------------------------------------------------------

def test_extract_links_resolves_relative_urls_and_dates():
    html = """
    <table>
      <tr><td>12/03/2026</td><td><a href="/files/a.pdf">TRAI releases consultation on spectrum</a></td></tr>
      <tr><td><a href="detail.aspx?id=3">DoT notifies new 6 GHz Wi-Fi rules today</a></td></tr>
      <tr><td><a href="//cdn.example.org/b">Protocol relative link about satellite</a></td></tr>
      <tr><td><a href="javascript:void(0)">javascript link that must be skipped</a></td></tr>
      <tr><td><a href="#top">Anchor link that must be skipped too</a></td></tr>
      <tr><td><a href="/short">short</a></td></tr>
    </table>"""
    items = n.extract_links(BeautifulSoup(html, "html.parser"), "https://trai.gov.in/notifications/press-release")
    urls = {u: d for _, u, d in items}
    assert urls["https://trai.gov.in/files/a.pdf"] == "2026-03-12"
    assert "https://trai.gov.in/notifications/detail.aspx?id=3" in urls
    assert "https://cdn.example.org/b" in urls
    assert len(items) == 3


# --- storage / dedup / provenance ----------------------------------------------------------------

def test_store_dedups_and_labels_origin(db):
    first = n.store_items([_item("Spectrum paper", "https://a.in/x?utm_source=1", origin="live")], db_path=db)
    assert len(first) == 1 and first[0]["origin"] == "live" and first[0]["id"]
    again = n.store_items([_item("spectrum  paper", "http://www.a.in/x/")], db_path=db)
    assert again == []
    rec = n.query_archive_records(db_path=db)[0]
    assert rec["origin"] == "live" and rec["date_source"] == "published" and rec["alerted_at"] is None


def test_missing_dates_are_marked_as_fetch_dates(db):
    stored = n.store_items([{"source": "S", "title": "No date item", "url": "https://a.in/n",
                             "date": None, "keywords_matched": []}], db_path=db)
    assert stored[0]["date_source"] == "fetched" and len(stored[0]["date"]) == 10


def test_items_without_title_are_skipped(db):
    assert n.store_items([{"url": "https://a.in/x"}, _item("ok", "https://a.in/ok")], db_path=db)[0]["title"] == "ok"
    assert len(n.query_archive_records(db_path=db)) == 1


def test_sample_dataset_is_labelled_and_filterable(db):
    items = n.load_dummy_dataset()
    assert items and all(i["origin"] == "sample" for i in items)
    n.store_items(items, db_path=db)
    n.store_items([_item("Real one", "https://real.example/r", origin="live")], db_path=db)
    assert n.archive_origin_summary(db)["sample"] == len(items)
    only_real = n.query_archive_records(50, db, include_sample=False)
    assert [r["title"] for r in only_real] == ["Real one"]
    assert len(n.query_archive(50, db, include_sample=False)) == 1        # legacy tuple view


def test_clean_limit_blocks_unbounded_and_negative_limits():
    assert n.clean_limit(-1) == 1 and n.clean_limit("abc") == 50 and n.clean_limit(10**9) == 500
    assert n.clean_limit("7") == 7


def test_archive_is_ordered_by_iso_date(db):
    n.store_items([_item("Old", "https://a.in/1", date="2026-01-05"),
                   _item("New", "https://a.in/2", date="Tue, 03 Jun 2026 10:00:00 +0530"),
                   _item("Mid", "https://a.in/3", date="12/03/2026")], db_path=db)
    assert [r["title"] for r in n.query_archive_records(db_path=db)] == ["New", "Mid", "Old"]


# --- migration of a v2.0 archive -----------------------------------------------------------------

def test_old_archive_is_migrated_in_place(db):
    sample = n.load_dummy_dataset()[0]
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE press_releases (id TEXT PRIMARY KEY, source TEXT, title TEXT, url TEXT,
                    date TEXT, keywords_matched TEXT, fetched_at TEXT)""")
    old_rows = [("md5a", "TRAI", sample["title"], sample["url"], sample["date"], json.dumps(["spectrum"]), "2026-01-01 00:00:00"),
                ("md5b", "TRAI", "Real thing", "https://real.example/a?utm_source=x", "2026-04-01", "[]", "2026-04-02 00:00:00"),
                ("md5c", "TRAI", "Real thing", "https://real.example/a", "2026-04-01", "[]", "2026-04-03 00:00:00")]
    conn.executemany("INSERT INTO press_releases VALUES (?,?,?,?,?,?,?)", old_rows)
    conn.commit(); conn.close()

    records = n.query_archive_records(db_path=db)
    assert len(records) == 2                                  # the near-duplicate pair collapsed
    origins = {r["title"]: r["origin"] for r in records}
    assert origins[sample["title"]] == "sample" and origins["Real thing"] == "unknown"
    assert n.store_items([sample], db_path=db) == []          # re-loading the sample adds nothing
    assert n.query_archive_records(db_path=db)                # idempotent second open
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == n.SCHEMA_VERSION


# --- alerts --------------------------------------------------------------------------------------------------

def _stub_channels(monkeypatch, slack, email):
    sent = []
    monkeypatch.setattr(n, "send_slack_alert", lambda items: (sent.append(("slack", [i["title"] for i in items])) or slack))
    monkeypatch.setattr(n, "send_email_alert", lambda items: (sent.append(("email", len(items))) or email))
    return sent


def test_alerts_are_sent_once_and_never_for_sample_data(db, monkeypatch):
    n.store_items(n.load_dummy_dataset(), db_path=db)
    n.store_items([_item("Real A", "https://r.example/a", origin="live"),
                   _item("Real B", "https://r.example/b", origin="live")], db_path=db)
    sent = _stub_channels(monkeypatch, slack=True, email=None)

    first = n.alert_new_items(db_path=db)
    assert first["items_considered"] == 2 and first["slack_sent"] and first["marked_alerted"] == 2
    assert sorted(sent[0][1]) == ["Real A", "Real B"]
    second = n.alert_new_items(db_path=db)
    assert second["items_considered"] == 0
    assert [c for c, _ in sent].count("slack") == 1                       # nothing re-sent

    resend = n.alert_new_items(db_path=db, resend=True)
    assert resend["items_considered"] == 2 and resend["marked_alerted"] == 0


def test_sample_rows_cannot_crowd_real_items_out_of_the_alert_limit(db, monkeypatch):
    n.store_items(n.load_dummy_dataset(), db_path=db)                      # newest dates are sample rows
    n.store_items([_item("Old real", "https://r.example/old", date="2025-01-01", origin="live")], db_path=db)
    _stub_channels(monkeypatch, slack=True, email=None)
    out = n.alert_new_items(db_path=db, limit=3)
    assert out["items_considered"] == 1 and out["marked_alerted"] == 1


def test_items_stay_unalerted_when_a_configured_channel_fails(db, monkeypatch):
    n.store_items([_item("Real A", "https://r.example/a", origin="live")], db_path=db)
    _stub_channels(monkeypatch, slack=True, email=False)                  # email configured but failing
    assert n.alert_new_items(db_path=db)["marked_alerted"] == 0
    _stub_channels(monkeypatch, slack=None, email=None)                   # nothing configured at all
    assert n.alert_new_items(db_path=db)["marked_alerted"] == 0
    assert len(n.query_archive_records(db_path=db, only_unalerted=True)) == 1


def test_alert_on_explicit_items_with_sample_opt_in(db, monkeypatch):
    stored = n.store_items(n.load_dummy_dataset()[:2], db_path=db)
    _stub_channels(monkeypatch, slack=True, email=None)
    assert n.alert_new_items(stored)["items_considered"] == 0
    out = n.alert_new_items(stored, include_sample=True, db_path=db)
    assert out["items_considered"] == 2 and out["marked_alerted"] == 2


def test_slack_messages_are_escaped_and_batched(monkeypatch):
    posts = []

    class Resp:
        def raise_for_status(self): pass
    monkeypatch.setattr(n.requests, "post", lambda url, json=None, timeout=None: (posts.append(json["text"]) or Resp()))
    items = [{"title": f"A <b>&</b> {i}", "url": f"https://x.example/{i}|evil", "source": "T"} for i in range(45)]
    assert n.send_slack_alert(items, webhook_url="https://hooks.example/abc") is True
    assert len(posts) == 3                                                 # 20 + 20 + 5
    assert "&lt;b&gt;&amp;" in posts[0] and "%7Cevil" in posts[0] and "<b>" not in posts[0]
    assert n.send_slack_alert(items, webhook_url="http://insecure.example/x") is False
    assert n.send_slack_alert(items, webhook_url="") is None


# --- trend ---------------------------------------------------------------------------------------------------------

def test_keyword_counts_and_monthly_series(db):
    n.store_items([_item("a one", "https://a.in/1", date="2026-01-10", keywords_matched=["5g", "spectrum"]),
                   _item("b two", "https://a.in/2", date="2026-02-11", keywords_matched=["5g"]),
                   _item("c three", "https://a.in/3", date="2026-02-20", keywords_matched=["5g"])], db_path=db)
    assert n.keyword_counts(db) == {"5g": 3, "spectrum": 1}
    series = n.keyword_trend_series(db)
    assert series["months"] == ["2026-01", "2026-02"] and series["series"]["5g"] == [1, 2]


def test_trend_chart_is_written(db, tmp_path):
    n.store_items(n.load_dummy_dataset(), db_path=db)
    out = tmp_path / "trend.png"
    counts = n.keyword_trend_report(db_path=db, out_path=str(out))
    assert counts and out.read_bytes()[:4] == b"\x89PNG"
    assert n.keyword_trend_report(db_path=str(tmp_path / "empty.db"), out_path=str(tmp_path / "x.png")) is None
