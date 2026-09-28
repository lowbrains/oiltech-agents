"""Планировщик: ровно один (сессия C, п. 2) и без вечных шагов (инцидент 24.09).

21.09 лишний `compose up` поднял второй планировщик: 8,5 ч дублей сбора и ИИ ($4,48),
радар дня потерян. Теперь процесс планировщика держит advisory lock в Postgres всё время
жизни; второй пишет в лог и ждёт, не делая ни одного шага.

24.09 08:32 шаг parse не вернулся 20 ч 45 мин: page.content() у листинга JPT ждал без
срока, шаги идут друг за другом — стоял весь цикл. Под PID 1 контейнера к тому часу было
около 2250 зомби chrome (по 2 на рендер; 25.09 в 05:31 — 2259) при pids.max 2315. run_step
ведёт шаг под сроком (oiltech_digest/step_timeout.py), сервисы с Chromium идут с init.

Процессы настоящие: замок — это сессия базы, срок — сигналы и группы процессов; подделка
их не проверит. Каждый тест падает на коде до своей правки, кроме страховки остановки
(test_container_stop_still_reaches_the_step) — она держит от регресса."""

import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest
import yaml

from oiltech_digest import singleton
from oiltech_digest.config import DATABASE_URL

ROOT = Path(__file__).resolve().parents[1]
# Шаг «планировщика»: отмечается в файле и живёт, пока его не остановят.
STEP = ("import os, sys, time\n"
        "open(sys.argv[1], 'a').write(f\"{os.getpid()} {os.environ.get('SCHEDULER_LOCK_HELD')}\\n\")\n"
        "time.sleep(120)\n")


def _wrapper(key: int, marker: Path, *extra: str) -> subprocess.Popen:
    # Вывод — в файл, а не в pipe: шаг живёт в своей группе процессов, и оставшись сиротой,
    # он держал бы pipe открытым — чтение вывода висело бы до его конца.
    out = marker.with_name(f"{marker.name}.{time.monotonic_ns()}.out")
    proc = subprocess.Popen(
        [sys.executable, "-m", "oiltech_digest.cli", "scheduler-lock", "--key", str(key),
         "--poll-seconds", "0.2", *extra, "--", sys.executable, "-c", STEP, str(marker)],
        cwd=str(ROOT), env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=out.open("w"), stderr=subprocess.STDOUT,
    )
    proc.out_path = out  # type: ignore[attr-defined]
    proc.marker = marker  # type: ignore[attr-defined]
    return proc


def _output(proc: subprocess.Popen) -> str:
    return proc.out_path.read_text()  # type: ignore[attr-defined]


def _steps(marker: Path) -> list[list[str]]:
    return [line.split() for line in marker.read_text().splitlines()] if marker.exists() else []


def _wait(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # macOS так отвечает про зомби, которого ещё не подобрали
        return True
    return True


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # вышел или зомби (EPERM на macOS)
        pass


def _stop(*procs: subprocess.Popen) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)  # обёртка гасит свои шаги сама
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for pid, _held in _steps(proc.marker):  # type: ignore[attr-defined]
            _kill_quietly(int(pid))


@pytest.fixture()
def key() -> int:
    # Замок общий на всю базу, а не на схему теста: у каждого теста свой ключ.
    return random.randint(10**9, 2 * 10**9)


def test_second_scheduler_waits_and_runs_no_steps(tmp_path, key):
    marker = tmp_path / "steps"
    first = _wrapper(key, marker)
    second = None
    try:
        assert _wait(lambda: len(_steps(marker)) == 1)
        assert _steps(marker)[0][1] == "1"  # шаги знают, что замок у их процесса

        second = _wrapper(key, marker)
        time.sleep(2.0)
        assert len(_steps(marker)) == 1  # второй не сделал ни одного шага
        assert second.poll() is None  # и не упал — ждёт

        first.send_signal(signal.SIGTERM)
        first.wait(timeout=15)
        assert _wait(lambda: len(_steps(marker)) == 2)  # замок перешёл к ждущему
    finally:
        _stop(*(proc for proc in (first, second) if proc is not None))


def test_waiting_scheduler_says_who_holds_the_lock(tmp_path, key):
    marker = tmp_path / "steps"
    first = _wrapper(key, marker)
    second = None
    try:
        assert _wait(lambda: len(_steps(marker)) == 1)
        second = _wrapper(key, marker)
        time.sleep(1.5)
        second.send_signal(signal.SIGTERM)
        second.wait(timeout=15)
    finally:
        _stop(*(proc for proc in (first, second) if proc is not None))
    output = _output(second)
    assert "жду" in output and f"scheduler-lock:{key}" in output
    assert second.returncode != 0


def test_steps_stop_when_lock_connection_is_lost(tmp_path, key):
    """Замок — сессия базы. Порвалась (перезапуск Postgres) — второй экземпляр может его
    взять, поэтому шаги этого процесса обязаны остановиться, а не идти без замка."""
    marker = tmp_path / "steps"
    first = _wrapper(key, marker, "--check-seconds", "0.3")
    try:
        assert _wait(lambda: len(_steps(marker)) == 1)
        step_pid = int(_steps(marker)[0][0])
        with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
            killed = admin.execute(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE application_name LIKE %s",
                (f"oiltech-scheduler-lock:{key}%",),
            ).fetchone()[0]
        assert killed == 1
        first.wait(timeout=15)
        assert first.returncode == 75  # Docker поднимет заново, и замок будет взят честно
        assert _wait(lambda: not _alive(step_pid), 5)
    finally:
        _stop(first)


def test_sigterm_stops_scheduler_steps_quickly(tmp_path, key):
    marker = tmp_path / "steps"
    first = _wrapper(key, marker)
    try:
        assert _wait(lambda: len(_steps(marker)) == 1)
        step_pid = int(_steps(marker)[0][0])
        started = time.monotonic()
        first.send_signal(signal.SIGTERM)
        first.wait(timeout=15)
        assert time.monotonic() - started < 5  # раньше PID 1-shell игнорировал SIGTERM, Docker ждал 10 с
        assert _wait(lambda: not _alive(step_pid), 5)
    finally:
        _stop(first)


def test_scheduler_script_takes_the_lock_before_any_step():
    script = (ROOT / "scripts" / "docker-scheduler.sh").read_text().splitlines()
    lock_line = next(index for index, line in enumerate(script) if "scheduler-lock" in line and "exec " in line)
    first_step = next(index for index, line in enumerate(script)
                      if line.strip().startswith(("run_step ", "run_required_step ")))
    assert lock_line < first_step


def _app_service(services: dict) -> str:
    """Сервис приложения зовётся по-разному: app у основного стека, agents-app у агентного
    (общая сеть не даёт делить имя) — до смены идентичности стека в D."""
    return "app" if "app" in services else "agents-app"


def test_bare_compose_up_cannot_start_pipeline_services():
    """Голый `up -d` 21.09 поднял всё. Конвейер — только по профилю или по имени сервиса."""
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name in ("worker", "playwright-worker", "scheduler"):
        assert "pipeline" in (services[name].get("profiles") or []), name
    # tasks — архивный модуль (сессия B): его голый `up` тоже не поднимает, но другим профилем.
    assert services["tasks"].get("profiles"), "tasks"
    # Caddy — только у основного стека; у агентного до смены идентичности в D его нет.
    for name in ("db", _app_service(services), *(["caddy"] if "caddy" in services else [])):
        assert not services[name].get("profiles"), name


# Подставной python для docker-scheduler.sh: шаги cli отмечаются в файле («имя pid»), шаг
# HANG_STEP висит, а сторож шага и всё прочее идут настоящим интерпретатором.
FAKE_PYTHON = """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "oiltech_digest.cli" ]; then
  echo "$3 $$" >> "$STEPS_FILE"
  if [ "$3" = "$HANG_STEP" ]; then
    exec sleep 300
  fi
  exit 0
fi
exec "$REAL_PYTHON" "$@"
"""


def _fake_steps(path: Path) -> list[tuple[str, int]]:
    if not path.exists():
        return []
    return [(name, int(pid)) for name, pid in (line.split() for line in path.read_text().splitlines())]


def _fake_python_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python"
    fake.write_text(FAKE_PYTHON)
    fake.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "REAL_PYTHON": sys.executable,
        "STEPS_FILE": str(tmp_path / "steps"),
    }


def _scheduler(tmp_path: Path, **env: str) -> subprocess.Popen:
    """docker-scheduler.sh без замка и бутстрапа: первый шаг цикла — maintenance-cleanup."""
    out = tmp_path / "scheduler.out"
    proc = subprocess.Popen(
        ["sh", "scripts/docker-scheduler.sh"],
        cwd=str(ROOT),
        env={
            **_fake_python_env(tmp_path),
            "HANG_STEP": "maintenance-cleanup",
            "SCHEDULER_LOCK_HELD": "1",
            "SKIP_BOOTSTRAP": "1",
            "RUN_MAINTENANCE_ON_START": "1",
            "RUN_DISCOVER_ON_START": "1",
            "AI_PROCESS_LIMIT": "0",
            "FETCH_EXTERNAL_ENABLED": "0",
            "REPRINTS_INTERVAL_HOURS": "0",
            "CYCLE_INTERVAL_SECONDS": "300",
            "STEP_TIMEOUT_SECONDS": "1",
            "STEP_KILL_AFTER_SECONDS": "1",
            **env,
        },
        # Как у scheduler-lock: скрипт — лидер своей группы, остановка бьёт в группу.
        start_new_session=True,
        stdout=out.open("w"),
        stderr=subprocess.STDOUT,
    )
    proc.out_path = out  # type: ignore[attr-defined]
    proc.steps_path = tmp_path / "steps"  # type: ignore[attr-defined]
    return proc


def _cleanup(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # EPERM на macOS: в группе один зомби скрипта
        pass
    proc.wait(timeout=5)
    for name, pid in _fake_steps(proc.steps_path):  # type: ignore[attr-defined]
        if name == "maintenance-cleanup":  # прочие подставные шаги выходят сами
            _kill_quietly(pid)


def test_hung_step_is_cut_and_the_cycle_goes_on(tmp_path):
    proc = _scheduler(tmp_path)
    try:
        # До правки maintenance-cleanup висел бы 300 с, и discover-rss не начался бы.
        assert _wait(lambda: "Cycle finished" in proc.out_path.read_text(), 20), proc.out_path.read_text()
        names = [name for name, _pid in _fake_steps(proc.steps_path)]
        assert names[:3] == ["maintenance-cleanup", "discover-rss", "parse"]
        assert names[-2:] == ["check-lanes", "stats"]
        hung_pid = _fake_steps(proc.steps_path)[0][1]
        assert not _alive(hung_pid)
        output = proc.out_path.read_text()
        assert "TIMEOUT maintenance-cleanup" in output
        assert "FAIL maintenance-cleanup exit=124" in output
        assert "OK discover-rss" in output
    finally:
        _cleanup(proc)


def test_failed_step_reports_its_real_exit_code(tmp_path):
    """`code="$?"` сразу после `if ...; fi` давал 0: по POSIX так выходит `if` без ветки.
    Лог писал «FAIL … exit=0», и run_required_step не останавливал скрипт никогда."""
    script = (ROOT / "scripts" / "docker-scheduler.sh").read_text()
    defs = script[script.index("log() {"):script.index("CYCLE_INTERVAL_SECONDS=")]
    result = subprocess.run(
        ["sh", "-c", defs + '\nrun_step fake sh -c "exit 3"; echo "returned=$?"'],
        cwd=str(ROOT),
        env={**_fake_python_env(tmp_path), "STEP_TIMEOUT_SECONDS": "30", "STEP_KILL_AFTER_SECONDS": "1"},
        capture_output=True, text=True, timeout=30,
    )
    assert "FAIL fake exit=3" in result.stdout, result.stdout + result.stderr
    assert "returned=3" in result.stdout


def test_bad_step_timeout_does_not_stop_the_steps(tmp_path):
    """Сторож с кривым сроком шаг не запускает: опечатка в STEP_TIMEOUT_SECONDS (1h) без
    проверки в скрипте остановила бы каждый шаг каждого цикла — тот же простой, что 24.09."""
    proc = _scheduler(tmp_path, HANG_STEP="", STEP_TIMEOUT_SECONDS="1h")
    try:
        assert _wait(lambda: "Cycle finished" in proc.out_path.read_text(), 20), proc.out_path.read_text()
        output = proc.out_path.read_text()
        assert "STEP_TIMEOUT_SECONDS=«1h»" in output
        assert [name for name, _pid in _fake_steps(proc.steps_path)][:2] == ["maintenance-cleanup", "discover-rss"]
        assert "FAIL" not in output
    finally:
        _cleanup(proc)


def test_container_stop_still_reaches_the_step(tmp_path):
    """scheduler-lock гасит скрипт сигналом всей его группе процессов (singleton._signal_child).
    coreutils `timeout` без --foreground увёл бы шаг в свою группу — и остановка контейнера
    до шага не дошла бы. Сторож оставляет шаг в группе скрипта."""
    proc = _scheduler(tmp_path, STEP_TIMEOUT_SECONDS="300")
    try:
        assert _wait(lambda: len(_fake_steps(proc.steps_path)) == 1, 15)
        step_pid = _fake_steps(proc.steps_path)[0][1]
        singleton._signal_child(proc, signal.SIGTERM)
        assert _wait(lambda: not _alive(step_pid), 5)
    finally:
        _cleanup(proc)


def _guard(*args: str, timeout: float = 30) -> tuple[int, float, str]:
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "oiltech_digest.step_timeout", *args],
        cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
    )
    return result.returncode, time.monotonic() - started, result.stdout + result.stderr


def test_guard_passes_the_step_exit_code_through():
    assert _guard("30", "1", "--", sys.executable, "-c", "raise SystemExit(0)")[0] == 0
    assert _guard("30", "1", "--", sys.executable, "-c", "raise SystemExit(3)")[0] == 3
    killed = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    assert _guard("30", "1", "--", sys.executable, "-c", killed)[0] == 128 + signal.SIGKILL


def test_guard_stops_a_step_past_the_deadline_with_124():
    code, seconds, output = _guard("1", "5", "--", sys.executable, "-c", "import time; time.sleep(60)")
    assert code == 124
    assert seconds < 5  # SIGTERM снял шаг сразу, до SIGKILL дело не дошло
    assert "SIGTERM" in output and "SIGKILL" not in output


def test_guard_kills_a_step_that_ignores_sigterm():
    ignores = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    code, seconds, output = _guard("1", "1", "--", sys.executable, "-c", ignores)
    assert code == 124
    assert 2 <= seconds < 10
    assert "SIGKILL" in output


def test_zero_deadline_runs_the_step_without_a_ceiling():
    code, _seconds, _output = _guard("0", "1", "--", sys.executable, "-c", "import time; time.sleep(1.5); raise SystemExit(5)")
    assert code == 5


def test_services_that_launch_chromium_reap_zombies():
    """25.09 под PID 1 планировщика было 2259 зомби chrome при pids.max 2315 (DefaultTasksMax
    systemd — 15% от threads-max): каждый рендер оставлял ~2 процесса, которые python под
    PID 1 не подбирает. init (tini) подбирает осиротевших, и слоты не кончаются."""
    core = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name in ("scheduler", _app_service(core), "worker", "playwright-worker"):
        assert core[name].get("init") is True, name
    nl = yaml.safe_load((ROOT / "docker-compose.external-worker.yml").read_text())["services"]
    assert nl["external-worker-browser"].get("init") is True


@pytest.mark.parametrize("value", ["", "abc", "-1"])
def test_guard_rejects_a_bad_deadline(value):
    code, _seconds, output = _guard(value, "1", "--", "true")
    assert code == 2
