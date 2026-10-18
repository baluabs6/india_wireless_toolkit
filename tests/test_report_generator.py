import os

from india_wireless_toolkit import data_visualization as dv
from india_wireless_toolkit import news_scraper as n
from india_wireless_toolkit import report_generator as rg


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(n, "DB_PATH", str(tmp_path / "archive.db"))
    monkeypatch.setattr(dv, "OUT_DIR", str(tmp_path / "charts"))
    monkeypatch.setattr(rg, "CHARTS_DIR", str(tmp_path / "charts"))
    monkeypatch.setattr(rg, "REPORTS_DIR", str(tmp_path / "reports"))


def test_scraped_content_cannot_inject_html_or_script_urls(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    n.store_items([
        {"source": "<img src=x onerror=alert(1)>", "title": "<script>alert('xss')</script> spectrum",
         "url": "javascript:alert(document.cookie)", "date": "2026-05-01", "origin": "live"},
        {"source": "TRAI", "title": "Fine item", "url": "https://trai.gov.in/a?x=1&y='\"", "date": "2026-05-02",
         "origin": "live"},
    ])
    page = open(rg.generate_report(str(tmp_path / "reports" / "r.html")), encoding="utf-8").read()
    assert "<script" not in page and "onerror=alert" not in page.replace("&lt;img src=x onerror=alert(1)&gt;", "")
    assert "&lt;script&gt;" in page
    assert "href='#'" in page and "javascript:" not in page.split("<a href=")[1].split(">")[0]
    assert "&#x27;" in page or "&quot;" in page          # quotes in URLs escaped inside the attribute


def test_report_flags_sample_rows_and_reports_consistent_economics(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    n.store_items(n.load_dummy_dataset())
    path = rg.generate_report(str(tmp_path / "reports" / "r.html"))
    page = open(path, encoding="utf-8").read()
    assert "Sample data" in page and "<td>sample</td>" in page
    assert "NPV" in page and "Break-even" in page and "SINR vs distance" in page
    assert os.path.getsize(path) > 10_000
