"""Правила показа ленты бизнес-сигналов: окно месяца и базовая видимость (visible_sql).

ADR 0001, п. 6 — репозиторий lowbrains/oiltech-agents, docs/adr/0001-single-contour.md.

Лента показывает текущий месяц, а пока по МСК идут первые дни следующего
(день < FEED_ROLLOVER_DAY) — ещё и предыдущий: выпуск за месяц собирается в начале
следующего, и прошлый месяц в эти дни должен быть виден и доступен для отметок.
Прошлые месяцы открываются архивом (`month=ГГГГ-ММ`) только на просмотр.

Данные не меняются: это условие в запросе, а не флаг, который ставит задача 5-го
числа. Такая задача может не отработать или отработать дважды, а статья с поздней
датой проскочит мимо (ADR 0001, «Отвергнуто»).

Правило одно и живёт здесь. Лента (/api/articles), её счётчики (/api/stats) и отказ
в правке архива (PATCH /api/articles/{id}) берут условие отсюда. Для всех ролей
одинаково: исключения для админа нет (решение владельца 23.09).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

from oiltech_digest import config

MSK = ZoneInfo("Europe/Moscow")

_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
MONTH_PATTERN = _MONTH_RE.pattern

_MONTH_NAMES = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)


def period_month_sql(alias: str = "a") -> str:
    """Месяц периода статьи в SQL — то же выражение, что у сборщика выпуска.

    `repository.digest_candidates` относит статью к месяцу через
    `to_char(COALESCE(published_at, collected_at), 'YYYY-MM')` в часовом поясе сессии
    БД (на проде UTC). Лента обязана делить по месяцам так же: что видно в ленте за
    месяц, то и может попасть в выпуск этого месяца. Делить «по МСК» здесь нельзя:
    замер 23.09 — 69 статей ложатся в разные месяцы по UTC и по МСК. Такая статья
    была бы видна в октябрьской ленте, а в выпуск шла бы сентябрьским.
    """
    return f"to_char(COALESCE({alias}.published_at, {alias}.collected_at), 'YYYY-MM')"


def visible_sql(article: str = "a", card: str = "c", source: str = "s") -> str:
    """Что вообще может показать лента — одно условие для всех мест.

    Ждёт в запросе статью, её карточку (LEFT JOIN: у свежей статьи карточки ещё нет, и
    лента её показывает) и источник. Берут его лента (/api/articles), её счётчики
    (/api/stats), список архивных месяцев и сборщик выпуска (digest_candidates). До 23.09
    условие было переписано в каждом месте по-своему, и цифры разъехались: счётчик над
    лентой считал 2 205 «сигналов» при 1 934 статьях в самой ленте (перепечатки и статьи
    архивных источников считал, свежие без карточки — нет), а в выпуск шла статья,
    помеченная на удаление, которую лента давно прятала.

    - отклонённое гейтом релевантности (relevant = false) не показываем; ещё не проверенное
      (NULL) — показываем;
    - помеченное на удаление перепроверкой (recheck --mark) скрыто до purge/unmark;
    - архивный источник уносит с собой свои статьи (требование заказчика 12.09) — этого не
      делало `enabled = FALSE`: сбор прекращался, а накопленное висело в ленте у всех;
    - перепечатка скрыта: в ленте остаётся одна главная копия группы (08.09). Скрыта
      именно копия — главная (primary_id) в article_reprints не значится. Запись обратима.
    """
    return (
        f"{card}.relevant IS NOT FALSE"
        f" AND NOT {article}.pending_deletion"
        f" AND {source}.archived_at IS NULL"
        f" AND NOT EXISTS (SELECT 1 FROM article_reprints ar WHERE ar.article_id = {article}.id)"
    )


def _now() -> datetime:
    """Текущий момент. Отдельной функцией, чтобы тесты замораживали часы."""
    return datetime.now(MSK)


def parse_month(value: str) -> date:
    """«2026-08» → 1 августа 2026. Всё остальное — ValueError."""
    match = _MONTH_RE.match(value or "")
    if not match:
        raise ValueError(f"Месяц задаётся как ГГГГ-ММ, получено: {value!r}")
    return date(int(match.group(1)), int(match.group(2)), 1)


def month_key(month: date) -> str:
    return f"{month:%Y-%m}"


def month_label(month: date) -> str:
    """«август 2026» — для текста отказа и подписей."""
    return f"{_MONTH_NAMES[month.month - 1]} {month.year}"


def _previous_month(month: date) -> date:
    return (month.replace(day=1) - timedelta(days=1)).replace(day=1)


@dataclass(frozen=True)
class FeedWindow:
    """Открытые месяцы ленты на сегодня и, если запрошен, отдельный месяц.

    `start` — первый открытый месяц, последний — текущий по МСК. Статей «из будущего»
    лента не показывает: их и не бывает. Сбор отбрасывает даты дальше «сейчас + 2 дня»
    (normalize.is_future_date — это были анонсы событий из календарей сайтов), а замер
    23.09 — у 411 статей публикация позже сбора, наибольшее опережение 2 ч 56 мин
    (часовой пояс источника), в другой месяц не переходит ни одна.
    """

    start: date
    today: date
    month: date | None = None

    @property
    def open_months(self) -> list[str]:
        months = [self.start]
        current = self.today.replace(day=1)
        while months[-1] < current:
            months.append((months[-1] + timedelta(days=32)).replace(day=1))
        return [month_key(month) for month in months]

    @property
    def read_only(self) -> bool:
        """Запрошен прошлый месяц — архив, только просмотр."""
        return self.month is not None and self.month < self.start

    def is_open(self, period_month: str) -> bool:
        """Можно ли править статью с таким месяцем периода (строка «ГГГГ-ММ»): не архив."""
        return period_month >= month_key(self.start)

    def sql(self, alias: str = "a") -> str:
        """SQL-условие окна для WHERE.

        Месяцы подставляются литералами, а не параметрами: их формирует этот модуль из
        объектов `date` (только цифры и дефис), поэтому инъекции здесь быть не может.
        Литерал позволяет вставлять условие в запросы и с `%s`, и с `%(name)s`.
        """
        expr = period_month_sql(alias)
        if self.month is not None:
            return f"{expr} = '{month_key(self.month)}'"
        return f"{expr} BETWEEN '{month_key(self.start)}' AND '{month_key(self.today.replace(day=1))}'"

    def describe(self) -> dict:
        return {
            "months": self.open_months,
            "month": month_key(self.month) if self.month else None,
            "read_only": self.read_only,
            "rollover_day": config.FEED_ROLLOVER_DAY,
        }


def current(month: str | None = None, *, now: datetime | None = None) -> FeedWindow:
    """Окно на момент `now` (по умолчанию — сейчас), при необходимости с месяцем."""
    today = (now or _now()).astimezone(MSK).date()
    start = today.replace(day=1)
    if today.day < config.FEED_ROLLOVER_DAY:
        start = _previous_month(start)
    return FeedWindow(start=start, today=today, month=parse_month(month) if month else None)
