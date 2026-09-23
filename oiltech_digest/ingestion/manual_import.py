"""Manual article import helpers shared by admin UI and CLI scripts."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from psycopg.rows import dict_row

from oiltech_digest.db import repository
from oiltech_digest.db.connection import get_connection
from oiltech_digest.ingestion import normalize
from oiltech_digest.ingestion.article_fetcher import extract_og_image, fetch_article_text
from oiltech_digest.ingestion.http_client import fetch
from oiltech_digest.ingestion.request_parser import parse_article_page


class ManualImportError(ValueError):
    """Validation or fetch/parsing problem during manual import."""


@dataclass(frozen=True)
class ManualImportResult:
    article_id: int
    source_id: int
    source_name: str
    duplicate: bool
    title: str
    fetch_method: str
    full_text_status: str | None
    full_text_method: str | None
    full_text_chars: int


def import_article(url: str, explicit_source_id: int | None = None) -> ManualImportResult:
    normalized_url = url.strip()
    existing = article_by_url(normalized_url)
    if existing is not None:
        return ManualImportResult(
            article_id=int(existing["id"]),
            source_id=int(existing["source_id"]),
            source_name=str(existing["source_name"]),
            duplicate=True,
            title=str(existing["title"] or normalized_url),
            fetch_method="existing",
            full_text_status=existing.get("full_text_status"),
            full_text_method=existing.get("full_text_method"),
            full_text_chars=int(existing.get("full_text_chars") or 0),
        )

    # Сначала СКАЧИВАЕМ, только потом заводим источник. Обратный порядок плодил
    # источники-призраки: упавшая загрузка оставляла в каталоге строку
    # «Manual import: домен» с enabled=TRUE и листингом на главную, и планировщик
    # начинал опрашивать её вечно. Так в ленту заехало 223 статьи научпопа со
    # scientificrussia.ru и 17 инвест-релизов IT-компании — замер 17.09.
    content, fetch_method = fetch_content(normalized_url)
    source = find_or_create_source(normalized_url, explicit_source_id)
    title, published_at, raw_text = parse_article_page(content, "")
    title = (title or normalized_url).strip()
    raw_text = (raw_text or title).strip()
    image_url = extract_og_image(content) or None

    repository.insert_article(
        {
            "source_id": int(source["id"]),
            "title": title[:500],
            "url": normalized_url,
            "published_at": published_at,
            "raw_text": raw_text,
            "text_truncated": normalize.is_truncated(raw_text),
            "language": guess_language(raw_text or title),
            "content_hash": normalize.compute_content_hash(title, normalized_url),
            "image_url": image_url,
        }
    )
    inserted = article_by_url(normalized_url)
    if inserted is None:
        raise ManualImportError("article insert did not return a row")
    refresh = refresh_full_text(int(inserted["id"]))
    return ManualImportResult(
        article_id=int(inserted["id"]),
        source_id=int(source["id"]),
        source_name=str(source["name"]),
        duplicate=False,
        title=title,
        fetch_method=fetch_method,
        full_text_status=refresh["status"],
        full_text_method=refresh["method"],
        full_text_chars=int(refresh["chars"]),
    )


def article_by_url(article_url: str) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.id, a.source_id, a.title, a.url, s.name AS source_name,
                   a.full_text_status, NULL::text AS full_text_method,
                   char_length(coalesce(a.raw_text, '')) AS full_text_chars
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.url = %s
            """,
            (article_url,),
        )
        return cur.fetchone()


def host_tokens(article_url: str) -> tuple[str, str]:
    parsed = urlparse(article_url)
    host = (parsed.hostname or "").lower()
    if not parsed.scheme.startswith("http") or not host:
        raise ManualImportError("URL must be absolute http(s)")
    bare = host[4:] if host.startswith("www.") else host
    return host, bare


def find_or_create_source(article_url: str, explicit_source_id: int | None) -> dict:
    if explicit_source_id is not None:
        source = repository.get_source(int(explicit_source_id))
        if source is None:
            raise ManualImportError(f"source_id={explicit_source_id} not found")
        return source

    host, bare = host_tokens(article_url)
    host_like = f"%{host}%"
    bare_like = f"%{bare}%"
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        # Ищем среди ВСЕХ источников домена, а не только включённых. Фильтр
        # `enabled = TRUE` означал, что ссылка с заархивированного домена заводит
        # ему дубль-призрак с enabled=TRUE — и домен, который осознанно убрали из
        # работы, молча возвращался в опрос. После архивации 28 источников 13–17.09
        # это стало вопросом времени. Архивный источник переиспользуем как есть:
        # статья привяжется к нему и останется скрытой, что и требуется.
        cur.execute(
            """
            SELECT *
            FROM sources
            WHERE (
                lower(coalesce(url, '')) LIKE %s
                OR lower(coalesce(url, '')) LIKE %s
                OR lower(coalesce(rss_url, '')) LIKE %s
                OR lower(coalesce(rss_url, '')) LIKE %s
                OR lower(coalesce(listing_url, '')) LIKE %s
                OR lower(coalesce(listing_url, '')) LIKE %s
              )
            ORDER BY (archived_at IS NOT NULL), priority DESC NULLS LAST, id
            LIMIT 1
            """,
            (host_like, bare_like, host_like, bare_like, host_like, bare_like),
        )
        source = cur.fetchone()
        if source is not None:
            return source

    source_id = repository.add_rss_source(
        name=f"Manual import: {bare}",
        rss_url="",
        source_type="manual",
        url=f"{urlparse(article_url).scheme}://{host}",
        parse_strategy="request",
    )
    # Держатель для вручную внесённой статьи, а не подписка на сайт: выключен.
    # Включённым он опрашивался `request` по главной вечно — так 17.09 в ленту
    # заехали 223 статьи научпопа с scientificrussia.ru. Статья видна в ленте и так
    # (скрывает только архив). Подписаться на сайт — через добавление источника,
    # где к ссылке пробуется каждая стратегия.
    repository.set_source_collection(source_id, listing_url=None, network_region="auto", enabled=False)
    source = repository.get_source(source_id)
    if source is None:
        raise ManualImportError("fallback source was not created")
    return source


def fetch_content(article_url: str) -> tuple[bytes | str, str]:
    content = fetch(article_url)
    if content:
        return content, "http"

    try:
        from oiltech_digest.ingestion.playwright_parser import fetch_rendered

        rendered = fetch_rendered(article_url, settle_ms=8000)
        if rendered:
            return rendered, "playwright"
    except Exception as exc:  # noqa: BLE001
        raise ManualImportError(f"playwright fallback failed: {exc}") from exc

    raise ManualImportError("could not fetch article content")


def guess_language(text: str) -> str:
    sample = text[:3000].lower()
    return "ru" if any("а" <= ch <= "я" for ch in sample) else "en"


def refresh_full_text(article_id: int) -> dict[str, str | int | None]:
    article = repository.get_article(article_id)
    if article is None:
        return {"status": None, "method": None, "chars": 0}
    result = fetch_article_text(article, min_chars=500)
    repository.update_article_full_text(
        article_id,
        raw_text=result.text if result.status == "ok" else None,
        text_truncated=result.status != "ok",
        status=result.status,
        method=result.method,
        error=result.error,
        image_url=result.image_url,
    )
    return {"status": result.status, "method": result.method, "chars": len(result.text or "")}
