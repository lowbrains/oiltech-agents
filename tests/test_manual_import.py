"""Ручной импорт статьи по ссылке: порядок вызовов и поиск источника.

Оба дефекта подтверждены данными прода 17.09, см. коммит b670821.
"""

import pytest

from oiltech_digest.ingestion import manual_import


def test_source_is_not_created_when_download_fails(monkeypatch):
    """Источник заводится ПОСЛЕ скачивания.

    Обратный порядок плодил призраков: упавшая загрузка оставляла в каталоге строку
    «Manual import: домен» с enabled=TRUE и листингом на главную, и планировщик
    опрашивал её вечно. Так в ленту заехало 223 статьи научпопа со scientificrussia.ru.
    """
    created: list[str] = []

    monkeypatch.setattr(manual_import, "article_by_url", lambda url: None)
    monkeypatch.setattr(
        manual_import, "find_or_create_source",
        lambda url, explicit: created.append(url) or {"id": 1, "name": "x"},
    )

    def failing_fetch(url):
        raise manual_import.ManualImportError("сайт не ответил")

    monkeypatch.setattr(manual_import, "fetch_content", failing_fetch)

    with pytest.raises(manual_import.ManualImportError):
        manual_import.import_article("https://example.com/news/1")

    assert created == [], "источник заведён, хотя скачивание упало"


def test_archived_source_is_reused_instead_of_cloned(monkeypatch):
    """Поиск идёт среди ВСЕХ источников домена, не только включённых.

    С фильтром enabled = TRUE ссылка с заархивированного домена заводила ему дубль
    с enabled=TRUE — домен, который осознанно убрали из работы, молча возвращался
    в опрос. После архивации 28 источников 13–17.09 это было вопросом времени.
    """
    rows = [{"id": 7, "name": "Архивный", "archived_at": "2026-09-13", "enabled": False}]
    captured: dict = {}

    class _Cur:
        def execute(self, sql, params):
            captured["sql"] = sql

        def fetchone(self):
            return rows[0]

    class _Conn:
        def cursor(self, **kwargs):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(manual_import, "get_connection", lambda: _Conn())

    source = manual_import.find_or_create_source("https://archived.example/news/1", None)

    assert source["id"] == 7, "архивный источник не переиспользован — завёлся бы дубль"
    assert "enabled = TRUE" not in captured["sql"], "фильтр enabled вернулся в запрос"
    assert "archived_at IS NOT NULL" in captured["sql"], "активные должны идти вперёд архивных"


def test_explicit_source_id_skips_lookup(monkeypatch):
    """Явный source_id — единственный путь, который призрака не создаёт вовсе."""
    monkeypatch.setattr(manual_import.repository, "get_source", lambda sid: {"id": sid, "name": "n"})
    assert manual_import.find_or_create_source("https://x.example/a", 42)["id"] == 42


def test_explicit_source_id_must_exist(monkeypatch):
    monkeypatch.setattr(manual_import.repository, "get_source", lambda sid: None)
    with pytest.raises(manual_import.ManualImportError):
        manual_import.find_or_create_source("https://x.example/a", 999)
