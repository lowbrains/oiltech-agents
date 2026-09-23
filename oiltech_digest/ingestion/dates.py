"""Дата публикации из карточки листинга, адреса статьи или разметки страницы.

Зачем отдельный модуль: прежний разбор знал одну форму — `2026-09-17`. Обход 30
молчащих источников 18.09 показал, как даты пишут на самом деле: «17.09.2026»
(ТПУ, Сургутнефтегаз), «17 сентября 2026» (Белоруснефть), «24/08/2026» (Kuwait Oil),
«Sep 18, 2026» (S&P), «10 Sept 2026» (Shell), «17 September 2026» (Wood Mackenzie),
дата в адресе `news-14092026` (ТеДо) и `-2026.09.17-` (Белоруснефть). Без даты
источник теряет всё сразу: фильтр по возрасту не работает, и старьё (у ЦДУ ТЭК —
журнал за декабрь 2024) ложится в ленту как новое, а свежее не отличить от
закреплённого.

Разбор только по ЯВНЫМ формам. `dateparser.parse(fuzzy=True)` сюда не годится —
проверено ещё для JPT: он выдумывает дату там, где её нет («Section 5 of 12» →
2026-05-12). Поэтому каждое правило ниже — конкретная форма с годом.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from dateutil import parser as dateparser

from oiltech_digest.ingestion import normalize

_RU_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7,
    "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
_EN_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Порядок важен: сначала формы с названием месяца, потом цифровые. Иначе в строке
# «17 сентября 2026, 12.30» цифровое правило раньше увидело бы «12.30» — не дату,
# а время, но уже сбило бы разбор.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(\d{1,2})\s+([а-яё]{3,9})\.?\s+(20\d{2})\b", re.I), "d_mon_y_ru"),
    (re.compile(r"\b(\d{1,2})\s+([a-z]{3,9})\.?,?\s+(20\d{2})\b", re.I), "d_mon_y_en"),
    (re.compile(r"\b([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d{2})\b", re.I), "mon_d_y_en"),
    (re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(20\d{2})[./](\d{1,2})[./](\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b"), "dmy"),
]
# Дата в адресе статьи. Отдельно, потому что в тексте такие формы дают ложные
# срабатывания (номера документов, артикулы), а в пути статьи — нет.
_URL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)"), "ymd"),
    (re.compile(r"(?<!\d)(20\d{2})\.(\d{2})\.(\d{2})(?!\d)"), "ymd"),
    (re.compile(r"(?<![\d])(\d{2})(\d{2})(20\d{2})(?![\d])"), "dmy"),
    # Слитно год-месяц-день: CNOOC `t20260914_122684.html`.
    (re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)"), "ymd"),
]


def _month(token: str, table: dict[str, int]) -> int | None:
    token = token.lower()
    for prefix, number in table.items():
        if token.startswith(prefix):
            return number
    return None


def _build(kind: str, match: re.Match[str]) -> datetime | None:
    try:
        if kind == "d_mon_y_ru":
            day, month, year = int(match[1]), _month(match[2], _RU_MONTHS), int(match[3])
        elif kind == "d_mon_y_en":
            day, month, year = int(match[1]), _month(match[2], _EN_MONTHS), int(match[3])
        elif kind == "mon_d_y_en":
            month, day, year = _month(match[1], _EN_MONTHS), int(match[2]), int(match[3])
        elif kind == "ymd":
            year, month, day = int(match[1]), int(match[2]), int(match[3])
        else:  # dmy — день первым: источники не американские, а «24/08» однозначно
            day, month, year = int(match[1]), int(match[2]), int(match[3])
        if not month:
            return None
        return datetime(year, month, day, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _first(text: str, patterns: list[tuple[re.Pattern[str], str]]) -> datetime | None:
    for pattern, kind in patterns:
        for match in pattern.finditer(text):
            parsed = _build(kind, match)
            # Дата из будущего — это анонс события, а не публикация.
            if parsed is not None and parsed.year >= 2000 and not normalize.is_future_date(parsed):
                return parsed
    return None


def date_from_text(text: str | None) -> datetime | None:
    """Первая явная дата в тексте карточки или элемента разметки."""
    if not text:
        return None
    return _first(" ".join(str(text).split()), _PATTERNS)


def date_from_url(url: str | None) -> datetime | None:
    """Дата, зашитая в путь статьи: /2026/09/17/, -2026.09.17-, news-14092026."""
    if not url:
        return None
    return _first(url, _URL_PATTERNS)


def date_from_markup(doc) -> datetime | None:
    """Дата публикации из разметки страницы статьи: мета-теги, itemprop, JSON-LD.

    Прежде смотрелись только `article:published_time`, `pubdate` и первый `<time>` —
    на ЦДУ ТЭК, Сколтехе и ТеДо этого не было, и `published_at` оставался пуст.
    """
    values: list[str] = []
    for xpath in (
        "//meta[@property='article:published_time']/@content",
        "//meta[@name='pubdate']/@content",
        "//meta[@itemprop='datePublished']/@content",
        "//*[@itemprop='datePublished']/@datetime",
        "//meta[@name='date']/@content",
        "//meta[@name='DC.date' or @name='dc.date']/@content",
        "//time[1]/@datetime",
    ):
        try:
            values.extend(str(v) for v in doc.xpath(xpath))
        except Exception:  # noqa: BLE001 — битая разметка не должна ронять разбор
            continue
    for script in doc.xpath("//script[@type='application/ld+json']/text()"):
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', str(script))
        if match:
            values.append(match.group(1))
    try:
        values.append(str(doc.xpath("string(//time[1])")))
    except Exception:  # noqa: BLE001
        pass
    for value in values:
        parsed = date_from_text(value)
        if parsed is not None:
            return parsed
    return None


# --- Прежний разбор дат парсера листингов (перенесён из request_parser 18.09) ---
# Имена в request_parser сохранены алиасами: на них ссылаются тесты и соседние модули.

_DATE_TEXT_RE = re.compile(r"\b(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\b")
# Форма даты внутри более длинной строки: ISO, «19 August 2026», «August 25, 2026».
# Используется только как запасной путь в _parse_datetime — см. комментарий там.
_DATE_IN_TEXT_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4}\b"
    r"|\b[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}\b"
)


def guess_date_text(raw: str) -> str:
    if not raw:
        return ""
    match = _DATE_TEXT_RE.search(raw)
    return match.group(1) if match else ""


def _try_parse_datetime(raw: str) -> datetime | None:
    try:
        return dateparser.parse(raw)
    except (ValueError, TypeError, OverflowError):
        return None


def parse_datetime(raw: str) -> datetime | None:
    if not raw:
        return None
    parsed = _try_parse_datetime(raw)
    if parsed is None:
        # В карточке листинга дата почти никогда не лежит одна: рядом автор и название
        # издания («August 25, 2026 • JPT Staff • Journal of Petroleum Technology»).
        # Строгий разбор на такой строке падает, и источник годами идёт с пустым
        # published_at — ровно это было у JPT.
        #
        # dateparser.parse(fuzzy=True) сюда НЕ годится, проверено на реальных строках:
        # он выдумывает дату там, где её нет — «Section 5 of 12» превращалось в
        # 2026-05-12, а «Halliburton 2026 Q3 results webcast» в 2026-03-25. Поэтому
        # сначала вырезаем ФОРМУ даты, и разбираем только её.
        match = _DATE_IN_TEXT_RE.search(raw)
        if match:
            parsed = _try_parse_datetime(match.group(0))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if normalize.is_future_date(parsed):
        return None  # дата-анонс из будущего (календарь событий) — не дата публикации
    return parsed
