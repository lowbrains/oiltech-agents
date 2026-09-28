from oiltech_digest.processing import external_ai
from oiltech_digest.processing.openai_client import AIResponse


def test_process_source_candidate_payload_builds_article_results(monkeypatch):
    article = {
        "id": 101,
        "candidate_id": 42,
        "title": "Robotic drilling system improves operations",
        "url": "https://example.com/news/robotic-drilling",
        "raw_text": "Robotic drilling system improves oilfield operations.",
        "published_at": "2026-08-20T10:00:00+00:00",
        "language": "en",
        "source_name": "Example",
        "source_category": "роботизация бурения",
    }
    tags = [{"id": 7, "name": "Бурение", "parent_id": None, "keywords_json": [], "keywords_en_json": ["drilling"]}]
    criteria = [{"id": 9, "name": "Технологичность", "weight": 100, "keywords_json": [], "keywords_en_json": ["drilling"]}]

    gate_tags = []
    monkeypatch.setattr(external_ai, "make_client", lambda offline: object())
    monkeypatch.setattr(
        external_ai,
        "relevance_article",
        lambda article, client, tags=None: gate_tags.append(tags)
        or AIResponse({"relevant": True, "reason": "topic match"}, "fake", 10, 2),
    )
    monkeypatch.setattr(
        external_ai,
        "summarize_article",
        lambda article, client: AIResponse({"summary": "Короткая суть"}, "fake", 10, 3),
    )
    monkeypatch.setattr(external_ai, "title_ru_for_article", lambda article, client: ("Русский заголовок", None))
    monkeypatch.setattr(
        external_ai,
        "tag_article",
        lambda article, tags, client: AIResponse({"tag_id": 7, "confidence": 0.9, "rationale": "drilling"}, "fake", 10, 2),
    )
    monkeypatch.setattr(
        external_ai,
        "score_article",
        lambda article, criteria, client: AIResponse(
                {
                    "total_score": 80,
                    "score_label": "high",
                    "explanation": "strong signal",
                    "items": [{"criterion_id": 9, "ai_score": 80, "rationale": "r"}],
                },
                "fake",
                10,
            5,
        ),
    )

    result = external_ai.process_source_candidate_payload({
        "candidate_id": 42,
        "articles": [article],
        "tags": tags,
        "criteria": criteria,
    })

    assert result["source_candidate_evaluate"] is True
    assert result["stats"]["processed"] == 1
    assert result["stats"]["relevant"] == 1
    # Гейт на NL судит по тематикам заказчика из payload: базы у воркера нет.
    assert gate_tags == [tags]
    assert result["articles"][0]["candidate_article_id"] == 101
    assert result["articles"][0]["summary"]["summary"] == "Короткая суть"
    assert result["articles"][0]["tagging"]["tag_id"] == 7
    assert result["articles"][0]["scoring"]["total_score"] == 80
    assert result["source_regularity"]["dated_articles"] == 1
    assert result["source_health"]["health_score"] is not None
    assert result["source_health"]["recommended_action"] in {"add", "test_more", "human_review", "reject"}


def test_apply_source_candidate_result_updates_articles_and_assessment(monkeypatch):
    saved_articles = []
    assessment = {}
    memory = []
    actions = []

    monkeypatch.setattr(
        external_ai.repository,
        "update_source_candidate_article_result",
        lambda article_id, payload: saved_articles.append({"article_id": article_id, **payload}),
    )
    monkeypatch.setattr(
        external_ai.repository,
        "source_candidate_article_metrics",
        lambda candidate_id: {
            "tested_articles": 1,
            "relevant_articles": 1,
            "avg_score": 80.0,
            "duplicate_count": 0,
            "noise_count": 0,
        },
    )
    monkeypatch.setattr(
        external_ai.repository,
        "update_source_candidate_assessment",
        lambda candidate_id, **kwargs: assessment.update({"candidate_id": candidate_id, **kwargs}),
    )
    monkeypatch.setattr(
        external_ai.repository,
        "get_source_candidate",
        lambda candidate_id: {
            "id": candidate_id,
            "url": "https://example.com/news",
            "normalized_domain": "example.com",
            "topic": "бурение",
        },
    )
    monkeypatch.setattr(
        external_ai.repository,
        "get_background_job",
        lambda job_id: {"id": job_id, "agent_run_id": 77},
    )
    monkeypatch.setattr(
        external_ai.repository,
        "upsert_agent_memory",
        lambda **kwargs: memory.append(kwargs) or len(memory),
    )
    monkeypatch.setattr(
        external_ai.repository,
        "record_agent_action",
        lambda task_id, action_type, **kwargs: actions.append({"task_id": task_id, "action_type": action_type, **kwargs}) or 1,
    )

    applied = external_ai.apply_source_candidate_result({
        "candidate_id": 42,
        "articles": [
            {
                "candidate_article_id": 101,
                "relevance": {"relevant": True, "reason": "topic match", "model": "fake"},
                "summary": {"summary": "Короткая суть", "model": "fake"},
                "translation": {"title_ru": "Русский заголовок"},
                "tagging": {"tag_id": 7, "confidence": 0.9, "rationale": "drilling", "model": "fake"},
                "scoring": {
                    "total_score": 80,
                    "score_label": "high",
                    "explanation": "strong signal",
                    "items": [{"criterion_id": 9, "score": 80}],
                    "model": "fake",
                },
            }
        ],
        "source_regularity": {
            "regularity_label": "active_but_sparse",
            "is_regular": True,
            "sample_size": 1,
            "dated_articles": 1,
            "articles_last_30_days": 1,
            "articles_last_90_days": 1,
            "latest_age_days": 2,
            "archive_suspected": False,
            "section_updates": True,
            "reason": "Источник обновляется.",
            "risks": [],
            "next_checks": [],
            "confidence": 0.6,
        },
        "source_health": {
            "health_score": 52,
            "verdict": "promising_needs_more_data",
            "recommended_action": "test_more",
            "reason": "Плюсы: тестовый источник.",
            "factors": ["тестовый источник"],
            "risks": ["малая выборка"],
            "confidence": 0.6,
            "components": {},
        },
    }, job_id=99)

    assert applied["ok"] == 1
    assert saved_articles[0]["article_id"] == 101
    assert saved_articles[0]["processing_status"] == "ok"
    assert saved_articles[0]["total_score"] == 80
    assert assessment["candidate_id"] == 42
    assert assessment["tested_articles"] == 1
    assert assessment["relevant_articles"] == 1
    assert assessment["recommended_action"] in {"add", "test_more", "human_review", "reject"}
    assert applied["source_health"]["verdict"] == "promising_needs_more_data"
    assert "Итоговая оценка источника" in assessment["review_comment"]
    assert "Регулярность источника" in assessment["review_comment"]
    assert applied["learning"]["ok"] is True
    assert {item["memory_type"] for item in memory} == {"topic", "domain"}
    assert memory[0]["facts"]["candidate_id"] == 42
    assert actions[0]["action_type"] == "source_candidate_learning"
    assert actions[0]["run_id"] == 77
