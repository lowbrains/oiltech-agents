"""Одна плохая страница не роняет источник; внешняя очередь не исполняется на месте.

21.09 у агентов пустое тело (200) дало lxml ParserError «Document is empty» — это не
ValueError, и его не ловило ни одно место разбора. В MVP-1 тот же разбор: одна такая
статья обрывала весь источник. Каждый тест здесь падает на коде до правки."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from lxml import etree

from oiltech_digest import background_jobs
from oiltech_digest.db import repository
from oiltech_digest.ingestion import external_fetch, normalize, request_parser
from oiltech_digest.ingestion.request_parser import CandidateLink

EMPTY_BODIES = [b"", b"   \n", "", b"<!-- -->"]


@pytest.mark.parametrize("body", EMPTY_BODIES)
def test_parse_html_turns_empty_document_into_value_error(body):
    with pytest.raises(ValueError):
        normalize.parse_html(body)


@pytest.mark.parametrize("body", EMPTY_BODIES)
def test_parse_article_page_survives_empty_body(body):
    assert request_parser.parse_article_page(body, "Заголовок из листинга") == ("Заголовок из листинга", None, "")


def test_extract_candidate_links_survives_empty_listing():
    assert request_parser.extract_candidate_links({"id": 1}, "https://example.com/news", b"") == []


def test_fetch_article_candidate_with_empty_page_is_just_skipped(monkeypatch):
    monkeypatch.setattr(request_parser, "fetch", lambda url: b"")
    candidate = CandidateLink("https://example.com/empty", "Статья", 10, None)

    assert request_parser.fetch_article_candidate(candidate, {"id": 1}) is None


def _candidates():
    return [
        CandidateLink("https://example.com/broken", "Сломанная", 10, datetime(2026, 9, 20, tzinfo=timezone.utc)),
        CandidateLink("https://example.com/good", "Хорошая", 9, datetime(2026, 9, 20, tzinfo=timezone.utc)),
    ]


def _fetch_or_raise(candidate, source):
    if candidate.url.endswith("broken"):
        raise etree.XMLSyntaxError("битая разметка", None, 1, 1)
    return {
        "source_id": source["id"],
        "title": candidate.title,
        "url": candidate.url,
        "published_at": candidate.published_at,
        "raw_text": "Достаточно длинный текст статьи о бурении. " * 10,
        "text_truncated": False,
        "language": "ru",
        "content_hash": "hash",
    }


def test_external_worker_keeps_listing_when_one_article_raises(monkeypatch):
    monkeypatch.setattr(external_fetch, "should_keep_article", lambda title, text, source: type("R", (), {"keep": True})())

    result = external_fetch._articles_from_candidates(
        {"id": 7}, _candidates(), {}, _fetch_or_raise, lambda candidates: "hash"
    )

    assert [article["url"] for article in result["articles"]] == ["https://example.com/good"]
    assert result["stats"]["failed_fetch"] == 1


def test_local_collection_keeps_listing_when_one_article_raises(monkeypatch):
    inserted = []
    monkeypatch.setattr(request_parser, "should_keep_article", lambda title, text, source: type("R", (), {"keep": True})())
    monkeypatch.setattr(request_parser.repository, "article_exists", lambda url: False)
    monkeypatch.setattr(request_parser.repository, "insert_article", lambda article: inserted.append(article["url"]) or True)
    monkeypatch.setattr(request_parser.repository, "touch_last_parsed", lambda source_id: None)
    monkeypatch.setattr(request_parser.repository, "update_source_request_state", lambda source_id, **kwargs: None)

    stats = request_parser.insert_candidates({"id": 7}, _candidates(), article_fetcher=_fetch_or_raise)

    assert inserted == ["https://example.com/good"]
    assert stats["added"] == 1


def test_enqueue_never_runs_external_queue_inline(monkeypatch, isolated_db):
    """У агентов 20.09 планировщик без BACKGROUND_JOB_INLINE=0 выполнил радар из
    external-ai прямо на РФ-ядре. Маршрут задачи сильнее флага окружения."""
    submitted = []
    monkeypatch.setattr(background_jobs.config, "BACKGROUND_JOB_INLINE", True)
    monkeypatch.setattr(background_jobs._executor, "submit", lambda *args, **kwargs: submitted.append(args))
    monkeypatch.setitem(background_jobs._HANDLERS, "test_queued", lambda payload, job_id: {"ok": True})

    external = background_jobs.enqueue("scrape_source", {"source_id": 1}, queue_name="external-fetch", execution_region="external")
    local = background_jobs.enqueue("test_queued", {}, queue_name="default")

    assert repository.get_background_job(int(external["id"]))["status"] == "queued"
    assert [args[1] for args in submitted] == [int(local["id"])]


def test_every_long_running_service_disables_inline_execution():
    services = yaml.safe_load((Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text())["services"]
    # Сервис приложения зовётся по-разному (app у основного стека, agents-app у агентного,
    # чтобы не делить имя в общей сети) — проверяются все долгоживущие сервисы из образа.
    long_running = {
        name for name, service in services.items()
        if "build" in service and service.get("restart") == "unless-stopped"
    }

    assert {"tasks", "worker", "playwright-worker", "scheduler"} < long_running
    for name in long_running:
        assert services[name]["environment"]["BACKGROUND_JOB_INLINE"] == "0", name


def test_worker_sends_result_with_dates_to_core(monkeypatch):
    """Граница с ядром: дата в любом поле итога — строкой ISO, а не падение отправки уже
    сделанной работы (18.09 — сбор MVP-1, 21.09 — радар агентов: один класс дважды)."""
    import json as jsonlib
    from decimal import Decimal

    from oiltech_digest import external_worker

    client = external_worker.ExternalWorkerClient(
        core_api_url="https://core.example", token="t", worker_id="w", queues=["external-fetch"], capabilities=[]
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
        {"articles": [{"published_at": datetime(2026, 9, 1, tzinfo=timezone.utc), "score": Decimal("7.5")}]},
    )

    assert '"published_at": "2026-09-01T00:00:00+00:00"' in sent["body"]
    assert '"score": 7.5' in sent["body"]


def test_worker_still_refuses_unknown_objects_loudly():
    from oiltech_digest import external_worker

    with pytest.raises(TypeError):
        external_worker.json_ready({"tags": {"a", "b"}})
