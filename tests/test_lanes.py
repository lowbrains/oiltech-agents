"""Полосы исполнения (21.09): таблица очередей, отказ на входе, резерв статей, сторож.

Каждый тест падает на коде до правки."""

from datetime import datetime, timedelta, timezone

import pytest

from oiltech_digest import api, cli, lanes, network_policy
from oiltech_digest.db import connection, repository

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


# --- Отказ на входе --------------------------------------------------------------------


def test_external_queue_refuses_kind_its_worker_cannot_run(isolated_db):
    # 13–17.09 радар ставили в external-ai, где его никто не умел исполнять: 134 падения.
    with pytest.raises(ValueError, match="не обслуживает"):
        repository.create_background_job("signal_discovery", {}, queue_name="external-ai", execution_region="external")
    with pytest.raises(ValueError, match="не обслуживает"):
        repository.create_background_job("scrape_source", {"source_id": 1}, queue_name="external-ai")
    with pytest.raises(ValueError, match="такой полосы нет"):
        repository.create_background_job("scrape_source", {"source_id": 1}, queue_name="external-fetch2")

    assert repository.create_background_job("process_articles", {"limit": 5}, queue_name="external-ai-bulk")
    assert repository.create_background_job("anything_local", {}, queue_name="default")  # местные — без таблицы


def test_bulk_lane_job_gets_articles_like_live_lane(monkeypatch):
    """Раньше payload собирался только при queue_name == 'external-ai' — задача в новой
    полосе ушла бы воркеру сырым payload без статей."""
    built = []
    monkeypatch.setattr(
        api.external_ai,
        "build_process_articles_payload",
        lambda payload, job_id=None: built.append(job_id) or {"articles": [{"id": 1}]},
    )

    payload = api._external_worker_payload(
        {"id": 5, "kind": "process_articles", "queue_name": "external-ai-bulk", "payload_json": {"limit": 3}}
    )

    assert payload == {"articles": [{"id": 1}]}
    assert built == [5]


def test_bulk_routing_only_when_enabled(monkeypatch):
    monkeypatch.setattr(network_policy.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(network_policy.config, "AI_EXECUTION_REGION", "external")

    monkeypatch.setattr(network_policy.config, "AI_BULK_LANE_ENABLED", False)
    assert network_policy.route_ai_bulk().queue_name == "external-ai"  # пока на NL нет воркера полосы

    monkeypatch.setattr(network_policy.config, "AI_BULK_LANE_ENABLED", True)
    assert network_policy.route_ai_bulk().queue_name == "external-ai-bulk"

    monkeypatch.setattr(network_policy.config, "AI_EXECUTION_REGION", "ru")
    assert network_policy.route_ai_bulk().queue_name == "ai"  # местный контур не трогаем


def test_title_backfill_goes_to_bulk_lane(monkeypatch):
    monkeypatch.setattr(network_policy.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(network_policy.config, "AI_EXECUTION_REGION", "external")
    monkeypatch.setattr(network_policy.config, "AI_BULK_LANE_ENABLED", True)
    queues = []
    monkeypatch.setattr(repository, "article_ids_needing_title_ru", lambda: [1, 2, 3])
    monkeypatch.setattr(
        repository, "create_background_job", lambda kind, payload, **kwargs: queues.append(kwargs["queue_name"]) or {"id": 1}
    )

    cli.cmd_enqueue_translate(type("Args", (), {"limit": None, "batch_size": 2})())

    assert queues == ["external-ai-bulk", "external-ai-bulk"]


# --- Резерв статей при выдаче ------------------------------------------------------------


def _articles(count: int) -> list[int]:
    with connection.get_connection() as conn:
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('Lane Source', 'News', 'https://lane.example', TRUE, 'request') RETURNING id"
        ).fetchone()[0]
        conn.commit()
    for index in range(count):
        repository.insert_article({
            "source_id": source_id, "title": f"Статья {index}", "url": f"https://lane.example/{index}",
            "published_at": NOW - timedelta(hours=index), "raw_text": f"Статья {index}: бурение, контракт. " * 10,
            "text_truncated": False, "language": "ru", "content_hash": f"hash-{index}",
        })
    with connection.get_connection() as conn:
        ids = [int(row[0]) for row in conn.execute("SELECT id FROM articles ORDER BY published_at DESC").fetchall()]
    assert len(ids) == count  # иначе тест резерва прошёл бы вхолостую на одной статье
    return ids


def _running_job(payload: dict) -> int:
    job = repository.create_background_job("process_articles", payload, queue_name="external-ai")
    with connection.get_connection() as conn:
        conn.execute("UPDATE background_jobs SET status = 'running' WHERE id = %s", (int(job["id"]),))
        conn.commit()
    return int(job["id"])


def test_two_ai_lanes_never_get_the_same_articles(isolated_db):
    ids = _articles(5)
    live, bulk = _running_job({"limit": 3}), _running_job({"limit": 3})

    first = repository.reserve_process_articles(live, limit=3)
    second = repository.reserve_process_articles(bulk, limit=3)

    assert first == ids[:3]
    assert second == ids[3:]  # не 3 статьи, а те 2, что ещё свободны


def test_recount_waits_for_job_holding_old_text_instead_of_dropping_article(isolated_db):
    """Ревью 21.09: урезанный пересчёт оставлял статью посчитанной по обрывку, который
    держал пакет дня. Теперь выдача откладывается, а список идёт целиком после соседки."""
    ids = _articles(4)
    live = _running_job({"limit": 2})
    repository.reserve_process_articles(live, limit=2)
    recount = _running_job({"article_ids": [ids[0], ids[3]]})

    with pytest.raises(repository.ArticlesBusy):
        repository.reserve_process_articles(recount, limit=2, article_ids=[ids[0], ids[3]])

    with connection.get_connection() as conn:
        conn.execute("UPDATE background_jobs SET status = 'ok' WHERE id = %s", (live,))
        conn.commit()
    assert repository.reserve_process_articles(recount, limit=2, article_ids=[ids[0], ids[3]]) == [ids[0], ids[3]]


def test_null_article_ids_in_running_job_do_not_break_claims(isolated_db):
    """Ревью 21.09: "article_ids": null (так пишет /api/jobs/process) — jsonb null, а не SQL
    NULL; пока такая задача выполнялась, любая выдача ИИ-пакета падала 500."""
    ids = _articles(3)
    _running_job({"article_ids": None, "limit": 5})  # выдана, резерв ещё не записан
    job = _running_job({"limit": 2})

    assert repository.reserve_process_articles(job, limit=2) == ids[:2]


def test_busy_recount_goes_back_to_queue_without_spending_attempt(isolated_db, monkeypatch):
    from fastapi.testclient import TestClient

    ids = _articles(2)
    live = _running_job({"limit": 1})
    repository.reserve_process_articles(live, limit=1)
    recount = repository.create_background_job("process_articles", {"article_ids": [ids[0]], "limit": 1},
                                               queue_name="external-ai-bulk", execution_region="external",
                                               capability="openai")
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))

    response = TestClient(api.app).post(
        "/api/external-worker/claim",
        headers={"Authorization": "Bearer secret"},
        json={"worker_id": "nl-ai-bulk-1", "queues": ["external-ai-bulk"], "capabilities": ["openai"]},
    )

    assert response.status_code == 200 and response.json() == {"job": None}
    stored = repository.get_background_job(int(recount["id"]))
    assert stored["status"] == "queued" and stored["attempts"] == 0
    assert stored["run_after"] > datetime.now(timezone.utc)


def test_bulk_lane_refuses_relevance_recheck(isolated_db):
    """Перепроверка удаляет статьи; резерв защищает только пакет×пакет — в полосу
    пересчётов её не ставим, она идёт потоком дня (ревью 21.09, п.5)."""
    with pytest.raises(ValueError, match="не обслуживает"):
        repository.create_background_job("recheck_relevance", {"article_ids": [1]}, queue_name="external-ai-bulk")


# --- Сторож ------------------------------------------------------------------------------


def _status(*rows: dict, expired: int = 0) -> dict:
    return {"totals": {"expired_leases": expired}, "queues": list(rows)}


def _row(queue: str, *, queued: int, ready_minutes: float | None, activity_minutes: float | None, running: int = 0):
    return {
        "queue_name": queue, "queued": queued, "running": running, "finalizing": 0,
        "oldest_ready_at": None if ready_minutes is None else _ago(ready_minutes),
        "last_activity_at": None if activity_minutes is None else _ago(activity_minutes),
    }


def test_watchdog_flags_queue_nobody_takes():
    # 20.09: 208 задач сбора у агентов ждали потребителя, которого не было.
    alerts = lanes.lane_alerts(_status(_row("external-fetch", queued=208, ready_minutes=20, activity_minutes=None)), now=NOW)

    assert [alert["kind"] for alert in alerts] == ["no_consumer"]


def test_watchdog_flags_congested_live_ai_lane():
    # 18.09: поток дня ждал за пересчётами часами, хотя воркер работал.
    alerts = lanes.lane_alerts(_status(_row("external-ai", queued=3, ready_minutes=45, activity_minutes=1, running=1)), now=NOW)

    assert [alert["kind"] for alert in alerts] == ["stale"]
    assert alerts[0]["minutes"] == 45


def test_watchdog_keeps_quiet_on_fresh_burst_and_delayed_retries():
    alerts = lanes.lane_alerts(
        _status(
            # пачка только что пришла после 40 мин тишины — норма
            _row("external-fetch", queued=21, ready_minutes=2, activity_minutes=40),
            # отложенный повтор (run_after в будущем) — готовых задач нет
            _row("external-playwright", queued=1, ready_minutes=None, activity_minutes=300),
            # пересчёт ждёт 2 ч — для своей полосы это норма
            _row("external-ai-bulk", queued=5, ready_minutes=120, activity_minutes=3, running=1),
        ),
        now=NOW,
    )

    assert alerts == []


def test_watchdog_flags_unknown_queue_and_expired_leases():
    alerts = lanes.lane_alerts(
        _status(_row("external-fetch2", queued=4, ready_minutes=1, activity_minutes=None), expired=2), now=NOW
    )

    assert sorted(alert["kind"] for alert in alerts) == ["expired_leases", "unknown_queue"]


def test_queue_status_counts_ready_time_from_run_after(isolated_db):
    stale = repository.create_background_job("scrape_source", {"source_id": 1}, queue_name="external-fetch")
    delayed = repository.create_background_job("refetch_text", {}, queue_name="external-playwright")
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET created_at = now() - interval '40 minutes', "
            "run_after = now() - interval '40 minutes' WHERE id = %s", (int(stale["id"]),)
        )
        conn.execute("UPDATE background_jobs SET run_after = now() + interval '10 minutes' WHERE id = %s", (int(delayed["id"]),))
        conn.commit()

    status = repository.external_queue_status()

    by_queue = {row["queue_name"]: row for row in status["queues"]}
    assert by_queue["external-playwright"]["oldest_ready_at"] is None
    assert [(alert["queue"], alert["kind"]) for alert in status["alerts"]] == [("external-fetch", "no_consumer")]


def test_check_lanes_exits_2_with_alert_lines(monkeypatch, capsys):
    monkeypatch.setattr(
        repository, "external_queue_status",
        lambda: {"alerts": [{"kind": "no_consumer", "queue": "external-fetch", "count": 3, "message": "нет воркера"}]},
    )

    with pytest.raises(SystemExit) as exit_info:
        cli.cmd_check_lanes(None)

    assert exit_info.value.code == 2
    assert "ТРЕВОГА нет воркера" in capsys.readouterr().out


def test_check_lanes_quiet_when_all_lanes_healthy(monkeypatch, capsys):
    monkeypatch.setattr(repository, "external_queue_status", lambda: {"alerts": []})

    cli.cmd_check_lanes(None)

    assert "check-lanes: ok" in capsys.readouterr().out


def test_diagnose_of_external_source_stays_on_core(monkeypatch):
    """Внешний воркер не умеет diagnose_source: с 17.09 задача падала на NL, а с отказом
    на входе кнопка «Диагностика» вернула бы 500."""
    monkeypatch.setattr(network_policy.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(network_policy.config, "FETCH_EXTERNAL_ENABLED", True)
    source = {"parse_strategy": "request", "network_region": "external"}

    diagnose = network_policy.route_source_task(source, task_kind="diagnose")
    scrape = network_policy.route_source_task(source, task_kind="scrape")

    assert diagnose.queue_name == "default"
    assert scrape.queue_name == "external-fetch"
    assert lanes.serves(scrape.queue_name, "scrape_source")
