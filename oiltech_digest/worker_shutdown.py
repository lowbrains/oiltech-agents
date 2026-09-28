"""Мягкая остановка воркера NL (SIGTERM при выкате): кто в работе, кто отчитается, надзор.

До 23.09 обработчика SIGTERM не было, а stop_grace_period — 10 с по умолчанию: каждая
пересборка NL обрывала задачи в работе, они ждали конца аренды (600 с), а оплаченная часть
ИИ-пакета оплачивалась заново. Теперь по сигналу процесс перестаёт брать задачи, текущим даёт
EXTERNAL_WORKER_STOP_GRACE_SECONDS на то, чтобы закончить, затем обработчик останавливается на
ближайшем шаге и отдаёт ядру сделанное (release). Шаг, не дошедший до границы за
EXTERNAL_WORKER_STOP_STEP_SECONDS, возвращает главный поток — с последним снимком сделанного:
граница шага в ИИ-пакете — целая статья, до пяти вызовов модели подряд, и без снимка
оплаченное до зависшего шага пропало бы (ревью 23.09).

Тот же снимок отдаёт и сторож аренды, когда шаг завис дольше срока (external_worker.LeaseKeeper):
процесс уходит на перезапуск, а сделанное не выбрасывается (хвост сессии C, 28.09).

Здесь — состояние процесса и надзор главного потока; как задачу вернуть — external_worker.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Any, Callable

from oiltech_digest import config

logger = logging.getLogger(__name__)

# Сколько главный поток ждёт отчёта, который уже в пути (complete/release/fail).
REPORT_SECONDS = 15.0


class _Job:
    """Задача в работе у процесса. Отчитаться о ней должен ровно один: поток обработчика
    (complete/release/fail), главный поток на остановке или сторож аренды при зависшем шаге —
    иначе пустой release главного потока мог бы обогнать уже готовый итог."""

    __slots__ = ("client", "job", "state", "done")

    def __init__(self, client: Any, job: dict[str, Any]) -> None:
        self.client = client
        self.job = job
        self.state = "working"  # working → reporting (обработчик) | returned (главный поток, сторож)
        self.done: dict[str, Any] | None = None  # снимок сделанного на последней границе шага


def _snapshot(done: dict[str, Any]) -> dict[str, Any]:
    """Копия на момент шага: обработчик дописывает свои списки дальше, а строки, уже
    попавшие в список, не меняет — копии на один уровень достаточно."""
    copy = {
        key: list(value) if isinstance(value, list) else dict(value) if isinstance(value, dict) else value
        for key, value in done.items()
    }
    copy["partial"] = True
    return copy


class Shutdown:
    """Остановка процесса воркера: сигнал, срок, задачи в работе."""

    def __init__(self) -> None:
        self.requested = threading.Event()
        self.at: float | None = None
        self.reason = ""
        self._jobs: dict[int, _Job] = {}
        self._lock = threading.Lock()

    def request(self, reason: str = "") -> None:
        # Зовётся из обработчика сигнала: только флаг, время и причина — без логов и замков.
        if self.requested.is_set():
            return
        self.at = time.monotonic()
        self.reason = reason
        self.requested.set()

    def due(self) -> bool:
        """Срок на завершение вышел — обработчик останавливается на ближайшем шаге."""
        return self.requested.is_set() and (
            time.monotonic() >= (self.at or 0.0) + config.EXTERNAL_WORKER_STOP_GRACE_SECONDS
        )

    def pause(self, seconds: float) -> None:
        """Пауза, которую прерывает остановка: простаивающий воркер уходит сразу, а не через 30 с."""
        self.requested.wait(seconds)

    def track(self, client: Any, job: dict[str, Any]) -> None:
        with self._lock:
            self._jobs[int(job["id"])] = _Job(client, job)

    def untrack(self, job: dict[str, Any]) -> None:
        with self._lock:
            self._jobs.pop(int(job["id"]), None)

    def in_work(self, exclude: dict[str, Any] | None = None) -> int:
        """Сколько задач в работе. exclude — не считать эту (сторож ждёт соседей своей задачи)."""
        with self._lock:
            if exclude is None:
                return len(self._jobs)
            return sum(1 for key in self._jobs if key != int(exclude["id"]))

    def checkpoint(self, job: dict[str, Any], done: dict[str, Any]) -> None:
        """Граница шага: запомнить сделанное — его отдаст главный поток, если шаг зависнет."""
        snapshot = _snapshot(done)
        with self._lock:
            entry = self._jobs.get(int(job["id"]))
            if entry is not None:
                entry.done = snapshot

    def begin_report(self, job: dict[str, Any]) -> bool:
        """Поток обработчика отчитывается сам — если задачу ещё не вернул главный поток."""
        with self._lock:
            entry = self._jobs.get(int(job["id"]))
            if entry is None:
                return True
            if entry.state == "returned":
                return False
            entry.state = "reporting"
            return True

    def take_unfinished(self) -> list[_Job]:
        with self._lock:
            taken = [entry for entry in self._jobs.values() if entry.state == "working"]
            for entry in taken:
                entry.state = "returned"
            return taken

    def take(self, job: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        """Сторож аренды забирает зависшую задачу, чтобы отчитаться за неё (external_worker).

        (True, снимок сделанного или None) — отчитывается сторож, поток обработчика, проснувшись,
        уже промолчит. (False, None) — отчёт уже у обработчика (итог в пути), задачу вернул главный
        поток на остановке или её нет в реестре: обработчик уже отчитался и вышел."""
        with self._lock:
            entry = self._jobs.get(int(job["id"]))
            if entry is None or entry.state != "working":
                return False, None
            entry.state = "returned"
            return True, entry.done

    def reporting(self) -> bool:
        with self._lock:
            return any(entry.state == "reporting" for entry in self._jobs.values())

    def busy(self) -> list[int]:
        with self._lock:
            return sorted(self._jobs)


SHUTDOWN = Shutdown()


def on_signal(signum: int, frame: Any) -> None:
    SHUTDOWN.request(signal.Signals(signum).name)


def install_signals() -> dict[int, Any]:
    if threading.current_thread() is not threading.main_thread():
        return {}
    return {signum: signal.signal(signum, on_signal) for signum in (signal.SIGTERM, signal.SIGINT)}


def restore_signals(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        if handler is not None:
            signal.signal(signum, handler)


def supervise(threads: list[threading.Thread],
              release: Callable[[Any, dict[str, Any], str, dict[str, Any] | None], None]) -> None:
    """Главный поток: ждёт сигнала, а по нему возвращает ядру то, что не успело закончиться."""
    state = SHUTDOWN
    while any(thread.is_alive() for thread in threads):
        if state.requested.wait(1.0):
            break
    if not state.requested.is_set():
        return
    grace = config.EXTERNAL_WORKER_STOP_GRACE_SECONDS
    logger.warning("external_worker_stopping reason=%s — новых задач не беру; в работе %s, на завершение %s с",
                   state.reason or "—", state.busy() or "нет", int(grace))
    deadline = (state.at if state.at is not None else time.monotonic()) + grace + config.EXTERNAL_WORKER_STOP_STEP_SECONDS
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    for entry in state.take_unfinished():
        fork = getattr(entry.client, "fork", None)
        release(fork() if callable(fork) else entry.client, entry.job,
                "остановка воркера: шаг не завершился в срок", entry.done)
    report_deadline = time.monotonic() + REPORT_SECONDS
    while state.reporting() and time.monotonic() < report_deadline:
        time.sleep(0.1)
    logger.warning("external_worker_stopped")
