"""Сбой поиска радара виден админу.

23–27.09 Brave отвечал 402 (исчерпан месячный лимит) во всех 13 темах: сигналов 0,
а задача и прогон — ok, и пять дней этого никто не заметил. В итог темы шёл только
web_status, код ошибки терялся.

Теперь итог темы хранит первую ошибку поиска коротко, прогон — здоровье поиска, и это
одинаково на пути на месте и через воркер NL. Статус ЗАДАЧИ не меняется: его читает
правило «ежедневный прогон уже ставили в эти сутки». Здоровье отдаётся только админу."""

import json

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, background_jobs, config, network_policy, signal_discovery
from oiltech_digest.db import repository
from oiltech_digest.source_discovery import agent as source_agent

# Ответ Brave при исчерпанном месячном лимите. Собран по пробам 23.09 и 27.09 (402,
# USAGE_LIMIT_EXCEEDED, «Usage limit exceeded», current_spend 5.0 при usage_limit 5.0) и
# схеме ошибок Brave; дословное тело ответа в заметках не сохранилось.
BRAVE_402 = json.dumps(
    {
        "type": "ErrorResponse",
        "error": {
            "id": "0c2f5c1e-6b1d-4f7e-9a51-7d7b2d1c4402",
            "status": 402,
            "code": "USAGE_LIMIT_EXCEEDED",
            "detail": "Usage limit exceeded",
            "meta": {"plan": "Search", "current_spend": 5.0, "usage_limit": 5.0},
        },
        "time": 1758944100,
    },
    separators=(",", ":"),
)
BRAVE_EMPTY = json.dumps({"web": {"results": []}})
TOPICS = ["Бурение", "Экология", "Цифровизация"]
WORKER_HEADERS = {"Authorization": "Bearer worker-secret"}
ADMIN = {"id": 1, "email": "admin@example.com", "role": "admin"}
USER = {"id": 2, "email": "user@example.com", "role": "user"}


@pytest.mark.parametrize(
    ("web_search", "expected"),
    [
        ({"status": "error", "errors": [f"2026 Бурение news: HTTP 402 {BRAVE_402}"]}, "HTTP 402 Usage limit exceeded"),
        # agent._search_brave кладёт первые 200 знаков тела — JSON обрезан.
        ({"status": "error", "errors": [f"q: HTTP 402 {BRAVE_402[:200]}"]}, "HTTP 402 Usage limit exceeded"),
        # Обрыв посреди detail — остаётся код ошибки Brave.
        ({"status": "error", "errors": [f"q: HTTP 402 {BRAVE_402[:BRAVE_402.index('Usage') + 3]}"]},
         "HTTP 402 USAGE_LIMIT_EXCEEDED"),
        ({"status": "error", "errors": ["q: HTTP 503 Service Unavailable\n"]}, "HTTP 503 Service Unavailable"),
        ({"status": "error", "errors": ["q: HTTP 502 <html><head><title>502 Bad Gateway</title>"]}, "HTTP 502"),
        # Без кода HTTP — текст исключения, без запроса впереди (в запросе тоже бывает «: »).
        ({"status": "error", "queries": ["oil: drilling 2026"],
          "errors": ["oil: drilling 2026: Read timed out. (read timeout=20)"]}, "Read timed out. (read timeout=20)"),
        # Текст уходит на экран админа: ключ из адреса запроса (так его передаёт SerpAPI) — маской.
        ({"status": "error", "queries": ["q"],
          "errors": ["q: HTTPSConnectionPool(host='serpapi.com', port=443): Max retries exceeded with url: "
                     "/search.json?engine=google&q=q&api_key=SECRET123&num=10"]},
         "HTTPSConnectionPool(host='serpapi.com', port=443): Max retries exceeded with url: "
         "/search.json?engine=google&q=q&api_key=***&num=10"),
        # Поиск не запускался: ошибок нет, есть причина.
        ({"status": "missing_api_key", "reason": "BRAVE_SEARCH_API_KEY is empty"}, "BRAVE_SEARCH_API_KEY is empty"),
        ({"status": "ok", "errors": []}, None),
        ({"status": "empty", "errors": []}, None),
        (None, None),
    ],
)
def test_topic_summary_keeps_first_search_error_short(web_search, expected):
    assert signal_discovery._search_error_summary(web_search) == expected


class _BraveResponse:
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.text = body

    def json(self):
        return json.loads(self.text)


def _brave_answers(monkeypatch, answer) -> list[str]:
    """Настоящий agent._search_brave, подменён только HTTP: answer(запрос) -> (код, тело)."""
    asked: list[str] = []

    def get(url, headers=None, params=None, timeout=None):
        assert url == "https://api.search.brave.com/res/v1/web/search"
        asked.append(params["q"])
        return _BraveResponse(*answer(params["q"]))

    monkeypatch.setattr(source_agent.requests, "get", get)
    return asked


@pytest.fixture
def radar(isolated_db, monkeypatch):
    """Три корневые тематики заказчика = три темы радара; Brave включён; модель не зовётся."""
    with repository.get_connection() as conn:
        for order, name in enumerate(TOPICS, start=1):
            conn.execute("INSERT INTO tags (name, enabled, sort_order) VALUES (%s, TRUE, %s)", (name, order))
        conn.commit()
    monkeypatch.setattr(config, "SIGNAL_RADAR_TOPIC_SOURCE", "tags")
    monkeypatch.setattr(config, "SOURCE_DISCOVERY_SEARCH_PROVIDER", "brave")
    monkeypatch.setattr(config, "BRAVE_SEARCH_API_KEY", "test-key")
    monkeypatch.setattr(config, "SIGNAL_DISCOVERY_DAILY_ENABLED", True)
    monkeypatch.setattr(config, "BACKGROUND_JOB_INLINE", False)
    monkeypatch.setattr(config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("worker-secret"))
    monkeypatch.setattr(source_agent, "generate_search_queries", lambda topic, **kwargs: [])
    return isolated_db


def _route(monkeypatch, *, external: bool) -> None:
    decision = (
        network_policy.ExecutionDecision("external-ai", "external", "openai", "ai_external_enabled")
        if external else network_policy.ExecutionDecision("ai", "ru", "openai", "ai_local")
    )
    monkeypatch.setattr(network_policy, "route_ai_processing", lambda: decision)


def _daily_on_site(monkeypatch, *, force: bool = False) -> dict:
    """Ежедневный прогон, исполненный на ядре (CLI, локальная очередь)."""
    _route(monkeypatch, external=False)
    job_id = int(background_jobs.enqueue_daily_signal_discovery(force=force)["job"]["id"])
    background_jobs.run(job_id)
    return repository.get_background_job(job_id)


def _daily_through_worker(monkeypatch, *, force: bool = False) -> dict:
    """Ежедневный прогон через NL: выдача со снимком → прогон без базы → complete."""
    _route(monkeypatch, external=True)
    job_id = int(background_jobs.enqueue_daily_signal_discovery(force=force)["job"]["id"])
    # Часы базы в ВМ colima спешат и откатываются рывками (28.09: до 1,1 с назад за 20 с):
    # задача с run_after = now() на миг «из будущего», и выдача её не видит — тест плавал.
    with repository.get_connection() as conn:
        conn.execute("UPDATE background_jobs SET run_after = now() - interval '1 minute' WHERE id = %s", (job_id,))
        conn.commit()
    client = TestClient(api.app)
    claimed = client.post(
        "/api/external-worker/claim",
        headers=WORKER_HEADERS,
        json={"worker_id": "nl-agents-1", "queues": ["external-agents"], "capabilities": ["openai"]},
    ).json()["job"]
    with monkeypatch.context() as worker:
        def no_database(*args, **kwargs):
            raise RuntimeError("у воркера NL нет базы")

        for name in ("get_connection", "list_enabled_tags", "list_signal_agent_memory", "list_agent_memory"):
            worker.setattr(repository, name, no_database)
        result = signal_discovery.process_external_payload(json.loads(json.dumps(claimed["payload"])))
    completed = client.post(
        f"/api/external-worker/jobs/{claimed['id']}/complete",
        headers=WORKER_HEADERS,
        json={"lease_token": claimed["lease_token"], "result": json.loads(json.dumps(result))},
    )
    assert completed.status_code == 200
    return repository.get_background_job(int(claimed["id"]))


def _generation_run(job_id: int) -> dict:
    with repository.get_connection() as conn:
        row = conn.execute(
            "SELECT status, result_json, error_message FROM signal_generation_runs WHERE background_job_id = %s",
            (job_id,),
        ).fetchone()
    return {"status": row[0], "result": row[1], "error_message": row[2]}


def test_brave_402_gives_the_same_topic_summary_and_run_on_both_paths(radar, monkeypatch):
    asked = _brave_answers(monkeypatch, lambda query: (402, BRAVE_402))

    on_site = _daily_on_site(monkeypatch)
    through_worker = _daily_through_worker(monkeypatch, force=True)

    assert asked  # поиск на обоих путях — настоящий _search_brave
    on_site_topics = on_site["result_json"]["topic_results"]
    worker_topics = through_worker["result_json"]["applied"]["topics"]
    assert [row["web_error"] for row in on_site_topics] == ["HTTP 402 Usage limit exceeded"] * 3
    assert [(row["topic"], row["web_error"]) for row in worker_topics] == [
        (row["topic"], row["web_error"]) for row in on_site_topics
    ]
    assert [row["web_status"] for row in worker_topics] == ["error"] * 3

    run = _generation_run(int(on_site["id"]))
    assert run == _generation_run(int(through_worker["id"]))
    assert run["status"] == "failed"
    assert run["result"]["search_health"] == {
        "topics": 3, "failed": 3, "first_error": "HTTP 402 Usage limit exceeded",
        "http_status": 402, "provider": "brave", "cause": "http",
    }
    assert run["result"]["signals"] == 0
    assert run["error_message"] == "Поиск не ответил во всех темах прогона (3 из 3): HTTP 402 Usage limit exceeded"


@pytest.mark.parametrize("path", ["on_site", "through_worker"])
def test_search_failure_keeps_job_ok_and_the_daily_rule(radar, monkeypatch, path):
    """Прогон failed, а задача ok: правило «уже ставили в эти сутки» её видит — радар не
    переставляется каждые полчаса (134 запуска за 5 дней в сентябре)."""
    _brave_answers(monkeypatch, lambda query: (402, BRAVE_402))
    enqueued: list = []

    job = (_daily_on_site if path == "on_site" else _daily_through_worker)(monkeypatch)

    assert job["status"] == "ok"
    assert _generation_run(int(job["id"]))["status"] == "failed"
    monkeypatch.setattr(background_jobs, "enqueue", lambda *args, **kwargs: enqueued.append(args))
    assert background_jobs.enqueue_daily_signal_discovery()["reason"] == "already_scheduled"
    assert enqueued == []


def test_partial_search_failure_keeps_run_ok_with_health(radar, monkeypatch):
    _brave_answers(monkeypatch, lambda query: (200, BRAVE_EMPTY) if "Цифровизация" in query else (402, BRAVE_402))

    job = _daily_through_worker(monkeypatch)

    run = _generation_run(int(job["id"]))
    assert run["status"] == "ok"
    assert run["error_message"] is None
    assert run["result"]["search_health"] == {
        "topics": 3, "failed": 2, "first_error": "HTTP 402 Usage limit exceeded",
        "http_status": 402, "provider": "brave", "cause": "http",
    }
    # Тема, где поиск ответил пусто, — не сбой.
    assert [row["web_error"] for row in job["result_json"]["applied"]["topics"]] == [
        "HTTP 402 Usage limit exceeded", "HTTP 402 Usage limit exceeded", None,
    ]


# Настоящие тексты исключений requests 2.32 / urllib3 2.7 (сняты 28.09 локально: сервер без
# ответа, закрытый порт, имя .invalid) — такими их кладёт в errors agent._search_brave.
READ_TIMEOUT = "HTTPConnectionPool(host='127.0.0.1', port=51630): Read timed out. (read timeout=0.5)"
REFUSED = (
    "HTTPConnectionPool(host='127.0.0.1', port=9): Max retries exceeded with url: /res/v1/web/search?q=x "
    "(Caused by NewConnectionError(\"HTTPConnection(host='127.0.0.1', port=9): Failed to establish a new "
    "connection: [Errno 61] Connection refused\"))"
)
NO_DNS = (
    "HTTPConnectionPool(host='api.search.brave.invalid', port=80): Max retries exceeded with url: "
    "/res/v1/web/search?q=x (Caused by NameResolutionError(\"HTTPConnection(host='api.search.brave.invalid', "
    "port=80): Failed to resolve 'api.search.brave.invalid' ([Errno 8] nodename nor servname provided, or not known)\"))"
)

TLS_FAILURE = (
    "HTTPSConnectionPool(host='api.search.brave.com', port=443): Max retries exceeded with url: "
    "/res/v1/web/search?q=x (Caused by SSLError(SSLError(1, '[SSL: WRONG_VERSION_NUMBER] wrong version number "
    "(_ssl.c:1000)')))"
)


@pytest.mark.parametrize(
    ("web_search", "cause"),
    [
        ({"status": "error", "provider": "brave", "errors": [f"q: HTTP 402 {BRAVE_402}"]}, "http"),
        ({"status": "missing_api_key", "provider": "brave", "reason": "BRAVE_SEARCH_API_KEY is empty"}, "not_configured"),
        ({"status": "not_configured", "provider": "none", "reason": "search provider is not connected yet"},
         "not_configured"),
        ({"status": "unsupported_provider", "provider": "brvae", "reason": "unsupported SOURCE_DISCOVERY_SEARCH_PROVIDER=brvae"},
         "unsupported_provider"),
        # Таймаут и обрыв соединения: слова причины — за 160 знаками first_error, смотрим сырую строку.
        ({"status": "error", "provider": "brave", "errors": [f"q: {READ_TIMEOUT}"]}, "network"),
        ({"status": "error", "provider": "brave", "errors": [f"q: {REFUSED}"]}, "network"),
        ({"status": "error", "provider": "brave", "errors": [f"q: {NO_DNS}"]}, "network"),
        # 504 с «Timeout» в теле — это ответ сервиса, а не сеть.
        ({"status": "error", "provider": "brave", "errors": ["q: HTTP 504 Gateway Timeout"]}, "http"),
        ({"status": "error", "provider": "brave", "errors": ["q: Expecting value: line 1 column 1 (char 0)"]}, "other"),
        # TLS в общей обёртке urllib3 «Max retries exceeded» — не «не ответил вовремя» (проверка 28.09).
        ({"status": "error", "provider": "brave", "errors": [f"q: {TLS_FAILURE}"]}, "connection"),
        # Слова самого запроса не решают причину: «connection broken» здесь — только в запросе.
        ({"status": "error", "provider": "brave", "queries": ["2026 premium connection broken"],
          "errors": ["2026 premium connection broken: Expecting value: line 1 column 1 (char 0)"]}, "other"),
    ],
)
def test_run_health_names_the_cause_of_the_first_failure(web_search, cause):
    rows = [
        {"topic": "Бурение", "web_search": {"status": "ok", "provider": "brave"}, "web_error": None},
        {"topic": "Экология", "web_search": web_search, "web_error": signal_discovery._search_error_summary(web_search)},
    ]

    health = signal_discovery._search_health(rows)

    assert (health["failed"], health["cause"]) == (1, cause)


def test_run_health_has_no_cause_when_search_answered():
    rows = [{"topic": "Бурение", "web_search": {"status": "empty", "provider": "brave"}, "web_error": None}]

    assert signal_discovery._search_health(rows)["cause"] is None


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("no_key", {"cause": "not_configured", "http_status": None, "first_error": "BRAVE_SEARCH_API_KEY is empty"}),
        ("timeout", {"cause": "network", "http_status": None, "first_error": READ_TIMEOUT}),
        ("rate_limit", {"cause": "http", "http_status": 429, "first_error": "HTTP 429 Request rate limit exceeded for plan"}),
        ("unavailable", {"cause": "http", "http_status": 503, "first_error": "HTTP 503 Service Unavailable"}),
    ],
)
def test_real_brave_failures_reach_the_run_health(radar, monkeypatch, setup, expected):
    import requests

    if setup == "no_key":
        monkeypatch.setattr(config, "BRAVE_SEARCH_API_KEY", "")
    elif setup == "timeout":
        def timeout(query):
            raise requests.exceptions.ReadTimeout(READ_TIMEOUT)

        _brave_answers(monkeypatch, timeout)
    elif setup == "rate_limit":
        body = {"type": "ErrorResponse", "error": {"status": 429, "code": "RATE_LIMITED",
                                                   "detail": "Request rate limit exceeded for plan."}}
        _brave_answers(monkeypatch, lambda query: (429, json.dumps(body)))
    else:
        _brave_answers(monkeypatch, lambda query: (503, "Service Unavailable"))

    job = _daily_through_worker(monkeypatch)

    health = _generation_run(int(job["id"]))["result"]["search_health"]
    assert {key: health[key] for key in expected} == expected
    assert (health["topics"], health["failed"]) == (3, 3)


def _get(path: str, user: dict):
    api.app.dependency_overrides[api.require_user] = lambda: user
    try:
        return TestClient(api.app).get(path)
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)


def test_search_health_goes_to_admin_only(radar, monkeypatch):
    _brave_answers(monkeypatch, lambda query: (402, BRAVE_402))
    job = _daily_through_worker(monkeypatch)
    repository.upsert_signal({"signal_key": "k-old", "title": "Прошлый сигнал", "theme": "Бурение", "score": 60})

    admin = _get("/api/signals/search-health", ADMIN)

    assert admin.status_code == 200
    health = admin.json()["search_health"]
    assert health["run_at"] == job["started_at"].isoformat()
    assert {key: health[key] for key in ("status", "signals", "topics", "failed", "first_error", "http_status",
                                         "provider", "cause", "job_id")} == {
        "status": "failed", "signals": 0, "topics": 3, "failed": 3, "first_error": "HTTP 402 Usage limit exceeded",
        "http_status": 402, "provider": "brave", "cause": "http", "job_id": job["id"],
    }

    read: list = []
    monkeypatch.setattr(repository, "latest_signal_generation_run", lambda **kwargs: read.append(kwargs))
    denied = _get("/api/signals/search-health", USER)
    assert denied.status_code == 403
    assert read == []  # до базы запрос не дошёл
    # Лента радара обычного пользователя — прежняя: список карточек, здоровья поиска в ней нет.
    signals = _get("/api/signals", USER).json()
    assert [row["title"] for row in signals] == ["Прошлый сигнал"]
    assert not any("search_health" in row or "web_error" in row for row in signals)


def _job_with_run(*, daily: bool, run_status: str, health: dict | None, minutes_ago: int) -> int:
    payload = background_jobs.daily_signal_discovery_payload() if daily else {"web_only": True, "topic": "Бурение"}
    result = {"topics": 3, "signals": 1, "returned_signals": 1}
    if health is not None:
        result["search_health"] = health
    with repository.get_connection() as conn:
        job_id = conn.execute(
            """
            INSERT INTO background_jobs (kind, queue_name, status, payload_json, max_attempts, created_at)
            VALUES ('signal_discovery', 'external-agents', 'ok', %s::jsonb, 1, now() - make_interval(mins => %s))
            RETURNING id
            """,
            (json.dumps(payload), minutes_ago),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO signal_generation_runs (background_job_id, trigger, status, result_json)
            VALUES (%s, 'signal_discovery_external', %s, %s::jsonb)
            """,
            (job_id, run_status, json.dumps(result)),
        )
        conn.commit()
    return job_id


def test_search_health_is_of_the_latest_finished_daily_run(radar):
    failed = {"topics": 3, "failed": 3, "first_error": "HTTP 402 Usage limit exceeded", "http_status": 402,
              "provider": "brave"}
    healthy = {"topics": 3, "failed": 0, "first_error": None, "http_status": None, "provider": None}
    _job_with_run(daily=True, run_status="failed", health=failed, minutes_ago=3 * 24 * 60)
    latest_daily = _job_with_run(daily=True, run_status="ok", health=healthy, minutes_ago=24 * 60)
    _job_with_run(daily=False, run_status="failed", health=failed, minutes_ago=60)  # ручной прогон — не ежедневный
    _job_with_run(daily=True, run_status="running", health=None, minutes_ago=5)  # ещё идёт

    health = _get("/api/signals/search-health", ADMIN).json()["search_health"]

    # После успешного прогона сбоя нет — экран плашку не покажет.
    assert (health["job_id"], health["failed"], health["topics"]) == (latest_daily, 0, 3)


def test_search_health_is_empty_without_daily_runs(radar):
    assert _get("/api/signals/search-health", ADMIN).json() == {"search_health": None}
