"""Воркер полос (21.09): аренда продлевается фоном, зависание лечится перезапуском,
полоса может держать несколько потоков, каждая очередь в раскладке NL имеет воркера.

Каждый тест падает на коде до правки."""

import threading
import time
from pathlib import Path

import pytest
import requests
import yaml

from oiltech_digest import external_worker, lanes
from oiltech_digest.processing import external_ai

JOB = {"id": 7, "kind": "process_articles", "lease_token": "t", "payload": {}}


def _http_409() -> requests.HTTPError:
    response = requests.Response()
    response.status_code = 409
    return requests.HTTPError("409", response=response)


class _Client:
    def __init__(self, *, heartbeat_error: Exception | None = None, forked: "_Client | None" = None):
        self.heartbeats = 0
        self.completed: list = []
        self.failed: list = []
        self.heartbeat_error = heartbeat_error
        self.forked = forked
        self.worker_id = "nl-test"

    def fork(self):
        return self.forked or self

    def heartbeat(self, job):
        self.heartbeats += 1
        if self.heartbeat_error:
            raise self.heartbeat_error

    def progress(self, job, progress):
        pass

    def complete(self, job, result):
        self.completed.append(result)

    def fail(self, job, error, *, retryable=True, retry_after_seconds=300):
        self.failed.append((error, retryable))


def _wait_until(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_lease_is_extended_even_when_handler_never_calls_heartbeat(monkeypatch):
    """Класс 24.07 / 17.09 / 21.09: новый код забывал beat() — аренда уходила."""
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(external_ai, "process_payload", lambda payload, heartbeat=None: time.sleep(0.2) or {"ok": 1})
    keeper_client = _Client()
    client = _Client(forked=keeper_client)

    external_worker._handle_job(client, dict(JOB))

    assert keeper_client.heartbeats >= 5
    assert client.completed == [{"ok": 1}]


def test_revoked_lease_stops_paid_work_at_next_step(monkeypatch):
    """Ядро отозвало аренду (409) — фон отмечает, и следующий шаг обработчика прерывается:
    за выброшенный результат не платим (24.07: ~$11/ч в петле)."""
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_HEARTBEAT_SECONDS", 0.01)
    steps = []

    def handler(payload, heartbeat=None):
        for _ in range(200):
            heartbeat()
            steps.append(1)
            time.sleep(0.01)
        return {"ok": 1}

    monkeypatch.setattr(external_ai, "process_payload", handler)
    monkeypatch.setattr(external_worker, "_safe_heartbeat", lambda client, job: None)  # сам шаг сеть не трогает
    client = _Client(forked=_Client(heartbeat_error=_http_409()))

    external_worker._handle_job(client, dict(JOB))

    assert 0 < len(steps) < 200
    assert client.completed == [] and client.failed == []


def test_hung_job_goes_back_to_queue_and_process_restarts(monkeypatch):
    monkeypatch.setattr(external_worker, "_DRAINING", threading.Event())
    restarted = threading.Event()
    client = _Client()

    keeper = external_worker.LeaseKeeper(client, dict(JOB), interval=0.01, stall_seconds=0.05,
                                         drain_seconds=0.1, on_deadline=restarted.set)
    keeper.start()

    assert restarted.wait(2.0)
    assert client.failed and client.failed[0][1] is True  # retryable: задача вернётся в очередь
    assert external_worker._DRAINING.is_set()  # новых задач процесс уже не берёт
    keeper.stop()


def test_long_batch_with_steady_progress_is_not_killed(monkeypatch):
    """Ревью 21.09: потолок на ОБЩЕЕ время трижды выбросил бы оплаченную пачку в 500
    статей. Пока обработчик продвигается, аренда продлевается сколько угодно."""
    monkeypatch.setattr(external_worker, "_DRAINING", threading.Event())
    restarted = threading.Event()
    client = _Client()
    keeper = external_worker.LeaseKeeper(client, dict(JOB), interval=0.01, stall_seconds=0.1,
                                         on_deadline=restarted.set).start()

    for _ in range(30):  # 0,6 с работы — в шесть раз дольше предела без продвижения
        keeper.touch()
        time.sleep(0.02)
    keeper.stop()

    assert not restarted.is_set()
    assert client.failed == []


def test_restart_waits_for_sibling_threads_to_finish(monkeypatch):
    """os._exit посреди соседних задач рвал их здоровую работу (ревью 21.09)."""
    monkeypatch.setattr(external_worker, "_DRAINING", threading.Event())
    monkeypatch.setattr(external_worker, "_INFLIGHT", 2)  # зависшая + соседняя
    restarted_at = []
    keeper = external_worker.LeaseKeeper(_Client(), dict(JOB), interval=0.01, stall_seconds=0.02,
                                         drain_seconds=5, on_deadline=lambda: restarted_at.append(time.monotonic()))
    sibling_done = time.monotonic() + 0.3
    keeper.start()
    time.sleep(0.3)
    external_worker._inflight(-1)  # соседка закончила

    assert _wait_until(lambda: restarted_at)
    assert restarted_at[0] >= sibling_done - 0.05


def test_stall_limits_are_per_kind():
    assert external_worker.job_stall_seconds("process_articles") >= 10 * 60  # шаг — одна статья
    assert external_worker.job_stall_seconds("scrape_source") >= 5 * 90  # шаг — одна страница
    assert external_worker.job_stall_seconds("unknown_kind") == external_worker.config.EXTERNAL_JOB_MAX_SECONDS


def test_three_fetch_threads_keep_customer_keywords_until_last_job_ends():
    """Ревью 21.09: задача, закончившая первой, возвращала None, пока соседняя шла, —
    и та фильтровала без ключей заказчика."""
    from oiltech_digest.ingestion import relevance_filter

    # Порядок строго по событиям, без таймаутов: A входит, B входит, A выходит, B смотрит.
    a_inside, b_inside, a_done = threading.Event(), threading.Event(), threading.Event()
    seen = []

    def job_a():
        with relevance_filter.use_tag_keywords(["бурение"], []):
            a_inside.set()
            b_inside.wait(2)
        a_done.set()

    def job_b():
        a_inside.wait(2)
        with relevance_filter.use_tag_keywords(["бурение"], []):
            b_inside.set()
            a_done.wait(2)  # A уже вышла
            seen.append(relevance_filter.tag_keywords())

    threads = [threading.Thread(target=job_a), threading.Thread(target=job_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)

    assert seen and seen[0][0] == ("бурение",)
    assert relevance_filter._TAG_KEYWORDS_OVERRIDE is None  # после последней — как было


def test_lane_runs_several_claim_threads_with_distinct_names(monkeypatch):
    seen = []
    lock = threading.Lock()

    def fake_loop(client, sleep_seconds, *, once=False):
        with lock:
            seen.append(client.worker_id)

    monkeypatch.setattr(external_worker, "_claim_loop", fake_loop)

    external_worker.run_loop(core_api_url="https://core.example", token="t", worker_id="nl-fetch-1",
                             queues=["external-fetch"], capabilities=["http_fetch"], concurrency=3)

    assert sorted(seen) == ["nl-fetch-1#1", "nl-fetch-1#2", "nl-fetch-1#3"]


def test_claim_loop_survives_core_outage(monkeypatch):
    """Раньше сбой выдачи ронял процесс; в полосе из трёх потоков так тихо умирал бы поток."""

    class Stop(BaseException):
        pass

    answers = [requests.ConnectionError("core restarting"), {"id": 1, "kind": "scrape_source"}, Stop()]
    handled = []

    class Client:
        worker_id = "nl-fetch-1#1"

        def claim(self):
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

    monkeypatch.setattr(external_worker.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(external_worker, "_handle_job", lambda client, job: handled.append(job["id"]))

    with pytest.raises(Stop):
        external_worker._claim_loop(Client(), 0.0)

    assert handled == [1]


def test_every_lane_has_its_own_nl_worker():
    """Раскладка NL: у каждой внешней очереди есть воркер, полосы не делят контейнер
    (иначе пересчёт снова встанет перед потоком дня, а браузер — перед RSS)."""
    compose = Path(__file__).resolve().parents[1] / "docker-compose.external-worker.yml"
    services = yaml.safe_load(compose.read_text())["services"]
    served: dict[str, list[str]] = {}
    for name, service in services.items():
        env = service["environment"]
        queues = [item.strip() for item in env["EXTERNAL_WORKER_QUEUES"].split(",")]
        assert len(queues) == 1, f"{name} слушает несколько полос: {queues}"
        served.setdefault(queues[0], []).append(name)

    assert set(served) == set(lanes.EXTERNAL_LANES)
    assert all(len(names) == 1 for names in served.values())
    by_queue = {queue: services[names[0]]["environment"] for queue, names in served.items()}
    assert by_queue[lanes.FETCH].get("EXTERNAL_WORKER_CONCURRENCY") == "3"
    assert by_queue[lanes.BROWSER].get("EXTERNAL_WORKER_CONCURRENCY", "1") == "1"
