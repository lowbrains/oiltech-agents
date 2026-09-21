"""Telegram public-preview parser.

Uses https://t.me/s/<channel> pages, so public channels can be ingested without
Telegram API credentials or a user session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re
from urllib.parse import urlsplit

from dateutil import parser as dateparser
from lxml import etree, html

from oiltech_digest.db import repository
from oiltech_digest.ingestion import normalize
from oiltech_digest.ingestion.http_client import fetch
from oiltech_digest.ingestion.relevance_filter import should_keep_article

logger = logging.getLogger(__name__)

_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")
_POST_RE = re.compile(r"^([A-Za-z0-9_]{3,64})/(\d+)$")


@dataclass(frozen=True)
class TelegramPost:
    url: str
    title: str
    text: str
    published_at: datetime | None


def parse_source(source: dict, max_age_days: int | None = None, post_limit: int = 20) -> dict:
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
    if posts and source.get("last_listing_hash") and listing_hash == source.get("last_listing_hash"):
        repository.touch_last_parsed(source["id"])
        return {**_empty_stats(), "skipped_known": len(posts)}

    cutoff = None
    if max_age_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    last_seen_url = source.get("last_seen_article_url") or ""
    last_seen_published = source.get("last_seen_published_at")
    if isinstance(last_seen_published, str):
        last_seen_published = _parse_datetime(last_seen_published)

    added = attempted = skipped_old = skipped_irrelevant = skipped_known = 0
    newest_seen_url: str | None = None
    newest_seen_published: datetime | None = None

    for post in posts:
        if newest_seen_url is None:
            newest_seen_url = post.url
            newest_seen_published = post.published_at

        if last_seen_url and post.url == last_seen_url:
            skipped_known += 1
            break
        if repository.article_exists(post.url):
            skipped_known += 1
            continue
        if cutoff is not None and post.published_at and post.published_at < cutoff:
            skipped_old += 1
            continue
        if last_seen_published and post.published_at and post.published_at <= last_seen_published:
            skipped_old += 1
            continue

        pre_filter = should_keep_article(post.title, post.text, source)
        if not pre_filter.keep:
            skipped_irrelevant += 1
            logger.info(
                "Telegram pre-filter skipped %s: %s (%s)",
                source.get("name"),
                post.title,
                ", ".join(pre_filter.matched_noise[:5]),
            )
            continue

        attempted += 1
        if repository.insert_article(
            {
                "source_id": source["id"],
                "title": post.title[:500],
                "url": post.url,
                "published_at": post.published_at,
                "raw_text": post.text,
                "text_truncated": False,
                "language": "ru",
                "content_hash": normalize.compute_content_hash(post.title, post.url),
            }
        ):
            added += 1

    repository.touch_last_parsed(source["id"])
    repository.update_source_request_state(
        source["id"],
        last_seen_article_url=newest_seen_url,
        last_seen_published_at=newest_seen_published,
        last_listing_hash=listing_hash,
    )
    return {
        "added": added,
        "attempted": attempted,
        "skipped_old": skipped_old,
        "skipped_irrelevant": skipped_irrelevant,
        "skipped_known": skipped_known,
    }


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
    except (ValueError, TypeError, etree.ParserError):
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
    text = normalize.clean_html(text_nodes[0].text_content()) if text_nodes else ""
    if not text:
        return None

    published_at = _parse_datetime(
        _first_non_empty(
            node.xpath("string(.//time[1]/@datetime)"),
            node.xpath("string(.//a[contains(@class, 'tgme_widget_message_date')][1]/@href)"),
        )
    )
    title = _title_from_text(text)
    return TelegramPost(url=url, title=title, text=text, published_at=published_at)


def _title_from_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return "Telegram post"
    sentence = re.split(r"(?<=[.!?])\s+", compact, maxsplit=1)[0]
    if len(sentence) < 25:
        sentence = compact
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
