"""Мягкая остановка на настоящих процессах (сессия C, п. 1) и зависший шаг (хвост C, 28.09).

Спека: «SIGTERM посреди задачи → задача снова в очереди за секунды, а не через 600 с; для
уже записанных статей ИИ повторно не вызывается». Ядро — настоящий uvicorn, воркер —
отдельный процесс `cli external-worker`, сигнал — настоящий SIGTERM, итог уходит через
requests и JSON, как на NL (урок 21.09: подмена функций этой границы не видит). Модель
офлайн: замедлена и пишет, за какую статью её звали (sitecustomize процесса воркера); для
зависшего шага она на одной статье не возвращается, а срок зависания — секунды, а не 20 минут.
"""

import hashlib
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Json

from oiltech_digest.config import DATABASE_URL

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "live-stop-secret"
SLOW_MODEL = """
import os, time
if os.environ.get("LIVE_CALLS_LOG"):
    from oiltech_digest.processing import openai_client
    _orig = openai_client.OfflineAIClient.complete_json
    def _slow(self, instructions, user_input, schema, **kwargs):
        if schema["name"] == "article_relevance":
            with open(os.environ["LIVE_CALLS_LOG"], "a", encoding="utf-8") as fh:
                fh.write(user_input.splitlines()[0] + "\\n")
            if user_input.splitlines()[0] == os.environ.get("LIVE_HANG_ON"):
                time.sleep(3600)  # вызов модели, который не возвращается
        time.sleep(float(os.environ.get("LIVE_SLOW_AI") or 0))
        return _orig(self, instructions, user_input, schema, **kwargs)
    openai_client.OfflineAIClient.complete_json = _slow
if os.environ.get("LIVE_STALL_SECONDS"):
    from oiltech_digest import external_worker
    external_worker._JOB_STALL_SECONDS["process_articles"] = float(os.environ["LIVE_STALL_SECONDS"])
"""


def _wait(predicate, timeout: float, step: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


@pytest.fixture()
def live(tmp_path):
    schema = f"test_live_{uuid.uuid4().hex[:12]}"
    url = f"{DATABASE_URL}{'&' if '?' in DATABASE_URL else '?'}options=-csearch_path%3D{schema}"
    with psycopg.connect(DATABASE_URL) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.commit()
    with psycopg.connect(url) as conn:
        conn.execute((ROOT / "oiltech_digest/db/schema.sql").read_text())
        conn.commit()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "sitecustomize.py").write_text(SLOW_MODEL)
    procs: list[subprocess.Popen] = []

    def start(args: list[str], **env: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-m", *args], cwd=str(ROOT),
            env={**os.environ, "DATABASE_URL": url, "PYTHONUNBUFFERED": "1", **env},
            stdout=(tmp_path / f"proc{len(procs)}.log").open("w"), stderr=subprocess.STDOUT,
        )
        procs.append(proc)
        return proc

    def sql(query: str, params=None):
        with psycopg.connect(url) as conn:
            cur = conn.execute(query, params)
            rows = cur.fetchall() if cur.description else None
            conn.commit()
            return rows

    core = start(["uvicorn", "oiltech_digest.api:app", "--host", "127.0.0.1", "--port", str(port)],
                 EXTERNAL_WORKER_TOKEN_HASH=hashlib.sha256(TOKEN.encode()).hexdigest())

    def healthy():
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2).status == 200
        except OSError:
            return False

    try:
        assert _wait(healthy, 60, 0.3), "ядро не поднялось"
        yield {"sql": sql, "start": start, "port": port, "tmp": tmp_path, "core": core}
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
        with psycopg.connect(DATABASE_URL) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            admin.commit()


def _worker(live, worker_id: str, calls: Path, *, slow: float, **env: str):
    return live["start"](
        ["oiltech_digest.cli", "-v", "external-worker", "--core-api-url", f"http://127.0.0.1:{live['port']}",
         "--token", TOKEN, "--worker-id", worker_id, "--queue", "external-ai", "--capability", "openai",
         "--poll-seconds", "0.2"],
        PYTHONPATH=f"{live['tmp'] / 'site'}:{ROOT}", LIVE_CALLS_LOG=str(calls), LIVE_SLOW_AI=str(slow),
        EXTERNAL_WORKER_STOP_GRACE_SECONDS="1", EXTERNAL_WORKER_STOP_STEP_SECONDS="10", **env,
    )


def _batch(sql, count: int) -> tuple[list[int], int]:
    """Статьи «Живая статья N» (N по порядку пакета) и задача ИИ-пакета явным списком."""
    source = sql("INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
                 "VALUES ('Live', 'News', 'https://live.example', TRUE, 'request') RETURNING id")[0][0]
    sql("INSERT INTO tags (name, enabled, sort_order) VALUES ('Бурение', TRUE, 1)")
    sql("INSERT INTO scoring_criteria (name, weight, enabled, sort_order) VALUES ('Значимость', 100, TRUE, 1)")
    now = datetime.now(timezone.utc)
    ids = [sql("INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language, content_hash) "
               "VALUES (%s, %s, %s, %s, %s, %s, 'ru', %s) RETURNING id",
               (source, f"Живая статья {index}", f"https://live.example/{index}", now - timedelta(hours=index), now,
                f"Живая статья {index}: бурение и сервисный контракт. " * 12, f"live-{index}"))[0][0]
           for index in range(count)]
    job = sql("INSERT INTO background_jobs (kind, payload_json, queue_name, execution_region, capability) "
              "VALUES ('process_articles', %s, 'external-ai', 'external', 'openai') RETURNING id",
              (Json({"article_ids": ids, "limit": count, "offline": True}),))[0][0]
    return ids, job


def _called(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_sigterm_mid_batch_requeues_in_seconds_and_nothing_is_paid_twice(live):
    sql = live["sql"]
    ids, job = _batch(sql, 6)

    def state():
        return sql("SELECT status, attempts, claimed_by, lease_token_hash IS NULL, payload_json "
                   "FROM background_jobs WHERE id = %s", (job,))[0]

    called = _called
    first_calls = live["tmp"] / "first.calls"
    first = _worker(live, "nl-live-1", first_calls, slow=0.25)
    assert _wait(lambda: len(called(first_calls)) >= 2, 30), "воркер не начал пакет"

    stopped_at = time.monotonic()
    first.send_signal(signal.SIGTERM)
    assert _wait(lambda: state()[0] == "queued", 15), f"задача не вернулась в очередь: {state()[:3]}"
    returned_in = time.monotonic() - stopped_at
    first.wait(timeout=30)

    status, attempts, claimed_by, lease_cleared, payload = state()
    written = {row[0] for row in sql("SELECT article_id FROM article_cards WHERE summary IS NOT NULL")}
    assert returned_in < 10  # секунды, а не 600 с аренды
    assert first.returncode == 0
    assert attempts == 0 and claimed_by is None and lease_cleared
    assert 0 < len(written) < len(ids)
    assert sorted(payload["article_ids"]) == sorted(set(ids) - written)
    assert "reserved_article_ids" not in payload

    second_calls = live["tmp"] / "second.calls"
    second = _worker(live, "nl-live-2", second_calls, slow=0)
    assert _wait(lambda: state()[0] == "ok", 60), f"остаток не доделан: {state()[:3]}"
    second.send_signal(signal.SIGTERM)
    second.wait(timeout=30)

    paid = called(first_calls) + called(second_calls)
    assert len(paid) == len(ids) and len(set(paid)) == len(ids)  # каждая статья — ровно один вызов гейта


def test_stalled_step_hands_back_done_part_and_nothing_done_is_paid_twice(live):
    """Хвост сессии C: вызов модели на третьей статье не вернулся. Процесс замечает зависание,
    уходит на перезапуск (код 70 — Docker поднимет его снова), а задача раньше уходила ядру
    через fail без снимка — две оплаченные статьи делались и оплачивались заново."""
    sql = live["sql"]
    ids, job = _batch(sql, 6)

    def state():
        return sql("SELECT status, attempts, claimed_by, lease_token_hash IS NULL, payload_json, error_message "
                   "FROM background_jobs WHERE id = %s", (job,))[0]

    first_calls = live["tmp"] / "stalled.calls"
    # Срок зависания — 3 с при шаге ~0,4 с: запас, чтобы под нагрузкой сторож не сработал раньше
    # зависшей статьи.
    first = _worker(live, "nl-live-1", first_calls, slow=0.1, LIVE_HANG_ON="title: Живая статья 2",
                    LIVE_STALL_SECONDS="3", EXTERNAL_WORKER_HEARTBEAT_SECONDS="0.2")
    first.wait(timeout=60)

    status, attempts, claimed_by, lease_cleared, payload, error = state()
    written = [row[0] for row in sql("SELECT article_id FROM article_cards WHERE summary IS NOT NULL ORDER BY article_id")]
    assert first.returncode == 70  # перезапуск из-за зависшего шага, как и раньше
    assert status == "queued" and claimed_by is None and lease_cleared
    assert attempts == 1  # попытка списана: зависание может быть в самой задаче
    assert written == ids[:2]  # сделанное до зависшего шага записано
    assert payload["article_ids"] == ids[2:]  # и вычтено из задачи
    assert "сделано до сбоя: 2" in error

    sql("UPDATE background_jobs SET run_after = now() WHERE id = %s", (job,))  # не ждать паузу ретрая 60 с
    second_calls = live["tmp"] / "after-stall.calls"
    second = _worker(live, "nl-live-2", second_calls, slow=0)
    assert _wait(lambda: state()[0] == "ok", 60), f"остаток не доделан: {state()[:3]}"
    second.send_signal(signal.SIGTERM)
    second.wait(timeout=30)

    assert _called(first_calls) == [f"title: Живая статья {index}" for index in range(3)]
    # За записанные 0 и 1 модель второй раз не звали; 2-я — повтор зависшего вызова, он не вернулся.
    assert _called(second_calls) == [f"title: Живая статья {index}" for index in range(2, 6)]
