"""Какие ссылки листинга считать статьями и в каком порядке брать их под лимит.

Вынесено из request_parser 18.09, когда правка выбора кандидатов вывела его за 500
строк. Здесь — всё, что решает «какие ссылки внутри одного источника»: порядок
(свежее первым), свой сайт или чужой, заголовок для ссылки без текста.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from oiltech_digest.ingestion import normalize


def safe_text_content(node) -> str:
    try:
        return node.text_content()
    except UnicodeDecodeError:
        return ""


def spaced_text(node) -> str:
    """Текст узла с пробелом между элементами. `text_content()` склеивает соседей
    вплотную — «…fraud warning02.04.2025», и дата теряет границу слова."""
    try:
        return " ".join(part.strip() for part in node.itertext() if part and part.strip())
    except (AttributeError, ValueError, UnicodeDecodeError, TypeError):
        return ""


def ordered(candidates: list[tuple[int, object]], *, trust_page_order: bool) -> list:
    """Порядок, в котором кандидаты идут под лимит: свежее — первым.

    Раньше сортировка была `(-балл, есть ли дата, дата ПО ВОЗРАСТАНИЮ, адрес)`: из
    датированных под лимит шли самые СТАРЫЕ, из недатированных — первые по АЛФАВИТУ
    адреса. Порядок страницы, где лента свежим сверху, выбрасывался. Закреплённые
    пункты занимали места навсегда, и источник «замерзал» на первом улове.

    Теперь: датированные — от новых к старым; недатированные — в порядке страницы
    (при заданном селекторе это и есть лента) или, без селектора, сперва по баллу
    статейности, а внутри балла — в порядке страницы.
    """
    dated = [pair for pair in candidates if pair[1].published_at is not None]
    undated = [pair for pair in candidates if pair[1].published_at is None]
    dated.sort(key=lambda pair: (-pair[1].published_at.timestamp(), pair[0]))
    if trust_page_order:
        undated.sort(key=lambda pair: pair[0])
    else:
        undated.sort(key=lambda pair: (-pair[1].score, pair[0]))
    return [item for _, item in dated + undated]


GENERIC_LINK_TEXT_RE = re.compile(
    r"^(подробнее|читать далее|читать|далее|узнать больше|перейти|смотреть|"
    r"read more|learn more|more|view press release|view more|details|see more|continue reading)\W*$",
    re.I,
)


def same_site(host: str, base_host: str) -> bool:
    """Ссылка того же сайта: тот же хост или его поддомен.

    Строгое равенство хостов отсекало новости на поддомене: у ТПУ лента живёт на
    `news.tpu.ru`, а источник был заведён на `tpu.ru` — не прошла ни одна ссылка.
    Родительский домен и соседние поддомены не принимаем: там навигация сайта.
    """
    host = (host or "").lower().removeprefix("www.")
    base = (base_host or "").lower().removeprefix("www.")
    return bool(host) and (host == base or host.endswith("." + base))


def link_key(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return (parts.netloc or "").lower().removeprefix("www."), (parts.path or "").rstrip("/")


def card_of(node, home_url: str, url: str):
    """Ближайший предок-карточка этой статьи: ссылки в нём ведут только на неё.

    Выше не поднимаемся — там уже список, и заголовок или дата окажутся чужими.
    """
    own = link_key(url)
    card, current = None, node
    try:
        for _ in range(4):
            parent = current.getparent()
            if parent is None:
                break
            others = {
                link_key(urljoin(home_url, href))
                for href in parent.xpath(".//a/@href")
                if href and not href.startswith(("#", "javascript:", "mailto:"))
            }
            others.discard(own)
            if others:
                break
            card, current = parent, parent
    except (AttributeError, ValueError, UnicodeDecodeError):
        # Битая разметка отдельной ссылки не должна ронять разбор всей страницы.
        return card
    return card


def fallback_title(node, card) -> str:
    """Заголовок для ссылки без текста: атрибуты ссылки, заголовок карточки, alt картинки."""
    def usable(value: str) -> str:
        value = normalize.clean_html(value or "")
        return value if len(value) >= 18 and not GENERIC_LINK_TEXT_RE.match(value) else ""

    try:
        for attr in ("title", "aria-label"):
            if found := usable(node.get(attr)):
                return found
        if card is not None:
            for heading in card.xpath(".//h1|.//h2|.//h3|.//h4|.//*[contains(@class, 'title')]"):
                if found := usable(safe_text_content(heading)):
                    return found
        for alt in node.xpath(".//img/@alt"):
            if found := usable(alt):
                return found
        if card is not None:
            text = usable(spaced_text(card))
            if text and len(text) <= 300:
                return text
            # Карточка с анонсом длиннее 300 знаков, заголовок не в h1–h4 (Mubadala:
            # «1 Sep | Mubadala Energy Publishes 2025 Sustainability Report… | анонс |
            # Learn more»). Заголовок — первый содержательный фрагмент карточки.
            for part in card.itertext():
                if (found := usable(part)) and len(found) <= 300:
                    return found
    except (AttributeError, ValueError, UnicodeDecodeError):
        return ""
    return ""
