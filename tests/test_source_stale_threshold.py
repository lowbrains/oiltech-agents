"""Порог «Требуют внимания» (вердикт stale) на экране «Источники».

Решение владельца 28.09: одно правило для всех источников — 7 дней без нового
материала вместо 3. При 3 днях под порог попадали и редко пишущие источники: в плитке
«Требуют внимания» было 58 из 129, и сломанные источники в этом списке тонули.

Дни — календарные по Москве, как в колонке «Последняя загрузка» («N дн. назад»,
sourceUtils.ts): «7 дн. назад» — требует внимания, «6 дн. назад» — работает штатно.
Скользящие 7 × 24 ч с колонкой расходились: источник до суток показывал «7 дн. назад»
и оставался «Работает штатно» (ревью PR #71).

Число живёт в одном месте — config.SOURCE_STALE_DAYS (переменная окружения
SOURCE_STALE_DAYS). Отчёт, API и команды CLI своей копии не держат: без явного
порога все берут его оттуда в момент вызова.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, cli, config, feed_window
from oiltech_digest.db import connection, repository

# Сколько молчит источник: давность его последнего материала по дате сбора. Далеко от
# границы дня, поэтому вердикт не зависит от часа, в который идёт тест.
SILENCE = {"Silent 5 days": timedelta(days=5), "Silent 8 days": timedelta(days=8)}
EXPECTED = {"Silent 5 days": "ok", "Silent 8 days": "stale"}


def _add_source(conn, name: str, collected_at: datetime) -> None:
    """Включённый источник с одной статьёй, собранной в collected_at."""
    slug = name.lower().replace(" ", "-")
    source_id = conn.execute(
        """
        INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category)
        VALUES (%s, 'News', %s, TRUE, 'rss', 'международные')
        RETURNING id
        """,
        (name, f"https://example.com/{slug}"),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language)
        VALUES (%s, %s, %s, %s, %s, 'Text', 'en')
        """,
        (source_id, name, f"https://example.com/{slug}/1", collected_at, collected_at),
    )


@pytest.fixture
def silent_sources(isolated_db):
    now = datetime.now(timezone.utc)
    with connection.get_connection() as conn:
        for name, silence in SILENCE.items():
            _add_source(conn, name, now - silence)
        conn.commit()


def _verdicts(rows) -> dict[str, str]:
    return {row["name"]: row["verdict"] for row in rows}


def _get_health(query: str = "") -> list[dict]:
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    try:
        response = TestClient(app).get(f"/api/source-health?limit=500{query}")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    return response.json()


def test_source_silent_five_days_is_fine_and_eight_days_needs_attention(silent_sources):
    assert config.SOURCE_STALE_DAYS == 7
    assert _verdicts(repository.source_health_report(limit=10)) == EXPECTED


def test_days_are_moscow_calendar_days_like_the_last_load_column(isolated_db, monkeypatch):
    """«7 дн. назад» ⇔ «Требует внимания», «6 дн. назад» ⇔ «Работает штатно».

    Новый день начинается в 00:00 МСК = 21:00 UTC. Загрузка 22.09 в 00:00 МСК: в 23:59 МСК
    28.09 — 6 дней, штатно; минутой позже, в 00:00 МСК 29.09, — 7 дней, требует внимания.
    Загрузка минутой раньше, 21.09 в 23:59 МСК, — тот же день по UTC, но другой по Москве:
    в 23:59 МСК 28.09 это уже 7 дней, хотя прошло ровно 7 × 24 ч и скользящее окно считало
    источник штатным. Экран для тех же моментов показывает те же числа — sourceUtils.test.ts,
    «counts days by the Moscow calendar».
    """
    with connection.get_connection() as conn:
        _add_source(conn, "Loaded 21.09 23:59 MSK", datetime(2026, 9, 21, 20, 59, tzinfo=timezone.utc))
        _add_source(conn, "Loaded 22.09 00:00 MSK", datetime(2026, 9, 21, 21, 0, tzinfo=timezone.utc))
        conn.commit()

    def verdicts_at(moment: datetime) -> dict[str, str]:
        monkeypatch.setattr(feed_window, "_now", lambda: moment)
        return _verdicts(repository.source_health_report(limit=10))

    assert verdicts_at(datetime(2026, 9, 28, 20, 59, tzinfo=timezone.utc)) == {
        "Loaded 21.09 23:59 MSK": "stale",
        "Loaded 22.09 00:00 MSK": "ok",
    }
    assert verdicts_at(datetime(2026, 9, 28, 21, 0, tzinfo=timezone.utc)) == {
        "Loaded 21.09 23:59 MSK": "stale",
        "Loaded 22.09 00:00 MSK": "stale",
    }


def test_sources_screen_request_gets_the_same_verdicts(silent_sources):
    """Экран зовёт отчёт без порога (`/api/source-health?limit=500`): порог решает сервер."""
    assert _verdicts(_get_health()) == EXPECTED


def test_cli_source_health_and_source_retry_use_the_same_rule(silent_sources, monkeypatch, capsys):
    cli.main(["source-health"])
    header = capsys.readouterr().out.splitlines()[0]
    assert "stale_days=7" in header
    assert "stale=1" in header and "ok=1" in header

    # source-retry форсирует сбор только у тех, кто требует внимания, — не у молчащих 5 дней.
    retried: list[int] = []
    monkeypatch.setattr(
        "oiltech_digest.ingestion.rss_parser.parse_all",
        lambda **kwargs: retried.append(kwargs["source_id"]) or {},
    )
    cli.main(["source-retry"])
    names = {row["id"]: row["name"] for row in repository.source_health_report(limit=10)}
    assert [names[source_id] for source_id in retried] == ["Silent 8 days"]


def test_cli_prints_the_moscow_date_the_rule_counts_from(isolated_db, monkeypatch, capsys):
    """last= в source-health — дата по Москве, как в правиле.

    Сессия БД на проде в UTC, и загрузка с 00:00 до 03:00 МСК печаталась днём раньше:
    22.09 в 01:30 МСК выходила как 21.09, 28.09 оператор насчитывал 7 дней и ждал stale,
    а правило давало 6 — ok (повторное ревью PR #71).
    """
    connect = repository.get_connection

    def connect_in_utc():
        conn = connect()
        conn.execute("SET TIME ZONE 'UTC'")  # как на проде
        return conn

    monkeypatch.setattr(repository, "get_connection", connect_in_utc)
    with connection.get_connection() as conn:
        _add_source(conn, "Loaded 22.09 01:30 MSK", datetime(2026, 9, 21, 22, 30, tzinfo=timezone.utc))
        conn.commit()
    monkeypatch.setattr(feed_window, "_now", lambda: datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc))  # 20:00 МСК

    cli.main(["source-health"])
    row = capsys.readouterr().out.splitlines()[1]
    assert row.split()[1] == "ok"
    assert "last=2026-09-22" in row


def test_threshold_lives_in_config_and_an_explicit_one_still_wins(silent_sources, monkeypatch, capsys):
    """Одно место правды: сменили config.SOURCE_STALE_DAYS — сменились отчёт, API и CLI.

    Явный порог (разовый срез из CLI или API) по-прежнему главнее умолчания.
    """
    monkeypatch.setattr(config, "SOURCE_STALE_DAYS", 10)

    assert set(_verdicts(repository.source_health_report(limit=10)).values()) == {"ok"}
    assert set(_verdicts(_get_health()).values()) == {"ok"}
    assert set(_verdicts(_get_health("&stale_days=3")).values()) == {"stale"}

    cli.main(["source-health"])
    header = capsys.readouterr().out.splitlines()[0]
    assert "stale_days=10" in header and "stale=0" in header
    cli.main(["source-health", "--stale-days", "3"])
    header = capsys.readouterr().out.splitlines()[0]
    assert "stale_days=3" in header and "stale=2" in header
