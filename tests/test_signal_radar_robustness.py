"""Радар после 655cdbe (21.09): что роняло прогон или портило данные.

Каждый тест падает на коде до правки: пустая страница роняла тему, докачка шла без
heartbeat и без бюджета, 0 в настройках превращался в 20, дубль из ревью пачки
становился браком, год был зашит, итог задачи не показывал новые шаги."""

from pathlib import Path

import pytest
import yaml
from lxml import etree

from oiltech_digest import background_jobs, signal_discovery
from oiltech_digest.db import repository
from oiltech_digest.ingestion import request_parser, source_diagnostics
from oiltech_digest.ingestion.source_diagnostics import ProbeResult
from oiltech_digest.processing.openai_client import AIResponse

SNIPPET = "Field trial of drilling automation for an oil and gas operator."


def _web_item(url: str) -> dict:
    return {
        "source_url": url,
        "title": "Oil and gas operator deploys drilling automation",
        "extracted_fact": SNIPPET,
        "raw_payload": {"snippet": SNIPPET},
    }


def _candidate(key: str, score: float = 70, urls: list[str] | None = None) -> dict:
    return {
        "signal": {
            "signal_key": key,
            "title": key,
            "score": score,
            "companies": [],
            "evidence": [{"source_url": url, "title": key} for url in urls or []],
        },
        "raw_output": {},
        "rejected": False,
        "training_input": {},
    }


def _decision(key: str, *, keep: bool, duplicate_of: str = "", reason: str = "ok", interest: float = 0) -> dict:
    return {
        "signal_key": key,
        "keep": keep,
        "duplicate_of_signal_key": duplicate_of,
        "reason": reason,
        "interest_score": interest,
        "why_interesting": "первое внедрение" if keep else "",
    }


def _review_client(decisions: list[dict]):
    class Client:
        def complete_json(self, *args, **kwargs):
            return AIResponse(data={"decisions": decisions}, model="fake-ai")

    return Client()


# --- 1. Пустая страница -------------------------------------------------------------


@pytest.mark.parametrize("body", [b"", b"   \n", "", b"<!-- -->"])
def test_parse_article_page_survives_empty_body(body):
    assert request_parser.parse_article_page(body, "Заголовок из листинга") == ("Заголовок из листинга", None, "")


def test_extract_candidate_links_survives_empty_listing():
    assert request_parser.extract_candidate_links({"id": 1}, "https://example.com/news", b"") == []


def test_empty_page_in_fulltext_falls_back_to_snippet(monkeypatch):
    """Ровно путь 21.09: 200 с пустым телом — lxml ParserError сквозь весь прогон."""
    monkeypatch.setattr(
        source_diagnostics, "probe_url", lambda url, timeout=20: (ProbeResult(url=url, status=200), b"")
    )

    evidence, stats = signal_discovery._enrich_web_evidence_with_full_text(
        [_web_item("https://example.com/empty")], "Бурение", limit=5
    )

    assert stats["too_short"] == 1
    assert evidence[0]["extracted_fact"] == SNIPPET
    assert evidence[0]["raw_payload"]["full_text_fetched"] is False


def test_any_page_error_keeps_candidate_with_snippet(monkeypatch):
    def boom(url, fallback_title="", **kwargs):
        raise etree.ParserError("Document is empty")

    monkeypatch.setattr(signal_discovery, "_fetch_full_text", boom)

    evidence, stats = signal_discovery._enrich_web_evidence_with_full_text(
        [_web_item("https://example.com/broken")], "Бурение", limit=5
    )

    assert stats == {"attempted": 1, "fetched": 0, "too_short": 0, "failed": 1, "skipped_budget": 0}
    assert evidence[0]["extracted_fact"] == SNIPPET
    assert "ParserError" in evidence[0]["raw_payload"]["full_text_error"]


def test_fetch_full_text_skips_documents_and_binary(monkeypatch):
    probed = []
    monkeypatch.setattr(
        source_diagnostics,
        "probe_url",
        lambda url, timeout=20: probed.append((url, timeout)) or (ProbeResult(url=url, status=200), b"%PDF-1.7 ..."),
    )

    assert signal_discovery._fetch_full_text("https://example.com/annual-report.PDF")["error"] == "not_html"
    assert probed == []  # документ по ссылке даже не качаем

    result = signal_discovery._fetch_full_text("https://example.com/news?id=1")

    assert result["ok"] is False
    assert result["error"] == "not_html"  # байты PDF не уходят судье «текстом»
    assert probed == [("https://example.com/news?id=1", signal_discovery.app_config.SIGNAL_DISCOVERY_FULLTEXT_TIMEOUT_SECONDS)]


# --- 2. Аренда задачи: heartbeat и бюджет времени ------------------------------------


def test_fulltext_beats_before_every_page(monkeypatch):
    beats: list[int] = []
    fetched: list[tuple[str, int]] = []

    def fetch(url, fallback_title="", **kwargs):
        fetched.append((url, len(beats)))
        return {"ok": False, "error": "http_403", "raw_text": ""}

    monkeypatch.setattr(signal_discovery, "_fetch_full_text", fetch)

    signal_discovery._enrich_web_evidence_with_full_text(
        [_web_item(f"https://example.com/{index}") for index in range(3)],
        "Бурение",
        limit=5,
        heartbeat=lambda: beats.append(1),
    )

    assert fetched == [("https://example.com/0", 1), ("https://example.com/1", 2), ("https://example.com/2", 3)]


def test_fulltext_stops_at_topic_time_budget(monkeypatch):
    now = [0.0]

    def slow_page(url, fallback_title="", **kwargs):
        now[0] += 50
        return {"ok": False, "error": "timeout", "raw_text": ""}

    monkeypatch.setattr(signal_discovery, "_fetch_full_text", slow_page)

    evidence, stats = signal_discovery._enrich_web_evidence_with_full_text(
        [_web_item(f"https://example.com/{index}") for index in range(5)],
        "Бурение",
        limit=5,
        budget_seconds=120,
        clock=lambda: now[0],
    )

    assert (stats["attempted"], stats["skipped_budget"]) == (3, 2)
    assert len(evidence) == 5  # кандидаты сверх бюджета остаются со сниппетом, не теряются


def test_run_discovery_heartbeat_reaches_search_and_pages(monkeypatch):
    from oiltech_digest.source_discovery import agent as source_agent

    events: list[str] = []
    monkeypatch.setattr(signal_discovery, "feedback_query_hints", lambda topic, limit=8, **kwargs: [])
    monkeypatch.setattr(source_agent, "generate_search_queries", lambda topic, offline=True, limit=8, strategy="broad": ["q"])

    def search(queries, limit=80):
        events.append("search")
        return {"status": "ok", "provider": "test", "results": [{
            "url": "https://example.com/a",
            "title": "Oil and gas operator deploys drilling automation",
            "snippet": "Contract signed for deployment at an oil and gas field.",
            "query": queries[0],
        }]}

    monkeypatch.setattr(source_agent, "search_web", search)
    monkeypatch.setattr(
        signal_discovery,
        "_fetch_full_text",
        lambda url, fallback_title="", **kwargs: events.append("page") or {"ok": False, "error": "x", "raw_text": ""},
    )
    config = signal_discovery.SignalDiscoveryConfig(
        offline=True, dry_run=True, web_only=True, research_rounds=1, web_query_limit=1, limit=5, web_fulltext_limit=5
    )

    signal_discovery.run_discovery(
        config, {"topics": [{"name": "Бурение", "query_seeds_json": []}], "tags": []}, heartbeat=lambda: events.append("beat")
    )

    assert events[events.index("search") - 1] == "beat"
    assert events[events.index("page") - 1] == "beat"


# --- 3. Ноль в настройках ------------------------------------------------------------


def test_config_keeps_explicit_zero_and_takes_core_default_for_missing(monkeypatch):
    monkeypatch.setattr(signal_discovery.app_config, "SIGNAL_DISCOVERY_WEB_FULLTEXT_LIMIT", 0)
    monkeypatch.setattr(signal_discovery.app_config, "SIGNAL_DISCOVERY_RESEARCH_ROUNDS", 1)

    zero = signal_discovery.config_from_payload({"web_fulltext_limit": 0, "research_rounds": 0, "min_score": 0})
    missing = signal_discovery.config_from_payload({})

    assert (zero.web_fulltext_limit, zero.research_rounds, zero.min_score) == (0, 0, 0.0)
    # Задача с экрана полей не шлёт — выключатель в .env ядра действует и на неё.
    assert (missing.web_fulltext_limit, missing.research_rounds) == (0, 1)
    assert (missing.days, missing.limit, missing.max_signals, missing.web_query_limit) == (14, 80, 10, 8)


def test_daily_payload_with_zero_limit_does_not_fetch_pages(monkeypatch):
    monkeypatch.setattr(background_jobs.config, "SIGNAL_DISCOVERY_WEB_FULLTEXT_LIMIT", 0)
    calls = []
    monkeypatch.setattr(signal_discovery, "_fetch_full_text", lambda *args, **kwargs: calls.append(args))

    config = signal_discovery.config_from_payload(background_jobs.daily_signal_discovery_payload())
    _, stats = signal_discovery._enrich_web_evidence_with_full_text(
        [_web_item("https://example.com/1")], "Бурение", limit=config.web_fulltext_limit
    )

    assert config.web_fulltext_limit == 0
    assert calls == []
    assert stats["attempted"] == 0


# --- 4. Дубль из ревью пачки — не брак ------------------------------------------------


def test_batch_review_duplicate_is_merged_not_rejected(monkeypatch):
    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: _review_client([
        _decision("main", keep=True, interest=80),
        _decision("retell", keep=False, duplicate_of="main", reason="тот же контракт"),
        _decision("noise", keep=False, reason="обзор рынка без нового факта"),
    ]))
    main, retell, noise = _candidate("main", 80), _candidate("retell", 60), _candidate("noise", 50)

    result = signal_discovery._batch_review_candidates([main, retell, noise], "Бурение", offline=False)

    assert (result["dropped"], result["duplicates"]) == (1, 1)
    assert retell["rejected"] is False
    assert retell["duplicate_of"] == {"signal_key": "main"}
    assert retell["duplicate_reason"] == "тот же контракт"
    assert noise["rejected"] is True
    assert "duplicate_of" not in noise


def test_batch_review_duplicate_chain_cycle_and_unknown_target(monkeypatch):
    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: _review_client([
        _decision("root", keep=True, interest=90),
        _decision("b", keep=False, duplicate_of="root"),
        _decision("a", keep=False, duplicate_of="b"),
        _decision("x", keep=False, duplicate_of="y"),
        _decision("y", keep=False, duplicate_of="x"),
        _decision("ghost", keep=False, duplicate_of="no-such-key"),
    ]))
    items = {key: _candidate(key) for key in ("root", "b", "a", "x", "y", "ghost")}

    signal_discovery._batch_review_candidates(list(items.values()), "Бурение", offline=False)

    # A → B → root: дубль идёт в оставшегося, звезда, а не цепочка.
    assert items["a"]["duplicate_of"] == {"signal_key": "root"}
    # X ↔ Y — модель противоречит себе: один остаётся, второй сливается в него.
    assert not items["x"]["rejected"] and not items["y"]["rejected"]
    assert "duplicate_of" not in items["x"]
    assert items["y"]["duplicate_of"] == {"signal_key": "x"}
    # Ссылка на несуществующий ключ — обычный отказ, как и без поля.
    assert items["ghost"]["rejected"] is True


def test_batch_review_duplicate_merges_into_primary_card(monkeypatch):
    """Одно событие — одна карточка с обеими ссылками; в обучение дубль идёт как duplicate."""
    upserted: list[str] = []
    links: list[tuple[int, str]] = []
    examples: list[tuple[str, str]] = []
    monkeypatch.setattr(repository, "signal_key_owners", lambda keys: {})
    monkeypatch.setattr(repository, "upsert_signal", lambda signal: upserted.append(signal["signal_key"]) or 10)
    monkeypatch.setattr(
        repository, "upsert_signal_evidence", lambda signal_id, item: links.append((signal_id, item["source_url"])) or 1
    )
    monkeypatch.setattr(repository, "refresh_signal_evidence_count", lambda signal_id: 2)
    monkeypatch.setattr(repository, "resolve_signal_merge_root", lambda signal_id: signal_id)
    monkeypatch.setattr(repository, "touch_signal", lambda signal_id: None)
    monkeypatch.setattr(
        repository,
        "create_signal_training_example",
        lambda **kwargs: examples.append((kwargs["signal_key"], kwargs["pipeline_verdict"])) or 1,
    )
    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: _review_client([
        _decision("main", keep=True, interest=80),
        _decision("retell", keep=False, duplicate_of="main", reason="тот же контракт"),
    ]))
    main = _candidate("main", 80, ["https://a.example/1"])
    retell = _candidate("retell", 60, ["https://b.example/2"])
    signal_discovery._batch_review_candidates([main, retell], "Бурение", offline=False)

    signal_discovery.apply_discovery(
        signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=False),
        {"topics": [{"topic": "Бурение", "candidates": [main, retell]}], "dedup": {}},
        generation_run_id=5,
    )

    assert upserted == ["main"]
    assert links == [(10, "https://a.example/1"), (10, "https://b.example/2")]
    assert examples == [("main", "accepted"), ("retell", "duplicate")]


def test_run_dedup_redirects_batch_duplicate_when_its_primary_is_merged(monkeypatch):
    main, retell = _candidate("main"), _candidate("retell")
    retell["duplicate_of"] = {"signal_key": "main"}
    paired: list[str] = []

    def fake_dedupe(nodes, **kwargs):
        paired.extend(node["signal"]["signal_key"] for node in nodes if node["kind"] == "new")
        return {"assigned": {1: (0, "то же событие")}, "stats": {"pairs": 1}}  # main → существующая №7

    monkeypatch.setattr(signal_discovery.signal_dedup, "dedupe", fake_dedupe)

    stats = signal_discovery._dedupe_run(
        signal_discovery.SignalDiscoveryConfig(offline=False),
        {"existing_signals": [{"id": 7, "evidence_urls": [], "signal_key": "old"}]},
        [{"candidates": [main, retell]}],
        lambda: None,
    )

    assert paired == ["main"]  # найденный ревью дубль в пары не идёт
    assert main["duplicate_of"] == {"signal_id": 7}
    assert retell["duplicate_of"] == {"signal_id": 7}  # звезда, а не цепочка
    assert stats["batch_duplicates"] == 1


# --- 5. Балл интереса хранится ------------------------------------------------------


def test_upsert_signal_keeps_interest_when_next_run_has_none(isolated_db):
    base = {"signal_key": "k-interest", "title": "T", "theme": "Бурение", "maturity": "watch", "score": 70}
    signal_id = repository.upsert_signal({**base, "interest_score": 88, "why_interesting": "первое внедрение"})
    repository.upsert_signal(dict(base))  # тема из одного кандидата ревью пачки не проходит

    row = next(item for item in repository.list_signals(limit=10) if item["id"] == signal_id)

    assert float(row["interest_score"]) == 88.0
    assert row["why_interesting"] == "первое внедрение"


# --- 7. Год --------------------------------------------------------------------------


def test_search_queries_use_current_year_not_2026(monkeypatch):
    from oiltech_digest.source_discovery import agent as source_agent

    queries: list[str] = []
    monkeypatch.setattr(signal_discovery, "_current_year", lambda: 2027)
    monkeypatch.setattr(signal_discovery, "feedback_query_hints", lambda topic, limit=8, **kwargs: [])
    monkeypatch.setattr(source_agent, "generate_search_queries", lambda topic, offline=True, limit=8, strategy="broad": [])

    def search(batch, limit=80):
        queries.extend(batch)
        # Слабая выдача (без отрасли и события) — второй раунд уходит в pivot.
        return {"status": "ok", "provider": "test", "results": [{"url": "https://e.example/1", "title": "Weather", "snippet": "Rain"}]}

    monkeypatch.setattr(source_agent, "search_web", search)

    signal_discovery._search_web_evidence(
        {"name": "Бурение", "query_seeds_json": ["directional drilling"]},
        signal_discovery.SignalDiscoveryConfig(web_query_limit=3, limit=5, research_rounds=2, web_fulltext_limit=0),
    )

    assert queries
    assert not [query for query in queries if "2026" in query]
    assert any(query.startswith("2027 ") for query in queries)


# --- 8. Итог задачи показывает новые шаги -------------------------------------------


def test_external_summary_shows_rounds_fulltext_and_batch_review(monkeypatch):
    monkeypatch.setattr(repository, "create_signal_generation_run", lambda **kwargs: None)
    monkeypatch.setattr(repository, "signal_key_owners", lambda keys: {})
    result = {
        "signal_discovery": True,
        "config": {"web_only": True, "offline": False, "dry_run": True},
        "run": {"topics": [{
            "topic": "Бурение",
            "web_search": {
                "status": "ok",
                "queries": ["q1", "q2"],
                "results": 7,
                "research_rounds": [{"mode": "initial"}, {"mode": "pivot"}],
                "fulltext": {"attempted": 5, "fetched": 3, "too_short": 1, "failed": 1, "skipped_budget": 0},
            },
            "batch_review": {"status": "ok", "source": "ai", "reviewed": 4, "dropped": 1, "duplicates": 1,
                             "decisions": [{"signal_key": "x"}], "interest_scores": {"y": 80}},
            "candidates": [],
        }]},
    }

    summary = signal_discovery.apply_external_result(result, job_id=1)

    topic = summary["topics"][0]
    assert topic["research_modes"] == ["initial", "pivot"]
    assert topic["fulltext"]["fetched"] == 3
    assert topic["batch_review"] == {"status": "ok", "source": "ai", "reviewed": 4, "dropped": 1, "duplicates": 1}


# --- 10. Внешняя очередь не исполняется на месте, конвейер — под профилем -------------


def test_enqueue_never_runs_external_queue_inline(monkeypatch, isolated_db):
    """20.09: планировщик без BACKGROUND_JOB_INLINE=0 выполнил радар из external-ai на РФ."""
    submitted = []
    monkeypatch.setattr(background_jobs.config, "BACKGROUND_JOB_INLINE", True)
    monkeypatch.setattr(background_jobs._executor, "submit", lambda *args, **kwargs: submitted.append(args))
    monkeypatch.setitem(background_jobs._HANDLERS, "test_queued", lambda payload, job_id: {"ok": True})

    external = background_jobs.enqueue("test_queued", {}, queue_name="external-ai", execution_region="external")
    local = background_jobs.enqueue("test_queued", {}, queue_name="default")

    assert repository.get_background_job(int(external["id"]))["status"] == "queued"
    assert [args[1] for args in submitted] == [int(local["id"])]


def test_daily_radar_routed_abroad_is_not_executed_by_the_enqueuing_process(monkeypatch, isolated_db):
    from oiltech_digest import network_policy

    submitted = []
    monkeypatch.setattr(background_jobs.config, "BACKGROUND_JOB_INLINE", True)
    monkeypatch.setattr(background_jobs.config, "SIGNAL_DISCOVERY_DAILY_ENABLED", True)
    monkeypatch.setattr(background_jobs._executor, "submit", lambda *args, **kwargs: submitted.append(args))
    monkeypatch.setattr(
        network_policy,
        "route_ai_processing",
        lambda: network_policy.ExecutionDecision("external-ai", "external", "openai", "ai_external_enabled"),
    )

    result = background_jobs.enqueue_daily_signal_discovery()

    assert result["enqueued"] is True
    assert result["job"]["queue_name"] == "external-ai"
    assert submitted == []


def test_bare_compose_up_starts_only_core_services():
    services = yaml.safe_load((Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text())["services"]

    unprofiled = {name for name, service in services.items() if not service.get("profiles")}

    assert unprofiled == {"db", "bootstrap", "agents-app"}
    for name in ("tasks", "worker", "playwright-worker", "scheduler"):
        assert services[name]["profiles"] == ["pipeline"]
        assert services[name]["environment"]["BACKGROUND_JOB_INLINE"] == "0"


def test_worker_result_does_not_carry_web_evidence_twice(monkeypatch):
    """Тексты страниц едут в кандидатах; в блоке web_search темы — только счётчики."""
    page = {**_web_item("https://example.com/a"), "extracted_fact": "Contract signed for oil and gas drilling automation."}
    monkeypatch.setattr(
        signal_discovery,
        "_search_web_evidence",
        lambda topic, config, **kwargs: {"status": "ok", "queries": ["q"], "results": 1, "evidence": [page]},
    )
    config = signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, web_only=True)

    run = signal_discovery.run_discovery(config, {"topics": [{"name": "Бурение"}], "tags": []})

    web = run["topics"][0]["web_search"]
    assert "evidence" not in web
    assert web["evidence_count"] == 1
    assert run["topics"][0]["candidates"][0]["signal"]["evidence"][0]["source_url"] == "https://example.com/a"


# --- Итог воркера — JSON (поймано проверкой 4712 после пересборки NL 21.09) -----------


def test_fetched_page_date_travels_as_iso_string(monkeypatch):
    """Докачка клала published_at объектом datetime — итог воркера не уходил ядру."""
    from datetime import datetime, timezone

    monkeypatch.setattr(
        signal_discovery,
        "_fetch_full_text",
        lambda url, fallback_title="", **kwargs: {
            "ok": True, "error": None, "title": "Drilling automation contract",
            "raw_text": "Oil and gas operator signed a drilling automation contract. " * 10,
            "published_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        },
    )

    evidence, _ = signal_discovery._enrich_web_evidence_with_full_text(
        [_web_item("https://example.com/a")], "Бурение", limit=5
    )

    assert evidence[0]["published_at"] == "2026-09-01T00:00:00+00:00"


def test_worker_sends_result_with_dates_to_core(monkeypatch):
    """Граница с ядром: любой вид задачи отдаёт дату строкой ISO, а не падает на отправке
    (18.09 — сбор, 21.09 — радар: один и тот же класс дважды)."""
    import json as jsonlib
    from datetime import datetime, timezone

    from oiltech_digest import external_worker

    client = external_worker.ExternalWorkerClient(
        core_api_url="https://core.example", token="t", worker_id="w", queues=["external-ai"], capabilities=["openai"]
    )
    sent = {}

    class Response:
        def raise_for_status(self):
            return None

    def post(url, json=None, timeout=None):
        sent["body"] = jsonlib.dumps(json)  # как requests: без default
        return Response()

    monkeypatch.setattr(client.session, "post", post)

    client.complete(
        {"id": 1, "lease_token": "x"},
        {"run": {"evidence": [{"published_at": datetime(2026, 9, 1, tzinfo=timezone.utc)}]}},
    )

    assert '"published_at": "2026-09-01T00:00:00+00:00"' in sent["body"]


def test_core_stores_iso_published_at_from_worker(isolated_db):
    signal_id = repository.upsert_signal({"signal_key": "k-date", "title": "T", "theme": "Бурение", "score": 60})

    repository.upsert_signal_evidence(
        signal_id, {"source_url": "https://example.com/a", "title": "T", "published_at": "2026-09-01T00:00:00+00:00"}
    )

    stored = repository.list_signal_evidence(signal_id)[0]["published_at"]
    assert stored.isoformat().startswith("2026-09-01")
