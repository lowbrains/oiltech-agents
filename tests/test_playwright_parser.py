from oiltech_digest.ingestion import playwright_parser


LISTING_HTML = b"""
<html>
  <body>
    <a href="/news/2026/06/js-rendered-drilling-automation">
      JS rendered drilling automation platform improves oilfield operations
    </a>
  </body>
</html>
"""

ARTICLE_HTML = b"""
<html>
  <head>
    <meta property="og:title" content="JS rendered drilling automation platform improves oilfield operations">
    <meta property="article:published_time" content="2026-06-05T08:30:00Z">
  </head>
  <body>
    <article>
      <p>The company deployed a drilling automation platform across oilfield service crews.</p>
      <p>The system improves well construction, equipment uptime, and production operations.</p>
      <p>Additional industrial context keeps this article above teaser length for downstream AI stages.</p>
    </article>
  </body>
</html>
"""


def test_parse_source_renders_listing_and_article_pages(monkeypatch):
    rendered_urls = []
    inserted = []
    state = {}

    def fake_fetch_rendered(url, **kwargs):
        rendered_urls.append(url)
        if url == "https://example.com/news":
            return LISTING_HTML
        if url == "https://example.com/news/2026/06/js-rendered-drilling-automation":
            return ARTICLE_HTML
        return None

    monkeypatch.setattr(playwright_parser, "is_available", lambda: True)
    monkeypatch.setattr(playwright_parser, "fetch_rendered", fake_fetch_rendered)
    monkeypatch.setattr(playwright_parser.repository, "insert_article", lambda article: inserted.append(article) or True)
    monkeypatch.setattr(playwright_parser.repository, "article_exists", lambda url: False)
    monkeypatch.setattr(playwright_parser.repository, "touch_last_parsed", lambda source_id: state.setdefault("touched", source_id))
    monkeypatch.setattr(
        playwright_parser.repository,
        "update_source_request_state",
        lambda source_id, **kwargs: state.update({"source_id": source_id, **kwargs}),
    )

    stats = playwright_parser.parse_source(
        {
            "id": 17,
            "name": "Rendered Example",
            "parse_strategy": "playwright",
            "listing_url": "https://example.com/news",
            "category": "международные",
        },
        article_limit=5,
    )

    assert stats["added"] == 1
    assert rendered_urls == [
        "https://example.com/news",
        "https://example.com/news/2026/06/js-rendered-drilling-automation",
    ]
    assert inserted[0]["url"] == "https://example.com/news/2026/06/js-rendered-drilling-automation"
    assert inserted[0]["language"] == "en"
    assert state["source_id"] == 17
    assert state["last_seen_article_url"] == "https://example.com/news/2026/06/js-rendered-drilling-automation"


def test_parse_source_reports_unavailable_without_rendering(monkeypatch):
    monkeypatch.setattr(playwright_parser, "is_available", lambda: False)
    monkeypatch.setattr(
        playwright_parser,
        "fetch_rendered",
        lambda url: (_ for _ in ()).throw(AssertionError("fetch_rendered should not be called")),
    )

    stats = playwright_parser.parse_source({"id": 18, "name": "No browser", "listing_url": "https://example.com/news"})

    assert stats["added"] == 0
    assert stats["attempted"] == 0


def test_playwright_proxy_for_only_overridden_hosts(monkeypatch):
    """Через прокси идут только хосты из PROXY_HOST_OVERRIDES (точечно для Hard-WAF);
    остальные playwright-источники — напрямую. PROXY_URL парсится в playwright-формат."""
    from oiltech_digest.ingestion import http_client

    monkeypatch.setattr(
        http_client,
        "_proxy_for",
        lambda host: (
            {"http": "http://u:p@eu.proxy.2captcha.com:2334",
             "https": "http://u:p@eu.proxy.2captcha.com:2334"}
            if host == "www.energyvoice.com" else None
        ),
    )

    assert playwright_parser._playwright_proxy_for("https://www.energyvoice.com/news") == {
        "server": "http://eu.proxy.2captcha.com:2334",
        "username": "u",
        "password": "p",
    }
    assert playwright_parser._playwright_proxy_for("https://www.slb.com/news-and-insights") is None


def test_listing_gets_a_second_longer_attempt_when_first_is_empty(monkeypatch):
    """«Пока не получится — пара попыток»: у ядра повтор был, а NL-воркер его не
    унаследовал. Теперь оба зовут одну функцию."""
    settles: list[int] = []

    def fake_render(url, settle_ms=0, **_):
        settles.append(settle_ms)
        return LISTING_HTML if len(settles) == 2 else b"<html><body>loading...</body></html>"
    monkeypatch.setattr(playwright_parser, "fetch_rendered", fake_render)

    candidates = playwright_parser.render_listing_candidates({"name": "S"}, "https://example.com/news", limit=5)

    assert settles == list(playwright_parser.LISTING_SETTLE_MS)
    assert settles[1] > settles[0], "вторая попытка ждёт дольше"
    assert len(candidates) == 1


def test_article_gets_a_second_longer_attempt_when_text_is_short(monkeypatch):
    settles: list[int] = []

    def fake_render(url, settle_ms=0, **_):
        settles.append(settle_ms)
        return ARTICLE_HTML if len(settles) == 2 else b"<html><head><title>x</title></head><body>...</body></html>"
    monkeypatch.setattr(playwright_parser, "fetch_rendered", fake_render)
    from oiltech_digest.ingestion.request_parser import CandidateLink

    record = playwright_parser.rendered_article(
        CandidateLink("https://example.com/news/a", "JS rendered drilling automation platform", 5), {"id": 3})

    assert settles == list(playwright_parser.ARTICLE_SETTLE_MS)
    assert record is not None and record["source_id"] == 3


def test_blocked_article_is_not_retried(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(playwright_parser, "fetch_rendered", lambda url, settle_ms=0, **_: calls.append(1))
    from oiltech_digest.ingestion.request_parser import CandidateLink

    assert playwright_parser.rendered_article(CandidateLink("https://e.com/a", "t" * 30, 5), {"id": 1}) is None
    assert len(calls) == 1, "блок (403/429/503) ожиданием не лечится"
