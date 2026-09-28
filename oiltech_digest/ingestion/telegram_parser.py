"""Telegram public-preview parser.

Uses https://t.me/s/<channel> pages, so public channels can be ingested without
Telegram API credentials or a user session.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re
from urllib.parse import urlsplit

from dateutil import parser as dateparser
from lxml import etree, html

from oiltech_digest.db import repository
from oiltech_digest.ingestion import normalize, verdicts
from oiltech_digest.ingestion.verdicts import Step
from oiltech_digest.ingestion.http_client import fetch
from oiltech_digest.ingestion.relevance_filter import should_keep_article

logger = logging.getLogger(__name__)

_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")
_POST_RE = re.compile(r"^([A-Za-z0-9_]{3,64})/(\d+)$")
# Граница строк поста: `<br>` и концы блоков. Переносы в исходнике HTML — не граница.
_LINE_BREAK = "\u2028"
_BLOCK_TAGS = ("p", "div", "blockquote", "li")
# Первая строка короче — рубрика («#ЦифраДня», «⚡️ Энергофакт»), а не заголовок.
MIN_TITLE_CHARS = 25


@dataclass(frozen=True)
class TelegramPost:
    url: str
    title: str
    text: str
    published_at: datetime | None


# Сколько постов превью берёт сбор за раз.
POST_LIMIT = 20


def parse_source(source: dict, max_age_days: int | None = None, post_limit: int = POST_LIMIT) -> dict:
    """Fetch a public Telegram channel preview and insert new posts as articles."""
    preview_url = preview_url_for_source(source)
    if not preview_url:
        logger.warning("Telegram %s — cannot derive channel from url=%r", source.get("name"), source.get("url"))
        return _empty_stats()

    content = fetch(preview_url)
    if content is None:
        return _empty_stats()

    posts = extract_posts(content, limit=post_limit)
    listing_hash = _listing_hash(posts)
    if listing_unchanged(source, posts):
        repository.touch_last_parsed(source["id"])
        return {**_empty_stats(), "skipped_known": len(posts)}

    added = attempted = skipped_old = skipped_irrelevant = skipped_known = 0
    for step in post_steps(source, posts, max_age_days):
        if step.stage == verdicts.KNOWN:
            skipped_known += 1
        elif step.stage == verdicts.OLD:
            skipped_old += 1
        elif step.stage == verdicts.PREFILTER:
            skipped_irrelevant += 1
        elif step.stage == verdicts.READY:
            attempted += 1
            if repository.insert_article(step.record):
                added += 1

    newest = posts[0] if posts else None
    repository.touch_last_parsed(source["id"])
    repository.update_source_request_state(
        source["id"],
        last_seen_article_url=newest.url if newest else None,
        last_seen_published_at=newest.published_at if newest else None,
        last_listing_hash=listing_hash,
    )
    return {
        "added": added,
        "attempted": attempted,
        "skipped_old": skipped_old,
        "skipped_irrelevant": skipped_irrelevant,
        "skipped_known": skipped_known,
    }


def listing_unchanged(source: dict, posts: list[TelegramPost]) -> bool:
    """Превью то же, что при прошлом сборе: тогда сбор не смотрит посты вовсе."""
    return bool(posts) and bool(source.get("last_listing_hash")) \
        and _listing_hash(posts) == source.get("last_listing_hash")


def post_steps(source: dict, posts: list[TelegramPost], max_age_days: int | None = None) -> Iterator[Step]:
    """Рубежи сбора по каждому посту превью (от новых к старым) — как их проходит сбор.

    Единственная реализация: parse_source вставляет READY, проба источника
    (`source-probe`) печатает вердикты. Последний пост прошлого сбора останавливает
    просмотр: он и всё, что ниже, — KNOWN, сбор их не смотрит.
    """
    cutoff = None
    if max_age_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    last_seen_url = source.get("last_seen_article_url") or ""
    last_seen_published = source.get("last_seen_published_at")
    if isinstance(last_seen_published, str):
        last_seen_published = _parse_datetime(last_seen_published)

    for index, post in enumerate(posts):
        seen = {"url": post.url, "title": post.title, "published_at": post.published_at,
                "text_chars": len(post.text)}
        if last_seen_url and post.url == last_seen_url:
            yield Step(verdicts.KNOWN, **seen, detail="последний пост прошлого сбора — ниже сбор не смотрит")
            for older in posts[index + 1:]:
                yield Step(verdicts.KNOWN, older.url, older.title, older.published_at, len(older.text),
                           detail="ниже последнего поста прошлого сбора")
            return
        if repository.article_exists(post.url):
            yield Step(verdicts.KNOWN, **seen)
            continue
        if cutoff is not None and post.published_at and post.published_at < cutoff:
            yield Step(verdicts.OLD, **seen)
            continue
        if last_seen_published and post.published_at and post.published_at <= last_seen_published:
            yield Step(verdicts.OLD, **seen, detail="не новее последнего поста прошлого сбора")
            continue

        pre_filter = should_keep_article(post.title, post.text, source)
        if not pre_filter.keep:
            logger.info(
                "Telegram pre-filter skipped %s: %s (%s)",
                source.get("name"),
                post.title,
                ", ".join(pre_filter.matched_noise[:5]),
            )
            yield Step(verdicts.PREFILTER, **seen, detail=", ".join(pre_filter.matched_noise[:5]))
            continue

        yield Step(verdicts.READY, **seen, record={
            "source_id": source["id"],
            "title": post.title[:500],
            "url": post.url,
            "published_at": post.published_at,
            "raw_text": post.text,
            "text_truncated": False,
            "language": "ru",
            "content_hash": normalize.compute_content_hash(post.title, post.url),
        })


def preview_url_for_source(source: dict) -> str | None:
    channel = channel_from_url(source.get("url") or "")
    return f"https://t.me/s/{channel}" if channel else None


def channel_from_url(raw_url: str) -> str | None:
    raw = (raw_url or "").strip()
    if not raw:
        return None
    if raw.startswith("@"):
        raw = raw[1:]
    if _CHANNEL_RE.fullmatch(raw):
        return raw

    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.netloc or "").lower()
    if host not in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
        return None
    chunks = [chunk for chunk in parts.path.split("/") if chunk]
    if not chunks:
        return None
    channel = chunks[1] if chunks[0] == "s" and len(chunks) > 1 else chunks[0]
    return channel if _CHANNEL_RE.fullmatch(channel) else None


def extract_posts(content: bytes | str, limit: int = 20) -> list[TelegramPost]:
    try:
        doc = html.fromstring(content)
    except (ValueError, TypeError, etree.ParserError):  # пустое тело — «Document is empty»
        return []

    posts: list[TelegramPost] = []
    for node in doc.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' tgme_widget_message ')]"):
        post = _post_from_node(node)
        if post is not None:
            posts.append(post)
    posts.sort(key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return posts[:limit]


def _post_from_node(node) -> TelegramPost | None:
    post_ref = (node.get("data-post") or "").strip()
    match = _POST_RE.match(post_ref)
    if not match:
        return None
    channel, post_id = match.groups()
    url = f"https://t.me/{channel}/{post_id}"

    text_nodes = node.xpath(".//*[contains(concat(' ', normalize-space(@class), ' '), ' tgme_widget_message_text ')]")
    lines = _message_lines(text_nodes[0]) if text_nodes else []
    text = " ".join(lines)
    if not text:
        return None

    published_at = _parse_datetime(
        _first_non_empty(
            node.xpath("string(.//time[1]/@datetime)"),
            node.xpath("string(.//a[contains(@class, 'tgme_widget_message_date')][1]/@href)"),
        )
    )
    title = title_from_text("\n".join(lines))
    return TelegramPost(url=url, title=title, text=text, published_at=published_at)


def _message_lines(node) -> list[str]:
    """Строки поста. `text_content()` теряет `<br>` и склеивает строки: до 25.09 так
    вышло «в Иллинойсе<br>ExxonMobil…» → «ИллинойсеExxonMobil», и заголовок захватывал
    начало второй строки (862 из 3423 заголовков Telegram)."""
    for element in node.iter():
        if element.tag == "br" or element.tag in _BLOCK_TAGS:
            element.tail = _LINE_BREAK + (element.tail or "")
    parts = (normalize.clean_html(part) for part in node.text_content().split(_LINE_BREAK))
    return [part for part in parts if part]


def title_from_text(text: str) -> str:
    """Заголовок поста: первая строка (строки — через перевод строки), в ней — первое
    предложение. Короткая первая строка — рубрика: тогда первое предложение всего текста."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in (text or "").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return "Telegram post"
    head = lines[0] if len(lines[0]) >= MIN_TITLE_CHARS else " ".join(lines)
    sentence = re.split(r"(?<=[.!?])\s+", head, maxsplit=1)[0]
    if len(sentence) < MIN_TITLE_CHARS:
        sentence = head
    return sentence[:140].strip()


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _parse_datetime(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = dateparser.parse(raw)
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _listing_hash(posts: list[TelegramPost]) -> str | None:
    if not posts:
        return None
    basis = "\n".join(post.url for post in posts[:20])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _empty_stats() -> dict:
    return {"added": 0, "attempted": 0, "skipped_old": 0, "skipped_irrelevant": 0, "skipped_known": 0}
