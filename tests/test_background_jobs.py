from datetime import datetime, timedelta, timezone

from oiltech_digest import background_jobs
from oiltech_digest.db import connection
from oiltech_digest.db import repository


def test_background_job_run_records_success(monkeypatch, isolated_db):
    job = repository.create_background_job("test_success", {"value": 3})

    monkeypatch.setitem(
        background_jobs._HANDLERS,
        "test_success",
        lambda payload, job_id: {"doubled": payload["value"] * 2},
    )

    background_jobs.run(int(job["id"]))

    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "ok"
    assert stored["progress"] == 100
    assert stored["result_json"] == {"doubled": 6}
    assert stored["error_message"] is None
    assert stored["started_at"] is not None
    assert stored["finished_at"] is not None


def test_background_job_run_records_failure(monkeypatch, isolated_db):
    job = repository.create_background_job("test_failure", {}, max_attempts=1)

    def fail(payload, job_id):
        raise RuntimeError("boom")

    monkeypatch.setitem(background_jobs._HANDLERS, "test_failure", fail)

    background_jobs.run(int(job["id"]))

    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "failed"
    assert stored["error_message"] == "boom"
    assert stored["started_at"] is not None
    assert stored["finished_at"] is not None


def test_background_job_run_requeues_retryable_failure(monkeypatch, isolated_db):
    job = repository.create_background_job("test_retry", {}, max_attempts=3)

    def fail(payload, job_id):
        raise RuntimeError("temporary")

    monkeypatch.setattr(background_jobs.config, "BACKGROUND_JOB_RETRY_BASE_SECONDS", 5)
    monkeypatch.setitem(background_jobs._HANDLERS, "test_retry", fail)

    background_jobs.run(int(job["id"]))

    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "queued"
    assert stored["attempts"] == 1
    assert stored["error_message"] == "temporary"
    assert stored["run_after"] is not None


def test_process_job_without_article_ids_uses_pipeline_queue(monkeypatch):
    calls = []

    monkeypatch.setattr(background_jobs, "make_client", lambda offline: object())
    monkeypatch.setattr(background_jobs.repository, "update_background_job_progress", lambda job_id, progress: None)
    monkeypatch.setattr(background_jobs.repository, "mark_background_job_ai_started", lambda job_id: None)
    monkeypatch.setattr(
        background_jobs.repository,
        "get_articles_needing_pipeline",
        lambda limit: calls.append(("pipeline_queue", limit)) or [{"id": 1}],
    )
    monkeypatch.setattr(
        background_jobs,
        "process_pipeline_articles",
        lambda articles, client, fetch_full=True: calls.append(("pipeline", [a["id"] for a in articles], fetch_full))
        or {"processed": len(articles), "relevant": 1, "rejected": 0},
    )

    result = background_jobs._run_process_articles({"limit": 10}, job_id=123)

    assert calls == [
        ("pipeline_queue", 10),
        ("pipeline", [1], True),
    ]
    assert result["pipeline"] == {"processed": 1, "relevant": 1, "rejected": 0}


def test_process_job_marks_ai_started_before_first_model_call(monkeypatch):
    """ai_started_at ОБЯЗАН ставиться до первого вызова модели.

    На этом порядке держится вся защита от двойной оплаты OpenAI: если пометить позже
    (или после process_pipeline_articles), то падение на первой же стадии оставит
    ai_started_at IS NULL, requeue вернёт задачу в очередь и уже оплаченные summary
    прогонятся через модель повторно. Мокаем реальные вызовы и проверяем ПОРЯДОК.
    """
    order = []

    monkeypatch.setattr(background_jobs, "make_client", lambda offline: object())
    monkeypatch.setattr(background_jobs.repository, "update_background_job_progress", lambda job_id, progress: None)
    monkeypatch.setattr(
        background_jobs.repository,
        "mark_background_job_ai_started",
        lambda job_id: order.append("ai_started"),
    )
    monkeypatch.setattr(background_jobs.repository, "get_articles_needing_pipeline", lambda limit: [{"id": 1}])
    monkeypatch.setattr(
        background_jobs,
        "process_pipeline_articles",
        lambda articles, client, fetch_full=True: order.append("model_call") or {"processed": len(articles)},
    )

    background_jobs._run_process_articles({"limit": 10}, job_id=123)

    assert "ai_started" in order, "ai_started_at не был помечен вовсе"
    assert order.index("ai_started") < order.index("model_call"), (
        "ai_started_at помечен ПОСЛЕ первого вызова модели — защита от двойной оплаты дырявая"
    )


def test_enqueue_daily_signal_discovery_skips_existing_daily_job(monkeypatch):
    called = []

    monkeypatch.setattr(background_jobs.config, "SIGNAL_DISCOVERY_DAILY_ENABLED", True)
    monkeypatch.setattr(
        background_jobs.repository,
        "has_recent_background_job",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(background_jobs, "enqueue", lambda *args, **kwargs: called.append((args, kwargs)))

    result = background_jobs.enqueue_daily_signal_discovery()

    assert result["enqueued"] is False and result["reason"] == "already_scheduled"
    assert 0 < result["lookback_hours"] <= 24  # окно — с полуночи по Москве
    assert called == []


def test_enqueue_daily_signal_discovery_creates_web_only_ai_job(monkeypatch):
    captured = {}

    monkeypatch.setattr(background_jobs.config, "SIGNAL_DISCOVERY_DAILY_ENABLED", True)
    monkeypatch.setattr(background_jobs.config, "SIGNAL_DISCOVERY_DAYS", 14)
    monkeypatch.setattr(background_jobs.config, "SIGNAL_DISCOVERY_LIMIT", 120)
    monkeypatch.setattr(background_jobs.config, "SIGNAL_DISCOVERY_MIN_SCORE", 40)
    monkeypatch.setattr(background_jobs.config, "SIGNAL_DISCOVERY_MAX_SIGNALS", 20)
    monkeypatch.setattr(background_jobs.config, "SIGNAL_DISCOVERY_WEB_QUERY_LIMIT", 8)
    monkeypatch.setattr(
        background_jobs.repository,
        "has_recent_background_job",
        lambda **kwargs: False,
    )

    def fake_enqueue(kind, payload, **kwargs):
        captured.update({"kind": kind, "payload": payload, **kwargs})
        return {"id": 92, "kind": kind, "queue_name": kwargs["queue_name"], "payload_json": payload}

    monkeypatch.setattr(background_jobs, "enqueue", fake_enqueue)

    result = background_jobs.enqueue_daily_signal_discovery()

    assert result["enqueued"] is True
    assert captured["kind"] == "signal_discovery"
    assert captured["queue_name"] == "ai"
    assert captured["capability"] == "openai"
    assert captured["max_attempts"] == 1
    assert captured["payload"]["offline"] is False
    assert captured["payload"]["web_search"] is True
    assert captured["payload"]["web_only"] is True
    assert captured["payload"]["max_signals"] == 20
    assert captured["payload"]["schedule"] == "daily_signal_discovery"


def test_discover_source_candidates_job_uses_topic_gaps_and_evaluates(monkeypatch):
    from oiltech_digest.source_discovery import agent
    from oiltech_digest.source_discovery import sandbox

    progress = []
    configs = []

    monkeypatch.setattr(
        background_jobs.repository,
        "update_background_job_progress",
        lambda job_id, value: progress.append((job_id, value)),
    )
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit: [{"topic": "роботизация бурения"}])

    def fake_discover(config):
        configs.append(config)
        return {
            "task_id": 7,
            "search": {"status": "ok"},
            "candidates": [
                {
                    "id": 42,
                    "url": "https://example.com/newsroom",
                    "recommended_action": "add",
                }
            ],
        }

    monkeypatch.setattr(agent, "discover_sources", fake_discover)
    monkeypatch.setattr(
        sandbox,
        "evaluate_source_candidate",
        lambda candidate_id, article_limit, offline, collect, process: {
            "candidate_id": candidate_id,
            "metrics": {"tested_articles": article_limit, "relevant_articles": 3},
            "recommended_action": "add",
            "next_status": "needs_human_review",
        },
    )

    result = background_jobs._run_discover_source_candidates(
        {
            "topic_limit": 1,
            "limit": 5,
            "offline": True,
            "auto_evaluate": True,
            "article_limit": 4,
        },
        job_id=123,
    )

    assert result["topics"] == ["роботизация бурения"]
    assert result["candidates"] == 1
    assert result["evaluated"] == 1
    assert result["results"][0]["candidates"][0]["id"] == 42
    assert result["results"][0]["evaluations"][0]["metrics"]["tested_articles"] == 4
    assert configs[0].topic == "роботизация бурения"
    assert configs[0].limit == 5
    assert configs[0].offline is True
    assert progress[0] == (123, 10)
    assert progress[-1] == (123, 95)


def test_discover_source_candidates_job_enqueues_external_evaluation(monkeypatch):
    from oiltech_digest.source_discovery import agent

    jobs = []

    monkeypatch.setattr(background_jobs.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(background_jobs.config, "AI_EXECUTION_REGION", "external")
    monkeypatch.setattr(background_jobs.repository, "update_background_job_progress", lambda job_id, value: None)
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit: [{"topic": "роботизация бурения"}])
    monkeypatch.setattr(
        agent,
        "discover_sources",
        lambda config: {
            "task_id": 7,
            "search": {"status": "ok"},
            "candidates": [{"id": 42, "url": "https://example.com/newsroom", "recommended_action": "add"}],
        },
    )
    monkeypatch.setattr(
        background_jobs.repository,
        "create_background_job",
        lambda kind, payload, **kwargs: jobs.append({"kind": kind, "payload": payload, **kwargs}) or {"id": 99},
    )

    result = background_jobs._run_discover_source_candidates(
        {
            "topic_limit": 1,
            "limit": 5,
            "offline": False,  # флажок «Без ИИ» снят — оценка в очередь ИИ
            "auto_evaluate": True,
            "article_limit": 4,
        },
        job_id=123,
    )

    assert result["evaluated"] == 0
    assert result["evaluation_jobs"] == 1
    assert jobs[0]["agent_run_id"] is None
    assert jobs == [
        {
            "kind": "source_candidate_evaluate",
            "payload": {
                "candidate_id": 42,
                "article_limit": 4,
                "offline": False,
                "collect": True,
                "process": True,
            },
            "queue_name": "external-ai",
            "execution_region": "external",
            "capability": "openai",
            "max_attempts": 1,
            "agent_run_id": None,
        }
    ]


def test_source_discovery_plan_job_builds_plan_and_queues_actions(monkeypatch):
    from oiltech_digest.source_discovery import planner

    progress = []
    config_seen = {}
    finished = []

    monkeypatch.setattr(
        background_jobs.repository,
        "update_background_job_progress",
        lambda job_id, value: progress.append((job_id, value)),
    )
    monkeypatch.setattr(background_jobs.repository, "create_agent_run", lambda *args, **kwargs: 555)
    monkeypatch.setattr(
        background_jobs.repository,
        "finish_agent_run",
        lambda run_id, **kwargs: finished.append({"run_id": run_id, **kwargs}),
    )

    def fake_build_plan(config):
        config_seen.update({
            "days": config.days,
            "target_per_topic": config.target_per_topic,
            "topic_limit": config.topic_limit,
            "candidate_limit": config.candidate_limit,
            "max_actions": config.max_actions,
            "persist_memory": config.persist_memory,
            "run_id": config.run_id,
        })
        return {"actions": [{"action_type": "discover_sources", "topic": "бурение", "priority": 90, "limit": 5}]}

    monkeypatch.setattr(planner, "build_plan", fake_build_plan)
    monkeypatch.setattr(
        planner,
        "enqueue_plan_actions",
        lambda plan, offline, evaluate, run_id=None: {
            "queued": 1,
            "jobs": [{"job_id": 77, "topic": "бурение", "run_id": run_id}],
        },
    )

    result = background_jobs._run_source_discovery_plan(
        {
            "days": 14,
            "target_per_topic": 8,
            "topic_limit": 2,
            "candidate_limit": 5,
            "max_actions": 3,
            "persist_memory": False,
            "offline": True,
            "evaluate": False,
        },
        job_id=123,
    )

    assert config_seen == {
        "days": 14,
        "target_per_topic": 8,
        "topic_limit": 2,
        "candidate_limit": 5,
        "max_actions": 3,
        "persist_memory": False,
        "run_id": 555,
    }
    assert result["run_id"] == 555
    assert result["queued"]["queued"] == 1
    assert result["queued"]["jobs"][0]["run_id"] == 555
    assert finished[0]["run_id"] == 555
    assert finished[0]["status"] == "ok"
    assert progress == [(123, 20), (123, 70), (123, 95)]


def test_source_discovery_loop_job_runs_loop(monkeypatch):
    from oiltech_digest.source_discovery import loop

    progress = []
    captured = {}
    monkeypatch.setattr(background_jobs.repository, "update_background_job_progress", lambda job_id, value: progress.append((job_id, value)))
    monkeypatch.setattr(
        loop,
        "run_agent_loop",
        lambda config: captured.update({"config": config}) or {
            "run_id": 12,
            "iterations": [],
            "total_candidates": 0,
            "terminal_reason": "no_auto_actions",
        },
    )

    result = background_jobs._HANDLERS["source_discovery_loop"](
        {"goal": "найти", "max_iterations": 2, "max_actions": 3},
        99,
    )

    assert result["run_id"] == 12
    assert captured["config"].goal == "найти"
    assert captured["config"].max_iterations == 2
    assert captured["config"].max_actions == 3
    assert captured["config"].test_parse is True
    assert progress == [(99, 10), (99, 95)]


def test_parse_source_once_job_routes_rss_source(monkeypatch):
    progress = []
    monkeypatch.setattr(background_jobs.repository, "get_source", lambda source_id: {"id": source_id, "parse_strategy": "rss"})
    monkeypatch.setattr(background_jobs.repository, "update_background_job_progress", lambda job_id, value: progress.append((job_id, value)))
    monkeypatch.setattr(background_jobs.rss_parser, "parse_source", lambda source: {"added": 2, "attempted": 3})

    result = background_jobs._HANDLERS["parse_source_once"]({"source_id": 42}, 99)

    assert result == {"source_id": 42, "strategy": "rss", "stats": {"added": 2, "attempted": 3}}
    assert progress == [(99, 20), (99, 90)]


def test_enqueue_can_skip_inline_execution(monkeypatch, isolated_db):
    submitted = []
    monkeypatch.setattr(background_jobs.config, "BACKGROUND_JOB_INLINE", False)
    monkeypatch.setattr(background_jobs._executor, "submit", lambda *args, **kwargs: submitted.append(args))
    monkeypatch.setitem(background_jobs._HANDLERS, "test_queued", lambda payload, job_id: {"ok": True})

    job = background_jobs.enqueue("test_queued", {"x": 1})

    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "queued"
    assert submitted == []


def test_background_job_records_execution_metadata(isolated_db):
    job = repository.create_background_job(
        "process_articles",
        {"value": 1},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
    )

    stored = repository.get_background_job(int(job["id"]))

    assert stored["queue_name"] == "external-ai"
    assert stored["execution_region"] == "external"
    assert stored["capability"] == "openai"


def test_claim_next_background_job_marks_oldest_queued_job_running(isolated_db):
    first = repository.create_background_job("test_first", {}, queue_name="default")
    repository.create_background_job("test_second", {}, queue_name="playwright")

    claimed = repository.claim_next_background_job(queue_names=["default"])

    assert claimed["id"] == first["id"]
    assert claimed["status"] == "running"
    assert claimed["started_at"] is not None
    assert repository.get_background_job(int(first["id"]))["status"] == "running"
    assert repository.get_background_job(int(first["id"]))["attempts"] == 1


def test_claim_next_background_job_filters_by_queue(isolated_db):
    repository.create_background_job("test_default", {}, queue_name="default")
    playwright = repository.create_background_job("test_playwright", {}, queue_name="playwright")

    claimed = repository.claim_next_background_job(queue_names=["playwright"])

    assert claimed["id"] == playwright["id"]
    assert claimed["queue_name"] == "playwright"


def test_claim_external_background_job_sets_lease_metadata(isolated_db):
    default = repository.create_background_job("test_default", {}, queue_name="default")
    external = repository.create_background_job(
        "process_articles",
        {},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
    )

    claimed = repository.claim_external_background_job(
        queue_names=["external-ai"],
        capabilities=["openai"],
        worker_id="eu-worker-1",
        lease_token_hash="hash1",
        lease_seconds=600,
    )

    assert claimed["id"] == external["id"]
    assert claimed["status"] == "running"
    assert claimed["claimed_by"] == "eu-worker-1"
    assert claimed["lease_token_hash"] == "hash1"
    assert claimed["lease_expires_at"] is not None
    assert repository.get_background_job(int(default["id"]))["status"] == "queued"


def test_external_job_progress_complete_and_wrong_lease(isolated_db):
    job = repository.create_background_job(
        "process_articles",
        {},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
    )
    claimed = repository.claim_external_background_job(
        queue_names=["external-ai"],
        capabilities=["openai"],
        worker_id="eu-worker-1",
        lease_token_hash="hash1",
        lease_seconds=600,
    )

    assert repository.update_external_background_job_progress(int(claimed["id"]), lease_token_hash="wrong", progress=50) is False
    assert repository.external_background_job_lease_is_active(int(claimed["id"]), lease_token_hash="wrong") is False
    assert repository.external_background_job_lease_is_active(int(claimed["id"]), lease_token_hash="hash1") is True
    assert repository.update_external_background_job_progress(int(claimed["id"]), lease_token_hash="hash1", progress=50) is True
    assert repository.finish_external_background_job(int(claimed["id"]), lease_token_hash="wrong", result={"ok": True}) is False
    assert repository.finish_external_background_job(int(claimed["id"]), lease_token_hash="hash1", result={"ok": True}) is True

    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "ok"
    assert stored["progress"] == 100
    assert stored["result_json"] == {"ok": True}
    assert stored["lease_token_hash"] is None


def test_external_job_retryable_fail_requeues(isolated_db):
    job = repository.create_background_job(
        "process_articles",
        {},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
        max_attempts=3,
    )
    repository.claim_external_background_job(
        queue_names=["external-ai"],
        capabilities=["openai"],
        worker_id="eu-worker-1",
        lease_token_hash="hash1",
        lease_seconds=600,
    )

    assert repository.fail_external_background_job(
        int(job["id"]),
        lease_token_hash="hash1",
        error_message="temporary",
        retryable=True,
        retry_delay_seconds=120,
    ) is True

    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "queued"
    assert stored["error_message"] == "temporary"
    assert stored["lease_token_hash"] is None


def test_requeue_expired_external_leases(isolated_db):
    job = repository.create_background_job(
        "process_articles",
        {},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
    )
    repository.claim_external_background_job(
        queue_names=["external-ai"],
        capabilities=["openai"],
        worker_id="eu-worker-1",
        lease_token_hash="hash1",
        lease_seconds=600,
    )
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = %s",
            (job["id"],),
        )
        conn.commit()

    outcome = repository.requeue_expired_external_leases()
    assert (outcome.requeued, outcome.exhausted) == (1, 0)
    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "queued"
    assert stored["claimed_by"] is None
    assert stored["lease_token_hash"] is None


def test_external_queue_status_summarizes_external_jobs(isolated_db):
    repository.create_background_job(
        "process_articles",
        {},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
    )
    repository.create_background_job("test_local", {}, queue_name="default")

    status = repository.external_queue_status()

    assert status["totals"]["queued"] == 1
    assert status["totals"]["running"] == 0
    assert status["queues"][0]["queue_name"] == "external-ai"
    assert status["queues"][0]["queued"] == 1


def test_claim_next_background_job_skips_delayed_retry(isolated_db):
    job = repository.create_background_job("test_delayed", {}, queue_name="default")
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET run_after = now() + interval '10 minutes' WHERE id = %s",
            (job["id"],),
        )
        conn.commit()

    assert repository.claim_next_background_job(queue_names=["default"]) is None


def test_requeue_stale_background_jobs_recovers_stuck_running_job(isolated_db):
    job = repository.create_background_job("test_stale", {}, queue_name="default")
    claimed = repository.claim_next_background_job(queue_names=["default"])
    assert claimed["status"] == "running"

    stale_started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET started_at = %s WHERE id = %s",
            (stale_started_at, job["id"]),
        )
        conn.commit()

    outcome = repository.requeue_stale_background_jobs(stale_minutes=60)
    stored = repository.get_background_job(int(job["id"]))

    assert (outcome.requeued, outcome.exhausted) == (1, 0)
    assert stored["status"] == "queued"
    assert stored["progress"] == 0
    assert stored["started_at"] is None
    assert stored["error_message"] == "Requeued after stale running/finalizing timeout"


def test_requeue_stale_does_not_rerun_local_ai_job_that_already_spent_money(isolated_db):
    """Зависшая ЛОКАЛЬНАЯ AI-обработка, уже начавшая жечь OpenAI, не перезапускается.

    requeue переиспользует тот же job_id, а get_articles_by_ids не пропускает уже
    обработанные статьи — повторный прогон означает повторный РЕАЛЬНЫЙ расход.
    Дедуп биллинга по (job_id, article_id, stage) тут не помог бы: он лишь СПРЯТАЛ бы
    второй, реально оплаченный вызов из отчёта о стоимости. Поэтому помечаем failed
    без авто-ретрая. Внешний контур (external-ai) не затрагиваем — у него свой lease.

    Дискриминатор — ai_started_at, НЕ progress: claim/mark-running форсят progress в 10
    ещё до тела обработчика, поэтому «до-AI» кейс здесь реалистичен (running, progress=10,
    ai_started_at IS NULL) — ровно он ломался у прежнего гарда `progress > 0`.
    """
    stale_started_at = datetime.now(timezone.utc) - timedelta(hours=2)

    def make_stale_running(
        kind: str, queue_name: str, ai_started: bool, execution_region: str = "ru"
    ) -> dict:
        job = repository.create_background_job(
            kind, {}, queue_name=queue_name, execution_region=execution_region
        )
        # claim переводит в running и сам поднимает progress до 10 (как в проде).
        repository.claim_next_background_job(queue_names=[queue_name])
        with connection.get_connection() as conn:
            conn.execute(
                "UPDATE background_jobs SET started_at = %s, ai_started_at = %s WHERE id = %s",
                (stale_started_at, stale_started_at if ai_started else None, job["id"]),
            )
            conn.commit()
        return job

    burned = make_stale_running("process_articles", "default", ai_started=True)
    not_started = make_stale_running("process_articles", "default", ai_started=False)
    external = make_stale_running(
        "process_articles", "external-ai", ai_started=True, execution_region="external"
    )

    # Инвариант, на котором держится дискриминатор: у running-задачи progress уже >0
    # (claim поднял до 10), поэтому различать по нему «до/после AI» нельзя.
    assert repository.get_background_job(int(not_started["id"]))["progress"] > 0

    repository.requeue_stale_background_jobs(stale_minutes=60)

    # Уже потратила деньги (ai_started_at выставлен) → failed, без авто-возврата в очередь.
    burned_stored = repository.get_background_job(int(burned["id"]))
    assert burned_stored["status"] == "failed"
    assert "дважды" in (burned_stored["error_message"] or "")

    # Упала ДО первого вызова модели (ai_started_at IS NULL) → безопасно перезапустить.
    assert repository.get_background_job(int(not_started["id"]))["status"] == "queued"

    # Внешний контур не задет — у него собственная защита (lease/finalize, T2/H1).
    # Проверяем именно 'running': прежняя версия теста ждала 'queued', а это состояние
    # давали ОБА поведения — и «не тронули», и «переочередили». Ассерт был слеп ровно
    # к тому багу, который потом устроил вечную петлю 1181.
    assert repository.get_background_job(int(external["id"]))["status"] == "running"


def _claimed_external_job(*, max_attempts: int = 3, lease_seconds: int = 600) -> dict:
    job = repository.create_background_job(
        "process_articles",
        {"limit": 800},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
        max_attempts=max_attempts,
    )
    repository.claim_external_background_job(
        queue_names=["external-ai"],
        capabilities=["openai"],
        worker_id="nl-worker-1",
        lease_token_hash="hash-live",
        lease_seconds=lease_seconds,
    )
    return job


def test_stale_sweeper_does_not_touch_external_job_with_live_lease(isolated_db):
    """Регресс на вечную петлю 1181 (24.07).

    Батч 800 статей идёт ~93 минуты — дольше stale_minutes=60. Воркер при этом жив
    и продлевает lease heartbeat'ом перед каждой статьёй. Уборщик по настенным часам
    возвращал такую задачу в очередь вопреки живому lease: воркер получал 409, бросал
    батч, тут же забирал ту же задачу (она самая старая в очереди) и начинал с первой
    статьи. Ровно раз в час, вечно, каждый круг заново оплачивая OpenAI.

    Настоящий признак жизни внешней задачи — lease, и он здесь свежий. Значит уборщик
    по часам обязан пройти мимо.
    """
    job = _claimed_external_job()
    long_running = datetime.now(timezone.utc) - timedelta(hours=2)
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET started_at = %s WHERE id = %s",
            (long_running, job["id"]),
        )
        conn.commit()

    outcome = repository.requeue_stale_background_jobs(stale_minutes=60)

    assert (outcome.requeued, outcome.exhausted) == (0, 0)
    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "running"
    assert stored["lease_token_hash"] == "hash-live"

    # А по своему собственному сигналу — протухшему lease — она разбирается штатно.
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = %s",
            (job["id"],),
        )
        conn.commit()
    assert repository.requeue_expired_external_leases().requeued == 1
    assert repository.get_background_job(int(job["id"]))["status"] == "queued"


def test_external_job_running_without_lease_is_not_orphaned(isolated_db):
    """У внешней задачи осталась ОДНА страховка — lease, значит она обязана крыть всё поле.

    `release_external_background_job_finalize` откатывает finalizing→running и рассчитывает,
    что задачу подберёт путь восстановления. Пока уборщик по часам тоже трогал внешние задачи,
    'running' без lease подобрал бы он. Теперь не подберёт — поэтому «lease IS NULL» обязано
    считаться потерей, иначе задача зависла бы в 'running' навсегда.
    """
    job = _claimed_external_job()
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET lease_expires_at = NULL WHERE id = %s",
            (job["id"],),
        )
        conn.commit()

    assert repository.requeue_expired_external_leases().requeued == 1
    assert repository.get_background_job(int(job["id"]))["status"] == "queued"


def test_lost_external_job_dies_after_max_attempts_instead_of_looping(isolated_db):
    """Потолок попыток обязан срабатывать и на переочереди, а не только в fail_background_job.

    У 1181 на проде было attempts=6 при max_attempts=3: переочередь не смотрела attempts,
    поэтому лимит не наступал НИКОГДА. Задача, которая не может завершиться, обязана
    умереть и дождаться человека, а не крутиться вечно.
    """
    job = _claimed_external_job(max_attempts=3)
    job_id = int(job["id"])

    def expire_lease() -> None:
        with connection.get_connection() as conn:
            conn.execute(
                "UPDATE background_jobs SET lease_expires_at = now() - interval '1 minute' "
                "WHERE id = %s",
                (job_id,),
            )
            conn.commit()

    def reclaim() -> None:
        repository.claim_external_background_job(
            queue_names=["external-ai"],
            capabilities=["openai"],
            worker_id="nl-worker-1",
            lease_token_hash="hash-live",
            lease_seconds=600,
        )

    # attempts=1 после первого claim. Два круга «протух lease → вернули в очередь → взяли снова».
    for _ in range(2):
        expire_lease()
        assert repository.requeue_expired_external_leases().requeued == 1
        reclaim()

    assert repository.get_background_job(job_id)["attempts"] == 3

    # Третий круг: попытки исчерпаны — задача закрывается, а не возвращается в очередь.
    expire_lease()
    outcome = repository.requeue_expired_external_leases()

    assert (outcome.requeued, outcome.exhausted) == (0, 1)
    stored = repository.get_background_job(job_id)
    assert stored["status"] == "failed"
    assert "исчерпаны попытки" in (stored["error_message"] or "")
    assert stored["lease_token_hash"] is None


def test_stale_local_job_dies_after_max_attempts(isolated_db):
    """Тот же потолок для локального контура: настенные часы тоже не должны крутить вечно."""
    job = repository.create_background_job(
        "test_stale", {}, queue_name="default", max_attempts=1
    )
    repository.claim_next_background_job(queue_names=["default"])
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET started_at = %s WHERE id = %s",
            (datetime.now(timezone.utc) - timedelta(hours=2), job["id"]),
        )
        conn.commit()

    outcome = repository.requeue_stale_background_jobs(stale_minutes=60)

    assert (outcome.requeued, outcome.exhausted) == (0, 1)
    assert repository.get_background_job(int(job["id"]))["status"] == "failed"


def test_worker_loop_sweep_reaps_expired_external_leases(monkeypatch, isolated_db):
    """T21: реапер lease обязан работать по расписанию, а не только внутри claim.

    Пока воркер занят длинной задачей, за новой он не приходит — значит и протухшие
    lease никто не разбирает. Умри воркер совсем, задачи висели бы 'running' вечно,
    и интерфейс показывал бы идущую работу, которой нет.
    """
    job = _claimed_external_job()
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = %s",
            (job["id"],),
        )
        conn.commit()

    # Локальный воркер обслуживает свою очередь и внешнюю задачу не исполняет,
    # но подметать протухшие lease обязан.
    background_jobs.worker_loop(once=True, poll_seconds=0, stale_minutes=60, queue_names=["default"])

    assert repository.get_background_job(int(job["id"]))["status"] == "queued"


def test_worker_loop_once_processes_queued_jobs(monkeypatch, isolated_db):
    first = repository.create_background_job("test_worker", {"value": 2})
    second = repository.create_background_job("test_worker", {"value": 4})

    monkeypatch.setitem(
        background_jobs._HANDLERS,
        "test_worker",
        lambda payload, job_id: {"value": payload["value"]},
    )

    background_jobs.worker_loop(once=True, poll_seconds=0, stale_minutes=60)

    assert repository.get_background_job(int(first["id"]))["status"] == "ok"
    assert repository.get_background_job(int(first["id"]))["result_json"] == {"value": 2}
    assert repository.get_background_job(int(second["id"]))["status"] == "ok"
    assert repository.get_background_job(int(second["id"]))["result_json"] == {"value": 4}


def test_discover_source_candidates_offline_does_not_enqueue_paid_ai(monkeypatch):
    # Флажок «Без ИИ» (offline=True): оценка по правилам на месте, в очередь ИИ — ничего.
    # Раньше флажок здесь не смотрелся, и с воркером агентов на NL каждое нажатие
    # «Поставить в очередь» стоило бы до 26 вызовов модели на кандидата.
    from oiltech_digest.source_discovery import agent

    jobs = []
    evaluated = []
    monkeypatch.setattr(background_jobs.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(background_jobs.config, "AI_EXECUTION_REGION", "external")
    monkeypatch.setattr(background_jobs.repository, "update_background_job_progress", lambda job_id, value: None)
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit: [{"topic": "роботизация бурения"}])
    monkeypatch.setattr(
        agent,
        "discover_sources",
        lambda config: {"task_id": 7, "search": {"status": "ok"},
                        "candidates": [{"id": 42, "url": "https://example.com/newsroom", "recommended_action": "add"}]},
    )
    monkeypatch.setattr(
        background_jobs.repository,
        "create_background_job",
        lambda kind, payload, **kwargs: jobs.append(kind) or {"id": 99},
    )
    from oiltech_digest.source_discovery import sandbox

    def fake_evaluate(candidate_id, **kwargs):
        evaluated.append((candidate_id, kwargs.get("offline")))
        return {"metrics": {}, "recommended_action": "review", "next_status": "review"}

    monkeypatch.setattr(sandbox, "evaluate_source_candidate", fake_evaluate)

    result = background_jobs._run_discover_source_candidates(
        {"topic_limit": 1, "limit": 5, "offline": True, "auto_evaluate": True, "article_limit": 4},
        job_id=123,
    )

    assert jobs == []
    assert result["evaluation_jobs"] == 0
    assert evaluated == [(42, True)]
