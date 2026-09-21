"""Fetch full article pages and extract readable main text.

RSS feeds often contain only a short teaser. This module upgrades those
records by downloading the linked article page and replacing ``raw_text`` when
the extracted body is clearly better than the RSS snippet.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re

from lxml import etree, html

from oiltech_digest import config
from oiltech_digest.db import repository
from oiltech_digest.ingestion import normalize
from oiltech_digest.ingestion.http_client import fetch

logger = logging.getLogger(__name__)

MIN_FULL_TEXT_CHARS = config.MIN_FULL_TEXT_CHARS
MIN_GAIN_RATIO = 2.0

_DROP_XPATH = (
    ".//script", ".//style", ".//noscript", ".//svg", ".//iframe",
    ".//nav", ".//footer", ".//header", ".//aside", ".//form",
)
_BAD_CLASS_RE = re.compile(
    r"(nav|menu|footer|header|cookie|banner|share|social|subscribe|"
    r"newsletter|advert|promo|related|sidebar|breadcrumb|comment)",
    re.I,
)
_GOOD_CLASS_RE = re.compile(r"(article|content|story|post|entry|body|main|text)", re.I)


@dataclass(frozen=True)
class ExtractionResult:
    text: str
    status: str
    method: str = "lxml"
    error: str | None = None
    image_url: str = ""


_OG_IMAGE_XPATHS = (
    "//meta[@property='og:image']/@content",
    "//meta[@property='og:image:url']/@content",
    "//meta[@property='og:image:secure_url']/@content",
    "//meta[@name='og:image']/@content",
    "//meta[@name='twitter:image']/@content",
    "//meta[@name='twitter:image:src']/@content",
    "//link[@rel='image_src']/@href",
)


def extract_og_image(content: bytes | str) -> str:
    """Best-effort lead image for a news card: og:image / twitter:image / image_src."""
    if not content:
        return ""
    try:
        doc = html.fromstring(content)
    except (ValueError, TypeError, etree.ParserError):
        return ""
    for xpath in _OG_IMAGE_XPATHS:
        for value in doc.xpath(xpath):
            url = (value or "").strip()
            if url.startswith("http"):
                return url
    return ""


def fetch_full_text(limit: int = 50, min_chars: int = MIN_FULL_TEXT_CHARS,
                    retry_too_short: bool = False) -> dict:
    """Fetch and store full text for truncated articles.

    retry_too_short=True re-attempts articles previously marked too_short or
    no_gain, useful after adding a new extraction backend (e.g. trafilatura).
    """
    # mismatch — отдельно от failed: это не сбой сети, а сработавшая защита от подмены
    # текста (№24). Смешивать их нельзя, иначе не видно, работает ли страж.
    stats = {"processed": 0, "updated": 0, "failed": 0, "too_short": 0,
             "no_gain": 0, "mismatch": 0}
    articles = repository.get_articles_needing_full_text(limit=limit, retry_too_short=retry_too_short)
    for article in articles:
        stats["processed"] += 1
        try:
            result = fetch_article_text(article, min_chars=min_chars)
            if result.status == "ok":
                repository.update_article_full_text(
                    int(article["id"]),
                    raw_text=result.text,
                    text_truncated=False,
                    status=result.status,
                    method=result.method,
                    error=None,
                    image_url=result.image_url,
                )
                stats["updated"] += 1
            else:
                repository.update_article_full_text(
                    int(article["id"]),
                    raw_text=None,
                    text_truncated=True,
                    status=result.status,
                    method=result.method,
                    error=result.error,
                    image_url=result.image_url,
                )
                stats[result.status if result.status in ("too_short", "no_gain", "mismatch") else "failed"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            logger.warning("Full text fetch failed for article %s: %s", article.get("id"), exc)
            repository.update_article_full_text(
                int(article["id"]),
                raw_text=None,
                text_truncated=True,
                status="failed",
                method="lxml",
                error=str(exc)[:500],
            )
            stats["failed"] += 1
    return stats


def backfill_images(limit: int = 200) -> dict:
    """Дозаполнить image_url (og:image) у статей без картинки — для дайджеста.

    fetch-full-text обрабатывает только статьи без полного текста; уже обработанные
    остаются без картинки. Здесь перефетчим страницу и берём og:image/twitter:image.
    """
    from oiltech_digest.ingestion.http_client import fetch

    stats = {"processed": 0, "updated": 0, "no_image": 0, "failed": 0}
    for article in repository.get_articles_missing_image(limit=limit):
        stats["processed"] += 1
        try:
            content = fetch(article["url"])
            if not content:
                stats["failed"] += 1
                continue
            image_url = extract_og_image(content)
            if image_url and repository.set_article_image(int(article["id"]), image_url):
                stats["updated"] += 1
            else:
                stats["no_image"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            logger.warning("backfill image failed for article %s: %s", article.get("id"), exc)
            stats["failed"] += 1
    return stats


def fetch_article_text(article: dict, min_chars: int = MIN_FULL_TEXT_CHARS) -> ExtractionResult:
    url = article.get("url")
    if not url:
        return ExtractionResult("", "failed", error="missing article url")
    content = fetch(url)
    if content is None:
        return ExtractionResult("", "failed", error="download failed")
    current = article.get("raw_text") or ""
    image_url = extract_og_image(content)

    title = article.get("title") or ""
    # Запоминаем отказ стража: если обе ветки извлечения дали ЧУЖОЙ текст, итог должен быть
    # mismatch, а не too_short. Иначе сработавшая защита выглядела бы как «мало текста»,
    # и починку дефекта №24 нельзя было бы отличить в статистике от обычной неудачи.
    rejected_reason: str | None = None
    rejected_method = "lxml"

    extracted = extract_main_text(content)
    if _is_better_text(extracted, current, min_chars=min_chars):
        rejection = _ownership_rejection(article, title, extracted)
        if rejection is None:
            return ExtractionResult(extracted, "ok", method="lxml", image_url=image_url)
        # Текст не принадлежит статье — НЕ пишем его, но пробуем вторую ветку извлечения.
        logger.warning("full_text_rejected article=%s method=lxml reason=%s",
                       article.get("id"), rejection)
        rejected_reason, extracted = rejection, ""

    # Fallback: trafilatura often handles cluttered pages better than lxml heuristics.
    traf = _trafilatura_extract(content)
    if traf and _is_better_text(traf, current, min_chars=min_chars):
        rejection = _ownership_rejection(article, title, traf)
        if rejection is None:
            return ExtractionResult(traf, "ok", method="trafilatura", image_url=image_url)
        logger.warning("full_text_rejected article=%s method=trafilatura reason=%s",
                       article.get("id"), rejection)
        rejected_reason, rejected_method, traf = rejection, "trafilatura", ""

    if rejected_reason is not None:
        # Сохранять нечего: прежний (пусть куцый, но СВОЙ) raw_text остаётся нетронутым.
        return ExtractionResult(
            "", "mismatch", method=rejected_method,
            error=f"extracted text rejected: {rejected_reason}", image_url=image_url,
        )

    best = traf if len(traf) > len(extracted) else extracted
    # Сюда попадают ДВА разных исхода, и раньше оба назывались too_short:
    #   1) страница действительно куцая — извлекли меньше min_chars;
    #   2) извлекли нормально, но не вдвое больше уже сохранённого (MIN_GAIN_RATIO),
    #      то есть текст у статьи ЕСТЬ и он не хуже — перезаписывать нечем.
    # Второе — не проблема, а штатный отказ от бесполезной перезаписи. Общее имя
    # too_short заставляло читать «у статьи нет текста» там, где текст на месте:
    # при ручном импорте 24.08 четыре статьи из пяти выглядели сломанными, хотя
    # в базе лежали их полные тела (у одной — 40 297 знаков).
    status = "no_gain" if len(best) >= min_chars else "too_short"
    return ExtractionResult(
        best,
        status,
        method="trafilatura" if traf and len(traf) > len(extracted) else "lxml",
        error=f"extracted={len(best)} chars, current={len(current)} chars",
        image_url=image_url,
    )


def extract_main_text(content: bytes | str) -> str:
    """Extract readable text from an article HTML page.

    This is deliberately conservative: it prefers semantic article/main nodes,
    removes navigation/ads, and returns a clean text block only when there is
    enough paragraph-like content.
    """
    if not content:
        return ""
    try:
        doc = html.fromstring(content)
    except (ValueError, TypeError, etree.ParserError):
        return ""

    structured_text = _json_ld_article_text(doc)

    for xpath in _DROP_XPATH:
        for node in doc.xpath(xpath):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)

    candidates = doc.xpath("//article|//main")
    candidates.extend(
        doc.xpath(
            "//*[self::div or self::section][contains(translate(@class,"
            " 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'article')"
            " or contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'content')"
            " or contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'story')"
            " or contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'article')"
            " or contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'content')]"
        )
    )
    if not candidates:
        candidates = [doc]

    best_text = structured_text
    best_score = len(structured_text) + 300 if len(structured_text) >= 120 else -1
    for node in candidates:
        text = _node_text(node)
        if len(text) < 120:
            continue
        score = _score_node(node, text)
        if score > best_score:
            best_score = score
            best_text = text
    return best_text


def _json_ld_article_text(doc) -> str:
    """Extract articleBody/text from JSON-LD structured data when present."""
    texts: list[str] = []
    for node in doc.xpath("//script[contains(translate(@type, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'ld+json')]"):
        raw = (node.text or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        texts.extend(_json_ld_text_values(payload))
    if not texts:
        return ""
    return max((normalize.clean_html(text) for text in texts), key=len, default="")


def _json_ld_text_values(payload) -> list[str]:
    if isinstance(payload, list):
        values: list[str] = []
        for item in payload:
            values.extend(_json_ld_text_values(item))
        return values
    if not isinstance(payload, dict):
        return []

    values = []
    for key in ("articleBody", "text"):
        value = payload.get(key)
        if isinstance(value, str) and len(value.strip()) >= 120:
            values.append(value)
    graph = payload.get("@graph")
    if graph:
        values.extend(_json_ld_text_values(graph))
    return values


def _node_text(node) -> str:
    chunks = []
    for item in node.xpath(".//p|.//li|.//h2|.//h3|.//blockquote"):
        class_id = " ".join(filter(None, [item.get("class"), item.get("id")]))
        if _BAD_CLASS_RE.search(class_id):
            continue
        text = normalize.clean_html(item.text_content())
        if len(text) >= 30:
            chunks.append(text)
    text = "\n\n".join(_dedupe_preserve_order(chunks))
    if len(text) < 200:
        # Вёрстка без значимых <p> (текст лежит в <div>/таблицах — частый случай
        # у CMS вроде EnergyLand): берём очищенный текст самого узла-кандидата.
        node_text = normalize.clean_html(node.text_content())
        if len(node_text) > len(text):
            text = node_text
    return text


def _score_node(node, text: str) -> float:
    class_id = " ".join(filter(None, [node.get("class"), node.get("id")]))
    good_bonus = 250 if _GOOD_CLASS_RE.search(class_id) else 0
    bad_penalty = 500 if _BAD_CLASS_RE.search(class_id) else 0
    paragraph_count = max(1, text.count("\n\n") + 1)
    link_text = " ".join(normalize.clean_html(a.text_content()) for a in node.xpath(".//a"))
    link_penalty = min(400, len(link_text) * 0.4)
    return len(text) + paragraph_count * 30 + good_bonus - bad_penalty - link_penalty


def _ownership_rejection(article: dict, title: str, text: str) -> str | None:
    """Причина, по которой текст НЕ принадлежит этой статье, либо None если всё в порядке.

    Два рубежа против подмены (задача №24, замер 28.07 — чужое тело у 10.4% видимой ленты,
    у Neftegaz.ru 37.7%):

    1. Заголовок не пересекается с текстом. Прежде принадлежность не проверялась ВООБЩЕ:
       `_is_better_text` смотрел только на длину, поэтому листинг, пейвол или «избранный»
       материал из JSON-LD побеждали настоящую статью и записывались со статусом ok.
    2. Такое же тело уже есть у другой статьи ЭТОГО источника. Ловит случай, который
       первый рубеж пропускает: сайт стабильно отдаёт одну и ту же страницу на все URL
       (пейвол), и её заголовок-обвязка может формально пересечься с заголовком статьи.
    """
    if not normalize.title_matches_body(title, text):
        return "title does not match body"

    source_id = article.get("source_id")
    if source_id is None:
        return None
    body_hash = normalize.compute_body_hash(text)
    try:
        if repository.body_hash_belongs_to_other_article(
            int(source_id), body_hash, exclude_article_id=article.get("id")
        ):
            return "identical body already stored for another article of this source"
    except Exception:  # noqa: BLE001 - сбой проверки не должен ронять дозагрузку
        logger.warning("body_hash check failed article=%s", article.get("id"))
    return None


def _is_better_text(extracted: str, current: str, min_chars: int) -> bool:
    extracted_len = len(extracted or "")
    current_len = len(current or "")
    if extracted_len < min_chars:
        return False
    if current_len < min_chars:
        return True
    if current_len and extracted_len < current_len * MIN_GAIN_RATIO:
        return False
    return True


def _trafilatura_extract(content: bytes | str) -> str:
    try:
        import trafilatura  # optional dep — not available in all envs
    except ImportError:
        return ""
    try:
        text = trafilatura.extract(
            content,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
        return (text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
