"""Полоса external-agents (ADR 0001, слияние 23.09): радар сигналов и оценка кандидатов
агента источников — в своей очереди и у своего воркера на NL.

До слияния радар делил external-ai с потоком дня: прогон радара и пакет статей ждали
друг друга на одном однопоточном воркере. Код радара и агента источников — Германа и
не меняется, поэтому полосу задаче выбирает постановка, а не вызывающий код.

Каждый тест, кроме двух стражей (полоса берёт только агентов; раскладка NL), падает
на коде до правки: слитое ядро без полосы отказывало радару в постановке."""

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from oiltech_digest import api, background_jobs, external_worker, lanes, network_policy
from oiltech_digest.db import repository

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


# --- Постановка --------------------------------------------------------------------------


def test_daily_radar_lands_in_agents_lane(isolated_db, monkeypatch):
    """Ежедневный радар ставит сам код радара: маршрут ИИ (external-ai) — и попадает к
    воркеру агентов, а не в очередь потока дня."""
    monkeypatch.setattr(network_policy.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(network_policy.config, "AI_EXECUTION_REGION", "external")
    monkeypatch.setattr(background_jobs.config, "BACKGROUND_JOB_INLINE", False)

    result = background_jobs.enqueue_daily_signal_discovery(force=True)

    assert result["enqueued"]
    assert result["job"]["queue_name"] == lanes.AGENTS


def test_agent_jobs_named_for_ai_lane_go_to_agents_lane(isolated_db):
    # source_discovery/loop.py ставит оценку кандидата в "external-ai" строкой.
    evaluation = repository.create_background_job(
        "source_candidate_evaluate", {"candidate_id": 1}, queue_name="external-ai", execution_region="external"
    )
    radar_bulk = repository.create_background_job(
        "signal_discovery", {}, queue_name="external-ai-bulk", execution_region="external"
    )

    assert evaluation["queue_name"] == radar_bulk["queue_name"] == lanes.AGENTS


def test_agents_lane_takes_only_agent_jobs(isolated_db):
    # Поток дня в полосу агентов не встаёт: иначе пакет статей снова ждал бы прогон радара.
    with pytest.raises(ValueError, match="не обслуживает"):
        repository.create_background_job("process_articles", {"limit": 5}, queue_name=lanes.AGENTS)
    # Радар в полосе сбора — ошибка вызывающего, а не повод угадывать.
    with pytest.raises(ValueError, match="не обслуживает"):
        repository.create_background_job("signal_discovery", {}, queue_name="external-fetch")
    # Местный контур (ru, offline) остаётся местным, поток дня — в своей полосе.
    assert repository.create_background_job("signal_discovery", {}, queue_name="ai")["queue_name"] == "ai"
    assert repository.create_background_job("process_articles", {"limit": 5}, queue_name="external-ai")["queue_name"] == "external-ai"


# --- Выдача ------------------------------------------------------------------------------


def test_agent_jobs_get_their_payload_in_agents_lane(monkeypatch):
    """Payload собирается по таблице полос: без агентной полосы воркер получил бы сырой
    payload — радар без снимка базы, оценка без статей кандидата."""
    from oiltech_digest import signal_discovery

    monkeypatch.setattr(signal_discovery, "build_external_payload", lambda payload: {"snapshot": {"topics": ["Бурение"]}})
    monkeypatch.setattr(api.external_ai, "build_source_candidate_evaluate_payload", lambda payload: {"articles": [{"id": 1}]})

    radar = api._external_worker_payload(
        {"id": 1, "kind": "signal_discovery", "queue_name": lanes.AGENTS, "payload_json": {"web_only": True}}
    )
    evaluation = api._external_worker_payload(
        {"id": 2, "kind": "source_candidate_evaluate", "queue_name": lanes.AGENTS, "payload_json": {"candidate_id": 1}}
    )

    assert radar == {"snapshot": {"topics": ["Бурение"]}}
    assert evaluation == {"articles": [{"id": 1}]}


# --- Сторож ------------------------------------------------------------------------------


def _queue(*, queued: int, ready_minutes: float, activity_minutes: float | None, running: int = 0) -> dict:
    return {
        "totals": {"expired_leases": 0},
        "queues": [{
            "queue_name": lanes.AGENTS,
            "queued": queued,
            "running": running,
            "finalizing": 0,
            "oldest_ready_at": NOW - timedelta(minutes=ready_minutes),
            "last_activity_at": None if activity_minutes is None else NOW - timedelta(minutes=activity_minutes),
        }],
    }


def test_watchdog_flags_agents_lane_without_worker():
    alerts = lanes.lane_alerts(_queue(queued=1, ready_minutes=20, activity_minutes=None), now=NOW)

    assert [alert["kind"] for alert in alerts] == ["no_consumer"]


def test_watchdog_lets_evaluation_wait_behind_radar_run():
    # Один поток: оценка кандидата ждёт, пока идёт прогон радара, — это норма, не застой.
    alerts = lanes.lane_alerts(_queue(queued=3, ready_minutes=50, activity_minutes=1, running=1), now=NOW)

    assert alerts == []


# --- Воркер ------------------------------------------------------------------------------


class _Client:
    worker_id = "nl-agents-1"

    def __init__(self):
        self.completed: list = []
        self.failed: list = []

    def fork(self):
        return self

    def heartbeat(self, job):
        pass

    def progress(self, job, progress):
        pass

    def complete(self, job, result):
        self.completed.append(result)

    def fail(self, job, error, *, retryable=True, retry_after_seconds=300):
        self.failed.append((error, retryable))


def _steady_handler(payload, heartbeat=None):
    for _ in range(30):  # 0,6 с работы — вшестеро дольше предела без продвижения
        heartbeat()
        time.sleep(0.02)
    return {"ok": 1}


@pytest.mark.parametrize("kind", ["signal_discovery", "source_candidate_evaluate"])
def test_long_agent_job_with_steady_heartbeats_is_not_restarted(monkeypatch, kind):
    """Сторож зависания MVP-1 (LeaseKeeper) считает продвижением только beat(). Агентные
    обработчики звали голый heartbeat: прогон радара дольше 20 мин снимался бы как
    зависший — задача в повтор, процесс на перезапуск, оплаченная работа в мусор."""
    from oiltech_digest import signal_discovery

    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(external_worker.config, "EXTERNAL_JOB_MAX_SECONDS", 0.1)
    monkeypatch.setattr(external_worker, "_DRAINING", threading.Event())
    exits: list = []
    monkeypatch.setattr(external_worker.os, "_exit", exits.append)
    monkeypatch.setattr(signal_discovery, "process_external_payload", _steady_handler)
    monkeypatch.setattr(external_worker.external_ai, "process_source_candidate_payload", _steady_handler)
    client = _Client()

    external_worker._handle_job(client, {"id": 9, "kind": kind, "lease_token": "t", "payload": {}})

    assert exits == [] and client.failed == []
    assert client.completed == [{"ok": 1}]


# --- Раскладка NL ------------------------------------------------------------------------


def test_agents_lane_has_single_thread_worker_with_search_on_nl():
    compose = Path(__file__).resolve().parents[1] / "docker-compose.external-worker.yml"
    services = yaml.safe_load(compose.read_text())["services"]
    env = next(
        service["environment"] for service in services.values()
        if service["environment"]["EXTERNAL_WORKER_QUEUES"] == lanes.AGENTS
    )

    assert env.get("EXTERNAL_WORKER_CONCURRENCY", "1") == "1"
    assert env["EXTERNAL_WORKER_CAPABILITIES"] == "openai"
    # Без поискового провайдера радар не падает, а молча находит ноль (CLAUDE.md, 18.09).
    assert env["SOURCE_DISCOVERY_SEARCH_PROVIDER"] == "brave"
