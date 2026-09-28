"""Контракт версий РФ↔NL (сессия C, п. 3).

18.09 и 21.09 воркер старой сборки падал на итоге новой формы, а «пересобран ли NL» узнавали
по косвенному полю skipped_known. Теперь воркер при выдаче сообщает сборку и номер
контракта, ядро помнит их по каждому контейнеру NL, а check-lanes и экран обслуживания
показывают версии и тревогу contract_mismatch.

Каждый тест падает на коде до правки."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from oiltech_digest import api, cli, contract, external_worker, lanes
from oiltech_digest.db import connection, repository

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _core(monkeypatch) -> TestClient:
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    return TestClient(api.app)


def _claim(core: TestClient, worker_id: str, queues: list[str], headers: dict | None = None):
    response = core.post(
        "/api/external-worker/claim",
        headers={"Authorization": "Bearer secret", **(headers or {})},
        json={"worker_id": worker_id, "queues": queues, "capabilities": ["openai"]},
    )
    assert response.status_code == 200
    return response


def _current(build: str = "abc1234") -> dict:
    return {contract.HEADER_BUILD: build, contract.HEADER_CONTRACT: str(contract.CONTRACT)}


def test_claim_records_build_and_contract_per_container(isolated_db, monkeypatch):
    core = _core(monkeypatch)

    _claim(core, "nl-fetch-1#2", ["external-fetch"], _current("f00d123"))
    _claim(core, "nl-ai-1", ["external-ai"], _current("abc1234"))

    consumers = {row["consumer"]: row for row in repository.list_external_consumers()}
    assert set(consumers) == {"nl-fetch-1", "nl-ai-1"}  # потребитель — контейнер, а не поток
    assert consumers["nl-fetch-1"]["build"] == "f00d123"
    assert consumers["nl-fetch-1"]["contract"] == contract.CONTRACT
    assert consumers["nl-fetch-1"]["queues"] == ["external-fetch"]


def test_old_worker_without_contract_raises_alert(isolated_db, monkeypatch):
    """Воркер сборки до контракта заголовков не шлёт — ровно такой NL будет сразу после
    выката ядра, и сторож обязан это показать, а не молчать до первой поломки."""
    core = _core(monkeypatch)
    _claim(core, "nl-ai-1", ["external-ai"])  # старый воркер: без заголовков
    _claim(core, "nl-browser-1", ["external-playwright"], _current())

    alerts = [alert for alert in repository.external_queue_status()["alerts"] if alert["kind"] == "contract_mismatch"]

    assert [alert["consumer"] for alert in alerts] == ["nl-ai-1"]
    assert "пересобрать" in alerts[0]["message"]


def test_worker_with_other_contract_number_raises_alert():
    status = {"contract": 2, "consumers": [
        {"consumer": "nl-ai-1", "queues": ["external-ai"], "build": "abc", "contract": 1, "last_seen_at": NOW},
        {"consumer": "nl-fetch-1", "queues": ["external-fetch"], "build": "abc", "contract": 2, "last_seen_at": NOW},
    ]}

    alerts = lanes.lane_alerts(status, now=NOW)

    assert [(alert["kind"], alert["consumer"]) for alert in alerts] == [("contract_mismatch", "nl-ai-1")]


def test_long_gone_consumer_is_listed_but_not_alarmed():
    """Переименованный или снятый контейнер не должен звенеть вечно: его место в очереди
    сторож и так видит как «нет живого потребителя»."""
    gone = NOW - timedelta(hours=lanes.CONSUMER_ACTIVE_HOURS + 1)
    status = {"contract": 2, "consumers": [
        {"consumer": "external-worker-1", "queues": ["external-ai"], "build": None, "contract": None, "last_seen_at": gone},
    ]}

    assert lanes.lane_alerts(status, now=NOW) == []


def test_worker_reports_its_build_and_contract(monkeypatch):
    monkeypatch.setattr(external_worker.config, "OILTECH_BUILD", "abc1234")
    client = external_worker.ExternalWorkerClient(core_api_url="https://core.example", token="t", worker_id="nl-ai-1",
                                                  queues=["external-ai"], capabilities=["openai"])

    headers = client.fork().session.headers  # и фоновое продление аренды шлёт то же

    assert headers[contract.HEADER_BUILD] == "abc1234"
    assert headers[contract.HEADER_CONTRACT] == str(contract.CONTRACT)


def test_check_lanes_prints_versions_of_every_consumer(isolated_db, monkeypatch, capsys):
    core = _core(monkeypatch)
    for worker_id, queue in (("nl-ai-1", "external-ai"), ("nl-ai-bulk-1", "external-ai-bulk"),
                             ("nl-fetch-1#1", "external-fetch"), ("nl-browser-1", "external-playwright")):
        _claim(core, worker_id, [queue], _current("abc1234"))

    cli.cmd_check_lanes(type("Args", (), {})())

    out = capsys.readouterr().out
    for consumer in ("nl-ai-1", "nl-ai-bulk-1", "nl-fetch-1", "nl-browser-1"):
        assert f"{consumer} " in out
    assert out.count("abc1234") == 4
    assert f"контракт ядра {contract.CONTRACT}" in out
    assert "check-lanes: ok" in out


def test_check_lanes_fails_on_contract_mismatch(isolated_db, monkeypatch, capsys):
    core = _core(monkeypatch)
    _claim(core, "nl-ai-1", ["external-ai"])

    with pytest.raises(SystemExit) as exit_info:
        cli.cmd_check_lanes(type("Args", (), {})())

    assert exit_info.value.code == 2
    assert "ТРЕВОГА" in capsys.readouterr().out


def test_nl_can_read_consumer_versions_with_worker_token(isolated_db, monkeypatch):
    """На NL нет базы: версии потребителей скрипт выката читает у ядра по токену воркера."""
    core = _core(monkeypatch)
    _claim(core, "nl-ai-1", ["external-ai"], _current("abc1234"))

    denied = core.get("/api/external-worker/consumers")
    allowed = core.get("/api/external-worker/consumers", headers={"Authorization": "Bearer secret"})

    assert denied.status_code == 401
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["contract"] == contract.CONTRACT
    assert [row["consumer"] for row in body["consumers"]] == ["nl-ai-1"]


@pytest.mark.parametrize(("build", "number", "code"), [
    ("abc1234", contract.CONTRACT, 0),
    ("old0000", contract.CONTRACT, 1),  # контейнер ещё не пересобран
    ("abc1234", contract.CONTRACT - 1, 1),
])
def test_worker_versions_self_check(monkeypatch, capsys, build, number, code):
    now = datetime.now(timezone.utc)
    row = {"consumer": "nl-ai-1", "queues": ["external-ai"], "build": build, "contract": number,
           "last_seen_at": now.isoformat()}
    row["mismatch"] = lanes.consumer_mismatch(row, contract.CONTRACT, now=now)  # как считает ядро
    monkeypatch.setattr(cli, "_fetch_consumer_versions", lambda: {"contract": contract.CONTRACT, "consumers": [row]})
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_ID", "nl-ai-1")

    args = type("Args", (), {"self_check": True, "expect_build": "abc1234"})()
    if code:
        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_worker_versions(args)
        assert exit_info.value.code == code
    else:
        cli.cmd_worker_versions(args)
    assert "nl-ai-1" in capsys.readouterr().out


def test_screen_and_nl_get_the_same_mismatch_flag_as_the_watchdog(isolated_db):
    """Ревью 23.09: экран красил расхождение без окна «живой за 6 ч» — переименованный
    контейнер горел бы вечно. Флаг считает ядро одним правилом с тревогами сторожа."""
    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO external_worker_consumers (consumer, queues, build, contract, last_seen_at) VALUES "
            "('nl-ai-1', '{external-ai}', NULL, NULL, now()), "
            "('external-worker-1', '{external-ai}', NULL, NULL, now() - interval '7 hours'), "
            "('nl-fetch-1', '{external-fetch}', 'abc1234', %s, now())",
            (contract.CONTRACT,),
        )
        conn.commit()

    flags = {row["consumer"]: row["mismatch"] for row in repository.external_consumers_status()["consumers"]}
    alarmed = {alert["consumer"] for alert in repository.external_queue_status()["alerts"]
               if alert["kind"] == "contract_mismatch"}

    assert flags == {"nl-ai-1": True, "external-worker-1": False, "nl-fetch-1": False}
    assert alarmed == {name for name, flag in flags.items() if flag}


def test_nl_images_carry_git_build():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "ARG GIT_SHA" in dockerfile and "OILTECH_BUILD" in dockerfile
    services = yaml.safe_load((ROOT / "docker-compose.external-worker.yml").read_text())["services"]
    for name, service in services.items():
        assert (service["build"].get("args") or {}).get("GIT_SHA"), name


def test_consumers_table_exists_after_init_db(isolated_db):
    with connection.get_connection() as conn:
        columns = {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'external_worker_consumers' "
            "AND table_schema = current_schema()").fetchall()}
    assert {"consumer", "queues", "build", "contract", "first_seen_at", "last_seen_at"} <= columns
