"""Small persistent background-job runner for heavy API operations.

This is intentionally lightweight: one process-local executor plus database
state. It gives the API stable job contracts now and leaves room to swap the
executor for Redis/Celery later without changing frontend-facing endpoints.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
import time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from oiltech_digest import config
from oiltech_digest.db import repository
from oiltech_digest.ingestion import playwright_parser, request_parser, rss_parser, telegram_parser
from oiltech_digest.ingestion.source_diagnostics import diagnose_source
from oiltech_digest.processing.digest import write_digest_export
from oiltech_digest.processing.pipeline import (
    make_client,
    process_pipeline_articles,
)

_executor = ThreadPoolExecutor(max_workers=max(1, config.BACKGROUND_JOB_WORKERS))
logger = logging.getLogger(__name__)
DAILY_SIGNAL_DISCOVERY_MARKER = "daily_signal_discovery"
# «Ежедневный» — это календарные сутки заказчика, а не «24 часа назад».
DAILY_SIGNAL_DISCOVERY_TZ = ZoneInfo("Europe/Moscow")


def daily_signal_discovery_payload() -> dict[str, Any]:
    return {
        "topic": None,
        "days": config.SIGNAL_DISCOVERY_DAYS,
        "limit": config.SIGNAL_DISCOVERY_LIMIT,
        "min_score": config.SIGNAL_DISCOVERY_MIN_SCORE,
        "offline": False,
        "dry_run": False,
        "max_signals": config.SIGNAL_DISCOVERY_MAX_SIGNALS,
        "web_search": True,
        "web_only": True,
        "web_query_limit": config.SIGNAL_DISCOVERY_WEB_QUERY_LIMIT,
        "trigger": "scheduler_daily",
        "schedule": DAILY_SIGNAL_DISCOVERY_MARKER,
    }


def _hours_since_local_midnight(now: datetime | None = None) -> float:
    local = (now or datetime.now(timezone.utc)).astimezone(DAILY_SIGNAL_DISCOVERY_TZ)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return round(max((local - midnight).total_seconds() / 3600, 0.01), 4)


def enqueue_daily_signal_discovery(*, force: bool = False) -> dict[str, Any]:
    if not config.SIGNAL_DISCOVERY_DAILY_ENABLED and not force:
        return {"enqueued": False, "reason": "disabled"}

    marker = {"schedule": DAILY_SIGNAL_DISCOVERY_MARKER}
    # С полуночи по Москве, а не «за 24 часа»: 18.09 запуск в 17:15 МСК заблокировал
    # утренний крон 19.09 (прошло 14 ч из 24), и радар в тот день не отработал.
    # При фиксированном часе крона окно ровно в 24 ч — ещё и гонка секунд.
    lookback_hours = _hours_since_local_midnight()
    # 'failed' — тоже попытка этого дня. Без него упавшая задача ставилась заново на
    # каждом цикле планировщика (30 мин): 134 запуска за 5 дней вместо 5. Пока падение
    # было мгновенным (403 на первом вызове), это ничего не стоило; падение в конце
    # прогона сжигало бы почти весь его бюджет каждые полчаса.
    if not force and repository.has_recent_background_job(
        kind="signal_discovery",
        payload_subset=marker,
        lookback_hours=lookback_hours,
        statuses=("queued", "running", "finalizing", "ok", "failed"),
    ):
        return {
            "enqueued": False,
            "reason": "already_scheduled",
            "lookback_hours": lookback_hours,
        }

    # Маршрут спрашиваем у политики, а не прибиваем к 'ru'. Радар генерирует
    # поисковые запросы через OpenAI, а OpenAI отвечает РФ-адресам 403
    # unsupported_country_region_territory — поэтому жёсткий регион 'ru' ронял
    # ежедневную задачу КАЖДЫЙ раз, молча: сигналы стояли с 13.09.
    # Остальные ИИ-стадии давно ходят этим же маршрутом, через внешний воркер.
    from oiltech_digest import network_policy

    decision = network_policy.route_ai_processing()
    job = enqueue(
        "signal_discovery",
        daily_signal_discovery_payload(),
        queue_name=decision.queue_name,
        execution_region=decision.execution_region,
        capability=decision.capability,
        max_attempts=1,
    )
    return {"enqueued": True, "job": job, "lookback_hours": lookback_hours}


def enqueue(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    user_id: int | None = None,
    queue_name: str = "default",
    execution_region: str = "ru",
    capability: str | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Create a persistent job and submit it to the local executor."""
    if kind not in _HANDLERS:
        raise ValueError(f"Unknown background job kind: {kind}")
    job = repository.create_background_job(
        kind,
        payload or {},
        user_id=user_id,
        queue_name=queue_name,
        execution_region=execution_region,
        capability=capability,
        max_attempts=max_attempts,
    )
    if config.BACKGROUND_JOB_INLINE:
        _executor.submit(run, int(job["id"]))
    return job


def run(job_id: int) -> None:
    """Execute a queued job and persist terminal state."""
    job = repository.get_background_job(job_id)
    if job is None:
        return
    _execute(job, mark_running=True)


def run_claimed(job: dict[str, Any]) -> None:
    """Execute a job already claimed by an external worker."""
    _execute(job, mark_running=False)


def worker_loop(
    *,
    poll_seconds: float | None = None,
    once: bool = False,
    stale_minutes: int | None = None,
    queue_names: list[str] | None = None,
) -> None:
    """Poll the DB queue and execute jobs in the current process."""
    poll_seconds = config.BACKGROUND_JOB_POLL_SECONDS if poll_seconds is None else poll_seconds
    stale_minutes = config.BACKGROUND_JOB_STALE_MINUTES if stale_minutes is None else stale_minutes
    queue_names = queue_names or config.BACKGROUND_JOB_QUEUES
    finalize_minutes = config.FINALIZE_STALE_MINUTES
    sweep_interval = max(poll_seconds, 30.0)  # переочередь зависших — не чаще раза в ~30с

    def _sweep_stale() -> None:
        local = repository.requeue_stale_background_jobs(stale_minutes, finalize_minutes)
        if local.requeued or local.exhausted:
            logger.warning(
                "jobs_requeued_stale requeued=%s exhausted=%s stale_minutes=%s "
                "finalize_minutes=%s queues=%s",
                local.requeued,
                local.exhausted,
                stale_minutes,
                finalize_minutes,
                ",".join(queue_names),
            )
        # Внешний контур убирается ПО LEASE, а не по настенным часам, — и делать это надо
        # по расписанию. Раньше реапер жил только внутри /api/external-worker/claim (T21):
        # пока воркер занят длинной задачей, он за новой не приходит, значит и протухшие
        # lease никто не разбирает. Умри воркер совсем — задачи висели бы 'running' вечно,
        # и интерфейс врал бы про идущую работу. Здесь этот сигнал наконец опрашивается
        # регулярно, независимо от того, приходит воркер за работой или нет.
        external = repository.requeue_expired_external_leases()
        if external.requeued or external.exhausted:
            logger.warning(
                "external_leases_reaped requeued=%s exhausted=%s",
                external.requeued,
                external.exhausted,
            )

    last_sweep = 0.0
    while True:
        # Периодически вытаскиваем зависшие running/finalizing (раньше — только разово на старте,
        # из-за чего застрявший после краша 'finalizing' ждал рестарта воркача; баг T2/H2).
        now_mono = time.monotonic()
        if now_mono - last_sweep >= sweep_interval:
            _sweep_stale()
            last_sweep = now_mono
        job = repository.claim_next_background_job(queue_names=queue_names)
        if job is None:
            if once:
                return
            time.sleep(poll_seconds)
            continue
        logger.info(
            "background_job_started job_id=%s kind=%s queue=%s attempts=%s",
            job["id"],
            job["kind"],
            job.get("queue_name"),
            job.get("attempts"),
        )
        run_claimed(job)


def _execute(job: dict[str, Any], *, mark_running: bool) -> None:
    job_id = int(job["id"])
    handler = _HANDLERS.get(job["kind"])
    if handler is None:
        repository.fail_background_job(job_id, f"Unknown background job kind: {job['kind']}")
        return

    try:
        if mark_running:
            repository.mark_background_job_running(job_id)
            job = repository.get_background_job(job_id) or job
        result = handler(dict(job.get("payload_json") or {}), job_id)
        repository.finish_background_job(job_id, result)
        logger.info(
            "background_job_finished job_id=%s kind=%s queue=%s",
            job["id"],
            job["kind"],
            job.get("queue_name"),
        )
    except Exception as exc:  # noqa: BLE001 - terminal job errors must be recorded
        retry_delay = _retry_delay_seconds(job)
        repository.fail_background_job(int(job["id"]), str(exc), retry_delay_seconds=retry_delay)
        logger.exception(
            "background_job_failed job_id=%s kind=%s queue=%s retry_delay_seconds=%s",
            job["id"],
            job["kind"],
            job.get("queue_name"),
            retry_delay,
        )


def _retry_delay_seconds(job: dict[str, Any]) -> int | None:
    attempts = int(job.get("attempts") or 0)
    max_attempts = int(job.get("max_attempts") or 0)
    if attempts >= max_attempts:
        return None
    return min(config.BACKGROUND_JOB_RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)), 1800)


def _run_digest_export(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    repository.update_background_job_progress(job_id, 25)
    result = write_digest_export(
        month=str(payload.get("month") or ""),
        export_format=str(payload.get("export_format") or "pdf"),
        limit=int(payload.get("limit") or 100),
        min_score=float(payload.get("min_score") or 0),
        max_score=float(payload["max_score"]) if payload.get("max_score") is not None else None,
        search=str(payload.get("search") or "") or None,
        top_tag=str(payload.get("top_tag") or "") or None,
        user_id=int(payload["user_id"]) if payload.get("user_id") is not None else None,
    )
    repository.update_background_job_progress(job_id, 90)
    return result


def _run_process_articles(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    client = make_client(bool(payload.get("offline", False)))
    article_ids = payload.get("article_ids") or []
    has_article_ids = bool(article_ids)
    limit = int(payload.get("limit") or 5)

    if has_article_ids:
        ids = [int(article_id) for article_id in article_ids]
        articles = repository.get_articles_by_ids(ids, include_summary=True)
    else:
        articles = repository.get_articles_needing_pipeline(limit)

    # Отметка «эта попытка НАЧАЛА жечь OpenAI» — строго ДО первого обращения к модели.
    # По ней requeue_stale_background_jobs отличает задачу, которую нельзя перезапускать
    # автоматически (повторный прогон = повторный реальный расход), от упавшей до AI.
    # Именно отдельная колонка, а не progress: claim/mark-running форсят progress в 10
    # ещё до тела обработчика, поэтому по progress «до/после AI» не различить.
    repository.mark_background_job_ai_started(job_id)
    repository.update_background_job_progress(job_id, 20)
    stats = process_pipeline_articles(articles, client, fetch_full=True)
    repository.update_background_job_progress(job_id, 95)

    return {"pipeline": stats}


def _run_scrape_source(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    source_id = int(payload["source_id"])
    source = repository.get_source(source_id)
    if source is None:
        raise ValueError("Source not found")

    strategy = source.get("parse_strategy")
    if strategy not in {"request", "playwright"}:
        raise ValueError("Скраппер доступен только для request/playwright-источников")

    repository.update_background_job_progress(job_id, 25)
    stats = playwright_parser.parse_source(source) if strategy == "playwright" else request_parser.parse_source(source)
    repository.update_background_job_progress(job_id, 90)
    return {"source_id": source_id, "stats": stats}


def _run_parse_source_once(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    source_id = int(payload["source_id"])
    source = repository.get_source(source_id)
    if source is None:
        raise ValueError("Source not found")
    strategy = source.get("parse_strategy")
    repository.update_background_job_progress(job_id, 20)
    if strategy == "rss":
        stats = rss_parser.parse_source(source)
    elif strategy == "request":
        stats = request_parser.parse_source(source)
    elif strategy == "telegram":
        stats = telegram_parser.parse_source(source)
    elif strategy == "playwright":
        stats = playwright_parser.parse_source(source)
    else:
        raise ValueError("parse_source_once supports rss/request/telegram/playwright sources")
    repository.update_background_job_progress(job_id, 90)
    return {"source_id": source_id, "strategy": strategy, "stats": stats}


def _run_diagnose_source(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    source_id = int(payload["source_id"])
    source = repository.get_source(source_id)
    if source is None:
        raise ValueError("Source not found")
    overrides = payload.get("overrides") or {}
    limit = int(payload.get("limit") or 5)

    repository.update_background_job_progress(job_id, 20)
    result = diagnose_source({**source, **overrides}, limit=limit)
    repository.update_background_job_progress(job_id, 90)
    return result


def _run_discover_source_candidates(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    from oiltech_digest.source_discovery.agent import DiscoveryConfig, discover_sources, get_topic_gaps
    from oiltech_digest.source_discovery.sandbox import evaluate_source_candidate

    topics = [str(item).strip() for item in payload.get("topics") or [] if str(item).strip()]
    if not topics:
        gaps = get_topic_gaps(limit=int(payload.get("topic_limit") or 3))
        topics = [str(row.get("topic") or "").strip() for row in gaps if str(row.get("topic") or "").strip()]
    if not topics:
        return {"topics": [], "candidates": 0, "evaluated": 0, "reason": "no topic gaps found"}

    limit = int(payload.get("limit") or 10)
    offline = bool(payload.get("offline", False))
    fetch_inspection = bool(payload.get("fetch_inspection", False))
    test_parse = bool(payload.get("test_parse", True))
    auto_evaluate = bool(payload.get("auto_evaluate", True))
    article_limit = int(payload.get("article_limit") or 5)
    agent_run_id = int(payload["agent_run_id"]) if payload.get("agent_run_id") else None
    results: list[dict[str, Any]] = []
    total_candidates = 0
    total_evaluated = 0
    total_evaluation_jobs = 0

    for index, topic in enumerate(topics, start=1):
        progress = int(min(85, 10 + (index - 1) * 70 / max(len(topics), 1)))
        repository.update_background_job_progress(job_id, progress)
        discovery = discover_sources(DiscoveryConfig(
            topic=topic,
            limit=limit,
            seed_urls=tuple(payload.get("seed_urls") or ()),
            offline=offline,
            dry_run=False,
            fetch_inspection=fetch_inspection,
            test_parse=test_parse,
            run_id=agent_run_id,
        ))
        topic_result = {
            "topic": topic,
            "task_id": discovery.get("task_id"),
            "search": discovery.get("search"),
            "candidates": [
                {"id": item.get("id"), "url": item.get("url"), "action": item.get("recommended_action")}
                for item in discovery.get("candidates") or []
            ],
            "evaluations": [],
        }
        total_candidates += len(topic_result["candidates"])
        if auto_evaluate:
            for item in discovery.get("candidates") or []:
                candidate_id = item.get("id")
                if not candidate_id:
                    continue
                # В платную очередь ИИ — только если человек снял флажок «Без ИИ» (offline=False).
                # Раньше флажок здесь не смотрелся: с воркером агентов на NL каждое нажатие
                # «Поставить в очередь» стоило бы до 26 вызовов модели на кандидата.
                if not offline and config.EXTERNAL_WORKERS_ENABLED and config.AI_EXECUTION_REGION == "external":
                    evaluation_job = repository.create_background_job(
                        "source_candidate_evaluate",
                        {
                            "candidate_id": int(candidate_id),
                            "article_limit": article_limit,
                            "offline": False,
                            "collect": True,
                            "process": True,
                        },
                        queue_name="external-ai",
                        execution_region="external",
                        capability="openai",
                        max_attempts=1,
                        agent_run_id=agent_run_id,
                    )
                    topic_result["evaluations"].append({
                        "candidate_id": int(candidate_id),
                        "job_id": int(evaluation_job["id"]),
                        "queued": "external-ai",
                    })
                    total_evaluation_jobs += 1
                    continue
                evaluation = evaluate_source_candidate(
                    int(candidate_id),
                    article_limit=article_limit,
                    offline=True,
                    collect=True,
                    process=True,
                )
                topic_result["evaluations"].append({
                    "candidate_id": int(candidate_id),
                    "metrics": evaluation.get("metrics"),
                    "recommended_action": evaluation.get("recommended_action"),
                    "next_status": evaluation.get("next_status"),
                })
                total_evaluated += 1
        results.append(topic_result)

    repository.update_background_job_progress(job_id, 95)
    return {
        "topics": topics,
        "candidates": total_candidates,
        "evaluated": total_evaluated,
        "evaluation_jobs": total_evaluation_jobs,
        "results": results,
    }


def _run_source_discovery_plan(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    from oiltech_digest.source_discovery.planner import PlannerConfig, build_plan, enqueue_plan_actions

    run_id = repository.create_agent_run(
        "source_discovery_cycle",
        trigger=str(payload.get("trigger") or "background_job"),
        payload={**payload, "background_job_id": job_id},
    )
    repository.update_background_job_progress(job_id, 20)
    try:
        plan = build_plan(PlannerConfig(
            days=int(payload.get("days") or 30),
            target_per_topic=int(payload.get("target_per_topic") or 10),
            topic_limit=int(payload.get("topic_limit") or 5),
            candidate_limit=int(payload.get("candidate_limit") or 10),
            max_actions=int(payload.get("max_actions") or 5),
            persist_memory=bool(payload.get("persist_memory", True)),
            run_id=run_id,
        ))
        repository.update_background_job_progress(job_id, 70)
        queued = enqueue_plan_actions(
            plan,
            offline=bool(payload.get("offline", True)),
            evaluate=bool(payload.get("evaluate", True)),
            run_id=run_id,
        )
        repository.update_background_job_progress(job_id, 95)
        result = {**plan, "run_id": run_id, "queued": queued}
        repository.finish_agent_run(run_id, status="ok", result=result)
        return result
    except Exception as exc:
        repository.finish_agent_run(run_id, status="failed", result={}, error_message=str(exc)[:1000])
        raise


def _run_source_discovery_loop(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    from oiltech_digest.source_discovery.loop import AgentLoopConfig, run_agent_loop

    repository.update_background_job_progress(job_id, 10)
    result = run_agent_loop(AgentLoopConfig(
        goal=str(payload.get("goal") or "Найти новые полезные источники сигналов"),
        days=int(payload.get("days") or 30),
        target_per_topic=int(payload.get("target_per_topic") or 10),
        topic_limit=int(payload.get("topic_limit") or 5),
        candidate_limit=int(payload.get("candidate_limit") or 10),
        max_actions=int(payload.get("max_actions") or 5),
        max_iterations=int(payload.get("max_iterations") or 3),
        offline=bool(payload.get("offline", True)),
        fetch_inspection=bool(payload.get("fetch_inspection", False)),
        test_parse=bool(payload.get("test_parse", True)),
        dry_run=bool(payload.get("dry_run", False)),
        auto_evaluate=bool(payload.get("auto_evaluate", True)),
        article_limit=int(payload.get("article_limit") or 5),
        persist_memory=bool(payload.get("persist_memory", True)),
        max_daily_loop_runs=int(payload.get("max_daily_loop_runs") or 4),
        max_daily_candidates=int(payload.get("max_daily_candidates") or 100),
        max_daily_evaluations=int(payload.get("max_daily_evaluations") or 100),
    ))
    repository.update_background_job_progress(job_id, 95)
    return result


def _run_signal_discovery(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    from oiltech_digest.signal_discovery import config_from_payload, discover_signals

    repository.update_background_job_progress(job_id, 15)
    result = discover_signals(config_from_payload(payload, background_job_id=job_id))
    repository.update_background_job_progress(job_id, 95)
    return result


def job_download_path(job: dict[str, Any]) -> Path | None:
    result = job.get("result_json") or {}
    path = result.get("path")
    return Path(path) if path else None


_HANDLERS: dict[str, Callable[[dict[str, Any], int], dict[str, Any]]] = {
    "digest_export": _run_digest_export,
    "process_articles": _run_process_articles,
    "parse_source_once": _run_parse_source_once,
    "scrape_source": _run_scrape_source,
    "diagnose_source": _run_diagnose_source,
    "source_discovery_plan": _run_source_discovery_plan,
    "source_discovery_loop": _run_source_discovery_loop,
    "discover_source_candidates": _run_discover_source_candidates,
    "signal_discovery": _run_signal_discovery,
}
