"""External-worker source scraping payloads and result application."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from oiltech_digest import config
from oiltech_digest.config import MIN_ARTICLE_TEXT_CHARS, REQUEST_ARTICLE_LIMIT
from oiltech_digest.db import repository
from oiltech_digest.ingestion import relevance_filter
from oiltech_digest.ingestion.relevance_filter import should_keep_article

logger = logging.getLogger(__name__)


def build_scrape_source_payload(source_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    source = repository.get_source(source_id)
    if source is None:
        raise ValueError("Source not found")
    return {
        "kind": "scrape_source",
        "source": _jsonable_dict(source),
        "max_age_days": payload.get("max_age_days"),
        "article_limit": int(payload.get("article_limit") or REQUEST_ARTICLE_LIMIT),
        "known_urls": repository.recent_article_urls_for_site(source.get("listing_url") or source.get("url")),
        "prefilter_tag_keywords": _prefilter_tag_keywords(),
    }


def _prefilter_tag_keywords() -> dict[str, list[str]]:
    positive, negative = relevance_filter.tag_keywords()
    return {"positive": list(positive), "negative": list(negative)}


def process_payload(payload: dict[str, Any], heartbeat=None) -> dict[str, Any]:
    keywords = payload.get("prefilter_tag_keywords")
    if keywords is None:  # ядро старее воркера — работаем как раньше
        return _process_source(payload, heartbeat=heartbeat)
    with relevance_filter.use_tag_keywords(keywords.get("positive") or [], keywords.get("negative") or []):
        return _process_source(payload, heartbeat=heartbeat)


def _process_source(payload: dict[str, Any], heartbeat=None) -> dict[str, Any]:
    source = payload["source"]
    strategy = source.get("parse_strategy")
    # heartbeat — всем стратегиям, а не только RSS. До 18.09 request и playwright шли
    # без него при lease в 600 с: браузерная статья — до 30 с на загрузку плюс
    # ожидание, и пакет из десятка статей упирался в lease так же, как задача 3908.
    if strategy == "playwright":
        return _process_playwright(source, payload, heartbeat=heartbeat)
    if strategy == "request":
        return _process_request(source, payload, heartbeat=heartbeat)
    if strategy == "rss":
        return _process_rss(source, payload, heartbeat=heartbeat)
    raise ValueError(f"Unsupported external scrape strategy: {strategy}")


def apply_scrape_result(result: dict[str, Any]) -> dict[str, Any]:
    source_id = int(result["source_id"])
    inserted = duplicates = 0
    for article in result.get("articles") or []:
        if repository.insert_article({**article, "source_id": source_id}):
            inserted += 1
        else:
            duplicates += 1
    repository.touch_last_parsed(source_id)
    repository.update_source_request_state(
        source_id,
        last_seen_article_url=result.get("last_seen_article_url"),
        last_seen_published_at=result.get("last_seen_published_at"),
        last_listing_hash=result.get("last_listing_hash"),
    )
    return {"inserted": inserted, "duplicates": duplicates, "source_id": source_id}


def _process_request(source: dict[str, Any], payload: dict[str, Any], heartbeat=None) -> dict[str, Any]:
    from oiltech_digest.ingestion.request_parser import extract_candidate_links, fetch_article_candidate, _listing_hash
    from oiltech_digest.ingestion.http_client import fetch

    listing_url = source.get("listing_url") or source.get("url")
    content = fetch(listing_url) if listing_url else None
    candidates = extract_candidate_links(source, listing_url, content, limit=int(payload.get("article_limit") or REQUEST_ARTICLE_LIMIT)) if content else []
    result = _articles_from_candidates(source, candidates, payload, fetch_article_candidate, _listing_hash,
                                       heartbeat=heartbeat)
    listing = "no_listing_url" if not listing_url else ("fetch_failed" if content is None else "ok")
    return _with_listing_report(result, listing, len(candidates))


def _process_rss(source: dict[str, Any], payload: dict[str, Any], heartbeat=None) -> dict[str, Any]:
    from oiltech_digest.ingestion.http_client import fetch
    from oiltech_digest.ingestion import rss_parser

    rss_url = source.get("rss_url")
    content = fetch(rss_url) if rss_url else None
    max_age_days = payload.get("max_age_days")
    recs, feed_stats = (
        rss_parser.extract_articles_from_feed(source, content, max_age_days)
        if content else ([], {"skipped_old": 0, "skipped_irrelevant": 0})
    )
    # Лента отдаёт заголовок и анонс, а не статью. Для источников зарубежного
    # контура это тупик: они там именно потому, что с РФ-адреса закрыты, и локальная
    # дозагрузка на них гарантированно ловит 403 и помечает failed НАВСЕГДА (одна
    # попытка). Замер 17.09: у Oil & Gas Journal и Offshore Magazine 25 из 25 статей
    # короче 600 знаков, средняя длина 183. Поэтому тело добираем здесь же, на
    # воркере, которому сайт отвечает.
    articles = [
        _jsonable_dict({k: v for k, v in rec.items() if k != "source_id"})
        for rec in _fill_bodies_from_source(source, recs, heartbeat=heartbeat)
    ]
    return {
        "external_fetch": True,
        "kind": "scrape_source",
        "source_id": int(source["id"]),
        "strategy": "rss",
        "stats": {
            "attempted": len(articles),
            "skipped_old": feed_stats.get("skipped_old", 0),
            "skipped_irrelevant": feed_stats.get("skipped_irrelevant", 0),
            "failed_fetch": 0,
        },
        "articles": articles,
        # RSS дедупится по article_exists (articles.url уникален) — listing-hash и
        # last_seen для лент не нужны (нет проблемы дедуп-заморозки request-парсера).
        "last_seen_article_url": articles[0]["url"] if articles else None,
        "last_seen_published_at": articles[0].get("published_at") if articles else None,
        "last_listing_hash": None,
    }


def _process_playwright(source: dict[str, Any], payload: dict[str, Any], heartbeat=None) -> dict[str, Any]:
    from oiltech_digest.ingestion.playwright_parser import render_listing_candidates, rendered_article
    from oiltech_digest.ingestion.request_parser import _listing_hash

    listing_url = source.get("listing_url") or source.get("url")
    limit = int(payload.get("article_limit") or REQUEST_ARTICLE_LIMIT)
    # Та же пара попыток, что у ядра, — общей функцией, а не копией.
    candidates = render_listing_candidates(source, listing_url, limit=limit) if listing_url else []
    result = _articles_from_candidates(source, candidates, payload, rendered_article, _listing_hash,
                                       heartbeat=heartbeat)
    if not listing_url:
        listing = "no_listing_url"
    elif candidates:
        listing = "ok"
    else:
        from oiltech_digest.ingestion.playwright_parser import last_fetch_status

        listing = last_fetch_status() or "no_candidates"
    return _with_listing_report(result, listing, len(candidates))


def _with_listing_report(result: dict[str, Any], listing: str, candidates: int) -> dict[str, Any]:
    """Положить в итог, что было с лентой: ok, blocked:403, fetch_failed, error:….

    Без этого задача с закрытой лентой завершалась «ok» с нулями и не отличалась от
    источника, у которого просто нет нового (S&P Global 18.09: 403 на ленте)."""
    stats = dict(result.get("stats") or {})
    if listing.startswith("ok") and candidates == 0:
        listing = "no_candidates"
    stats["listing"] = listing
    stats["listing_candidates"] = candidates
    return {**result, "stats": stats}


def _articles_from_candidates(source: dict[str, Any], candidates: list, payload: dict[str, Any], article_fetcher,
                              listing_hash_fn, heartbeat=None) -> dict[str, Any]:
    cutoff = None
    if payload.get("max_age_days") is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(payload["max_age_days"]))
    # Базы у воркера нет, и раньше он качал КАЖДУЮ статью листинга на каждом цикле:
    # ядро отбрасывало знакомые только при вставке. Список известных адресов даёт
    # ядро в payload — данные параметром, а не чтение базы (правило §11.4).
    known = set(payload.get("known_urls") or [])
    stats = {"attempted": 0, "skipped_old": 0, "skipped_irrelevant": 0, "failed_fetch": 0, "skipped_known": 0}
    articles: list[dict[str, Any]] = []
    newest_seen_url = candidates[0].url if candidates else None
    newest_seen_published = candidates[0].published_at if candidates else None
    for candidate in candidates:
        if candidate.url in known:
            stats["skipped_known"] += 1
            continue
        if heartbeat is not None:
            heartbeat()
        if cutoff is not None and candidate.published_at and candidate.published_at < cutoff:
            stats["skipped_old"] += 1
            continue
        try:
            article = article_fetcher(candidate, source)
        except Exception as exc:  # noqa: BLE001 - одна статья не роняет весь источник
            # Без этого любая ошибка страницы обрывала задачу целиком: уже скачанные
            # статьи этого же листинга терялись, а повтор задачи качал их заново.
            logger.warning("external_fetch: статья %s пропущена: %s: %s", candidate.url, type(exc).__name__, exc)
            article = None
        if article is None:
            stats["failed_fetch"] += 1
            continue
        if cutoff is not None and article.get("published_at") and article["published_at"] < cutoff:
            stats["skipped_old"] += 1
            continue
        pre_filter = should_keep_article(article["title"], article.get("raw_text") or "", source)
        if not pre_filter.keep:
            stats["skipped_irrelevant"] += 1
            continue
        stats["attempted"] += 1
        article = {key: value for key, value in article.items() if key != "source_id"}
        articles.append(_jsonable_dict(article))
    return {
        "external_fetch": True,
        "kind": "scrape_source",
        "source_id": int(source["id"]),
        "strategy": source.get("parse_strategy"),
        "stats": stats,
        "articles": articles,
        "last_seen_article_url": newest_seen_url,
        # Строкой, а не datetime: результат уходит ядру JSON-ом. Пока у кандидатов
        # не было дат, здесь стоял None и дыра молчала; правка дат 18.09 её вскрыла —
        # задачи падали «Object of type datetime is not JSON serializable».
        "last_seen_published_at": _jsonable(newest_seen_published),
        "last_listing_hash": listing_hash_fn(candidates),
    }


def _jsonable_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _jsonable(value) for key, value in dict(row).items()}


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _fill_bodies_from_source(source: dict[str, Any], recs: list[dict[str, Any]],
                             heartbeat=None) -> list[dict[str, Any]]:
    """Дотянуть полный текст статей ленты со страницы издания.

    Работает только там, где вызвано, — на воркере зарубежного контура. Берём
    страницу лишь когда анонс короче порога: у лент с полным текстом ходить
    незачем. Неудача не отбрасывает запись — остаётся анонс, как было раньше.
    """
    from oiltech_digest.ingestion import normalize
    from oiltech_digest.ingestion.article_fetcher import extract_main_text
    from oiltech_digest.ingestion.http_client import fetch

    filled: list[dict[str, Any]] = []
    for rec in recs:
        if heartbeat:
            heartbeat()
        text = str(rec.get("raw_text") or "")
        url = str(rec.get("url") or "")
        if len(text) >= MIN_ARTICLE_TEXT_CHARS or not url:
            filled.append(rec)
            continue
        title = str(rec.get("title") or "")
        try:
            content = fetch(url)
            body = extract_main_text(content, title=title) if content else ""
        except Exception:  # noqa: BLE001 - одна статья не валит прогон источника
            body = ""
        # Страж принадлежности (№24) здесь раньше не стоял вовсе: тело бралось, если
        # оно просто длиннее анонса. Чужой текст хуже короткого своего — оставляем анонс.
        if body and len(body) > len(text) and normalize.title_matches_body(title, body):
            rec = {**rec, "raw_text": body, "text_truncated": normalize.is_truncated(body)}
        filled.append(rec)
    return filled


def build_refetch_text_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Пакет статей-обрывков на дозаполнение зарубежным воркером.

    Локальная дозагрузка эти статьи не берёт (repository.get_articles_needing_full_text
    пропускает network_region='external'): с РФ-адреса они отдают 403, а попытка там
    ОДНА — статья получила бы failed навсегда. Тело есть кому добрать только на
    воркере, которому сайт отвечает.

    Шлём минимум — id, адрес и заголовок: заголовок нужен воркеру не для отображения,
    а чтобы отбить подменённый текст до того, как он поедет обратно.
    """
    ids = [int(x) for x in (payload.get("article_ids") or [])]
    if not ids:
        raise ValueError("refetch_text: пустой список article_ids")
    rows = repository.get_articles_for_external_refetch(ids)
    return {
        "kind": "refetch_text",
        "articles": [
            {"id": int(r["id"]), "url": r["url"], "title": r.get("title") or ""}
            for r in rows
        ],
        "min_chars": int(payload.get("min_chars") or config.MIN_FULL_TEXT_CHARS),
    }


def process_refetch_text_payload(payload: dict[str, Any], heartbeat=None) -> dict[str, Any]:
    """Сторона воркера: скачать страницы и вернуть тела. В базу не ходит.

    Биение на каждую статью обязательно: один недоступный хост стоит до 63 с
    (3 попытки по 20 с), lease внешней задачи — 600 с. Без биения пакет из 25
    статей не доживал до конца — его забирал реапер протухших lease на девятой,
    задача уходила в ретрай и на третьей попытке умирала вместе со всем пакетом.
    Ровно этот урок уже был получен на ИИ-обработчиках (T2/24.07), но загрузочные
    написаны позже и его не унаследовали.
    """
    from oiltech_digest.ingestion.article_fetcher import extract_main_text
    from oiltech_digest.ingestion.http_client import fetch

    min_chars = int(payload.get("min_chars") or 0)
    out: list[dict[str, Any]] = []
    for item in payload.get("articles") or []:
        if heartbeat:
            heartbeat()
        url = str(item.get("url") or "")
        record: dict[str, Any] = {"id": int(item["id"]), "status": "failed", "text": None}
        if url:
            try:
                content = fetch(url)
                body = extract_main_text(content, title=str(item.get("title") or "")) if content else ""
            except Exception as exc:  # noqa: BLE001 - одна статья не валит пакет
                body = ""
                record["error"] = str(exc)[:200]
            if body and len(body) >= min_chars:
                record.update({"status": "ok", "text": body})
            elif body:
                record.update({"status": "too_short", "text": body})
        out.append(record)
    return {
        "external_fetch": True,
        "kind": "refetch_text",
        "results": out,
        "stats": {
            "attempted": len(out),
            "ok": sum(1 for r in out if r["status"] == "ok"),
            "failed": sum(1 for r in out if r["status"] == "failed"),
        },
    }


def apply_refetch_text_result(result: dict[str, Any]) -> dict[str, Any]:
    """Сторона ядра: записать тела, пропустив подменённые.

    Страж принадлежности (задача №24) обязателен и здесь: воркер отдаёт то, что
    выдал сайт, а сайт умеет отдавать пейвол или листинг на любой адрес. Без этой
    проверки мы бы аккуратно разложили чужой текст по статьям.
    """
    from oiltech_digest.ingestion.article_fetcher import _ownership_rejection
    from oiltech_digest.ingestion import normalize

    applied = skipped = mismatched = 0
    for row in result.get("results") or []:
        article_id = int(row.get("id") or 0)
        text = row.get("text")
        status = str(row.get("status") or "failed")
        if not article_id:
            continue
        if status == "failed" or not text:
            repository.update_article_full_text(
                article_id, None, True, "failed", "external", error=row.get("error"))
            skipped += 1
            continue
        article = repository.get_article(article_id)
        if article is None:
            skipped += 1
            continue
        rejection = _ownership_rejection(article, article.get("title") or "", text)
        if rejection:
            repository.update_article_full_text(
                article_id, None, True, "mismatch", "external", error=rejection)
            mismatched += 1
            continue
        repository.update_article_full_text(
            article_id, text, normalize.is_truncated(text), status, "external")
        applied += 1
    return {"applied": applied, "skipped": skipped, "mismatched": mismatched}
