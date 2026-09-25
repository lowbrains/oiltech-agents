"""CRUD and query helpers for sources, articles, auth and AI processing."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import re
from typing import Literal, NamedTuple, get_args
from urllib.parse import urlsplit

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest import auth, config, lanes
from oiltech_digest.ingestion import normalize
from oiltech_digest.db.connection import get_connection

# Единый источник правды для набора пер-юзерных рабочих статусов статьи (#12).
# ДОЛЖЕН совпадать с union Article["status"] во фронте (frontend/src/api/types.ts).
# api.ArticlePatch.status импортирует ArticleStatus отсюда (валидация 422), dashboard_stats
# проецирует счётчики по ARTICLE_STATUS_VALUES — так Python-половина не рассинхронится:
# добавил статус в Literal → он автоматически появился и в кортеже (get_args).
# 12.09: статус `review` («На проверке») убран по требованию заказчика. Он был
# декоративным — ничего не скрывал и ни на что не влиял, но при этом был ЕДИНСТВЕННЫМ
# статусом, который система ставила сама (снятие статьи из дайджеста). Теперь этот
# переход ведёт в `archive`, а сам `archive` из пустого счётчика стал рабочим: он
# скрывает статью из ленты, как `noise`/`duplicate`, но без штрафа баллу источника
# (см. api.list_articles и repository.compute_source_quality_rows).
ArticleStatus = Literal["new", "digest", "archive", "noise", "duplicate"]
ARTICLE_STATUS_VALUES: tuple[ArticleStatus, ...] = get_args(ArticleStatus)

# Служебный тег-приёмник: сюда падает статья, не совпавшая ни с одной тематикой
# (pipeline._fallback_tag). Он не тематика, а предохранитель — без него классификация
# уводит весь непонятый поток в ПЕРВЫЙ тег списка. Поэтому его нельзя выключить ни
# сохранением экрана тегов, ни удалением: 13.09 он выключился именно так и молча,
# а страховка «больше половины» такой случай не ловит (один тег из четырнадцати).
SYSTEM_TAG_UNCLASSIFIED = "Не классифицировано / новая тема"

# ---------------------------------------------------------------------------
#  sources
# ---------------------------------------------------------------------------

def upsert_source(rec: dict) -> str:
    """Вставить/обновить источник по name (естественный ключ).

    Ключ — (name, source_type): один бренд может иметь сайт и Telegram-канал с
    одинаковым именем, но разным типом (это разные источники). При конфликте
    обновляются только описательные поля (url, category, priority). rss_url /
    parse_strategy / enabled НЕ трогаем — ими управляет discover-rss.
    Возвращает 'inserted' либо 'updated'.
    """
    with get_connection() as conn:
        rec = {**rec, "update_frequency": rec.get("update_frequency")}
        cur = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, rss_url, enabled,
                                 parse_strategy, category, update_frequency, priority)
            VALUES (%(name)s, %(source_type)s, %(url)s, %(rss_url)s,
                    COALESCE(%(enabled)s, TRUE), %(parse_strategy)s,
                    %(category)s, %(update_frequency)s, COALESCE(%(priority)s, 1.0))
            ON CONFLICT (name, source_type) DO UPDATE SET
                url         = EXCLUDED.url,
                category    = EXCLUDED.category,
                update_frequency = EXCLUDED.update_frequency,
                priority    = EXCLUDED.priority,
                updated_at  = now()
            RETURNING (xmax = 0) AS inserted
            """,
            rec,
        )
        inserted = cur.fetchone()[0]
        conn.commit()
    return "inserted" if inserted else "updated"


def get_enabled_sources(strategy: str | None = None) -> list[dict]:
    """Включённые источники, опционально с фильтром по parse_strategy."""
    query = "SELECT * FROM sources WHERE enabled = TRUE"
    params: list = []
    if strategy is not None:
        query += " AND parse_strategy = %s"
        params.append(strategy)
    query += " ORDER BY id"
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(query, params)
        return cur.fetchall()


def get_sources_for_discovery(only_missing: bool = True,
                              source_id: int | None = None,
                              limit: int | None = None) -> list[dict]:
    """Источники-кандидаты на автообнаружение RSS (не Telegram и не Playwright).

    `playwright` — осознанно выставленная вручную стратегия (JS/WAF-сайты); discover-rss
    НЕ должен её сбрасывать в request, иначе оверрайды откатываются на каждом цикле.

    По той же причине исключаются источники с заданным `listing_url`: он ставится только
    осознанно (реестр оверрайдов или админка), а discover_feed пробует НЕ его, а `url` —
    главную страницу издания. У федеральных СМИ главная всегда рекламирует RSS, поэтому
    update_source_rss перезаписал бы parse_strategy на 'rss' с ОБЩИМ фидом издания —
    ровно тем мусором, ради которого источник и правился (#61). Порядок на деплое делает
    это неизбежным: bootstrap применяет реестр, а первый же цикл планировщика запускает
    discover-rss (RUN_DISCOVER_ON_START=1), то есть откат случился бы до первого парса.
    """
    query = ("SELECT * FROM sources WHERE enabled = TRUE "
             "AND parse_strategy NOT IN ('telegram', 'playwright') "
             "AND (listing_url IS NULL OR listing_url = '')")
    params: list = []
    if only_missing:
        query += " AND (rss_url IS NULL OR rss_url = '')"
    if source_id is not None:
        query += " AND id = %s"
        params.append(source_id)
    query += " ORDER BY id"
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(query, params)
        return cur.fetchall()


def update_source_rss(source_id: int, rss_url: str | None, parse_strategy: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE sources SET rss_url = %s, parse_strategy = %s, updated_at = now() WHERE id = %s",
            (rss_url, parse_strategy, source_id),
        )
        conn.commit()


def set_source_enabled(source_id: int, enabled: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE sources SET enabled = %s, updated_at = now() WHERE id = %s",
            (enabled, source_id),
        )
        conn.commit()


def add_rss_source(name: str, rss_url: str, source_type: str = "RSS",
                   url: str | None = None, priority: float = 1.0,
                   category: str | None = None,
                   update_frequency: str | None = None,
                   parse_strategy: str = "rss") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, rss_url, enabled,
                                 parse_strategy, category, update_frequency, priority)
            VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, %s)
            ON CONFLICT (name, source_type) DO UPDATE SET
                url = EXCLUDED.url,
                rss_url = EXCLUDED.rss_url,
                -- Архивный источник повторным добавлением НЕ воскрешаем. Иначе получилось
                -- бы худшее из двух: сбор возобновился (enabled=TRUE), а статьи всё равно
                -- скрыты (archived_at не NULL) — источник молча жжёт ИИ в никуда.
                -- Вернуть в работу можно только явно, через /api/sources/{id}/unarchive.
                enabled = (sources.archived_at IS NULL),
                parse_strategy = EXCLUDED.parse_strategy,
                category = EXCLUDED.category,
                update_frequency = EXCLUDED.update_frequency,
                priority = EXCLUDED.priority,
                updated_at = now()
            RETURNING id
            """,
            (name, source_type, url, rss_url, parse_strategy, category, update_frequency, priority),
        )
        source_id = cur.fetchone()[0]
        conn.commit()
        return source_id


def list_sources(search: str | None = None, limit: int = 50) -> list[dict]:
    query = "SELECT * FROM sources"
    params: list = []
    if search:
        query += " WHERE name ILIKE %s OR url ILIKE %s OR rss_url ILIKE %s OR listing_url ILIKE %s"
        like = f"%{search}%"
        params.extend([like, like, like, like])
    query += " ORDER BY enabled DESC, parse_strategy NULLS LAST, id LIMIT %s"
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(query, params)
        return cur.fetchall()


def source_inventory_index() -> dict[str, dict]:
    """Индекс существующих источников по URL и доменам для дедупликации discovery."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT id, name, url, rss_url, listing_url, enabled
            FROM sources
            """
        )
        rows = cur.fetchall()

    by_url: dict[str, dict] = {}
    by_domain: dict[str, dict] = {}
    for row in rows:
        for field in ("url", "rss_url", "listing_url"):
            value = str(row.get(field) or "").strip()
            if not value:
                continue
            by_url[value.rstrip("/").lower()] = row
            domain = normalize_domain(value).lower()
            if domain and domain not in by_domain:
                by_domain[domain] = row
    return {"by_url": by_url, "by_domain": by_domain}


def get_source(source_id: int) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM sources WHERE id = %s", (source_id,))
        return cur.fetchone()


def touch_last_parsed(source_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE sources SET last_parsed_at = now() WHERE id = %s", (source_id,)
        )
        conn.commit()


def set_sources_network_region(ids: list[int], region: str) -> int:
    """Проставить network_region (auto|ru|external) источникам по списку id.

    При уходе в/из external сбрасываем request-состояние (last_seen/hash), чтобы
    смена пути парсинга не коротила на старом хэше. Возвращает число обновлённых строк."""
    if not ids:
        return 0
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE sources SET network_region = %s, last_listing_hash = NULL, "
            "last_seen_article_url = NULL, last_seen_published_at = NULL, updated_at = now() "
            "WHERE id = ANY(%s)",
            (region, list(ids)),
        )
        conn.commit()
        return cur.rowcount


# ---------------------------------------------------------------------------
#  Разведка источников и обратная связь
# ---------------------------------------------------------------------------

SOURCE_CANDIDATE_STATUSES = (
    "new",
    "researching",
    "test_parsing",
    "needs_human_review",
    "approved",
    "rejected",
    "paused",
)

SOURCE_CANDIDATE_ACTIONS = ("add", "test_more", "reject", "human_review")

SIGNAL_FEEDBACK_EVENTS = (
    "added_to_digest",
    "marked_noise",
    "marked_duplicate",
    "tag_changed",
    "score_changed",
    "status_changed",
    "comment_added",
)


def normalize_domain(url: str) -> str:
    parsed = urlsplit((url or "").strip())
    host = (parsed.netloc or parsed.path.split("/")[0]).lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split("@")[-1].split(":")[0]


def record_signal_feedback_event(
    article_id: int | None,
    event_type: str,
    *,
    signal_id: int | None = None,
    signal_evidence_id: int | None = None,
    source_url: str | None = None,
    signal_title: str | None = None,
    user_id: int | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    comment: str | None = None,
    verdict: str | None = None,
    reason: str | None = None,
    corrected_title: str | None = None,
    corrected_thesis: str | None = None,
    duplicate_of_signal_id: int | None = None,
) -> int:
    if event_type not in SIGNAL_FEEDBACK_EVENTS:
        raise ValueError(f"Unknown signal feedback event_type: {event_type}")
    if article_id is None and signal_id is None and not source_url:
        raise ValueError("signal feedback requires article_id, signal_id or source_url")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO signal_feedback_events
              (article_id, signal_id, signal_evidence_id, source_url, signal_title,
               user_id, event_type, old_value, new_value, comment,
               verdict, reason, corrected_title, corrected_thesis, duplicate_of_signal_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                article_id,
                signal_id,
                signal_evidence_id,
                source_url,
                signal_title,
                user_id,
                event_type,
                old_value,
                new_value,
                comment,
                verdict,
                reason,
                corrected_title,
                corrected_thesis,
                duplicate_of_signal_id,
            ),
        )
        event_id = int(cur.fetchone()[0])
        conn.commit()
        return event_id


def upsert_source_candidate(rec: dict) -> int:
    url = (rec.get("url") or "").strip()
    if not url:
        raise ValueError("source candidate url is required")
    status = rec.get("status") or "new"
    if status not in SOURCE_CANDIDATE_STATUSES:
        raise ValueError(f"Unknown source candidate status: {status}")
    recommended_action = rec.get("recommended_action")
    if recommended_action and recommended_action not in SOURCE_CANDIDATE_ACTIONS:
        raise ValueError(f"Unknown source candidate recommended_action: {recommended_action}")
    payload = {
        **rec,
        "url": url,
        "normalized_domain": rec.get("normalized_domain") or normalize_domain(url),
        "name": rec.get("name"),
        "candidate_type": rec.get("candidate_type"),
        "status": status,
        "discovered_by": rec.get("discovered_by") or "manual",
        "discovery_reason": rec.get("discovery_reason"),
        "topic": rec.get("topic"),
        "expected_tags_json": Json(rec.get("expected_tags_json") or []),
        "confidence": rec.get("confidence"),
        "tested_articles": rec.get("tested_articles"),
        "relevant_articles": rec.get("relevant_articles"),
        "avg_score": rec.get("avg_score"),
        "duplicate_count": rec.get("duplicate_count"),
        "noise_count": rec.get("noise_count"),
        "recommended_action": recommended_action,
        "review_comment": rec.get("review_comment"),
        "approved_source_id": rec.get("approved_source_id"),
    }
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO source_candidates (
              url, normalized_domain, name, candidate_type, status, discovered_by,
              discovery_reason, topic, expected_tags_json, confidence,
              tested_articles, relevant_articles, avg_score, duplicate_count,
              noise_count, recommended_action, review_comment, approved_source_id
            )
            VALUES (
              %(url)s, %(normalized_domain)s, %(name)s, %(candidate_type)s, %(status)s,
              %(discovered_by)s, %(discovery_reason)s, %(topic)s, %(expected_tags_json)s,
              %(confidence)s, COALESCE(%(tested_articles)s, 0),
              COALESCE(%(relevant_articles)s, 0), %(avg_score)s,
              COALESCE(%(duplicate_count)s, 0), COALESCE(%(noise_count)s, 0),
              %(recommended_action)s, %(review_comment)s, %(approved_source_id)s
            )
            ON CONFLICT (url) DO UPDATE SET
              normalized_domain = EXCLUDED.normalized_domain,
              name = COALESCE(EXCLUDED.name, source_candidates.name),
              candidate_type = COALESCE(EXCLUDED.candidate_type, source_candidates.candidate_type),
              status = EXCLUDED.status,
              discovered_by = EXCLUDED.discovered_by,
              discovery_reason = COALESCE(EXCLUDED.discovery_reason, source_candidates.discovery_reason),
              topic = COALESCE(EXCLUDED.topic, source_candidates.topic),
              expected_tags_json = EXCLUDED.expected_tags_json,
              confidence = COALESCE(EXCLUDED.confidence, source_candidates.confidence),
              tested_articles = EXCLUDED.tested_articles,
              relevant_articles = EXCLUDED.relevant_articles,
              avg_score = COALESCE(EXCLUDED.avg_score, source_candidates.avg_score),
              duplicate_count = EXCLUDED.duplicate_count,
              noise_count = EXCLUDED.noise_count,
              recommended_action = COALESCE(EXCLUDED.recommended_action, source_candidates.recommended_action),
              review_comment = COALESCE(EXCLUDED.review_comment, source_candidates.review_comment),
              approved_source_id = COALESCE(EXCLUDED.approved_source_id, source_candidates.approved_source_id),
              updated_at = now()
            RETURNING id
            """,
            payload,
        )
        candidate_id = int(cur.fetchone()[0])
        conn.commit()
        return candidate_id


def list_source_candidates(
    *,
    status: str | None = None,
    topic: str | None = None,
    limit: int = 50,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if topic:
        clauses.append("topic ILIKE %s")
        params.append(f"%{topic}%")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT *
            FROM source_candidates
            {where}
            ORDER BY
              CASE status
                WHEN 'needs_human_review' THEN 0
                WHEN 'new' THEN 1
                WHEN 'researching' THEN 2
                WHEN 'test_parsing' THEN 3
                WHEN 'approved' THEN 4
                WHEN 'paused' THEN 5
                ELSE 6
              END,
              created_at DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def source_candidate_triage_report(*, limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT
              *,
              (
                CASE COALESCE(recommended_action, '')
                  WHEN 'add' THEN 90
                  WHEN 'test_more' THEN 70
                  WHEN 'human_review' THEN 55
                  WHEN 'reject' THEN 25
                  ELSE 40
                END
                + LEAST(COALESCE(relevant_articles, 0) * 4, 24)
                + LEAST(COALESCE(avg_score, 0) / 5, 16)
                - LEAST(COALESCE(noise_count, 0) * 5, 30)
              )::float AS triage_priority,
              CASE
                WHEN recommended_action = 'add' THEN 'Можно добавлять после проверки человеком'
                WHEN recommended_action = 'test_more' THEN 'Нужна дополнительная песочница'
                WHEN recommended_action = 'reject' THEN 'Похоже на шум или слабый источник'
                ELSE 'Нужно ручное решение'
              END AS triage_reason
            FROM source_candidates
            WHERE status NOT IN ('approved', 'rejected')
            ORDER BY triage_priority DESC, updated_at DESC, created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def source_candidate_quality_report(*, group_by: str = "topic", limit: int = 20) -> list[dict]:
    if group_by not in {"topic", "domain"}:
        raise ValueError("group_by must be topic or domain")
    subject_expr = "COALESCE(NULLIF(topic, ''), 'Без темы')" if group_by == "topic" else "normalized_domain"
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT
              {subject_expr} AS subject,
              COUNT(*)::int AS candidates,
              COUNT(*) FILTER (WHERE status = 'approved')::int AS approved,
              COUNT(*) FILTER (WHERE status = 'rejected')::int AS rejected,
              COUNT(*) FILTER (WHERE status = 'paused')::int AS paused,
              COUNT(*) FILTER (WHERE status = 'needs_human_review')::int AS needs_human_review,
              COUNT(*) FILTER (WHERE recommended_action = 'test_more')::int AS test_more,
              COALESCE(SUM(tested_articles), 0)::int AS tested_articles,
              COALESCE(SUM(relevant_articles), 0)::int AS relevant_articles,
              COALESCE(SUM(noise_count), 0)::int AS noise_count,
              AVG(avg_score) FILTER (WHERE avg_score IS NOT NULL)::float AS avg_score,
              CASE
                WHEN COUNT(*) FILTER (WHERE status IN ('approved', 'rejected')) = 0 THEN 0
                ELSE ROUND(
                  COUNT(*) FILTER (WHERE status = 'approved')::numeric
                  / COUNT(*) FILTER (WHERE status IN ('approved', 'rejected'))::numeric,
                  3
                )::float
              END AS approval_rate,
              CASE
                WHEN COALESCE(SUM(tested_articles), 0) = 0 THEN 0
                ELSE ROUND(
                  COALESCE(SUM(relevant_articles), 0)::numeric
                  / COALESCE(SUM(tested_articles), 0)::numeric,
                  3
                )::float
              END AS relevance_rate
            FROM source_candidates
            WHERE {subject_expr} IS NOT NULL AND {subject_expr} <> ''
            GROUP BY subject
            ORDER BY
              approved DESC,
              relevance_rate DESC,
              rejected ASC,
              candidates DESC,
              subject ASC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def get_source_candidate(candidate_id: int) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM source_candidates WHERE id = %s", (candidate_id,))
        return cur.fetchone()


def update_source_candidate_assessment(
    candidate_id: int,
    *,
    status: str | None = None,
    tested_articles: int | None = None,
    relevant_articles: int | None = None,
    avg_score: float | None = None,
    duplicate_count: int | None = None,
    noise_count: int | None = None,
    recommended_action: str | None = None,
    review_comment: str | None = None,
) -> None:
    if status and status not in SOURCE_CANDIDATE_STATUSES:
        raise ValueError(f"Unknown source candidate status: {status}")
    if recommended_action and recommended_action not in SOURCE_CANDIDATE_ACTIONS:
        raise ValueError(f"Unknown source candidate recommended_action: {recommended_action}")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE source_candidates
            SET status = COALESCE(%s, status),
                tested_articles = COALESCE(%s, tested_articles),
                relevant_articles = COALESCE(%s, relevant_articles),
                avg_score = COALESCE(%s, avg_score),
                duplicate_count = COALESCE(%s, duplicate_count),
                noise_count = COALESCE(%s, noise_count),
                recommended_action = COALESCE(%s, recommended_action),
                review_comment = COALESCE(%s, review_comment),
                updated_at = now()
            WHERE id = %s
            """,
            (
                status, tested_articles, relevant_articles, avg_score,
                duplicate_count, noise_count, recommended_action, review_comment,
                candidate_id,
            ),
        )
        conn.commit()


def approve_source_candidate(
    candidate_id: int,
    *,
    name: str | None = None,
    source_type: str = "Discovered",
    parse_strategy: str | None = None,
    enabled: bool = False,
    category: str | None = None,
    priority: float = 1.0,
    network_region: str = "auto",
) -> int:
    candidate = get_source_candidate(candidate_id)
    if candidate is None:
        raise ValueError(f"source candidate id={candidate_id} not found")
    if candidate.get("approved_source_id"):
        return int(candidate["approved_source_id"])

    url = str(candidate["url"])
    candidate_type = str(candidate.get("candidate_type") or "").lower()
    strategy = parse_strategy or ("rss" if candidate_type == "rss" or url.lower().endswith(".xml") else "request")
    source_name = (name or candidate.get("name") or normalize_domain(url) or f"Source candidate {candidate_id}").strip()
    source_category = category if category is not None else candidate.get("topic")
    rss_url = url if strategy == "rss" else None
    listing_url = None if strategy == "rss" else url
    with get_connection() as conn:
        same_name = conn.execute(
            "SELECT url, listing_url, rss_url FROM sources WHERE name = %s AND source_type = %s",
            (source_name, source_type),
        ).fetchone()
        domain = normalize_domain(url)
        if same_name and domain and domain not in {normalize_domain(str(value or "")) for value in same_name}:
            # Имя берётся из заголовка страницы и бывает общим («Press Releases», «News»):
            # без этого ON CONFLICT ниже молча переписал бы адрес ЧУЖОГО источника.
            source_name = f"{source_name} ({domain})"
        cur = conn.execute(
            """
            INSERT INTO sources (
              name, source_type, url, rss_url, enabled, parse_strategy,
              listing_url, category, priority, network_region
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (name, source_type) DO UPDATE SET
              url = EXCLUDED.url,
              rss_url = EXCLUDED.rss_url,
              enabled = EXCLUDED.enabled,
              parse_strategy = EXCLUDED.parse_strategy,
              listing_url = EXCLUDED.listing_url,
              category = COALESCE(EXCLUDED.category, sources.category),
              priority = EXCLUDED.priority,
              network_region = EXCLUDED.network_region,
              updated_at = now()
            RETURNING id
            """,
            (
                source_name,
                source_type,
                url,
                rss_url,
                enabled,
                strategy,
                listing_url,
                source_category,
                priority,
                network_region,
            ),
        )
        source_id = int(cur.fetchone()[0])
        conn.execute(
            """
            UPDATE source_candidates
            SET status = 'approved',
                approved_source_id = %s,
                recommended_action = COALESCE(recommended_action, 'add'),
                review_comment = COALESCE(review_comment, 'Одобрено человеком и создано как источник.'),
                updated_at = now()
            WHERE id = %s
            """,
            (source_id, candidate_id),
        )
        conn.commit()
        return source_id


def upsert_source_candidate_article(candidate_id: int, rec: dict) -> int:
    url = (rec.get("url") or "").strip()
    title = (rec.get("title") or "").strip()
    if not url:
        raise ValueError("source candidate article url is required")
    if not title:
        title = url
    raw_text = rec.get("raw_text") or ""
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO source_candidate_articles (
              candidate_id, title, url, published_at, raw_text, language, text_chars,
              prefilter_keep, prefilter_reason
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (candidate_id, url) DO UPDATE SET
              title = EXCLUDED.title,
              published_at = COALESCE(EXCLUDED.published_at, source_candidate_articles.published_at),
              raw_text = COALESCE(EXCLUDED.raw_text, source_candidate_articles.raw_text),
              language = COALESCE(EXCLUDED.language, source_candidate_articles.language),
              text_chars = EXCLUDED.text_chars,
              prefilter_keep = EXCLUDED.prefilter_keep,
              prefilter_reason = EXCLUDED.prefilter_reason,
              updated_at = now()
            RETURNING id
            """,
            (
                candidate_id,
                title,
                url,
                rec.get("published_at"),
                raw_text,
                rec.get("language"),
                len(raw_text),
                rec.get("prefilter_keep"),
                rec.get("prefilter_reason"),
            ),
        )
        article_id = int(cur.fetchone()[0])
        conn.commit()
        return article_id


def list_source_candidate_articles(
    candidate_id: int,
    *,
    limit: int = 20,
    only_unprocessed: bool = False,
) -> list[dict]:
    clauses = ["candidate_id = %s"]
    params: list = [candidate_id]
    if only_unprocessed:
        clauses.append("processing_status IN ('new', 'error')")
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT sca.*, sc.name AS source_name, sc.topic AS source_category
            FROM source_candidate_articles sca
            JOIN source_candidates sc ON sc.id = sca.candidate_id
            WHERE {" AND ".join(clauses)}
            ORDER BY sca.created_at DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def update_source_candidate_article_result(article_id: int, payload: dict) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE source_candidate_articles
            SET relevant = %(relevant)s,
                relevance_reason = %(relevance_reason)s,
                relevance_model = %(relevance_model)s,
                summary = %(summary)s,
                summary_model = %(summary_model)s,
                title_ru = %(title_ru)s,
                tag_id = %(tag_id)s,
                tag_confidence = %(tag_confidence)s,
                tag_rationale = %(tag_rationale)s,
                tag_model = %(tag_model)s,
                total_score = %(total_score)s,
                score_label = %(score_label)s,
                score_explanation = %(score_explanation)s,
                score_items_json = %(score_items_json)s,
                score_model = %(score_model)s,
                processing_status = %(processing_status)s,
                error_message = %(error_message)s,
                updated_at = now()
            WHERE id = %(id)s
            """,
            {
                **payload,
                "id": article_id,
                "score_items_json": Json(payload.get("score_items") or []),
            },
        )
        conn.commit()


def source_candidate_article_metrics(candidate_id: int) -> dict:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT
              COUNT(*)::int AS tested_articles,
              COUNT(*) FILTER (WHERE text_chars > 0)::int AS parsed_articles,
              COUNT(*) FILTER (WHERE processing_status IN ('ok', 'rejected'))::int AS processed_articles,
              COUNT(*) FILTER (WHERE prefilter_keep IS TRUE)::int AS kept_by_prefilter,
              COUNT(*) FILTER (WHERE relevant IS TRUE)::int AS relevant_articles,
              COUNT(*) FILTER (WHERE total_score IS NOT NULL)::int AS scored_articles,
              COUNT(*) FILTER (WHERE total_score >= 50)::int AS high_score_articles,
              AVG(total_score) FILTER (WHERE total_score IS NOT NULL) AS avg_score,
              COUNT(*) FILTER (WHERE processing_status = 'rejected')::int AS noise_count,
              0::int AS duplicate_count
            FROM source_candidate_articles
            WHERE candidate_id = %s
            """,
            (candidate_id,),
        )
        row = cur.fetchone() or {}
        avg_score = row.get("avg_score")
        return {
            "tested_articles": int(row.get("tested_articles") or 0),
            "parsed_articles": int(row.get("parsed_articles") or 0),
            "processed_articles": int(row.get("processed_articles") or 0),
            "kept_by_prefilter": int(row.get("kept_by_prefilter") or 0),
            "relevant_articles": int(row.get("relevant_articles") or 0),
            "scored_articles": int(row.get("scored_articles") or 0),
            "high_score_articles": int(row.get("high_score_articles") or 0),
            "avg_score": round(float(avg_score), 2) if avg_score is not None else None,
            "duplicate_count": int(row.get("duplicate_count") or 0),
            "noise_count": int(row.get("noise_count") or 0),
        }


def seed_signal_radar_topics(topics: list[dict]) -> int:
    changed = 0
    with get_connection() as conn:
        for order, topic in enumerate(topics, start=1):
            cur = conn.execute(
                """
                INSERT INTO signal_radar_topics (
                  name, description, query_seeds_json, industry_scope_json, enabled, sort_order
                )
                VALUES (%s, %s, %s, %s, TRUE, %s)
                ON CONFLICT (name) DO UPDATE SET
                  description = EXCLUDED.description,
                  query_seeds_json = EXCLUDED.query_seeds_json,
                  industry_scope_json = EXCLUDED.industry_scope_json,
                  enabled = TRUE,
                  sort_order = EXCLUDED.sort_order,
                  updated_at = now()
                RETURNING id
                """,
                (
                    topic["name"],
                    topic.get("description"),
                    Json(_jsonable(topic.get("query_seeds") or [])),
                    Json(_jsonable(topic.get("industry_scope") or [])),
                    order,
                ),
            )
            if cur.fetchone():
                changed += 1
        conn.commit()
    return changed


def list_signal_radar_topics(*, enabled_only: bool = True) -> list[dict]:
    query = "SELECT * FROM signal_radar_topics"
    params: list = []
    if enabled_only:
        query += " WHERE enabled"
    query += " ORDER BY sort_order, name"
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(query, params)
        return cur.fetchall()


def list_signal_article_evidence(
    *,
    topic: str | None = None,
    days: int = 14,
    limit: int = 80,
    min_score: float = 40,
) -> list[dict]:
    clauses = ["a.collected_at >= now() - (%s::text || ' days')::interval"]
    params: list = [days]
    if topic:
        terms = [
            term
            for term in dict.fromkeys(part.lower() for part in re.findall(r"[A-Za-zА-Яа-яЁё0-9]{3,}", topic))
            if term not in {"and", "the", "for", "hse", "oil", "gas"}
        ][:6]
        topic_clauses = []
        for term in terms or [topic]:
            like = f"%{term}%"
            topic_clauses.append(
                """
                (
                  COALESCE(t.name, '') ILIKE %s OR COALESCE(t.name_en, '') ILIKE %s
                  OR COALESCE(ac.summary, '') ILIKE %s OR COALESCE(a.title, '') ILIKE %s
                  OR COALESCE(a.raw_text, '') ILIKE %s
                )
                """
            )
            params.extend([like, like, like, like, like])
        clauses.append(
            "(" + " OR ".join(topic_clauses) + ")"
        )
    params.extend([min_score, limit])
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT
              a.id AS article_id,
              a.title,
              COALESCE(ac.title_ru, a.title) AS title_ru,
              a.url AS source_url,
              a.published_at,
              a.collected_at,
              a.language,
              a.raw_text,
              s.name AS publisher,
              s.source_type,
              ac.summary,
              ac.relevant,
              ac.relevance_reason,
              t.name AS tag_name,
              t.name_en AS tag_name_en,
              article_scores.total_score,
              article_scores.score_label,
              article_scores.explanation AS score_explanation
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards ac ON ac.article_id = a.id
            LEFT JOIN article_tags at ON at.article_id = a.id
            LEFT JOIN tags t ON t.id = at.tag_id
            LEFT JOIN article_scores ON article_scores.article_id = a.id
            WHERE {" AND ".join(clauses)}
              AND COALESCE(ac.relevant, TRUE) IS TRUE
              AND COALESCE(article_scores.total_score, 50) >= %s
              AND NOT a.pending_deletion
            ORDER BY COALESCE(article_scores.total_score, 50) DESC, a.collected_at DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def upsert_signal(signal: dict) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals (
              signal_key, title, title_ru, theme, summary, thesis, transferability, maturity, confidence, score,
              why_now, why_not_noise, companies_json, industries_json, evidence_count,
              interest_score, why_interesting
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (signal_key) DO UPDATE SET
              title = EXCLUDED.title,
              title_ru = EXCLUDED.title_ru,
              theme = EXCLUDED.theme,
              summary = EXCLUDED.summary,
              thesis = EXCLUDED.thesis,
              transferability = EXCLUDED.transferability,
              maturity = EXCLUDED.maturity,
              confidence = EXCLUDED.confidence,
              score = EXCLUDED.score,
              why_now = EXCLUDED.why_now,
              why_not_noise = EXCLUDED.why_not_noise,
              companies_json = EXCLUDED.companies_json,
              industries_json = EXCLUDED.industries_json,
              evidence_count = EXCLUDED.evidence_count,
              -- Балл интереса — сравнение внутри пачки; тема из одного кандидата его
              -- не получает, и прошлый балл не должен стираться NULL'ом.
              interest_score = COALESCE(EXCLUDED.interest_score, signals.interest_score),
              why_interesting = COALESCE(NULLIF(EXCLUDED.why_interesting, ''), signals.why_interesting),
              last_seen_at = now(),
              updated_at = now()
            RETURNING id
            """,
            (
                signal["signal_key"],
                signal["title"],
                signal.get("title_ru"),
                signal["theme"],
                signal.get("summary"),
                signal.get("thesis"),
                signal.get("transferability"),
                signal.get("maturity") or "watch",
                float(signal.get("confidence") or 0),
                float(signal.get("score") or 0),
                signal.get("why_now"),
                signal.get("why_not_noise"),
                Json(_jsonable(signal.get("companies") or [])),
                Json(_jsonable(signal.get("industries") or [])),
                int(signal.get("evidence_count") or 0),
                float(signal["interest_score"]) if signal.get("interest_score") is not None else None,
                (signal.get("why_interesting") or None),
            ),
        )
        signal_id = int(cur.fetchone()[0])
        conn.commit()
        return signal_id


def upsert_signal_evidence(signal_id: int, evidence: dict) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO signal_evidence (
              signal_id, article_id, source_url, title, title_ru, publisher, published_at,
              evidence_type, extracted_fact, summary_ru, strength, raw_payload_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_url) DO UPDATE SET
              signal_id = EXCLUDED.signal_id,
              article_id = COALESCE(EXCLUDED.article_id, signal_evidence.article_id),
              title = EXCLUDED.title,
              title_ru = EXCLUDED.title_ru,
              publisher = EXCLUDED.publisher,
              published_at = COALESCE(EXCLUDED.published_at, signal_evidence.published_at),
              evidence_type = EXCLUDED.evidence_type,
              extracted_fact = EXCLUDED.extracted_fact,
              summary_ru = EXCLUDED.summary_ru,
              strength = EXCLUDED.strength,
              raw_payload_json = EXCLUDED.raw_payload_json,
              updated_at = now()
            RETURNING id
            """,
            (
                signal_id,
                evidence.get("article_id"),
                evidence["source_url"],
                evidence["title"],
                evidence.get("title_ru"),
                evidence.get("publisher"),
                evidence.get("published_at"),
                evidence.get("evidence_type") or "article",
                evidence.get("extracted_fact"),
                evidence.get("summary_ru"),
                float(evidence.get("strength") or 0),
                Json(_jsonable(evidence.get("raw_payload") or evidence)),
            ),
        )
        evidence_id = int(cur.fetchone()[0])
        conn.commit()
        return evidence_id


def refresh_signal_evidence_count(signal_id: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE signals
            SET evidence_count = (
                  SELECT COUNT(*)::int
                  FROM signal_evidence
                  WHERE signal_id = %s
                ),
                updated_at = now()
            WHERE id = %s
            RETURNING evidence_count
            """,
            (signal_id, signal_id),
        )
        row = cur.fetchone()
        conn.commit()
        return int(row[0]) if row else 0


# Разобранная карточка — у неё есть суждение человека: вердикт, комментарий или статус,
# отличный от «наблюдать» (дайджест, архив, шум, дубль). Дедуп такую не прячет и не
# склеивает с другой разобранной; её ссылки радар не приносит повторно.
_SIGNAL_REVIEWED_SQL = """(
  EXISTS (SELECT 1 FROM signal_feedback_events sfe_r
          WHERE sfe_r.signal_id = {alias}.id
            AND (COALESCE(sfe_r.verdict, '') <> '' OR sfe_r.event_type = 'comment_added'))
  OR EXISTS (SELECT 1 FROM user_signal_states uss_r
             WHERE uss_r.signal_id = {alias}.id AND uss_r.status <> 'watch')
)"""


def list_signals_for_dedup(*, window_days: int = 30, fresh_days: int = 3, limit: int = 300) -> list[dict]:
    """Сохранённые карточки радара для сверки дублей на внешнем воркере (базы у него нет).

    Разобранные Виктором — всегда, любого возраста: по ним видно, что он уже одобрил
    или отклонил, и повтор того же события не должен вернуться новой карточкой.
    Неразобранные — за окно. fresh — появилась за последние дни: только такие
    неразобранные карточки сверяются между собой (см. signal_dedup._eligible)."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT s.id, s.signal_key, s.title, s.title_ru, s.theme,
                   left(s.summary, 400) AS summary,
                   s.companies_json AS companies, s.score, s.evidence_count,
                   (s.first_seen_at >= now() - make_interval(days => %s)) AS fresh,
                   to_char(s.first_seen_at AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD') AS first_seen_day,
                   v.verdict,
                   {reviewed} AS reviewed,
                   COALESCE(ev.urls, ARRAY[]::text[]) AS evidence_urls
            FROM signals s
            LEFT JOIN LATERAL (
              SELECT sfe.verdict
              FROM signal_feedback_events sfe
              WHERE sfe.signal_id = s.id AND COALESCE(sfe.verdict, '') <> ''
              ORDER BY sfe.created_at DESC, sfe.id DESC
              LIMIT 1
            ) v ON TRUE
            LEFT JOIN LATERAL (
              SELECT array_agg(top.source_url ORDER BY top.strength DESC, top.id) AS urls
              FROM (
                SELECT source_url, strength, id
                FROM signal_evidence
                WHERE signal_id = s.id
                ORDER BY strength DESC, id
                LIMIT 10
              ) top
            ) ev ON TRUE
            WHERE s.merged_into_signal_id IS NULL
              AND ({reviewed} OR s.last_seen_at >= now() - make_interval(days => %s))
            ORDER BY {reviewed} DESC, s.last_seen_at DESC, s.id DESC
            LIMIT %s
            """.format(reviewed=_SIGNAL_REVIEWED_SQL.format(alias="s")),
            (fresh_days, window_days, limit),
        )
        rows = cur.fetchall()
    return [
        {**row, "score": float(row["score"] or 0), "companies": list(row["companies"] or []),
         "evidence_urls": list(row["evidence_urls"] or [])}
        for row in rows
    ]


def resolve_signal_merge_root(signal_id: int) -> int | None:
    """Главная карточка группы: идти по merged_into_signal_id до видимой. None — нет такой."""
    with get_connection() as conn:
        current = int(signal_id)
        for _ in range(10):
            row = conn.execute("SELECT merged_into_signal_id FROM signals WHERE id = %s", (current,)).fetchone()
            if row is None:
                return None
            if row[0] is None:
                return current
            current = int(row[0])
    return None


def mark_signal_merged(signal_id: int, into_signal_id: int, reason: str = "") -> bool:
    """Скрыть карточку-дубль со ссылкой на главную. Не удаляет: пометку снимает UPDATE.

    Разобранную карточку не прячем, даже если судья счёл её дублем: между выдачей
    задачи и её завершением её могли успеть разобрать или выбрать в дайджест."""
    root = resolve_signal_merge_root(into_signal_id)
    if root is None or root == int(signal_id):
        return False
    with get_connection() as conn:
        row = conn.execute(
            """
            UPDATE signals
            SET merged_into_signal_id = %s, merge_reason = %s, updated_at = now()
            WHERE id = %s
              AND merged_into_signal_id IS NULL
              AND NOT {reviewed}
            RETURNING id
            """.format(reviewed=_SIGNAL_REVIEWED_SQL.format(alias="signals")),
            (root, (reason or "")[:500] or None, int(signal_id)),
        ).fetchone()
        if row is not None:
            # Группа всегда в один шаг: всё, что было склеено с этой карточкой, — к корню.
            conn.execute(
                "UPDATE signals SET merged_into_signal_id = %s, updated_at = now() WHERE merged_into_signal_id = %s",
                (root, int(signal_id)),
            )
        conn.commit()
        return row is not None


def touch_signal(signal_id: int) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE signals SET last_seen_at = now(), updated_at = now() WHERE id = %s", (int(signal_id),))
        conn.commit()


def signal_key_owners(keys: list[str]) -> dict[str, dict]:
    """Каким сохранённым карточкам уже принадлежат ключи кандидатов: id, склеена ли, разобрана ли."""
    if not keys:
        return {}
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT s.signal_key, s.id, s.merged_into_signal_id, {reviewed} AS reviewed
            FROM signals s
            WHERE s.signal_key = ANY(%s)
            """.format(reviewed=_SIGNAL_REVIEWED_SQL.format(alias="s")),
            (list(keys),),
        )
        return {row["signal_key"]: row for row in cur.fetchall()}


def create_signal_generation_run(
    *,
    config_payload: dict,
    trigger: str | None = None,
    background_job_id: int | None = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO signal_generation_runs (background_job_id, trigger, status, config_json)
            VALUES (%s, %s, 'running', %s)
            RETURNING id
            """,
            (background_job_id, trigger, Json(_jsonable(config_payload))),
        )
        run_id = int(cur.fetchone()[0])
        conn.commit()
        return run_id


def finish_signal_generation_run(
    run_id: int,
    *,
    status: str,
    result: dict | None = None,
    error_message: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE signal_generation_runs
            SET status = %s,
                result_json = %s,
                error_message = %s,
                finished_at = now()
            WHERE id = %s
            """,
            (status, Json(_jsonable(result or {})), error_message, run_id),
        )
        conn.commit()


def create_signal_training_example(
    *,
    generation_run_id: int | None,
    signal_id: int | None = None,
    topic: str,
    signal_key: str | None,
    pipeline_verdict: str,
    input_payload: dict,
    raw_output: dict | None = None,
    normalized_output: dict | None = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO signal_training_examples (
              generation_run_id, signal_id, topic, signal_key, pipeline_verdict,
              input_json, raw_output_json, normalized_output_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                generation_run_id,
                signal_id,
                topic,
                signal_key,
                pipeline_verdict,
                Json(_jsonable(input_payload)),
                Json(_jsonable(raw_output or {})),
                Json(_jsonable(normalized_output or {})),
            ),
        )
        example_id = int(cur.fetchone()[0])
        conn.commit()
        return example_id


def attach_feedback_to_signal_training_examples(
    feedback_event_id: int,
    *,
    signal_id: int | None = None,
    limit: int = 20,
) -> int:
    if signal_id is None:
        return 0
    with get_connection() as conn:
        cur = conn.execute(
            """
            WITH target AS (
              SELECT id
              FROM signal_training_examples
              WHERE signal_id = %s
                AND feedback_event_id IS NULL
              ORDER BY created_at DESC, id DESC
              LIMIT %s
            )
            UPDATE signal_training_examples ste
            SET feedback_event_id = %s,
                updated_at = now()
            FROM target
            WHERE ste.id = target.id
            """,
            (signal_id, limit, feedback_event_id),
        )
        conn.commit()
        return cur.rowcount or 0


def list_signal_training_examples(
    *,
    limit: int = 1000,
    with_feedback_only: bool = True,
    verdict: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if with_feedback_only:
        clauses.append("ste.feedback_event_id IS NOT NULL")
    if verdict:
        clauses.append("sfe.verdict = %s")
        params.append(verdict)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT
              ste.id,
              ste.generation_run_id,
              ste.signal_id,
              ste.feedback_event_id,
              ste.topic,
              ste.signal_key,
              ste.pipeline_verdict,
              ste.input_json,
              ste.raw_output_json,
              ste.normalized_output_json,
              ste.created_at,
              ste.updated_at,
              sfe.event_type AS feedback_event_type,
              sfe.verdict AS feedback_verdict,
              sfe.reason AS feedback_reason,
              sfe.corrected_title AS feedback_corrected_title,
              sfe.corrected_thesis AS feedback_corrected_thesis,
              sfe.duplicate_of_signal_id AS feedback_duplicate_of_signal_id,
              sfe.comment AS feedback_comment,
              sfe.created_at AS feedback_created_at,
              s.title AS signal_title,
              s.title_ru AS signal_title_ru,
              s.theme AS signal_theme,
              s.score AS signal_score,
              s.maturity AS signal_maturity
            FROM signal_training_examples ste
            LEFT JOIN signal_feedback_events sfe ON sfe.id = ste.feedback_event_id
            LEFT JOIN signals s ON s.id = ste.signal_id
            {where}
            ORDER BY ste.created_at DESC, ste.id DESC
            LIMIT %s
            """,
            params,
        )
        return list(cur.fetchall())


def list_signals(*, maturity: str | None = None, theme: str | None = None, limit: int = 50,
                 user_id: int | None = None) -> list[dict]:
    # Дубли скрыты: их ссылки уже в главной карточке (signal_dedup).
    clauses = ["s.merged_into_signal_id IS NULL"]
    params: list = []
    if maturity:
        clauses.append("s.maturity = %s")
        params.append(maturity)
    if theme:
        clauses.append("s.theme ILIKE %s")
        params.append(f"%{theme}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params = [user_id, *params, limit]
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT s.*,
                   COALESCE(uss.status, 'watch') AS user_status,
                   (COALESCE(uss.status, 'watch') = 'digest') AS selected_for_digest,
                   uss.analyst_comment AS user_comment,
                   uss.updated_at AS user_status_updated_at,
                   (
                     SELECT COUNT(*)
                     FROM signal_feedback_events sfe
                     WHERE sfe.signal_id = s.id
                        OR (sfe.source_url IS NOT NULL AND EXISTS (
                             SELECT 1 FROM signal_evidence se
                             WHERE se.signal_id = s.id AND se.source_url = sfe.source_url
                           ))
                   ) AS feedback_count,
                   (SELECT COUNT(*) FROM signals m WHERE m.merged_into_signal_id = s.id) AS merged_count
            FROM signals s
            LEFT JOIN user_signal_states uss ON uss.signal_id = s.id AND uss.user_id = %s
            {where}
            ORDER BY s.score DESC, s.last_seen_at DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def set_user_signal_status(
    user_id: int,
    signal_id: int,
    *,
    status: str | None = None,
    analyst_comment: str | None = None,
) -> None:
    if status is None and analyst_comment is None:
        return
    with get_connection() as conn:
        exists = conn.execute("SELECT 1 FROM signals WHERE id = %s", (signal_id,)).fetchone()
        if not exists:
            raise KeyError(f"Signal not found: {signal_id}")
        conn.execute(
            """
            INSERT INTO user_signal_states (user_id, signal_id, status, analyst_comment)
            VALUES (%s, %s, COALESCE(%s, 'watch'), %s)
            ON CONFLICT (user_id, signal_id) DO UPDATE SET
              status = COALESCE(%s, user_signal_states.status),
              analyst_comment = COALESCE(%s, user_signal_states.analyst_comment),
              updated_at = now()
            """,
            (user_id, signal_id, status, analyst_comment, status, analyst_comment),
        )
        conn.commit()


def list_signal_evidence(signal_id: int, *, limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT *
            FROM signal_evidence
            WHERE signal_id = %s
               OR signal_id IN (SELECT id FROM signals WHERE merged_into_signal_id = %s)
            ORDER BY strength DESC, published_at DESC NULLS LAST, created_at DESC
            LIMIT %s
            """,
            (signal_id, signal_id, limit),
        )
        return cur.fetchall()


def create_agent_task(
    kind: str,
    *,
    topic: str | None = None,
    payload: dict | None = None,
    budget: dict | None = None,
    status: str = "planned",
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_tasks (kind, status, topic, payload_json, budget_json)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (kind, status, topic, Json(_jsonable(payload or {})), Json(_jsonable(budget or {}))),
        )
        task_id = int(cur.fetchone()[0])
        conn.commit()
        return task_id


def create_agent_run(
    kind: str,
    *,
    trigger: str | None = None,
    payload: dict | None = None,
    status: str = "running",
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_runs (kind, status, trigger, payload_json, started_at)
            VALUES (%s, %s, %s, %s, now())
            RETURNING id
            """,
            (kind, status, trigger, Json(_jsonable(payload or {}))),
        )
        run_id = int(cur.fetchone()[0])
        conn.commit()
        return run_id


def finish_agent_run(
    run_id: int,
    *,
    status: str = "ok",
    result: dict | None = None,
    error_message: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE agent_runs
            SET status = %s,
                result_json = %s,
                error_message = %s,
                finished_at = now()
            WHERE id = %s
            """,
            (status, Json(_jsonable(result or {})), error_message, run_id),
        )
        conn.commit()


def list_agent_runs(*, status: str | None = None, limit: int = 50) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("ar.status = %s")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT
              ar.*,
              COUNT(DISTINCT aa.id)::int AS action_count,
              COUNT(DISTINCT bj.id)::int AS job_count,
              COUNT(DISTINCT bj.id) FILTER (WHERE bj.status = 'ok')::int AS ok_job_count,
              COUNT(DISTINCT bj.id) FILTER (WHERE bj.status = 'failed')::int AS failed_job_count
            FROM agent_runs ar
            LEFT JOIN agent_actions aa ON aa.run_id = ar.id
            LEFT JOIN background_jobs bj ON bj.agent_run_id = ar.id
            {where}
            GROUP BY ar.id
            ORDER BY ar.created_at DESC, ar.id DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def record_agent_action(
    task_id: int | None,
    action_type: str,
    *,
    run_id: int | None = None,
    input_payload: dict | None = None,
    output_payload: dict | None = None,
    cost_usd: float = 0,
    duration_ms: int | None = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_actions
              (run_id, task_id, action_type, input_json, output_json, cost_usd, duration_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (run_id, task_id, action_type, Json(_jsonable(input_payload or {})), Json(_jsonable(output_payload or {})),
             cost_usd, duration_ms),
        )
        action_id = int(cur.fetchone()[0])
        conn.commit()
        return action_id


def list_agent_actions(
    *,
    action_type: str | None = None,
    run_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if action_type:
        clauses.append("aa.action_type = %s")
        params.append(action_type)
    if run_id is not None:
        clauses.append("aa.run_id = %s")
        params.append(run_id)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT
              aa.*,
              at.kind AS task_kind,
              at.status AS task_status,
              at.topic AS task_topic
            FROM agent_actions aa
            LEFT JOIN agent_tasks at ON at.id = aa.task_id
            {where}
            ORDER BY aa.created_at DESC
            LIMIT %s
            """,
            params,
        )
        return [_with_agent_action_summary(row) for row in cur.fetchall()]


def _with_agent_action_summary(row: dict) -> dict:
    output = row.get("output_json") or {}
    input_payload = row.get("input_json") or {}
    action_type = str(row.get("action_type") or "")
    title = action_type
    summary = "Детали записаны в журнал действий"
    tone = "neutral"
    if action_type == "source_discovery_plan_built":
        title = "План построен"
        policy = output.get("policy") or {}
        summary = (
            f"Авто: {policy.get('auto', 0)}, вручную: {policy.get('human_review', 0)}, "
            f"запрещено: {policy.get('blocked', 0)}."
        )
    elif action_type == "discover_sources_finished":
        title = "Поиск источников завершён"
        candidates = output.get("candidates") or []
        candidate_count = candidates if isinstance(candidates, int | float) else len(candidates)
        topic = output.get("topic") or input_payload.get("topic") or row.get("task_topic")
        summary = f"Тема: {topic or '—'}, кандидатов: {candidate_count}, поиск: {(output.get('search') or {}).get('status', '—')}."
        tone = "good" if candidate_count else "neutral"
    elif action_type == "source_discovery_loop_iteration":
        title = "Итерация агента"
        observations = output.get("observations") or []
        candidates = sum(int((item or {}).get("candidate_count") or 0) for item in observations)
        queued = sum(int((item or {}).get("evaluation_jobs") or 0) for item in observations)
        summary = f"Наблюдений: {len(observations)}, кандидатов: {candidates}, AI-оценок в очереди: {queued}."
        tone = "good" if candidates else "neutral"
    elif action_type == "source_discovery_loop_budget_stop":
        title = "Агент остановлен бюджетом"
        summary = f"Причина: {output.get('terminal_reason') or (output.get('budget') or {}).get('reason') or 'лимит'}."
        tone = "warning"
    elif action_type == "source_candidate_learning":
        title = "Агент обучился"
        summary = (
            f"Кандидат #{output.get('candidate_id', '—')}, тема: {output.get('topic') or '—'}, "
            f"домен: {output.get('domain') or '—'}, релевантных: {output.get('relevant_articles', 0)}, "
            f"оценка памяти: {output.get('score', '—')}."
        )
        tone = "good" if str(output.get("recommended_action") or "") in {"add", "test_more"} else "neutral"
    elif action_type == "approve_source_candidate":
        title = "Кандидат одобрен"
        summary = f"Создан источник #{output.get('source_id', '—')}, первый сбор: {output.get('initial_job_id') or 'не ставился'}."
        tone = "good"
    elif action_type == "update_agent_memory":
        title = "Память изменена"
        summary = f"Запись #{input_payload.get('memory_id', '—')} переведена в статус {input_payload.get('status', '—')}."
    elif action_type == "create_agent_memory":
        title = "Правило добавлено"
        summary = (
            f"{output.get('memory_type') or input_payload.get('memory_type') or 'память'}: "
            f"{output.get('subject') or input_payload.get('subject') or '—'}, "
            f"статус: {output.get('status') or input_payload.get('status') or '—'}."
        )
        tone = "warning" if (output.get("status") or input_payload.get("status")) == "rejected" else "good"
    elif action_type == "update_source_candidate":
        title = "Кандидат изменён"
        summary = f"Кандидат #{input_payload.get('candidate_id', '—')}, статус: {input_payload.get('status', '—')}."
    row["decision_title"] = title
    row["decision_summary"] = summary
    row["decision_tone"] = tone
    return row


def source_discovery_daily_usage() -> dict[str, int]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT
              (
                SELECT COUNT(*)::int
                FROM agent_runs
                WHERE kind = 'source_discovery_loop'
                  AND created_at >= date_trunc('day', now())
              ) AS loop_runs,
              (
                SELECT COUNT(*)::int
                FROM agent_actions
                WHERE action_type = 'create_source_candidate'
                  AND created_at >= date_trunc('day', now())
              ) AS candidates_created,
              (
                SELECT COUNT(*)::int
                FROM background_jobs
                WHERE kind = 'source_candidate_evaluate'
                  AND created_at >= date_trunc('day', now())
              ) AS candidate_evaluations
            """,
        )
        row = cur.fetchone() or {}
        return {
            "loop_runs": int(row.get("loop_runs") or 0),
            "candidates_created": int(row.get("candidates_created") or 0),
            "candidate_evaluations": int(row.get("candidate_evaluations") or 0),
        }


def upsert_agent_memory(
    *,
    memory_key: str,
    memory_type: str,
    subject: str,
    status: str = "active",
    score: float = 0,
    facts: dict | None = None,
) -> int:
    key = memory_key.strip()
    if not key:
        raise ValueError("agent memory_key is required")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_memory
              (memory_key, memory_type, subject, status, score, facts_json, last_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (memory_key) DO UPDATE SET
              memory_type = EXCLUDED.memory_type,
              subject = EXCLUDED.subject,
              status = EXCLUDED.status,
              score = EXCLUDED.score,
              facts_json = EXCLUDED.facts_json,
              last_seen_at = now(),
              updated_at = now()
            RETURNING id
            """,
            (key, memory_type, subject, status, score, Json(_jsonable(facts or {}))),
        )
        memory_id = int(cur.fetchone()[0])
        conn.commit()
        return memory_id


def list_agent_memory(
    *,
    memory_type: str | None = None,
    status: str | None = "active",
    limit: int = 50,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if memory_type:
        clauses.append("memory_type = %s")
        params.append(memory_type)
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT *
            FROM agent_memory
            {where}
            ORDER BY score DESC, updated_at DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def upsert_signal_agent_memory(
    *,
    memory_key: str,
    memory_type: str,
    subject: str,
    status: str = "active",
    score: float = 0,
    facts: dict | None = None,
) -> int:
    key = memory_key.strip()
    if not key:
        raise ValueError("signal agent memory_key is required")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO signal_agent_memory
              (memory_key, memory_type, subject, status, score, facts_json, last_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (memory_key) DO UPDATE SET
              memory_type = EXCLUDED.memory_type,
              subject = EXCLUDED.subject,
              status = EXCLUDED.status,
              score = EXCLUDED.score,
              facts_json = EXCLUDED.facts_json,
              last_seen_at = now(),
              updated_at = now()
            RETURNING id
            """,
            (key, memory_type, subject, status, score, Json(_jsonable(facts or {}))),
        )
        memory_id = int(cur.fetchone()[0])
        conn.commit()
        return memory_id


def list_signal_agent_memory(
    *,
    memory_type: str | None = None,
    status: str | None = "active",
    limit: int = 50,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if memory_type:
        clauses.append("memory_type = %s")
        params.append(memory_type)
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT *
            FROM signal_agent_memory
            {where}
            ORDER BY score DESC, updated_at DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def merge_signal_agent_memory_facts(memory_id: int, patch: dict) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE signal_agent_memory SET facts_json = facts_json || %s::jsonb, updated_at = now() WHERE id = %s",
            (Json(_jsonable(patch)), memory_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_signal_theme(signal_id: int) -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT theme FROM signals WHERE id = %s", (signal_id,)).fetchone()
        return str(row[0]) if row and row[0] else None


def set_signal_agent_memory_status(memory_id: int, status: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE signal_agent_memory
            SET status = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (status, memory_id),
        )
        conn.commit()
        return cur.rowcount > 0


def list_reviewed_signal_urls() -> list[str]:
    """Адреса материалов, по которым человек уже вынес суждение о сигнале.

    Радар не должен приносить их снова: отклонённое вернулось бы тем же сигналом,
    а одобренное перезаписалось бы новой оценкой модели поверх разобранного
    (signal_evidence.source_url уникален — повторная находка забирает материал себе)."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            WITH reviewed AS (
                SELECT f.signal_id AS id FROM signal_feedback_events f
                WHERE f.signal_id IS NOT NULL
                  AND (f.verdict IS NOT NULL OR f.event_type = 'comment_added')
            )
            SELECT DISTINCT e.source_url
            FROM signal_evidence e
            WHERE e.signal_id IN (SELECT id FROM reviewed)
               -- ссылки скрытых дублей разобранной карточки — тоже разобраны
               OR e.signal_id IN (SELECT s.id FROM signals s WHERE s.merged_into_signal_id IN (SELECT id FROM reviewed))
            UNION
            SELECT DISTINCT f.source_url
            FROM signal_feedback_events f
            WHERE f.signal_id IS NOT NULL
              AND COALESCE(f.source_url, '') <> ''
            """
        )
        return [str(row[0]) for row in cur.fetchall() if row[0]]


def update_agent_memory_status(memory_id: int, status: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE agent_memory
            SET status = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (status, memory_id),
        )
        conn.commit()
        return cur.rowcount > 0


def query_memory_report(*, status: str | None = "active", limit: int = 20) -> list[dict]:
    rows = list_agent_memory(memory_type="query", status=status, limit=limit)
    report = []
    for row in rows:
        facts = row.get("facts_json") or {}
        tested = int(facts.get("tested_articles") or 0)
        relevant = int(facts.get("relevant_articles") or 0)
        report.append({
            "query": row.get("subject"),
            "topic": facts.get("topic"),
            "score": float(row.get("score") or 0),
            "status": row.get("status"),
            "found_candidates": int(facts.get("found_candidates") or 0),
            "tested_articles": tested,
            "relevant_articles": relevant,
            "avg_score": facts.get("avg_score"),
            "empty_result": bool(facts.get("empty_result") or False),
            "relevance_rate": round(relevant / max(tested, 1), 3) if tested else 0.0,
            "last_seen_at": row.get("last_seen_at"),
            "updated_at": row.get("updated_at"),
        })
    return report


def compute_source_quality_rows(period_from: datetime, period_to: datetime) -> list[dict]:
    """Посчитать качество источников за период без записи в историю."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            WITH article_metrics AS (
              SELECT
                a.id,
                a.source_id,
                c.summary,
                c.relevant,
                c.status AS card_status,
                sc.total_score,
                EXISTS (
                  SELECT 1 FROM user_article_states uas
                  WHERE uas.article_id = a.id AND uas.status = 'digest'
                ) AS user_digest,
                EXISTS (
                  SELECT 1 FROM user_article_states uas
                  WHERE uas.article_id = a.id AND uas.status = 'duplicate'
                ) AS user_duplicate,
                EXISTS (
                  SELECT 1 FROM user_article_states uas
                  WHERE uas.article_id = a.id AND uas.status = 'noise'
                ) AS user_noise,
                EXISTS (SELECT 1 FROM article_tags at WHERE at.article_id = a.id) AS has_tag,
                COALESCE((
                  SELECT SUM(r.cost_usd)
                  FROM ai_processing_runs r
                  WHERE r.article_id = a.id AND r.status = 'ok'
                ), 0) AS processing_cost_usd
              FROM articles a
              LEFT JOIN article_cards c ON c.article_id = a.id
              LEFT JOIN article_scores sc ON sc.article_id = a.id
              WHERE a.collected_at >= %s
                AND a.collected_at < %s
            )
            SELECT
              s.id AS source_id,
              s.name AS source_name,
              s.enabled,
              s.parse_strategy,
              s.source_type,
              s.update_frequency,
              s.network_region,
              s.network_profile,
              s.last_ru_probe_status,
              s.last_external_probe_status,
              s.external_required_reason,
              s.last_seen_published_at,
              COUNT(am.id) AS articles_found,
              COUNT(am.id) FILTER (
                WHERE am.summary IS NOT NULL
                   OR am.relevant IS NOT NULL
                   OR am.has_tag
                   OR am.total_score IS NOT NULL
              ) AS articles_processed,
              COUNT(am.id) FILTER (WHERE am.relevant IS TRUE) AS relevant_count,
              COUNT(am.id) FILTER (WHERE am.relevant IS FALSE OR am.card_status = 'rejected') AS rejected_count,
              ROUND(AVG(am.total_score), 2) AS avg_score,
              COUNT(am.id) FILTER (WHERE am.user_digest OR am.card_status = 'digest') AS digest_count,
              COUNT(am.id) FILTER (WHERE am.user_duplicate OR am.card_status = 'duplicate') AS duplicate_count,
              COUNT(am.id) FILTER (WHERE am.user_noise OR am.card_status = 'noise') AS noise_count,
              COALESCE(ROUND(SUM(am.processing_cost_usd), 6), 0) AS processing_cost_usd
            FROM sources s
            LEFT JOIN article_metrics am ON am.source_id = s.id
            GROUP BY s.id, s.name, s.enabled, s.parse_strategy, s.source_type,
              s.update_frequency, s.network_region, s.network_profile,
              s.last_ru_probe_status, s.last_external_probe_status,
              s.external_required_reason, s.last_seen_published_at
            ORDER BY articles_found DESC, s.name
            """,
            (period_from, period_to),
        )
        rows = []
        for row in cur.fetchall():
            item = dict(row)
            item["quality_score"] = _source_quality_score(item)
            rows.append(item)
        return rows


def compute_topic_gap_rows(
    period_from: datetime,
    period_to: datetime,
    target_per_topic: int = 10,
    limit: int = 10,
) -> list[dict]:
    """Темы/теги с дефицитом релевантных сигналов за период."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            WITH tagged_articles AS (
              SELECT
                COALESCE(parent.name, t.name) AS topic,
                a.id AS article_id,
                c.relevant,
                c.status AS card_status,
                sc.total_score,
                EXISTS (
                  SELECT 1 FROM user_article_states uas
                  WHERE uas.article_id = a.id AND uas.status = 'digest'
                ) AS user_digest
              FROM tags t
              LEFT JOIN tags parent ON parent.id = t.parent_id
              LEFT JOIN article_tags at ON at.tag_id = t.id
              LEFT JOIN articles a
                     ON a.id = at.article_id
                    AND a.collected_at >= %s
                    AND a.collected_at < %s
              LEFT JOIN article_cards c ON c.article_id = a.id
              LEFT JOIN article_scores sc ON sc.article_id = a.id
              WHERE t.enabled = TRUE
            )
            SELECT
              topic,
              COUNT(article_id) FILTER (WHERE relevant IS TRUE) AS signals,
              ROUND(AVG(total_score), 2) AS avg_score,
              COUNT(article_id) FILTER (WHERE user_digest OR card_status = 'digest') AS digest_count
            FROM tagged_articles
            GROUP BY topic
            ORDER BY GREATEST(%s - COUNT(article_id) FILTER (WHERE relevant IS TRUE), 0) DESC,
                     topic
            LIMIT %s
            """,
            (period_from, period_to, target_per_topic, limit),
        )
        rows = []
        for row in cur.fetchall():
            signals = int(row["signals"] or 0)
            gap = max(int(target_per_topic) - signals, 0)
            rows.append({
                "topic": row["topic"],
                "signals": signals,
                "target_signals": int(target_per_topic),
                "gap": gap,
                "avg_score": row.get("avg_score"),
                "digest_count": int(row["digest_count"] or 0),
                "priority": round(min(100, gap / max(target_per_topic, 1) * 100), 2),
            })
        return rows


def snapshot_source_quality(period_from: datetime, period_to: datetime) -> int:
    rows = compute_source_quality_rows(period_from, period_to)
    if not rows:
        return 0
    with get_connection() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO source_quality_snapshots (
                  source_id, period_from, period_to, articles_found, articles_processed,
                  relevant_count, rejected_count, avg_score, digest_count, duplicate_count,
                  noise_count, processing_cost_usd, quality_score
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row["source_id"], period_from, period_to, row["articles_found"],
                    row["articles_processed"], row["relevant_count"], row["rejected_count"],
                    row["avg_score"], row["digest_count"], row["duplicate_count"],
                    row["noise_count"], row["processing_cost_usd"], row["quality_score"],
                ),
            )
        conn.commit()
    return len(rows)


def _source_quality_score(row: dict) -> float:
    found = max(int(row.get("articles_found") or 0), 1)
    relevant_rate = int(row.get("relevant_count") or 0) / found
    digest_rate = int(row.get("digest_count") or 0) / found
    duplicate_rate = int(row.get("duplicate_count") or 0) / found
    noise_rate = int(row.get("noise_count") or 0) / found
    avg_score = float(row.get("avg_score") or 0) / 100
    score = (
        relevant_rate * 35
        + avg_score * 25
        + digest_rate * 25
        - duplicate_rate * 10
        - noise_rate * 5
    )
    return round(max(0.0, min(100.0, score)), 2)


def article_exists(url: str) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT 1 FROM articles WHERE url = %s", (url,)).fetchone()
        return row is not None


def set_source_collection(source_id: int, *, listing_url: str | None, network_region: str,
                          enabled: bool) -> None:
    """Записать, чем и откуда собирать источник, — итог перебора стратегий.

    Архивный источник этим не воскрешается: `enabled` ставится только вместе с
    `archived_at IS NULL`, как и в add_rss_source.
    """
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE sources
            SET listing_url = %s, network_region = %s,
                enabled = (%s AND archived_at IS NULL), updated_at = now()
            WHERE id = %s
            """,
            (listing_url, network_region, bool(enabled), int(source_id)),
        )
        conn.commit()


def recent_article_urls_for_site(site_url: str | None, limit: int = 500) -> list[str]:
    """Последние адреса статей этого сайта и его поддоменов — от любого источника.

    Для внешнего воркера: базы у него нет, и без этого списка он качает каждую статью
    листинга на каждом цикле. Сайт, а не источник: у JPT восемь разделов-источников
    делят одни статьи, и знакомую для соседа статью качать тоже незачем.
    """
    host = (urlsplit(site_url or "").netloc or "").lower().removeprefix("www.")
    if not host:
        return []
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT url FROM articles
            WHERE lower(split_part(url, '/', 3)) IN (%s, %s)
               OR lower(split_part(url, '/', 3)) LIKE %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (host, "www." + host, "%." + host, int(limit)),
        ).fetchall()
    return [row[0] for row in rows]


def update_source_request_state(
    source_id: int,
    *,
    last_seen_article_url: str | None = None,
    last_seen_published_at=None,
    last_listing_hash: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE sources
            SET last_seen_article_url = COALESCE(%s, last_seen_article_url),
                last_seen_published_at = COALESCE(%s, last_seen_published_at),
                last_listing_hash = COALESCE(%s, last_listing_hash),
                updated_at = now()
            WHERE id = %s
            """,
            (last_seen_article_url, last_seen_published_at, last_listing_hash, source_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
#  auth
# ---------------------------------------------------------------------------

def create_user(email: str, password: str, role: str = "user") -> dict:
    email = auth.normalize_email(email)
    role = role if role in ("admin", "user") else "user"
    salt_hex, password_hash = auth.hash_password(password)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        if cur.fetchone() is not None:
            raise ValueError("Пользователь с таким email уже существует")
        cur.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES (%s, %s, %s, %s)
            RETURNING id, email, role, created_at
            """,
            (email, salt_hex, password_hash, role),
        )
        user = cur.fetchone()
        conn.commit()
        return user


def authenticate_user(email: str, password: str) -> dict | None:
    email = auth.normalize_email(email)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, email, role, password_salt, password_hash, created_at FROM users WHERE email = %s",
            (email,),
        )
        user = cur.fetchone()
        if user is None:
            return None
        if not auth.verify_password(password, user["password_salt"], user["password_hash"]):
            return None
        return {"id": user["id"], "email": user["email"], "role": user["role"], "created_at": user["created_at"]}


def create_user_session(user_id: int) -> str:
    token = auth.create_session_token()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_sessions (user_id, session_token, expires_at)
            VALUES (%s, %s, now() + %s::interval)
            """,
            (user_id, token, f"{config.AUTH_SESSION_DAYS} days"),
        )
        conn.commit()
    return token


def get_user_by_session(session_token: str) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT u.id, u.email, u.role, u.created_at
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.session_token = %s
              AND s.expires_at > now()
            """,
            (session_token,),
        )
        user = cur.fetchone()
        if user is not None:
            conn.execute(
                "UPDATE user_sessions SET last_seen_at = now() WHERE session_token = %s",
                (session_token,),
            )
            conn.commit()
        return user


def delete_user_session(session_token: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM user_sessions WHERE session_token = %s", (session_token,))
        conn.commit()


def delete_expired_user_sessions() -> int:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM user_sessions WHERE expires_at <= now()")
        conn.commit()
        return cur.rowcount or 0


def count_expired_user_sessions() -> int:
    with get_connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM user_sessions WHERE expires_at <= now()").fetchone()[0])


def count_users() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def list_users() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, email, role, created_at FROM users ORDER BY id")
        return cur.fetchall()


def get_user_by_id(user_id: int) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, email, role, created_at FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()


def set_user_role(user_id: int, role: str) -> None:
    role = role if role in ("admin", "user") else "user"
    with get_connection() as conn:
        conn.execute("UPDATE users SET role = %s, updated_at = now() WHERE id = %s", (role, user_id))
        conn.commit()


def set_user_password(user_id: int, password: str) -> None:
    salt_hex, password_hash = auth.hash_password(password)
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET password_salt = %s, password_hash = %s, updated_at = now() WHERE id = %s",
            (salt_hex, password_hash, user_id),
        )
        conn.commit()


def delete_user(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM user_sessions WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


def count_admins() -> int:
    with get_connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0])


def set_user_article_status(user_id: int, article_id: int, status: str | None = None,
                            analyst_comment: str | None = None) -> None:
    """Пер-юзерный рабочий статус статьи. status=None — не трогаем (только коммент)."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_article_states (user_id, article_id, status, analyst_comment)
            VALUES (%s, %s, COALESCE(%s, 'new'), %s)
            ON CONFLICT (user_id, article_id) DO UPDATE SET
              status = COALESCE(%s, user_article_states.status),
              analyst_comment = COALESCE(%s, user_article_states.analyst_comment),
              updated_at = now()
            """,
            (user_id, article_id, status, analyst_comment, status, analyst_comment),
        )
        conn.commit()


def migrate_global_status_to_user(user_id: int) -> int:
    """Разовый перенос текущих ГЛОБАЛЬНЫХ статусов (article_cards.status != 'new')
    в личное состояние указанного пользователя — чтобы его дайджест/работа сохранились
    при переходе на пер-юзерную модель. Идемпотентно (не перетирает уже заданные)."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO user_article_states (user_id, article_id, status, analyst_comment)
            SELECT %s, c.article_id, c.status, c.analyst_comment
            FROM article_cards c
            WHERE COALESCE(c.status, 'new') <> 'new'
            ON CONFLICT (user_id, article_id) DO NOTHING
            """,
            (user_id,),
        )
        conn.commit()
        return cur.rowcount or 0


def ensure_admin_bootstrap() -> int | None:
    """Если админов нет, а пользователи есть — назначить админом самого первого
    (по id). Возвращает id назначенного админа или None. Идемпотентно."""
    with get_connection() as conn:
        has_admin = conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        if has_admin:
            return None
        row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("UPDATE users SET role = 'admin', updated_at = now() WHERE id = %s", (row[0],))
        conn.commit()
        return int(row[0])


def create_export_job(export_type: str, export_format: str) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO export_jobs (export_type, format, status, started_at)
            VALUES (%s, %s, 'running', now())
            RETURNING id
            """,
            (export_type, export_format),
        )
        job_id = cur.fetchone()[0]
        conn.commit()
        return job_id


def finish_export_job(job_id: int, status: str, file_path: str | None = None,
                      error_message: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE export_jobs
            SET status = %s,
                file_path = COALESCE(%s, file_path),
                error_message = %s,
                finished_at = now()
            WHERE id = %s
            """,
            (status, file_path, error_message, job_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
#  background jobs
# ---------------------------------------------------------------------------

def create_background_job(
    kind: str,
    payload: dict | None = None,
    *,
    user_id: int | None = None,
    queue_name: str = "default",
    execution_region: str = "ru",
    capability: str | None = None,
    max_attempts: int = 3,
    agent_run_id: int | None = None,
) -> dict:
    # Агентные задачи — в свою полосу, как бы их ни назвал вызывающий код (lanes.route).
    queue_name = lanes.route(queue_name, kind)
    # Внешняя очередь принимает только то, что её воркер умеет исполнять (lanes.py).
    lanes.check_enqueue(queue_name, kind)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            INSERT INTO background_jobs (
                user_id, kind, queue_name, execution_region, capability,
                agent_run_id, status, progress, max_attempts, payload_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'queued', 0, %s, %s)
            RETURNING *
            """,
            (
                user_id,
                kind,
                queue_name,
                execution_region,
                capability,
                agent_run_id,
                max_attempts,
                Json(_jsonable(payload or {})),
            ),
        )
        job = cur.fetchone()
        conn.commit()
        return job


def has_recent_background_job(
    *,
    kind: str,
    payload_subset: dict,
    lookback_hours: int,
    statuses: tuple[str, ...] = ("queued", "running", "finalizing", "ok"),
) -> bool:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT 1
            FROM background_jobs
            WHERE kind = %s
              AND status = ANY(%s)
              AND created_at >= now() - (%s::text || ' hours')::interval
              AND payload_json @> %s::jsonb
            LIMIT 1
            """,
            (kind, list(statuses), lookback_hours, Json(_jsonable(payload_subset))),
        )
        return cur.fetchone() is not None


def get_background_job(job_id: int, *, user_id: int | None = None) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if user_id is None:
            cur.execute("SELECT * FROM background_jobs WHERE id = %s", (job_id,))
        else:
            cur.execute(
                "SELECT * FROM background_jobs WHERE id = %s AND user_id = %s",
                (job_id, user_id),
            )
        return cur.fetchone()


def claim_next_background_job(queue_names: list[str] | None = None) -> dict | None:
    """Atomically claim the oldest queued job for an external worker."""
    queue_names = queue_names or ["default"]
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            WITH next_job AS (
                SELECT id
                FROM background_jobs
                WHERE status = 'queued'
                  AND queue_name = ANY(%s)
                  AND run_after <= now()
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE background_jobs j
            SET status = 'running',
                progress = CASE WHEN j.progress < 10 THEN 10 ELSE j.progress END,
                attempts = j.attempts + 1,
                started_at = COALESCE(j.started_at, now()),
                error_message = NULL
            FROM next_job
            WHERE j.id = next_job.id
            RETURNING j.*
            """,
            (queue_names,),
        )
        job = cur.fetchone()
        conn.commit()
        return job


class RequeueOutcome(NamedTuple):
    """Итог разбора потерянных задач: сколько вернули в очередь и сколько похоронили."""

    requeued: int
    exhausted: int


def _requeue_or_exhaust(conn, *, lost_where: str, lost_params: tuple, reason: str) -> RequeueOutcome:
    """Разобрать задачи, признанные потерянными: вернуть в очередь либо закрыть как failed.

    ПЕРЕОЧЕРЕДЬ — ЭТО ТОЖЕ ПОПЫТКА. Пока `attempts` не сверялся с `max_attempts`, задача,
    которая физически не может завершиться, возвращалась в очередь вечно. Инцидент 24.07:
    задача 1181 (батч 800 статей) намотала 6 кругов при `max_attempts=3`, каждый круг
    начиная с первой статьи и заново оплачивая OpenAI, — а потолок попыток не срабатывал
    никогда, потому что его проверял только `fail_background_job`, до которого дело не доходило.
    Исчерпавшие лимит уходят в `failed`: дальше решает человек, а не бесконечный ретрай.

    `lost_where` — SQL-предикат «задача потеряна». Он приходит из кода модуля (константа),
    не из пользовательского ввода; переменная часть предиката передаётся через `lost_params`.
    Вызывающий отвечает ровно за одно: чем он доказывает потерю. Что делать дальше —
    политика, и она живёт здесь, в одном месте, для всех контуров.
    """
    exhausted = conn.execute(
        f"""
        UPDATE background_jobs
        SET status = 'failed',
            finished_at = now(),
            claimed_by = NULL,
            lease_token_hash = NULL,
            lease_expires_at = NULL,
            error_message = %s
        WHERE ({lost_where})
          AND attempts >= max_attempts
        """,
        (f"{reason}; исчерпаны попытки — автоматически не перезапускаем",) + lost_params,
    )
    requeued = conn.execute(
        f"""
        UPDATE background_jobs
        SET status = 'queued',
            progress = 0,
            started_at = NULL,
            claimed_by = NULL,
            lease_token_hash = NULL,
            lease_expires_at = NULL,
            error_message = %s
        WHERE ({lost_where})
          AND attempts < max_attempts
        """,
        (reason,) + lost_params,
    )
    return RequeueOutcome(requeued=requeued.rowcount or 0, exhausted=exhausted.rowcount or 0)


# Внешняя задача считается потерянной ТОЛЬКО по отсутствию валидного lease. Это её настоящий
# признак жизни: воркер шлёт heartbeat перед каждой статьёй, и пока он жив, lease продлевается.
#
# NULL здесь тоже потеря, и это НЕ придирка. Раньше у внешней задачи было две страховки:
# lease и уборщик по настенным часам. Вторую забрали (она и устраивала вечную петлю), значит
# первая обязана покрыть всё поле. Иначе `running` без lease не подберёт уже никто:
# `release_external_background_job_finalize` возвращает задачу в 'running' при откате
# finalize и прямо рассчитывает, что её подберёт «requeue_expired по истечении лиза
# / requeue_stale». Задача в 'running' без lease не имеет ВООБЩЕ никаких доказательств жизни —
# это ровно определение потерянной.
_EXTERNAL_LOST_WHERE = """
    status = 'running'
    AND execution_region = 'external'
    AND (lease_expires_at IS NULL OR lease_expires_at < now())
"""


def requeue_expired_external_leases() -> RequeueOutcome:
    """Разобрать внешние задачи с протухшим lease — воркер потерян."""
    with get_connection() as conn:
        outcome = _requeue_or_exhaust(
            conn,
            lost_where=_EXTERNAL_LOST_WHERE,
            lost_params=(),
            reason="Requeued after external worker lease expired",
        )
        conn.commit()
        return outcome


def claim_external_background_job(
    *,
    queue_names: list[str],
    capabilities: list[str],
    worker_id: str,
    lease_token_hash: str,
    lease_seconds: int,
) -> dict | None:
    """Atomically lease the oldest queued external job for a remote worker."""
    queue_names = queue_names or ["external-ai", "external-fetch", "external-playwright"]
    capabilities = capabilities or []
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            WITH next_job AS (
                SELECT id
                FROM background_jobs
                WHERE status = 'queued'
                  AND execution_region = 'external'
                  AND queue_name = ANY(%s)
                  AND (%s::text[] = '{}'::text[] OR capability IS NULL OR capability = ANY(%s))
                  AND run_after <= now()
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE background_jobs j
            SET status = 'running',
                progress = CASE WHEN j.progress < 10 THEN 10 ELSE j.progress END,
                attempts = j.attempts + 1,
                started_at = COALESCE(j.started_at, now()),
                claimed_by = %s,
                lease_token_hash = %s,
                lease_expires_at = now() + (%s::text || ' seconds')::interval,
                last_heartbeat_at = now(),
                error_message = NULL
            FROM next_job
            WHERE j.id = next_job.id
            RETURNING j.*
            """,
            (queue_names, capabilities, capabilities, worker_id, lease_token_hash, lease_seconds),
        )
        job = cur.fetchone()
        conn.commit()
        return job


def list_background_jobs(
    *,
    status: str | None = None,
    kind: str | None = None,
    queue_name: str | None = None,
    user_id: int | None = None,
    agent_run_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    clauses = []
    params: list = []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if kind:
        clauses.append("kind = %s")
        params.append(kind)
    if queue_name:
        clauses.append("queue_name = %s")
        params.append(queue_name)
    if user_id is not None:
        clauses.append("user_id = %s")
        params.append(user_id)
    if agent_run_id is not None:
        clauses.append("agent_run_id = %s")
        params.append(agent_run_id)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT *
            FROM background_jobs
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def background_job_status_counts(*, capability: str | None = None, kind_prefix: str | None = None) -> dict[str, int]:
    clauses: list[str] = []
    params: list = []
    if capability:
        clauses.append("capability = %s")
        params.append(capability)
    if kind_prefix:
        clauses.append("kind LIKE %s")
        params.append(f"{kind_prefix}%")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT status, COUNT(*)::int AS count
            FROM background_jobs
            {where}
            GROUP BY status
            """,
            params,
        )
        return {str(row["status"]): int(row["count"]) for row in cur.fetchall()}


_EXTERNAL_QUEUE_STATUS_COLUMNS = """
              COUNT(*) FILTER (WHERE status = 'queued') AS queued,
              COUNT(*) FILTER (WHERE status = 'running') AS running,
              COUNT(*) FILTER (WHERE status = 'finalizing') AS finalizing,
              COUNT(*) FILTER (WHERE status = 'failed') AS failed,
              COUNT(*) FILTER (WHERE status = 'ok') AS ok,
              MIN(created_at) FILTER (WHERE status = 'queued') AS oldest_queued_at,
              -- «Ждёт с» — от момента, когда задачу можно было взять: отложенная
              -- на повтор (run_after в будущем) — не застой.
              MIN(GREATEST(created_at, run_after)) FILTER (
                WHERE status = 'queued' AND run_after <= now()
              ) AS oldest_ready_at,
              MAX(last_heartbeat_at) FILTER (WHERE status = 'running') AS last_heartbeat_at,
              MAX(GREATEST(started_at, last_heartbeat_at, finished_at)) AS last_activity_at,
              COUNT(*) FILTER (
                WHERE status = 'running'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < now()
              ) AS expired_leases
"""
# Внешней считается и задача с чужим регионом в очереди external-*: такая ошибка
# маршрута как раз и должна быть видна, а не выпадать из сводки.
_EXTERNAL_JOBS_WHERE = "(execution_region = 'external' OR queue_name LIKE 'external%%')"


def external_queue_status() -> dict:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"SELECT {_EXTERNAL_QUEUE_STATUS_COLUMNS} FROM background_jobs WHERE {_EXTERNAL_JOBS_WHERE}"
        )
        totals = cur.fetchone() or {}
        cur.execute(
            f"""
            SELECT queue_name, {_EXTERNAL_QUEUE_STATUS_COLUMNS}
            FROM background_jobs
            WHERE {_EXTERNAL_JOBS_WHERE}
            GROUP BY queue_name
            ORDER BY queue_name
            """
        )
        queues = cur.fetchall()
    status = {
        "totals": dict(totals),
        "queues": [dict(row) for row in queues],
    }
    status["alerts"] = lanes.lane_alerts(status)
    return status


def mark_background_job_running(job_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE background_jobs
            SET status = 'running',
                progress = CASE WHEN progress < 10 THEN 10 ELSE progress END,
                attempts = attempts + 1,
                started_at = COALESCE(started_at, now()),
                error_message = NULL
            WHERE id = %s
            """,
            (job_id,),
        )
        conn.commit()


def update_background_job_progress(job_id: int, progress: float) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET progress = %s WHERE id = %s",
            (progress, job_id),
        )
        conn.commit()


def mark_background_job_ai_started(job_id: int) -> None:
    """Отметить, что локальная AI-обработка сделала первый вызов модели.

    Служит дискриминатором для requeue_stale_background_jobs: задачу с проставленным
    ai_started_at нельзя авто-перезапускать (повторный прогон = повторный расход OpenAI),
    а упавшую ДО первого вызова (ai_started_at IS NULL) — можно. progress для этого не
    годится: claim/mark-running форсят его в 10 ещё до тела обработчика, поэтому у любой
    running-задачи progress уже >0 независимо от того, был ли вызов модели."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET ai_started_at = now() WHERE id = %s",
            (job_id,),
        )
        conn.commit()


def update_external_background_job_progress(
    job_id: int,
    *,
    lease_token_hash: str,
    progress: float,
    lease_seconds: int | None = None,
) -> bool:
    with get_connection() as conn:
        if lease_seconds is None:
            cur = conn.execute(
                """
                UPDATE background_jobs
                SET progress = %s,
                    last_heartbeat_at = now()
                WHERE id = %s
                  AND status = 'running'
                  AND execution_region = 'external'
                  AND lease_token_hash = %s
                  AND lease_expires_at > now()
                """,
                (progress, job_id, lease_token_hash),
            )
        else:
            cur = conn.execute(
                """
                UPDATE background_jobs
                SET progress = %s,
                    last_heartbeat_at = now(),
                    lease_expires_at = now() + (%s::text || ' seconds')::interval
                WHERE id = %s
                  AND status = 'running'
                  AND execution_region = 'external'
                  AND lease_token_hash = %s
                  AND lease_expires_at > now()
                """,
                (progress, lease_seconds, job_id, lease_token_hash),
            )
        conn.commit()
        return bool(cur.rowcount)


def heartbeat_external_background_job(job_id: int, *, lease_token_hash: str, lease_seconds: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET last_heartbeat_at = now(),
                lease_expires_at = now() + (%s::text || ' seconds')::interval
            WHERE id = %s
              AND status = 'running'
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            """,
            (lease_seconds, job_id, lease_token_hash),
        )
        conn.commit()
        return bool(cur.rowcount)


def external_background_job_lease_is_active(job_id: int, *, lease_token_hash: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM background_jobs
            WHERE id = %s
              AND status = 'running'
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            """,
            (job_id, lease_token_hash),
        ).fetchone()
        return row is not None


def finish_background_job(job_id: int, result: dict | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE background_jobs
            SET status = 'ok',
                progress = 100,
                result_json = %s,
                error_message = NULL,
                finished_at = now()
            WHERE id = %s
            """,
            (Json(_jsonable(result or {})), job_id),
        )
        conn.commit()


def begin_external_background_job_finalize(job_id: int, *, lease_token_hash: str) -> bool:
    """Атомарно перевести внешнюю задачу running→finalizing под защитой лиза (баг T2).

    Закрывает окно двойного AI-расхода: применять результат и биллить ai_processing_runs
    можно ТОЛЬКО после успешного перехода в 'finalizing'. Пока задача 'finalizing',
    requeue_expired_external_leases (он трогает лишь status='running') её НЕ переотдаст,
    поэтому другой воркер не прогонит AI повторно. Возвращает True, если лиз ещё валиден
    и задача застолблена именно за этим воркером (его lease_token_hash)."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'finalizing',
                last_heartbeat_at = now()
            WHERE id = %s
              AND status = 'running'
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            """,
            (job_id, lease_token_hash),
        )
        conn.commit()
        return bool(cur.rowcount)


def release_external_background_job_finalize(job_id: int, *, lease_token_hash: str) -> bool:
    """Откатить finalizing→running, если применение результата упало (баг T2, восстановление).

    Без отката задача залипла бы в 'finalizing' навсегда. После отката её подберёт обычный
    путь восстановления (requeue_expired по истечении лиза / requeue_stale)."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'running'
            WHERE id = %s
              AND status = 'finalizing'
              AND lease_token_hash = %s
            """,
            (job_id, lease_token_hash),
        )
        conn.commit()
        return bool(cur.rowcount)


def finish_external_background_job(job_id: int, *, lease_token_hash: str, result: dict | None = None) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'ok',
                progress = 100,
                result_json = %s,
                error_message = NULL,
                claimed_by = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL,
                finished_at = now()
            WHERE id = %s
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND (status = 'finalizing'
                   OR (status = 'running' AND lease_expires_at > now()))
            """,
            (Json(_jsonable(result or {})), job_id, lease_token_hash),
        )
        conn.commit()
        return bool(cur.rowcount)


def fail_background_job(job_id: int, error_message: str, *, retry_delay_seconds: int | None = None) -> None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT attempts, max_attempts FROM background_jobs WHERE id = %s",
            (job_id,),
        )
        row = cur.fetchone()
        should_retry = (
            retry_delay_seconds is not None
            and row is not None
            and int(row["attempts"] or 0) < int(row["max_attempts"] or 0)
        )
        if should_retry:
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'queued',
                    progress = 0,
                    run_after = now() + (%s::text || ' seconds')::interval,
                    error_message = %s,
                    started_at = NULL
                WHERE id = %s
                """,
                (retry_delay_seconds, error_message, job_id),
            )
        else:
            conn.execute(
            """
            UPDATE background_jobs
            SET status = 'failed',
                error_message = %s,
                finished_at = now()
            WHERE id = %s
            """,
                (error_message, job_id),
            )
        conn.commit()


def fail_external_background_job(
    job_id: int,
    *,
    lease_token_hash: str,
    error_message: str,
    retryable: bool,
    retry_delay_seconds: int | None = None,
) -> bool:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT attempts, max_attempts
            FROM background_jobs
            WHERE id = %s
              AND status = 'running'
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            """,
            (job_id, lease_token_hash),
        )
        row = cur.fetchone()
        if row is None:
            return False
        should_retry = retryable and int(row["attempts"] or 0) < int(row["max_attempts"] or 0)
        if should_retry:
            delay = retry_delay_seconds if retry_delay_seconds is not None else 60
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'queued',
                    progress = 0,
                    run_after = now() + (%s::text || ' seconds')::interval,
                    error_message = %s,
                    started_at = NULL,
                    claimed_by = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL
                WHERE id = %s
                """,
                (delay, error_message, job_id),
            )
        else:
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'failed',
                    error_message = %s,
                    claimed_by = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    finished_at = now()
                WHERE id = %s
                """,
                (error_message, job_id),
            )
        conn.commit()
        return True


def requeue_stale_background_jobs(
    stale_minutes: int, finalizing_stale_minutes: int | None = None
) -> RequeueOutcome:
    """Разобрать локальные running и любые finalizing, зависшие после краша воркера/core.

    Настенные часы по `started_at` — ЗАПАСНАЯ эвристика живости, для задач, у которых
    настоящего протокола живости нет: локальный воркер исполняет задачу в своём процессе
    и, падая, не оставляет о себе никакого сигнала, кроме «висит слишком долго».

    У ВНЕШНЕГО контура протокол есть — lease + heartbeat перед каждой статьёй, — и он
    достовернее любого таймаута. Поэтому внешние `running` здесь не трогаются вовсе; ими
    занимается `requeue_expired_external_leases`. Раньше этот фильтр стоял только на ветке
    «пометить failed», а ветка переочереди хватала всё подряд, и комментарий про «внешний
    контур не трогаем» описывал намерение, которого в коде не было. Итог (инцидент 24.07):
    внешний батч длиннее stale_minutes переочередивался вопреки живому, только что
    продлённому lease — воркер получал 409, бросал батч, тут же забирал ту же задачу
    и начинал с нуля. Ровно раз в час, вечно.

    'finalizing' (баг T2) — переходный статус на время применения результата внешней задачи;
    он остаётся здесь СОЗНАТЕЛЬНО. В нём работает уже не воркер, а core, heartbeat не идёт,
    и признак жизни тут действительно временной: `last_heartbeat_at` ставится при входе
    в finalize, а сам apply обязан занимать секунды. Отдельный короткий таймаут
    (`finalizing_stale_minutes`) — не «как обычный running».
    """
    if finalizing_stale_minutes is None:
        finalizing_stale_minutes = stale_minutes
    with get_connection() as conn:
        # ЛОКАЛЬНУЮ AI-обработку, которая уже НАЧАЛА жечь OpenAI, в очередь НЕ возвращаем:
        # повторный прогон — это повторный РЕАЛЬНЫЙ расход. requeue переиспользует тот же
        # job_id, а get_articles_by_ids не пропускает уже обработанные статьи, поэтому модель
        # вызвалась бы заново. Дискриминатор — ai_started_at (ставится строго перед первым
        # вызовом модели). progress тут НЕ годится: claim/mark-running форсят его в 10 ещё до
        # тела обработчика, так что по progress «до/после AI» не отличить — и задача, упавшая
        # ДО первого вызова ($0), ложно попадала бы под failed вместо авто-восстановления.
        # NB: дедуп биллинга по (job_id, article_id, stage) здесь НЕ решение — он бы просто
        # СПРЯТАЛ второй, реально оплаченный вызов из отчёта о стоимости (к тому же
        # fail_background_job умеет ретраить ту же задачу, пока attempts < max_attempts).
        # Помечаем failed без авто-ретрая — пусть решает человек.
        conn.execute(
            """
            UPDATE background_jobs
            SET status = 'failed',
                error_message = 'Зависла после начала AI-обработки. Не перезапускаем '
                                'автоматически, чтобы не оплатить OpenAI дважды — '
                                'проверьте результат и при необходимости запустите заново.'
            WHERE status = 'running'
              AND started_at < now() - (%s::text || ' minutes')::interval
              AND kind = 'process_articles'
              AND execution_region IS DISTINCT FROM 'external'
              AND ai_started_at IS NOT NULL
            """,
            (stale_minutes,),
        )
        outcome = _requeue_or_exhaust(
            conn,
            lost_where="""
                (status = 'running'
                 AND execution_region IS DISTINCT FROM 'external'
                 AND started_at < now() - (%s::text || ' minutes')::interval)
                OR (status = 'finalizing'
                    AND last_heartbeat_at < now() - (%s::text || ' minutes')::interval)
            """,
            lost_params=(stale_minutes, finalizing_stale_minutes),
            reason="Requeued after stale running/finalizing timeout",
        )
        conn.commit()
        return outcome


def count_stale_running_background_jobs(stale_minutes: int) -> int:
    with get_connection() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM background_jobs
                WHERE status = 'running'
                  AND started_at < now() - (%s::text || ' minutes')::interval
                """,
                (stale_minutes,),
            ).fetchone()[0]
        )


def cleanup_finished_background_jobs(retention_days: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            DELETE FROM background_jobs
            WHERE status IN ('ok', 'failed')
              AND COALESCE(finished_at, started_at, created_at)
                  < now() - (%s::text || ' days')::interval
            """,
            (retention_days,),
        )
        conn.commit()
        return cur.rowcount or 0


def count_finished_background_jobs_eligible_for_cleanup(retention_days: int) -> int:
    with get_connection() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM background_jobs
                WHERE status IN ('ok', 'failed')
                  AND COALESCE(finished_at, started_at, created_at)
                      < now() - (%s::text || ' days')::interval
                """,
                (retention_days,),
            ).fetchone()[0]
        )


def cleanup_finished_export_jobs(retention_days: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            DELETE FROM export_jobs
            WHERE status IN ('ok', 'failed')
              AND COALESCE(finished_at, started_at)
                  < now() - (%s::text || ' days')::interval
            """,
            (retention_days,),
        )
        conn.commit()
        return cur.rowcount or 0


def count_finished_export_jobs_eligible_for_cleanup(retention_days: int) -> int:
    with get_connection() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM export_jobs
                WHERE status IN ('ok', 'failed')
                  AND COALESCE(finished_at, started_at)
                      < now() - (%s::text || ' days')::interval
                """,
                (retention_days,),
            ).fetchone()[0]
        )


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


# ---------------------------------------------------------------------------
#  articles
# ---------------------------------------------------------------------------

def insert_article(rec: dict) -> bool:
    """Вставить статью. Дубликаты игнорируются. Возвращает True, если строка вставлена.

    Тождество статьи — по НОРМАЛИЗОВАННОМУ адресу (`normalize.url_key`), а не по сырому
    url. Замер прода 13.09: на сыром url за 90 дней накопилось 940 лишних статей, и
    причины ровно три — query-хвосты (`?from=main_lines_11` против `?from=newsfeed` у
    РБК), схема (`http://` против `https://` у Ростеха) и хвостовой слэш (Wood Mackenzie).
    Каждая копия проходила полный ИИ-конвейер заново и занимала отдельную карточку в
    ленте: заказчик 08.09 прислал скрин «все 4 новости об одном».

    ДВА ON CONFLICT нужны оба: по `url` — исторический уникальный индекс, он никуда не
    делся; по `url_key` — новый частичный (скрытые копии его не занимают). Пишем первым
    попавшийся конфликт, поэтому проверку по ключу делаем явным запросом до вставки:
    ON CONFLICT умеет целиться только в один индекс за раз.
    """
    url_key = normalize.url_key(rec.get("url") or "")
    rec = {**rec, "image_url": rec.get("image_url"),
           "body_hash": normalize.compute_body_hash(rec.get("raw_text")),
           "url_key": url_key}
    with get_connection() as conn:
        if url_key:
            seen = conn.execute(
                "SELECT 1 FROM articles WHERE url_key = %s AND NOT pending_deletion LIMIT 1",
                (url_key,),
            ).fetchone()
            if seen is not None:
                return False
        # Третий рубеж: одинаковое ТЕЛО у того же источника. Такая же проверка уже
        # стояла в дозагрузке (article_fetcher), но только на замену текста — на
        # первичной вставке её не было, и брак заезжал свободно.
        #
        # Замер прода 17.09: 830 статей с повторяющимся телом, 311 у активных
        # источников, за 134 уже заплачен ИИ. У Новатэка так набралось 82 «статьи»
        # из 86 — это оказались страницы навигации сайта (/ru/esg, /ru/press/
        # calculator, даже PDF политики конфиденциальности): парсер берёт из
        # листинга все ссылки подряд, включая меню, а сайт отдаёт на них одну и ту
        # же оболочку. Разные статьи одного источника не совпадают телом побайтово,
        # поэтому проверка безопасна. Перепечатки между источниками НЕ трогаем —
        # это задача №21, и там нужен семантический дедуп, а не хэш.
        body_hash = rec.get("body_hash")
        source_id = rec.get("source_id")
        if body_hash and source_id is not None:
            # NOT pending_deletion — как на соседнем рубеже по url_key. Скрытая копия
            # иначе блокировала бы пересбор навсегда: у RSS телом на вставке служит
            # summary ленты, и один постоянный тизер-заглушка отрезал бы источник
            # целиком после первой же статьи.
            twin = conn.execute(
                "SELECT 1 FROM articles WHERE source_id = %s AND body_hash = %s "
                "AND NOT pending_deletion LIMIT 1",
                (int(source_id), body_hash),
            ).fetchone()
            if twin is not None:
                return False
        cur = conn.execute(
            """
            INSERT INTO articles (source_id, title, url, url_key, published_at,
                                  raw_text, text_truncated, language, content_hash, image_url,
                                  body_hash)
            VALUES (%(source_id)s, %(title)s, %(url)s, %(url_key)s, %(published_at)s,
                    %(raw_text)s, COALESCE(%(text_truncated)s, FALSE), %(language)s,
                    %(content_hash)s, %(image_url)s, %(body_hash)s)
            ON CONFLICT (url) DO NOTHING
            RETURNING id
            """,
            rec,
        )
        row = cur.fetchone()
        conn.commit()
    return row is not None


def body_hash_belongs_to_other_article(source_id: int, body_hash: str | None,
                                       exclude_article_id: int | None = None) -> bool:
    """Есть ли у ЭТОГО источника другая статья с таким же телом (задача №24).

    Один и тот же текст у разных статей источника — почти всегда подмена: сайт отдал
    листинг/пейвол/заглушку вместо статьи. Проверка идёт в паре (source_id, body_hash),
    под неё есть составной индекс. Кросс-источниковые совпадения НЕ считаем подменой —
    это перепечатки, ими занимается дедуп (№21).
    """
    if not body_hash:
        return False
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT 1 FROM articles
            WHERE source_id = %s AND body_hash = %s
              AND (%s::bigint IS NULL OR id <> %s::bigint)
            LIMIT 1
            """,
            (source_id, body_hash, exclude_article_id, exclude_article_id),
        )
        return cur.fetchone() is not None


def get_articles_missing_image(limit: int = 200) -> list[dict]:
    """Статьи без картинки (image_url пуст) — для бэкфилла og:image в дайджест.
    fetch-full-text трогает только статьи без полного текста, поэтому уже
    обработанные статьи остаются без image_url, и их добирает эта выборка.

    СКРЫТЫЕ И ОТКЛОНЁННЫЕ ИСКЛЮЧЕНЫ. Бэкфилл — это РЕАЛЬНЫЙ поход в интернет за
    каждой статьёй (медленный, с ретраями), а картинка нужна только тому, что
    показывается. Замер 24.07: из 4675 статей без картинки 2085 (45%) были скрыты
    (pending_deletion) или отклонены гейтом — почти половина запросов уходила
    впустую. relevant IS NULL оставляем: статья ещё не гейчена и может стать видимой."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.id, a.url
            FROM articles a
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE a.url IS NOT NULL AND COALESCE(a.image_url, '') = ''
              AND NOT a.pending_deletion
              AND c.relevant IS NOT FALSE
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def set_article_image(article_id: int, image_url: str) -> bool:
    """Проставить image_url, только если его ещё нет. True, если обновлено."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE articles SET image_url = %s, updated_at = now() "
            "WHERE id = %s AND COALESCE(image_url, '') = ''",
            (image_url, article_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_articles_needing_full_text(limit: int = 50, retry_too_short: bool = False) -> list[dict]:
    """Articles whose RSS body is likely only a teaser and needs URL extraction.

    retry_too_short=True also includes articles previously marked too_short so they
    can be re-attempted (e.g. after trafilatura is added to the extraction chain).

    Сюда же берём no_gain: этот статус выделен из too_short позже, и в базе с
    прежних прогонов лежат строки, где «нет прироста» записано как too_short.
    Если брать только одно из двух значений, часть статей молча выпадет из
    повтора — поэтому список, а не равенство.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        status_filter = (
            "AND (a.full_text_status IS NULL OR a.full_text_status IN ('too_short', 'no_gain'))"
            if retry_too_short
            else "AND a.full_text_status IS NULL"
        )
        cur.execute(
            f"""
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.url IS NOT NULL
              -- Источники зарубежного контура пропускаем: они там именно потому, что
              -- с РФ-адреса закрыты, и локальная дозагрузка ловит на них 403
              -- гарантированно. Попытка ОДНА и навсегда — статья получила бы
              -- full_text_status='failed' и больше никогда не переспрашивалась, даже
              -- когда тело уже добрал зарубежный воркер (external_fetch).
              AND COALESCE(s.network_region, 'auto') <> 'external'
              {status_filter}
              AND (
                COALESCE(a.text_truncated, FALSE) = TRUE
                OR length(COALESCE(a.raw_text, '')) < %s
              )
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (config.MIN_FULL_TEXT_CHARS, limit),
        )
        return cur.fetchall()


def update_article_full_text(article_id: int, raw_text: str | None, text_truncated: bool,
                             status: str, method: str, error: str | None = None,
                             image_url: str | None = None) -> None:
    """Store full-text extraction result without losing the RSS teaser on failure."""
    with get_connection() as conn:
        if image_url:
            # Заполняем картинку, только если её ещё нет — RSS-media в приоритете.
            conn.execute(
                "UPDATE articles SET image_url = %s "
                "WHERE id = %s AND COALESCE(image_url, '') = ''",
                (image_url, article_id),
            )
        if raw_text is not None:
            conn.execute(
                """
                UPDATE articles
                SET raw_text = %s,
                    body_hash = %s,
                    text_truncated = %s,
                    full_text_fetched_at = now(),
                    full_text_status = %s,
                    full_text_error = %s,
                    extraction_method = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (raw_text, normalize.compute_body_hash(raw_text), text_truncated,
                 status, error, method, article_id),
            )
        else:
            conn.execute(
                """
                UPDATE articles
                SET text_truncated = %s,
                    full_text_fetched_at = now(),
                    full_text_status = %s,
                    full_text_error = %s,
                    extraction_method = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (text_truncated, status, error, method, article_id),
            )
        conn.commit()


# ---------------------------------------------------------------------------
#  Диагностика (для stats)
# ---------------------------------------------------------------------------

def count_sources() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]


def count_articles() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]


def dashboard_stats(user_id: int | None = None) -> dict:
    """Aggregate counters for the admin dashboard cards.

    Computed over the FULL database (not the loaded page), so the numbers stay
    correct regardless of how many articles the UI fetches. ``avg_score`` is the
    mean over scored articles only — unscored articles do not drag it to zero.
    ``selected_for_digest`` — ПЕР-ЮЗЕРНО (выбор в дайджест личный, #12).
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT
              -- «Сигналы» = то, что реально дошло до ленты: прошло гейт релевантности
              -- и не убрано перепроверкой. Раньше здесь был COUNT(*) по ВСЕМ статьям,
              -- и плитка показывала 14785 при 6.6к настоящих сигналов — отрезанное
              -- попадало в базу счёта и делало все цифры бессмысленными.
              (SELECT COUNT(*) FROM articles a
                 JOIN article_cards c ON c.article_id = a.id
                WHERE c.relevant IS NOT FALSE AND NOT a.pending_deletion) AS total_articles,
              (SELECT COUNT(*) FROM articles a
                 JOIN article_cards c ON c.article_id = a.id
                WHERE c.relevant IS NOT FALSE AND NOT a.pending_deletion
                  AND COALESCE(c.summary, '') <> '') AS with_summary,
              -- Обработано — тоже ТОЛЬКО по сигналам, иначе «обработано» может
              -- превысить «всего сигналов» (считалось по всей базе, включая отсев).
              (SELECT COUNT(*)
                 FROM articles a
                 JOIN article_cards c ON c.article_id = a.id
                WHERE c.relevant IS NOT FALSE AND NOT a.pending_deletion
                  AND COALESCE(c.summary, '') <> ''
                  AND c.relevant IS NOT NULL
                  AND (
                    EXISTS (SELECT 1 FROM article_tags at WHERE at.article_id = c.article_id)
                    OR EXISTS (SELECT 1 FROM article_scores sc WHERE sc.article_id = c.article_id)
                  )) AS processed_articles,
              (SELECT COUNT(*) FROM articles WHERE pending_deletion) AS cleaned_articles,
              (SELECT COUNT(*) FROM user_article_states
                 WHERE user_id = %(user_id)s AND status = 'digest') AS selected_for_digest,
              (SELECT ROUND(AVG(total_score)) FROM article_scores) AS avg_score,
              (SELECT COUNT(*) FROM sources) AS sources,
              -- «Всего» на дашборде показывает ВЕСЬ объём собранного (решение владельца
              -- 25.07): все статьи в базе, включая отсев по релевантности и вычищенные
              -- перепроверкой. total_articles выше остаётся «сигналами» (его читает
              -- workingTotal и арифметика соседних плиток) — это ОТДЕЛЬНОЕ поле только
              -- под первую плитку.
              (SELECT COUNT(*) FROM articles) AS all_articles
            """,
            {"user_id": user_id},
        )
        row = cur.fetchone()

        # Пер-статусные счётчики для плиток — по ВСЕЙ базе, а не по загруженной странице.
        # Раньше фронт считал их по массиву загруженных статей (топ-2000, к тому же
        # суженный текущим фильтром), поэтому «Новые/На проверке/Шум/Дубликаты» занижали
        # и «плавали» при фильтрации, расходясь с соседними плитками «Всего»/«Обработано».
        # Видимость та же, что у ленты (list_articles): отклонённые гейтом релевантности и
        # помеченные на удаление не показываем — иначе цифра не сойдётся с тем, что видно.
        # Один GROUP BY вместо пяти отдельных COUNT-подзапросов.
        cur.execute(
            """
            SELECT COALESCE(uas.status, 'new') AS status, COUNT(*) AS cnt
              FROM articles a
              LEFT JOIN article_cards c ON c.article_id = a.id
              LEFT JOIN user_article_states uas
                     ON uas.article_id = a.id AND uas.user_id = %(user_id)s
             WHERE c.relevant IS NOT FALSE
               AND NOT a.pending_deletion
             GROUP BY 1
            """,
            {"user_id": user_id},
        )
        status_counts = {str(r["status"]): int(r["cnt"] or 0) for r in cur.fetchall()}

    return {
        "total_articles": int(row["total_articles"] or 0),
        # Весь объём базы — только под плитку «Всего» на дашборде. Отдельно от
        # total_articles («сигналы»), чтобы не задеть workingTotal и «Обработано».
        "all_articles": int(row["all_articles"] or 0),
        "with_summary": int(row["with_summary"] or 0),
        "processed_articles": int(row["processed_articles"] or 0),
        # Терялось: SQL считал cleaned_articles, а возврат собирается вручную и поле
        # в него не попадало → на фронте плитка «Почищено» всегда показывала 0.
        "cleaned_articles": int(row["cleaned_articles"] or 0),
        "selected_for_digest": int(row["selected_for_digest"] or 0),
        "avg_score": int(row["avg_score"] or 0),
        "sources": int(row["sources"] or 0),
        "status_counts": {
            status: status_counts.get(status, 0)
            for status in ARTICLE_STATUS_VALUES
        },
    }


def monthly_platform_stats(months: int = 6) -> list[dict]:
    """Месячная воронка платформы: собрано → прошло гейт → обработано → скрыто.

    Глобальная (не пер-юзерная) метрика: это результат работы САМОЙ платформы,
    одинаковый для всех. Пер-юзерная активность считается отдельно —
    monthly_user_activity(), у неё свой скоуп по владельцу.
    Месяц берём по collected_at (когда МЫ собрали), а не по published_at:
    иначе старые статьи из архивных лент искажают динамику текущей работы.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT to_char(a.collected_at, 'YYYY-MM') AS month,
                   count(*) AS collected,
                   count(*) FILTER (WHERE c.relevant IS TRUE) AS relevant,
                   count(*) FILTER (WHERE c.relevant IS FALSE) AS rejected,
                   count(*) FILTER (WHERE a.pending_deletion) AS hidden,
                   count(*) FILTER (WHERE COALESCE(c.summary, '') <> '') AS summarized,
                   count(*) FILTER (WHERE sc.total_score IS NOT NULL) AS scored,
                   round(avg(sc.total_score)) AS avg_score,
                   count(*) FILTER (WHERE sc.total_score >= 60) AS digest_ready
            FROM articles a
            LEFT JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_scores sc ON sc.article_id = a.id
            WHERE a.collected_at >= date_trunc('month', now()) - make_interval(months => %s)
            GROUP BY 1
            ORDER BY 1
            """,
            (months,),
        )
        return cur.fetchall()


def monthly_ai_cost(months: int = 6) -> list[dict]:
    """Стоимость ИИ по месяцам и моделям.

    Считается по ai_processing_runs.cost_usd. ВАЖНО: до фикса 23.07 (T10) сюда не
    попадали вызовы гейта по ОТКЛОНЁННЫМ статьям — исторические месяцы занижены.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT to_char(created_at, 'YYYY-MM') AS month,
                   model,
                   count(*) AS runs,
                   round(sum(cost_usd)::numeric, 2) AS cost_usd
            FROM ai_processing_runs
            WHERE created_at >= date_trunc('month', now()) - make_interval(months => %s)
            GROUP BY 1, 2
            ORDER BY 1, 4 DESC
            """,
            (months,),
        )
        return cur.fetchall()


def monthly_user_activity(months: int = 6, user_id: int | None = None) -> list[dict]:
    """Разметка пользователей по месяцам: кто сколько чего пометил.

    СКОУП: user_id=None — все пользователи (только для админа, решается в api).
    Передан user_id — строго его собственная активность. Fail-closed не нужен:
    вызывающий обязан передать user_id для не-админа (см. api.monthly_stats).
    Месяц — по дате статьи, чтобы «разметка за июль» означала июльские новости.
    """
    params: list = [months]
    user_clause = ""
    if user_id is not None:
        user_clause = "AND s.user_id = %s"
        params.append(user_id)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT to_char(COALESCE(a.published_at, a.collected_at), 'YYYY-MM') AS month,
                   s.user_id,
                   u.email,
                   s.status,
                   count(*) AS marks
            FROM user_article_states s
            JOIN users u ON u.id = s.user_id
            JOIN articles a ON a.id = s.article_id
            WHERE COALESCE(a.published_at, a.collected_at)
                  >= date_trunc('month', now()) - make_interval(months => %s)
              {user_clause}
            GROUP BY 1, 2, 3, 4
            ORDER BY 1 DESC, 5 DESC
            """,
            params,
        )
        return cur.fetchall()


def clear_future_published_dates(tolerance_days: int = 2) -> int:
    """Обнулить недостоверные даты публикации из будущего (анонсы-события календаря).

    Статья сохраняется — убирается только ошибочная дата, после чего она
    сортируется/показывается по реальному `collected_at` и перестаёт помечаться
    как «дата в будущем». Идемпотентно. Возвращает число затронутых строк.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE articles SET published_at = NULL, updated_at = now() "
            "WHERE published_at > now() + make_interval(days => %s)",
            (tolerance_days,),
        )
        conn.commit()
        return cur.rowcount


def sources_by_strategy() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT parse_strategy, COUNT(*) AS n FROM sources "
            "GROUP BY parse_strategy ORDER BY n DESC"
        )
        return cur.fetchall()


def source_health_report(stale_days: int = 3, limit: int = 300, verdict: str | None = None) -> list[dict]:
    """Per-source article coverage verdict for operations diagnostics."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            WITH src AS (
              SELECT s.id, s.name, s.enabled, s.parse_strategy, s.source_type,
                     s.url, s.rss_url, s.listing_url,
                     COUNT(a.id) AS articles,
                     MAX(a.collected_at) AS last_article_at
              FROM sources s
              LEFT JOIN articles a ON a.source_id = s.id
              GROUP BY s.id
            ),
            verdicts AS (
              SELECT *,
                     CASE
                       WHEN NOT enabled THEN 'disabled'
                       WHEN articles = 0 THEN 'no_articles'
                       WHEN last_article_at < now() - (%s::text || ' days')::interval THEN 'stale'
                       ELSE 'ok'
                     END AS verdict
              FROM src
            )
            SELECT *
            FROM verdicts
            WHERE (%s::text IS NULL OR verdict = %s)
            ORDER BY
              CASE
                WHEN NOT enabled THEN 4
                WHEN articles = 0 THEN 1
                WHEN last_article_at < now() - (%s::text || ' days')::interval THEN 2
                ELSE 3
              END,
              articles ASC,
              last_article_at NULLS FIRST,
              name
            LIMIT %s
            """,
            (stale_days, verdict, verdict, stale_days, limit),
        )
        return cur.fetchall()


def top_sources(limit: int = 10) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT s.name, COUNT(a.id) AS n FROM sources s "
            "JOIN articles a ON a.source_id = s.id "
            "GROUP BY s.id, s.name ORDER BY n DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def cross_dup_candidates() -> int:
    """Сколько content_hash встречается более чем у одного URL (кандидаты-перепечатки)."""
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT content_hash FROM articles "
            "  WHERE content_hash IS NOT NULL "
            "  GROUP BY content_hash HAVING COUNT(DISTINCT url) > 1"
            ") t"
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
#  AI processing: articles, cards, tags, scoring, metrics
# ---------------------------------------------------------------------------

def external_refetch_candidates(limit: int = 200) -> list[dict]:
    """Статьи-обрывки у источников зарубежного контура — кандидаты на дозаполнение.

    Локальная дозагрузка их не берёт намеренно (см. get_articles_needing_full_text):
    с РФ-адреса эти сайты отдают 403, а попытка одна и навсегда. Тело для них может
    добрать только внешний воркер, и до 17.09 такого пути не было вовсе — замер
    показал 25 из 25 статей короче 600 знаков у Oil & Gas Journal и Offshore Magazine.

    Берём и уже помеченные failed: прежние пометки ставились локальной дозагрузкой,
    то есть отражают недоступность с РФ, а не непригодность статьи.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.id, a.url, a.title
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.url IS NOT NULL
              AND s.network_region = 'external'
              AND s.archived_at IS NULL
              AND (
                a.full_text_status IS NULL
                -- Повтор после неудачи — но НЕ каждый цикл. Статусы failed/too_short
                -- ставит сама же внешняя попытка, поэтому без паузы статья
                -- переочередивалась бы вечно: в июле так задача 1181 крутилась в
                -- петле по часу и жгла деньги. Даём сутки на остыть.
                OR (a.full_text_status IN ('failed', 'too_short')
                    AND (a.full_text_fetched_at IS NULL
                         OR a.full_text_fetched_at < now() - interval '1 day'))
              )
              AND length(COALESCE(a.raw_text, '')) < %s
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (config.MIN_FULL_TEXT_CHARS, limit),
        )
        return cur.fetchall()


def get_articles_for_external_refetch(article_ids: list[int]) -> list[dict]:
    """Адреса и заголовки по списку id — то, что уезжает на воркер без базы."""
    if not article_ids:
        return []
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, url, title FROM articles WHERE id = ANY(%s) AND url IS NOT NULL",
            (list(article_ids),),
        )
        return cur.fetchall()


def reprint_candidates(*, days: int = 14, min_overlap: float = 0.35,
                       max_overlap: float = 1.0,
                       max_days_apart: int = 5, limit: int = 200) -> list[dict]:
    """Пары-кандидаты в перепечатки: разные источники, близкие даты, общие слова.

    Правило намеренно широкое — оно отвечает за полноту, а решает модель. Замер на
    случае заказчика от 08.09: настоящие дубли дали пересечение от 36% до 78%, то
    есть порога, который ловит все и не ловит лишнего, не существует.

    Слова режем до 6 знаков вместо лемматизации: «установки/установок/установках»
    сходятся, а тащить морфологию в SQL ради этого незачем. Короче 5 знаков
    отбрасываем — предлоги и «нефть» есть почти везде и только шумят.

    Разные источники — условие, а не настройка: внутри одного издания повтор
    заголовка это серийная сводка, и схлопывание таких пар уже уничтожало сотни
    статей в июле.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            r"""
            WITH t AS (
              SELECT a.id, a.source_id, a.title, a.published_at,
                     ARRAY(SELECT DISTINCT left(w, 6)
                           FROM unnest(regexp_split_to_array(
                                lower(regexp_replace(a.title, '[^[:alnum:][:space:]]', ' ', 'g')),
                                '\s+')) w
                           WHERE length(w) >= 5) AS toks
              FROM articles a
              LEFT JOIN article_reprints r ON r.article_id = a.id
              JOIN sources s ON s.id = a.source_id
              LEFT JOIN article_cards c ON c.article_id = a.id
              WHERE a.created_at > now() - make_interval(days => %(days)s)
                AND length(a.title) > 25
                AND r.article_id IS NULL
                -- Только то, что реально видно в ленте. Замер 17.09 на 71 паре,
                -- признанной дублем: 39 пар состояли ИЗ ДВУХ невидимых статей
                -- (модель звали впустую), а в 6 главной копией становилась
                -- невидимая — то есть пометка не схлопывала дубль, а убирала
                -- новость из ленты совсем. Полезными были 21 пара из 71.
                -- Условия те же, что в ленте (api.articles_feed) и в выпуске.
                AND s.archived_at IS NULL
                AND NOT a.pending_deletion
                AND c.relevant IS NOT FALSE
            )
            SELECT x.id AS a_id, y.id AS b_id,
                   x.title AS a_title, y.title AS b_title,
                   round(
                     cardinality(ARRAY(SELECT unnest(x.toks) INTERSECT SELECT unnest(y.toks)))::numeric
                     / NULLIF(cardinality(ARRAY(SELECT unnest(x.toks) UNION SELECT unnest(y.toks))), 0),
                   3) AS overlap
            FROM t x
            JOIN t y ON y.id > x.id
                    AND y.source_id <> x.source_id
                    AND (x.published_at IS NULL OR y.published_at IS NULL
                         OR abs(EXTRACT(EPOCH FROM (x.published_at - y.published_at)))
                            < %(apart)s * 86400)
            WHERE cardinality(x.toks) >= 3 AND cardinality(y.toks) >= 3
              AND cardinality(ARRAY(SELECT unnest(x.toks) INTERSECT SELECT unnest(y.toks)))::numeric
                  / NULLIF(cardinality(ARRAY(SELECT unnest(x.toks) UNION SELECT unnest(y.toks))), 0)
                  >= %(overlap)s
              AND cardinality(ARRAY(SELECT unnest(x.toks) INTERSECT SELECT unnest(y.toks)))::numeric
                  / NULLIF(cardinality(ARRAY(SELECT unnest(x.toks) UNION SELECT unnest(y.toks))), 0)
                  <= %(max_overlap)s
            -- Порядок намеренно НЕ по overlap. Замер 17.09 на живом корпусе: из 318
            -- пар за 14 дней только 57 тривиальных (100%%), а полоса, ради которой
            -- судья и заведён (35-49%% — случай заказчика от 08.09), самая большая:
            -- 132 пары. При ORDER BY overlap DESC любой LIMIT отдавал модели ровно
            -- верхушку, и спорные пары не доезжали до неё никогда. md5 по паре id
            -- даёт порядок, не связанный с силой совпадения, и при этом устойчивый
            -- между прогонами.
            ORDER BY md5(x.id::text || ':' || y.id::text)
            LIMIT %(limit)s
            """,
            {"days": days, "apart": max_days_apart, "overlap": min_overlap,
             "max_overlap": max_overlap, "limit": limit},
        )
        return cur.fetchall()


def resolve_reprint_root(conn, article_id: int, *, max_hops: int = 8) -> int:
    """Корень группы перепечаток: главная копия, которая сама ничьей копией не является.

    Пометки ставятся ПОПАРНО, и без разрешения до корня получалась бы цепочка
    C→A→D: каждая пара выбирает главную независимо. С фильтром ленты, который
    прячет всё, что значится копией, такая цепочка унесла бы из выборки ВСЮ группу
    вместе с оригиналом. Ограничение по шагам — защита от кольца в уже накопленных
    данных, а не теоретическая.
    """
    current = int(article_id)
    for _ in range(max_hops):
        row = conn.execute(
            "SELECT primary_id FROM article_reprints WHERE article_id = %s", (current,)
        ).fetchone()
        if row is None:
            return current
        nxt = int(row[0])
        if nxt == current:
            return current
        current = nxt
    return current


def article_visible_in_feed(conn, article_id: int) -> bool:
    """Видна ли статья в ленте — теми же условиями, что и сама лента.

    Заведено не ради стройности: перепечатки решает модель на внешнем воркере, а
    её ответ это недоверенный вход. Пометка прячет копию, и если главной окажется
    статья, которой в ленте нет (архивный источник, помечена на удаление, отбита
    гейтом), то новость исчезнет целиком вместо схлопывания дубля.
    """
    row = conn.execute(
        """
        SELECT 1 FROM articles a
        JOIN sources s ON s.id = a.source_id
        LEFT JOIN article_cards c ON c.article_id = a.id
        WHERE a.id = %s
          AND s.archived_at IS NULL
          AND NOT a.pending_deletion
          AND c.relevant IS NOT FALSE
        """,
        (int(article_id),),
    ).fetchone()
    return row is not None


def mark_article_reprint(*, article_id: int, primary_id: int, similarity: float | None,
                         reason: str | None, decided_by: str = "ai",
                         model: str | None = None) -> None:
    """Пометить статью перепечаткой. Запись обратима: удаления нет намеренно."""
    if int(article_id) == int(primary_id):
        raise ValueError("статья не может быть перепечаткой самой себя")
    with get_connection() as conn:
        # Главной назначаем КОРЕНЬ группы, а не соседа по паре: иначе цепочка
        # C→A→D спрячет из ленты и оригинал.
        primary_id = resolve_reprint_root(conn, int(primary_id))
        if int(article_id) == int(primary_id):
            raise ValueError("статья уже является корнем своей группы перепечаток")
        # Прятать копию можно только в пользу той, которую читатель увидит.
        if not article_visible_in_feed(conn, primary_id):
            raise ValueError(
                f"главная копия {primary_id} не видна в ленте — пометка убрала бы новость целиком"
            )
        conn.execute(
            """
            INSERT INTO article_reprints (article_id, primary_id, similarity, reason, decided_by, model)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (article_id) DO UPDATE
               SET primary_id = EXCLUDED.primary_id,
                   similarity = EXCLUDED.similarity,
                   reason = EXCLUDED.reason,
                   decided_by = EXCLUDED.decided_by,
                   model = EXCLUDED.model
            """,
            (int(article_id), int(primary_id), similarity, reason, decided_by, model),
        )
        conn.commit()


def unmark_article_reprint(article_id: int) -> bool:
    """Снять пометку перепечатки — статья возвращается в ленту и в выпуск.

    Без этой операции «обратимо» было бы только на словах: пометка ставится ИИ, и
    у человека должен быть способ её отменить, иначе ошибка модели становится
    необратимой ровно так же, как удаление.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM article_reprints WHERE article_id = %s RETURNING article_id",
            (int(article_id),),
        )
        removed = cur.fetchone() is not None
        conn.commit()
    return removed


def list_article_reprints(limit: int = 50) -> list[dict]:
    """Помеченные перепечатки с объяснением — чтобы решение можно было проверить."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT r.article_id, r.primary_id, r.similarity, r.reason, r.decided_by,
                   r.created_at, d.title AS duplicate_title, p.title AS primary_title,
                   ds.name AS duplicate_source, ps.name AS primary_source
            FROM article_reprints r
            JOIN articles d ON d.id = r.article_id
            JOIN articles p ON p.id = r.primary_id
            JOIN sources ds ON ds.id = d.source_id
            JOIN sources ps ON ps.id = p.source_id
            ORDER BY r.created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def reprint_stats() -> dict:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT count(*) AS total, count(DISTINCT primary_id) AS groups FROM article_reprints"
        )
        return cur.fetchone() or {"total": 0, "groups": 0}


def get_article(article_id: int) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.id = %s
            """,
            (article_id,),
        )
        return cur.fetchone()


def get_articles_by_ids(article_ids: list[int], include_summary: bool = False) -> list[dict]:
    if not article_ids:
        return []
    summary_select = (
        ", c.summary, c.relevant, c.title_ru, at.id AS existing_tag_id, sc.id AS existing_score_id"
        if include_summary else ""
    )
    summary_join = (
        """
        LEFT JOIN article_cards c ON c.article_id = a.id
        LEFT JOIN article_tags at ON at.article_id = a.id
        LEFT JOIN article_scores sc ON sc.article_id = a.id
        """
        if include_summary else ""
    )
    placeholders = ", ".join(["%s"] * len(article_ids))
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
                   {summary_select}
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            {summary_join}
            WHERE a.id IN ({placeholders})
            ORDER BY array_position(%s::bigint[], a.id)
            """,
            [*article_ids, article_ids],
        )
        return cur.fetchall()


def delete_article(article_id: int, *, force: bool = False) -> bool:
    """Физически удалить статью и все её зависимые строки. Возвращает True, если удалена.

    FK на articles в основном БЕЗ ON DELETE CASCADE, поэтому удаляем детей вручную
    в правильном порядке (user_article_states каскадится сам). По умолчанию НЕ удаляем
    статью, входящую в сохранённый месячный дайджест (monthly_digest_items) — чтобы не
    рвать историю; force=True снимает защиту (удалит и ссылки дайджеста)."""
    with get_connection() as conn:
        if not force:
            in_digest = conn.execute(
                "SELECT 1 FROM monthly_digest_items WHERE article_id = %s LIMIT 1",
                (article_id,),
            ).fetchone()
            if in_digest:
                return False
        conn.execute(
            """
            DELETE FROM article_score_items
            WHERE article_score_id IN (SELECT id FROM article_scores WHERE article_id = %s)
            """,
            (article_id,),
        )
        conn.execute("DELETE FROM article_scores WHERE article_id = %s", (article_id,))
        conn.execute("DELETE FROM article_tags WHERE article_id = %s", (article_id,))
        conn.execute("DELETE FROM ai_processing_runs WHERE article_id = %s", (article_id,))
        conn.execute("DELETE FROM article_cards WHERE article_id = %s", (article_id,))
        if force:
            conn.execute("DELETE FROM monthly_digest_items WHERE article_id = %s", (article_id,))
        cur = conn.execute("DELETE FROM articles WHERE id = %s RETURNING id", (article_id,))
        deleted = cur.fetchone() is not None
        conn.commit()
    return deleted


def mark_article_for_deletion(article_id: int, reason: str | None, *, force: bool = False) -> str:
    """Пометить статью на удаление (мягко, без физического DELETE). Возвращает
    'marked' либо 'skipped_in_digest' (статья в сохранённом дайджесте, force=False)."""
    with get_connection() as conn:
        if not force:
            in_digest = conn.execute(
                "SELECT 1 FROM monthly_digest_items WHERE article_id = %s LIMIT 1", (article_id,)
            ).fetchone()
            if in_digest:
                return "skipped_in_digest"
        conn.execute(
            "UPDATE articles SET pending_deletion = TRUE, deletion_reason = %s, "
            "marked_for_deletion_at = now(), updated_at = now() WHERE id = %s",
            (reason, article_id),
        )
        conn.commit()
    return "marked"


def count_pending_deletion() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT count(*) FROM articles WHERE pending_deletion").fetchone()[0]


def list_pending_deletion(limit: int = 100) -> list[dict]:
    """Помеченные на удаление — заголовок/источник/причина (для просмотра перед purge)."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT a.id, a.title, s.name AS source_name, a.deletion_reason "
            "FROM articles a JOIN sources s ON s.id = a.source_id "
            "WHERE a.pending_deletion ORDER BY a.id LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def purge_pending_deletion(*, force: bool = False) -> int:
    """Физически удалить все помеченные (pending_deletion) статьи. Возвращает число удалённых.
    Использует delete_article (тот же каскад + защита дайджеста при force=False)."""
    with get_connection() as conn:
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM articles WHERE pending_deletion ORDER BY id"
        ).fetchall()]
    deleted = 0
    for article_id in ids:
        if delete_article(article_id, force=force):
            deleted += 1
    return deleted


def unmark_all_pending_deletion() -> int:
    """Снять пометку «на удаление» со всех статей (вернуть в строй). Возвращает число."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE articles SET pending_deletion = FALSE, deletion_reason = NULL, "
            "marked_for_deletion_at = NULL, updated_at = now() WHERE pending_deletion"
        )
        conn.commit()
        return cur.rowcount


def all_article_ids() -> list[int]:
    """Все id статей по возрастанию — для батч-перепрогона релевантности."""
    with get_connection() as conn:
        cur = conn.execute("SELECT id FROM articles ORDER BY id ASC")
        return [row[0] for row in cur.fetchall()]


def article_ids_needing_title_ru() -> list[int]:
    """id статей без русского заголовка — для батч-бэкфилла перевода через воркер."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT a.id FROM articles a
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE c.title_ru IS NULL
            ORDER BY a.id ASC
            """
        )
        return [row[0] for row in cur.fetchall()]


def get_articles_for_recheck(after_id: int = 0, limit: int = 100) -> list[dict]:
    """Все статьи по возрастанию id после чекпоинта — для локального перепрогона
    релевантности на сыром тексте (независимо от наличия карточки)."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.id > %s
            ORDER BY a.id ASC
            LIMIT %s
            """,
            (after_id, limit),
        )
        return cur.fetchall()


def get_articles_needing_summary(limit: int = 20) -> list[dict]:
    """Статьи, которым нужна AI-суть: ещё не обработанные + релевантные без сути (повтор после сбоя).

    ОТКЛОНЁННЫЕ ГЕЙТОМ (relevant IS FALSE) ИСКЛЮЧЕНЫ, и это принципиально. На проде гейт
    релевантности идёт ПЕРВЫМ и отклонённой статье суть НЕ пишется (external_ai.process_payload:
    `if not relevant: continue`). Без этого фильтра такая статья вечно подходила под
    `summary IS NULL` и возвращалась в обработку КАЖДЫЙ цикл: гейт режет ~78%, поэтому бюджет
    цикла (AI_PROCESS_LIMIT) съедали повторные отказы одних и тех же статей, свежие вытеснялись
    из топа (ORDER BY published_at DESC), а дорогой гейт (gpt-5.5) жёгся заново на том же наборе.
    `IS NOT FALSE` (а не `IS NULL`) — чтобы релевантная статья без сути (сбой записи) всё-таки
    попала на повтор. Идиома совпадает с list_articles и digest_candidates.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE c.summary IS NULL
              AND c.relevant IS NOT FALSE
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


_NEEDS_PIPELINE_FROM = """
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_tags at ON at.article_id = a.id
            LEFT JOIN article_scores sc ON sc.article_id = a.id
            WHERE (
                  c.relevant IS NULL
               OR (
                    c.relevant IS TRUE
                    AND (
                        c.summary IS NULL
                        OR c.title_ru IS NULL
                        OR at.id IS NULL
                        OR sc.id IS NULL
                    )
               )
               OR c.article_id IS NULL
            )
"""


def get_articles_needing_pipeline(limit: int = 20) -> list[dict]:
    """Статьи, которым не хватает любого AI-этапа канонического pipeline.

    Используется process/process-full/background/external enqueue. Старый выбор только по
    ``summary IS NULL`` не поднимал статьи после частичного сбоя на тегировании/скоринге.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.*, c.summary, c.relevant, c.title_ru,
                   at.id AS existing_tag_id, sc.id AS existing_score_id,
                   s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            {_NEEDS_PIPELINE_FROM}
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


# Ключ advisory-lock: выбор статей для ИИ-пакета при выдаче идёт по одному.
_PROCESS_RESERVE_LOCK = 7_290_921


class ArticlesBusy(RuntimeError):
    """Статьи явного списка сейчас в работе у другой задачи — выдачу надо отложить."""


def reserve_process_articles(job_id: int, *, limit: int, article_ids: list[int] | None = None) -> list[int]:
    """Статьи ИИ-пакета при выдаче — за вычетом тех, что уже в работе у других задач.

    Пакет без article_ids выбирает «статьи без обработки» в момент выдачи; две ИИ-полосы
    (поток дня и пересчёты) взяли бы одни и те же и оплатили бы их дважды — поэтому второй
    ИИ-воркер 18.09 и не ставили. Выбор и запись резерва — в одной транзакции под
    advisory-lock: параллельная выдача ждёт, а не читает резерв до записи.

    Явный список не урезается: соседняя задача могла взять статью со СТАРЫМ текстом
    (пересчёт ставят после перекачки тела), и выброшенная из пересчёта статья осталась бы
    посчитанной по обрывку. Такой список — ArticlesBusy: выдачу откладывают, пока соседка
    не закончит."""
    with get_connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PROCESS_RESERVE_LOCK,))
        # Только настоящий массив: payload с "article_ids": null (так его пишет
        # /api/jobs/process) — это jsonb null, а не SQL NULL, COALESCE его не пропускает,
        # и выдача падала бы для всех ИИ-задач, пока такая задача выполняется.
        busy_rows = conn.execute(
            """
            SELECT DISTINCT (jsonb_array_elements_text(CASE
                       WHEN jsonb_typeof(payload_json->'reserved_article_ids') = 'array'
                           THEN payload_json->'reserved_article_ids'
                       WHEN jsonb_typeof(payload_json->'article_ids') = 'array'
                           THEN payload_json->'article_ids'
                       ELSE '[]'::jsonb
                   END))::bigint
            FROM background_jobs
            WHERE kind = 'process_articles'
              AND status IN ('running', 'finalizing')
              AND id <> %s
            """,
            (job_id,),
        ).fetchall()
        busy = [int(row[0]) for row in busy_rows]
        if article_ids:
            overlap = sorted(set(int(item) for item in article_ids) & set(busy))
            if overlap:
                raise ArticlesBusy(f"статьи {overlap[:10]} сейчас в работе у другой задачи")
            chosen = [int(item) for item in article_ids]
        else:
            chosen = [
                int(row[0])
                for row in conn.execute(
                    f"""
                    SELECT a.id
                    {_NEEDS_PIPELINE_FROM}
                      AND a.id <> ALL(%s::bigint[])
                    ORDER BY a.published_at DESC NULLS LAST, a.id DESC
                    LIMIT %s
                    """,
                    (busy, limit),
                ).fetchall()
            ]
        conn.execute(
            """
            UPDATE background_jobs
            SET payload_json = payload_json || jsonb_build_object('reserved_article_ids', %s::jsonb)
            WHERE id = %s
            """,
            (Json(chosen), job_id),
        )
        conn.commit()
    return chosen


def defer_claimed_background_job(job_id: int, *, seconds: int = 120) -> bool:
    """Вернуть только что выданную задачу в очередь на потом, не тратя попытку."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'queued',
                attempts = GREATEST(attempts - 1, 0),
                run_after = now() + (%s::text || ' seconds')::interval,
                claimed_by = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL
            WHERE id = %s AND status = 'running'
            """,
            (seconds, job_id),
        )
        conn.commit()
        return cur.rowcount == 1


def get_articles_needing_relevance(limit: int = 20) -> list[dict]:
    """Статьи с готовой сутью, но без проверки релевантности (card.relevant IS NULL)."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, c.summary, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            WHERE c.summary IS NOT NULL AND c.relevant IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def set_article_relevance(article_id: int, relevant: bool, reason: str | None,
                          model: str | None = None) -> None:
    """Записать вердикт релевантности (UPSERT). Нерелевантные → status='rejected'.

    Раньше был чистый UPDATE — но после перестановки «релевантность ПЕРВОЙ» у
    отклонённой статьи карточки ещё нет (она создаётся на этапе суммаризации),
    и UPDATE по 0 строк терял вердикт. UPSERT создаёт карточку при необходимости."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO article_cards (article_id, relevant, relevance_reason, relevance_model, status)
            VALUES (%(id)s, %(rel)s, %(reason)s, %(model)s,
                    CASE WHEN %(rel)s THEN 'new' ELSE 'rejected' END)
            ON CONFLICT (article_id) DO UPDATE SET
                relevant = EXCLUDED.relevant,
                relevance_reason = EXCLUDED.relevance_reason,
                relevance_model = EXCLUDED.relevance_model,
                status = CASE WHEN EXCLUDED.relevant THEN article_cards.status ELSE 'rejected' END,
                updated_at = now()
            """,
            {"id": article_id, "rel": relevant, "reason": reason, "model": model},
        )
        conn.commit()


def get_articles_needing_tags(limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, c.summary, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_tags at ON at.article_id = a.id
            WHERE c.summary IS NOT NULL AND c.relevant IS TRUE AND at.id IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def get_articles_needing_scores(limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, c.summary, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_scores sc ON sc.article_id = a.id
            WHERE c.summary IS NOT NULL AND c.relevant IS TRUE AND sc.id IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def find_article_candidates(query: str, limit: int = 20) -> list[dict]:
    terms = [term.strip().lower() for term in query.split() if term.strip()]
    if not terms:
        return []
    conditions = []
    params: list[str] = []
    for term in terms:
        conditions.append("LOWER(a.title || ' ' || COALESCE(a.raw_text, '')) LIKE %s")
        params.append(f"%{term}%")
    where = " OR ".join(conditions)
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, a.title, a.language, a.published_at, s.name AS source_name,
                   LEFT(COALESCE(a.raw_text, ''), 240) AS snippet
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE {where}
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def upsert_article_card(article_id: int, summary: str, model: str | None = None,
                        title_ru: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO article_cards (article_id, summary, summary_model, title_ru, summary_generated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (article_id) DO UPDATE SET
                summary = EXCLUDED.summary,
                summary_model = EXCLUDED.summary_model,
                title_ru = COALESCE(EXCLUDED.title_ru, article_cards.title_ru),
                summary_generated_at = now(),
                updated_at = now()
            """,
            (article_id, summary, model, title_ru),
        )
        conn.commit()


def set_article_title_ru(article_id: int, title_ru: str) -> None:
    """Проставить русский заголовок отдельной стадией перевода. Создаёт карточку,
    если её ещё нет (статья могла не пройти суммаризацию), не трогая summary."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO article_cards (article_id, title_ru)
            VALUES (%s, %s)
            ON CONFLICT (article_id) DO UPDATE SET
                title_ru = EXCLUDED.title_ru,
                updated_at = now()
            """,
            (article_id, title_ru),
        )
        conn.commit()


def list_article_texts_for_terminology_audit(limit: int = 200, article_id: int | None = None) -> list[dict]:
    clauses = ["(COALESCE(c.summary, '') <> '' OR COALESCE(c.title_ru, '') <> '')"]
    params: list = []
    if article_id is not None:
        clauses.append("a.id = %s")
        params.append(article_id)
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, a.title, a.raw_text, a.language, s.name AS source_name,
                   s.category AS source_category, c.summary, c.title_ru
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            WHERE {" AND ".join(clauses)}
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def update_article_terminology_texts(article_id: int, *, summary: str | None = None, title_ru: str | None = None) -> None:
    if summary is None and title_ru is None:
        return
    sets = ["updated_at = now()"]
    params: dict[str, object] = {"article_id": article_id}
    if summary is not None:
        sets.append("summary = %(summary)s")
        params["summary"] = summary
    if title_ru is not None:
        sets.append("title_ru = %(title_ru)s")
        params["title_ru"] = title_ru
    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE article_cards
            SET {", ".join(sets)}
            WHERE article_id = %(article_id)s
            """,
            params,
        )
        conn.commit()


def get_articles_needing_title_ru(limit: int = 20) -> list[dict]:
    """Статьи без русского заголовка (card.title_ru IS NULL ИЛИ карточки ещё нет).
    Бэкфилл отдельной стадии перевода по всей базе."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE c.title_ru IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def upsert_tag(rec: dict) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT id FROM tags
            WHERE name = %s AND (
                (parent_id IS NULL AND %s::bigint IS NULL) OR parent_id = %s::bigint
            )
            """,
            (rec.get("name"), rec.get("parent_id"), rec.get("parent_id")),
        )
        row = cur.fetchone()
        if row:
            tag_id = row[0]
            # Сид ДОПОЛНЯЕТ ключевые слова, а не заменяет. С 12.09 их можно править в UI
            # («Теги» → «Ключевые слова RU/EN»), и прежняя перезапись молча стирала бы
            # правки заказчика при каждом повторном прогоне seed-tags — а прогон случается
            # сам, в bootstrap на деплое. Порядок сохраняем: сначала то, что уже было.
            existing = conn.execute(
                "SELECT COALESCE(keywords_json, '[]'::jsonb), COALESCE(keywords_en_json, '[]'::jsonb), "
                "COALESCE(negative_keywords_json, '[]'::jsonb) "
                "FROM tags WHERE id = %s", (tag_id,)
            ).fetchone()

            def _merge(current, incoming):
                merged, seen = [], set()
                for word in [*(current or []), *(incoming or [])]:
                    key = str(word).strip().lower()
                    if key and key not in seen:
                        seen.add(key)
                        merged.append(word)
                return merged

            conn.execute(
                """
                UPDATE tags
                SET name_en = %(name_en)s,
                    description = %(description)s,
                    keywords_json = %(keywords_json)s,
                    keywords_en_json = %(keywords_en_json)s,
                    -- Стоп-слова сид РАНЬШЕ НЕ ПИСАЛ ВООБЩЕ: ни в UPDATE, ни в INSERT.
                    -- Поэтому в проде они были NULL у всех 18 тегов, механизм подавления
                    -- шума стоял пустым, и он же ломал сохранение экрана «Теги» (422).
                    negative_keywords_json = %(negative_keywords_json)s,
                    sort_order = %(sort_order)s,
                    updated_at = now()
                WHERE id = %(id)s
                """,
                {
                    **rec,
                    "id": tag_id,
                    "keywords_json": Json(_merge(existing[0], rec.get("keywords_json"))),
                    "keywords_en_json": Json(_merge(existing[1], rec.get("keywords_en_json"))),
                    # Стоп-слова ЗАМЕНЯЕМ, а не сливаем: они запрещают, и «случайно
                    # накопленный» запрет молча выкашивал бы статьи. Если вызывающий их
                    # не передал (None) — оставляем набранное руками, не затираем.
                    "negative_keywords_json": Json(
                        rec["negative_keywords_json"]
                        if rec.get("negative_keywords_json") is not None
                        else (existing[2] or [])
                    ),
                },
            )
            conn.commit()
            return tag_id

        cur = conn.execute(
            """
            INSERT INTO tags (parent_id, name, name_en, description, keywords_json,
                              keywords_en_json, negative_keywords_json, enabled, sort_order)
            VALUES (%(parent_id)s, %(name)s, %(name_en)s, %(description)s,
                    %(keywords_json)s, %(keywords_en_json)s,
                    %(negative_keywords_json)s, TRUE, %(sort_order)s)
            RETURNING id
            """,
            {
                **rec,
                "keywords_json": Json(rec.get("keywords_json") or []),
                "keywords_en_json": Json(rec.get("keywords_en_json") or []),
                "negative_keywords_json": Json(rec.get("negative_keywords_json") or []),
            },
        )
        tag_id = cur.fetchone()[0]
        conn.commit()
        return tag_id


def list_enabled_tags() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT child.*, parent.name AS parent_name, parent.name_en AS parent_name_en
            FROM tags child
            LEFT JOIN tags parent ON parent.id = child.parent_id
            WHERE child.enabled = TRUE
            ORDER BY child.parent_id NULLS FIRST, child.sort_order, child.id
            """
        )
        return cur.fetchall()


def save_tags(items: list[dict]) -> dict:
    """Bulk-сохранение дерева тегов из UI. Родители обрабатываются раньше детей
    (parent резолвится по имени). Отсутствующие в списке — отключаются (soft delete,
    чтобы не рвать FK на article_tags)."""
    name_to_id: dict[str, int] = {}
    keep: list[int] = []
    # сначала корневые, затем дочерние — чтобы parent_id уже был известен
    ordered = [i for i in items if not i.get("parent_name")] + [i for i in items if i.get("parent_name")]
    with get_connection() as conn:
        for it in ordered:
            parent_id = None
            parent_name = it.get("parent_name")
            if parent_name:
                # `parent_id` (если фронт его прислал) НАДЁЖНЕЕ имени: раньше связь искалась
                # только по имени, и переименование родителя в UI молча делало все его
                # подтеги КОРНЕВЫМИ — без ошибки и без предупреждения. Дерево разваливалось
                # на первом же редактировании, а заказчик как раз собирался расширять теги.
                parent_id = it.get("parent_id") or name_to_id.get(parent_name)
                if parent_id is None:
                    raise ValueError(
                        f"Подтег «{it['name']}» ссылается на родителя «{parent_name}», "
                        "которого нет в сохраняемом списке. Сохранение отменено, "
                        "чтобы подтеги не стали корневыми молча."
                    )
            payload = {
                "parent_id": parent_id,
                "name": it["name"],
                "name_en": it.get("name_en"),
                "description": it.get("description"),
                "keywords_json": Json(it.get("keywords_json") or []),
                "keywords_en_json": Json(it.get("keywords_en_json") or []),
                "negative_keywords_json": Json(it.get("negative_keywords_json") or []),
                "enabled": bool(it.get("enabled", True)),
                "sort_order": it.get("sort_order") or 0,
            }
            previous_name: str | None = None
            if it.get("id"):
                row = conn.execute("SELECT name FROM tags WHERE id = %s", (int(it["id"]),)).fetchone()
                previous_name = row[0] if row else None
                conn.execute(
                    """
                    UPDATE tags SET parent_id=%(parent_id)s, name=%(name)s, name_en=%(name_en)s,
                        description=%(description)s, keywords_json=%(keywords_json)s,
                        keywords_en_json=%(keywords_en_json)s, negative_keywords_json=%(negative_keywords_json)s,
                        enabled=%(enabled)s, sort_order=%(sort_order)s, updated_at=now()
                    WHERE id=%(id)s
                    """,
                    {**payload, "id": int(it["id"])},
                )
                tag_id = int(it["id"])
            else:
                # UPSERT, а не голый INSERT: тег с таким именем под тем же родителем мог
                # существовать и быть выключённым (delete_tag — мягкое удаление). Раньше
                # повторное добавление роняло запрос UniqueViolation → 500 у заказчика,
                # и «удалил, потом снова добавил» превращалось в тупик.
                # Конфликт по тому же выражению, что и idx_tags_name_parent.
                cur = conn.execute(
                    """
                    INSERT INTO tags (parent_id, name, name_en, description, keywords_json,
                                      keywords_en_json, negative_keywords_json, enabled, sort_order)
                    VALUES (%(parent_id)s, %(name)s, %(name_en)s, %(description)s,
                            %(keywords_json)s, %(keywords_en_json)s, %(negative_keywords_json)s, %(enabled)s, %(sort_order)s)
                    ON CONFLICT (name, (COALESCE(parent_id, 0))) DO UPDATE SET
                        parent_id = EXCLUDED.parent_id,
                        name_en = EXCLUDED.name_en,
                        description = EXCLUDED.description,
                        keywords_json = EXCLUDED.keywords_json,
                        keywords_en_json = EXCLUDED.keywords_en_json,
                        negative_keywords_json = EXCLUDED.negative_keywords_json,
                        enabled = EXCLUDED.enabled,
                        sort_order = EXCLUDED.sort_order,
                        updated_at = now()
                    RETURNING id
                    """,
                    payload,
                )
                tag_id = int(cur.fetchone()[0])
            name_to_id[it["name"]] = tag_id
            # СТАРОЕ имя тоже обязано вести к этому id: подтеги в запросе всё ещё несут
            # прежний parent_name, если родителя только что переименовали. Берём его из
            # БД по id — это не зависит от того, прислал ли фронт original_name.
            if previous_name and previous_name != it["name"]:
                name_to_id.setdefault(previous_name, tag_id)
            keep.append(tag_id)
        if keep:
            # СТРАХОВКА от потери таксономии одним нажатием. Сохранение выключает всё,
            # чего нет в присланном списке, — и это правильно, пока фронт шлёт ВЕСЬ
            # список. Но 13.09 проверочный запрос с одним тегом выключил разом все 14:
            # неполный список (обрыв загрузки, частичный рендер, чужой скрипт) стирает
            # справочник молча и без следа.
            # Порог намеренно мягкий: сокращение набора — законная операция (мы сами
            # ужали 18 направлений до 13), поэтому запрещаем только обвал БОЛЬШЕ чем
            # наполовину. Осознанная смена таксономии идёт отдельной командой
            # `retire-tags`, которой этот предохранитель не мешает.
            enabled_before = conn.execute(
                "SELECT count(*) FROM tags WHERE enabled"
            ).fetchone()[0]
            would_disable = conn.execute(
                "SELECT count(*) FROM tags WHERE enabled AND id <> ALL(%s)", (keep,)
            ).fetchone()[0]
            if enabled_before >= 4 and would_disable * 2 > enabled_before:
                raise ValueError(
                    f"Сохранение выключило бы {would_disable} тегов из {enabled_before} — "
                    "больше половины справочника. Похоже, список пришёл неполным. "
                    "Изменения отменены; смена таксономии целиком делается командой retire-tags."
                )
            system_row = conn.execute(
                "SELECT id FROM tags WHERE name = %s", (SYSTEM_TAG_UNCLASSIFIED,)
            ).fetchone()
            if system_row and int(system_row[0]) not in keep:
                keep.append(int(system_row[0]))
            conn.execute("UPDATE tags SET enabled=FALSE WHERE id <> ALL(%s)", (keep,))
        conn.commit()
    return {"saved": len(items)}


def clear_article_tags_for_disabled(limit: int | None = None) -> int:
    """Снять теги, присвоенные ныне ВЫКЛЮЧЕННЫМИ тегами. Возвращает, сколько снято.

    Нужно при смене таксономии: 13.09 набор из 18 направлений заменён на 13 тематик
    заказчика, и 11 тысяч статей остались с классификацией по несуществующему больше
    справочнику. Выборка тегирования берёт статьи, у которых тега НЕТ
    (`get_articles_needing_tags`: `at.id IS NULL`), поэтому снятие строки — и есть
    способ вернуть статью в очередь.

    Удаляются только строки связи, сами статьи и их суть не трогаются.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """
            DELETE FROM article_tags
            WHERE id IN (
                SELECT at.id FROM article_tags at
                JOIN tags t ON t.id = at.tag_id
                WHERE NOT t.enabled
                ORDER BY at.id
                LIMIT %s
            )
            """,
            (limit if limit is not None else 10_000_000,),
        )
        conn.commit()
        return cur.rowcount or 0


def disable_tags_except(names: list[str]) -> int:
    """Выключить все теги, кроме перечисленных. Возвращает, сколько выключено.

    Мягко: на теги ссылается article_tags, и удаление уничтожило бы историю
    классификации всего корпуса. Выключенный тег не предлагается модели
    (list_enabled_tags фильтрует по enabled) и не участвует в новой разметке.
    """
    if not names:
        raise ValueError("Пустой список тегов к сохранению — это выключило бы все теги разом")
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE tags SET enabled = FALSE, updated_at = now() "
            "WHERE enabled AND name <> ALL(%s)",
            (names,),
        )
        conn.commit()
        return cur.rowcount or 0


def delete_tag(tag_id: int) -> None:
    """Мягкое удаление тега и его подтегов (enabled=FALSE) — FK на article_tags не рвём."""
    with get_connection() as conn:
        row = conn.execute("SELECT name FROM tags WHERE id = %s", (tag_id,)).fetchone()
        if row and row[0] == SYSTEM_TAG_UNCLASSIFIED:
            raise ValueError(
                f"«{SYSTEM_TAG_UNCLASSIFIED}» — служебный приёмник, а не тематика. "
                "Без него статьи, не подошедшие ни к одной теме, уедут в первый тег списка."
            )
        conn.execute(
            "UPDATE tags SET enabled=FALSE, updated_at=now() WHERE id=%s OR parent_id=%s",
            (tag_id, tag_id),
        )
        conn.commit()


def upsert_article_tag(article_id: int, tag_id: int, confidence: float,
                       rationale: str | None, model: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO article_tags (article_id, tag_id, confidence, rationale, model)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (article_id) DO UPDATE SET
                tag_id = EXCLUDED.tag_id,
                confidence = EXCLUDED.confidence,
                rationale = EXCLUDED.rationale,
                model = EXCLUDED.model,
                created_at = now()
            """,
            (article_id, tag_id, confidence, rationale, model),
        )
        conn.commit()


def upsert_scoring_criterion(rec: dict) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO scoring_criteria (name, description, weight, keywords_json,
                                          keywords_en_json, enabled, sort_order)
            VALUES (%(name)s, %(description)s, %(weight)s, %(keywords_json)s,
                    %(keywords_en_json)s, TRUE, %(sort_order)s)
            -- Сид ГАРАНТИРУЕТ СУЩЕСТВОВАНИЕ критериев по умолчанию, но НЕ переопределяет
            -- решения человека. Раньше здесь стояло `enabled = TRUE`, и каждый деплой
            -- воскрешал критерии, которые заказчик выключил в UI. Это не теория: 11.09
            -- заказчик утром перестроил профиль (5 критериев, сумма ровно 100), в 11:40
            -- прошёл деплой брендинга, bootstrap поднял обратно три старых — и через две
            -- минуты он написал «а что случилось со скорингом? там сейчас 9 параметров».
            -- Хуже того, сумма весов стала бы 175 вместо 100, и стадия скоринга падает
            -- целиком на первой же статье (_validate_weights).
            -- Вес и флаг — территория человека (экран «Скоринг»), сид их не трогает.
            -- Ключевые слова дополняем, а не заменяем: их там тоже правят руками.
            ON CONFLICT (name) DO UPDATE SET
                description = COALESCE(scoring_criteria.description, EXCLUDED.description),
                keywords_json = (
                    SELECT COALESCE(jsonb_agg(DISTINCT w), '[]'::jsonb)
                    FROM jsonb_array_elements(
                        COALESCE(scoring_criteria.keywords_json, '[]'::jsonb) || EXCLUDED.keywords_json
                    ) AS w
                ),
                keywords_en_json = (
                    SELECT COALESCE(jsonb_agg(DISTINCT w), '[]'::jsonb)
                    FROM jsonb_array_elements(
                        COALESCE(scoring_criteria.keywords_en_json, '[]'::jsonb) || EXCLUDED.keywords_en_json
                    ) AS w
                ),
                updated_at = now()
            RETURNING id
            """,
            {
                **rec,
                "keywords_json": Json(rec.get("keywords_json") or []),
                "keywords_en_json": Json(rec.get("keywords_en_json") or []),
            },
        )
        criterion_id = cur.fetchone()[0]
        conn.commit()
        return criterion_id


def list_enabled_scoring_criteria() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT *
            FROM scoring_criteria
            WHERE enabled = TRUE
            ORDER BY sort_order, id
            """
        )
        return cur.fetchall()


def delete_scoring_criterion(criterion_id: int) -> None:
    """Мягкое удаление критерия (enabled=FALSE) — не рвём FK на article_score_items.

    ПРОВЕРЯЕМ сумму весов оставшихся активных критериев. Раньше не проверяли, и это было
    миной: «Сохранить» сумму валидирует, а «Удалить» уходило в БД немедленно. Стоило уйти
    со страницы, не добив веса до 100, — и следующая стадия скоринга падала ЦЕЛИКОМ, ещё
    до первой статьи (`_validate_weights`, pipeline.py). Заказчик 11.09 как раз просил
    «убрать старые, не актуальные» критерии — по одному это гарантированно ломало бы
    скоринг на каждом шаге. Теперь удаление отклоняется с понятным текстом, а привести
    профиль в порядок можно одним «Сохранить» (bulk), где веса пересчитываются вместе.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(weight), 0) FROM scoring_criteria "
            "WHERE enabled = TRUE AND id <> %s",
            (criterion_id,),
        ).fetchone()
        remaining = round(float(row[0]), 2)
        if remaining != 100:
            raise ValueError(
                f"После удаления сумма весов активных критериев станет {remaining}, а нужна 100. "
                "Сначала перераспределите веса и нажмите «Сохранить» — тогда скоринг не упадёт."
            )
        conn.execute(
            "UPDATE scoring_criteria SET enabled = FALSE, updated_at = now() WHERE id = %s",
            (criterion_id,),
        )
        conn.commit()


def save_scoring_criteria(items: list[dict]) -> dict:
    """Bulk-сохранение профиля критериев (как кнопка «Сохранить» в мокапе).

    Валидирует сумму весов = 100. Существующие обновляются по id, новые вставляются,
    отсутствующие в списке — отключаются (soft delete, чтобы не рвать FK).
    """
    total = round(sum(float(i.get("weight") or 0) for i in items), 2)
    if total != 100:
        raise ValueError(f"Сумма весов критериев должна быть 100, сейчас {total}")

    keep_ids: list[int] = []
    with get_connection() as conn:
        for it in items:
            payload = {
                "name": it["name"],
                "description": it.get("description"),
                "weight": it["weight"],
                "keywords_json": Json(it.get("keywords_json") or []),
                "keywords_en_json": Json(it.get("keywords_en_json") or []),
                "sort_order": it.get("sort_order") or 0,
            }
            if it.get("id"):
                conn.execute(
                    """
                    UPDATE scoring_criteria SET name=%(name)s, description=%(description)s,
                        weight=%(weight)s, keywords_json=%(keywords_json)s,
                        keywords_en_json=%(keywords_en_json)s, sort_order=%(sort_order)s,
                        enabled=TRUE, updated_at=now()
                    WHERE id=%(id)s
                    """,
                    {**payload, "id": int(it["id"])},
                )
                keep_ids.append(int(it["id"]))
            else:
                cur = conn.execute(
                    """
                    INSERT INTO scoring_criteria (name, description, weight, keywords_json,
                                                  keywords_en_json, enabled, sort_order)
                    VALUES (%(name)s, %(description)s, %(weight)s, %(keywords_json)s,
                            %(keywords_en_json)s, TRUE, %(sort_order)s)
                    ON CONFLICT (name) DO UPDATE SET description=EXCLUDED.description,
                        weight=EXCLUDED.weight, keywords_json=EXCLUDED.keywords_json,
                        keywords_en_json=EXCLUDED.keywords_en_json, enabled=TRUE, updated_at=now()
                    RETURNING id
                    """,
                    payload,
                )
                keep_ids.append(int(cur.fetchone()[0]))
        if keep_ids:
            conn.execute(
                "UPDATE scoring_criteria SET enabled=FALSE, updated_at=now() WHERE id <> ALL(%s)",
                (keep_ids,),
            )
        conn.commit()
    return {"saved": len(items), "weight_sum": total}


def replace_article_score(article_id: int, total_score: float, score_label: str,
                          explanation: str, items: list[dict],
                          model: str | None = None) -> None:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO article_scores (article_id, model, total_score, score_label, explanation)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (article_id) DO UPDATE SET
                model = EXCLUDED.model,
                total_score = EXCLUDED.total_score,
                score_label = EXCLUDED.score_label,
                explanation = EXCLUDED.explanation,
                updated_at = now()
            RETURNING id
            """,
            (article_id, model, total_score, score_label, explanation),
        )
        score_id = cur.fetchone()[0]
        conn.execute("DELETE FROM article_score_items WHERE article_score_id = %s", (score_id,))
        for item in items:
            conn.execute(
                """
                INSERT INTO article_score_items
                  (article_score_id, criterion_id, keyword_score, ai_score, final_score, rationale)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    score_id,
                    item["criterion_id"],
                    item.get("keyword_score"),
                    item.get("ai_score"),
                    item.get("final_score"),
                    item.get("rationale"),
                ),
            )
        conn.commit()


def recompute_total_scores_from_items(keyword_weight: float, ai_weight: float) -> int:
    """Пересчитать total_score/score_label/final_score из УЖЕ сохранённых ai_score/keyword_score
    (article_score_items) по текущему блендингу — БЕЗ повторного вызова OpenAI и без воркера.

    Применяет НОВЫЙ блендинг к старым AI-баллам (полезно, когда менялась только формула, а
    переспрашивать модель дорого/недоступно — напр. внешний воркер лежит). Формула синхронна
    pipeline.normalize_score_payload: final = max(ai, kw*keyword_weight + ai*ai_weight);
    total = Σ final*weight/100 (вес критерия из scoring_criteria). Пороги score_label синхронны
    pipeline.score_label (80/65/40). ai_score/keyword_score не трогаются → можно гонять повторно
    или поверх сделать полный AI-перепрогон. Возвращает число обновлённых статей."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE article_score_items
            SET final_score = ROUND(
                GREATEST(COALESCE(ai_score, 0),
                         COALESCE(keyword_score, 0) * %s + COALESCE(ai_score, 0) * %s)::numeric, 2)
            """,
            (keyword_weight, ai_weight),
        )
        cur = conn.execute(
            """
            -- ТОЛЬКО активные критерии. Без фильтра выключенные продолжали вносить вклад:
            -- на проде 12.09 три выключенных критерия несут вес 35+30+10 = 75, и сумма
            -- весов у старой статьи становилась 175 вместо 100 — баллы уезжали вверх без
            -- всякой причины. Заказчик 11.09 как раз сменил профиль критериев, так что
            -- «старые items + новые веса» — это не теория, а текущее состояние базы.
            WITH recomputed AS (
                SELECT i.article_score_id,
                       SUM(i.final_score * c.weight / 100.0) AS total,
                       SUM(c.weight) AS weight_sum
                FROM article_score_items i
                JOIN scoring_criteria c ON c.id = i.criterion_id AND c.enabled
                GROUP BY i.article_score_id
                -- Статьи, оценённые ТОЛЬКО по ныне выключенным критериям, пропускаем:
                -- их «пересчёт» дал бы 0 и молча обнулил ленту. Им нужен полноценный
                -- перепрогон скоринга, а не пересчёт блендинга.
                HAVING SUM(c.weight) > 0
            )
            UPDATE article_scores s
            SET total_score = ROUND(LEAST(GREATEST(r.total, 0), 100)::numeric, 2),
                score_label = CASE
                    WHEN r.total >= 80 THEN 'Высокая'
                    WHEN r.total >= 65 THEN 'Выше средней'
                    WHEN r.total >= 40 THEN 'Средняя'
                    ELSE 'Низкая' END,
                updated_at = now()
            FROM recomputed r
            WHERE s.id = r.article_score_id
            """
        )
        conn.commit()
        return cur.rowcount or 0


def insert_ai_run(rec: dict) -> None:
    # job_id по умолчанию NULL — для локального пути и старых вызовов (не дедуплицируются).
    # ON CONFLICT DO NOTHING (баг H1/T2): повторное применение результата задачи не двоит
    # биллинг — одна строка на (job_id, article_id, stage).
    rec = {"job_id": None, **rec}
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO ai_processing_runs
              (job_id, article_id, stage, provider, model, language, input_tokens, output_tokens,
               total_tokens, cost_usd, status, error_message)
            VALUES (%(job_id)s, %(article_id)s, %(stage)s, %(provider)s, %(model)s, %(language)s,
                    %(input_tokens)s, %(output_tokens)s, %(total_tokens)s, %(cost_usd)s,
                    %(status)s, %(error_message)s)
            ON CONFLICT (job_id, article_id, stage) DO NOTHING
            """,
            rec,
        )
        conn.commit()


def ai_cost_report() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT stage, COALESCE(language, 'unknown') AS language,
                   COUNT(*) AS runs,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(total_tokens) AS total_tokens,
                   ROUND(SUM(cost_usd)::numeric, 6) AS cost_usd,
                   ROUND(AVG(total_tokens)::numeric, 2) AS avg_tokens_per_run
            FROM ai_processing_runs
            GROUP BY stage, COALESCE(language, 'unknown')
            ORDER BY stage, language
            """
        )
        return cur.fetchall()


def ai_article_cost_report(limit: int = 20, complete_only: bool = True) -> list[dict]:
    """Cost of processing one article through AI stages.

    complete_only=True returns articles that have successful summary, tagging and
    scoring runs, which is the cleanest estimate for one full processing cycle.
    """
    stages_filter = "HAVING COUNT(DISTINCT r.stage) = 3" if complete_only else ""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT
                a.id AS article_id,
                a.title,
                a.language,
                COUNT(*) FILTER (WHERE r.status = 'ok') AS runs,
                COUNT(DISTINCT r.stage) FILTER (WHERE r.status = 'ok') AS stages,
                SUM(r.input_tokens) FILTER (WHERE r.status = 'ok') AS input_tokens,
                SUM(r.output_tokens) FILTER (WHERE r.status = 'ok') AS output_tokens,
                SUM(r.total_tokens) FILTER (WHERE r.status = 'ok') AS total_tokens,
                ROUND(SUM(r.cost_usd) FILTER (WHERE r.status = 'ok')::numeric, 6) AS cost_usd
            FROM ai_processing_runs r
            JOIN articles a ON a.id = r.article_id
            WHERE r.stage IN ('summary', 'tagging', 'scoring')
            GROUP BY a.id, a.title, a.language
            {stages_filter}
            ORDER BY cost_usd DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def digest_candidates(month: str | None = None, limit: int = 20, min_score: float = 60,
                      user_id: int | None = None, max_score: float | None = None,
                      search: str | None = None, top_tag: str | None = None) -> list[dict]:
    """Статьи, выбранные в дайджест КОНКРЕТНЫМ пользователем (его user_article_states.status='digest').

    ``month`` опционален: пусто/None — фильтр периода снимается, возвращаются все
    выбранные (превью совпадает с экспортом). Будущие публикации всегда исключены.
    """
    params: dict = {"min_score": min_score, "limit": limit, "user_id": user_id}
    month_clause = ""
    max_score_clause = ""
    search_clause = ""
    tag_clause = ""
    if month:
        month_clause = "AND to_char(COALESCE(a.published_at, a.collected_at), 'YYYY-MM') = %(month)s"
        params["month"] = month
    if max_score is not None:
        max_score_clause = "AND COALESCE(sc.total_score, 0) <= %(max_score)s"
        params["max_score"] = max_score
    if search:
        search_clause = """
              AND (
                   COALESCE(c.title_ru, a.title) ILIKE %(search)s
                OR COALESCE(c.summary, '') ILIKE %(search)s
                OR COALESCE(t.name, '') ILIKE %(search)s
                OR COALESCE(parent.name, '') ILIKE %(search)s
              )
        """
        params["search"] = f"%{search}%"
    if top_tag:
        tag_clause = """
              AND (
                   t.name = %(top_tag)s
                OR parent.name = %(top_tag)s
              )
        """
        params["top_tag"] = top_tag
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, COALESCE(c.title_ru, a.title) AS title, a.url, a.published_at, a.language, a.image_url,
                   s.name AS source_name,
                   c.summary, TRUE AS selected_for_digest,
                   sc.total_score, sc.score_label,
                   t.name AS tag_name, parent.name AS parent_tag_name
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN user_article_states uas ON uas.article_id = a.id AND uas.user_id = %(user_id)s
            LEFT JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_scores sc ON sc.article_id = a.id
            LEFT JOIN article_tags at ON at.article_id = a.id
            LEFT JOIN tags t ON t.id = at.tag_id
            LEFT JOIN tags parent ON parent.id = t.parent_id
            WHERE uas.status = 'digest'
              AND s.archived_at IS NULL          -- архивный источник не попадает и в выпуск
              -- Перепечатка в выпуск не идёт: в дайджесте нужна одна копия новости,
              -- и это ровно то, что заказчик делает руками («одну заберу в дайджест,
              -- вторую отмечу как дубликат», 22.08).
              AND NOT EXISTS (SELECT 1 FROM article_reprints ar WHERE ar.article_id = a.id)
              AND c.relevant IS NOT FALSE
              AND (a.published_at IS NULL OR a.published_at <= now() + interval '2 days')
              AND COALESCE(sc.total_score, 0) >= %(min_score)s
              {max_score_clause}
              {month_clause}
              {search_clause}
              {tag_clause}
            ORDER BY sc.total_score DESC NULLS LAST,
                     a.published_at DESC NULLS LAST
            LIMIT %(limit)s
            """,
            params,
        )
        article_rows = cur.fetchall()
        signal_params = dict(params)
        signal_month_clause = ""
        signal_search_clause = ""
        signal_tag_clause = ""
        if month:
            signal_month_clause = "AND to_char(COALESCE(best_evidence.published_at, sig.last_seen_at, sig.created_at), 'YYYY-MM') = %(month)s"
        if search:
            signal_search_clause = """
              AND (
                   COALESCE(sig.title_ru, sig.title) ILIKE %(search)s
                OR COALESCE(sig.summary, '') ILIKE %(search)s
                OR COALESCE(sig.thesis, '') ILIKE %(search)s
                OR COALESCE(sig.theme, '') ILIKE %(search)s
              )
            """
        if top_tag:
            signal_tag_clause = "AND sig.theme = %(top_tag)s"
        cur.execute(
            f"""
            SELECT sig.id,
                   COALESCE(sig.title_ru, sig.title) AS title,
                   COALESCE(best_evidence.source_url, '') AS url,
                   COALESCE(best_evidence.published_at, sig.last_seen_at) AS published_at,
                   'mixed' AS language,
                   '' AS image_url,
                   COALESCE(best_evidence.publisher, 'Радар сигналов') AS source_name,
                   COALESCE(sig.summary, sig.thesis, '') AS summary,
                   TRUE AS selected_for_digest,
                   sig.score AS total_score,
                   sig.maturity AS score_label,
                   sig.theme AS tag_name,
                   NULL AS parent_tag_name,
                   'signal' AS item_type
            FROM signals sig
            JOIN user_signal_states uss ON uss.signal_id = sig.id AND uss.user_id = %(user_id)s
            LEFT JOIN LATERAL (
              SELECT source_url, publisher, published_at
              FROM signal_evidence
              WHERE signal_id = sig.id
              ORDER BY strength DESC, published_at DESC NULLS LAST, created_at DESC
              LIMIT 1
            ) best_evidence ON TRUE
            WHERE uss.status = 'digest'
              AND sig.maturity <> 'reject'
              AND sig.score >= %(min_score)s
              {max_score_clause.replace('COALESCE(sc.total_score, 0)', 'sig.score')}
              {signal_month_clause}
              {signal_search_clause}
              {signal_tag_clause}
            ORDER BY sig.score DESC NULLS LAST,
                     COALESCE(best_evidence.published_at, sig.last_seen_at) DESC NULLS LAST
            LIMIT %(limit)s
            """,
            signal_params,
        )
        signal_rows = cur.fetchall()
        combined = [*article_rows, *signal_rows]
        combined.sort(
            key=lambda row: (
                float(row.get("total_score") or 0),
                row.get("published_at") or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )
        return combined[:limit]


def save_monthly_digest(
    month: str,
    title: str,
    items: list[dict],
    status: str = "draft",
    user_id: int | None = None,
) -> dict:
    """Persist a monthly digest draft and replace its ordered item list."""
    with get_connection() as conn:
        if user_id is None:
            cur = conn.execute(
                """
                INSERT INTO monthly_digests (user_id, month, title, status)
                VALUES (NULL, %s, %s, %s)
                ON CONFLICT (month) WHERE user_id IS NULL DO UPDATE SET
                    title = EXCLUDED.title,
                    status = EXCLUDED.status,
                    updated_at = now()
                RETURNING id, user_id, month, title, status
                """,
                (month, title, status),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO monthly_digests (user_id, month, title, status)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, month) DO UPDATE SET
                    title = EXCLUDED.title,
                    status = EXCLUDED.status,
                    updated_at = now()
                RETURNING id, user_id, month, title, status
                """,
                (user_id, month, title, status),
            )
        digest = cur.fetchone()
        digest_id = digest[0]
        conn.execute("DELETE FROM monthly_digest_items WHERE digest_id = %s", (digest_id,))
        for idx, item in enumerate(items, start=1):
            conn.execute(
                """
                INSERT INTO monthly_digest_items (digest_id, article_id, sort_order, section, editor_note)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    digest_id,
                    int(item["article_id"]),
                    idx,
                    item.get("section"),
                    item.get("editor_note"),
                ),
            )
        conn.commit()
        return {
            "id": digest[0],
            "user_id": digest[1],
            "month": digest[2],
            "title": digest[3],
            "status": digest[4],
            "items": len(items),
        }


def get_monthly_digest(month: str, user_id: int | None = None) -> dict | None:
    """Fetch a persisted monthly digest draft with ordered item article ids."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if user_id is None:
            cur.execute(
                """
                SELECT id, user_id, month, title, status, created_at, updated_at
                FROM monthly_digests
                WHERE month = %s AND user_id IS NULL
                """,
                (month,),
            )
        else:
            # Свой дайджест ДОЛЖЕН побеждать общий/легаси (user_id IS NULL).
            # Было: ORDER BY (user_id = <id>) DESC — для общей строки выражение
            # NULL = <id> даёт NULL, а в Postgres DESC по умолчанию NULLS FIRST,
            # то есть легаси-строка вставала ПЕРЕД личной и LIMIT 1 отдавал её
            # (проверено на проде). Условие (user_id IS NOT NULL) NULL не порождает.
            cur.execute(
                """
                SELECT id, user_id, month, title, status, created_at, updated_at
                FROM monthly_digests
                WHERE month = %s AND (user_id = %s OR user_id IS NULL)
                ORDER BY (user_id IS NOT NULL) DESC, updated_at DESC
                LIMIT 1
                """,
                (month, user_id),
            )
        digest = cur.fetchone()
        if digest is None:
            return None
        cur.execute(
            """
            SELECT article_id, sort_order, section, editor_note
            FROM monthly_digest_items
            WHERE digest_id = %s
            ORDER BY sort_order, id
            """,
            (digest["id"],),
        )
        return {**digest, "items": cur.fetchall()}


def digest_items_by_article_ids(article_ids: list[int]) -> list[dict]:
    """Детали статей сохранённого дайджеста по списку id (порядок сохраняется).

    ПЕР-ЮЗЕРНОГО СКОУПА ЗДЕСЬ НЕТ И БЫТЬ НЕ ДОЛЖНО: выбираются только глобальные поля
    статьи (заголовок, ссылка, суть, скор, теги). Раньше функция принимала `user_id`
    и НИГДЕ его не использовала — это создавало ложное ощущение фильтрации по владельцу.
    За принадлежность отвечает ВЫЗЫВАЮЩИЙ: article_ids приходят из get_monthly_digest,
    который скоупит дайджест по user_id.
    """
    if not article_ids:
        return []
    order_case = "CASE " + " ".join(f"WHEN a.id = %s THEN {index}" for index, _ in enumerate(article_ids, start=1)) + " END"
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, COALESCE(c.title_ru, a.title) AS title, a.url, a.published_at, a.language, a.image_url,
                   s.name AS source_name,
                   c.summary,
                   sc.total_score, sc.score_label,
                   t.name AS tag_name, parent.name AS parent_tag_name
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_scores sc ON sc.article_id = a.id
            LEFT JOIN article_tags at ON at.article_id = a.id
            LEFT JOIN tags t ON t.id = at.tag_id
            LEFT JOIN tags parent ON parent.id = t.parent_id
            WHERE a.id = ANY(%s)
              AND c.relevant IS NOT FALSE
              AND (a.published_at IS NULL OR a.published_at <= now() + interval '2 days')
            ORDER BY {order_case}
            """,
            [article_ids, *article_ids],
        )
        return cur.fetchall()


def max_article_id() -> int | None:
    """Return the current maximum article id, or None if the table is empty."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(id) FROM articles")
        row = cur.fetchone()
        return row[0] if row else None


def get_articles_needing_summary_after(after_id: int, limit: int = 20) -> list[dict]:
    """Articles that have no summary yet and whose id > after_id (streaming checkpoint)."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE a.id > %s AND c.summary IS NULL
            ORDER BY a.id ASC
            LIMIT %s
            """,
            (after_id, limit),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
#  Обратная связь человека: оценки + комментарий (требование владельца 12.09)
# ---------------------------------------------------------------------------
# Быстрые причины в один клик. Набор выведен из того, за что заказчик РЕАЛЬНО ставит
# «Шум»: замер 03.09 — 239 пометок, из них 34 оказались не «не по теме», а обрывками.
# Без причины эти два случая сливаются, и обучать гейт на такой смеси нельзя: система
# выучит «короткие статьи нерелевантны» вместо «эта тема не наша».
FEEDBACK_REASONS = (
    "off_topic",        # не наша тема
    "incomplete_text",  # обрывок, судить не о чем
    "duplicate",        # уже было
    "bad_translation",  # смысл искажён переводом
    "bad_source",       # дело не в статье, а в источнике
    "good",             # годный сигнал — положительный пример тоже обучает
    "other",
)


def get_user_article_status(user_id: int, article_id: int) -> str | None:
    """Текущий статус статьи у этого человека, или None если он её не трогал.

    Нужен, чтобы записать в журнал ПРЕЖНЕЕ значение: в user_article_states одна строка
    на пару «человек × статья», истории переходов нет, и после UPDATE старое уже не достать.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM user_article_states WHERE user_id = %s AND article_id = %s",
            (user_id, article_id),
        ).fetchone()
        return row[0] if row else None


def save_feedback_entry(user_id: int, *, article_id: int | None = None,
                        source_id: int | None = None, reason: str | None = None,
                        usefulness: int | None = None, translation: int | None = None,
                        source_quality: int | None = None,
                        comment: str | None = None) -> dict:
    """Сохранить или обновить карточку обратной связи.

    Одна карточка на пару «человек × сигнал»: повторное сохранение ПРАВИТ её, а не
    плодит дубли — иначе обучающая выборка окажется перекошена теми статьями, которые
    человек открывал чаще.

    `source_id` при ОС о сигнале подставляется из статьи, даже если не передан: так
    накопленное сворачивается по источнику без прохода по всей ленте.
    """
    if article_id is None and source_id is None:
        raise ValueError("Обратная связь должна быть привязана к сигналу или к источнику")
    if reason is not None and reason not in FEEDBACK_REASONS:
        raise ValueError(f"Неизвестная причина: {reason}")
    for name, value in (("usefulness", usefulness), ("translation", translation),
                        ("source_quality", source_quality)):
        if value is not None and not 1 <= int(value) <= 5:
            raise ValueError(f"Оценка «{name}» должна быть от 1 до 5, получено {value}")

    with get_connection() as conn:
        if article_id is not None and source_id is None:
            row = conn.execute("SELECT source_id FROM articles WHERE id = %s", (article_id,)).fetchone()
            if row is None:
                raise ValueError(f"Статья {article_id} не найдена")
            source_id = int(row[0])

        # COALESCE на UPDATE: частичное сохранение (поставил только оценку) не должно
        # стирать уже написанный комментарий — правка карточки идёт по кусочкам.
        conflict = ("(user_id, article_id) WHERE article_id IS NOT NULL" if article_id is not None
                    else "(user_id, source_id) WHERE article_id IS NULL AND source_id IS NOT NULL")
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            INSERT INTO feedback_entries
              (user_id, article_id, source_id, reason, usefulness, translation, source_quality, comment)
            VALUES (%(user_id)s, %(article_id)s, %(source_id)s, %(reason)s,
                    %(usefulness)s, %(translation)s, %(source_quality)s, %(comment)s)
            ON CONFLICT {conflict} DO UPDATE SET
              source_id      = COALESCE(EXCLUDED.source_id, feedback_entries.source_id),
              reason         = COALESCE(EXCLUDED.reason, feedback_entries.reason),
              usefulness     = COALESCE(EXCLUDED.usefulness, feedback_entries.usefulness),
              translation    = COALESCE(EXCLUDED.translation, feedback_entries.translation),
              source_quality = COALESCE(EXCLUDED.source_quality, feedback_entries.source_quality),
              comment        = COALESCE(EXCLUDED.comment, feedback_entries.comment),
              updated_at     = now()
            RETURNING *
            """,
            {"user_id": user_id, "article_id": article_id, "source_id": source_id,
             "reason": reason, "usefulness": usefulness, "translation": translation,
             "source_quality": source_quality, "comment": comment},
        )
        saved = cur.fetchone()
        conn.commit()
        return saved


def get_feedback_entry(user_id: int, article_id: int | None = None,
                       source_id: int | None = None) -> dict | None:
    """Карточка ОС этого человека по сигналу или по источнику."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if article_id is not None:
            cur.execute(
                "SELECT * FROM feedback_entries WHERE user_id = %s AND article_id = %s",
                (user_id, article_id),
            )
        else:
            cur.execute(
                "SELECT * FROM feedback_entries "
                "WHERE user_id = %s AND source_id = %s AND article_id IS NULL",
                (user_id, source_id),
            )
        return cur.fetchone()


def feedback_training_set(limit: int = 500, reason: str | None = None) -> list[dict]:
    """Выгрузка обратной связи для обучения агентов.

    Отдаёт то, чего у модели нет из самой статьи: вердикт человека, его оценки и текст.
    Заголовок и суть приложены, чтобы набор был самодостаточным — агент читает его без
    доступа к БД (ИИ на проде считается на внешнем воркере, у него БД нет).
    """
    clauses, params = ["f.comment IS NOT NULL OR f.reason IS NOT NULL"], []
    if reason:
        clauses.append("f.reason = %s")
        params.append(reason)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT f.id, f.reason, f.usefulness, f.translation, f.source_quality,
                   f.comment, f.created_at,
                   a.id AS article_id, a.title, a.url,
                   c.title_ru, c.summary,
                   s.id AS source_id, s.name AS source_name
            FROM feedback_entries f
            LEFT JOIN articles a ON a.id = f.article_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN sources s ON s.id = f.source_id
            WHERE {" AND ".join(clauses)}
            ORDER BY f.created_at DESC
            LIMIT %s
            """,
            [*params, limit],
        )
        return cur.fetchall()


def feedback_source_summary(limit: int = 300) -> list[dict]:
    """Свод оценок по источникам: на что человек жалуется чаще всего.

    Это прямой ответ на «почему не идут технологические новости» и на запрос заказчика
    убрать мёртвые источники: видно, какие источники люди системно метят как негодные.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT s.id AS source_id, s.name AS source_name,
                   count(*) AS entries,
                   round(avg(f.usefulness)::numeric, 2)     AS avg_usefulness,
                   round(avg(f.translation)::numeric, 2)    AS avg_translation,
                   round(avg(f.source_quality)::numeric, 2) AS avg_source_quality,
                   count(*) FILTER (WHERE f.reason = 'off_topic')       AS off_topic,
                   count(*) FILTER (WHERE f.reason = 'incomplete_text') AS incomplete_text,
                   count(*) FILTER (WHERE f.reason = 'duplicate')       AS duplicate,
                   count(*) FILTER (WHERE f.reason = 'bad_translation') AS bad_translation,
                   count(*) FILTER (WHERE f.reason = 'good')            AS good
            FROM feedback_entries f
            JOIN sources s ON s.id = f.source_id
            GROUP BY s.id, s.name
            ORDER BY count(*) DESC, s.name
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()
