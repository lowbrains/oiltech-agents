"""Ровно один планировщик (ADR 0001, п. 4).

21.09 лишний `docker compose up` поднял второй планировщик: 8,5 ч дублей сбора и ИИ
($4,48), прогон радара за день потерян. Планировщик — shell-цикл, и ничто не знало, что
он уже запущен. Теперь его процесс держит advisory lock в Postgres всё время жизни — и цикл,
и паузу между циклами. Второй экземпляр пишет в лог, у кого замок, и ждёт, не делая ни
одного шага. Замок — сессия базы: процесс умер — замок снят сам, ждущий его забирает.

Почему весь процесс, а не каждый цикл: с замком на цикл два экземпляра шли бы по очереди —
без наложения, но со сбором и постановкой ИИ вдвое чаще, то есть тот же дубль.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from datetime import datetime
from typing import Callable

import psycopg

from oiltech_digest import config

# Ключ замка планировщика. Рядом с ключом резерва статей (repository._PROCESS_RESERVE_LOCK).
SCHEDULER_LOCK_KEY = 7_290_922
# Процесс под замком получает эту переменную — скрипт планировщика по ней понимает,
# что замок уже взят, и не заворачивает себя второй раз.
HELD_ENV = "SCHEDULER_LOCK_HELD"
# Код выхода, когда соединение с замком потеряно: Docker перезапустит контейнер, и замок
# будет взят заново честно — а не шаги пойдут дальше без него.
EXIT_LOCK_LOST = 75
# Ждущий напоминает о себе в логе не чаще этого — строка в лог, а не поток строк.
_WAIT_LOG_EVERY_SECONDS = 600
_CHILD_STOP_SECONDS = 20


def _log(message: str) -> None:
    # Формат строк docker-scheduler.sh: время ISO и текст — лог планировщика читается одним потоком.
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}", flush=True)


def lock_name(key: int) -> str:
    return f"oiltech-scheduler-lock:{key}@{socket.gethostname()}"[:63]


def _holder(conn: psycopg.Connection, key: int) -> str:
    row = conn.execute(
        """
        SELECT a.application_name, COALESCE(host(a.client_addr), 'local'), a.backend_start
        FROM pg_locks l
        JOIN pg_stat_activity a ON a.pid = l.pid
        WHERE l.locktype = 'advisory' AND l.granted
          AND l.classid = %s AND l.objid = %s AND l.objsubid = 1
        """,
        ((key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF),
    ).fetchone()
    if row is None:
        return "неизвестно у кого"
    return f"{row[0] or 'без имени'} ({row[1]}, с {row[2]:%Y-%m-%d %H:%M:%S %Z})"


class _Stop:
    def __init__(self) -> None:
        self.requested = False
        self.child: subprocess.Popen | None = None

    def __call__(self, signum: int, frame: object) -> None:
        self.requested = True
        _signal_child(self.child, signal.SIGTERM)


def _signal_child(child: subprocess.Popen | None, signum: int) -> None:
    if child is None or child.poll() is not None:
        return
    try:
        # Весь процесс-группой: шаг цикла (python -m ...) и sleep — дети shell-скрипта.
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass


def _stop_child(child: subprocess.Popen) -> int:
    _signal_child(child, signal.SIGTERM)
    try:
        child.wait(timeout=_CHILD_STOP_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_child(child, signal.SIGKILL)
        child.wait(timeout=5)
    return _exit_code(child.returncode)


def _exit_code(code: int) -> int:
    # Убитый сигналом ребёнок даёт -N; наружу — как принято у shell, 128 + N.
    return 128 - code if code < 0 else code


def _sleep(seconds: float, stop: _Stop) -> None:
    """Пауза ожидания, которую прерывает остановка: docker stop не ждёт 30 с опроса."""
    deadline = time.monotonic() + seconds
    while not stop.requested and time.monotonic() < deadline:
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def _acquire(key: int, stop: _Stop, poll_seconds: float,
             connect: Callable[[], psycopg.Connection]) -> psycopg.Connection | None:
    """Ждать замок. None — пришла остановка, пока ждали."""
    conn: psycopg.Connection | None = None
    last_wait_log = 0.0
    while not stop.requested:
        try:
            if conn is None or conn.closed:
                conn = connect()
            if conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
                return conn
            if time.monotonic() - last_wait_log >= _WAIT_LOG_EVERY_SECONDS:
                _log(f"scheduler-lock: планировщик уже работает — замок {key} держит {_holder(conn, key)}; жду")
                last_wait_log = time.monotonic()
        except psycopg.Error as exc:
            _log(f"scheduler-lock: база недоступна ({type(exc).__name__}: {str(exc).strip()[:200]}); жду")
            if conn is not None:
                conn.close()
            conn = None
        _sleep(poll_seconds, stop)
    if conn is not None:
        conn.close()
    return None


def run_exclusive(
    command: list[str],
    *,
    key: int = SCHEDULER_LOCK_KEY,
    poll_seconds: float = 30.0,
    check_seconds: float = 30.0,
) -> int:
    """Выполнить команду, держа замок `key` всё время её жизни. Возвращает код выхода."""
    if not command:
        raise ValueError("scheduler-lock: не указана команда")
    stop = _Stop()
    previous = {signum: signal.signal(signum, stop) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        conn = _acquire(
            key, stop, poll_seconds,
            lambda: psycopg.connect(config.DATABASE_URL, autocommit=True, application_name=lock_name(key)),
        )
        if conn is None:
            _log("scheduler-lock: остановка до получения замка")
            return 128 + signal.SIGTERM
        with conn:
            _log(f"scheduler-lock: замок {key} взят ({lock_name(key)}) — запускаю {' '.join(command)}")
            child = subprocess.Popen(command, env={**os.environ, HELD_ENV: "1"}, start_new_session=True)
            stop.child = child
            if stop.requested:  # сигнал пришёл между взятием замка и запуском
                return _stop_child(child)
            while True:
                try:
                    return _exit_code(child.wait(timeout=check_seconds))
                except subprocess.TimeoutExpired:
                    pass
                if stop.requested:
                    return _stop_child(child)
                try:
                    conn.execute("SELECT 1")
                except psycopg.Error as exc:
                    _log(f"scheduler-lock: соединение с замком потеряно ({type(exc).__name__}) — "
                         "останавливаю шаги: без замка может начать второй экземпляр")
                    _stop_child(child)
                    return EXIT_LOCK_LOST
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
