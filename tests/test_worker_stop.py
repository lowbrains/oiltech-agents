"""Мягкая остановка воркера (сессия C, п. 1): SIGTERM при выкате NL.

До правки обработчика SIGTERM не было: пересборка NL обрывала задачи в работе, они
висели до конца аренды (600 с), а оплаченная часть ИИ-пакета выбрасывалась и
оплачивалась заново. Теперь задачам даётся срок закончить, остальные возвращаются
ядру сразу (release) — с тем, что успели сделать.

Каждый тест падает на коде до правки."""

import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from oiltech_digest import api, external_worker, worker_shutdown
from oiltech_digest.db import connection, repository
from oiltech_digest.processing import external_ai
from oiltech_digest.processing.openai_client import OfflineAIClient

AUTH = {"Authorization": "Bearer secret"}
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_stop_state(monkeypatch):
    monkeypatch.setattr(worker_shutdown, "SHUTDOWN", worker_shutdown.Shutdown())
    monkeypatch.setattr(external_worker, "_DRAINING", threading.Event())


def _stop(reason: str = "test") -> None:
    worker_shutdown.SHUTDOWN.request(reason)


# --- Воркер: остановка без базы ------------------------------------------------------------


class _Client:
    """Воркер глазами ядра: что он прислал и в каком порядке."""

    def __init__(self, jobs=None, *, on_claim=None):
        self.worker_id = "nl-test"
        self.jobs = list(jobs or [])
        self.on_claim = on_claim
        self.claims = 0
        self.calls: list[tuple] = []
        self.lock = threading.Lock()

    def fork(self):
        return self

    def claim(self):
        self.claims += 1
        if self.on_claim:
            self.on_claim()
        return self.jobs.pop(0) if self.jobs else None

    def heartbeat(self, job):
        pass

    def progress(self, job, progress):
        pass

    def complete(self, job, result):
        with self.lock:
            self.calls.append(("complete", job["id"], result))

    def fail(self, job, error, *, retryable=True, retry_after_seconds=300):
        with self.lock:
            self.calls.append(("fail", job["id"], error))

    def release(self, job, *, reason, result=None):
        with self.lock:
            self.calls.append(("release", job["id"], result))

    def kinds(self):
        return [call[0] for call in self.calls]


def _job(job_id=7, kind="process_articles"):
    return {"id": job_id, "kind": kind, "lease_token": "t", "payload": {}}


def _stepping_handler(steps: list, *, step_seconds=0.02, total=200):
    """Обработчик по договору циклов ИИ: heartbeat перед каждой статьёй, на остановке —
    сделанное с пометкой partial."""

    def handler(payload, heartbeat=None):
        done = []
        for index in range(total):
            try:
                heartbeat()
            except external_ai.StopRequested:
                return {"partial": True, "articles": done}
            done.append({"article_id": index})
            steps.append(index)
            time.sleep(step_seconds)
        return {"articles": done}

    return handler


def test_stop_request_ends_claiming_without_new_jobs(monkeypatch):
    client = _Client(on_claim=lambda: _stop())

    external_worker._claim_loop(client, 0.0)

    assert client.claims == 1  # после остановки выдачу больше не просим


def test_job_handed_out_at_the_moment_of_stop_goes_back_untouched(monkeypatch):
    started = []
    monkeypatch.setattr(external_ai, "process_payload", lambda payload, heartbeat=None: started.append(1) or {})
    client = _Client([_job(11)], on_claim=lambda: _stop())

    external_worker._claim_loop(client, 0.0)

    assert started == []
    assert client.kinds() == ["release"] and client.calls[0][2] is None


def test_job_finishing_within_grace_completes_normally(monkeypatch):
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_STOP_GRACE_SECONDS", 5.0)
    steps: list = []
    monkeypatch.setattr(external_ai, "process_payload", _stepping_handler(steps, total=10))
    client = _Client()
    worker = threading.Thread(target=external_worker._handle_job, args=(client, _job()))

    worker.start()
    time.sleep(0.05)
    _stop()
    worker.join(5)

    assert len(steps) == 10
    assert client.kinds() == ["complete"]


def test_after_grace_batch_stops_at_next_step_and_hands_back_done_part(monkeypatch):
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_STOP_GRACE_SECONDS", 0.05)
    steps: list = []
    monkeypatch.setattr(external_ai, "process_payload", _stepping_handler(steps))
    client = _Client()
    worker = threading.Thread(target=external_worker._handle_job, args=(client, _job()))

    worker.start()
    time.sleep(0.1)
    _stop()
    worker.join(5)

    assert 0 < len(steps) < 200
    assert client.kinds() == ["release"]
    released = client.calls[0][2]
    assert released["partial"] is True
    assert [item["article_id"] for item in released["articles"]] == steps  # всё сделанное — ядру


def test_handler_that_ignores_stop_still_returns_job_without_result(monkeypatch):
    """Сбор и документ частичный итог не отдают: остановка обрывает их, задача — в очередь целиком."""
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_STOP_GRACE_SECONDS", 0.0)

    def fetch_handler(payload, heartbeat=None):
        for _ in range(100):
            heartbeat()  # как в сборе: без try — остановка уходит наружу
            time.sleep(0.01)
        return {"articles": []}

    monkeypatch.setattr(external_worker.external_fetch, "process_payload", fetch_handler)
    client = _Client()
    _stop()

    external_worker._handle_job(client, _job(kind="scrape_source"))

    assert client.kinds() == ["release"] and client.calls[0][2] is None


def test_supervisor_returns_job_whose_step_never_ends(monkeypatch):
    """Шаг висит в вызове модели и до границы не доходит — процесс возвращает задачу сам,
    иначе она ждала бы конца аренды, а Docker убил бы процесс по stop_grace_period."""
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_STOP_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_STOP_STEP_SECONDS", 0.1)
    unblock = threading.Event()
    monkeypatch.setattr(external_ai, "process_payload", lambda payload, heartbeat=None: unblock.wait(5) or {})
    client = _Client()
    worker = threading.Thread(target=external_worker._handle_job, args=(client, _job(21)), daemon=True)
    worker.start()
    time.sleep(0.05)

    _stop()
    started = time.monotonic()
    worker_shutdown.supervise([worker], external_worker._release)
    elapsed = time.monotonic() - started
    unblock.set()
    worker.join(2)

    assert ("release", 21, None) in client.calls
    assert elapsed < 2.0


def test_supervisor_hands_back_what_was_done_before_the_hung_step(monkeypatch):
    """Ревью 23.09: шаг ИИ-пакета — целая статья, до пяти вызовов модели. Завис он дольше
    срока — раньше главный поток возвращал задачу пустой, и 19 из 50 оплаченных статей
    оплачивались второй раз. Теперь уходит снимок сделанного на последней границе шага."""
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_STOP_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_STOP_STEP_SECONDS", 0.1)
    unblock = threading.Event()
    in_step = threading.Event()

    def handler(payload, heartbeat=None):
        result = {"external_ai": True, "articles": []}
        for index in range(3):
            heartbeat(result)
            result["articles"].append({"article_id": index})
        heartbeat(result)  # граница четвёртой статьи, дальше — вызов модели, который висит
        in_step.set()
        unblock.wait(5)
        result["articles"].append({"article_id": 3})
        return result

    monkeypatch.setattr(external_ai, "process_payload", handler)
    client = _Client()
    worker = threading.Thread(target=external_worker._handle_job, args=(client, _job(31)), daemon=True)
    worker.start()
    assert in_step.wait(2)

    _stop()
    worker_shutdown.supervise([worker], external_worker._release)
    unblock.set()
    worker.join(2)

    releases = [call for call in client.calls if call[0] == "release"]
    assert len(releases) == 1 and releases[0][1] == 31
    handed = releases[0][2]
    assert handed["partial"] is True
    assert [item["article_id"] for item in handed["articles"]] == [0, 1, 2]
    assert client.kinds() == ["release"]  # поток обработчика, дойдя до конца, второй раз не отчитался


def test_ready_result_is_not_handed_back_empty_while_progress_is_sent(monkeypatch):
    """Ревью 23.09: progress(90) шёл, пока задача числилась «в работе», — главный поток мог
    вернуть её пустой, и готовый полный итог выбрасывался."""
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_STOP_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_STOP_STEP_SECONDS", 0.05)
    progressing = threading.Event()

    class SlowProgress(_Client):
        def progress(self, job, progress):
            if progress == 90:
                progressing.set()
                time.sleep(0.3)  # дольше срока остановки

    monkeypatch.setattr(external_ai, "process_payload", lambda payload, heartbeat=None: {"articles": [{"article_id": 1}]})
    client = SlowProgress()
    worker = threading.Thread(target=external_worker._handle_job, args=(client, _job(41)), daemon=True)
    worker.start()
    assert progressing.wait(2)

    _stop()
    worker_shutdown.supervise([worker], external_worker._release)
    worker.join(2)

    assert client.kinds() == ["complete"]


def test_failed_progress_mark_does_not_throw_away_a_paid_result(monkeypatch):
    """Повторное ревью 23.09: сбой отметки progress(90) превращал готовый итог в fail."""

    class FlakyProgress(_Client):
        def progress(self, job, progress):
            if progress == 90:
                raise ConnectionError("ядро моргнуло")

    monkeypatch.setattr(external_ai, "process_payload", lambda payload, heartbeat=None: {"articles": [{"article_id": 1}]})
    client = FlakyProgress()

    external_worker._handle_job(client, _job(51))

    assert client.kinds() == ["complete"]


def test_sigterm_reaches_worker_loop_and_restores_previous_handler(monkeypatch):
    """Настоящий сигнал, а не вызов функции: так останавливает контейнер Docker."""

    class Fake(_Client):
        def __init__(self, **kwargs):
            super().__init__()
            self.worker_id = kwargs["worker_id"]

        def claim(self):
            self.claims += 1
            if self.claims > 500:  # страховка: без обработчика цикл не кончился бы никогда
                raise KeyboardInterrupt
            return None

    monkeypatch.setattr(external_worker, "ExternalWorkerClient", Fake)
    fired = []
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: fired.append(signum))
    try:
        timer = threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM))
        timer.start()
        started = time.monotonic()
        external_worker.run_loop(core_api_url="https://core.example", token="t", worker_id="nl-ai-1",
                                 queues=["external-ai"], capabilities=["openai"], poll_seconds=0.01)
        elapsed = time.monotonic() - started
        timer.join()
        assert worker_shutdown.SHUTDOWN.requested.is_set()
        assert fired == []  # сигнал принял воркер, а не прежний обработчик
        assert elapsed < 3.0
        assert signal.getsignal(signal.SIGTERM) is not worker_shutdown.on_signal
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_compose_gives_workers_time_to_hand_jobs_back():
    """stop_grace_period — под срок остановки: иначе Docker убьёт процесс до release,
    и задача снова будет ждать конца аренды."""
    compose = Path(__file__).resolve().parents[1] / "docker-compose.external-worker.yml"
    services = yaml.safe_load(compose.read_text())["services"]
    need = (external_worker.config.EXTERNAL_WORKER_STOP_GRACE_SECONDS
            + external_worker.config.EXTERNAL_WORKER_STOP_STEP_SECONDS
            + worker_shutdown.REPORT_SECONDS + 5)

    for name, service in services.items():
        grace = str(service.get("stop_grace_period") or "10s")
        assert grace.endswith("s"), name
        assert float(grace[:-1]) >= need, f"{name}: stop_grace_period {grace} < {need} с"


# --- Ядро: приём возвращённой задачи -------------------------------------------------------


class _CountingAI(OfflineAIClient):
    """Офлайн-модель со счётчиком: по заголовку видно, за какую статью платили бы."""

    def __init__(self, calls: list):
        self.calls = calls

    def complete_json(self, instructions, user_input, schema, **kwargs):
        if schema["name"] == "article_relevance":
            self.calls.append(next(line for line in user_input.splitlines() if "Статья" in line))
        return super().complete_json(instructions, user_input, schema, **kwargs)


def _seed(count: int) -> list[int]:
    with connection.get_connection() as conn:
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('Stop Source', 'News', 'https://stop.example', TRUE, 'request') RETURNING id"
        ).fetchone()[0]
        conn.execute("INSERT INTO tags (name, enabled, sort_order) VALUES ('Бурение', TRUE, 1)")
        conn.execute("INSERT INTO scoring_criteria (name, weight, enabled, sort_order) VALUES ('Значимость', 100, TRUE, 1)")
        conn.commit()
    for index in range(count):
        repository.insert_article({
            "source_id": source_id, "title": f"Статья {index}", "url": f"https://stop.example/{index}",
            "published_at": NOW - timedelta(hours=index), "raw_text": f"Статья {index}: бурение и контракт. " * 12,
            "text_truncated": False, "language": "ru", "content_hash": f"stop-{index}",
        })
    with connection.get_connection() as conn:
        ids = [int(row[0]) for row in conn.execute("SELECT id FROM articles ORDER BY published_at DESC").fetchall()]
    assert len(ids) == count  # иначе проверка прошла бы вхолостую на одной статье
    return ids


def _core(monkeypatch) -> TestClient:
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    return TestClient(api.app)


def _claim(core: TestClient) -> dict:
    # Повтор до 2 с: часы ВМ colima спешат и раз в минуту подводятся назад (~200 мс), и
    # только что поставленная задача на этот миг «из будущего» (run_after > now() базы).
    # Поймано 23.09: 2 прогона из 25, оба ровно через 60 с друг от друга. Продукт это не
    # задевает — на проде часы подтягиваются плавно, худшее — задача готова на миг позже.
    deadline = time.monotonic() + 2.0
    while True:
        response = core.post("/api/external-worker/claim", headers=AUTH,
                             json={"worker_id": "nl-ai-1", "queues": ["external-ai"], "capabilities": ["openai"]})
        assert response.status_code == 200
        job = response.json()["job"]
        if job is not None:
            return job
        if time.monotonic() > deadline:
            break
        time.sleep(0.05)
    with connection.get_connection() as conn:
        state = conn.execute("SELECT id, status, run_after, now(), run_after <= now() FROM background_jobs").fetchall()
    raise AssertionError(f"ядро не выдало задачу; задачи (id, статус, run_after, now, готова): {state}")


def _release(core: TestClient, job: dict, result=None):
    return core.post(f"/api/external-worker/jobs/{job['id']}/release", headers=AUTH,
                     json={"lease_token": job["lease_token"], "reason": "выкат NL", "result": result})


def _stop_before(article_number: int):
    """heartbeat, который останавливает пакет перед статьёй с этим номером (с 1)."""
    beats = {"count": 0}

    def heartbeat(done=None):
        beats["count"] += 1
        if beats["count"] >= article_number:
            raise external_ai.StopRequested("остановка воркера")

    return heartbeat


def test_release_puts_job_back_at_once_without_spending_attempt(isolated_db, monkeypatch):
    _seed(3)
    created = repository.create_background_job("process_articles", {"limit": 3}, queue_name="external-ai",
                                               execution_region="external", capability="openai")
    core = _core(monkeypatch)
    job = _claim(core)
    assert repository.get_background_job(job["id"])["payload_json"].get("reserved_article_ids")

    response = _release(core, job)

    assert response.status_code == 200
    stored = repository.get_background_job(int(created["id"]))
    assert stored["status"] == "queued"
    assert stored["attempts"] == 0  # остановку устроили мы, попытка задачи не списана
    assert stored["lease_token_hash"] is None and stored["claimed_by"] is None
    with connection.get_connection() as conn:  # часы базы, а не Mac: выдача сверяет run_after с now() базы
        ready = conn.execute("SELECT run_after <= now() + interval '1 second' FROM background_jobs WHERE id = %s",
                             (int(created["id"]),)).fetchone()[0]
    assert ready  # в очереди сразу, а не через 600 с (секунда — на подводку часов ВМ, см. _claim)
    assert "reserved_article_ids" not in stored["payload_json"]  # статьи не держатся за ушедшим


def test_partial_release_writes_done_articles_and_next_claim_skips_them(isolated_db, monkeypatch):
    """Главная проверка пункта: оплаченная часть пакета не оплачивается второй раз."""
    ids = _seed(3)
    calls: list = []
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _CountingAI(calls))
    created = repository.create_background_job("process_articles", {"article_ids": ids, "limit": 3},
                                               queue_name="external-ai", execution_region="external",
                                               capability="openai")
    core = _core(monkeypatch)
    job = _claim(core)

    partial = external_ai.process_payload(job["payload"], heartbeat=_stop_before(3))
    assert partial.get("partial") is True
    assert [item["article_id"] for item in partial["articles"]] == ids[:2]
    assert _release(core, job, partial).status_code == 200

    stored = repository.get_background_job(int(created["id"]))
    assert stored["status"] == "queued" and stored["attempts"] == 0
    assert stored["payload_json"]["article_ids"] == ids[2:]
    with connection.get_connection() as conn:
        written = {int(row[0]) for row in conn.execute(
            "SELECT article_id FROM article_cards WHERE relevant IS TRUE AND summary IS NOT NULL").fetchall()}
        runs = conn.execute("SELECT COUNT(*) FROM ai_processing_runs WHERE job_id = %s AND stage = 'relevance'",
                            (int(created["id"]),)).fetchone()[0]
    assert written == set(ids[:2])
    assert runs == 2  # расход сделанной части учтён

    calls.clear()
    again = _claim(core)
    assert [article["id"] for article in again["payload"]["articles"]] == ids[2:]
    external_ai.process_payload(again["payload"])
    assert calls == ["title: Статья 2"]  # модель зовётся только за оставшуюся статью


def test_partial_release_of_limit_batch_takes_only_unprocessed_articles(isolated_db, monkeypatch):
    ids = _seed(5)
    calls: list = []
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _CountingAI(calls))
    created = repository.create_background_job("process_articles", {"limit": 3}, queue_name="external-ai",
                                               execution_region="external", capability="openai")
    core = _core(monkeypatch)
    job = _claim(core)
    assert [article["id"] for article in job["payload"]["articles"]] == ids[:3]

    partial = external_ai.process_payload(job["payload"], heartbeat=_stop_before(2))
    assert _release(core, job, partial).status_code == 200
    assert repository.get_background_job(int(created["id"]))["payload_json"]["limit"] == 2  # всего 3, как заказано

    calls.clear()
    again = _claim(core)
    assert [article["id"] for article in again["payload"]["articles"]] == ids[1:3]
    assert "title: Статья 0" not in calls


def test_dry_run_job_goes_back_whole_so_its_report_stays_complete(isolated_db, monkeypatch):
    """Повторное ревью 23.09: итог пробной перепроверки — отчёт (recheck-dry-show). По частям
    он не складывается: принятая сделанная часть пропала бы из итогового отчёта."""
    ids = _seed(3)
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _CountingAI([]))
    created = repository.create_background_job("recheck_relevance", {"article_ids": ids, "dry_run": True},
                                               queue_name="external-ai", execution_region="external",
                                               capability="openai")
    core = _core(monkeypatch)
    job = _claim(core)
    partial = external_ai.process_recheck_payload(job["payload"], heartbeat=_stop_before(3))
    assert partial.get("partial") is True

    assert _release(core, job, partial).status_code == 200

    stored = repository.get_background_job(int(created["id"]))
    assert stored["status"] == "queued" and stored["attempts"] == 0
    assert stored["payload_json"]["article_ids"] == ids  # целиком: отчёт соберётся полным


def test_release_with_all_work_done_finishes_job(isolated_db, monkeypatch):
    ids = _seed(2)
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _CountingAI([]))
    created = repository.create_background_job("process_articles", {"article_ids": ids}, queue_name="external-ai",
                                               execution_region="external", capability="openai")
    core = _core(monkeypatch)
    job = _claim(core)
    done = external_ai.process_payload(job["payload"])
    done["partial"] = True  # остановка пришла после последней статьи

    assert _release(core, job, done).status_code == 200

    stored = repository.get_background_job(int(created["id"]))
    assert stored["status"] == "ok"
    assert stored["result_json"]["applied"]["articles"] == 2


def _fail(core: TestClient, job: dict, result=None):
    return core.post(f"/api/external-worker/jobs/{job['id']}/fail", headers=AUTH,
                     json={"lease_token": job["lease_token"], "error": "нет продвижения 1200 с",
                           "retryable": True, "retry_after_seconds": 60, "result": result})


def test_stalled_job_writes_done_part_and_spends_its_attempt(isolated_db, monkeypatch):
    """Хвост сессии C: зависший пакет приходит через fail со снимком. Сделанное пишется и
    вычитается, как при release, но попытка списывается: задача, которая виснет сама (вечный
    вызов, регулярка по тексту статьи), иначе крутилась бы без конца — класс 24.07 (задача 1181)."""
    ids = _seed(3)
    calls: list = []
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _CountingAI(calls))
    created = repository.create_background_job("process_articles", {"article_ids": ids, "limit": 3},
                                               queue_name="external-ai", execution_region="external",
                                               capability="openai")
    core = _core(monkeypatch)
    job = _claim(core)
    done = external_ai.process_payload(job["payload"], heartbeat=_stop_before(3))  # снимок: 2 статьи

    assert _fail(core, job, done).status_code == 200

    stored = repository.get_background_job(int(created["id"]))
    assert stored["status"] == "queued"
    assert stored["attempts"] == 1  # попытка списана
    assert stored["payload_json"]["article_ids"] == ids[2:]
    assert "reserved_article_ids" not in stored["payload_json"]
    assert "сделано до сбоя: 2" in stored["error_message"]
    with connection.get_connection() as conn:
        written = {int(row[0]) for row in conn.execute(
            "SELECT article_id FROM article_cards WHERE relevant IS TRUE AND summary IS NOT NULL").fetchall()}
        runs = conn.execute("SELECT COUNT(*) FROM ai_processing_runs WHERE job_id = %s AND stage = 'relevance'",
                            (int(created["id"]),)).fetchone()[0]
        delay = conn.execute("SELECT EXTRACT(EPOCH FROM run_after - now()) FROM background_jobs WHERE id = %s",
                             (int(created["id"]),)).fetchone()[0]
        conn.execute("UPDATE background_jobs SET run_after = now() WHERE id = %s", (int(created["id"]),))
        conn.commit()
    assert written == set(ids[:2]) and runs == 2
    assert 50 < float(delay) <= 61  # пауза ретрая из запроса, как у обычного fail

    calls.clear()
    again = _claim(core)
    assert [article["id"] for article in again["payload"]["articles"]] == ids[2:]
    external_ai.process_payload(again["payload"])
    assert calls == ["title: Статья 2"]  # за записанное модель второй раз не зовётся


def test_stalled_job_out_of_attempts_fails_but_keeps_done_part(isolated_db, monkeypatch):
    ids = _seed(3)
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _CountingAI([]))
    created = repository.create_background_job("process_articles", {"article_ids": ids, "limit": 3},
                                               queue_name="external-ai", execution_region="external",
                                               capability="openai", max_attempts=1)
    core = _core(monkeypatch)
    job = _claim(core)
    done = external_ai.process_payload(job["payload"], heartbeat=_stop_before(2))

    assert _fail(core, job, done).status_code == 200

    stored = repository.get_background_job(int(created["id"]))
    assert stored["status"] == "failed" and stored["lease_token_hash"] is None
    assert stored["payload_json"]["article_ids"] == ids[1:]  # ручной перезапуск возьмёт только остаток
    with connection.get_connection() as conn:
        written = {int(row[0]) for row in conn.execute("SELECT article_id FROM article_cards").fetchall()}
    assert written == {ids[0]}


def test_stalled_dry_run_goes_back_whole(isolated_db, monkeypatch):
    """Пробный прогон по частям не складывается (как и при release) — обычный fail целиком."""
    ids = _seed(3)
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _CountingAI([]))
    created = repository.create_background_job("recheck_relevance", {"article_ids": ids, "dry_run": True},
                                               queue_name="external-ai", execution_region="external",
                                               capability="openai")
    core = _core(monkeypatch)
    job = _claim(core)
    done = external_ai.process_recheck_payload(job["payload"], heartbeat=_stop_before(3))
    assert done.get("partial") is True

    assert _fail(core, job, done).status_code == 200

    stored = repository.get_background_job(int(created["id"]))
    assert stored["status"] == "queued" and stored["attempts"] == 1
    assert stored["payload_json"]["article_ids"] == ids


def test_stalled_fail_with_done_part_needs_the_live_lease(isolated_db, monkeypatch):
    ids = _seed(2)
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _CountingAI([]))
    repository.create_background_job("process_articles", {"article_ids": ids}, queue_name="external-ai",
                                     execution_region="external", capability="openai")
    core = _core(monkeypatch)
    job = _claim(core)
    done = external_ai.process_payload(job["payload"], heartbeat=_stop_before(2))

    forged = _fail(core, {**job, "lease_token": "чужой"}, done)

    assert forged.status_code == 409
    assert repository.get_background_job(job["id"])["status"] == "running"
    with connection.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM article_cards").fetchone()[0] == 0


def test_release_needs_the_live_lease(isolated_db, monkeypatch):
    _seed(1)
    repository.create_background_job("process_articles", {"limit": 1}, queue_name="external-ai",
                                     execution_region="external", capability="openai")
    core = _core(monkeypatch)
    job = _claim(core)

    forged = core.post(f"/api/external-worker/jobs/{job['id']}/release", headers=AUTH,
                       json={"lease_token": "чужой", "reason": "x"})

    assert forged.status_code == 409
    assert repository.get_background_job(job["id"])["status"] == "running"
