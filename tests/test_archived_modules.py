"""Архивные модули (ADR 0001, п. 7; список утверждён владельцем 23.09).

Модуль убран из меню и маршрутов, но код остаётся за флагом ARCHIVED_MODULES:
по умолчанию выключен, флагом возвращается без раскопок в истории git.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from oiltech_digest import api, config


def _client_as_user() -> TestClient:
    api.app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "u@example.com", "role": "user"}
    return TestClient(api.app)


def test_task_tracker_is_gone_without_the_flag(monkeypatch):
    monkeypatch.setattr(config, "ARCHIVED_MODULES", frozenset())
    client = _client_as_user()
    try:
        # До 23.09 трекер открывался и через основное приложение: https://oiltech-digest.ru/tasks.
        assert client.get("/tasks").status_code == 404
        assert client.get("/tasks/").status_code == 404
        assert client.get("/api/backlog").status_code == 404
        assert client.post("/api/backlog/tasks", json={"title": "x"}).status_code == 404
        assert client.patch("/api/backlog/tasks/P1", json={"status": "done"}).status_code == 404
        assert client.post("/api/backlog/tasks/P1/comments", json={"text": "x"}).status_code == 404
    finally:
        api.app.dependency_overrides.clear()


def test_task_tracker_comes_back_with_the_flag(monkeypatch):
    monkeypatch.setattr(config, "ARCHIVED_MODULES", frozenset({"backlog"}))
    monkeypatch.setattr(api.backlog, "read_backlog", lambda: {"tasks": []})
    client = _client_as_user()
    try:
        assert client.get("/tasks").status_code == 200
        assert client.get("/api/backlog").json() == {"tasks": []}
    finally:
        api.app.dependency_overrides.clear()


def test_session_reports_archived_modules_switched_on_by_the_flag(monkeypatch):
    api.app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "u@example.com", "role": "admin"}
    try:
        monkeypatch.setattr(config, "ARCHIVED_MODULES", frozenset())
        assert TestClient(api.app).get("/api/auth/me").json()["archived_modules"] == []
        monkeypatch.setattr(config, "ARCHIVED_MODULES", frozenset({"tech-preview", "analytics-preview"}))
        payload = TestClient(api.app).get("/api/auth/me").json()
        assert payload["archived_modules"] == ["analytics-preview", "tech-preview"]
        assert payload["user"]["email"] == "u@example.com"
    finally:
        api.app.dependency_overrides.clear()


def test_flag_parsing_keeps_only_known_modules():
    assert config.parse_archived_modules(" backlog, радар ,tech-preview,, ") == frozenset({"backlog", "tech-preview"})
    assert config.parse_archived_modules("") == frozenset()
