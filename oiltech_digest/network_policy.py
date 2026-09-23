"""Routing rules for local and future external job execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from oiltech_digest import config


@dataclass(frozen=True)
class ExecutionDecision:
    queue_name: str
    execution_region: str
    capability: str
    reason: str


def route_digest_export(export_format: str) -> ExecutionDecision:
    if export_format == "pdf":
        return ExecutionDecision("playwright", "ru", "playwright", "pdf_export")
    return ExecutionDecision("default", "ru", "default", "file_export")


def route_ai_processing() -> ExecutionDecision:
    if config.EXTERNAL_WORKERS_ENABLED and config.AI_EXECUTION_REGION == "external":
        return ExecutionDecision("external-ai", "external", "openai", "ai_external_enabled")
    return ExecutionDecision("ai", "ru", "openai", "ai_local")


def route_ai_bulk() -> ExecutionDecision:
    """Пересчёт корпуса — своя полоса, если она включена; иначе как поток дня."""
    decision = route_ai_processing()
    if decision.queue_name == "external-ai" and config.AI_BULK_LANE_ENABLED:
        return ExecutionDecision("external-ai-bulk", "external", "openai", "ai_external_bulk")
    return decision


def route_source_task(source: dict[str, Any], *, task_kind: str) -> ExecutionDecision:
    strategy = str(source.get("parse_strategy") or "").strip().lower()
    network_region = str(source.get("network_region") or "auto").strip().lower()
    network_profile = str(source.get("network_profile") or "direct").strip().lower()
    capability = "playwright" if strategy == "playwright" or network_profile == "browser" else "http_fetch"

    wants_external = network_region == "external"
    can_use_external = config.EXTERNAL_WORKERS_ENABLED and config.FETCH_EXTERNAL_ENABLED
    # Диагностику внешний воркер не исполняет (lanes.py): с 17.09 (гео-маршрут включён)
    # кнопка для западного источника ставила задачу, которая падала на NL с
    # «Unsupported external job kind». Пока — взгляд с РФ-ядра, как до 17.09.
    if task_kind == "diagnose":
        wants_external = False
    if wants_external and can_use_external:
        queue_name = "external-playwright" if capability == "playwright" else "external-fetch"
        return ExecutionDecision(queue_name, "external", capability, f"{task_kind}_source_external")

    if capability == "playwright":
        return ExecutionDecision("playwright", "ru", capability, f"{task_kind}_local_playwright")
    return ExecutionDecision("default", "ru", capability, f"{task_kind}_local_default")

