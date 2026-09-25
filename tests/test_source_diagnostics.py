from datetime import datetime, timezone
from oiltech_digest.ingestion import source_diagnostics
from oiltech_digest.ingestion.source_diagnostics import ProbeResult


LISTING_HTML = b"""
<html>
  <body>
    <a href="/news/2026/06/drilling-automation-platform">
      Drilling automation platform expands well service efficiency
    </a>
  </body>
</html>
"""

ARTICLE_HTML = b"""
<html>
  <head>
    <meta property="og:title" content="Drilling automation platform expands well service efficiency">
    <meta property="article:published_time" content="2026-06-03T10:00:00Z">
  </head>
  <body>
    <article>
      <p>The company deployed drilling automation software for oilfield service crews.</p>
      <p>The platform improves well construction, equipment uptime, and production operations.</p>
      <p>Extra industrial context keeps the extracted article body long enough for insertion diagnostics.</p>
    </article>
  </body>
</html>
"""

TELEGRAM_HTML = """
<div class="tgme_widget_message" data-post="oiltechnews/42">
  <div class="tgme_widget_message_text js-message_text">
    Новая система автоматизации бурения внедрена на месторождении.
  </div>
  <a class="tgme_widget_message_date">
    <time datetime="2026-06-03T10:15:00+00:00"></time>
  </a>
</div>
"""

RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>Automation news</title><link>https://example.com/a</link></item>
</channel></rss>"""


def test_diagnose_request_source_reports_insertable_candidate(monkeypatch):
    def fake_probe(url, timeout=20):
        if url == "https://example.com/news":
            return ProbeResult(url=url, status=200, bytes=len(LISTING_HTML)), LISTING_HTML
        if url == "https://example.com/news/2026/06/drilling-automation-platform":
            return ProbeResult(url=url, status=200, bytes=len(ARTICLE_HTML)), ARTICLE_HTML
        raise AssertionError(url)

    monkeypatch.setattr(source_diagnostics, "probe_url", fake_probe)

    result = source_diagnostics.diagnose_source(
        {"id": 7, "name": "Example", "parse_strategy": "request", "listing_url": "https://example.com/news"},
        limit=3,
    )

    assert result["verdict"] == "ok"
    assert result["candidate_count"] == 1
    assert result["article_checks"][0]["verdict"] == "ok"
    assert result["article_checks"][0]["prefilter_keep"] is True


def test_diagnose_request_source_reports_no_candidates(monkeypatch):
    monkeypatch.setattr(
        source_diagnostics,
        "probe_url",
        lambda url, timeout=20: (ProbeResult(url=url, status=200, bytes=13), b"<html></html>"),
    )

    result = source_diagnostics.diagnose_source(
        {"id": 8, "name": "Empty", "parse_strategy": "request", "url": "https://example.com"},
    )

    assert result["verdict"] == "no_candidates"
    assert result["candidate_count"] == 0


def test_diagnose_telegram_source_reports_posts(monkeypatch):
    monkeypatch.setattr(
        source_diagnostics,
        "probe_url",
        lambda url, timeout=20: (ProbeResult(url=url, status=200, bytes=len(TELEGRAM_HTML)), TELEGRAM_HTML),
    )

    result = source_diagnostics.diagnose_source(
        {"id": 15, "name": "TG", "parse_strategy": "telegram", "url": "https://t.me/oiltechnews"},
    )

    assert result["verdict"] == "ok"
    assert result["post_count"] == 1
    assert result["posts"][0]["url"] == "https://t.me/oiltechnews/42"


def test_diagnose_rss_source_reports_entries(monkeypatch):
    monkeypatch.setattr(
        source_diagnostics,
        "probe_url",
        lambda url, timeout=20: (ProbeResult(url=url, status=200, bytes=len(RSS_XML)), RSS_XML),
    )

    result = source_diagnostics.diagnose_source(
        {"id": 3, "name": "RSS", "parse_strategy": "rss", "rss_url": "https://example.com/feed.xml"},
    )

    assert result["verdict"] == "ok"
    assert result["entry_count"] == 1
    assert result["entries"][0]["title"] == "Automation news"


def test_diagnose_playwright_source_reports_insertable_candidate(monkeypatch):
    rendered = {
        "https://example.com/news": LISTING_HTML,
        "https://example.com/news/2026/06/drilling-automation-platform": ARTICLE_HTML,
    }

    monkeypatch.setattr(source_diagnostics.playwright_parser, "is_available", lambda: True)
    monkeypatch.setattr(source_diagnostics.playwright_parser, "fetch_rendered", lambda url, **_: rendered.get(url))

    result = source_diagnostics.diagnose_source(
        {"id": 11, "name": "Rendered", "parse_strategy": "playwright", "listing_url": "https://example.com/news"},
        limit=3,
    )

    assert result["verdict"] == "ok"
    assert result["strategy"] == "playwright"
    assert result["candidate_count"] == 1
    assert result["article_checks"][0]["article_probe"]["status"] == "rendered"


def _probe_with(monkeypatch, *, feed=None, rss=None, request=None, browser=None):
    monkeypatch.setattr("oiltech_digest.ingestion.rss_discovery.discover_feed", lambda url: feed)
    monkeypatch.setattr(source_diagnostics, "diagnose_rss_source", lambda s, limit=2: rss)
    monkeypatch.setattr(source_diagnostics, "diagnose_request_source", lambda s, limit=2: request)
    monkeypatch.setattr(source_diagnostics, "diagnose_playwright_source", lambda s, limit=2: browser)
    return source_diagnostics.probe_strategies("https://site.example/news")


def _report(status, texts):
    return {"verdict": "ok" if texts else "no_candidates", "listing_probe": {"status": status},
            "candidate_count": len(texts), "candidates": [{"title": f"t{i}"} for i in range(len(texts))],
            "article_checks": [{"text_chars": n} for n in texts]}


def test_probe_takes_fresh_rss(monkeypatch):
    fresh = datetime.now(timezone.utc).isoformat()
    result = _probe_with(monkeypatch, feed="https://site.example/rss",
                         rss={"verdict": "ok", "entry_count": 5, "latest_entry": fresh})
    assert result["chosen"] == {"parse_strategy": "rss", "rss_url": "https://site.example/rss", "network_region": "auto"}


def test_probe_skips_dead_rss_and_tries_request(monkeypatch):
    """PETRONAS: лента отдаёт пункты, но последний — от мая 2025."""
    result = _probe_with(monkeypatch, feed="https://site.example/rss",
                         rss={"verdict": "ok", "entry_count": 5, "latest_entry": "2025-05-07T00:00:00+00:00"},
                         request=_report(200, [800, 900]))
    assert result["chosen"]["parse_strategy"] == "request"
    assert result["chosen"]["listing_url"] == "https://site.example/news"
    assert [a["strategy"] for a in result["attempts"]] == ["rss", "request"]


def test_probe_falls_to_browser_when_request_gives_no_text(monkeypatch):
    result = _probe_with(monkeypatch, request=_report(200, []), browser=_report("rendered", [1500]))
    assert result["chosen"]["parse_strategy"] == "playwright"


def test_probe_routes_abroad_when_blocked_from_ru_core(monkeypatch):
    """QatarEnergy: с российских адресов 403 — сбор должен идти через NL-воркер."""
    result = _probe_with(monkeypatch, request=_report(403, []), browser=_report("ERR", []))
    assert result["chosen"]["network_region"] == "external"


def test_probe_chooses_nothing_when_site_opens_but_has_no_articles(monkeypatch):
    result = _probe_with(monkeypatch, request=_report(200, []), browser=_report("rendered", []))
    assert result["chosen"] is None
