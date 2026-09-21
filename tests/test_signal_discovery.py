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

    # Две статьи — два разных события (ADNOC выбрала роботов / обзор Physical AI):
    # ключ кластера «шаблон + отпечаток текста» (655cdbe) их больше не склеивает, как
    # склеивал голый шаблон «physical-ai-robotics». Одно событие в одну карточку сводят
    # дедуп и ревью пачки — см. test_batch_review_duplicate_merges_into_primary_card.
    assert len(result["signals"]) == 2
    assert {item["maturity"] for item in result["signals"]} <= {"watch", "shortlist", "proven"}
    assert [item["evidence_count"] for item in result["signals"]] == [1, 1]
    assert saved == {"signals": 0, "evidence": 0}


def test_discover_signals_persists_when_not_dry_run(monkeypatch):
    saved = {"signals": 0, "evidence": 0}
    # Сверка ключей и снимок для дедупа ходят в базу — в этом тесте её нет.
    monkeypatch.setattr(signal_discovery.repository, "signal_key_owners", lambda keys: {})
    monkeypatch.setattr(signal_discovery.repository, "list_signals_for_dedup", lambda: [])

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
    # Сверка ключей и снимок для дедупа ходят в базу — в этом тесте её нет.
    monkeypatch.setattr(signal_discovery.repository, "signal_key_owners", lambda keys: {})
    monkeypatch.setattr(signal_discovery.repository, "list_signals_for_dedup", lambda: [])

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
        lambda topic, config, **kwargs: {
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
        lambda topic, config, **kwargs: {
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
        lambda topic, config, **kwargs: {"status": "empty", "provider": "test", "queries": [], "results": 0, "evidence": []},
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


def test_signal_judge_prompt_requires_russian_user_fields():
    instructions = signal_discovery.SIGNAL_JUDGE_INSTRUCTIONS
    schema = signal_discovery.SIGNAL_JUDGE_SCHEMA["schema"]

    assert "Все пользовательские текстовые поля возвращай на русском" in instructions
    assert "Не копируй англоязычный или китайский" in instructions
    assert "Названия компаний" in instructions
    assert "title_ru" in schema["required"]
    assert schema["properties"]["title_ru"] == {"type": "string"}


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
        signal_discovery.SignalDiscoveryConfig(web_query_limit=3, limit=5, research_rounds=1, web_fulltext_limit=0),
    )

    assert captured["queries"][0] == "2026 closed-loop rig automation oil gas deployment"
    assert "2026 generic drilling automation news" in captured["queries"]
    assert result["evidence"]


def test_web_search_keeps_generated_angles_when_seeds_are_many(monkeypatch):
    from oiltech_digest.source_discovery import agent as source_agent

    captured = {}
    monkeypatch.setattr(signal_discovery, "feedback_query_hints", lambda topic, limit=8, **kwargs: [])
    monkeypatch.setattr(
        source_agent,
        "generate_search_queries",
        lambda topic, offline=True, limit=8, strategy="broad": ["2026 rare cementing deployment operator case study"],
    )

    def fake_search_web(queries, limit=80):
        captured["queries"] = queries
        return {"status": "ok", "provider": "test", "results": []}

    monkeypatch.setattr(source_agent, "search_web", fake_search_web)

    signal_discovery._search_web_evidence(
        {
            "name": "Цементирование",
            "query_seeds_json": [
                "cementing automation",
                "zonal isolation",
                "well cementing additive",
                "cement bond log",
                "lost circulation material",
            ],
        },
        signal_discovery.SignalDiscoveryConfig(web_query_limit=4, limit=5),
    )

    assert "2026 rare cementing deployment operator case study" in captured["queries"]


def test_web_search_runs_followup_research_round(monkeypatch):
    from oiltech_digest.source_discovery import agent as source_agent

    calls: list[list[str]] = []
    monkeypatch.setattr(signal_discovery, "feedback_query_hints", lambda topic, limit=8, **kwargs: [])
    monkeypatch.setattr(
        source_agent,
        "generate_search_queries",
        lambda topic, offline=True, limit=8, strategy="broad": ["2026 autonomous drilling deployment oil gas"],
    )

    def fake_search_web(queries, limit=80):
        calls.append(list(queries))
        if len(calls) == 1:
            return {
                "status": "ok",
                "provider": "test",
                "results": [
                    {
                        "url": "https://example.com/adnoc-ai-rig",
                        "title": "ADNOC deploys autonomous drilling system on oil and gas wells",
                        "snippet": "Field deployment improves drilling performance for an oil and gas operator.",
                        "provider": "test",
                        "query": queries[0],
                    }
                ],
            }
        return {
            "status": "ok",
            "provider": "test",
            "results": [
                {
                    "url": "https://example.com/adnoc-ai-rig-case",
                    "title": "ADNOC autonomous drilling case study reports rig performance",
                    "snippet": "Oilfield case study confirms operator deployment and KPI gains.",
                    "provider": "test",
                    "query": queries[0],
                }
            ],
        }

    monkeypatch.setattr(source_agent, "search_web", fake_search_web)

    result = signal_discovery._search_web_evidence(
        {"name": "Бурение", "query_seeds_json": ["autonomous drilling"]},
        signal_discovery.SignalDiscoveryConfig(web_query_limit=2, limit=5, research_rounds=2, web_fulltext_limit=0),
    )

    assert len(calls) == 2
    assert any("ADNOC" in query and "drilling" in query for query in calls[1])
    rounds = result["research_rounds"]
    assert [row["round"] for row in rounds] == [1, 2]
    assert rounds[0]["mode"] == "initial"
    assert rounds[0]["quality"]["strong"] is True
    assert rounds[0]["next_mode"] == "followup"
    assert rounds[1]["mode"] == "followup"
    assert rounds[1]["followup"] is True
    assert len(result["evidence"]) == 2


def test_web_search_pivots_after_weak_research_round(monkeypatch):
    """Раунд без отраслевого контекста и признаков события не должен углубляться follow-up'ом.

    Follow-up вытаскивает компанию/технологию из найденного текста, поэтому на слабой
    выдаче он просто повторит тот же шум другими словами. Pivot вместо этого берёт
    свежую пару термин×угол и не зависит от содержимого слабого раунда.
    """
    from oiltech_digest.source_discovery import agent as source_agent

    calls: list[list[str]] = []
    monkeypatch.setattr(signal_discovery, "feedback_query_hints", lambda topic, limit=8, **kwargs: [])
    monkeypatch.setattr(
        source_agent,
        "generate_search_queries",
        lambda topic, offline=True, limit=8, strategy="broad": ["2026 autonomous drilling deployment oil gas"],
    )

    def fake_search_web(queries, limit=80):
        calls.append(list(queries))
        return {
            "status": "ok",
            "provider": "test",
            "results": [
                {
                    "url": f"https://example.com/weak-{len(calls)}",
                    "title": "New robot demonstrated at trade show",
                    "snippet": "The device impressed attendees with a smooth demo.",
                    "provider": "test",
                    "query": queries[0],
                }
            ],
        }

    monkeypatch.setattr(source_agent, "search_web", fake_search_web)

    result = signal_discovery._search_web_evidence(
        {"name": "Бурение", "query_seeds_json": ["autonomous drilling"]},
        signal_discovery.SignalDiscoveryConfig(web_query_limit=2, limit=5, research_rounds=2, web_fulltext_limit=0),
    )

    assert len(calls) == 2
    rounds = result["research_rounds"]
    assert rounds[0]["quality"]["has_industry_context"] is False
    assert rounds[0]["quality"]["strong"] is False
    assert rounds[0]["next_mode"] == "pivot"
    assert rounds[1]["mode"] == "pivot"
    assert calls[1] != calls[0]
    assert not any('"' in query for query in calls[1])


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
        signal_discovery.SignalDiscoveryConfig(web_query_limit=5, limit=5, research_rounds=1, web_fulltext_limit=0),
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
        signal_discovery.SignalDiscoveryConfig(web_query_limit=4, limit=5, research_rounds=1, web_fulltext_limit=0),
    )

    urls = [item["source_url"] for item in result["evidence"]]
    assert urls == ["https://example.com/useful"]


def test_web_search_enriches_evidence_with_fetched_full_text(monkeypatch):
    """Судья должен получать реальный текст страницы, а не обрывок сниппета поиска.

    Сниппет — 1-2 обрубленных предложения; ни контракта, ни KPI, ни даты события в
    нём обычно нет. Докачка полного текста должна заменить extracted_fact/summary_ru/
    title/published_at, если страница отдала достаточно текста.
    """
    from datetime import datetime, timezone

    from oiltech_digest.source_discovery import agent as source_agent

    monkeypatch.setattr(signal_discovery, "feedback_query_hints", lambda topic, limit=8, **kwargs: [])
    monkeypatch.setattr(
        source_agent,
        "generate_search_queries",
        lambda topic, offline=True, limit=8, strategy="broad": ["2026 autonomous drilling oil gas"],
    )

    def fake_search_web(queries, limit=80):
        return {
            "status": "ok",
            "provider": "test",
            "results": [
                {
                    "url": "https://example.com/adnoc-pilot",
                    "title": "ADNOC pilots autonomous drilling",
                    "snippet": "Oil and gas field trial begins for autonomous drilling system.",
                    "provider": "test",
                    "query": queries[0],
                }
            ],
        }

    monkeypatch.setattr(source_agent, "search_web", fake_search_web)

    full_text = (
        "ADNOC completed a field trial of an autonomous drilling system with an oilfield "
        "services contractor. The operator reports a 20% faster rate of penetration and "
        "signed a follow-on contract for further deployment across offshore wells in 2026."
    )
    published_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    fetched_calls = []

    def fake_fetch_full_text(url, fallback_title=""):
        fetched_calls.append(url)
        return {
            "ok": True,
            "error": None,
            "raw_text": full_text,
            "published_at": published_at,
            "title": "ADNOC completes autonomous drilling pilot with 20% faster ROP",
        }

    monkeypatch.setattr(signal_discovery, "_fetch_full_text", fake_fetch_full_text)

    result = signal_discovery._search_web_evidence(
        {"name": "Бурение", "query_seeds_json": ["autonomous drilling"]},
        signal_discovery.SignalDiscoveryConfig(web_query_limit=2, limit=5, research_rounds=1, web_fulltext_limit=5),
    )

    assert fetched_calls == ["https://example.com/adnoc-pilot"]
    assert result["fulltext"] == {"attempted": 1, "fetched": 1, "too_short": 0, "failed": 0, "skipped_budget": 0}
    evidence = result["evidence"]
    assert len(evidence) == 1
    item = evidence[0]
    assert item["title"] == "ADNOC completes autonomous drilling pilot with 20% faster ROP"
    assert item["extracted_fact"] == full_text
    # Строкой ISO: объект datetime ронял отправку итога воркера ядру (4712, 21.09).
    assert item["published_at"] == published_at.isoformat()
    assert item["raw_payload"]["full_text_fetched"] is True
    assert item["raw_payload"]["full_text_chars"] == len(full_text)


def test_judge_prompt_includes_published_at_when_known(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(signal_discovery, "feedback_prompt_block", lambda topic, **kwargs: "")

    with_date = {
        "title": "ADNOC completes autonomous drilling pilot",
        "publisher": "example.com",
        "source_url": "https://example.com/adnoc-pilot",
        "evidence_type": "case_study",
        "published_at": datetime(2026, 3, 1, tzinfo=timezone.utc),
        "extracted_fact": "20% faster rate of penetration.",
        "summary_ru": "ADNOC на 20% ускорила проходку.",
    }
    without_date = {**with_date, "published_at": None, "source_url": "https://example.com/no-date"}

    prompt_with_date = signal_discovery._judge_prompt([with_date], "Бурение")
    prompt_without_date = signal_discovery._judge_prompt([without_date], "Бурение")

    assert "published_at: 2026-03-01" in prompt_with_date
    assert "published_at" not in prompt_without_date


def test_web_search_full_text_fetch_falls_back_gracefully(monkeypatch):
    """Неудачная докачка (404, антибот, короткая страница) не должна терять кандидата.

    Карточка должна остаться такой же, как без докачки, — просто с пометкой в
    raw_payload, почему полный текст не заменил сниппет.
    """
    from oiltech_digest.source_discovery import agent as source_agent

    monkeypatch.setattr(signal_discovery, "feedback_query_hints", lambda topic, limit=8, **kwargs: [])
    monkeypatch.setattr(
        source_agent,
        "generate_search_queries",
        lambda topic, offline=True, limit=8, strategy="broad": ["2026 autonomous drilling oil gas"],
    )

    def fake_search_web(queries, limit=80):
        return {
            "status": "ok",
            "provider": "test",
            "results": [
                {
                    "url": "https://example.com/blocked",
                    "title": "Oil and gas operator deploys autonomous drilling",
                    "snippet": "Field trial improves drilling performance for an oil and gas operator.",
                    "provider": "test",
                    "query": queries[0],
                },
                {
                    "url": "https://example.com/thin",
                    "title": "Oil and gas contractor launches drilling automation",
                    "snippet": "Contract signed for drilling automation deployment in oil and gas.",
                    "provider": "test",
                    "query": queries[0],
                },
            ],
        }

    monkeypatch.setattr(source_agent, "search_web", fake_search_web)

    def fake_fetch_full_text(url, fallback_title=""):
        if url == "https://example.com/blocked":
            return {"ok": False, "error": "http_403", "raw_text": "", "published_at": None, "title": ""}
        return {"ok": True, "error": None, "raw_text": "Too short.", "published_at": None, "title": "Thin page"}

    monkeypatch.setattr(signal_discovery, "_fetch_full_text", fake_fetch_full_text)

    result = signal_discovery._search_web_evidence(
        {"name": "Бурение", "query_seeds_json": ["autonomous drilling"]},
        signal_discovery.SignalDiscoveryConfig(web_query_limit=2, limit=5, research_rounds=1, web_fulltext_limit=5),
    )

    assert result["fulltext"] == {"attempted": 2, "fetched": 0, "too_short": 1, "failed": 1, "skipped_budget": 0}
    by_url = {item["source_url"]: item for item in result["evidence"]}
    blocked = by_url["https://example.com/blocked"]
    assert blocked["extracted_fact"] == "Field trial improves drilling performance for an oil and gas operator."
    assert blocked["raw_payload"]["full_text_fetched"] is False
    assert blocked["raw_payload"]["full_text_error"] == "http_403"
    thin = by_url["https://example.com/thin"]
    assert thin["extracted_fact"] == "Contract signed for drilling automation deployment in oil and gas."
    assert thin["raw_payload"]["full_text_fetched"] is False
    assert thin["raw_payload"]["full_text_error"] == "too_short"


def test_batch_review_skips_when_fewer_than_two_candidates():
    candidates = [{"signal": {"signal_key": "a", "score": 70}, "rejected": False}]

    result = signal_discovery._batch_review_candidates(candidates, "Бурение", offline=True)

    assert result == {"status": "skipped", "reason": "fewer_than_2_candidates", "reviewed": 1, "dropped": 0,
                      "duplicates": 0}
    assert candidates[0]["rejected"] is False


def test_batch_review_offline_merges_near_duplicate_into_stronger():
    """Судья видел кластеры по отдельности и одобрил оба — офлайн-правило ловит

    то, что по заголовку и компании это один и тот же контракт ADNOC. Слабый — дубль
    сильного (его ссылки уйдут в ту карточку), а не брак: брак учит радар, что
    пересказ сильного события — мусор, и теряет ссылки (21.09).
    """
    strong = {
        "signal": {
            "signal_key": "strong",
            "title": "ADNOC deploys autonomous drilling rig",
            "score": 80,
            "companies": ["ADNOC"],
        },
        "rejected": False,
    }
    weak = {
        "signal": {
            "signal_key": "weak",
            "title": "ADNOC deploys autonomous drilling system",
            "score": 55,
            "companies": ["ADNOC"],
        },
        "rejected": False,
    }

    result = signal_discovery._batch_review_candidates([weak, strong], "Бурение", offline=True)

    assert result["status"] == "ok"
    assert result["source"] == "rules"
    assert result["dropped"] == 0
    assert result["duplicates"] == 1
    assert strong["rejected"] is False
    assert "duplicate_of" not in strong
    assert weak["rejected"] is False
    assert weak["duplicate_of"] == {"signal_key": "strong"}
    assert weak["signal"].get("maturity") != "reject"
    assert weak["signal"]["batch_review_reason"]


def test_batch_review_offline_keeps_distinct_candidates():
    a = {
        "signal": {"signal_key": "a", "title": "ADNOC deploys autonomous drilling rig", "score": 80, "companies": ["ADNOC"]},
        "rejected": False,
    }
    b = {
        "signal": {"signal_key": "b", "title": "Sinopec pilots robotic pipeline inspection", "score": 70, "companies": ["Sinopec"]},
        "rejected": False,
    }

    result = signal_discovery._batch_review_candidates([a, b], "HSE robotics", offline=True)

    assert result == {
        "status": "ok",
        "source": "rules",
        "reviewed": 2,
        "dropped": 0,
        "duplicates": 0,
        "decisions": [],
        "interest_scores": {"a": 80.0, "b": 70.0},
    }
    assert a["rejected"] is False
    assert b["rejected"] is False
    # Офлайн-режим не умеет сравнивать пачку — interest_score откатывается на score судьи.
    assert a["signal"]["interest_score"] == 80.0
    assert b["signal"]["interest_score"] == 70.0


def test_batch_review_ai_rejects_noise_candidate(monkeypatch):
    from oiltech_digest.processing.openai_client import AIResponse

    class Client:
        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            return AIResponse(
                data={
                    "decisions": [
                        {
                            "signal_key": "keep-me",
                            "keep": True,
                            "reason": "Отдельное событие.",
                            "interest_score": 88,
                            "why_interesting": "Первое промышленное внедрение у нового игрока на фоне пачки.",
                        },
                        {
                            "signal_key": "drop-me",
                            "keep": False,
                            "duplicate_of_signal_key": "",
                            "reason": "Общий обзор рынка без нового факта.",
                            "interest_score": 0,
                            "why_interesting": "",
                        },
                    ]
                },
                model="fake-ai",
            )

    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: Client())

    keep = {"signal": {"signal_key": "keep-me", "title": "A", "score": 80, "companies": [], "evidence": []}, "rejected": False}
    drop = {"signal": {"signal_key": "drop-me", "title": "B", "score": 60, "companies": [], "evidence": []}, "rejected": False}

    result = signal_discovery._batch_review_candidates([keep, drop], "Бурение", offline=False)

    assert result == {
        "status": "ok",
        "source": "ai",
        "model": "fake-ai",
        "reviewed": 2,
        "dropped": 1,
        "duplicates": 0,
        "decisions": [{"signal_key": "drop-me", "action": "reject", "reason": "Общий обзор рынка без нового факта."}],
        "interest_scores": {"keep-me": 88.0},
    }
    assert keep["rejected"] is False
    assert keep["signal"]["interest_score"] == 88.0
    assert keep["signal"]["why_interesting"] == "Первое промышленное внедрение у нового игрока на фоне пачки."
    assert drop["rejected"] is True
    assert drop["signal"]["maturity"] == "reject"
    assert drop["signal"]["batch_review_reason"] == "Общий обзор рынка без нового факта."
    assert "duplicate_of" not in drop
    assert "interest_score" not in drop["signal"]


def test_batch_review_ai_missing_decision_falls_back_to_score(monkeypatch):
    """Модель обязана вернуть решение по каждому signal_key, но если пропустила один —

    ранжирование не должно остаться без числа: используем score судьи как черновую замену.
    """
    from oiltech_digest.processing.openai_client import AIResponse

    class Client:
        def complete_json(self, *args, **kwargs):
            return AIResponse(
                data={
                    "decisions": [
                        {
                            "signal_key": "a",
                            "keep": True,
                            "reason": "ok",
                            "interest_score": 70,
                            "why_interesting": "x",
                        },
                    ]
                },
                model="fake-ai",
            )

    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: Client())

    a = {"signal": {"signal_key": "a", "title": "A", "score": 50, "companies": [], "evidence": []}, "rejected": False}
    b = {"signal": {"signal_key": "b", "title": "B", "score": 65, "companies": [], "evidence": []}, "rejected": False}

    result = signal_discovery._batch_review_candidates([a, b], "Бурение", offline=False)

    assert result["interest_scores"] == {"a": 70.0, "b": 65.0}
    assert a["signal"]["interest_score"] == 70.0
    assert b["signal"]["interest_score"] == 65.0
    assert b["rejected"] is False


def test_apply_discovery_ranks_final_signals_by_interest_score():
    """Финальная сортировка должна слушать interest_score (сравнение внутри пачки),

    а не сырой score судьи, который сравнивал кластер сам с собой.
    """
    run = {
        "topics": [
            {
                "topic": "Бурение",
                "article_evidence": 0,
                "total_evidence": 0,
                "skipped_reviewed": 0,
                "web_search": None,
                "clusters": 2,
                "candidates": [
                    {
                        "signal": {
                            "signal_key": "high-score-boring",
                            "score": 90,
                            "interest_score": 40,
                            "evidence_count": 1,
                            "evidence": [],
                        },
                        "raw_output": {},
                        "rejected": False,
                        "training_input": {},
                    },
                    {
                        "signal": {
                            "signal_key": "low-score-interesting",
                            "score": 60,
                            "interest_score": 92,
                            "evidence_count": 1,
                            "evidence": [],
                        },
                        "raw_output": {},
                        "rejected": False,
                        "training_input": {},
                    },
                ],
            }
        ],
        "dedup": {},
    }

    result = signal_discovery.apply_discovery(
        signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, max_signals=10),
        run,
    )

    assert [item["signal_key"] for item in result["signals"]] == ["low-score-interesting", "high-score-boring"]


def test_batch_review_ai_failure_is_graceful(monkeypatch):
    """Сбой ревью-вызова не должен ронять весь прогон темы или трогать кандидатов."""

    class Client:
        def complete_json(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: Client())

    a = {"signal": {"signal_key": "a", "title": "A", "score": 80, "companies": [], "evidence": []}, "rejected": False}
    b = {"signal": {"signal_key": "b", "title": "B", "score": 70, "companies": [], "evidence": []}, "rejected": False}

    result = signal_discovery._batch_review_candidates([a, b], "Бурение", offline=False)

    assert result["status"] == "error"
    assert "boom" in result["error"]
    assert a["rejected"] is False
    assert b["rejected"] is False


def test_cluster_key_keeps_same_direction_events_separate():
    first = signal_discovery._cluster_key(
        {
            "title": "Operator deploys autonomous robot on offshore platform",
            "extracted_fact": "Robot inspects oil and gas equipment for ADNOC.",
        },
        "HSE robotics / Physical AI",
    )
    second = signal_discovery._cluster_key(
        {
            "title": "Refinery pilots robotic inspection dog for hazardous zones",
            "extracted_fact": "Robotic dog checks petrochemical units for Sinopec.",
        },
        "HSE robotics / Physical AI",
    )

    assert first.startswith("physical-ai-robotics-")
    assert second.startswith("physical-ai-robotics-")
    assert first != second


def test_clusters_for_judging_round_robins_signal_families():
    clusters = [
        [
            {
                "title": "Oilfield deploys drilling automation",
                "extracted_fact": "The operator deployed the system.",
                "strength": 0.9,
            }
        ],
        [
            {
                "title": "Refinery deploys robotic inspection",
                "extracted_fact": "The operator deployed the robot.",
                "strength": 0.85,
            }
        ],
        [
            {
                "title": "Supplier wins oilfield automation contract",
                "extracted_fact": "Contract signed with an oil and gas operator.",
                "strength": 0.8,
            }
        ],
        [
            {
                "title": "Vendor launches upstream AI product",
                "extracted_fact": "Commercialized launch for oil and gas customers.",
                "strength": 0.75,
            }
        ],
    ]

    selected = signal_discovery._clusters_for_judging(clusters, 3)
    selected_titles = [cluster[0]["title"] for cluster in selected]

    assert selected_titles == [
        "Oilfield deploys drilling automation",
        "Supplier wins oilfield automation contract",
        "Vendor launches upstream AI product",
    ]
