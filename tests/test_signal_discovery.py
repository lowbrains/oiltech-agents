import pytest

from oiltech_digest import signal_discovery


@pytest.fixture(autouse=True)
def no_signal_feedback_memory(monkeypatch):
    monkeypatch.setattr(signal_discovery, "apply_feedback_glossary", lambda text, topic=None: text)
    # Темы — из таблицы, которую тесты подменяют; фильтр разобранного — пустой.
    # Иначе разведка пошла бы в настоящую локальную базу за тегами и адресами.
    monkeypatch.setattr(signal_discovery.app_config, "SIGNAL_RADAR_TOPIC_SOURCE", "table")
    monkeypatch.setattr(signal_discovery.repository, "list_reviewed_signal_urls", lambda: [])


def test_default_radar_topics_cover_business_directions():
    names = [topic["name"] for topic in signal_discovery.DEFAULT_RADAR_TOPICS]
    expected_fragments = [
        "Сейсморазведка",
        "ГИС",
        "Бурение",
        "Цементирование",
        "Заканчивание",
        "ГРП",
        "КРС",
        "Добыча",
        "Повышение нефтеотдачи",
        "Промысловая инфраструктура",
        "Энергетика",
        "Роботизация",
        "Логистика",
        "Экология",
        "Лабораторные",
        "Инжиниринг",
        "Рынок",
    ]

    assert len(signal_discovery.DEFAULT_RADAR_TOPICS) >= 21
    for fragment in expected_fragments:
        assert any(fragment in name for name in names), fragment


def test_business_radar_topics_have_chinese_query_seeds():
    for topic in signal_discovery.BUSINESS_RADAR_TOPICS:
        seeds = " ".join(topic["query_seeds"])
        assert signal_discovery._contains_cjk(seeds), topic["name"]


def test_discover_signals_builds_radar_signal_from_article_evidence(monkeypatch):
    saved = {"signals": 0, "evidence": 0}

    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_radar_topics",
        lambda enabled_only=True: [{"name": "HSE robotics / Physical AI", "query_seeds_json": []}],
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_article_evidence",
        lambda **kwargs: [
            {
                "article_id": 1,
                "title": "ADNOC selected autonomous inspection robots for hazardous sites",
                "title_ru": "ADNOC выбрала автономных роботов для опасных объектов",
                "source_url": "https://example.com/adnoc-robots",
                "published_at": None,
                "collected_at": None,
                "raw_text": "Autonomous robot supplier will inspect equipment and reduce human exposure.",
                "publisher": "Industrial News",
                "summary": "ADNOC допустила поставщика autonomous robots для инспекций и environmental monitoring.",
                "relevant": True,
                "relevance_reason": "HSE robotics",
                "tag_name": "HSE",
                "tag_name_en": "HSE",
                "total_score": 85,
                "score_label": "Высокая",
                "score_explanation": "Промышленный HSE-сценарий",
            },
            {
                "article_id": 2,
                "title": "Physical AI robots reduce hazardous work",
                "title_ru": "Physical AI снижает присутствие людей в опасных операциях",
                "source_url": "https://example.com/physical-ai",
                "published_at": None,
                "collected_at": None,
                "raw_text": "Robotic deployment for oilfield hazardous operations includes autonomous navigation.",
                "publisher": "Robotics Wire",
                "summary": "Robotic deployment переносит инспекции в автономный контур.",
                "relevant": True,
                "relevance_reason": "HSE robotics",
                "tag_name": "HSE",
                "tag_name_en": "HSE",
                "total_score": 80,
                "score_label": "Высокая",
                "score_explanation": "Снижение exposure",
            },
        ],
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "upsert_signal",
        lambda payload: saved.__setitem__("signals", saved["signals"] + 1) or 10,
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "upsert_signal_evidence",
        lambda signal_id, evidence: saved.__setitem__("evidence", saved["evidence"] + 1) or 20,
    )
    monkeypatch.setattr(signal_discovery.repository, "refresh_signal_evidence_count", lambda signal_id: 1)

    result = signal_discovery.discover_signals(
        signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True)
    )

    assert result["signals"]
    assert result["signals"][0]["maturity"] in {"watch", "shortlist", "proven"}
    assert result["signals"][0]["evidence_count"] == 2
    assert saved == {"signals": 0, "evidence": 0}


def test_discover_signals_persists_when_not_dry_run(monkeypatch):
    saved = {"signals": 0, "evidence": 0}

    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_radar_topics",
        lambda enabled_only=True: [{"name": "Predictive HSE", "query_seeds_json": []}],
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_article_evidence",
        lambda **kwargs: [
            {
                "article_id": 3,
                "title": "Predictive safety deployment detects hazardous equipment condition",
                "title_ru": "Predictive safety выявляет опасное состояние оборудования",
                "source_url": "https://example.com/predictive-safety",
                "published_at": None,
                "raw_text": "Deployment uses predictive analytics for hazardous condition detection.",
                "publisher": "Mining Safety",
                "summary": "Deployment predicts hazardous condition before incidents.",
                "relevant": True,
                "total_score": 90,
            }
        ],
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "upsert_signal",
        lambda payload: saved.__setitem__("signals", saved["signals"] + 1) or 10,
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "upsert_signal_evidence",
        lambda signal_id, evidence: saved.__setitem__("evidence", saved["evidence"] + 1) or 20,
    )
    monkeypatch.setattr(signal_discovery.repository, "refresh_signal_evidence_count", lambda signal_id: 1)

    result = signal_discovery.discover_signals(
        signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=False, persist_training_examples=False)
    )

    assert result["signals"]
    assert saved == {"signals": 1, "evidence": 1}


def test_discover_signals_persists_generation_training_examples(monkeypatch):
    captured = {"examples": []}

    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_radar_topics",
        lambda enabled_only=True: [{"name": "Predictive HSE", "query_seeds_json": []}],
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_article_evidence",
        lambda **kwargs: [
            {
                "article_id": 3,
                "title": "Predictive safety deployment detects hazardous equipment condition",
                "title_ru": "Predictive safety выявляет опасное состояние оборудования",
                "source_url": "https://example.com/predictive-safety",
                "published_at": None,
                "raw_text": "Deployment uses predictive analytics for hazardous condition detection in oil and gas.",
                "publisher": "Mining Safety",
                "summary": "Deployment predicts hazardous condition before incidents.",
                "relevant": True,
                "total_score": 90,
            }
        ],
    )
    monkeypatch.setattr(signal_discovery.repository, "create_signal_generation_run", lambda **kwargs: 77)
    monkeypatch.setattr(signal_discovery.repository, "finish_signal_generation_run", lambda *args, **kwargs: captured.update({"finished": (args, kwargs)}))
    monkeypatch.setattr(signal_discovery.repository, "upsert_signal", lambda payload: 10)
    monkeypatch.setattr(signal_discovery.repository, "upsert_signal_evidence", lambda signal_id, evidence: 20)
    monkeypatch.setattr(signal_discovery.repository, "refresh_signal_evidence_count", lambda signal_id: 1)
    monkeypatch.setattr(
        signal_discovery.repository,
        "create_signal_training_example",
        lambda **kwargs: captured["examples"].append(kwargs) or 101,
    )

    result = signal_discovery.discover_signals(
        signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=False)
    )

    assert result["generation_run_id"] == 77
    assert captured["examples"]
    example = captured["examples"][0]
    assert example["generation_run_id"] == 77
    assert example["signal_id"] == 10
    assert example["pipeline_verdict"] == "accepted"
    assert example["input_payload"]["topic"] == "Predictive HSE"
    assert example["input_payload"]["evidence"][0]["source_url"] == "https://example.com/predictive-safety"
    assert example["normalized_output"]["signal_key"]
    assert captured["finished"][1]["status"] == "ok"


def test_discover_signals_filters_reject_title_even_when_maturity_watch(monkeypatch):
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_radar_topics",
        lambda enabled_only=True: [{"name": "Predictive HSE", "query_seeds_json": []}],
    )
    monkeypatch.setattr(signal_discovery.repository, "list_signal_article_evidence", lambda **kwargs: [])
    monkeypatch.setattr(
        signal_discovery,
        "_search_web_evidence",
        lambda topic, config: {
            "status": "ok",
            "provider": "test",
            "queries": [],
            "results": 1,
            "evidence": [
                {
                    "source_url": "https://example.com/noise",
                    "title": "Oil and gas generic webinar",
                    "publisher": "example.com",
                    "extracted_fact": "No confirmed deployment in oil and gas.",
                    "summary_ru": "Нет подтвержденного внедрения.",
                    "strength": 0.7,
                }
            ],
        },
    )
    # Разведка зовёт judge_signal_snapshot. Подмена judge_signal сюда не доходила, и тест
    # шёл в настоящий OpenAI: с ключом в окружении «проходил» живым запросом, без ключа падал.
    monkeypatch.setattr(
        signal_discovery,
        "judge_signal_snapshot",
        lambda cluster, topic, offline=True: ({
            "title": "reject",
            "theme": topic,
            "summary": "Нет конкретного внедрения.",
            "thesis": "Нет конкретного внедрения.",
            "transferability": "",
            "maturity": "watch",
            "confidence": 0.4,
            "score": 0.5,
            "why_now": "",
            "why_not_noise": "Нет конкретного промышленного применения.",
            "companies": [],
            "industries": [],
        }, {}),
    )

    result = signal_discovery.discover_signals(
        signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=True, web_only=True)
    )

    assert result["signals"] == []


def test_signal_payload_normalizes_fractional_score_and_non_russian_theme():
    payload = signal_discovery._normalize_signal_payload(
        {
            "title": "Газпром нефть запускает беспилотные грузовики",
            "theme": "Industrial transport safety",
            "summary": "Промышленное внедрение автономной логистики.",
            "thesis": "Промышленное внедрение автономной логистики.",
            "transferability": "",
            "maturity": "watch",
            "confidence": 0.7,
            "score": 0.46,
            "why_now": "",
            "why_not_noise": "",
            "companies": [],
            "industries": [],
        },
        "Логистика, базы и сервисные подразделения",
    )

    assert payload["score"] == 46
    assert payload["theme"] == "Логистика, базы и сервисные подразделения"


def test_signal_key_merges_same_url_across_topics():
    evidence = [
        {
            "source_url": "https://www.example.com/news/autonomous-trucks?utm=1",
            "title": "Autonomous trucks launched in oilfield logistics",
            "strength": 0.8,
        }
    ]
    first = signal_discovery._signal_key({"title": "A", "companies": []}, evidence)
    second = signal_discovery._signal_key({"title": "B", "companies": []}, [
        {**evidence[0], "source_url": "http://example.com/news/autonomous-trucks#section"}
    ])

    assert first == second


def test_refresh_signal_evidence_count_uses_actual_linked_evidence(isolated_db):
    from oiltech_digest.db import repository

    signal_id = repository.upsert_signal(
        {
            "signal_key": "test-refresh-evidence-count",
            "title": "Автономная инспекция трубопровода",
            "theme": "Роботизация и автономные системы",
            "summary": "Тестовый сигнал",
            "thesis": "Тестовый сигнал",
            "transferability": "",
            "maturity": "watch",
            "confidence": 0.8,
            "score": 70,
            "why_now": "",
            "why_not_noise": "",
            "companies": [],
            "industries": [],
            "evidence_count": 9,
        }
    )
    repository.upsert_signal_evidence(
        signal_id,
        {
            "source_url": "https://example.com/pipeline-robot",
            "title": "Pipeline robot inspects oil and gas infrastructure",
            "publisher": "example.com",
            "evidence_type": "deployment",
            "strength": 0.8,
        },
    )

    assert repository.refresh_signal_evidence_count(signal_id) == 1
    stored = repository.list_signals(limit=1)[0]
    assert stored["evidence_count"] == 1


def test_discover_signals_filters_generic_ai_without_industry_context(monkeypatch):
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_radar_topics",
        lambda enabled_only=True: [{"name": "HSE robotics / Physical AI", "query_seeds_json": []}],
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_article_evidence",
        lambda **kwargs: [
            {
                "article_id": 1,
                "title": "AI robotics breakthrough for office workflows",
                "title_ru": "AI robotics breakthrough for office workflows",
                "source_url": "https://example.com/generic-ai",
                "published_at": None,
                "raw_text": "A software agent automates generic knowledge work.",
                "publisher": "Tech News",
                "summary": "Generic automation news for office teams.",
                "relevant": True,
                "total_score": 95,
            }
        ],
    )

    result = signal_discovery.discover_signals(
        signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True)
    )

    assert result["signals"] == []


def test_discover_signals_can_use_web_evidence_without_registered_sources(monkeypatch):
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_radar_topics",
        lambda enabled_only=True: [{"name": "Digital PTW / Control of Work", "query_seeds_json": []}],
    )
    monkeypatch.setattr(signal_discovery.repository, "list_signal_article_evidence", lambda **kwargs: [])
    monkeypatch.setattr(
        signal_discovery,
        "_search_web_evidence",
        lambda topic, config: {
            "status": "ok",
            "provider": "test",
            "queries": ["2026 electronic permit to work oil gas"],
            "results": 2,
            "evidence": [
                {
                    "article_id": None,
                    "source_url": "https://example.com/ptw",
                    "title": "Oil refinery deploys electronic permit to work with interlocks",
                    "title_ru": "НПЗ внедрил электронный наряд-допуск с цифровыми блокировками",
                    "publisher": "example.com",
                    "published_at": None,
                    "evidence_type": "deployment",
                    "extracted_fact": "Oil and gas operator moved high-risk work permits into a digital control system.",
                    "summary_ru": "Оператор нефтегаза перевёл наряды-допуски в цифровой контроль.",
                    "strength": 0.8,
                    "topic": "Digital PTW / Control of Work",
                    "raw_payload": {"evidence_source": "web_search"},
                },
                {
                    "article_id": None,
                    "source_url": "https://example.com/ptw-2",
                    "title": "Petrochemical operator reports 75% faster PTW approvals",
                    "title_ru": "Нефтехимический оператор ускорил согласование PTW на 75%",
                    "publisher": "example.com",
                    "published_at": None,
                    "evidence_type": "deployment",
                    "extracted_fact": "Petrochemical site reports digital permit to work deployment with 75% faster approvals.",
                    "summary_ru": "Нефтехимический объект внедрил электронный наряд-допуск и ускорил согласования на 75%.",
                    "strength": 0.8,
                    "topic": "Digital PTW / Control of Work",
                    "raw_payload": {"evidence_source": "web_search"},
                },
            ],
        },
    )

    result = signal_discovery.discover_signals(
        signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, web_search=True)
    )

    assert result["signals"]
    assert result["topic_results"][0]["web_search"]["status"] == "ok"
    assert result["topic_results"][0]["article_evidence"] == 0


def test_web_only_skips_local_article_evidence(monkeypatch):
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_radar_topics",
        lambda enabled_only=True: [{"name": "Digital PTW / Control of Work", "query_seeds_json": []}],
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_article_evidence",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("local articles must not be queried")),
    )
    monkeypatch.setattr(
        signal_discovery,
        "_search_web_evidence",
        lambda topic, config: {"status": "empty", "provider": "test", "queries": [], "results": 0, "evidence": []},
    )

    result = signal_discovery.discover_signals(
        signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, web_only=True)
    )

    assert result["web_search"] is True
    assert result["web_only"] is True
    assert result["topic_results"][0]["article_evidence"] == 0


def test_search_result_evidence_cleans_html_snippets():
    evidence = signal_discovery._search_result_to_evidence(
        {
            "url": "https://example.com/oil-gas-robots",
            "title": "Oil &amp; Gas <strong>robots</strong>",
            "snippet": "<strong>Robots</strong> inspect oil and gas facilities.",
            "provider": "test",
        },
        "HSE robotics / Physical AI",
    )

    assert evidence["title"] == "Oil & Gas robots"
    assert evidence["extracted_fact"] == "Robots inspect oil and gas facilities."


def test_web_search_prioritizes_feedback_query_hints(monkeypatch):
    from oiltech_digest.source_discovery import agent as source_agent

    captured = {}
    monkeypatch.setattr(
        signal_discovery,
        "feedback_query_hints",
        lambda topic, limit=8, **kwargs: ["2026 closed-loop rig automation oil gas deployment"],
    )
    monkeypatch.setattr(
        source_agent,
        "generate_search_queries",
        lambda topic, offline=True, limit=8, strategy="broad": ["2026 generic drilling automation news"],
    )

    def fake_search_web(queries, limit=80):
        captured["queries"] = queries
        return {
            "status": "ok",
            "provider": "test",
            "results": [
                {
                    "url": "https://example.com/closed-loop",
                    "title": "Oil and gas operator deploys closed-loop rig automation",
                    "snippet": "Closed-loop control improves drilling in oil and gas wells.",
                    "provider": "test",
                    "query": queries[0],
                }
            ],
        }

    monkeypatch.setattr(source_agent, "search_web", fake_search_web)

    result = signal_discovery._search_web_evidence(
        {"name": "Бурение", "query_seeds_json": ["drilling automation"]},
        signal_discovery.SignalDiscoveryConfig(web_query_limit=3, limit=5),
    )

    assert captured["queries"][0] == "2026 closed-loop rig automation oil gas deployment"
    assert result["evidence"]


def test_web_search_enriches_queries_with_tag_keywords(monkeypatch):
    from oiltech_digest.source_discovery import agent as source_agent

    captured = {}
    monkeypatch.setattr(signal_discovery, "feedback_query_hints", lambda topic, limit=8, **kwargs: [])
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_enabled_tags",
        lambda: [
            {
                "id": 1,
                "parent_id": None,
                "name": "Бурение",
                "name_en": "Drilling",
                "description": "Строительство нефтяных скважин",
                "keywords_json": ["управление бурением с замкнутым контуром"],
                "keywords_en_json": ["closed-loop drilling", "automated drilling rig"],
                "negative_keywords_json": ["construction drilling"],
            },
            {
                "id": 2,
                "parent_id": 1,
                "name": "Направленное бурение",
                "name_en": "Directional drilling",
                "description": "",
                "keywords_json": ["геонавигация"],
                "keywords_en_json": ["geosteering"],
                "negative_keywords_json": [],
            },
        ],
    )
    def fake_generate_search_queries(topic, offline=True, limit=8, strategy="broad"):
        captured["generation_topic"] = topic
        return ["generic drilling news"]

    monkeypatch.setattr(source_agent, "generate_search_queries", fake_generate_search_queries)

    def fake_search_web(queries, limit=80):
        captured["queries"] = queries
        return {
            "status": "ok",
            "provider": "test",
            "results": [
                {
                    "url": "https://example.com/closed-loop",
                    "title": "Oil and gas operator deploys closed-loop drilling",
                    "snippet": "Closed-loop drilling improves well construction in oil and gas.",
                    "provider": "test",
                    "query": queries[0],
                }
            ],
        }

    monkeypatch.setattr(source_agent, "search_web", fake_search_web)

    result = signal_discovery._search_web_evidence(
        {"name": "Бурение, направленное бурение, растворы и буровое оборудование", "query_seeds_json": []},
        signal_discovery.SignalDiscoveryConfig(web_query_limit=5, limit=5),
    )

    assert any("closed-loop drilling" in query for query in captured["queries"])
    assert "closed-loop drilling" in captured["generation_topic"]
    assert "construction drilling" in captured["generation_topic"]
    assert result["tag_context"]["keywords_en"][0] == "closed-loop drilling"
    assert result["evidence"]


def test_web_search_filters_results_by_tag_negative_keywords(monkeypatch):
    from oiltech_digest.source_discovery import agent as source_agent

    monkeypatch.setattr(signal_discovery, "feedback_query_hints", lambda topic, limit=8, **kwargs: [])
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_enabled_tags",
        lambda: [
            {
                "id": 1,
                "parent_id": None,
                "name": "Бурение",
                "name_en": "Drilling",
                "description": "",
                "keywords_json": ["бурение"],
                "keywords_en_json": ["drilling"],
                "negative_keywords_json": ["construction drilling"],
            }
        ],
    )
    monkeypatch.setattr(source_agent, "generate_search_queries", lambda topic, offline=True, limit=8, strategy="broad": [])

    def fake_search_web(queries, limit=80):
        return {
            "status": "ok",
            "provider": "test",
            "results": [
                {
                    "url": "https://example.com/noise",
                    "title": "Construction drilling robots for concrete sites",
                    "snippet": "No oil and gas deployment, only construction drilling equipment.",
                    "provider": "test",
                },
                {
                    "url": "https://example.com/useful",
                    "title": "Oil and gas operator deploys automated drilling rig",
                    "snippet": "The drilling system works on oil and gas wells.",
                    "provider": "test",
                },
            ],
        }

    monkeypatch.setattr(source_agent, "search_web", fake_search_web)

    result = signal_discovery._search_web_evidence(
        {"name": "Бурение", "query_seeds_json": []},
        signal_discovery.SignalDiscoveryConfig(web_query_limit=4, limit=5),
    )

    urls = [item["source_url"] for item in result["evidence"]]
    assert urls == ["https://example.com/useful"]
