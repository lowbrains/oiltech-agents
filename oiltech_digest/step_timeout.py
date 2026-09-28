"""Потолок времени шага планировщика (docker-scheduler.sh → run_step).

24.09 08:32 шаг parse не вернулся 20 ч 45 мин: page.content() у листинга JPT ждал без
срока, а шаги идут друг за другом — стоял весь цикл, ни сбора, ни постановки ИИ. Теперь
шаг идёт под сроком: дольше — SIGTERM, через kill_after — SIGKILL, код 124 (как у
coreutils timeout), и цикл идёт дальше.

Шаг остаётся в группе процессов скрипта, как у `timeout --foreground`: scheduler-lock
гасит скрипт сигналом всей группе (singleton._signal_child), и остановка контейнера
доходит до шага сама. coreutils `timeout` без --foreground уводит шаг в свою группу — и
SIGTERM до шага уже не доходит, а на маке, где идут тесты, `timeout` нет вовсе. Сигнал по
сроку получает только шаг: его драйвер Playwright выходит сам, когда закрывается труба от
убитого родителя, и снимает свой Chromium.

Не подкоманда cli, как scheduler-lock: cli.main настраивает логи через config, который читает
окружение при импорте, — сломанная переменная уронила бы сторожа, то есть каждый шаг, а не
один. Поэтому только стандартная библиотека (и памяти на всё время шага меньше: 9 МБ против 12).

Запуск: python -m oiltech_digest.step_timeout СЕКУНДЫ СЕКУНДЫ_ДО_KILL -- команда …
СЕКУНДЫ = 0 — без потолка: процесс становится самой командой.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
from datetime import datetime

EXIT_TIMEOUT = 124  # как у coreutils timeout: run_step по этому коду пишет TIMEOUT
EXIT_USAGE = 2
EXIT_NOT_STARTED = 127  # как у shell: команда не нашлась или не запустилась
USAGE = "использование: python -m oiltech_digest.step_timeout СЕКУНДЫ СЕКУНДЫ_ДО_KILL -- команда …"


def _log(message: str) -> None:
    # Формат строк docker-scheduler.sh: время ISO и текст — лог планировщика читается одним потоком.
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}", flush=True)


def _exit_code(code: int) -> int:
    # Убитый сигналом шаг даёт -N; наружу — как принято у shell, 128 + N.
    return 128 - code if code < 0 else code


def run(command: list[str], seconds: float, kill_after: float) -> int:
    """Выполнить команду не дольше `seconds`. Возвращает её код или 124 по сроку."""
    try:
        if seconds <= 0:
            os.execvp(command[0], command)
        child = subprocess.Popen(command)
    except OSError as exc:
        _log(f"step-timeout: не запустить {command[0]}: {exc}")
        return EXIT_NOT_STARTED
    try:
        return _exit_code(child.wait(timeout=seconds))
    except subprocess.TimeoutExpired:
        pass
    _log(f"step-timeout: шаг идёт дольше {seconds:g} с — SIGTERM (pid {child.pid}): {' '.join(command)}")
    child.terminate()
    try:
        child.wait(timeout=kill_after)
    except subprocess.TimeoutExpired:
        _log(f"step-timeout: шаг не вышел за {kill_after:g} с после SIGTERM — SIGKILL (pid {child.pid})")
        child.kill()
        child.wait()
    return EXIT_TIMEOUT


def _parse_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(value)
    return number


def main(argv: list[str]) -> int:
    try:
        seconds, kill_after = _parse_seconds(argv[0]), _parse_seconds(argv[1])
        if argv[2] != "--" or len(argv) < 4:
            raise ValueError(argv[2])
    except (IndexError, ValueError):
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    return run(argv[3:], seconds, kill_after)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
