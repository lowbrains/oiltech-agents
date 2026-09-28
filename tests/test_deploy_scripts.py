"""Скрипты выката (сессия C, п. 5): порядок шагов и отказы — настоящим sh.

docker, git и sleep подменены заглушками в PATH: каждая пишет, чем её позвали, и отвечает
так, как ответил бы сервер. Так проверяется весь ход выката, а не только синтаксис.
Каждый тест падает на коде до правки (скриптов не было)."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from oiltech_digest import lanes

ROOT = Path(__file__).resolve().parents[1]

DOCKER = r"""#!/bin/sh
echo "docker $*" >> "$FAKE_LOG"
case "$*" in
  *"live-ai-leases"*) [ "${FAKE_LIVE:-0}" = 0 ] || echo "live-ai-leases: задача 4711 process_articles [external-ai]"; exit "${FAKE_LIVE:-0}" ;;
  *"worker-versions --self"*) exit "${FAKE_CHECKIN:-0}" ;;
  *"worker-versions --help"*) exit "${FAKE_OLD:-0}" ;;
  "compose ps -q "*) echo "cid-$4" ;;
  "inspect -f "*) echo "running healthy" ;;
  "ps --format "*) [ -z "${FAKE_PS:-}" ] || echo "$FAKE_PS" ;;
esac
exit 0
"""
GIT = r"""#!/bin/sh
echo "git $*" >> "$FAKE_LOG"
case "$1 $2" in
  "rev-parse -q") [ -n "${FAKE_BASE:-}" ] || exit 1; echo "$FAKE_BASE" ;;
  "rev-parse "*) echo abc1234 ;;
  "diff "*) exit "${FAKE_SCHEMA_CHANGED:-0}" ;;
esac
exit 0
"""
SLEEP = "#!/bin/sh\nexit 0\n"


def _repo(tmp_path: Path, script: str, *, env_files: tuple[str, ...]) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / script, repo / "scripts" / script)
    for name in env_files:
        (repo / name).write_text("")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (("docker", DOCKER), ("git", GIT), ("sleep", SLEEP)):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    return repo


def _run(tmp_path: Path, repo: Path, script: str, *args: str, **env: str):
    log = tmp_path / "calls.log"
    result = subprocess.run(
        ["sh", str(repo / "scripts" / script), *args],
        cwd=str(tmp_path),  # не из каталога репозитория: скрипт обязан сам найти корень
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "FAKE_LOG": str(log),
             "FAKE_BASE": "base123", **env},
        capture_output=True, text=True, timeout=60,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def _index(calls: list[str], needle: str) -> int:
    return next(index for index, call in enumerate(calls) if needle in call)


# --- Ядро ----------------------------------------------------------------------------------


def test_core_deploy_runs_steps_in_safe_order(tmp_path):
    repo = _repo(tmp_path, "deploy-core.sh", env_files=(".env",))

    result, calls = _run(tmp_path, repo, "deploy-core.sh", "app", "scheduler")

    assert result.returncode == 0, result.stdout + result.stderr
    order = [
        "git fetch", "git reset --hard --quiet origin/main", "docker compose build app scheduler",
        "cli live-ai-leases", "docker compose up -d --no-deps app scheduler", "cli check-lanes",
        "git update-ref refs/deploy/core HEAD",  # база для следующего выката — только в конце
    ]
    positions = [_index(calls, step) for step in order]
    assert positions == sorted(positions), calls
    # Сервисы названы явно; ни сидов, ни схемы, раз schema.sql не менялся (ревью 23.09).
    assert not any(" up " in call and "--no-deps" not in call for call in calls)
    assert not any("seed" in call or "init-db" in call for call in calls)


def test_core_deploy_refuses_to_guess_when_schema_changed(tmp_path):
    """init-db на живой базе — одна транзакция с бэкфиллами и эксклюзивными замками на
    articles и background_jobs. Молча его не гоняем и молча не пропускаем."""
    repo = _repo(tmp_path, "deploy-core.sh", env_files=(".env",))

    result, calls = _run(tmp_path, repo, "deploy-core.sh", "app", FAKE_SCHEMA_CHANGED="1")

    assert result.returncode != 0
    assert "--schema" in result.stdout and "--no-schema" in result.stdout
    assert not any("compose build" in call for call in calls)  # отказ до сборки


def test_core_deploy_first_run_needs_explicit_schema_choice(tmp_path):
    """Первый выкат скриптом: что уже на базе, неизвестно — угадывать нельзя."""
    repo = _repo(tmp_path, "deploy-core.sh", env_files=(".env",))

    refused, calls = _run(tmp_path, repo, "deploy-core.sh", "app", FAKE_BASE="")
    assert refused.returncode != 0 and "первый выкат" in refused.stdout
    assert not any("compose build" in call for call in calls)

    (tmp_path / "calls.log").unlink()
    done, calls = _run(tmp_path, repo, "deploy-core.sh", "--no-schema", "app", FAKE_BASE="")
    assert done.returncode == 0, done.stdout + done.stderr
    assert calls[-1] == "git update-ref refs/deploy/core HEAD"


def test_core_deploy_does_not_record_a_failed_rollout(tmp_path):
    repo = _repo(tmp_path, "deploy-core.sh", env_files=(".env",))

    result, calls = _run(tmp_path, repo, "deploy-core.sh", "app", FAKE_LIVE="3")

    assert result.returncode != 0
    assert not any("update-ref" in call for call in calls)


def test_core_deploy_runs_schema_only_when_asked_and_after_the_guard(tmp_path):
    repo = _repo(tmp_path, "deploy-core.sh", env_files=(".env",))

    result, calls = _run(tmp_path, repo, "deploy-core.sh", "--schema", "app", FAKE_SCHEMA_CHANGED="1")

    assert result.returncode == 0, result.stdout + result.stderr
    guard, schema, up = (_index(calls, step) for step in ("cli live-ai-leases", "cli init-db", "up -d --no-deps app"))
    assert guard < schema < up


def test_core_deploy_refuses_while_ai_job_is_running(tmp_path):
    repo = _repo(tmp_path, "deploy-core.sh", env_files=(".env",))

    result, calls = _run(tmp_path, repo, "deploy-core.sh", "app", FAKE_LIVE="3")

    assert result.returncode != 0
    assert "ИИ-задачи в работе" in result.stdout
    assert not any("compose up" in call for call in calls)


def test_core_deploy_force_goes_on_and_keeps_flags_after_update(tmp_path):
    """Флаги переживают перезапуск скрипта новой версией после git reset."""
    repo = _repo(tmp_path, "deploy-core.sh", env_files=(".env",))

    result, calls = _run(tmp_path, repo, "deploy-core.sh", "--force", "--no-schema", "app", FAKE_LIVE="3")

    assert result.returncode == 0, result.stdout + result.stderr
    assert any("compose up -d --no-deps app" in call for call in calls)
    assert not any("init-db" in call for call in calls)


@pytest.mark.parametrize(("args", "env_files", "reason"), [
    (("db",), (".env",), "не выкатывается"),
    (("bootstrap",), (".env",), "не выкатывается"),
    (("app",), (), "нет .env"),
    (("app",), (".env", ".env.external-worker"), "это NL"),
])
def test_core_deploy_refuses_wrong_target(tmp_path, args, env_files, reason):
    repo = _repo(tmp_path, "deploy-core.sh", env_files=env_files)

    result, calls = _run(tmp_path, repo, "deploy-core.sh", *args)

    assert result.returncode != 0
    assert reason in result.stdout
    assert calls == []  # ни git, ни docker не тронуты


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=deploy-test@example.com", "-c", "user.name=deploy-test", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_core_deploy_schema_check_survives_retry_and_manual_reset(tmp_path):
    """Повторное ревью 23.09: база сравнения схемы была HEAD до reset — второй запуск той же
    командой после отказа (и ручной reset перед скриптом, как в инструкции первого выката)
    видел «без изменений» и выкатывал новый код на старую схему. Здесь настоящий git:
    bare-origin, серверный клон, смена schema.sql между выкатами."""
    origin, work, server = tmp_path / "origin.git", tmp_path / "work", tmp_path / "server"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    (work / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "deploy-core.sh", work / "scripts" / "deploy-core.sh")
    schema = work / "oiltech_digest" / "db" / "schema.sql"
    schema.parent.mkdir(parents=True)
    schema.write_text("-- v1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "v1")
    _git(work, "push", "origin", "HEAD:main")
    _git(tmp_path, "clone", str(origin), str(server))
    (server / ".env").write_text("")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (("docker", DOCKER), ("sleep", SLEEP)):  # git — настоящий
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)

    def deploy(*flags: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(server / "scripts" / "deploy-core.sh"), *flags, "app"], cwd=str(tmp_path),
            env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "FAKE_LOG": str(tmp_path / "calls.log")},
            capture_output=True, text=True, timeout=60,
        )

    first_rollout = deploy("--no-schema")  # первый выкат скриптом: выбор явный
    assert first_rollout.returncode == 0, first_rollout.stdout + first_rollout.stderr
    schema.write_text("-- v2: новая таблица\n")
    _git(work, "commit", "-am", "v2")
    _git(work, "push", "origin", "HEAD:main")

    refused, retried = deploy(), deploy()  # отказ — и та же команда ещё раз
    assert refused.returncode != 0 and retried.returncode != 0
    assert "schema.sql изменился" in retried.stdout
    _git(server, "reset", "--hard", "origin/main")  # ручной reset перед скриптом
    assert deploy().returncode != 0
    assert deploy("--no-schema").returncode == 0  # таблицы созданы вручную — выбор явный
    assert deploy().returncode == 0  # теперь схема та же, что у последнего выката


def test_core_guard_covers_every_ai_lane():
    """Выкат не рвёт ИИ ни в одной полосе; новая полоса защищена по умолчанию."""
    assert lanes.AI_LANES == set(lanes.EXTERNAL_LANES) - {lanes.FETCH, lanes.BROWSER}
    assert {lanes.AI_LIVE, lanes.AI_BULK} <= lanes.AI_LANES


# --- NL ------------------------------------------------------------------------------------


# Список — из compose, а не копией: при слиянии в агентный репозиторий пятый воркер
# (nl-agents-1) пришёл в compose, а скрипт по умолчанию выкатывал бы четыре (ревью 27.09).
NL_SERVICES = list(yaml.safe_load((ROOT / "docker-compose.external-worker.yml").read_text())["services"])


def test_nl_deploy_script_lists_exactly_the_compose_workers():
    """В обе стороны: лишний воркер в скрипте, которого нет в compose, прежний тест пропускал
    (строку сборки он искал подстрокой). Порядок сравниваем тоже: сборке он безразличен (у всех
    один build), но это порядок перезапуска по одному — тест ниже проверяет его по compose."""
    script = (ROOT / "scripts" / "deploy-nl.sh").read_text()
    declared = re.findall(r'^ALL_SERVICES="([^"]*)"$', script, re.M)

    assert len(declared) == 1, "в deploy-nl.sh должна быть ровно одна строка ALL_SERVICES=\"...\""
    assert declared[0].split() == NL_SERVICES


def test_nl_deploy_restarts_workers_one_by_one_after_checkin(tmp_path):
    repo = _repo(tmp_path, "deploy-nl.sh", env_files=(".env.external-worker",))

    result, calls = _run(tmp_path, repo, "deploy-nl.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    build = _index(calls, "compose -f docker-compose.external-worker.yml build ")
    assert calls[build] == "docker compose -f docker-compose.external-worker.yml build " + " ".join(NL_SERVICES)
    previous = build
    for service in NL_SERVICES:
        up = _index(calls, f"up -d --no-deps {service}")
        checkin = _index(calls, f"exec -T {service} python -m oiltech_digest.cli worker-versions --self --expect-build abc1234")
        assert previous < up < checkin  # следующий — только после отметки предыдущего
        previous = checkin
    assert "worker-versions" in calls[-1] and "--self" not in calls[-1]


def test_nl_deploy_does_not_wait_two_minutes_for_old_worker_that_ignores_sigterm(tmp_path):
    """Старый воркер (python под PID 1 без обработчика) SIGTERM игнорирует: 120 с ожидания
    ничего не дали бы — тот же SIGKILL. Новый получает весь срок, чтобы вернуть задачи."""
    repo = _repo(tmp_path, "deploy-nl.sh", env_files=(".env.external-worker",))

    _, old_calls = _run(tmp_path, repo, "deploy-nl.sh", "external-worker", FAKE_OLD="1")
    (tmp_path / "calls.log").unlink()
    _, new_calls = _run(tmp_path, repo, "deploy-nl.sh", "external-worker", FAKE_OLD="0")

    assert any("up -d --no-deps --timeout 10 external-worker" in call for call in old_calls)
    assert any(call.endswith("up -d --no-deps external-worker") for call in new_calls)


def test_nl_deploy_stops_at_first_worker_that_does_not_check_in(tmp_path):
    repo = _repo(tmp_path, "deploy-nl.sh", env_files=(".env.external-worker",))

    result, calls = _run(tmp_path, repo, "deploy-nl.sh", FAKE_CHECKIN="1", DEPLOY_NL_CHECKIN_TIMEOUT="10")

    assert result.returncode != 0
    assert "external-worker не отметился" in result.stdout
    assert not any("up -d --no-deps external-worker-bulk" in call for call in calls)


@pytest.mark.parametrize(("env_files", "ps", "reason"), [
    ((), "", "это не NL"),
    ((".env.external-worker",), "oiltech_app", "это РФ-ядро"),
])
def test_nl_deploy_refuses_on_wrong_host(tmp_path, env_files, ps, reason):
    repo = _repo(tmp_path, "deploy-nl.sh", env_files=env_files)

    result, calls = _run(tmp_path, repo, "deploy-nl.sh", FAKE_PS=ps)

    assert result.returncode != 0
    assert reason in result.stdout
    assert not any(call.startswith(("git ", "docker compose")) for call in calls)
