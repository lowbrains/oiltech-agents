"""Listing-page scraper for sources that do not expose RSS."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit


from oiltech_digest.config import MIN_ARTICLE_TEXT_CHARS, REQUEST_ARTICLE_LIMIT
from oiltech_digest.db import repository
from oiltech_digest.ingestion import dates, normalize, verdicts
from oiltech_digest.ingestion.verdicts import ArticleFetch, Step
from oiltech_digest.ingestion.dates import guess_date_text as _guess_date_from_text
from oiltech_digest.ingestion.dates import parse_datetime as _parse_datetime
from oiltech_digest.ingestion.listing_cards import (
    GENERIC_LINK_TEXT_RE as _GENERIC_LINK_TEXT_RE,
    PAGE_ORDER,
    card_of as _card_of,
    fallback_title as _fallback_title,
    ordered as _ordered,
    safe_text_content as _safe_text_content,
    same_site as _same_site,
    spaced_text as _spaced_text,
)
from oiltech_digest.ingestion.listing_cards import link_key as _link_key
# Разбор страницы статьи живёт в article_page; здесь — реэкспорт для прежних вызовов
# (playwright_parser, manual_import, source_diagnostics берут его из request_parser).
from oiltech_digest.ingestion.article_page import first_non_empty as _first_non_empty
from oiltech_digest.ingestion.article_page import parse_article_page
from oiltech_digest.ingestion.http_client import fetch, final_url_of
from oiltech_digest.ingestion.relevance_filter import should_keep_article

logger = logging.getLogger(__name__)

_ARTICLE_HINT_RE = re.compile(
    r"(news|press|media|article|articles|blog|post|posts|story|stories|"
    r"insight|insights|publication|publications|release|releases|updates?)",
    re.I,
)
_BAD_LINK_RE = re.compile(
    r"(contact|about|privacy|terms|career|job|vacan|event|webinar|podcast|"
    r"subscribe|signin|login|register|mailto:|javascript:|#)",
    re.I,
)
_MEDIA_LINK_EXT_RE = re.compile(r"\.(?:avif|gif|jpe?g|png|svg|webp|bmp|ico|pdf|zip|rar|7z|mp4|mov|webm|mp3|wav)$", re.I)
_DATE_HINT_RE = re.compile(r"/20\d{2}/\d{1,2}/\d{1,2}/")


@dataclass(frozen=True)
class CandidateLink:
    url: str
    title: str
    score: int
    published_at: datetime | None = None


def parse_source(source: dict, max_age_days: int | None = None, article_limit: int = REQUEST_ARTICLE_LIMIT) -> dict:
    candidates = listing_candidates(source, article_limit)
    if candidates is None:
        return _empty_stats()
    return insert_candidates(source, candidates, max_age_days=max_age_days)


def listing_candidates(source: dict, article_limit: int = REQUEST_ARTICLE_LIMIT) -> list[CandidateLink] | None:
    """Кандидаты ленты источника — так, как их берёт сбор. None — ленты нет: адрес не
    задан или страница не скачалась. Зовут сбор и проба источника."""
    listing_url = source.get("listing_url") or source.get("url")
    if not listing_url:
        return None
    content = fetch(listing_url)
    if content is None:
        return None
    return extract_candidate_links(source, listing_url, content, limit=article_limit)


def candidate_steps(
    source: dict,
    candidates: list[CandidateLink],
    max_age_days: int | None = None,
    article_fetcher=None,
) -> Iterator[Step]:
    """Рубежи сбора по каждому кандидату, в порядке сбора. Одна реализация на двоих:
    insert_candidates вставляет READY, проба источника спрашивает рубежи вставки. Ленивый:
    сбор вставляет кандидата прежде, чем смотрит следующего. `article_fetcher` отдаёт
    ArticleFetch или, как fetch_article_candidate, запись/None (тогда отказ — fetch_failed)."""
    if article_fetcher is None:
        article_fetcher = fetch_article

    # Дедуп держится на articles.url (уникальный индекс + ON CONFLICT), а не на
    # хрупких оптимизациях. Раньше здесь были три «замораживателя», из-за которых
    # источники со структурно стабильным листингом (корпоративные newsroom без
    # даты в URL → published_at=None → сортировка по score вырождается в алфавит,
    # топ-N и его hash неизменны) навсегда застывали на первом улове:
    #   1) short-circuit по last_listing_hash — пропускал ВЕСЬ источник;
    #   2) break по last_seen_article_url — обрывал на первом же знакомом URL;
    #   3) break по known_streak>=3 — обрывал, пряча новые статьи в хвосте списка.
    # Теперь проходим всех кандидатов: знакомые (уже в БД) пропускаем без фетча,
    # новые — добавляем. listing_hash по-прежнему пишем — для диагностики.
    cutoff = None
    if max_age_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    for candidate in candidates:
        seen = {"url": candidate.url, "title": candidate.title, "published_at": candidate.published_at}
        if repository.article_exists(candidate.url):
            yield Step(verdicts.KNOWN, **seen)
            continue

        if cutoff is not None and candidate.published_at and candidate.published_at < cutoff:
            yield Step(verdicts.OLD, **seen)
            continue

        try:
            fetched = article_fetcher(candidate, source)
        except Exception as exc:  # noqa: BLE001 - одна статья не роняет весь источник
            logger.warning("request_parser: статья %s пропущена: %s: %s", candidate.url, type(exc).__name__, exc)
            yield Step(verdicts.FETCH_FAILED, **seen, detail=f"{type(exc).__name__}: {exc}"[:200])
            continue
        if not isinstance(fetched, ArticleFetch):
            fetched = ArticleFetch(fetched, None if fetched is not None else verdicts.FETCH_FAILED)
        article = fetched.article
        if article is None:
            yield Step(fetched.failure or verdicts.FETCH_FAILED, candidate.url, fetched.title or candidate.title,
                       candidate.published_at, fetched.text_chars, detail=fetched.detail)
            continue
        got = {"url": candidate.url, "title": article["title"], "published_at": article.get("published_at"),
               "text_chars": len(article.get("raw_text") or ""), "record": article}
        if cutoff is not None and article.get("published_at") and article["published_at"] < cutoff:
            yield Step(verdicts.OLD, **got)
            continue
        pre_filter = should_keep_article(article["title"], article.get("raw_text") or "", source)
        if not pre_filter.keep:
            yield Step(verdicts.PREFILTER, **got, detail=", ".join(pre_filter.matched_noise[:5]))
            continue
        yield Step(verdicts.READY, **got)


def insert_candidates(
    source: dict,
    candidates: list[CandidateLink],
    max_age_days: int | None = None,
    article_fetcher=None,
) -> dict:
    """Dedup, filter and insert a pre-fetched candidate list into the articles table.

    Extracted from parse_source so alternative fetch backends (e.g. Playwright)
    can reuse the same insertion logic after obtaining their own candidate list.
    Рубежи до вставки — candidate_steps, сама вставка — insert_article.
    """
    listing_hash = _listing_hash(candidates)
    added = attempted = skipped_old = skipped_irrelevant = skipped_known = 0
    for step in candidate_steps(source, candidates, max_age_days, article_fetcher):
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

    newest = candidates[0] if candidates else None
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


def extract_candidate_links(source: dict | str, listing_url: str | bytes, content: bytes | str | None = None,
                            limit: int = 12) -> list[CandidateLink]:
    if isinstance(source, str):
        source_dict = {}
        home_url = source
        body = listing_url
    else:
        source_dict = source
        home_url = str(listing_url)
        body = content
    if body is None:
        return []

    try:
        doc = normalize.parse_html(body)
    except (ValueError, TypeError):
        return []

    # <base href> — адрес, от которого страница велит разрешать относительные ссылки.
    # Его же браузер парсера вписывает после переадресации (with_base_href).
    base_href = doc.xpath("string(//base/@href)").strip()
    if base_href:
        home_url = urljoin(home_url, base_href)
    explicit = _extract_candidates_with_selector(doc, home_url, source_dict)
    if explicit:
        return explicit[:limit]

    base_host = (urlsplit(home_url).netloc or "").lower()
    seen: set[str] = set()
    candidates: list[tuple[int, CandidateLink]] = []

    for index, node in enumerate(doc.xpath("//a[@href]")):
        item = _build_candidate_from_anchor(home_url, base_host, node)
        if item is None or item.url in seen:
            continue
        seen.add(item.url)
        candidates.append((index, item))

    return _ordered(candidates, trust_page_order=False)[:limit]


def fetch_article(candidate: CandidateLink, source: dict) -> ArticleFetch:
    """Скачать статью-кандидата: запись для вставки или причина, почему её нет."""
    content = fetch(candidate.url)
    if content is None:
        return ArticleFetch(None, verdicts.FETCH_FAILED)
    final_url = final_url_of(candidate.url)
    if _moved_to_home_page(candidate.url, final_url):
        # Сайт увёл со статьи на главную (переехал, статья снята) — это не статья.
        return ArticleFetch(None, verdicts.REDIRECTED_HOME, detail=f"→ {final_url}")
    title, published_at, raw_text = parse_article_page(content, candidate.title)
    final_published = published_at or candidate.published_at
    if not title or len(raw_text) < MIN_ARTICLE_TEXT_CHARS:
        return ArticleFetch(None, verdicts.TOO_SHORT, title, len(raw_text))
    return ArticleFetch({
        "source_id": source["id"],
        "title": title[:500],
        "url": candidate.url,
        "published_at": final_published,
        "raw_text": raw_text,
        "text_truncated": normalize.is_truncated(raw_text),
        "language": _guess_language(source),
        "content_hash": normalize.compute_content_hash(title, candidate.url),
    }, None, title, len(raw_text))


def fetch_article_candidate(candidate: CandidateLink, source: dict) -> dict | None:
    """Запись статьи или None — для кода, которому причина не нужна (зарубежный воркер)."""
    return fetch_article(candidate, source).article


def _moved_to_home_page(url: str, final_url: str | None) -> bool:
    """Переадресация со статьи на главную сайта (путь «/» без query).

    Сколково Energy (25.09): energy.skolkovo.ru отдаёт 301 на www.skolkovo.ru/ для ЛЮБОГО
    адреса, и каждая «статья» была главной школы — общий заголовок сайта, описание кампуса.
    От вставки спасал только предфильтр (слово «ресторан» в описании), то есть случайность.
    Обычные переадресации статьи (слэш, https, www, красивый адрес вместо `?p=`) ведут не на
    главную; ссылка, которая сама указывает на главную с query (`/?p=678`), не судится.
    """
    if not final_url or final_url == url:
        return False
    final, own = urlsplit(final_url), urlsplit(url)
    return final.path in ("", "/") and not final.query and own.path not in ("", "/")


def _extract_candidates_with_selector(doc, listing_url: str, source: dict) -> list[CandidateLink]:
    listing_selector = source.get("listing_selector")
    link_selector = source.get("article_link_selector")
    date_selector = source.get("article_date_selector")
    if not any((listing_selector, link_selector, date_selector)):
        return []

    base_host = (urlsplit(listing_url).netloc or "").lower()
    nodes = _nodes_by_selector(doc, listing_selector) if listing_selector else []
    if not nodes:
        if listing_selector:
            # Фоллбэк на весь документ — самый коварный исход: источник с настроенным
            # селектором собирает ссылки отовсюду (у СМИ это сквозной сайдбар общей ленты),
            # и выглядит это как рабочая настройка. Пусть будет видно в логе.
            logger.warning("listing_selector %r не нашёл узлов — беру всю страницу",
                           listing_selector)
        nodes = [doc]

    seen: set[str] = set()
    candidates: list[tuple[int, CandidateLink]] = []
    for node in nodes:
        link_nodes = _nodes_by_selector(node, link_selector) if link_selector else node.xpath(".//a[@href]")
        for link in link_nodes:
            item = _build_candidate_from_anchor(listing_url, base_host, link, trusted=bool(link_selector))
            if item is None or item.url in seen:
                continue
            published_at = item.published_at
            if date_selector:
                date_nodes = _nodes_by_selector(node, date_selector)
                date_text = _first_non_empty(*[_node_text(n) for n in date_nodes])
                published_at = (_parse_datetime(date_text) or dates.date_from_text(date_text)
                                or published_at)
            seen.add(item.url)
            candidates.append((len(candidates), CandidateLink(item.url, item.title, item.score + 2, published_at)))
    # Селектор задан человеком под эту ленту — её порядок и есть порядок свежести. С
    # `listing_strategy='page_order'` — целиком, вместе с карточками без даты. Только
    # здесь, а не в общем разборе: без селектора первыми на странице идут шапка и меню.
    keep_page_order = (source.get("listing_strategy") or "").strip().lower() == PAGE_ORDER
    return _ordered(candidates, trust_page_order=True, keep_page_order=keep_page_order)


_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "yclid", "ymclid", "_openstat", "igshid", "mc_cid", "mc_eid",
}


def _clean_query(query: str) -> str:
    """Оставить значимые query-параметры, выкинув рекламно-трекинговые."""
    if not query:
        return ""
    kept = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    return urlencode(kept)


def _build_candidate_from_anchor(home_url: str, base_host: str, node,
                                 trusted: bool = False) -> CandidateLink | None:
    """`trusted` — ссылку выбрал селектор, заданный человеком под эту ленту. Тогда
    общие эвристики «мусорности» её не отбрасывают: у Белоруснефти новости лежат по
    адресам `/detail-pages/event/…`, и фильтр против календарей мероприятий
    (слово `event`) резал всю ленту даже при правильном селекторе."""
    href = (node.get("href") or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return None
    if not trusted and _BAD_LINK_RE.search(href):
        return None
    url = urljoin(home_url, href)
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return None
    if not _same_site(parts.netloc, base_host):
        return None
    if _MEDIA_LINK_EXT_RE.search(parts.path or ""):
        return None
    # Сохраняем значимую query-строку: у части источников идентификатор статьи именно
    # в ней (?id=, ?p=, ?article=). Раньше query отбрасывалась → разные статьи
    # схлопывались в URL раздела, и «Читать далее» в дайджесте вёл на сайт, а не на
    # конкретную новость (бэклог заказчика #3). Трекинговые параметры отсекаем.
    query = _clean_query(parts.query)
    # Финальный слэш СОХРАНЯЕМ: на многих корпоративных РФ-сайтах (Bitrix) адрес без него
    # отдаёт 404 — листинг читается, кандидаты извлекаются, а статей добавляется 0, и
    # источник выглядит «замолчавшим» (поймано на проде у Сургутнефтегаза 20.07).
    # rstrip нужен был только чтобы отсеять главную («/» → пусто), поэтому он остаётся
    # в ПРОВЕРКЕ ниже, но не в самом URL, по которому мы идём за статьёй.
    path = parts.path
    if path == "/":
        # Корень схлопываем в пустой путь — как было раньше. Иначе query-only ссылки
        # (https://site?p=678) сменили бы форму на https://site/?p=678, а articles.url
        # уникален → уже сохранённые статьи вставились бы заново как дубликаты.
        path = ""
    clean_url = f"{parts.scheme}://{parts.netloc}{path}"
    if query:
        clean_url += f"?{query}"
    # Главная/раздел без статейного таргета (нет ни пути, ни query) — не статья.
    if not path.rstrip("/") and not query:
        return None
    # Ссылка на саму ленту («Новости» в меню, «перейти к содержимому») — не статья.
    if not query and _link_key(clean_url) == _link_key(home_url):
        return None
    title = normalize.clean_html(_safe_text_content(node))
    card = None
    if len(title) < 18 or _GENERIC_LINK_TEXT_RE.match(title):
        # Ссылка-картинка или «Подробнее»: заголовок — рядом, в карточке. Раньше такие
        # ссылки отбрасывались (текст < 18), и у Белоруснефти, Б1, «Яков и Партнёры»
        # лента не давала ни одного кандидата; а «View Press Release» (ровно 18 знаков)
        # у Weatherford прошёл бы заголовком статьи.
        card = _card_of(node, home_url, clean_url)
        title = _fallback_title(node, card)
    if len(title) < 18:
        return None
    parent_text = _node_text(node.getparent()) if node.getparent() is not None else ""
    published_at = _parse_datetime(
        _first_non_empty(
            node.get("datetime"),
            node.get("content"),
            node.get("data-date"),
            _guess_date_from_text(parent_text),
        )
    )
    if published_at is None:
        # Дата из текста карточки — только из карточки ЭТОЙ статьи, а не из общего
        # списка: иначе ссылка получит дату соседа.
        if card is None:
            card = _card_of(node, home_url, clean_url)
        published_at = (dates.date_from_text(_spaced_text(card)) if card is not None else None) \
            or dates.date_from_text(_spaced_text(node)) or dates.date_from_url(clean_url)
    score = _score_candidate(parts.path, title)
    if published_at is not None:
        score += 2
    if score <= 0:
        if not trusted:
            return None
        score = 1
    return CandidateLink(clean_url, title[:500], score, published_at)


def _score_candidate(path: str, title: str) -> int:
    path_lower = (path or "").lower()
    score = 0
    if _ARTICLE_HINT_RE.search(path_lower):
        score += 4
    if _DATE_HINT_RE.search(path_lower):
        score += 3
    if path_lower.count("/") >= 2:
        score += 1
    if len(title) >= 40:
        score += 2
    if len(title) >= 80:
        score += 1
    if _BAD_LINK_RE.search(path_lower):
        score -= 5
    return score


def _nodes_by_selector(node, selector: str | None) -> list:
    if not selector:
        return []
    selector = selector.strip()
    if not selector:
        return []
    try:
        if selector.startswith(("/", ".//", "(")):
            return list(node.xpath(selector))
        return list(node.cssselect(selector))
    except Exception as exc:  # noqa: BLE001 - кривой селектор не должен валить парс источника
        # Молчать здесь нельзя: вызывающий код при пустом результате берёт ВСЮ страницу,
        # то есть отказ селектора выглядит как успешная фильтрация. Так пропал целый
        # ImportError — cssselect не был в requirements, и любой CSS-селектор был no-op.
        logger.warning("селектор %r не отработал (%s: %s)", selector, type(exc).__name__, exc)
        return []


def _node_text(node) -> str:
    try:
        return normalize.clean_html(node.text_content())
    except Exception:
        return ""


def _listing_hash(candidates: list[CandidateLink]) -> str | None:
    if not candidates:
        return None
    basis = "\n".join(item.url for item in candidates[:10])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _guess_language(source: dict) -> str | None:
    category = (source.get("category") or "").lower()
    if any(marker in category for marker in ("рф", "снг", "россий", "telegram")):
        return "ru"
    if "международ" in category:
        return "en"
    return None


def _empty_stats() -> dict:
    return {"added": 0, "attempted": 0, "skipped_old": 0, "skipped_irrelevant": 0, "skipped_known": 0}
