"""HTTP-pull worker for the non-RU execution contour."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from oiltech_digest import config
from oiltech_digest.ingestion import external_fetch
from oiltech_digest.documents import external as documents_external
from oiltech_digest.processing import external_ai

logger = logging.getLogger(__name__)


def run_loop(
    *,
    core_api_url: str | None = None,
    token: str | None = None,
    worker_id: str | None = None,
    queues: list[str] | None = None,
    capabilities: list[str] | None = None,
    poll_seconds: float | None = None,
    once: bool = False,
) -> None:
    client = ExternalWorkerClient(
        core_api_url=core_api_url or config.CORE_API_URL,
        token=token or config.EXTERNAL_WORKER_TOKEN,
        worker_id=worker_id or config.EXTERNAL_WORKER_ID,
        queues=queues or config.EXTERNAL_WORKER_QUEUES,
        capabilities=capabilities or config.EXTERNAL_WORKER_CAPABILITIES,
    )
    sleep_seconds = config.EXTERNAL_WORKER_POLL_SECONDS if poll_seconds is None else poll_seconds
    while True:
        job = client.claim()
        if job is None:
            if once:
                return
            time.sleep(sleep_seconds)
            continue
        _handle_job(client, job)


class ExternalWorkerClient:
    def __init__(
        self,
        *,
        core_api_url: str,
        token: str,
        worker_id: str,
        queues: list[str],
        capabilities: list[str],
    ) -> None:
        if not core_api_url:
            raise ValueError("CORE_API_URL is required for external-worker")
        if not token:
            raise ValueError("EXTERNAL_WORKER_TOKEN is required for external-worker")
        self.core_api_url = core_api_url.rstrip("/")
        self.worker_id = worker_id
        self.queues = queues
        self.capabilities = capabilities
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def claim(self) -> dict[str, Any] | None:
        response = self.session.post(
            f"{self.core_api_url}/api/external-worker/claim",
            json={
                "worker_id": self.worker_id,
                "queues": self.queues,
                "capabilities": self.capabilities,
                "max_lease_seconds": config.EXTERNAL_WORKER_DEFAULT_LEASE_SECONDS,
            },
            timeout=30,
        )
        response.raise_for_status()
        return (response.json() or {}).get("job")

    def progress(self, job: dict[str, Any], progress: float) -> None:
        response = self.session.post(
            f"{self.core_api_url}/api/external-worker/jobs/{job['id']}/progress",
            json={"lease_token": job["lease_token"], "progress": progress},
            timeout=30,
        )
        response.raise_for_status()

    def heartbeat(self, job: dict[str, Any]) -> None:
        """Продлить lease задачи (без lease_seconds core берёт дефолт 600с)."""
        response = self.session.post(
            f"{self.core_api_url}/api/external-worker/jobs/{job['id']}/heartbeat",
            json={"lease_token": job["lease_token"]},
            timeout=30,
        )
        response.raise_for_status()

    def complete(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        response = self.session.post(
            f"{self.core_api_url}/api/external-worker/jobs/{job['id']}/complete",
            json={"lease_token": job["lease_token"], "result": result},
            timeout=60,
        )
        response.raise_for_status()

    def fail(self, job: dict[str, Any], error: str, *, retryable: bool = True, retry_after_seconds: int = 300) -> None:
        response = self.session.post(
            f"{self.core_api_url}/api/external-worker/jobs/{job['id']}/fail",
            json={
                "lease_token": job["lease_token"],
                "error": error[:1000],
                "retryable": retryable,
                "retry_after_seconds": retry_after_seconds,
            },
            timeout=30,
        )
        response.raise_for_status()


def _safe_heartbeat(client: "ExternalWorkerClient", job: dict[str, Any]) -> None:
    try:
        client.heartbeat(job)
    except requests.HTTPError as exc:
        # 409 = core отозвал lease (задача возвращена в очередь). Это НЕ временный сбой:
        # результат уже не примут, поэтому батч прерываем немедленно, а не платим OpenAI
        # за мусор. Прочие HTTP-ошибки по-прежнему считаем временными.
        if exc.response is not None and exc.response.status_code == 409:
            logger.warning("external_lease_lost job_id=%s — прерываю обработку", job.get("id"))
            raise external_ai.LeaseLost(f"lease lost for job {job.get('id')}") from exc
        logger.warning("external_heartbeat_failed job_id=%s", job.get("id"))
    except Exception:  # noqa: BLE001 - сбой heartbeat не должен прерывать обработку
        logger.warning("external_heartbeat_failed job_id=%s", job.get("id"))


def _handle_job(client: ExternalWorkerClient, job: dict[str, Any]) -> None:
    logger.info("external_job_started job_id=%s kind=%s queue=%s", job["id"], job.get("kind"), job.get("queue"))
    try:
        if job.get("kind") == "process_articles":
            client.progress(job, 20)
            # Heartbeat по каждой статье продлевает lease — большой батч на медленной
            # модели (gpt-5.5) больше не истекает по lease и не уходит в ретрай-петлю.
            result = external_ai.process_payload(
                job.get("payload") or {},
                heartbeat=lambda: _safe_heartbeat(client, job),
            )
            client.progress(job, 90)
            client.complete(job, result)
        elif job.get("kind") == "recheck_relevance":
            client.progress(job, 20)
            result = external_ai.process_recheck_payload(
                job.get("payload") or {},
                heartbeat=lambda: _safe_heartbeat(client, job),
            )
            client.progress(job, 90)
            client.complete(job, result)
        elif job.get("kind") == "translate_titles":
            client.progress(job, 20)
            result = external_ai.process_translate_payload(
                job.get("payload") or {},
                heartbeat=lambda: _safe_heartbeat(client, job),
            )
            client.progress(job, 90)
            client.complete(job, result)
        elif job.get("kind") == "source_candidate_evaluate":
            client.progress(job, 20)
            result = external_ai.process_source_candidate_payload(
                job.get("payload") or {},
                heartbeat=lambda: _safe_heartbeat(client, job),
            )
            client.progress(job, 90)
            client.complete(job, result)
        elif job.get("kind") == "process_document":
            client.progress(job, 20)
            result = documents_external.process_document_payload(
                job.get("payload") or {},
                heartbeat=lambda: _safe_heartbeat(client, job),
            )
            client.progress(job, 90)
            client.complete(job, result)
        elif job.get("kind") == "scrape_source":
            client.progress(job, 20)
            result = external_fetch.process_payload(job.get("payload") or {})
            client.progress(job, 90)
            client.complete(job, result)
        elif job.get("kind") == "signal_discovery":
            # Радар: снимок базы приходит в payload, сигналы пишет ядро при complete.
            from oiltech_digest import signal_discovery

            client.progress(job, 20)
            result = signal_discovery.process_external_payload(
                job.get("payload") or {},
                heartbeat=lambda: _safe_heartbeat(client, job),
            )
            client.progress(job, 90)
            client.complete(job, result)
        else:
            raise ValueError(f"Unsupported external job kind: {job.get('kind')}")
        logger.info("external_job_finished job_id=%s kind=%s", job["id"], job.get("kind"))
    except external_ai.LeaseLost:
        # Ни complete, ни fail слать нельзя — оба вернут 409. Просто выходим: задача уже
        # в очереди у core и будет выдана заново (возможно, этому же воркеру).
        logger.warning("external_job_abandoned job_id=%s — lease отозван, беру следующую", job.get("id"))
        return
    except Exception as exc:  # noqa: BLE001 - external failures must be returned to core
        logger.exception("external_job_failed job_id=%s kind=%s", job.get("id"), job.get("kind"))
        try:
            client.fail(job, str(exc), retryable=True)
        except Exception:
            logger.exception("external_job_fail_report_failed job_id=%s", job.get("id"))
