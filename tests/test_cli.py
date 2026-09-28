import argparse
import json

import pytest

from oiltech_digest import cli
from oiltech_digest.db import repository


def test_schema_check_command_reports_ok(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.readiness.schema_check",
        lambda: {"ok": True, "required_tables": ["articles"], "missing_tables": []},
    )

    cli.cmd_schema_check(argparse.Namespace())

    assert "schema-check: ok" in capsys.readouterr().out


def test_export_signal_training_jsonl_command_prints_stats(monkeypatch, capsys, tmp_path):
    path = tmp_path / "signals.jsonl"
    captured = {}

    def fake_export_signal_training_jsonl(output_path, **kwargs):
        captured["path"] = output_path
        captured.update(kwargs)
        return {
            "path": str(path),
            "examples": 2,
            "with_feedback_only": kwargs["with_feedback_only"],
            "verdict": kwargs["verdict"],
        }

    monkeypatch.setattr(
        "oiltech_digest.signal_training.export_signal_training_jsonl",
        fake_export_signal_training_jsonl,
    )

    cli.cmd_export_signal_training_jsonl(
        argparse.Namespace(path=str(path), limit=20, all=False, verdict="approved", json=False)
    )

    out = capsys.readouterr().out
    assert "export-signal-training-jsonl: examples=2" in out
    assert str(path) in out
    assert captured == {
        "path": str(path),
        "limit": 20,
        "with_feedback_only": True,
        "verdict": "approved",
    }


def test_export_signal_context_command_prints_stats(monkeypatch, capsys, tmp_path):
    path = tmp_path / "bundle.json"
    captured = {}

    def fake_export_signal_context_bundle(output_path, **kwargs):
        captured["path"] = output_path
        captured.update(kwargs)
        return {
            "path": str(path),
            "signals": 38,
            "evidence": 79,
            "memories": 12,
            "feedback_events": 6,
        }

    monkeypatch.setattr(
        "oiltech_digest.signal_training.export_signal_context_bundle",
        fake_export_signal_context_bundle,
    )

    cli.cmd_export_signal_context(argparse.Namespace(path=str(path), include_rejected=False, json=False))

    out = capsys.readouterr().out
    assert "export-signal-context: path=" in out
    assert "signals=38" in out
    assert captured == {"path": str(path), "include_rejected": False}


def test_import_signal_context_command_prints_dry_run(monkeypatch, capsys, tmp_path):
    path = tmp_path / "bundle.json"
    captured = {}

    def fake_import_signal_context_bundle(input_path, **kwargs):
        captured["path"] = input_path
        captured.update(kwargs)
        return {
            "path": str(path),
            "dry_run": True,
            "signals": 38,
            "evidence": 79,
            "memories": 12,
            "feedback_events": 6,
        }

    monkeypatch.setattr(
        "oiltech_digest.signal_training.import_signal_context_bundle",
        fake_import_signal_context_bundle,
    )

    cli.cmd_import_signal_context(argparse.Namespace(path=str(path), dry_run=True, json=False))

    out = capsys.readouterr().out
    assert "import-signal-context [dry-run]" in out
    assert "signals=38" in out
    assert captured == {"path": str(path), "dry_run": True}


def test_schema_check_command_exits_non_zero_when_missing_tables(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.readiness.schema_check",
        lambda: {"ok": False, "required_tables": ["articles"], "missing_tables": ["background_jobs"]},
    )

    with pytest.raises(SystemExit, match="1"):
        cli.cmd_schema_check(argparse.Namespace())

    assert "background_jobs" in capsys.readouterr().out


def test_enqueue_external_scrape_is_noop_when_contour_disabled(monkeypatch, capsys):
    monkeypatch.setattr("oiltech_digest.config.EXTERNAL_WORKERS_ENABLED", False)
    monkeypatch.setattr("oiltech_digest.config.FETCH_EXTERNAL_ENABLED", True)
    created = []
    monkeypatch.setattr("oiltech_digest.db.repository.create_background_job",
                        lambda *a, **k: created.append((a, k)))

    cli.cmd_enqueue_external_scrape(argparse.Namespace(max_age_days=None))

    assert created == []
    assert "выключен" in capsys.readouterr().out


def test_enqueue_external_scrape_enqueues_only_external_sources(monkeypatch, capsys):
    monkeypatch.setattr("oiltech_digest.config.EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr("oiltech_digest.config.FETCH_EXTERNAL_ENABLED", True)
    sources = [
        {"id": 22, "parse_strategy": "playwright", "network_region": "external"},
        {"id": 4, "parse_strategy": "rss", "network_region": "external"},
        {"id": 50, "parse_strategy": "request", "network_region": "auto"},      # локальный — пропуск
        {"id": 60, "parse_strategy": "telegram", "network_region": "external"}, # telegram — не трогаем
    ]
    monkeypatch.setattr("oiltech_digest.db.repository.get_enabled_sources", lambda: sources)
    jobs = []
    monkeypatch.setattr("oiltech_digest.db.repository.create_background_job",
                        lambda kind, payload, **k: jobs.append((kind, payload, k)))

    cli.cmd_enqueue_external_scrape(argparse.Namespace(max_age_days=7))

    enqueued_ids = {payload["source_id"] for _, payload, _ in jobs}
    assert enqueued_ids == {22, 4}
    queues = {k["queue_name"] for _, _, k in jobs}
    assert queues == {"external-playwright", "external-fetch"}
    assert "задач=2" in capsys.readouterr().out


def _resummarize_args(**overrides):
    values = {"article_id": None, "limit": 0, "batch_size": 20, "dry_run": True}
    values.update(overrides)
    return argparse.Namespace(**values)


def test_enqueue_resummarize_by_default_only_selects(monkeypatch, capsys):
    monkeypatch.setattr("oiltech_digest.processing.mixed_script.resummarize_selection",
                        lambda ids=None: {"summary": [1], "title": [3], "source_title": [5, 6]})
    created = []
    monkeypatch.setattr("oiltech_digest.db.repository.create_background_job", lambda *a, **k: created.append(a))

    cli.cmd_enqueue_resummarize(_resummarize_args())

    assert created == []
    out = capsys.readouterr().out
    assert "суть — 1 статей, только заголовок — 1; брак в самом исходном заголовке (переводом не лечится) — 2" in out


def test_enqueue_resummarize_marks_jobs_to_write_only_summary_and_translation(monkeypatch):
    monkeypatch.setattr("oiltech_digest.config.EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr("oiltech_digest.config.AI_EXECUTION_REGION", "external")
    monkeypatch.setattr("oiltech_digest.config.AI_BULK_LANE_ENABLED", True)
    monkeypatch.setattr("oiltech_digest.processing.mixed_script.resummarize_selection",
                        lambda ids=None: {"summary": [1], "title": [3], "source_title": [5]})
    jobs = []
    monkeypatch.setattr("oiltech_digest.db.repository.create_background_job",
                        lambda kind, payload, **k: jobs.append((kind, payload, k["queue_name"])) or {"id": len(jobs)})

    cli.cmd_enqueue_resummarize(_resummarize_args(dry_run=False))

    assert jobs == [
        ("process_articles", {"article_ids": [1], "limit": 1, "offline": False, "only": ["summary", "translation"]},
         "external-ai-bulk"),
        ("translate_titles", {"article_ids": [3]}, "external-ai-bulk"),
    ]


def test_enqueue_resummarize_refuses_local_pipeline(monkeypatch):
    """Локальный конвейер пометки only не знает и готовые стадии пропускает — задача прошла бы впустую."""
    monkeypatch.setattr("oiltech_digest.config.EXTERNAL_WORKERS_ENABLED", False)
    monkeypatch.setattr("oiltech_digest.processing.mixed_script.resummarize_selection",
                        lambda ids=None: {"summary": [1], "title": [], "source_title": []})
    created = []
    monkeypatch.setattr("oiltech_digest.db.repository.create_background_job", lambda *a, **k: created.append(a))

    with pytest.raises(SystemExit, match="внешний контур"):
        cli.cmd_enqueue_resummarize(_resummarize_args(dry_run=False))
    assert created == []


def test_scripts_only_json_keeps_every_change_for_rollback(monkeypatch, capsys):
    """Ревью 27.09: --json обрезал список до --show (30 из 185) — откатить было не по чему."""
    changes = [{"article_id": index, "field": "summary", "before": "вхoдит", "after": "входит"} for index in range(5)]
    monkeypatch.setattr("oiltech_digest.processing.mixed_script.repair_cards",
                        lambda **kwargs: {"changed_fields": 5, "applied": False, "changes": changes})

    cli.cmd_repair_terminology(argparse.Namespace(scripts_only=True, dry_run=True, article_id=None, json=True, show=2))

    assert json.loads(capsys.readouterr().out)["changes"] == changes


def test_repair_telegram_titles_needs_before_to_write(monkeypatch):
    called = []
    monkeypatch.setattr("oiltech_digest.ingestion.telegram_titles.repair", lambda **kwargs: called.append(kwargs))

    with pytest.raises(SystemExit, match="--before"):
        cli.cmd_repair_telegram_titles(argparse.Namespace(dry_run=False, before=None, json=False, show=0))
    assert called == []
    assert cli._utc_datetime("2026-09-25T12:00:00").tzinfo is not None


def test_source_dump_listing_prints_anchors_with_container(monkeypatch, capsys):
    html = (
        b"<html><body>"
        b'<nav><a href="/about">About</a></nav>'
        b'<div class="news-list"><a href="/news/oil-deal">Big oil deal 2026</a></div>'
        b"</body></html>"
    )
    monkeypatch.setattr("oiltech_digest.db.repository.get_source",
                        lambda sid: {"id": 35, "name": "IoT World", "parse_strategy": "request",
                                     "listing_url": "https://example.com/news"})
    monkeypatch.setattr("oiltech_digest.ingestion.http_client.fetch", lambda url: html)

    cli.cmd_source_dump_listing(argparse.Namespace(source_id=35, limit=40))

    out = capsys.readouterr().out
    assert "news-list" in out                                  # контейнер статей виден
    assert "https://example.com/news/oil-deal" in out          # ссылки абсолютизированы
    assert "Big oil deal 2026" in out


def test_source_dump_listing_render_uses_playwright(monkeypatch, capsys):
    html = b'<html><body><div class="news"><a href="/n/1">Rendered SPA article</a></div></body></html>'
    monkeypatch.setattr("oiltech_digest.db.repository.get_source",
                        lambda sid: {"id": 99, "name": "Узбекнефтегаз", "parse_strategy": "request",
                                     "url": "https://www.ung.uz"})
    monkeypatch.setattr("oiltech_digest.ingestion.playwright_parser.is_available", lambda: True)
    rendered = {}
    def fake_render(url, settle_ms=3500):
        rendered["url"] = url
        rendered["settle"] = settle_ms
        return html
    monkeypatch.setattr("oiltech_digest.ingestion.playwright_parser.fetch_rendered", fake_render)

    cli.cmd_source_dump_listing(argparse.Namespace(source_id=99, limit=40, render=True))

    out = capsys.readouterr().out
    assert "playwright-render" in out
    assert "Rendered SPA article" in out
    assert rendered["url"] == "https://www.ung.uz"
    assert rendered["settle"] == 8000          # увеличенный settle для SPA


def test_source_dump_listing_exits_when_listing_unavailable(monkeypatch):
    monkeypatch.setattr("oiltech_digest.db.repository.get_source",
                        lambda sid: {"id": 9, "parse_strategy": "request", "url": "https://x.test"})
    monkeypatch.setattr("oiltech_digest.ingestion.http_client.fetch", lambda url: None)

    with pytest.raises(SystemExit):
        cli.cmd_source_dump_listing(argparse.Namespace(source_id=9, limit=10))


def test_jobs_requeue_stale_command_uses_config_default(monkeypatch, capsys):
    monkeypatch.setattr("oiltech_digest.config.BACKGROUND_JOB_STALE_MINUTES", 75)
    called = {}

    def fake_requeue(stale_minutes):
        called["stale_minutes"] = stale_minutes
        return repository.RequeueOutcome(requeued=2, exhausted=1)

    monkeypatch.setattr("oiltech_digest.db.repository.requeue_stale_background_jobs", fake_requeue)
    monkeypatch.setattr(
        "oiltech_digest.db.repository.requeue_expired_external_leases",
        lambda: repository.RequeueOutcome(requeued=0, exhausted=0),
    )

    cli.main(["jobs-requeue-stale"])

    assert called["stale_minutes"] == 75
    output = capsys.readouterr().out
    assert "requeued=2" in output
    assert "exhausted=1" in output
    assert "stale_minutes=75" in output


def test_agent_query_memory_command_prints_rows(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        "oiltech_digest.db.repository.query_memory_report",
        lambda **kwargs: captured.update(kwargs) or [
            {
                "query": "robotic drilling automation newsroom",
                "topic": "бурение",
                "score": 76,
                "status": "active",
                "found_candidates": 3,
                "relevance_rate": 0.8,
                "empty_result": False,
            }
        ],
    )

    cli.cmd_agent_query_memory(argparse.Namespace(status="active", limit=5, json=False))

    out = capsys.readouterr().out
    assert "agent-query-memory: status=active rows=1" in out
    assert "robotic drilling automation newsroom" in out
    assert captured == {"status": "active", "limit": 5}


def test_agent_query_memory_command_all_status_passes_none(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        "oiltech_digest.db.repository.query_memory_report",
        lambda **kwargs: captured.update(kwargs) or [],
    )

    cli.cmd_agent_query_memory(argparse.Namespace(status="all", limit=10, json=True))

    assert capsys.readouterr().out.strip() == "[]"
    assert captured == {"status": None, "limit": 10}


def test_agent_readiness_command_prints_issues(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.source_discovery.readiness.source_discovery_readiness",
        lambda: {
            "ok": False,
            "status": "blocked",
            "checks": {"search": {"ok": False}},
            "issues": [{"severity": "blocker", "code": "brave_key_missing", "message": "BRAVE_SEARCH_API_KEY пустой"}],
            "recommendations": ["Заполните BRAVE_SEARCH_API_KEY"],
        },
    )

    cli.cmd_agent_readiness(argparse.Namespace(json=False))

    out = capsys.readouterr().out
    assert "agent-readiness: status=blocked ok=False" in out
    assert "brave_key_missing" in out
    assert "Заполните BRAVE_SEARCH_API_KEY" in out


def test_agent_loop_command_prints_summary(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.source_discovery.loop.run_agent_loop",
        lambda config: {
            "run_id": 77,
            "iterations": [
                {
                    "iteration": 1,
                    "action_count": 1,
                    "auto_action_count": 1,
                    "human_review_count": 0,
                    "observations": [{"topic": "бурение", "candidate_count": 2, "query_strategy": "balanced", "search_status": "ok"}],
                }
            ],
            "total_candidates": 2,
            "terminal_reason": "max_iterations_reached",
        },
    )

    cli.cmd_agent_loop(argparse.Namespace(
        goal="найти",
        days=30,
        target_per_topic=10,
        topic_limit=5,
        candidate_limit=10,
        max_actions=5,
        max_iterations=1,
        offline=True,
        fetch_inspection=False,
        test_parse=True,
        dry_run=False,
        evaluate=True,
        article_limit=5,
        no_memory=False,
        max_daily_loop_runs=4,
        max_daily_candidates=100,
        max_daily_evaluations=100,
        json=False,
    ))

    out = capsys.readouterr().out
    assert "agent-loop: run_id=77 iterations=1 candidates=2" in out
    assert "бурение: candidates=2 strategy=balanced search=ok" in out


def test_enqueue_agent_loop_command_creates_job(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        "oiltech_digest.db.repository.background_job_status_counts",
        lambda **kwargs: {},
    )
    monkeypatch.setattr(
        "oiltech_digest.db.repository.create_background_job",
        lambda kind, payload, **kwargs: captured.update({"kind": kind, "payload": payload, **kwargs}) or {"id": 91},
    )

    cli.cmd_enqueue_agent_loop(argparse.Namespace(
        goal="найти",
        days=30,
        target_per_topic=10,
        topic_limit=5,
        candidate_limit=10,
        max_actions=4,
        max_iterations=2,
        offline=True,
        fetch_inspection=False,
        dry_run=False,
        evaluate=True,
        article_limit=5,
        no_memory=False,
        max_daily_loop_runs=4,
        max_daily_candidates=100,
        max_daily_evaluations=100,
    ))

    assert captured["kind"] == "source_discovery_loop"
    assert captured["payload"]["max_iterations"] == 2
    assert captured["capability"] == "source-discovery"
    assert "enqueue-agent-loop: job id=91" in capsys.readouterr().out


def test_enqueue_agent_loop_command_skips_when_loop_already_active(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(
        "oiltech_digest.db.repository.background_job_status_counts",
        lambda **kwargs: {"queued": 1},
    )
    monkeypatch.setattr(
        "oiltech_digest.db.repository.create_background_job",
        lambda *args, **kwargs: called.append((args, kwargs)) or {"id": 91},
    )

    cli.cmd_enqueue_agent_loop(argparse.Namespace(
        goal="найти",
        days=30,
        target_per_topic=10,
        topic_limit=5,
        candidate_limit=10,
        max_actions=4,
        max_iterations=2,
        offline=True,
        fetch_inspection=False,
        dry_run=False,
        evaluate=True,
        article_limit=5,
        no_memory=False,
        allow_parallel=False,
        max_daily_loop_runs=4,
        max_daily_candidates=100,
        max_daily_evaluations=100,
    ))

    assert called == []
    assert "enqueue-agent-loop: skipped active_jobs=1" in capsys.readouterr().out


def test_enqueue_daily_signal_discovery_command_prints_created_job(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.background_jobs.enqueue_daily_signal_discovery",
        lambda force=False: {
            "enqueued": True,
            "job": {"id": 92, "queue_name": "ai", "payload_json": {"max_signals": 20}},
        },
    )

    cli.cmd_enqueue_daily_signal_discovery(argparse.Namespace(force=False))

    assert "enqueue-daily-signal-discovery: job id=92 queue=ai max_signals=20" in capsys.readouterr().out


def test_enqueue_daily_signal_discovery_command_prints_skip(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.background_jobs.enqueue_daily_signal_discovery",
        lambda force=False: {"enqueued": False, "reason": "already_scheduled"},
    )

    cli.cmd_enqueue_daily_signal_discovery(argparse.Namespace(force=False))

    assert "enqueue-daily-signal-discovery: skipped reason=already_scheduled" in capsys.readouterr().out


def test_source_candidate_triage_command_prints_rows(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        "oiltech_digest.db.repository.source_candidate_triage_report",
        lambda **kwargs: captured.update(kwargs) or [
            {
                "id": 7,
                "normalized_domain": "example.com",
                "url": "https://example.com/news",
                "status": "needs_human_review",
                "recommended_action": "add",
                "triage_priority": 120,
                "tested_articles": 5,
                "relevant_articles": 4,
                "avg_score": 80,
                "topic": "бурение",
            }
        ],
    )

    cli.cmd_source_candidate_triage(argparse.Namespace(limit=5, json=False))

    out = capsys.readouterr().out
    assert "source-candidate-triage: rows=1" in out
    assert "example.com" in out
    assert captured == {"limit": 5}


def test_jobs_requeue_stale_command_accepts_override(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.db.repository.requeue_stale_background_jobs",
        lambda stale_minutes: repository.RequeueOutcome(
            requeued=stale_minutes // 30, exhausted=0
        ),
    )
    monkeypatch.setattr(
        "oiltech_digest.db.repository.requeue_expired_external_leases",
        lambda: repository.RequeueOutcome(requeued=0, exhausted=0),
    )

    cli.main(["jobs-requeue-stale", "--stale-minutes", "120"])

    output = capsys.readouterr().out
    assert "requeued=4" in output
    assert "stale_minutes=120" in output


def test_external_queues_status_command(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.db.repository.external_queue_status",
        lambda: {
            "totals": {
                "queued": 3,
                "running": 1,
                "failed": 2,
                "ok": 0,
                "expired_leases": 0,
                "oldest_queued_at": None,
                "last_heartbeat_at": None,
            },
            "queues": [
                {
                    "queue_name": "external-ai",
                    "queued": 3,
                    "running": 1,
                    "failed": 2,
                    "ok": 0,
                    "oldest_queued_at": None,
                    "last_heartbeat_at": None,
                }
            ],
        },
    )

    cli.main(["external-queues-status"])

    output = capsys.readouterr().out
    assert "external-queues: queued=3" in output
    assert "external-ai: queued=3" in output


def test_maintenance_cleanup_command_uses_defaults(monkeypatch, capsys):
    monkeypatch.setattr("oiltech_digest.config.BACKGROUND_JOB_RETENTION_DAYS", 21)
    monkeypatch.setattr("oiltech_digest.config.EXPORT_JOB_RETENTION_DAYS", 14)
    monkeypatch.setattr("oiltech_digest.db.repository.delete_expired_user_sessions", lambda: 3)
    monkeypatch.setattr("oiltech_digest.db.repository.cleanup_finished_background_jobs", lambda days: days // 7)
    monkeypatch.setattr("oiltech_digest.db.repository.cleanup_finished_export_jobs", lambda days: days // 7)

    cli.main(["maintenance-cleanup"])

    output = capsys.readouterr().out
    assert "expired_sessions=3" in output
    assert "background_jobs=3" in output
    assert "background_job_days=21" in output
    assert "export_jobs=2" in output
    assert "export_job_days=14" in output


def test_maintenance_cleanup_command_accepts_overrides(monkeypatch, capsys):
    monkeypatch.setattr("oiltech_digest.db.repository.delete_expired_user_sessions", lambda: 1)
    monkeypatch.setattr("oiltech_digest.db.repository.cleanup_finished_background_jobs", lambda days: days)
    monkeypatch.setattr("oiltech_digest.db.repository.cleanup_finished_export_jobs", lambda days: days)

    cli.main(["maintenance-cleanup", "--background-job-days", "10", "--export-job-days", "5"])

    output = capsys.readouterr().out
    assert "expired_sessions=1" in output
    assert "background_jobs=10" in output
    assert "background_job_days=10" in output
    assert "export_jobs=5" in output
    assert "export_job_days=5" in output


def test_audit_terminology_command_reports_bad_terms(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.db.repository.list_article_texts_for_terminology_audit",
        lambda limit=500, article_id=None: [
            {
                "id": 42,
                "title": "Hydraulic fracturing expands",
                "raw_text": "Hydraulic fracturing expands in the field.",
                "language": "en",
                "source_name": "World Oil",
                "source_category": "Новости",
                "title_ru": "",
                "summary": "Компания расширила фракинг.",
            }
        ],
    )

    cli.cmd_audit_terminology(argparse.Namespace(limit=10, article_id=None, show=5, json=False))

    output = capsys.readouterr().out
    assert "terminology-audit: проблемных полей=1" in output
    assert "article=42" in output
    assert "фракинг -> ГРП" in output


def test_repair_terminology_command_dry_run_does_not_update(monkeypatch, capsys):
    updated = []
    monkeypatch.setattr(
        "oiltech_digest.db.repository.list_article_texts_for_terminology_audit",
        lambda limit=500, article_id=None: [
            {
                "id": 42,
                "title": "Hydraulic fracturing expands",
                "raw_text": "Hydraulic fracturing expands in the field.",
                "language": "en",
                "source_name": "World Oil",
                "source_category": "Новости",
                "title_ru": "",
                "summary": "Компания расширила фракинг.",
            }
        ],
    )
    monkeypatch.setattr(
        "oiltech_digest.db.repository.update_article_terminology_texts",
        lambda *args, **kwargs: updated.append((args, kwargs)),
    )

    cli.cmd_repair_terminology(argparse.Namespace(limit=10, article_id=None, show=5, dry_run=True, json=False))

    output = capsys.readouterr().out
    assert "terminology-repair [dry-run]" in output
    assert "полей к исправлению=1" in output
    assert "ГРП" in output
    assert updated == []


def test_repair_terminology_command_applies_updates(monkeypatch):
    updated = []
    monkeypatch.setattr(
        "oiltech_digest.db.repository.list_article_texts_for_terminology_audit",
        lambda limit=500, article_id=None: [
            {
                "id": 42,
                "title": "Hydraulic fracturing expands",
                "raw_text": "Hydraulic fracturing expands in the field.",
                "language": "en",
                "source_name": "World Oil",
                "source_category": "Новости",
                "title_ru": "",
                "summary": "Компания расширила фракинг.",
            }
        ],
    )
    monkeypatch.setattr(
        "oiltech_digest.db.repository.update_article_terminology_texts",
        lambda *args, **kwargs: updated.append((args, kwargs)),
    )

    cli.cmd_repair_terminology(argparse.Namespace(limit=10, article_id=None, show=5, dry_run=False, json=False))

    assert updated == [((42,), {"summary": "Компания расширила ГРП.", "title_ru": None})]


def test_validate_terminology_command_reports_ok(capsys):
    cli.cmd_validate_terminology(argparse.Namespace(json=False))

    output = capsys.readouterr().out
    assert "terminology-validate: ok=True" in output
    assert "terms=" in output
    assert "golden_cases=" in output


def test_eval_terminology_command_writes_reports(tmp_path, capsys):
    csv_path = tmp_path / "terminology.csv"
    md_path = tmp_path / "terminology.md"

    cli.cmd_eval_terminology(
        argparse.Namespace(
            limit=100,
            show=5,
            csv_path=str(csv_path),
            markdown_path=str(md_path),
            json=False,
        )
    )

    output = capsys.readouterr().out
    assert "terminology-eval: total=100 passed=100 failed=0" in output
    assert "ГРП / fracking" in csv_path.read_text(encoding="utf-8-sig")
    assert "Отчет по нефтегазовой терминологии" in md_path.read_text(encoding="utf-8")


def test_apply_source_overrides_prints_missing_and_ambiguous_names(monkeypatch, capsys):
    """Промах реестра виден в ВЫВОДЕ команды, а не только в логе.

    Вызов в bootstrap обёрнут в `|| true` (docker-compose) и идёт через необязательный
    run_step (docker-scheduler), поэтому код возврата до глаз не доходит. Если печатать
    только счётчики, «не найдено=1» теряется среди строк деплоя — и молчащий источник
    выглядит как успешно применённый оверрайд. Имя обязано быть в stdout.
    """
    monkeypatch.setattr(
        "oiltech_digest.ingestion.source_overrides.apply_overrides",
        lambda: {"changed": 2, "unchanged": 44, "not_found": 1, "ambiguous": 1,
                 "missing_names": ["РБК Энергетика [Media]"],
                 "ambiguous_names": ["Neftegaz.ru"]},
    )

    cli.cmd_apply_source_overrides(argparse.Namespace())

    out = capsys.readouterr().out
    assert "РБК Энергетика [Media]" in out
    assert "Neftegaz.ru" in out
    assert "source_type" in out          # подсказка, чем чинить неоднозначность
