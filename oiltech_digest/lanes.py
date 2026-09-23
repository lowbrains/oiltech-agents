"""Полосы исполнения внешнего контура: какая очередь какие задачи обслуживает.

Одна таблица вместо строк, разбросанных по коду. По ней:
- ядро отказывает в постановке задачи, которую полоса не обслуживает, — сразу, а не
  «Unsupported external job kind» у воркера через час (радар 13–17.09: 134 запуска
  в очередь, где его никто не умел исполнять);
- собирается payload при выдаче: новая очередь `external-ai-bulk` получает те же
  данные, что `external-ai`, а не сырой payload без статей;
- сторож сверяет очереди с порогами ожидания (208 задач без потребителя 20.09 было
  видно только по последствиям через 8,5 ч).

Разбиение по назначению (замер 18–21.09): мощности хватает, ломает изоляция — 18.09
28 пересчётов по ~9 мин держали ИИ-поток дня 4,5 ч. Поэтому пересчёты корпуса — своя
полоса, а сбор браузером — отдельно от сбора запросом.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

AI_LIVE = "external-ai"
AI_BULK = "external-ai-bulk"
FETCH = "external-fetch"
BROWSER = "external-playwright"
AGENTS = "external-agents"

_AI_KINDS = frozenset({"process_articles", "recheck_relevance", "translate_titles", "process_document", "reprint_review"})
_FETCH_KINDS = frozenset({"scrape_source", "refetch_text"})
_AGENT_KINDS = frozenset({"signal_discovery", "source_candidate_evaluate"})

EXTERNAL_LANES: dict[str, frozenset[str]] = {
    # Поток дня: пакеты планировщика, перепечатки, документы пользователей.
    AI_LIVE: _AI_KINDS,
    # Пересчёты корпуса — долгие пачки; не должны стоять перед потоком дня. Перепроверки
    # релевантности (удаляют статьи) здесь нет: резерв защищает только пару пакет×пакет, и
    # удаление параллельно пакету дня уронило бы его apply на внешнем ключе.
    AI_BULK: frozenset({"process_articles", "translate_titles"}),
    FETCH: _FETCH_KINDS,
    BROWSER: _FETCH_KINDS,
    # Радар и оценка кандидатов агента источников (ADR 0001): прогон радара — минуты,
    # а не секунды, и в общей полосе пакет статей дня ждал бы его, а радар — пакеты.
    AGENTS: _AGENT_KINDS,
}

# Сколько задача может ждать в очереди, пока это норма (минуты). Пачка сбора — раз в
# ~41 мин и разбирается за ~6; поток дня ИИ ждёт секунды; пересчёт — часами по замыслу;
# у агентов один поток: пачка оценок кандидатов ждёт прогон радара (12 прогонов до
# 23.09: медиана 4 мин, максимум 17) и друг друга.
STALE_AFTER_MINUTES: dict[str, int] = {AI_LIVE: 30, AI_BULK: 360, FETCH: 45, BROWSER: 45, AGENTS: 120}
# Задачи есть, но ни одна не стартовала столько минут и ничего не выполняется —
# у очереди нет живого потребителя.
IDLE_AFTER_MINUTES = 15


def is_external(queue_name: str | None) -> bool:
    return str(queue_name or "").startswith("external")


def serves(queue_name: str | None, kind: str | None) -> bool:
    return str(kind or "") in EXTERNAL_LANES.get(str(queue_name or ""), frozenset())


def route(queue_name: str | None, kind: str | None) -> str | None:
    """Очередь, в которую задача встанет на самом деле.

    Код радара и агента источников ставит свои задачи в ИИ-полосу: маршрутом ИИ
    (network_policy.route_ai_processing) или строкой "external-ai" (source_discovery/
    loop.py). Этот код — Германа и не меняется, поэтому полосу агентам выбирает постановка,
    в одном месте. Из других полос не переносим: радар в полосе сбора — ошибка
    вызывающего, и check_enqueue откажет ему громко."""
    if str(kind or "") in _AGENT_KINDS and queue_name in (AI_LIVE, AI_BULK):
        return AGENTS
    return queue_name


def check_enqueue(queue_name: str | None, kind: str | None) -> None:
    """Внешняя очередь принимает только то, что её воркер умеет исполнять."""
    if is_external(queue_name) and not serves(queue_name, kind):
        known = sorted(EXTERNAL_LANES.get(str(queue_name or ""), frozenset()))
        raise ValueError(
            f"Очередь {queue_name} не обслуживает задачи вида {kind!r}"
            + (f" (обслуживает: {', '.join(known)})" if known else " (такой полосы нет)")
        )


def _minutes_since(value: Any, now: datetime) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return (now - value).total_seconds() / 60


def lane_alerts(status: dict[str, Any], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Тревоги по итогу external_queue_status: застой, нет потребителя, неизвестная очередь."""
    now = now or datetime.now(timezone.utc)
    alerts: list[dict[str, Any]] = []
    expired = int((status.get("totals") or {}).get("expired_leases") or 0)
    if expired:
        alerts.append({"queue": None, "kind": "expired_leases", "count": expired,
                       "message": f"Истёкшие аренды у выполняющихся задач: {expired}"})
    for row in status.get("queues") or []:
        queue = str(row.get("queue_name") or "")
        queued = int(row.get("queued") or 0)
        if not queued:
            continue
        if queue not in EXTERNAL_LANES:
            alerts.append({"queue": queue, "kind": "unknown_queue", "count": queued,
                           "message": f"Очередь {queue}: {queued} задач, но такой полосы нет — их никто не возьмёт"})
            continue
        waited = _minutes_since(row.get("oldest_ready_at"), now)
        if waited is not None and waited > STALE_AFTER_MINUTES[queue]:
            alerts.append({"queue": queue, "kind": "stale", "count": queued, "minutes": round(waited),
                           "message": f"Очередь {queue}: самая старая из {queued} задач ждёт {round(waited)} мин"})
        # Последний признак жизни воркера в этой очереди: выдача, heartbeat или завершение.
        idle = _minutes_since(row.get("last_activity_at"), now)
        running = int(row.get("running") or 0) + int(row.get("finalizing") or 0)
        if waited is not None and not running and (idle is None or idle > IDLE_AFTER_MINUTES) \
                and waited > IDLE_AFTER_MINUTES:
            since = "ни разу" if idle is None else f"{round(idle)} мин"
            alerts.append({"queue": queue, "kind": "no_consumer", "count": queued,
                           "message": f"Очередь {queue}: {queued} задач ждут, а воркер не появлялся {since} — нет живого потребителя"})
    return alerts
