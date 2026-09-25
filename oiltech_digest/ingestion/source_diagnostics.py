"""Read-only source diagnostics for CLI/API troubleshooting."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import feedparser
import requests

from oiltech_digest.config import REQUEST_TIMEOUT
from oiltech_digest.ingestion import playwright_parser, request_parser, telegram_parser
from oiltech_digest.ingestion import http_client
from oiltech_digest.ingestion.http_client import _DEFAULT_HEADERS, _mask_proxy, _proxy_for
from oiltech_digest.ingestion.relevance_filter import should_keep_article


@dataclass(frozen=True)
class ProbeResult:
    url: str
    status: int | str
    bytes: int = 0
    seconds: float | None = None
    error: str | None = None
    proxy: str | None = None


def diagnose_source(source: dict, limit: int = 5) -> dict:
    """Return a read-only diagnostic snapshot for one source."""
    strategy = source.get("parse_strategy") or ""
    if strategy == "request":
        return diagnose_request_source(source, limit=limit)
    if strategy == "telegram":
        return diagnose_telegram_source(source, limit=limit)
    if strategy == "playwright":
        return diagnose_playwright_source(source, limit=limit)
    if strategy == "rss":
        return diagnose_rss_source(source, limit=limit)
    return {
        "source_id": source.get("id"),
        "source_name": source.get("name"),
        "strategy": strategy or None,
        "verdict": "unsupported_strategy",
    }


def diagnose_request_source(source: dict, limit: int = 5) -> dict:
    listing_url = source.get("listing_url") or source.get("url")
    base = _base_payload(source, "request", listing_url)
    if not listing_url:
        return {**base, "verdict": "missing_listing_url"}

    probe, content = probe_url(listing_url)
    payload = {**base, "listing_probe": asdict(probe)}
    if content is None:
        return {**payload, "verdict": "listing_fetch_failed", "candidates": []}

    candidates = request_parser.extract_candidate_links(source, listing_url, content, limit=limit)
    payload["candidate_count"] = len(candidates)
    payload["candidates"] = [
        {
            "url": item.url,
            "title": item.title,
            "score": item.score,
            "published_at": item.published_at,
        }
        for item in candidates
    ]
    if not candidates:
        return {**payload, "verdict": "no_candidates"}

    article_checks = []
    for candidate in candidates[:limit]:
        article_probe, article_content = probe_url(candidate.url)
        check = {"candidate_url": candidate.url, "article_probe": asdict(article_probe)}
        if article_content is None:
            check["verdict"] = "article_fetch_failed"
            article_checks.append(check)
            continue

        title, published_at, raw_text = request_parser.parse_article_page(article_content, candidate.title)
        pre_filter = should_keep_article(title, raw_text, source)
        check.update(
            {
                "verdict": "ok" if len(raw_text) >= 200 and pre_filter.keep else "article_not_insertable",
                "title": title,
                "published_at": published_at,
                "text_chars": len(raw_text),
                "prefilter_keep": pre_filter.keep,
                "prefilter_noise": pre_filter.matched_noise[:5],
                "prefilter_keywords": pre_filter.matched_keywords[:5],
            }
        )
        article_checks.append(check)

    return {
        **payload,
        "article_checks": article_checks,
        "verdict": "ok" if any(item.get("verdict") == "ok" for item in article_checks) else "no_insertable_articles",
    }


def diagnose_playwright_source(source: dict, limit: int = 5) -> dict:
    listing_url = source.get("listing_url") or source.get("url")
    base = _base_payload(source, "playwright", listing_url)
    if not listing_url:
        return {**base, "verdict": "missing_listing_url"}
    if not playwright_parser.is_available():
        return {**base, "verdict": "playwright_unavailable", "candidates": []}

    # Та же функция, что у парсера: с парой попыток. Прежде диагностика рендерила один
    # раз с ожиданием по умолчанию и видела сайт иначе, чем сбор.
    candidates = playwright_parser.render_listing_candidates(source, listing_url, limit=limit)
    payload = {**base, "listing_probe": {"url": listing_url, "status": "rendered" if candidates else "ERR"}}
    if not candidates:
        return {**payload, "verdict": "no_candidates", "candidates": []}
    payload["candidate_count"] = len(candidates)
    payload["candidates"] = [
        {
            "url": item.url,
            "title": item.title,
            "score": item.score,
            "published_at": item.published_at,
        }
        for item in candidates
    ]
    if not candidates:
        return {**payload, "verdict": "no_candidates"}

    article_checks = []
    for candidate in candidates[:limit]:
        record = playwright_parser.rendered_article(candidate, {**source, "id": source.get("id") or 0})
        check = {"candidate_url": candidate.url,
                 "article_probe": {"url": candidate.url, "status": "rendered" if record else "ERR"}}
        if record is None:
            check["verdict"] = "article_render_failed"
            article_checks.append(check)
            continue

        title, published_at, raw_text = record["title"], record["published_at"], record["raw_text"]
        pre_filter = should_keep_article(title, raw_text, source)
        check.update(
            {
                "verdict": "ok" if len(raw_text) >= 200 and pre_filter.keep else "article_not_insertable",
                "title": title,
                "published_at": published_at,
                "text_chars": len(raw_text),
                "prefilter_keep": pre_filter.keep,
                "prefilter_noise": pre_filter.matched_noise[:5],
                "prefilter_keywords": pre_filter.matched_keywords[:5],
            }
        )
        article_checks.append(check)

    return {
        **payload,
        "article_checks": article_checks,
        "verdict": "ok" if any(item.get("verdict") == "ok" for item in article_checks) else "no_insertable_articles",
    }


def diagnose_telegram_source(source: dict, limit: int = 5) -> dict:
    preview_url = telegram_parser.preview_url_for_source(source)
    base = _base_payload(source, "telegram", preview_url)
    if not preview_url:
        return {**base, "verdict": "missing_or_invalid_channel_url", "posts": []}

    probe, content = probe_url(preview_url)
    payload = {**base, "preview_probe": asdict(probe)}
    if content is None:
        return {**payload, "verdict": "preview_fetch_failed", "posts": []}

    posts = telegram_parser.extract_posts(content, limit=limit)
    return {
        **payload,
        "post_count": len(posts),
        "posts": [
            {
                "url": post.url,
                "title": post.title,
                "published_at": post.published_at,
                "text_chars": len(post.text),
            }
            for post in posts
        ],
        "verdict": "ok" if posts else "no_posts",
    }


def diagnose_rss_source(source: dict, limit: int = 5) -> dict:
    rss_url = source.get("rss_url")
    base = _base_payload(source, "rss", rss_url)
    if not rss_url:
        return {**base, "verdict": "missing_rss_url", "entries": []}

    probe, content = probe_url(rss_url)
    payload = {**base, "rss_probe": asdict(probe)}
    if content is None:
        return {**payload, "verdict": "rss_fetch_failed", "entries": []}

    feed = feedparser.parse(content)
    entries = []
    for entry in feed.entries[:limit]:
        entries.append({"title": entry.get("title", ""), "url": entry.get("link", "")})
    # Дата свежайшего пункта: ленты умирают молча. У PETRONAS фид отдаёт пункты, но
    # последний — от 07.05.2025, при живом сайте с релизами каждые 2–3 дня.
    stamps = [e.get("published_parsed") or e.get("updated_parsed") for e in feed.entries]
    stamps = [datetime(*s[:6], tzinfo=timezone.utc) for s in stamps if s]
    return {
        **payload,
        "entry_count": len(feed.entries),
        "entries": entries,
        "latest_entry": max(stamps).isoformat() if stamps else None,
        "verdict": "ok" if feed.entries else "no_entries",
    }


# Лента, чей свежайший пункт старше этого, считается мёртвой.
FEED_STALE_DAYS = 90
# Какие коды ответа означают «с этого адреса не пускают», а не «страницы нет».
_BLOCK_STATUSES = {401, 403, 429, 503, "ERR"}


def probe_strategies(url: str, limit: int = 2) -> dict:
    """Попробовать к ссылке каждую стратегию по очереди: RSS → запрос → браузер.

    Только чтение. Раньше при ручном вводе ссылки искалась только RSS-лента, а без
    неё источник молча получал `request` по введённому адресу — без проверки, что
    запрос даёт хоть одну статью, без браузера и без маршрута через зарубежный
    воркер. Так заводились источники, которые не дали ни одной статьи никогда.

    Возвращает отчёт по каждой попытке и выбор: `parse_strategy`, `rss_url`,
    `listing_url`, `network_region`. `chosen = None` — сайт открылся, но статей не
    нашла ни одна стратегия: заводить такой источник включённым нельзя.
    """
    from oiltech_digest.ingestion.rss_discovery import discover_feed

    stub = {"id": None, "name": url, "url": url, "listing_url": url}
    attempts: list[dict] = []

    feed_url = discover_feed(url)
    if feed_url:
        rss = diagnose_rss_source({**stub, "rss_url": feed_url, "parse_strategy": "rss"}, limit=limit)
        latest = rss.get("latest_entry")
        fresh = bool(latest) and (datetime.now(timezone.utc) - datetime.fromisoformat(latest)).days <= FEED_STALE_DAYS
        attempts.append({"strategy": "rss", "rss_url": feed_url, "verdict": rss["verdict"],
                         "entries": rss.get("entry_count", 0), "latest_entry": latest, "fresh": fresh})
        if rss["verdict"] == "ok" and fresh:
            return {"url": url, "attempts": attempts,
                    "chosen": {"parse_strategy": "rss", "rss_url": feed_url, "network_region": "auto"}}
    else:
        attempts.append({"strategy": "rss", "verdict": "feed_not_found"})

    request = diagnose_request_source({**stub, "parse_strategy": "request"}, limit=limit)
    attempts.append(_attempt_summary("request", request))
    if _works(request):
        return {"url": url, "attempts": attempts,
                "chosen": {"parse_strategy": "request", "listing_url": url, "network_region": "auto"}}

    browser = diagnose_playwright_source({**stub, "parse_strategy": "playwright"}, limit=limit)
    attempts.append(_attempt_summary("playwright", browser))
    if _works(browser):
        return {"url": url, "attempts": attempts,
                "chosen": {"parse_strategy": "playwright", "listing_url": url, "network_region": "auto"}}

    listing_status = (request.get("listing_probe") or {}).get("status")
    if listing_status in _BLOCK_STATUSES and browser.get("verdict") == "no_candidates":
        # С РФ-ядра сайт не открылся ни запросом, ни браузером. Проверить отсюда, что
        # он откроется из NL, нельзя — это покажет первый сбор воркера.
        return {"url": url, "attempts": attempts,
                "chosen": {"parse_strategy": "playwright", "listing_url": url, "network_region": "external",
                           "note": "с РФ-ядра не открылся — сбор пойдёт через зарубежный воркер, "
                                   "результат будет виден после первого цикла"}}
    return {"url": url, "attempts": attempts, "chosen": None}


def _works(report: dict) -> bool:
    """Стратегия работает, если хоть одна статья дала текст. Префильтр по теме сюда
    не входит: он про содержание, а не про то, умеем ли мы достать страницу."""
    return any(check.get("text_chars", 0) >= 200 for check in report.get("article_checks") or [])


def _attempt_summary(strategy: str, report: dict) -> dict:
    return {
        "strategy": strategy,
        "verdict": report.get("verdict"),
        "listing_status": (report.get("listing_probe") or {}).get("status"),
        "candidates": report.get("candidate_count", 0),
        "sample": [c.get("title", "")[:90] for c in (report.get("candidates") or [])[:5]],
        "articles_with_text": sum(1 for c in report.get("article_checks") or [] if c.get("text_chars", 0) >= 200),
    }


def probe_url(url: str, timeout: int = REQUEST_TIMEOUT) -> tuple[ProbeResult, bytes | None]:
    """Single diagnostic GET. Returns HTTP metadata even when content is unusable."""
    host = (urlsplit(url).netloc or "").lower()
    proxies = _proxy_for(host)
    proxy_label = _mask_proxy(next(iter(proxies.values()))) if proxies else None
    try:
        response = requests.get(
            url,
            headers=_DEFAULT_HEADERS,
            timeout=timeout,
            allow_redirects=True,
            proxies=proxies,
        )
        result = ProbeResult(
            url=url,
            status=response.status_code,
            bytes=len(response.content),
            seconds=round(response.elapsed.total_seconds(), 2),
            proxy=proxy_label,
        )
        if response.status_code >= 400:
            return result, None
        return result, response.content
    except requests.exceptions.SSLError as exc:
        # Сырой probe не повторяет боевой SSL-фоллбэк (extended CA-bundle → verify=False),
        # поэтому РФ-гос/корп сайты на «Российском CA» падают тут, хотя в проде fetch()
        # их достаёт. Повторяем боевым путём, чтобы вердикт аудита отражал реальность.
        content = http_client.fetch(url, timeout=timeout)
        if content is not None:
            return (
                ProbeResult(url=url, status="OK*", bytes=len(content), proxy=proxy_label),
                content,
            )
        return (
            ProbeResult(
                url=url,
                status="ERR",
                error=f"{type(exc).__name__}: {str(exc)[:160]}",
                proxy=proxy_label,
            ),
            None,
        )
    except requests.RequestException as exc:
        return (
            ProbeResult(
                url=url,
                status="ERR",
                error=f"{type(exc).__name__}: {str(exc)[:160]}",
                proxy=proxy_label,
            ),
            None,
        )


def _base_payload(source: dict, strategy: str, url: str | None) -> dict:
    return {
        "source_id": source.get("id"),
        "source_name": source.get("name"),
        "strategy": strategy,
        "url": url,
    }
