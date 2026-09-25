from fastapi.testclient import TestClient

from oiltech_digest import api


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, sql, params=None):
        self.connection.executed.append((sql, list(params or [])))
        if "FROM article_score_items" in sql:
            self.rows = [
                {
                    "article_id": 42,
                    "name": "Технологическая значимость",
                    "weight": 40,
                    "final_score": 88,
                    "ai_score": 90,
                    "keyword_score": 80,
                    "rationale": "Strong match",
                }
            ]
        else:
            self.rows = [
                {
                    "id": 42,
                    "title": "Directional drilling automation",
                    "url": "https://example.com/drilling",
                    "language": "en",
                    "raw_text": "Directional drilling automation improves well construction.",
                    "published_at": None,
                    "collected_at": None,
                    "text_truncated": False,
                    "source_name": "World Oil",
                    "summary": "Compact AI summary",
                    "status": "digest",
                    "relevant": True,
                    "relevance_reason": "Oilfield technology",
                    "selected_for_digest": True,
                    "total_score": 88,
                    "score_label": "High",
                    "score_explanation": "Relevant",
                    "tag_name": "Бурение",
                    "parent_tag_name": "Технологии",
                    "tag_confidence": 0.91,
                    "tag_rationale": "Keyword match",
                }
            ]
        return self

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self, row_factory=None):
        return FakeCursor(self)


def test_source_diagnose_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {"id": source_id, "name": "Example", "parse_strategy": "request"},
    )
    monkeypatch.setattr(
        api,
        "diagnose_source",
        lambda source, limit=5: {
            "source_id": source["id"],
            "source_name": source["name"],
            "strategy": source["parse_strategy"],
            "limit": limit,
            "verdict": "ok",
        },
    )
    try:
        client = TestClient(app)
        response = client.get("/api/sources/7/diagnose?limit=3")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "source_id": 7,
        "source_name": "Example",
        "strategy": "request",
        "limit": 3,
        "verdict": "ok",
    }


def test_signal_feedback_endpoint_stores_learning(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_admin] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}

    captured = {}

    def fake_store_signal_feedback(row, user_id=None, import_source=None, extracted_items=None):
        captured["row"] = row
        captured["user_id"] = user_id
        return {"event_id": 11, "memory_ids": [21, 22], "memories": 2}

    from oiltech_digest import signal_feedback

    monkeypatch.setattr(signal_feedback, "store_signal_feedback", fake_store_signal_feedback)
    try:
        response = TestClient(app).post(
            "/api/signals/feedback",
            json={
                "signal_id": 7,
                "source_url": "https://example.com/signal",
                "signal_title": "Closed-loop drilling",
                "comment": "closed-loop control -> управление с замкнутым контуром",
                "verdict": "approved",
                "reason": "Есть внедрение",
                "corrected_title": "Управление бурением с замкнутым контуром",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True, "event_id": 11, "memory_ids": [21, 22], "memories": 2}
    assert captured["row"]["signal_id"] == 7
    assert captured["row"]["verdict"] == "approved"
    assert captured["row"]["corrected_title"] == "Управление бурением с замкнутым контуром"
    assert captured["user_id"] == 1


def test_signal_feedback_endpoint_accepts_structured_feedback_without_comment(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_admin] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}

    captured = {}

    def fake_store_signal_feedback(row, user_id=None, import_source=None, extracted_items=None):
        captured["row"] = row
        return {"event_id": 12, "memory_ids": [31], "memories": 1}

    from oiltech_digest import signal_feedback

    monkeypatch.setattr(signal_feedback, "store_signal_feedback", fake_store_signal_feedback)
    try:
        response = TestClient(app).post(
            "/api/signals/feedback",
            json={
                "signal_id": 7,
                "comment": "",
                "verdict": "merge_duplicate",
                "duplicate_of_signal_id": 3,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured["row"]["verdict"] == "merge_duplicate"
    assert captured["row"]["duplicate_of_signal_id"] == 3


def test_signal_patch_endpoint_adds_signal_to_digest(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 3, "email": "editor@example.com", "role": "admin"}

    captured = {}
    events = []
    monkeypatch.setattr(
        api.repository,
        "set_user_signal_status",
        lambda user_id, signal_id, **kwargs: captured.update({"user_id": user_id, "signal_id": signal_id, **kwargs}),
    )
    monkeypatch.setattr(
        api.repository,
        "record_signal_feedback_event",
        lambda *args, **kwargs: events.append((args, kwargs)) or 12,
    )
    try:
        response = TestClient(app).patch("/api/signals/7", json={"selected_for_digest": True, "analyst_comment": "Берём в выпуск"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert captured == {"user_id": 3, "signal_id": 7, "status": "digest", "analyst_comment": "Берём в выпуск"}
    assert events[0][1]["signal_id"] == 7
    assert events[0][1]["user_id"] == 3


def test_signal_memory_endpoint_uses_signal_agent_memory(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_admin] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}

    captured = {}
    monkeypatch.setattr(
        api.repository,
        "list_signal_agent_memory",
        lambda memory_type=None, status="active", limit=100: captured.update(
            {"memory_type": memory_type, "status": status, "limit": limit}
        ) or [
            {
                "id": 5,
                "memory_type": memory_type,
                "subject": "closed-loop control",
                "status": status,
                "score": 85,
                "facts_json": {"preferred_ru": "управление с замкнутым контуром"},
            }
        ],
    )
    try:
        response = TestClient(app).get("/api/signals/memory?memory_type=signal_glossary&limit=10")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["subject"] == "closed-loop control"
    assert captured == {"memory_type": "signal_glossary", "status": "active", "limit": 10}


def test_source_candidates_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        api.repository,
        "list_source_candidates",
        lambda status=None, topic=None, limit=100: [
            {
                "id": 7,
                "url": "https://example.com/news",
                "status": status or "needs_human_review",
                "topic": topic,
                "tested_articles": 5,
                "relevant_articles": 4,
                "avg_score": 72,
            }
        ],
    )
    try:
        response = TestClient(app).get("/api/source-candidates?status=needs_human_review&topic=бурение&limit=10")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["id"] == 7
    assert payload[0]["status"] == "needs_human_review"
    assert payload[0]["topic"] == "бурение"


def test_source_candidate_triage_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    monkeypatch.setattr(
        api.repository,
        "source_candidate_triage_report",
        lambda **kwargs: captured.update(kwargs) or [
            {
                "id": 7,
                "url": "https://example.com/news",
                "normalized_domain": "example.com",
                "status": "needs_human_review",
                "recommended_action": "add",
                "triage_priority": 120,
                "triage_reason": "Можно добавлять после проверки человеком",
            }
        ],
    )
    try:
        response = TestClient(app).get("/api/source-candidates/triage?limit=5")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["triage_priority"] == 120
    assert captured == {"limit": 5}


def test_source_discovery_evaluation_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        api.repository,
        "list_source_candidates",
        lambda limit=500: [
            {"id": 1, "name": "Good", "status": "approved", "recommended_action": "add", "topic": "бурение", "avg_score": 80},
            {"id": 2, "name": "Bad", "status": "rejected", "recommended_action": "add", "topic": "бурение", "avg_score": 10},
        ],
    )
    monkeypatch.setattr(
        api.repository,
        "list_agent_memory",
        lambda **kwargs: [
            {
                "id": 1,
                "subject": "Blocked Source",
                "score": -60,
                "facts_json": {
                    "source_id": 11,
                    "problem_type": "needs_external",
                    "severity": "high",
                    "confidence": "high",
                    "recommendation": "move_to_external_region",
                    "recommendation_label": "Перенести в external-worker",
                    "decision_log": {
                        "triggered_rules": [{"rule": "needs_external", "severity": "high"}],
                        "suppressed_rules": [{"rule": "parser_suspect", "severity": "high"}],
                    },
                },
            }
        ],
    )
    monkeypatch.setattr(
        api.repository,
        "list_agent_actions",
        lambda **kwargs: [{"action_type": "source_candidate_learning"}, {"action_type": "source_discovery_plan_built"}],
    )
    try:
        response = TestClient(app).get("/api/source-discovery/evaluation?limit=500")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["candidate_decisions"] == 2
    assert payload["summary"]["candidate_agreement_rate"] == 0.5
    assert payload["summary"]["sources_under_watch"] == 1
    assert payload["source_audit"]["rules"][0]["rule"] == "parser_suspect"
    assert payload["recent_actions"]["learning_events"] == 1


def test_source_discovery_plan_endpoint(monkeypatch):
    from oiltech_digest.source_discovery import planner

    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    configs = []
    monkeypatch.setattr(
        planner,
        "build_plan",
        lambda config: configs.append(config) or {"actions": [{"action_type": "discover_sources", "topic": "бурение"}]},
    )
    try:
        response = TestClient(app).get(
            "/api/source-discovery/plan?days=14&target_per_topic=8&topic_limit=2&candidate_limit=6&max_actions=3"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["actions"][0]["topic"] == "бурение"
    assert configs[0].days == 14
    assert configs[0].target_per_topic == 8
    assert configs[0].topic_limit == 2
    assert configs[0].candidate_limit == 6
    assert configs[0].max_actions == 3
    assert configs[0].persist_memory is False


def test_source_discovery_discover_endpoint(monkeypatch):
    from oiltech_digest.source_discovery import agent

    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 5, "email": "admin@example.com", "role": "admin"}
    configs = []
    actions = []
    monkeypatch.setattr(
        agent,
        "discover_sources",
        lambda config: configs.append(config) or {
            "topic": config.topic,
            "candidates": [{"id": 11, "url": config.seed_urls[0]}],
            "duration_ms": 123,
        },
    )
    monkeypatch.setattr(
        api.repository,
        "record_agent_action",
        lambda *args, **kwargs: actions.append((args, kwargs)) or 1,
    )
    try:
        response = TestClient(app).post(
            "/api/source-discovery/discover",
            json={
                "topic": "бурение",
                "seed_url": "https://example.com/news",
                "limit": 7,
                "offline": True,
                "fetch_inspection": True,
                "test_parse": False,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["result"]["candidates"][0]["id"] == 11
    assert configs[0].topic == "бурение"
    assert configs[0].seed_urls == ("https://example.com/news",)
    assert configs[0].limit == 7
    assert configs[0].dry_run is False
    assert actions[0][1]["output_payload"]["candidate_ids"] == [11]


def test_enqueue_source_discovery_plan_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    monkeypatch.setattr(
        api.background_jobs,
        "enqueue",
        lambda kind, payload, **kwargs: captured.update({"kind": kind, "payload": payload, **kwargs}) or {
            "id": 77,
            "kind": kind,
            "queue_name": kwargs["queue_name"],
            "execution_region": kwargs["execution_region"],
            "capability": kwargs["capability"],
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": 1,
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    try:
        response = TestClient(app).post(
            "/api/source-discovery/plan/enqueue",
            json={"days": 14, "topic_limit": 2, "candidate_limit": 6, "max_actions": 3},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["job"]["id"] == 77
    assert captured["kind"] == "source_discovery_plan"
    assert captured["payload"]["days"] == 14
    assert captured["payload"]["topic_limit"] == 2
    assert captured["queue_name"] == "default"
    assert captured["user_id"] == 1


def test_enqueue_source_discovery_loop_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    monkeypatch.setattr(
        api.background_jobs,
        "enqueue",
        lambda kind, payload, **kwargs: captured.update({"kind": kind, "payload": payload, **kwargs}) or {
            "id": 88,
            "kind": kind,
            "queue_name": kwargs["queue_name"],
            "execution_region": kwargs["execution_region"],
            "capability": kwargs["capability"],
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": 1,
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    try:
        response = TestClient(app).post(
            "/api/source-discovery/loop/enqueue",
            json={"goal": "найти источники", "max_iterations": 2, "max_actions": 3},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["job"]["id"] == 88
    assert captured["kind"] == "source_discovery_loop"
    assert captured["payload"]["max_iterations"] == 2
    assert captured["payload"]["fetch_inspection"] is True
    assert captured["payload"]["test_parse"] is True
    assert captured["queue_name"] == "default"
    assert captured["user_id"] == 1


def test_source_discovery_loop_dry_run_endpoint(monkeypatch):
    from oiltech_digest.source_discovery import loop

    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}

    def fake_run_agent_loop(config):
        captured["config"] = config
        return {
            "run_id": None,
            "goal": config.goal,
            "iterations": [
                {
                    "iteration": 1,
                    "action_count": 1,
                    "auto_action_count": 1,
                    "human_review_count": 0,
                    "observations": [
                        {
                            "topic": "бурение",
                            "query_strategy": "balanced",
                            "search_status": "ok",
                            "query_count": 2,
                            "candidate_count": 1,
                        }
                    ],
                }
            ],
            "total_candidates": 1,
            "empty_iterations": 0,
            "terminal_reason": "max_iterations_reached",
            "dry_run": True,
            "duration_ms": 12,
        }

    monkeypatch.setattr(loop, "run_agent_loop", fake_run_agent_loop)
    try:
        response = TestClient(app).post(
            "/api/source-discovery/loop/dry-run",
            json={"goal": "проверить", "max_iterations": 2, "max_actions": 3, "dry_run": False, "auto_evaluate": True},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["result"]["dry_run"] is True
    assert body["result"]["run_id"] is None
    assert body["result"]["total_candidates"] == 1
    assert captured["config"].dry_run is True
    assert captured["config"].auto_evaluate is False
    assert captured["config"].persist_memory is False


def test_source_discovery_memory_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    monkeypatch.setattr(
        api.repository,
        "list_agent_memory",
        lambda **kwargs: captured.update(kwargs) or [
            {
                "id": 1,
                "memory_key": "topic:бурение",
                "memory_type": "topic",
                "subject": "бурение",
                "status": "active",
                "score": 80,
                "facts_json": {"gap": 3},
                "last_seen_at": None,
                "created_at": None,
                "updated_at": None,
            }
        ],
    )
    try:
        response = TestClient(app).get("/api/source-discovery/memory?memory_type=topic&status=active&limit=10")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["memory_key"] == "topic:бурение"
    assert captured == {"memory_type": "topic", "status": "active", "limit": 10}


def test_source_discovery_memory_patch_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    actions = []
    monkeypatch.setattr(api.repository, "update_agent_memory_status", lambda memory_id, status: captured.update({"memory_id": memory_id, "status": status}) or True)
    monkeypatch.setattr(
        api.repository,
        "record_agent_action",
        lambda task_id, action_type, **kwargs: actions.append({"task_id": task_id, "action_type": action_type, **kwargs}) or 1,
    )
    try:
        response = TestClient(app).patch("/api/source-discovery/memory/7", json={"status": "muted"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert captured == {"memory_id": 7, "status": "muted"}
    assert actions[0]["action_type"] == "update_agent_memory"


def test_source_discovery_memory_create_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    actions = []
    monkeypatch.setattr(
        api.repository,
        "upsert_agent_memory",
        lambda **kwargs: captured.update(kwargs) or 44,
    )
    monkeypatch.setattr(
        api.repository,
        "record_agent_action",
        lambda task_id, action_type, **kwargs: actions.append({"task_id": task_id, "action_type": action_type, **kwargs}) or 1,
    )
    try:
        response = TestClient(app).post(
            "/api/source-discovery/memory",
            json={"memory_type": "domain", "subject": "https://Example.com/news", "status": "rejected", "score": 0},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": 44}
    assert captured["memory_key"] == "manual:domain:example.com"
    assert captured["subject"] == "example.com"
    assert captured["status"] == "rejected"
    assert captured["facts"]["manual"] is True
    assert actions[0]["action_type"] == "create_agent_memory"


def test_source_discovery_quality_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    monkeypatch.setattr(
        api.repository,
        "source_candidate_quality_report",
        lambda **kwargs: captured.update(kwargs) or [
            {
                "subject": "бурение",
                "candidates": 3,
                "approved": 1,
                "rejected": 1,
                "paused": 0,
                "needs_human_review": 1,
                "test_more": 1,
                "tested_articles": 10,
                "relevant_articles": 6,
                "noise_count": 2,
                "avg_score": 70,
                "approval_rate": 0.5,
                "relevance_rate": 0.6,
            }
        ],
    )
    try:
        response = TestClient(app).get("/api/source-discovery/quality?group_by=topic&limit=5")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["subject"] == "бурение"
    assert response.json()[0]["approval_rate"] == 0.5
    assert captured == {"group_by": "topic", "limit": 5}


def test_source_discovery_quality_endpoint_rejects_bad_group_by(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    try:
        response = TestClient(app).get("/api/source-discovery/quality?group_by=source")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_source_discovery_query_memory_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    monkeypatch.setattr(
        api.repository,
        "query_memory_report",
        lambda **kwargs: captured.update(kwargs) or [
            {
                "query": "robotic drilling automation newsroom",
                "topic": "бурение",
                "score": 76,
                "status": "active",
                "found_candidates": 3,
                "tested_articles": 5,
                "relevant_articles": 4,
                "avg_score": 80,
                "empty_result": False,
                "relevance_rate": 0.8,
                "last_seen_at": None,
                "updated_at": None,
            }
        ],
    )
    try:
        response = TestClient(app).get("/api/source-discovery/query-memory?limit=5")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["query"] == "robotic drilling automation newsroom"
    assert captured == {"status": "active", "limit": 5}


def test_source_discovery_readiness_endpoint(monkeypatch):
    from oiltech_digest.source_discovery import readiness

    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        readiness,
        "source_discovery_readiness",
        lambda: {"ok": True, "status": "ready", "checks": {}, "issues": [], "recommendations": []},
    )
    try:
        response = TestClient(app).get("/api/source-discovery/readiness")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_source_discovery_actions_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    monkeypatch.setattr(
        api.repository,
        "list_agent_actions",
        lambda **kwargs: captured.update(kwargs) or [
            {
                "id": 1,
                "run_id": 55,
                "task_id": 7,
                "action_type": "discover_sources_finished",
                "input_json": {"topic": "бурение"},
                "output_json": {"candidates": 2},
                "cost_usd": 0,
                "duration_ms": 123,
                "created_at": None,
                "task_kind": "discover_sources",
                "task_status": "ok",
                "task_topic": "бурение",
                "decision_title": "Поиск источников завершён",
                "decision_summary": "Тема: бурение, кандидатов: 2.",
                "decision_tone": "good",
            }
        ],
    )
    try:
        response = TestClient(app).get("/api/source-discovery/actions?action_type=discover_sources_finished&limit=10")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["action_type"] == "discover_sources_finished"
    assert response.json()[0]["task_topic"] == "бурение"
    assert response.json()[0]["decision_title"] == "Поиск источников завершён"
    assert captured == {"action_type": "discover_sources_finished", "run_id": None, "limit": 10}


def test_source_discovery_runs_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    monkeypatch.setattr(
        api.repository,
        "list_agent_runs",
        lambda **kwargs: captured.update(kwargs) or [
            {
                "id": 55,
                "kind": "source_discovery_cycle",
                "status": "ok",
                "trigger": "scheduler",
                "payload_json": {"topic_limit": 5},
                "result_json": {"queued": {"queued": 2}},
                "error_message": None,
                "started_at": None,
                "finished_at": None,
                "created_at": None,
                "action_count": 3,
                "job_count": 2,
                "ok_job_count": 1,
                "failed_job_count": 0,
            }
        ],
    )
    try:
        response = TestClient(app).get("/api/source-discovery/runs?status=ok&limit=10")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["kind"] == "source_discovery_cycle"
    assert response.json()[0]["job_count"] == 2
    assert captured == {"status": "ok", "limit": 10}


def test_source_candidate_evaluate_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}

    def fake_eval(candidate_id, article_limit=5, offline=True, collect=True, process=True):
        return {
            "candidate_id": candidate_id,
            "task_id": 99,
            "metrics": {"tested_articles": article_limit, "relevant_articles": 3, "avg_score": 70},
            "recommended_action": "add",
            "next_status": "needs_human_review",
        }

    from oiltech_digest.source_discovery import sandbox

    monkeypatch.setattr(sandbox, "evaluate_source_candidate", fake_eval)
    try:
        response = TestClient(app).post(
            "/api/source-candidates/7/evaluate",
            json={"article_limit": 4, "offline": True, "collect": True, "process": True},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["metrics"]["tested_articles"] == 4


def test_source_candidate_approve_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    actions = []
    enqueued = []
    monkeypatch.setattr(api.repository, "approve_source_candidate", lambda candidate_id, **kwargs: 55)
    monkeypatch.setattr(api.repository, "get_source", lambda source_id: {"id": source_id, "parse_strategy": "request", "network_region": "auto"})
    monkeypatch.setattr(api, "apply_candidate_learning", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(
        api.background_jobs,
        "enqueue",
        lambda kind, payload, **kwargs: enqueued.append({"kind": kind, "payload": payload, **kwargs}) or {
            "id": 91,
            "kind": kind,
            "queue_name": kwargs["queue_name"],
            "execution_region": kwargs["execution_region"],
            "capability": kwargs["capability"],
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": kwargs.get("max_attempts", 3),
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    monkeypatch.setattr(
        api.repository,
        "record_agent_action",
        lambda task_id, action_type, **kwargs: actions.append({"task_id": task_id, "action_type": action_type, **kwargs}) or 1,
    )
    try:
        response = TestClient(app).post(
            "/api/source-candidates/7/approve",
            json={"enabled": True, "parse_strategy": "request", "network_region": "external"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["source_id"] == 55
    assert response.json()["initial_job"]["id"] == 91
    assert enqueued[0]["kind"] == "scrape_source"
    assert enqueued[0]["payload"] == {"source_id": 55, "reason": "approved_source_candidate", "candidate_id": 7}
    assert actions[0]["action_type"] == "approve_source_candidate"
    assert actions[0]["output_payload"]["initial_job_id"] == 91


def test_source_candidate_approve_endpoint_enqueues_initial_parse_for_rss(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(api.repository, "approve_source_candidate", lambda candidate_id, **kwargs: 55)
    monkeypatch.setattr(api.repository, "get_source", lambda source_id: {"id": source_id, "parse_strategy": "rss"})
    monkeypatch.setattr(api.repository, "record_agent_action", lambda *args, **kwargs: 1)
    monkeypatch.setattr(api, "apply_candidate_learning", lambda *args, **kwargs: {"ok": True})
    called = []
    monkeypatch.setattr(
        api.background_jobs,
        "enqueue",
        lambda kind, payload, **kwargs: called.append({"kind": kind, "payload": payload, **kwargs}) or {
            "id": 92,
            "kind": kind,
            "queue_name": kwargs["queue_name"],
            "execution_region": kwargs["execution_region"],
            "capability": kwargs["capability"],
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": kwargs.get("max_attempts", 3),
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    try:
        response = TestClient(app).post(
            "/api/source-candidates/7/approve",
            json={"enabled": True, "parse_strategy": "rss", "network_region": "auto"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["initial_job"]["id"] == 92
    assert called[0]["kind"] == "parse_source_once"
    assert called[0]["payload"] == {"source_id": 55, "reason": "approved_source_candidate", "candidate_id": 7}
    assert called[0]["capability"] == "rss_parse"


def test_source_candidate_patch_endpoint_records_operator_decision(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    actions = []
    monkeypatch.setattr(api.repository, "get_source_candidate", lambda candidate_id: {"id": candidate_id})
    monkeypatch.setattr(api, "apply_candidate_learning", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(
        api.repository,
        "update_source_candidate_assessment",
        lambda candidate_id, **kwargs: captured.update({"candidate_id": candidate_id, **kwargs}),
    )
    monkeypatch.setattr(
        api.repository,
        "record_agent_action",
        lambda task_id, action_type, **kwargs: actions.append({"task_id": task_id, "action_type": action_type, **kwargs}) or 1,
    )
    try:
        response = TestClient(app).patch(
            "/api/source-candidates/7",
            json={
                "status": "rejected",
                "recommended_action": "reject",
                "review_comment": "Не подходит по теме",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert captured == {
        "candidate_id": 7,
        "status": "rejected",
        "recommended_action": "reject",
        "review_comment": "Не подходит по теме",
    }
    assert actions[0]["action_type"] == "update_source_candidate"
    assert actions[0]["input_payload"]["candidate_id"] == 7


def test_source_diagnose_endpoint_accepts_unsaved_overrides(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {
            "id": source_id,
            "name": "Example",
            "parse_strategy": "request",
            "listing_url": "https://old.example.com/news",
        },
    )

    def fake_diagnose(source, limit=5):
        captured.update(source)
        return {"source_id": source["id"], "listing_url": source["listing_url"], "limit": limit, "verdict": "ok"}

    monkeypatch.setattr(api, "diagnose_source", fake_diagnose)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/sources/7/diagnose?limit=4",
            json={"listing_url": "https://new.example.com/news", "listing_selector": ".card"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "source_id": 7,
        "listing_url": "https://new.example.com/news",
        "limit": 4,
        "verdict": "ok",
    }
    assert captured["listing_selector"] == ".card"


def test_source_diagnose_endpoint_can_enqueue_background_job(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {
            "id": source_id,
            "name": "Example",
            "parse_strategy": "playwright",
            "listing_url": "https://old.example.com/news",
        },
    )
    monkeypatch.setattr(
        api.background_jobs,
        "enqueue",
        lambda kind, payload, **kwargs: {
            "id": 120,
            "kind": kind,
            "queue_name": kwargs.get("queue_name", "default"),
            "execution_region": kwargs.get("execution_region", "ru"),
            "capability": kwargs.get("capability"),
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": kwargs.get("max_attempts", 3),
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/api/sources/7/diagnose?limit=4&background=true",
            json={"listing_url": "https://new.example.com/news", "listing_selector": ".card"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["job"] == {
        "id": 120,
        "kind": "diagnose_source",
        "queue": "playwright",
        "execution_region": "ru",
        "capability": "playwright",
        "status": "queued",
        "progress": 0.0,
        "attempts": 0,
        "max_attempts": 3,
        "payload": {
            "source_id": 7,
            "overrides": {"listing_url": "https://new.example.com/news", "listing_selector": ".card"},
            "limit": 4,
        },
        "result": {},
        "error": None,
        "agent_run_id": None,
        "run_after": None,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
    }


def test_create_monthly_digest_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    monkeypatch.setattr(
        api,
        "save_digest_draft",
        lambda month, limit=20, min_score=60, user_id=None, **kwargs: captured.update(
            {"month": month, "limit": limit, "min_score": min_score, "user_id": user_id, **kwargs}
        )
        or {
            "id": 9,
            "month": month,
            "title": f"Digest {month}",
            "status": "draft",
            "items": limit,
            "min_score": min_score,
        },
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/api/monthly-digests",
            json={"month": "2026-05", "limit": 7, "min_score": 65, "max_score": 90, "search": "drilling", "top_tag": "Бурение"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "id": 9,
        "month": "2026-05",
        "title": "Digest 2026-05",
        "status": "draft",
        "items": 7,
        "min_score": 65,
    }
    assert captured == {
        "month": "2026-05",
        "limit": 7,
        "min_score": 65.0,
        "max_score": 90.0,
        "search": "drilling",
        "top_tag": "Бурение",
        "user_id": 1,
    }


def test_digest_content_endpoint_passes_filters(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    monkeypatch.setattr(
        api,
        "build_digest_content",
        lambda month="", limit=100, min_score=0, user_id=None, **kwargs: captured.update(
            {"month": month, "limit": limit, "min_score": min_score, "user_id": user_id, **kwargs}
        )
        or {"issue": {"title": "Digest"}, "news": [], "items": []},
    )
    try:
        client = TestClient(app)
        response = client.get("/api/digest-content?month=2026-06&limit=30&min_score=55&max_score=88&search=drilling&top_tag=%D0%91%D1%83%D1%80%D0%B5%D0%BD%D0%B8%D0%B5")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured == {
        "month": "2026-06",
        "limit": 30,
        "min_score": 55.0,
        "max_score": 88.0,
        "search": "drilling",
        "top_tag": "Бурение",
        "user_id": 1,
    }


def test_update_monthly_digest_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    monkeypatch.setattr(
        api.repository,
        "save_monthly_digest",
        lambda month, title, items, status="draft", user_id=None: captured.update(
            {"month": month, "title": title, "items": items, "status": status, "user_id": user_id}
        )
        or {"id": 12, "user_id": user_id, "month": month, "title": title, "status": status, "items": len(items)},
    )
    try:
        client = TestClient(app)
        response = client.put(
            "/api/monthly-digests/2026-06",
            json={
                "title": "Digest 2026-06",
                "status": "draft",
                "items": [
                    {"article_id": 42, "section": "Технологии / Бурение", "editor_note": "First"},
                    {"article_id": 43, "section": "Рынок / ОФС", "editor_note": "Second"},
                ],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "id": 12,
        "user_id": 1,
        "month": "2026-06",
        "title": "Digest 2026-06",
        "status": "draft",
        "items": 2,
    }
    assert captured == {
        "month": "2026-06",
        "title": "Digest 2026-06",
        "status": "draft",
        "user_id": 1,
        "items": [
            {"article_id": 42, "section": "Технологии / Бурение", "editor_note": "First"},
            {"article_id": 43, "section": "Рынок / ОФС", "editor_note": "Second"},
        ],
    }


def test_get_monthly_digest_endpoint_scopes_to_user(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 7, "email": "test@example.com", "role": "user"}
    captured = {}
    monkeypatch.setattr(
        api.repository,
        "get_monthly_digest",
        lambda month, user_id=None: captured.update({"month": month, "user_id": user_id})
        or {
            "id": 14,
            "user_id": user_id,
            "month": month,
            "title": f"Digest {month}",
            "status": "draft",
            "items": [],
        },
    )
    try:
        client = TestClient(app)
        response = client.get("/api/monthly-digests/2026-06")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured == {"month": "2026-06", "user_id": 7}
    assert response.json()["user_id"] == 7


def test_update_monthly_digest_endpoint_allows_empty_issue(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}
    monkeypatch.setattr(
        api.repository,
        "save_monthly_digest",
        lambda month, title, items, status="draft", user_id=None: captured.update(
            {"month": month, "title": title, "items": items, "status": status, "user_id": user_id}
        )
        or {"id": 13, "user_id": user_id, "month": month, "title": title, "status": status, "items": len(items)},
    )
    try:
        client = TestClient(app)
        response = client.put(
            "/api/monthly-digests/2026-07",
            json={
                "title": "Digest 2026-07",
                "status": "draft",
                "items": [],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "id": 13,
        "user_id": 1,
        "month": "2026-07",
        "title": "Digest 2026-07",
        "status": "draft",
        "items": 0,
    }
    assert captured == {
        "month": "2026-07",
        "title": "Digest 2026-07",
        "status": "draft",
        "user_id": 1,
        "items": [],
    }


def test_source_health_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        api.repository,
        "source_health_report",
        lambda stale_days=3, limit=500, verdict=None: [
            {
                "id": 7,
                "name": "Example",
                "verdict": verdict or "no_articles",
                "articles": 0,
                "stale_days": stale_days,
                "limit": limit,
            }
        ],
    )
    try:
        client = TestClient(app)
        response = client.get("/api/source-health?stale_days=5&limit=10&verdict=stale")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {"id": 7, "name": "Example", "verdict": "stale", "articles": 0, "stale_days": 5, "limit": 10}
    ]


def test_digest_branding_endpoints(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    branding = {
        "header": {
            "brand_text": "Тест бренд",
            "brand_suffix": "Тест слоган",
            "department_text": "Тест департамент",
        },
        "hero": {
            "badge": "ТЕСТ",
            "headline": "Тестовый дайджест",
            "subtitle": "Подзаголовок",
            "image_url": "https://example.com/hero.jpg",
        },
        "issue": {
            "title_template": "Дайджест",
            "title_template_with_month": "Дайджест · {month}",
            "period_label_all": "всё время",
            "preheader": "Прехедер",
            "intro_template": "Интро",
            "intro_template_with_month": "Интро {month}",
            "highlights_title": "Итоги",
            "news_title": "Сигналы",
            "read_more_label": "Открыть",
            "empty_summary_text": "Нет сути",
            "preview_empty_text": "Пусто",
        },
        "footer": {
            "contact_text": "Пишите нам",
            "contact_email": "digest@example.com",
            "note": "Тест",
            "socials": [{"label": "Portal", "accent": "#111111", "text": "P"}],
        },
        "highlights": {
            "analytics_source_keywords": ["rystad"],
            "analytics_category_keywords": ["аналит"],
            "business_category_keywords": ["контракт"],
            "cards": [
                {"metric": "total", "icon": "doc", "prefix": "", "suffix": "", "noun_one": "новость", "noun_few": "новости", "noun_many": "новостей"},
                {"metric": "analytics", "icon": "chart", "prefix": "аналитических", "suffix": "", "noun_one": "материал", "noun_few": "материала", "noun_many": "материалов"},
                {"metric": "business", "icon": "people", "prefix": "", "suffix": "для бизнеса", "noun_one": "возможность", "noun_few": "возможности", "noun_many": "возможностей"},
            ],
        },
    }
    monkeypatch.setattr(api, "get_digest_branding", lambda: branding)
    monkeypatch.setattr(api, "save_digest_branding", lambda payload: payload)
    try:
        client = TestClient(app)
        get_response = client.get("/api/digest-branding")
        put_response = client.put("/api/digest-branding", json=branding)
    finally:
        app.dependency_overrides.clear()

    assert get_response.status_code == 200
    assert get_response.json() == branding
    assert put_response.status_code == 200
    assert put_response.json() == {"ok": True, "branding": branding}


def test_readiness_endpoint_reports_ok(monkeypatch):
    app = api.app
    monkeypatch.setattr(
        api,
        "readiness_check",
        lambda: {
            "ok": True,
            "database": {"ok": True},
            "schema": {"ok": True, "missing_tables": []},
            "jobs": {"queued_or_running": 0, "stale_running": 0, "stale_minutes": 60},
            "articles": 10,
        },
    )
    client = TestClient(app)
    response = client.get("/api/readiness")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["jobs"]["stale_running"] == 0


def test_readiness_endpoint_returns_503_for_not_ready(monkeypatch):
    app = api.app
    monkeypatch.setattr(
        api,
        "readiness_check",
        lambda: {
            "ok": False,
            "database": {"ok": True},
            "schema": {"ok": False, "missing_tables": ["background_jobs"]},
            "jobs": {"queued_or_running": 3, "stale_running": 1, "stale_minutes": 60},
            "articles": 10,
        },
    )
    client = TestClient(app)
    response = client.get("/api/readiness")

    assert response.status_code == 503
    payload = response.json()
    assert payload["ok"] is False
    assert payload["schema"]["missing_tables"] == ["background_jobs"]


def test_readiness_endpoint_returns_503_for_db_error(monkeypatch):
    app = api.app
    monkeypatch.setattr(api, "readiness_check", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    client = TestClient(app)
    response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json() == {"ok": False, "database": {"ok": False}, "error": "db down"}


def test_list_articles_applies_filters_and_score_items(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    fake_conn = FakeConnection()
    monkeypatch.setattr(api, "get_connection", lambda: fake_conn)
    try:
        client = TestClient(app)
        response = client.get(
            "/api/articles",
            params={
                "search": "drilling",
                "source": "World Oil",
                "tag": "Бурение",
                "status": "digest",
                "language": "en",
                "min_score": 80,
                "max_score": 95,
                "date_from": "2026-06-01",
                "date_to": "2026-06-30",
                "sort": "date_desc",
                "changed_only": True,
                "limit": 25,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["id"] == 42
    assert payload[0]["tag"] == "Технологии / Бурение"
    assert payload[0]["score"] == 88
    assert payload[0]["digest"] is True
    assert payload[0]["score_items"][0]["name"] == "Технологическая значимость"

    articles_sql, articles_params = fake_conn.executed[0]
    # Поиск идёт по видимому заголовку (title_ru с откатом на оригинал), телу, сути И тегу —
    # проверяем состав, а не точную склейку строки, иначе тест ломается от переносов.
    assert "LOWER(" in articles_sql and "LIKE %s" in articles_sql
    for fragment in ("c.title_ru", "a.title", "a.raw_text", "c.summary", "t.name", "parent.name"):
        assert fragment in articles_sql, f"поиск обязан покрывать {fragment}"
    assert "s.name = %s" in articles_sql
    assert "(t.name = %s OR parent.name = %s)" in articles_sql
    assert "user_article_states uas ON uas.article_id = a.id AND uas.user_id = %s" in articles_sql  # пер-юзерный статус (#12)
    assert "COALESCE(uas.status, 'new') = %s" in articles_sql
    assert "COALESCE(uas.status, 'new') <> 'new'" in articles_sql
    assert "a.language = %s" in articles_sql
    # min_score применяется ТОЛЬКО к уже оценённым: у неоценённых балла нет, и COALESCE(...,0)
    # выдавал бы их за 0, отсекая свежий приток порогом (лента выглядела замороженной).
    assert "(sc.total_score IS NULL OR sc.total_score >= %s)" in articles_sql
    assert "COALESCE(sc.total_score, 0) <= %s" in articles_sql
    assert "COALESCE(a.published_at::date, a.collected_at::date) >= %s" in articles_sql
    assert "COALESCE(a.published_at::date, a.collected_at::date) <= %s" in articles_sql
    # user_id — первый параметр (LEFT JOIN), затем фильтры, затем limit.
    assert "ORDER BY a.published_at DESC NULLS LAST" in articles_sql
    assert articles_params == [1, "%drilling%", "World Oil", "Бурение", "Бурение", "digest", "en", 80.0, 95.0, "2026-06-01", "2026-06-30", 25]


def test_update_source_persists_non_rss_scraper_fields(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}

    class UpdateConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params

            class Result:
                def fetchone(self):
                    return {"id": 9}

            return Result()

        def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(api, "get_connection", lambda: UpdateConnection())
    try:
        client = TestClient(app)
        response = client.patch(
            "/api/sources/9",
            json={
                "parse_strategy": "playwright",
                "listing_url": "https://example.com/news",
                "listing_selector": ".card",
                "article_link_selector": ".card a",
                "article_date_selector": "time",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "parse_strategy = %s" in captured["sql"]
    assert "listing_selector = %s" in captured["sql"]
    assert captured["params"] == [
        "playwright",
        "https://example.com/news",
        ".card",
        ".card a",
        "time",
        9,
    ]
    assert captured["committed"] is True


def test_scrape_source_endpoint_routes_request_strategy(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {"id": source_id, "name": "Request Source", "parse_strategy": "request"},
    )
    monkeypatch.setattr(api.request_parser, "parse_source", lambda source: {"added": 2, "attempted": 3})
    try:
        client = TestClient(app)
        response = client.post("/api/sources/12/scrape")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True, "stats": {"added": 2, "attempted": 3}}


def test_scrape_source_endpoint_can_enqueue_background_job(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {"id": source_id, "name": "Request Source", "parse_strategy": "request"},
    )
    monkeypatch.setattr(
        api.background_jobs,
        "enqueue",
        lambda kind, payload, **kwargs: {
            "id": 99,
            "kind": kind,
            "queue_name": kwargs.get("queue_name", "default"),
            "execution_region": kwargs.get("execution_region", "ru"),
            "capability": kwargs.get("capability"),
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": kwargs.get("max_attempts", 3),
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    try:
        client = TestClient(app)
        response = client.post("/api/sources/12/scrape?background=true")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["job"] == {
        "id": 99,
        "kind": "scrape_source",
        "queue": "default",
        "execution_region": "ru",
        "capability": "http_fetch",
        "status": "queued",
        "progress": 0.0,
        "attempts": 0,
        "max_attempts": 3,
        "payload": {"source_id": 12},
        "result": {},
        "error": None,
        "agent_run_id": None,
        "run_after": None,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
    }


def test_scrape_source_endpoint_routes_playwright_strategy(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {"id": source_id, "name": "Rendered Source", "parse_strategy": "playwright"},
    )
    monkeypatch.setattr(api.playwright_parser, "parse_source", lambda source: {"added": 1, "attempted": 1})
    try:
        client = TestClient(app)
        response = client.post("/api/sources/13/scrape")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True, "stats": {"added": 1, "attempted": 1}}


def test_scrape_source_endpoint_rejects_non_scraper_strategy(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {"id": source_id, "name": "RSS Source", "parse_strategy": "rss"},
    )
    try:
        client = TestClient(app)
        response = client.post("/api/sources/14/scrape")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "request/playwright" in response.json()["detail"]


def test_auth_register_login_me_and_logout(monkeypatch):
    # Самостоятельная регистрация по умолчанию закрыта (#33) — здесь включаем её
    # явно, чтобы проверять сам сценарий, а не запрет.
    monkeypatch.setattr(api.config, "AUTH_ALLOW_SELF_REGISTRATION", True)
    app = api.app
    sessions = {}
    users = {"user@example.com": {"id": 1, "email": "user@example.com"}}

    monkeypatch.setattr(api.repository, "create_user", lambda email, password: users[email])
    monkeypatch.setattr(api.repository, "authenticate_user", lambda email, password: users.get(email))
    monkeypatch.setattr(api.repository, "create_user_session", lambda user_id: f"session-{user_id}")
    monkeypatch.setattr(api.repository, "get_user_by_session", lambda token: sessions.get(token))
    monkeypatch.setattr(api.repository, "delete_user_session", lambda token: sessions.pop(token, None))

    client = TestClient(app)
    register_response = client.post("/api/auth/register", json={"email": " USER@example.com ", "password": "12345678"})
    assert register_response.status_code == 200
    assert register_response.json()["user"]["email"] == "user@example.com"

    sessions["session-1"] = users["user@example.com"]
    me_response = client.get("/api/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["user"]["email"] == "user@example.com"

    login_response = client.post("/api/auth/login", json={"email": "user@example.com", "password": "12345678"})
    assert login_response.status_code == 200

    logout_response = client.post("/api/auth/logout")
    assert logout_response.status_code == 200
    assert logout_response.json() == {"ok": True}


def test_auth_rejects_invalid_payloads_and_missing_session(monkeypatch):
    client = TestClient(api.app)

    assert client.get("/api/auth/me").status_code == 401

    # Закрытая регистрация отвечает 403 ДО валидации полей: путь существует, но выключен.
    assert client.post("/api/auth/register", json={"email": "bad", "password": "12345678"}).status_code == 403

    monkeypatch.setattr(api.config, "AUTH_ALLOW_SELF_REGISTRATION", True)
    assert client.post("/api/auth/register", json={"email": "bad", "password": "12345678"}).status_code == 400
    assert client.post("/api/auth/register", json={"email": "user@example.com", "password": "1234567"}).status_code == 400

    monkeypatch.setattr(api.repository, "authenticate_user", lambda email, password: None)
    assert client.post("/api/auth/login", json={"email": "user@example.com", "password": "12345678"}).status_code == 401


def test_enqueue_digest_export_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}

    def fake_enqueue(kind, payload, **kwargs):
        captured["kind"] = kind
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return {
            "id": 7,
            "kind": kind,
            "queue_name": kwargs.get("queue_name", "default"),
            "execution_region": kwargs.get("execution_region", "ru"),
            "capability": kwargs.get("capability"),
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": kwargs.get("max_attempts", 3),
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        }

    monkeypatch.setattr(api.background_jobs, "enqueue", fake_enqueue)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/jobs/digest-export",
            json={"month": "2026-06", "export_format": "pdf", "limit": 25, "min_score": 60, "max_score": 95, "search": "drilling", "top_tag": "Бурение"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured == {
        "kind": "digest_export",
        "payload": {
            "month": "2026-06",
            "export_format": "pdf",
            "limit": 25,
            "min_score": 60.0,
            "max_score": 95.0,
            "search": "drilling",
            "top_tag": "Бурение",
            "user_id": 1,
        },
        "kwargs": {"user_id": 1, "queue_name": "playwright", "execution_region": "ru", "capability": "playwright"},
    }
    assert response.json()["job"]["status"] == "queued"
    assert response.json()["job"]["queue"] == "playwright"


def test_enqueue_process_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        api.background_jobs,
        "enqueue",
        lambda kind, payload, **kwargs: {
            "id": 8,
            "kind": kind,
            "queue_name": kwargs.get("queue_name", "default"),
            "execution_region": kwargs.get("execution_region", "ru"),
            "capability": kwargs.get("capability"),
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": kwargs.get("max_attempts", 3),
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    try:
        client = TestClient(app)
        response = client.post("/api/jobs/process", json={"article_ids": [1, 2], "offline": True})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["job"]["kind"] == "process_articles"
    assert response.json()["job"]["queue"] == "ai"
    assert response.json()["job"]["execution_region"] == "ru"
    assert response.json()["job"]["capability"] == "openai"
    assert response.json()["job"]["payload"]["article_ids"] == [1, 2]


def test_enqueue_process_endpoint_can_route_to_external_ai(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(api.network_policy.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(api.network_policy.config, "AI_EXECUTION_REGION", "external")
    captured = {}

    def fake_enqueue(kind, payload, **kwargs):
        captured.update(kwargs)
        return {
            "id": 9,
            "kind": kind,
            "queue_name": kwargs.get("queue_name", "default"),
            "execution_region": kwargs.get("execution_region", "ru"),
            "capability": kwargs.get("capability"),
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": kwargs.get("max_attempts", 3),
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        }

    monkeypatch.setattr(api.background_jobs, "enqueue", fake_enqueue)
    try:
        client = TestClient(app)
        response = client.post("/api/jobs/process", json={"limit": 10})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured == {"user_id": 1, "queue_name": "external-ai", "execution_region": "external", "capability": "openai"}
    assert response.json()["job"]["queue"] == "external-ai"
    assert response.json()["job"]["execution_region"] == "external"


def test_manual_article_import_endpoint_enqueues_ai_job(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        api,
        "import_manual_article",
        lambda url, explicit_source_id=None: type(
            "ImportResult",
            (),
            {
                "article_id": 55,
                "source_id": 7,
                "source_name": "Manual import: example.com",
                "duplicate": False,
                "title": "Example imported article",
                "fetch_method": "http",
                "full_text_status": "ok",
                "full_text_method": "http",
                "full_text_chars": 1840,
            },
        )(),
    )
    monkeypatch.setattr(api.network_policy.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(api.network_policy.config, "AI_EXECUTION_REGION", "external")
    captured = {}

    def fake_enqueue(kind, payload, **kwargs):
        captured["kind"] = kind
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return {
            "id": 41,
            "kind": kind,
            "queue_name": kwargs.get("queue_name", "default"),
            "execution_region": kwargs.get("execution_region", "ru"),
            "capability": kwargs.get("capability"),
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": kwargs.get("max_attempts", 3),
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        }

    monkeypatch.setattr(api.background_jobs, "enqueue", fake_enqueue)
    try:
        client = TestClient(app)
        response = client.post("/api/articles/import", json={"url": "https://example.com/news/1", "source_id": 7})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["article"] == {
        "id": 55,
        "source_id": 7,
        "source_name": "Manual import: example.com",
        "duplicate": False,
        "title": "Example imported article",
        "fetch_method": "http",
        "full_text_status": "ok",
        "full_text_method": "http",
        "full_text_chars": 1840,
    }
    assert captured == {
        "kind": "process_articles",
        "payload": {"article_ids": [55], "limit": 1, "offline": False},
        "kwargs": {"user_id": 1, "queue_name": "external-ai", "execution_region": "external", "capability": "openai"},
    }
    assert response.json()["job"]["queue"] == "external-ai"


def test_manual_article_import_endpoint_can_skip_ai_processing(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        api,
        "import_manual_article",
        lambda url, explicit_source_id=None: type(
            "ImportResult",
            (),
            {
                "article_id": 91,
                "source_id": 3,
                "source_name": "World Oil",
                "duplicate": True,
                "title": "Already imported",
                "fetch_method": "existing",
                "full_text_status": "ok",
                "full_text_method": "http",
                "full_text_chars": 920,
            },
        )(),
    )
    enqueue_mock = []
    monkeypatch.setattr(api.background_jobs, "enqueue", lambda *args, **kwargs: enqueue_mock.append((args, kwargs)))
    try:
        client = TestClient(app)
        response = client.post("/api/articles/import", json={"url": "https://example.com/news/2", "process": False})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["article"]["duplicate"] is True
    assert "job" not in response.json()
    assert enqueue_mock == []


def test_jobs_endpoints_scope_non_admin_to_own_jobs(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 7, "email": "user@example.com", "role": "user"}
    captured = {}

    def fake_list_background_jobs(**kwargs):
        captured["list"] = kwargs
        return []

    def fake_get_background_job(job_id, **kwargs):
        captured["get"] = {"job_id": job_id, **kwargs}
        return None

    monkeypatch.setattr(api.repository, "list_background_jobs", fake_list_background_jobs)
    monkeypatch.setattr(api.repository, "get_background_job", fake_get_background_job)
    try:
        client = TestClient(app)
        list_response = client.get("/api/jobs?kind=digest_export")
        get_response = client.get("/api/jobs/42")
    finally:
        app.dependency_overrides.clear()

    assert list_response.status_code == 200
    assert captured["list"]["user_id"] == 7
    assert get_response.status_code == 404
    assert captured["get"] == {"job_id": 42, "user_id": 7}


def test_jobs_endpoints_allow_admin_to_see_all_jobs(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "admin@example.com", "role": "admin"}
    captured = {}

    def fake_list_background_jobs(**kwargs):
        captured["list"] = kwargs
        return []

    def fake_get_background_job(job_id, **kwargs):
        captured["get"] = {"job_id": job_id, **kwargs}
        return {
            "id": job_id,
            "kind": "digest_export",
            "queue_name": "default",
            "execution_region": "ru",
            "capability": None,
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": 3,
            "run_after": None,
            "payload_json": {},
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        }

    monkeypatch.setattr(api.repository, "list_background_jobs", fake_list_background_jobs)
    monkeypatch.setattr(api.repository, "get_background_job", fake_get_background_job)
    try:
        client = TestClient(app)
        list_response = client.get("/api/jobs")
        get_response = client.get("/api/jobs/42")
    finally:
        app.dependency_overrides.clear()

    assert list_response.status_code == 200
    assert captured["list"]["user_id"] is None
    assert get_response.status_code == 200
    assert captured["get"] == {"job_id": 42}


def test_external_worker_claim_requires_token(monkeypatch):
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    client = TestClient(api.app)

    response = client.post("/api/external-worker/claim", json={"worker_id": "eu-1"})

    assert response.status_code == 401


def test_external_worker_claim_returns_leased_job(monkeypatch):
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    monkeypatch.setattr(api.repository, "requeue_expired_external_leases", lambda: 0)
    built_for = []
    monkeypatch.setattr(
        api.external_ai,
        "build_process_articles_payload",
        lambda payload, job_id=None: built_for.append(job_id)
        or {"kind": "process_articles", "articles": [{"id": 1}], "tags": [], "criteria": []},
    )
    captured = {}

    def fake_claim(**kwargs):
        captured.update(kwargs)
        return {
            "id": 10,
            "kind": "process_articles",
            "queue_name": "external-ai",
            "execution_region": "external",
            "capability": "openai",
            "status": "running",
            "progress": 10,
            "attempts": 1,
            "max_attempts": 3,
            "run_after": None,
            "payload_json": {"limit": 5},
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        }

    monkeypatch.setattr(api.repository, "claim_external_background_job", fake_claim)
    client = TestClient(api.app)

    response = client.post(
        "/api/external-worker/claim",
        headers={"Authorization": "Bearer secret"},
        json={"worker_id": "eu-1", "queues": ["external-ai"], "capabilities": ["openai"], "max_lease_seconds": 300},
    )

    assert response.status_code == 200
    assert captured["queue_names"] == ["external-ai"]
    assert captured["capabilities"] == ["openai"]
    assert captured["worker_id"] == "eu-1"
    assert captured["lease_seconds"] == 300
    assert captured["lease_token_hash"] != "secret"
    assert response.json()["job"]["queue"] == "external-ai"
    assert response.json()["job"]["payload"]["articles"] == [{"id": 1}]
    assert response.json()["job"]["lease_token"]
    assert built_for == [10]  # статьи резервируются за выданной задачей


def test_external_worker_claim_hydrates_external_scrape_payload(monkeypatch):
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    monkeypatch.setattr(api.repository, "requeue_expired_external_leases", lambda: 0)
    monkeypatch.setattr(
        api.external_fetch,
        "build_scrape_source_payload",
        lambda source_id, payload: {"kind": "scrape_source", "source": {"id": source_id, "parse_strategy": "request"}},
    )
    monkeypatch.setattr(
        api.repository,
        "claim_external_background_job",
        lambda **kwargs: {
            "id": 11,
            "kind": "scrape_source",
            "queue_name": "external-fetch",
            "execution_region": "external",
            "capability": "http_fetch",
            "status": "running",
            "progress": 10,
            "attempts": 1,
            "max_attempts": 3,
            "run_after": None,
            "payload_json": {"source_id": 7},
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    client = TestClient(api.app)

    response = client.post(
        "/api/external-worker/claim",
        headers={"Authorization": "Bearer secret"},
        json={"worker_id": "eu-1", "queues": ["external-fetch"], "capabilities": ["http_fetch"]},
    )

    assert response.status_code == 200
    assert response.json()["job"]["payload"]["source"] == {"id": 7, "parse_strategy": "request"}


def test_external_worker_claim_hydrates_source_candidate_evaluation_payload(monkeypatch):
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    monkeypatch.setattr(api.repository, "requeue_expired_external_leases", lambda: 0)
    monkeypatch.setattr(
        api.external_ai,
        "build_source_candidate_evaluate_payload",
        lambda payload: {"kind": "source_candidate_evaluate", "candidate_id": payload["candidate_id"], "articles": [{"id": 101}]},
    )
    monkeypatch.setattr(
        api.repository,
        "claim_external_background_job",
        lambda **kwargs: {
            "id": 12,
            "kind": "source_candidate_evaluate",
            "queue_name": "external-agents",
            "execution_region": "external",
            "capability": "openai",
            "status": "running",
            "progress": 10,
            "attempts": 1,
            "max_attempts": 1,
            "run_after": None,
            "payload_json": {"candidate_id": 42},
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    client = TestClient(api.app)

    response = client.post(
        "/api/external-worker/claim",
        headers={"Authorization": "Bearer secret"},
        json={"worker_id": "nl-agents-1", "queues": ["external-agents"], "capabilities": ["openai"]},
    )

    assert response.status_code == 200
    assert response.json()["job"]["payload"] == {
        "kind": "source_candidate_evaluate",
        "candidate_id": 42,
        "articles": [{"id": 101}],
    }


def test_external_worker_progress_and_complete_validate_lease(monkeypatch):
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    progress_calls = []
    complete_calls = []
    monkeypatch.setattr(
        api.repository,
        "update_external_background_job_progress",
        lambda job_id, **kwargs: progress_calls.append((job_id, kwargs)) or True,
    )
    monkeypatch.setattr(
        api.repository,
        "finish_external_background_job",
        lambda job_id, **kwargs: complete_calls.append((job_id, kwargs)) or True,
    )
    monkeypatch.setattr(api.repository, "get_background_job", lambda job_id: {"id": job_id, "kind": "digest_export"})
    monkeypatch.setattr(api.repository, "begin_external_background_job_finalize", lambda job_id, **kwargs: True)
    client = TestClient(api.app)

    progress = client.post(
        "/api/external-worker/jobs/10/progress",
        headers={"Authorization": "Bearer secret"},
        json={"lease_token": "lease", "progress": 55},
    )
    complete = client.post(
        "/api/external-worker/jobs/10/complete",
        headers={"Authorization": "Bearer secret"},
        json={"lease_token": "lease", "result": {"summary": {"processed": 1}}},
    )

    assert progress.status_code == 200
    assert complete.status_code == 200
    assert progress_calls[0][0] == 10
    assert progress_calls[0][1]["lease_token_hash"] == api._sha256_hex("lease")
    assert complete_calls[0][1]["result"] == {"summary": {"processed": 1}}


def test_external_worker_complete_applies_external_ai_result(monkeypatch):
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    applied = []
    completed = []
    monkeypatch.setattr(api.repository, "get_background_job", lambda job_id: {"id": job_id, "kind": "process_articles"})
    monkeypatch.setattr(api.repository, "begin_external_background_job_finalize", lambda job_id, **kwargs: True)
    monkeypatch.setattr(api.external_ai, "apply_process_result", lambda result, **kwargs: applied.append(result) or {"articles": 1})
    monkeypatch.setattr(
        api.repository,
        "finish_external_background_job",
        lambda job_id, **kwargs: completed.append((job_id, kwargs)) or True,
    )
    client = TestClient(api.app)

    response = client.post(
        "/api/external-worker/jobs/10/complete",
        headers={"Authorization": "Bearer secret"},
        json={"lease_token": "lease", "result": {"external_ai": True, "articles": [{"article_id": 1}]}},
    )

    assert response.status_code == 200
    assert applied == [{"external_ai": True, "articles": [{"article_id": 1}]}]
    assert completed[0][1]["result"]["applied"] == {"articles": 1}


def test_external_worker_complete_applies_source_candidate_result(monkeypatch):
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    applied = []
    completed = []
    monkeypatch.setattr(api.repository, "get_background_job", lambda job_id: {"id": job_id, "kind": "source_candidate_evaluate"})
    monkeypatch.setattr(api.repository, "begin_external_background_job_finalize", lambda job_id, **kwargs: True)
    monkeypatch.setattr(
        api.external_ai,
        "apply_source_candidate_result",
        lambda result, **kwargs: applied.append((result, kwargs)) or {"articles": 1, "ok": 1},
    )
    monkeypatch.setattr(
        api.repository,
        "finish_external_background_job",
        lambda job_id, **kwargs: completed.append((job_id, kwargs)) or True,
    )
    client = TestClient(api.app)

    response = client.post(
        "/api/external-worker/jobs/10/complete",
        headers={"Authorization": "Bearer secret"},
        json={"lease_token": "lease", "result": {"source_candidate_evaluate": True, "candidate_id": 42, "articles": []}},
    )

    assert response.status_code == 200
    assert applied == [({"source_candidate_evaluate": True, "candidate_id": 42, "articles": []}, {"job_id": 10})]
    assert completed[0][1]["result"]["applied"] == {"articles": 1, "ok": 1}


def test_external_worker_complete_reads_recheck_flags_from_payload_json(monkeypatch):
    """Флаги мягкого режима recheck читаются из payload_json — РЕАЛЬНОГО ключа строки задачи.

    Баг T3 (P1, потеря данных): код читал job.get("payload"), которого в строке нет —
    get_background_job делает SELECT *, поэтому ключ называется payload_json (schema.sql:304).
    В результате mark/dry_run/force ВСЕГДА были False, и recheck удалял статьи ФИЗИЧЕСКИ,
    даже когда оператор явно просил только пометить (--mark) или лишь посмотреть (--dry-run).
    Так уже потеряли ~2000 статей. Прод гонит AI на внешнем воркере, т.е. это живой путь.

    Прежние тесты этот баг не ловили, потому что мокали задачу рукописным словарём без
    payload_json — здесь форма строки повторяет то, что реально отдаёт репозиторий.
    """
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        api.repository,
        "get_background_job",
        lambda job_id: {
            "id": job_id,
            "kind": "recheck_relevance",
            "payload_json": {"mark": True, "dry_run": False, "force": True},
        },
    )
    monkeypatch.setattr(api.repository, "begin_external_background_job_finalize", lambda job_id, **kwargs: True)
    monkeypatch.setattr(
        api.external_ai,
        "apply_recheck_result",
        lambda result, **kwargs: captured.update(kwargs) or {"checked": 1, "marked": 1},
    )
    monkeypatch.setattr(api.repository, "finish_external_background_job", lambda job_id, **kwargs: True)
    client = TestClient(api.app)

    response = client.post(
        "/api/external-worker/jobs/10/complete",
        headers={"Authorization": "Bearer secret"},
        json={"lease_token": "lease", "result": {"recheck_relevance": True, "articles": []}},
    )

    assert response.status_code == 200
    assert captured.get("mark") is True, "mark потерян → статьи удалятся ФИЗИЧЕСКИ вместо пометки"
    assert captured.get("force") is True
    assert captured.get("dry_run") is False


def test_external_worker_complete_applies_external_fetch_result(monkeypatch):
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    applied = []
    completed = []
    monkeypatch.setattr(api.repository, "get_background_job", lambda job_id: {"id": job_id, "kind": "scrape_source"})
    monkeypatch.setattr(api.repository, "begin_external_background_job_finalize", lambda job_id, **kwargs: True)
    monkeypatch.setattr(api.external_fetch, "apply_scrape_result", lambda result: applied.append(result) or {"inserted": 1})
    monkeypatch.setattr(
        api.repository,
        "finish_external_background_job",
        lambda job_id, **kwargs: completed.append((job_id, kwargs)) or True,
    )
    client = TestClient(api.app)

    response = client.post(
        "/api/external-worker/jobs/10/complete",
        headers={"Authorization": "Bearer secret"},
        json={"lease_token": "lease", "result": {"external_fetch": True, "source_id": 1, "articles": []}},
    )

    assert response.status_code == 200
    assert applied == [{"external_fetch": True, "source_id": 1, "articles": []}]
    assert completed[0][1]["result"]["applied"] == {"inserted": 1}


def test_external_worker_complete_rejects_inactive_lease(monkeypatch):
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    monkeypatch.setattr(api.repository, "get_background_job", lambda job_id: {"id": job_id, "kind": "digest_export"})
    monkeypatch.setattr(api.repository, "begin_external_background_job_finalize", lambda job_id, **kwargs: False)
    monkeypatch.setattr(api.repository, "finish_external_background_job", lambda job_id, **kwargs: False)
    client = TestClient(api.app)

    response = client.post(
        "/api/external-worker/jobs/10/complete",
        headers={"Authorization": "Bearer secret"},
        json={"lease_token": "bad", "result": {}},
    )

    assert response.status_code == 409


def test_external_worker_fail_passes_retry_policy(monkeypatch):
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    captured = {}
    monkeypatch.setattr(
        api.repository,
        "fail_external_background_job",
        lambda job_id, **kwargs: captured.update({"job_id": job_id, **kwargs}) or True,
    )
    client = TestClient(api.app)

    response = client.post(
        "/api/external-worker/jobs/10/fail",
        headers={"Authorization": "Bearer secret"},
        json={"lease_token": "lease", "error": "timeout", "retryable": True, "retry_after_seconds": 120},
    )

    assert response.status_code == 200
    assert captured["job_id"] == 10
    assert captured["lease_token_hash"] == api._sha256_hex("lease")
    assert captured["error_message"] == "timeout"
    assert captured["retryable"] is True
    assert captured["retry_delay_seconds"] == 120


def test_maintenance_status_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    monkeypatch.setattr(
        api,
        "maintenance_status",
        lambda: {
            "retention": {"stale_minutes": 60, "background_job_days": 30, "export_job_days": 14},
            "expired_sessions": 2,
            "stale_running_jobs": 1,
            "cleanup_candidates": {"background_jobs": 5, "export_jobs": 3},
            "external_queues": {
                "totals": {"queued": 4, "running": 1, "failed": 0, "ok": 0, "oldest_queued_at": None, "last_heartbeat_at": None, "expired_leases": 0},
                "queues": [],
            },
        },
    )
    try:
        client = TestClient(app)
        response = client.get("/api/maintenance/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["expired_sessions"] == 2
    assert response.json()["cleanup_candidates"]["background_jobs"] == 5
    assert response.json()["external_queues"]["totals"]["queued"] == 4


def test_maintenance_cleanup_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}

    def fake_cleanup(**kwargs):
        captured.update(kwargs)
        return {"expired_sessions": 1, "background_jobs": 4, "background_job_days": 10, "export_jobs": 2, "export_job_days": 5}

    monkeypatch.setattr(api, "maintenance_cleanup", fake_cleanup)
    try:
        client = TestClient(app)
        response = client.post("/api/maintenance/cleanup", json={"background_job_days": 10, "export_job_days": 5})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured == {"background_job_days": 10, "export_job_days": 5}
    assert response.json()["ok"] is True
    assert response.json()["result"]["background_jobs"] == 4


def test_maintenance_cleanup_endpoint_rejects_invalid_retention():
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    try:
        client = TestClient(app)
        response = client.post("/api/maintenance/cleanup", json={"background_job_days": 0})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "background_job_days" in response.json()["detail"]


def test_maintenance_benchmark_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    captured = {}

    def fake_benchmark(**kwargs):
        captured.update(kwargs)
        return {
            "iterations": kwargs["iterations"],
            "warn_ms": kwargs["warn_ms"],
            "params": {"articles_limit": kwargs["articles_limit"]},
            "benchmarks": [{"name": "articles_list", "status": "ok", "rows": 10, "p50_ms": 12.0, "p95_ms": 20.0, "max_ms": 25.0}],
            "counts": {"articles": 42},
            "warnings": [],
        }

    monkeypatch.setattr(api, "run_readiness_benchmark", fake_benchmark)
    try:
        client = TestClient(app)
        response = client.get("/api/maintenance/benchmark?iterations=2&articles_limit=150")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured["iterations"] == 2
    assert captured["articles_limit"] == 150
    assert response.json()["counts"]["articles"] == 42
    assert response.json()["benchmarks"][0]["name"] == "articles_list"


def test_process_endpoints_require_admin():
    """Тех-долг T7: /api/process и /api/jobs/process тратят платный OpenAI —
    должны быть admin-only, обычному пользователю 403."""
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 2, "email": "user@example.com", "role": "user"}
    try:
        client = TestClient(app)
        r_enqueue = client.post("/api/jobs/process", json={"limit": 5})
        r_process = client.post("/api/process", json={"limit": 5})
    finally:
        app.dependency_overrides.clear()

    assert r_enqueue.status_code == 403
    assert r_process.status_code == 403


def test_process_endpoints_allow_admin(monkeypatch):
    """Админ проходит гейт прав (не 403) — enqueue замокан, без реального OpenAI/БД."""
    import types

    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "admin@example.com", "role": "admin"}
    monkeypatch.setattr(
        api.network_policy,
        "route_ai_processing",
        lambda: types.SimpleNamespace(queue_name="default", execution_region="ru", capability=None),
    )
    monkeypatch.setattr(api.background_jobs, "enqueue", lambda *a, **k: {"id": 1, "kind": "process_articles", "status": "queued"})
    monkeypatch.setattr(api, "_job_payload", lambda job: job)
    try:
        client = TestClient(app)
        r_enqueue = client.post("/api/jobs/process", json={"limit": 5})
    finally:
        app.dependency_overrides.clear()

    assert r_enqueue.status_code != 403
    assert r_enqueue.status_code == 200


def test_session_cookie_secure_follows_config(monkeypatch):
    """Тех-долг T8: при AUTH_COOKIE_SECURE=True (прод/HTTPS) cookie получает Secure;
    при False (локалка/http) — нет. HttpOnly всегда."""
    from fastapi import Response

    from oiltech_digest import config

    monkeypatch.setattr(config, "AUTH_COOKIE_SECURE", True)
    secure_resp = Response()
    api._set_session_cookie(secure_resp, "token-abc")
    secure_cookie = secure_resp.headers.get("set-cookie", "")
    assert "Secure" in secure_cookie
    assert "HttpOnly" in secure_cookie

    monkeypatch.setattr(config, "AUTH_COOKIE_SECURE", False)
    plain_resp = Response()
    api._set_session_cookie(plain_resp, "token-abc")
    plain_cookie = plain_resp.headers.get("set-cookie", "")
    assert "Secure" not in plain_cookie
    assert "HttpOnly" in plain_cookie


def _as_user(role: str):
    """Подменить текущего пользователя ролью (require_admin зависит от require_user транзитивно)."""
    return lambda: {"id": 2, "email": "user@example.com", "role": role}


def test_digest_branding_write_is_admin_only():
    """Аудит изоляции 24.07: PUT /api/digest-branding стоял на require_user, а пишет
    ОДИН общий файл digest_branding.json → обычный пользователь менял шапку/футер/hero
    во всех выгрузках у всех, включая ту, что уходит заказчику."""
    app = api.app
    app.dependency_overrides[api.require_user] = _as_user("user")
    try:
        client = TestClient(app)
        response = client.put("/api/digest-branding", json={})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


def test_maintenance_endpoints_are_admin_only():
    """Аудит изоляции 24.07: /api/maintenance/* стояли на require_user, а cleanup удаляет
    background_jobs и export_jobs ВСЕХ пользователей (в SQL нет фильтра по user_id) —
    любой пользователь стирал историю задач и выгрузок остальным."""
    app = api.app
    app.dependency_overrides[api.require_user] = _as_user("user")
    try:
        client = TestClient(app)
        cleanup = client.post("/api/maintenance/cleanup", json={"background_job_days": 1})
        status = client.get("/api/maintenance/status")
        benchmark = client.get("/api/maintenance/benchmark")
    finally:
        app.dependency_overrides.clear()

    assert cleanup.status_code == 403
    assert status.status_code == 403
    assert benchmark.status_code == 403


def test_admin_still_reaches_maintenance_status(monkeypatch):
    """Симметрия: админа не заблокировали (иначе «починили» ценой поломки экрана)."""
    app = api.app
    app.dependency_overrides[api.require_user] = _as_user("admin")
    monkeypatch.setattr(api, "maintenance_status", lambda: {"ok": True})
    try:
        client = TestClient(app)
        response = client.get("/api/maintenance/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


def _articles_sql(monkeypatch) -> str:
    """Перехватить SQL, который list_articles реально отправляет в БД."""
    captured = {}

    class Cur:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            return self

        def fetchall(self):
            return []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self, row_factory=None):
            return Cur()

    monkeypatch.setattr(api, "get_connection", lambda: Conn())
    return captured


def test_feed_hides_own_noise_duplicate_and_archive(monkeypatch):
    """Задача 19: пометка «Шум»/«Дубликат» обязана убирать статью из ЛИЧНОЙ ленты.
    Замер 24.07: 104 помеченных статьи продолжали висеть — фильтра по статусу не было вовсе.

    12.09: к ним добавлен `archive`. Раньше он был декоративным счётчиком и ничего не делал;
    теперь это целевой статус для «снял из дайджеста», и он обязан убирать с глаз —
    иначе снятая статья остаётся в ленте и операция выглядит как не сработавшая."""
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 7, "email": "u@e.ru", "role": "user"}
    captured = _articles_sql(monkeypatch)
    try:
        TestClient(app).get("/api/articles")
    finally:
        app.dependency_overrides.clear()

    assert "NOT IN ('noise', 'duplicate', 'archive')" in captured["sql"]
    # Статус `review` убран из набора: его не должно остаться ни в одном запросе ленты.
    assert "review" not in captured["sql"]


def test_feed_still_shows_noise_when_explicitly_filtered(monkeypatch):
    """Помеченное должно оставаться доступным по явному фильтру — иначе его не пересмотреть."""
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 7, "email": "u@e.ru", "role": "user"}
    captured = _articles_sql(monkeypatch)
    try:
        TestClient(app).get("/api/articles?status=noise")
    finally:
        app.dependency_overrides.clear()

    assert "NOT IN ('noise', 'duplicate')" not in captured["sql"]


def test_changed_only_tab_still_shows_marked(monkeypatch):
    """Вкладка «Со статусом» показывает всё размеченное, включая шум и дубликаты."""
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 7, "email": "u@e.ru", "role": "user"}
    captured = _articles_sql(monkeypatch)
    try:
        TestClient(app).get("/api/articles?changed_only=true")
    finally:
        app.dependency_overrides.clear()

    assert "NOT IN ('noise', 'duplicate')" not in captured["sql"]
    assert "<> 'new'" in captured["sql"]


def test_monthly_stats_is_admin_only(monkeypatch):
    """Задача 18: раздел статистики сводный — показывает работу КАЖДОГО пользователя,
    поэтому по решению владельца он admin-only. Гейт обязан стоять на API, а не только
    во фронте: аудит 24.07 показал, что фронтовый гейт без серверного (брендинг,
    maintenance) означает доступ любым запросом в обход UI."""
    app = api.app
    monkeypatch.setattr(api.repository, "monthly_platform_stats", lambda months: [])
    monkeypatch.setattr(api.repository, "monthly_ai_cost", lambda months: [])
    seen = {}
    monkeypatch.setattr(
        api.repository, "monthly_user_activity",
        lambda months, user_id=None: seen.update({"user_id": user_id}) or [],
    )

    app.dependency_overrides[api.require_user] = lambda: {"id": 7, "email": "u@e.ru", "role": "user"}
    try:
        denied = TestClient(app).get("/api/stats/monthly")
    finally:
        app.dependency_overrides.clear()
    assert denied.status_code == 403, "обычный пользователь получил сводную статистику по всем"
    assert seen == {}, "запрос к данным вообще не должен был дойти до репозитория"

    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "a@e.ru", "role": "admin"}
    try:
        body = TestClient(app).get("/api/stats/monthly").json()
    finally:
        app.dependency_overrides.clear()
    assert seen["user_id"] is None, "админ должен видеть активность всех пользователей"
    assert body["activity_scope"] == "all"


def test_article_payload_strips_emoji_from_title_and_summary():
    """Пункт 6 Виктора: в показе сигнала эмодзи быть не должно — ни в названии, ни в сути.

    Чистим на выдаче, а не при вставке (решение владельца 12.09), поэтому проверяем
    именно сериализатор: через него идут лента, поиск и карточка статьи.
    Телеграм-заголовок лепится из первого предложения поста, поэтому «🔥» оказывается
    ПЕРВЫМИ символами articles.title и без чистки уезжает в интерфейс и в дайджест.
    """
    row = {
        "id": 1,
        "title": "🔥 Срочно! Роснефть запустила установку 🚀",
        "url": "https://example.com/a",
        "source_name": "Neftegaz.ru",
        "summary": "Команда 👍🏽 сообщила: добыча ↓ 3% при ±5 °C",
        "language": "ru",
    }
    payload = api._article_payload(row)
    assert payload["title"] == "Срочно! Роснефть запустила установку"
    # Стрелка, знак ± и градусы — законная отраслевая запись, их резать нельзя:
    # замер 12.09 показал, что наивная регулярка на \p{Emoji} била именно по ним.
    assert payload["summary"] == "Команда сообщила: добыча ↓ 3% при ±5 °C"


def test_article_payload_keeps_plain_text_untouched():
    """Чистка не должна трогать обычный текст — иначе она незаметно портит корпус."""
    row = {
        "id": 2,
        "title": "«Газпром нефть» ввела НПЗ мощностью 15 000 барр./сут",
        "url": "https://example.com/b",
        "source_name": "Интерфакс ТЭК",
        "summary": "Baker Hughes © 2026, ГОСТ™ и ® знак — проверено ✓",
        "language": "ru",
    }
    payload = api._article_payload(row)
    assert payload["title"] == row["title"]
    assert payload["summary"] == row["summary"]


def test_feed_search_covers_translated_title_and_tag(monkeypatch):
    """Поиск обязан находить то, что человек ВИДИТ на экране, и работать по тегу.

    Два расхождения, которые чинит эта правка:
    (1) на карточке показывается COALESCE(c.title_ru, a.title), а искали только по
        a.title — русский заголовок иностранной статьи не находился;
    (2) теги в поиск не входили вовсе, хотя в сборщике дайджеста такой поиск уже был.
    Требование владельца 12.09: теги влияют и на парсинг, и на выдачу.
    """
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 7, "email": "u@e.ru", "role": "user"}
    captured = _articles_sql(monkeypatch)
    try:
        TestClient(app).get("/api/articles", params={"search": "бурение"})
    finally:
        app.dependency_overrides.clear()

    sql = captured["sql"]
    assert "c.title_ru" in sql, "поиск обязан покрывать переведённый заголовок"
    assert "t.name" in sql and "parent.name" in sql, "поиск обязан покрывать тег и родителя"


def test_feedback_reasons_use_official_wording():
    """Формулировки заказчика 13.09: «придать более официальный статус платформы».

    Платформа выходит на корпоративный портал ГПН, разговорный тон там неуместен.
    Закрепляем тестом, чтобы правка не отъехала при следующем редактировании словаря.
    """
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "u@e.ru", "role": "user"}
    try:
        payload = TestClient(app).get("/api/feedback/reasons").json()
    finally:
        app.dependency_overrides.clear()

    labels = {row["value"]: row["label"] for row in payload}
    assert labels["off_topic"] == "Не соответствует тематике"
    assert labels["incomplete_text"] == "Неполный материал"
    assert labels["duplicate"] == "Повторный сигнал"
    assert labels["bad_translation"] == "Некорректный перевод"
    assert labels["bad_source"] == "Низкое качество источника"
    assert labels["good"] == "Ценный сигнал"


def test_external_worker_claim_hydrates_signal_discovery_payload(monkeypatch):
    from oiltech_digest import signal_discovery

    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    monkeypatch.setattr(api.repository, "requeue_expired_external_leases", lambda: 0)
    monkeypatch.setattr(
        signal_discovery,
        "build_external_payload",
        lambda payload: {"kind": "signal_discovery", "config": {"web_only": payload["web_only"]},
                         "snapshot": {"topics": [{"name": "Бурение"}]}},
    )
    monkeypatch.setattr(
        api.repository,
        "claim_external_background_job",
        lambda **kwargs: {
            "id": 13, "kind": "signal_discovery", "queue_name": "external-agents", "execution_region": "external",
            "capability": "openai", "status": "running", "progress": 10, "attempts": 1, "max_attempts": 1,
            "run_after": None, "payload_json": {"web_only": True}, "result_json": None, "error_message": None,
            "created_at": None, "started_at": None, "finished_at": None,
        },
    )
    client = TestClient(api.app)

    response = client.post(
        "/api/external-worker/claim",
        headers={"Authorization": "Bearer secret"},
        json={"worker_id": "nl-agents-1", "queues": ["external-agents"], "capabilities": ["openai"]},
    )

    assert response.status_code == 200
    assert response.json()["job"]["payload"] == {
        "kind": "signal_discovery",
        "config": {"web_only": True},
        "snapshot": {"topics": [{"name": "Бурение"}]},
    }


def test_external_worker_complete_applies_signal_discovery_and_keeps_only_summary(monkeypatch):
    from oiltech_digest import signal_discovery

    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    applied = []
    completed = []
    monkeypatch.setattr(api.repository, "get_background_job", lambda job_id: {"id": job_id, "kind": "signal_discovery"})
    monkeypatch.setattr(api.repository, "begin_external_background_job_finalize", lambda job_id, **kwargs: True)
    monkeypatch.setattr(
        signal_discovery,
        "apply_external_result",
        lambda result, **kwargs: applied.append((result, kwargs)) or {"signals": 2, "topics": []},
    )
    monkeypatch.setattr(
        api.repository,
        "finish_external_background_job",
        lambda job_id, **kwargs: completed.append((job_id, kwargs)) or True,
    )
    client = TestClient(api.app)
    worker_result = {"signal_discovery": True, "config": {"web_only": True},
                     "run": {"topics": [{"topic": "Бурение", "candidates": [{"signal": {"title": "x" * 5000}}]}]}}

    response = client.post(
        "/api/external-worker/jobs/10/complete",
        headers={"Authorization": "Bearer secret"},
        json={"lease_token": "lease", "result": worker_result},
    )

    assert response.status_code == 200
    assert applied == [(worker_result, {"job_id": 10})]
    # В задаче остаётся итог, а не мегабайты кандидатов.
    assert completed[0][1]["result"] == {"signal_discovery": True, "applied": {"signals": 2, "topics": []}}
def test_self_registration_closed_by_default():
    """Предусловие релиза #33: платформа выходит на корпоративный портал заказчика,
    и /api/auth/register позволял любому завести себе учётку."""
    client = TestClient(api.app)
    response = client.post(
        "/api/auth/register", json={"email": "stranger@example.com", "password": "12345678"}
    )
    assert response.status_code == 403
    assert "администратор" in response.json()["detail"]
