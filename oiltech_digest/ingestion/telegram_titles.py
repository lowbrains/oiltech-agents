"""Разовая починка склеенных заголовков Telegram — по сохранённым данным, без сети.

До 25.09 парсер брал текст поста через `text_content()`, а он теряет `<br>`: строки
склеивались («…в Иллинойсе<br>ExxonMobil…» → «ИллинойсеExxonMobil»), и заголовок —
первое предложение — захватывал начало второй строки. Сохранённый текст склеен так же,
а перечитать пост нельзя: t.me с РФ-ядра отвечает с перебоями (замер 25.09).

Поэтому стык строк восстанавливается по самому заголовку. Заглавная сразу после
строчной внутри «слова», которого корпус целиком не знает, — начало новой строки, если
левая часть — известное слово или правая — частое. Бренды вида «КазМунайГаз», «кВт»,
«ExxonMobil» корпус знает целиком, их не режем. Выборка 45 из 862 правок на проде
(25.09) — все 45 дали ровно первую строку поста.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import re
from typing import Any

# Модулем, а не функцией: тестовая фикстура подменяет connection.get_connection, и прямой
# импорт функции писал бы в настоящую базу (так было с source_overrides).
from oiltech_digest.db import connection
from oiltech_digest.ingestion.telegram_parser import title_from_text
from oiltech_digest.processing.domain_glossary import normalize_scripts

# Слово «известно», если встречается в стольких статьях: склейка — только в своей.
KNOWN_WORD_ARTICLES = 3
# Правая часть — частое слово (начало строки): «утроиласьТатнефть», «шуткойГубернатор».
COMMON_WORD_ARTICLES = 20
# Короткая левая часть с заглавной — обрывок бренда («Мега|Фон», «Каз|Мунай», «Рус|Гидро»),
# а не конец строки: так не режем, даже если слово «известно».
BRAND_PREFIX_CHARS = 5
# Строка не кончается предлогом, союзом или одной латинской буквой: «добычу в|ЯНАО»,
# «на|Ямале», «для e|VTOL» — пропущенный пробел (ревью 27.09). Короткой единицей —
# кончается: «массой 215 т|Масса…», «до 3 км|В Санкт-Петербурге…» (прогон на проде 27.09),
# а также словами, которые бывают концом заголовка: «Где и когда», «до и после».
_NOT_LINE_ENDS = frozenset({
    "в", "во", "с", "со", "к", "ко", "о", "об", "у", "а", "и", "на", "по", "от", "за", "из", "не", "но",
    "для", "при", "без", "под", "над", "про", "через", "или", "либо", "чтобы",
})
_LATIN_LETTER = re.compile(r"[A-Za-z]")

_LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_HTTP_GLUE = re.compile(r"[А-Яа-яЁё](?=https?://)")


def document_frequency(conn) -> Counter:
    """В скольких статьях встречается слово (в нижнем регистре) — по всему корпусу."""
    frequency: Counter = Counter()
    with conn.cursor(name="telegram_titles_words") as cur:
        cur.itersize = 2000
        cur.execute(
            """
            SELECT a.title, left(coalesce(a.raw_text, ''), 4000), coalesce(c.title_ru, ''), coalesce(c.summary, '')
            FROM articles a LEFT JOIN article_cards c ON c.article_id = a.id
            """
        )
        for row in cur:
            frequency.update({word.lower() for word in _LETTERS.findall(" ".join(part or "" for part in row))})
    return frequency


def split_glued_lines(title: str, frequency: Mapping[str, int]) -> list[str]:
    """Заголовок, разрезанный по восстановленным стыкам строк."""
    cuts: list[int] = []
    for match in _LETTERS.finditer(title):
        run = match.group(0)
        if frequency.get(run.lower(), 0) >= KNOWN_WORD_ARTICLES:
            continue
        bounds = [j for j in range(1, len(run)) if run[j - 1].islower() and run[j].isupper()]
        if match.start() > 0 and title[match.start() - 1] == "#":
            # Хэштег рубрики слитный с заглавными («#ЦифраДняСтенки»): стык строки — последний.
            bounds = bounds[-1:]
        cut = _line_break_in(run, bounds, frequency)
        if cut is not None:
            cuts.append(match.start() + cut)
    cuts.extend(match.end() for match in _HTTP_GLUE.finditer(title))
    segments: list[str] = []
    start = 0
    for cut in sorted(set(cuts)):
        segments.append(title[start:cut])
        start = cut
    segments.append(title[start:])
    return [segment.strip() for segment in segments if segment.strip()]


def _line_break_in(run: str, bounds: list[int], frequency: Mapping[str, int]) -> int | None:
    """Стык строки внутри «слова»: последний, где слева известное слово; иначе последний,
    где справа частое. Бренд идёт первым, начало строки — последним: «ГазпромБанкПрезидент» —
    после «ГазпромБанк», а не после «Газпром», хоть и оно известно (ревью 27.09)."""
    def splittable(bound: int) -> bool:
        left = run[:bound]
        if left.lower() in _NOT_LINE_ENDS or _LATIN_LETTER.fullmatch(left):
            return False
        return not (left[:1].isupper() and len(left) <= BRAND_PREFIX_CHARS)

    for bound in reversed(bounds):
        if splittable(bound) and frequency.get(run[:bound].lower(), 0) >= KNOWN_WORD_ARTICLES:
            return bound
    for index in range(len(bounds) - 1, -1, -1):
        bound = bounds[index]
        right = run[bound:bounds[index + 1]] if index + 1 < len(bounds) else run[bound:]
        if splittable(bound) and frequency.get(right.lower(), 0) >= COMMON_WORD_ARTICLES:
            return bound
    return None


def repaired_title(title: str, frequency: Mapping[str, int]) -> str:
    """Заголовок по правилу исправленного парсера — будто стыки строк были на месте."""
    segments = split_glued_lines(title, frequency)
    if len(segments) < 2:
        return title
    return title_from_text("\n".join(segments))


def repair(
    *, apply: bool = False, collected_before: Any = None, frequency: Mapping[str, int] | None = None
) -> dict[str, Any]:
    """Найти и (с apply) записать новые заголовки статей Telegram.

    collected_before — только статьи, собранные старым парсером: заголовки нового —
    правильные первые строки, эвристику к ним не применяем (там бренды вроде «МегаФон»
    она могла бы разрезать). `title_ru` русского поста — копия заголовка и меняется
    вместе с ним, в том числе если её уже поправил `repair-terminology --scripts-only`;
    свой перевод не трогаем. Записи — только если значение не изменилось с чтения.
    """
    query = (
        "SELECT a.id, a.title, c.title_ru FROM articles a "
        "JOIN sources s ON s.id = a.source_id "
        "LEFT JOIN article_cards c ON c.article_id = a.id "
        "WHERE s.parse_strategy = 'telegram'"
    )
    params: list[Any] = []
    if collected_before is not None:
        query += " AND a.collected_at < %s"
        params.append(collected_before)
    with connection.get_connection() as conn:
        if frequency is None:
            frequency = document_frequency(conn)
        rows = conn.execute(query + " ORDER BY a.id", params).fetchall()
        changes = []
        for article_id, title, title_ru in rows:
            new_title = repaired_title(title or "", frequency)
            if new_title and new_title != title:
                copied = title_ru is not None and normalize_scripts(title_ru) == normalize_scripts((title or "")[:200])
                changes.append({
                    "article_id": int(article_id),
                    "before": title,
                    "after": new_title,
                    "title_ru_before": title_ru if copied else None,
                })
        if apply:
            for change in changes:
                conn.execute(
                    "UPDATE articles SET title = %s WHERE id = %s AND title = %s",
                    (change["after"][:500], change["article_id"], change["before"]),
                )
                if change["title_ru_before"] is not None:
                    conn.execute(
                        "UPDATE article_cards SET title_ru = %s, updated_at = now() "
                        "WHERE article_id = %s AND title_ru = %s",
                        (normalize_scripts(change["after"][:200]), change["article_id"], change["title_ru_before"]),
                    )
            conn.commit()
    return {"scanned": len(rows), "changed": len(changes), "applied": apply, "changes": changes}
