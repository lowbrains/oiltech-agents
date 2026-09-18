"""Радар через внешний воркер: снимок на ядре → прогон без базы → запись на ядре.

И обучение на ОС заказчика: чему учимся искать, что показываем судье, что не
возвращаем повторно."""

import json

import pytest

from oiltech_digest import background_jobs, external_worker, signal_discovery, signal_feedback
from oiltech_digest.db import repository
from oiltech_digest.source_discovery import agent as source_agent


DRILLING = "Бурение, направленное бурение, буровые растворы и буровое оборудование"

TAGS = [
    {
        "id": 20,
        "parent_id": None,
        "name": DRILLING,
        "name_en": "Drilling",
        "description": "Строительство скважин и буровое оборудование",
        "keywords_json": ["бурение"],
        "keywords_en_json": ["closed-loop drilling"],
        "negative_keywords_json": ["лотерея"],
    },
    {
        "id": 32,
        "parent_id": None,
        "name": repository.SYSTEM_TAG_UNCLASSIFIED,
        "name_en": "Unclassified",
        "description": "",
        "keywords_json": [],
        "keywords_en_json": [],
        "negative_keywords_json": [],
    },
]

MEMORY = {
    "signal_query_hint": [
        {"subject": "2026 closed-loop drilling automation oil gas", "score": 65,
         "facts_json": {"verdict": "approved"}},
        {"subject": "2026 lithium brine extraction oil gas", "score": 65,
         "facts_json": {"verdict": "approved"}},
    ],
}


def _signal(topic: str, title: str) -> dict:
    return signal_discovery._normalize_signal_payload(
        {
            "title": title,
            "title_ru": title,
            "theme": "HSE/бурение/что-то своё",
            "summary": "Оператор внедрил автоматизацию бурения на 40 скважинах.",
            "thesis": "Контракт и внедрение с цифрами.",
            "transferability": "Переносимо в нефтесервис.",
            "maturity": "shortlist",
            "confidence": 0.8,
            "score": 72,
            "why_now": "Контракт 2026 года.",
            "why_not_noise": "Есть заказчик и объём.",
            "companies": ["Operator"],
            "industries": ["oil and gas"],
        },
        topic,
    )


@pytest.fixture
def core_repository(monkeypatch):
    monkeypatch.setattr(signal_discovery.app_config, "SIGNAL_RADAR_TOPIC_SOURCE", "tags")
    monkeypatch.setattr(repository, "list_enabled_tags", lambda: TAGS)
    monkeypatch.setattr(
        repository,
        "list_signal_agent_memory",
        lambda memory_type=None, status="active", limit=50: MEMORY.get(memory_type, [])[:limit],
    )
    monkeypatch.setattr(repository, "list_reviewed_signal_urls", lambda: ["https://known.example/old/"])


def _take_database_away(monkeypatch):
    def no_database(*args, **kwargs):
        raise RuntimeError("у внешнего воркера нет базы")

    for name in (
        "list_enabled_tags",
        "list_signal_agent_memory",
        "list_signal_radar_topics",
        "list_reviewed_signal_urls",
        "list_signal_article_evidence",
    ):
        monkeypatch.setattr(repository, name, no_database)


def test_worker_run_gets_tags_memory_and_reviewed_urls_from_snapshot(monkeypatch, core_repository):
    payload = signal_discovery.build_external_payload(
        {"web_only": True, "offline": False, "max_signals": 3, "web_query_limit": 6}
    )
    json.dumps(payload)  # уезжает воркеру по HTTP

    # Служебный приёмник темой радара не становится.
    assert [topic["name"] for topic in payload["snapshot"]["topics"]] == [DRILLING]

    _take_database_away(monkeypatch)
    seen_queries: list[str] = []
    monkeypatch.setattr(
        source_agent,
        "generate_search_queries",
        lambda topic, offline=True, limit=8, strategy="broad": [],
    )

    def fake_search(queries, limit=20):
        seen_queries.extend(queries)
        return {
            "status": "ok",
            "provider": "test",
            "results": [
                {"url": "https://new.example/rig", "title": "Operator deploys closed-loop drilling automation on 40 wells",
                 "snippet": "Drilling automation contract, oil and gas wells", "query": queries[0], "provider": "test"},
                {"url": "https://known.example/old", "title": "Reviewed drilling item",
                 "snippet": "Drilling rig in oil and gas", "query": queries[0], "provider": "test"},
                {"url": "https://lottery.example/x", "title": "Лотерея: бурение на нефть",
                 "snippet": "лотерея нефть газ бурение", "query": queries[0], "provider": "test"},
            ],
        }

    monkeypatch.setattr(source_agent, "search_web", fake_search)
    judged: list[list[dict]] = []

    def fake_judge(cluster, topic, offline=True):
        judged.append(cluster)
        return _signal(topic, cluster[0]["title"]), {"raw": True}

    monkeypatch.setattr(signal_discovery, "judge_signal_snapshot", fake_judge)
    beats: list[int] = []

    result = signal_discovery.process_external_payload(payload, heartbeat=lambda: beats.append(1))

    json.dumps(result)  # уходит ядру по HTTP
    # Подсказка из одобренного по ЭТОЙ теме дошла; литий — чужая тема, не дошёл.
    assert "2026 closed-loop drilling automation oil gas" in seen_queries
    assert "2026 lithium brine extraction oil gas" not in seen_queries
    # Ключи тематики из снимка тегов попали в запросы.
    assert any("closed-loop drilling" in query and "news" in query for query in seen_queries)
    judged_urls = [item["source_url"] for cluster in judged for item in cluster]
    assert judged_urls == ["https://new.example/rig"]  # разобранное и стоп-слово — мимо судьи
    topic = result["run"]["topics"][0]
    assert topic["skipped_reviewed"] == 1
    assert topic["candidates"][0]["signal"]["theme"] == DRILLING
    assert beats  # воркер продлевает аренду задачи по ходу прогона


def test_core_applies_external_result_and_records_generation_run(monkeypatch):
    created: dict = {}
    upserted: list[dict] = []
    examples: list[dict] = []
    finished: list[tuple] = []
    monkeypatch.setattr(repository, "create_signal_generation_run", lambda **kwargs: created.update(kwargs) or 77)
    monkeypatch.setattr(repository, "upsert_signal", lambda signal: upserted.append(signal) or 10)
    monkeypatch.setattr(repository, "upsert_signal_evidence", lambda signal_id, item: 20)
    monkeypatch.setattr(repository, "refresh_signal_evidence_count", lambda signal_id: 1)
    monkeypatch.setattr(repository, "create_signal_training_example", lambda **kwargs: examples.append(kwargs) or 1)
    monkeypatch.setattr(
        repository, "finish_signal_generation_run", lambda run_id, **kwargs: finished.append((run_id, kwargs))
    )
    accepted = _signal(DRILLING, "Accepted")
    accepted.update({"signal_key": "k1", "evidence_count": 1,
                     "evidence": [{"source_url": "https://new.example/rig", "title": "Accepted"}]})
    rejected = {**_signal(DRILLING, "Rejected"), "maturity": "reject", "signal_key": "k2", "evidence_count": 1,
                "evidence": [{"source_url": "https://bad.example", "title": "Rejected"}]}
    result = {
        "signal_discovery": True,
        "config": {"web_only": True, "offline": False, "dry_run": False, "max_signals": 3},
        "run": {"topics": [{
            "topic": DRILLING,
            "total_evidence": 2,
            "skipped_reviewed": 0,
            "clusters": 2,
            "web_search": {"status": "ok", "queries": ["q1", "q2"], "results": 3},
            "candidates": [
                {"signal": accepted, "raw_output": {}, "rejected": False, "training_input": {"topic": DRILLING}},
                {"signal": rejected, "raw_output": {}, "rejected": True, "training_input": {"topic": DRILLING}},
            ],
        }]},
    }

    summary = signal_discovery.apply_external_result(result, job_id=55)

    assert created["background_job_id"] == 55
    assert [signal["title"] for signal in upserted] == ["Accepted"]  # отказ судьи не пишется в радар
    assert len(examples) == 2  # но в обучающие примеры идут оба
    assert finished == [(77, {"status": "ok", "result": {"topics": 1, "signals": 1, "returned_signals": 1}})]
    assert summary["signals"] == 1
    assert summary["topics"][0] == {
        "topic": DRILLING, "web_status": "ok", "queries": 2, "results": 3, "total_evidence": 2,
        "skipped_reviewed": 0, "clusters": 2, "signals": 1,
    }


class _FakeClient:
    def __init__(self):
        self.completed: list[dict] = []
        self.failed: list[str] = []

    def progress(self, job, progress):
        pass

    def heartbeat(self, job):
        pass

    def complete(self, job, result):
        self.completed.append(result)

    def fail(self, job, error, *, retryable=True, retry_after_seconds=300):
        self.failed.append(error)


def test_external_worker_handles_signal_discovery_kind(monkeypatch):
    monkeypatch.setattr(
        signal_discovery,
        "process_external_payload",
        lambda payload, heartbeat=None: {"signal_discovery": True, "run": {"topics": []}, "config": payload["config"]},
    )
    client = _FakeClient()

    external_worker._handle_job(client, {"id": 1, "kind": "signal_discovery", "payload": {"config": {"web_only": True}}})

    assert client.failed == []  # прежде: Unsupported external job kind: signal_discovery
    assert client.completed == [{"signal_discovery": True, "run": {"topics": []}, "config": {"web_only": True}}]


def test_topics_from_tags_take_root_customer_tags_only():
    tags = [
        *TAGS,
        {"id": 40, "parent_id": 20, "name": "Буровые растворы", "description": ""},
    ]

    topics = signal_discovery.topics_from_tags(tags)

    assert topics == [{"name": DRILLING, "description": "Строительство скважин и буровое оборудование",
                       "query_seeds_json": [], "tag_id": 20}]


def test_topic_tag_context_takes_exactly_the_named_tag():
    neighbours = [
        TAGS[0],
        {"id": 24, "parent_id": None, "name": "Добыча, механизированный фонд и внутрискважинное оборудование",
         "keywords_json": ["ЭЦН"], "keywords_en_json": ["ESP"], "negative_keywords_json": []},
        {"id": 41, "parent_id": 20, "name": "Буровые растворы", "keywords_json": ["раствор"],
         "keywords_en_json": [], "negative_keywords_json": []},
    ]

    selected = signal_discovery._select_topic_tags(DRILLING, neighbours)

    # Общее слово «оборудование» больше не притягивает «Добычу».
    assert [tag["id"] for tag in selected] == [20, 41]


# --- обучение на ОС ---------------------------------------------------------

_LITHIUM = {
    "Комментарий": "Школьный проект «Больших вызовов», промышленного внедрения нет",
    "Сигнал": "Добыча лития из попутно добываемой воды",
    "URL": "https://example.com/lithium",
    "reason": "Образовательный проект, не сигнал",
}


@pytest.mark.parametrize("verdict", ["reject", "wrong_domain", "too_generic", "merge_duplicate"])
def test_rejected_feedback_does_not_become_search_hint(verdict):
    memories = signal_feedback.extract_feedback_memories({**_LITHIUM, "verdict": verdict})

    assert [m for m in memories if m["memory_type"] == "signal_query_hint"] == []


def test_approved_feedback_still_becomes_search_hint():
    memories = signal_feedback.extract_feedback_memories({**_LITHIUM, "verdict": "approved"})

    assert [m["subject"] for m in memories if m["memory_type"] == "signal_query_hint"]


def test_prompt_block_shows_rejected_examples_even_when_outscored():
    approved = [
        {"subject": "approved", "score": 90, "facts_json": {"signal_title": f"Good {i}", "reason": "ok"}}
        for i in range(25)
    ]
    rejected = [
        {"subject": "reject", "score": -80,
         "facts_json": {"signal_title": "Обзор дронов 2019 года", "reason": "Обзор без события. " * 200}},
    ]
    with signal_feedback.use_memory_snapshot({"signal_verdict": approved + rejected}):
        block = signal_feedback.feedback_prompt_block("Бурение")

    assert "verdict=reject signal=Обзор дронов 2019 года" in block
    assert block.count("verdict=approved") == signal_feedback.PROMPT_POSITIVE_EXAMPLES
    assert max(len(line) for line in block.splitlines()) < 600  # развёрнутая причина обрезана


def test_feedback_query_hints_keep_only_hints_of_the_topic():
    with signal_feedback.use_memory_snapshot(MEMORY):
        hints = signal_feedback.feedback_query_hints(
            DRILLING, topic_terms=signal_feedback.topic_term_stems(DRILLING, "closed-loop drilling")
        )

    assert hints == ["2026 closed-loop drilling automation oil gas"]


def test_retire_query_hints_from_negative_feedback(isolated_db):
    for key, verdict, comment in (
        ("h-reject", "reject", ""),
        ("h-approved", "approved", ""),
        ("h-legacy", None, "Источник отличный, первичный"),
        ("h-legacy-neutral", None, "Проверить дату"),
    ):
        repository.upsert_signal_agent_memory(
            memory_key=key,
            memory_type="signal_query_hint",
            subject=f"2026 {key} oil gas",
            facts={"verdict": verdict, "comment": comment},
        )

    dry = signal_feedback.retire_query_hints_from_negative_feedback(dry_run=True)
    assert dry["retired"] == 1
    assert len(repository.list_signal_agent_memory(memory_type="signal_query_hint")) == 4  # сухой прогон не пишет

    result = signal_feedback.retire_query_hints_from_negative_feedback(dry_run=False)

    active = {row["subject"] for row in repository.list_signal_agent_memory(memory_type="signal_query_hint")}
    assert result["retired"] == 1
    # Без вердикта — первая таблица заказчика: не гасим, полярность маркерами не определить.
    assert active == {"2026 h-approved oil gas", "2026 h-legacy oil gas", "2026 h-legacy-neutral oil gas"}


def test_failed_daily_radar_is_not_requeued_the_same_day(isolated_db, monkeypatch):
    monkeypatch.setattr(background_jobs.config, "SIGNAL_DISCOVERY_DAILY_ENABLED", True)
    with repository.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO background_jobs (kind, queue_name, status, payload_json, max_attempts)
            VALUES ('signal_discovery', 'external-ai', 'failed', %s::jsonb, 1)
            """,
            (json.dumps(background_jobs.daily_signal_discovery_payload()),),
        )
        conn.commit()
    enqueued: list = []
    monkeypatch.setattr(background_jobs, "enqueue", lambda *args, **kwargs: enqueued.append(args))

    result = background_jobs.enqueue_daily_signal_discovery()

    assert result["enqueued"] is False and result["reason"] == "already_scheduled"
    assert enqueued == []
