"""Месячная аналитика платформы — экран «Статистика», блок графиков и показателей.

Что считаем — с точки зрения заказчика (презентация ГД «Нефтесервисный радар», июль 2026):
сквозной пайплайн «источник → сбор → отсев шума → суть и перевод → балл → дайджест»
(слайд 4), покрытие источников (>120, слайд 5), скорость появления сигнала (раннее
выявление, слайды 3 и 10), стоимость обработки против бюджета (слайд 7).

Месяц — календарный по Москве и по дате СБОРА (collected_at), как в
repository.monthly_platform_stats: по дате публикации архивные ленты искажали бы
динамику текущей работы. Незаконченный месяц сравнивается с тем же числом дней
прошлого — иначе «сентябрь по 19-е» всегда проигрывал бы полному августу."""

from __future__ import annotations

import calendar
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from oiltech_digest import config, fx
from oiltech_digest.db import connection

TZ = "Europe/Moscow"
_MONTH = f"to_char(a.collected_at AT TIME ZONE '{TZ}', 'YYYY-MM')"
_SINCE = f"(date_trunc('month', now() AT TIME ZONE '{TZ}') - make_interval(months => %(back)s)) AT TIME ZONE '{TZ}'"
_SAME_PERIOD = (
    f"AND {_MONTH} = %(month)s "
    f"AND extract(day FROM a.collected_at AT TIME ZONE '{TZ}') <= %(day)s"
)
# Время до сигнала — от публикации до готового балла. Только свежие публикации (не
# старше 30 дней к моменту сбора): архивная лента, впервые собранная, дала бы «сигнал
# через полгода», хотя платформа тут ни при чём.
_SPEED_FILTER = (
    "a.published_at IS NOT NULL AND s.created_at >= a.published_at "
    "AND a.collected_at - a.published_at < interval '30 days'"
)
_SPEED_HOURS = "extract(epoch FROM s.created_at - a.published_at) / 3600"

COUNTERS = (
    "collected", "en", "full_text", "relevant", "rejected", "summarized", "scored",
    "strong", "top", "hidden", "reprints", "digest_selected",
    "sources_active", "sources_relevant", "sources_strong",
)

_COUNTERS_SQL = f"""
    SELECT {_MONTH} AS month,
           count(*) AS collected,
           count(*) FILTER (WHERE a.language = 'en') AS en,
           count(*) FILTER (WHERE a.full_text_status = 'ok') AS full_text,
           count(*) FILTER (WHERE c.relevant IS TRUE) AS relevant,
           count(*) FILTER (WHERE c.relevant IS FALSE) AS rejected,
           count(*) FILTER (WHERE c.relevant IS TRUE AND COALESCE(c.summary, '') <> '') AS summarized,
           count(*) FILTER (WHERE s.total_score IS NOT NULL) AS scored,
           count(*) FILTER (WHERE s.total_score >= 60) AS strong,
           count(*) FILTER (WHERE s.total_score >= 70) AS top,
           count(*) FILTER (WHERE a.pending_deletion) AS hidden,
           count(*) FILTER (WHERE r.article_id IS NOT NULL) AS reprints,
           count(*) FILTER (WHERE d.article_id IS NOT NULL OR c.status = 'digest') AS digest_selected,
           count(DISTINCT a.source_id) AS sources_active,
           count(DISTINCT a.source_id) FILTER (WHERE c.relevant IS TRUE) AS sources_relevant,
           count(DISTINCT a.source_id) FILTER (WHERE s.total_score >= 60) AS sources_strong,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY {_SPEED_HOURS}) FILTER (WHERE {_SPEED_FILTER}) AS speed_p50_hours,
           percentile_cont(0.9) WITHIN GROUP (ORDER BY {_SPEED_HOURS}) FILTER (WHERE {_SPEED_FILTER}) AS speed_p90_hours
    FROM articles a
    LEFT JOIN article_cards c ON c.article_id = a.id
    LEFT JOIN article_scores s ON s.article_id = a.id
    LEFT JOIN article_reprints r ON r.article_id = a.id
    LEFT JOIN (SELECT DISTINCT article_id FROM user_article_states WHERE status = 'digest') d
           ON d.article_id = a.id
    WHERE a.collected_at >= {_SINCE}
      {{extra}}
    GROUP BY 1
    ORDER BY 1
"""

_COST_SQL = f"""
    SELECT to_char(r.created_at AT TIME ZONE '{TZ}', 'YYYY-MM') AS month,
           count(*) AS calls,
           count(DISTINCT r.article_id) AS articles,
           COALESCE(sum(r.cost_usd), 0) AS cost_usd
    FROM ai_processing_runs r
    WHERE r.created_at >= {_SINCE}
      {{extra}}
    GROUP BY 1
    ORDER BY 1
"""
_COST_SAME_PERIOD = (
    f"AND to_char(r.created_at AT TIME ZONE '{TZ}', 'YYYY-MM') = %(month)s "
    f"AND extract(day FROM r.created_at AT TIME ZONE '{TZ}') <= %(day)s"
)

_EXPORTS_SQL = f"""
    SELECT to_char(created_at AT TIME ZONE '{TZ}', 'YYYY-MM') AS month, count(*) AS digest_exports
    FROM background_jobs
    WHERE kind = 'digest_export' AND status = 'ok' AND created_at >= {_SINCE}
    GROUP BY 1
"""

# Темы — только действующие корневые теги (13 тематик заказчика с 13.09 и служебный
# приёмник). Статьи со старой таксономией сюда не попадают: экран показывает, сколько
# релевантного ещё не размечено по новым темам, вместо смешения двух классификаций.
_THEMES_SQL = f"""
    SELECT {_MONTH} AS month, root.id AS tag_id, root.name AS tag,
           count(*) FILTER (WHERE c.relevant IS TRUE) AS relevant,
           count(*) FILTER (WHERE s.total_score >= 60) AS strong
    FROM articles a
    JOIN article_cards c ON c.article_id = a.id
    JOIN article_tags at ON at.article_id = a.id
    JOIN tags t ON t.id = at.tag_id
    JOIN tags root ON root.id = COALESCE(t.parent_id, t.id)
    LEFT JOIN article_scores s ON s.article_id = a.id
    WHERE a.collected_at >= {_SINCE} AND root.parent_id IS NULL AND root.enabled
    GROUP BY 1, 2, 3
    ORDER BY 1, 4 DESC
"""

_TOP_SOURCES_SQL = f"""
    SELECT month, source_id, source, strong, relevant, collected
    FROM (
        SELECT {_MONTH} AS month, a.source_id, src.name AS source,
               count(*) FILTER (WHERE s.total_score >= 60) AS strong,
               count(*) FILTER (WHERE c.relevant IS TRUE) AS relevant,
               count(*) AS collected,
               row_number() OVER (
                   PARTITION BY {_MONTH}
                   ORDER BY count(*) FILTER (WHERE s.total_score >= 60) DESC,
                            count(*) FILTER (WHERE c.relevant IS TRUE) DESC, src.name
               ) AS place
        FROM articles a
        JOIN sources src ON src.id = a.source_id
        LEFT JOIN article_cards c ON c.article_id = a.id
        LEFT JOIN article_scores s ON s.article_id = a.id
        WHERE a.collected_at >= {_SINCE}
        GROUP BY 1, 2, 3
    ) ranked
    WHERE place <= %(top)s AND (strong > 0 OR relevant > 0)
    ORDER BY month, place
"""


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _rows(cur, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    cur.execute(sql, params)
    return [{key: _plain(value) for key, value in row.items()} for row in cur.fetchall()]


def _month_keys(now_local: datetime, months: int) -> list[str]:
    keys = []
    year, month = now_local.year, now_local.month
    for _ in range(months):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return keys[::-1]


def _empty_counters(month: str) -> dict[str, Any]:
    return {"month": month, **{key: 0 for key in COUNTERS}, "speed_p50_hours": None, "speed_p90_hours": None}


def monthly_analytics(months: int = 6, *, include_cost: bool = False, top_sources: int = 10) -> dict[str, Any]:
    """Всё для экрана одним ответом: месяцы подряд (пустые — нулями), тот же период
    прошлого месяца для честной дельты, темы, главные источники, цели из презентации.

    include_cost — стоимость ИИ только администратору: это коммерческая сторона."""
    months = max(1, min(int(months), 24))
    now_local = datetime.now(ZoneInfo(TZ))
    keys = _month_keys(now_local, max(months, 2))
    previous = keys[-2]
    day_cap = min(now_local.day, calendar.monthrange(int(previous[:4]), int(previous[5:]))[1])
    back = {"back": max(months, 2) - 1}
    same_period = {**back, "month": previous, "day": day_cap}
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        by_month = {row["month"]: row for row in _rows(cur, _COUNTERS_SQL.format(extra=""), back)}
        prev_rows = _rows(cur, _COUNTERS_SQL.format(extra=_SAME_PERIOD), same_period)
        exports = {row["month"]: row["digest_exports"] for row in _rows(cur, _EXPORTS_SQL, back)}
        themes = _rows(cur, _THEMES_SQL, back)
        sources = _rows(cur, _TOP_SOURCES_SQL, {**back, "top": top_sources})
        cur.execute("SELECT count(*) AS n FROM sources WHERE enabled AND archived_at IS NULL")
        sources_enabled = int(cur.fetchone()["n"])
        cost_rows = _rows(cur, _COST_SQL.format(extra=""), back) if include_cost else []
        cost_prev = _rows(cur, _COST_SQL.format(extra=_COST_SAME_PERIOD), same_period) if include_cost else []

    shown = keys[-months:]
    month_rows = []
    for key in shown:
        row = {**_empty_counters(key), **by_month.get(key, {})}
        row["digest_exports"] = int(exports.get(key, 0))
        row["complete"] = key != keys[-1]
        month_rows.append(row)
    prev_same = {**_empty_counters(previous), **(prev_rows[0] if prev_rows else {}), "days": day_cap}
    result: dict[str, Any] = {
        "timezone": TZ,
        "today": now_local.date().isoformat(),
        "current_month": keys[-1],
        "current_day": now_local.day,
        "months": month_rows,
        "previous_same_period": prev_same,
        "themes": [row for row in themes if row["month"] in shown],
        "top_sources": [row for row in sources if row["month"] in shown],
        "sources_enabled": sources_enabled,
        "targets": {
            "sources": config.ANALYTICS_TARGET_SOURCES,
            "articles_month": config.ANALYTICS_TARGET_ARTICLES_MONTH,
            "ai_rub_month": config.ANALYTICS_TARGET_AI_RUB_MONTH,
        },
    }
    if include_cost:
        # Курс ЦБ на последний день месяца, у текущего — на сегодня (fx.usd_rub).
        today = now_local.date()
        cost_by_month = {row["month"]: row for row in cost_rows}
        result["ai_cost"] = [
            {"month": key, "calls": 0, "articles": 0, "cost_usd": 0.0, **cost_by_month.get(key, {}),
             **_rate_fields(fx.usd_rub(_rate_day(key, today), today=today))}
            for key in shown
        ]
        result["ai_cost_previous_same_period"] = {
            "month": previous, "days": day_cap, "calls": 0, "articles": 0, "cost_usd": 0.0,
            **(cost_prev[0] if cost_prev else {}),
            **_rate_fields(fx.usd_rub(date(int(previous[:4]), int(previous[5:]), day_cap), today=today)),
        }
    return result


def _rate_day(month: str, today: date) -> date:
    year, number = int(month[:4]), int(month[5:])
    last = date(year, number, calendar.monthrange(year, number)[1])
    return min(last, today)


def _rate_fields(rate: dict[str, Any]) -> dict[str, Any]:
    return {"usd_rub": rate["rate"], "usd_rub_date": rate["date"], "usd_rub_source": rate["source"]}
