"""HTTP API for the OilTech Digest admin frontend."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import hmac
import logging
import os
from pathlib import Path
import secrets
import time
from typing import Any

from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest import auth, background_jobs, backlog, config
from oiltech_digest.benchmarks import run_readiness_benchmark
from oiltech_digest.config import REPO_ROOT
from oiltech_digest.db.connection import get_connection
from oiltech_digest.db import documents_repo, repository
from oiltech_digest.logging_utils import setup_logging
from oiltech_digest.maintenance import maintenance_cleanup, maintenance_status
from oiltech_digest import network_policy
from oiltech_digest.processing.pipeline import (
    make_client,
    process_pipeline_articles,
)
from oiltech_digest.readiness import readiness_check
from oiltech_digest.ingestion import normalize, playwright_parser, request_parser
from oiltech_digest.ingestion import external_fetch
from oiltech_digest.documents import external as documents_external
from oiltech_digest.documents import parsing as doc_parsing
from oiltech_digest.documents.model import DocumentError
from oiltech_digest.ingestion.manual_import import ManualImportError, import_article as import_manual_article
from oiltech_digest.ingestion.source_diagnostics import diagnose_source
from oiltech_digest.processing.digest import (
    build_digest_content,
    get_digest_branding,
    render_digest_email,
    save_digest_branding,
    save_digest_draft,
    write_digest_export,
)
from oiltech_digest.processing import external_ai
from oiltech_digest.source_discovery.learning import apply_candidate_learning

WEB_DIR = REPO_ROOT / "web"
FRONTEND_DIST_DIR = REPO_ROOT / "frontend" / "dist"

setup_logging("api")
logger = logging.getLogger(__name__)

app = FastAPI(title="OilTech Digest API")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
if (FRONTEND_DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST_DIR / "assets"), name="frontend-assets")


@app.middleware("http")
async def log_requests(request, call_next):
    if request.url.path.startswith("/static") or request.url.path.startswith("/assets"):
        return await call_next(request)

    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000
    client = request.client.host if request.client else "-"
    logger.info(
        "request method=%s path=%s status=%s duration_ms=%.1f client=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        client,
    )
    return response


# Набор допустимых пер-юзерных статусов — единый источник правды в repository.ArticleStatus
# (там же кортеж для счётчиков). Колонки статуса — свободный TEXT без CHECK, поэтому
# валидация на границе API — единственное, что не даёт записать мусор: статья с
# неизвестным статусом молча пропадает из всех вкладок (фильтры перечисляют известный набор).
class ArticlePatch(BaseModel):
    status: repository.ArticleStatus | None = None
    selected_for_digest: bool | None = None
    analyst_comment: str | None = None


class SourcePatch(BaseModel):
    enabled: bool | None = None
    url: str | None = None
    rss_url: str | None = None
    parse_strategy: str | None = None
    update_frequency: str | None = None
    listing_url: str | None = None
    listing_strategy: str | None = None
    listing_selector: str | None = None
    article_link_selector: str | None = None
    article_date_selector: str | None = None
    network_region: str | None = None
    network_profile: str | None = None


class SourceCreate(BaseModel):
    name: str
    url: str | None = None
    rss_url: str = ""
    priority: float = 1.0
    category: str | None = None
    update_frequency: str | None = None


class SourceCandidateEvaluateRequest(BaseModel):
    article_limit: int = 5
    offline: bool = True
    collect: bool = True
    process: bool = True


class SourceCandidateApproveRequest(BaseModel):
    name: str | None = None
    source_type: str = "Discovered"
    parse_strategy: str | None = None
    enabled: bool = False
    category: str | None = None
    priority: float = 1.0
    network_region: str = "auto"
    scrape_after_approve: bool = True


class SourceCandidatePatch(BaseModel):
    status: str | None = None
    recommended_action: str | None = None
    review_comment: str | None = None


class SignalDiscoveryRequest(BaseModel):
    topic: str | None = None
    days: int = 14
    limit: int = 80
    min_score: float = 40
    max_signals: int = 10
    offline: bool = True
    dry_run: bool = False
    web_search: bool = False
    web_only: bool = False
    web_query_limit: int = 8


class SignalFeedbackCreate(BaseModel):
    article_id: int | None = None
    signal_id: int | None = None
    signal_evidence_id: int | None = None
    source_url: str | None = None
    signal_title: str | None = None
    source: str | None = None
    comment: str = ""
    verdict: str | None = None
    reason: str | None = None
    corrected_title: str | None = None
    corrected_thesis: str | None = None
    duplicate_of_signal_id: int | None = None


class SignalPatch(BaseModel):
    status: str | None = None
    selected_for_digest: bool | None = None
    analyst_comment: str | None = None


class SourceDiscoveryPlanRequest(BaseModel):
    days: int = 30
    target_per_topic: int = 10
    topic_limit: int = 5
    candidate_limit: int = 10
    max_actions: int = 5
    persist_memory: bool = True
    offline: bool = True
    evaluate: bool = True


class SourceDiscoveryDiscoverRequest(BaseModel):
    topic: str
    seed_url: str
    limit: int = 20
    offline: bool = True
    fetch_inspection: bool = False
    test_parse: bool = False


class SourceDiscoveryLoopRequest(BaseModel):
    goal: str = "Найти новые полезные источники сигналов"
    days: int = 30
    target_per_topic: int = 10
    topic_limit: int = 5
    candidate_limit: int = 10
    max_actions: int = 5
    max_iterations: int = 3
    offline: bool = True
    fetch_inspection: bool = True
    test_parse: bool = True
    dry_run: bool = False
    auto_evaluate: bool = True
    article_limit: int = 5
    persist_memory: bool = True
    max_daily_loop_runs: int = 4
    max_daily_candidates: int = 100
    max_daily_evaluations: int = 100


class AgentMemoryPatch(BaseModel):
    status: str


class AgentMemoryCreate(BaseModel):
    memory_type: str
    subject: str
    status: str = "active"
    score: float = 50
    facts: dict[str, Any] | None = None


class ScoringCriterionIn(BaseModel):
    id: int | None = None
    name: str
    description: str | None = None
    weight: float
    # Та же ловушка, что у TagIn: явный null из БД отвергался бы 422-м.
    keywords_json: list[str] | None = None
    keywords_en_json: list[str] | None = None
    sort_order: int = 0


class TagIn(BaseModel):
    id: int | None = None
    parent_name: str | None = None
    # Надёжная связь с родителем: имя переживает переименование плохо (см. save_tags).
    parent_id: int | None = None
    original_name: str | None = None
    name: str
    name_en: str | None = None
    description: str | None = None
    # `| None` ОБЯЗАТЕЛЕН, а не «на всякий случай». Значение по умолчанию срабатывает
    # только когда ключ ОТСУТСТВУЕТ; пришедший явно `null` Pydantic отвергает 422-й.
    # А приходит он всегда: в БД `negative_keywords_json` равен NULL у ВСЕХ тегов,
    # `list_enabled_tags` отдаёт `SELECT child.*` как есть, фронт возвращает то же самое —
    # и сохранение экрана «Теги» падало ЦЕЛИКОМ на каждой попытке (скрин заказчика 13.09:
    # девять строк «Input should be a valid list», input: null). Репозиторий None
    # переваривает сам (`or []`), поэтому достаточно расширить тип.
    keywords_json: list[str] | None = None
    keywords_en_json: list[str] | None = None
    negative_keywords_json: list[str] | None = None
    enabled: bool = True
    sort_order: int = 0


class ProcessRequest(BaseModel):
    article_ids: list[int] | None = None
    limit: int = 5
    offline: bool = False


class ManualArticleImportRequest(BaseModel):
    url: str
    source_id: int | None = None
    process: bool = True
    offline: bool = False


class DigestRequest(BaseModel):
    month: str
    limit: int = 20
    min_score: float = 60
    max_score: float | None = None
    search: str = ""
    top_tag: str = ""


class MonthlyDigestItemIn(BaseModel):
    article_id: int
    section: str | None = None
    editor_note: str | None = None


class MonthlyDigestUpdateRequest(BaseModel):
    title: str | None = None
    status: str = "draft"
    items: list[MonthlyDigestItemIn]


class DigestExportJobRequest(BaseModel):
    month: str = ""
    export_format: str = "pdf"
    limit: int = 100
    min_score: float = 0
    max_score: float | None = None
    search: str = ""
    top_tag: str = ""


class MaintenanceCleanupRequest(BaseModel):
    background_job_days: int | None = None
    export_job_days: int | None = None


class BacklogTaskCreate(BaseModel):
    title: str
    priority: str = "P3"
    status: str = "new"
    details: str | None = None
    due_date: str | None = None


class BacklogTaskPatch(BaseModel):
    status: str | None = None
    due_date: str | None = None


class BacklogTaskCommentCreate(BaseModel):
    text: str


class ExternalWorkerClaimRequest(BaseModel):
    worker_id: str
    queues: list[str] = []
    capabilities: list[str] = []
    max_lease_seconds: int | None = None


class ExternalWorkerLeaseRequest(BaseModel):
    lease_token: str


class ExternalWorkerProgressRequest(ExternalWorkerLeaseRequest):
    progress: float
    lease_seconds: int | None = None


class ExternalWorkerHeartbeatRequest(ExternalWorkerLeaseRequest):
    lease_seconds: int | None = None


class ExternalWorkerCompleteRequest(ExternalWorkerLeaseRequest):
    result: dict[str, Any] = {}


class ExternalWorkerFailRequest(ExternalWorkerLeaseRequest):
    error: str
    retryable: bool = True
    retry_after_seconds: int | None = None


class DigestSocialIn(BaseModel):
    label: str
    accent: str
    text: str


class DigestHeaderBrandingIn(BaseModel):
    brand_text: str
    brand_suffix: str
    department_text: str


class DigestHeroBrandingIn(BaseModel):
    badge: str
    headline: str
    subtitle: str
    image_url: str = ""


class DigestIssueBrandingIn(BaseModel):
    title_template: str
    title_template_with_month: str
    period_label_all: str
    preheader: str
    intro_template: str
    intro_template_with_month: str
    highlights_title: str
    news_title: str
    read_more_label: str
    empty_summary_text: str
    preview_empty_text: str


class DigestFooterBrandingIn(BaseModel):
    contact_text: str
    contact_email: str
    note: str
    socials: list[DigestSocialIn] = []


class DigestHighlightsBrandingIn(BaseModel):
    analytics_source_keywords: list[str] = []
    analytics_category_keywords: list[str] = []
    business_category_keywords: list[str] = []
    cards: list[dict[str, str]] = []


class DigestBrandingIn(BaseModel):
    header: DigestHeaderBrandingIn
    hero: DigestHeroBrandingIn
    issue: DigestIssueBrandingIn
    footer: DigestFooterBrandingIn
    highlights: DigestHighlightsBrandingIn = DigestHighlightsBrandingIn()


class AuthPayload(BaseModel):
    email: str
    password: str


class UserCreate(BaseModel):
    email: str
    password: str
    role: str = "user"


class UserUpdate(BaseModel):
    role: str | None = None
    password: str | None = None


@app.get("/", response_model=None)
def index():
    if os.environ.get("TASKS_APP_MODE") == "1":
        return RedirectResponse("/tasks", status_code=307)
    if (FRONTEND_DIST_DIR / "index.html").exists():
        return FileResponse(FRONTEND_DIST_DIR / "index.html")
    return FileResponse(WEB_DIR / "app.html")


@app.get("/tasks")
@app.get("/tasks/")
def tasks_app() -> FileResponse:
    if (FRONTEND_DIST_DIR / "index.html").exists():
        return FileResponse(FRONTEND_DIST_DIR / "index.html")
    return FileResponse(WEB_DIR / "app.html")


def require_user(session_token: str | None = Cookie(default=None, alias=config.AUTH_COOKIE_NAME)) -> dict[str, Any]:
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = repository.get_user_by_session(session_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Доступ только администратору (настройка тегов/скоринга/источников, пользователи)."""
    if (user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user


def _set_session_cookie(response: Response, session_token: str) -> None:
    response.set_cookie(
        key=config.AUTH_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=config.AUTH_COOKIE_SECURE,
        samesite="lax",
        max_age=config.AUTH_SESSION_DAYS * 24 * 60 * 60,
    )


@app.get("/api/auth/me")
def auth_me(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return {"ok": True, "user": _clean(user)}


@app.post("/api/auth/register")
def auth_register(payload: AuthPayload, response: Response) -> dict[str, Any]:
    email = auth.normalize_email(payload.email)
    if not auth.validate_email(email):
        raise HTTPException(status_code=400, detail="Некорректный email")
    if not auth.validate_password(payload.password):
        raise HTTPException(status_code=400, detail="Пароль должен быть не короче 8 символов")
    try:
        user = repository.create_user(email, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    session_token = repository.create_user_session(int(user["id"]))
    _set_session_cookie(response, session_token)
    return {"ok": True, "user": _clean(user)}


@app.post("/api/auth/login")
def auth_login(payload: AuthPayload, response: Response) -> dict[str, Any]:
    user = repository.authenticate_user(payload.email, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    session_token = repository.create_user_session(int(user["id"]))
    _set_session_cookie(response, session_token)
    return {"ok": True, "user": _clean(user)}


@app.post("/api/auth/logout")
def auth_logout(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=config.AUTH_COOKIE_NAME),
) -> dict[str, Any]:
    if session_token:
        repository.delete_user_session(session_token)
    response.delete_cookie(config.AUTH_COOKIE_NAME)
    return {"ok": True}


@app.get("/api/users")
def list_users_endpoint(user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return {"users": [_clean(u) for u in repository.list_users()]}


@app.post("/api/users")
def create_user_endpoint(payload: UserCreate, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    email = auth.normalize_email(payload.email)
    if not auth.validate_email(email):
        raise HTTPException(status_code=400, detail="Некорректный email")
    if not auth.validate_password(payload.password):
        raise HTTPException(status_code=400, detail="Пароль должен быть не короче 8 символов")
    try:
        created = repository.create_user(email, payload.password, payload.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "user": _clean(created)}


@app.patch("/api/users/{user_id}")
def update_user_endpoint(user_id: int, payload: UserUpdate, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    target = repository.get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if payload.role is not None:
        new_role = payload.role if payload.role in ("admin", "user") else "user"
        if target["role"] == "admin" and new_role != "admin" and repository.count_admins() <= 1:
            raise HTTPException(status_code=400, detail="Нельзя снять роль у последнего администратора")
        repository.set_user_role(user_id, new_role)
    if payload.password is not None:
        if not auth.validate_password(payload.password):
            raise HTTPException(status_code=400, detail="Пароль должен быть не короче 8 символов")
        repository.set_user_password(user_id, payload.password)
    return {"ok": True, "user": _clean(repository.get_user_by_id(user_id))}


@app.delete("/api/users/{user_id}")
def delete_user_endpoint(user_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    target = repository.get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if int(user["id"]) == user_id:
        raise HTTPException(status_code=400, detail="Нельзя удалить собственную учётную запись")
    if target["role"] == "admin" and repository.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="Нельзя удалить последнего администратора")
    repository.delete_user(user_id)
    return {"ok": True}


@app.get("/api/health")
def health() -> dict[str, Any]:
    with get_connection() as conn:
        article_count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    return {"ok": True, "articles": article_count}


@app.get("/api/readiness")
def readiness() -> JSONResponse:
    try:
        payload = readiness_check()
    except Exception as exc:  # noqa: BLE001 - readiness must return a clear 503 payload
        return JSONResponse(
            status_code=503,
            content={"ok": False, "database": {"ok": False}, "error": str(exc)},
        )
    return JSONResponse(status_code=200 if payload["ok"] else 503, content=_clean(payload))


@app.get("/api/articles")
def list_articles(
    search: str | None = None,
    source: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    language: str | None = None,
    min_score: float | None = None,
    max_score: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = Query("score_desc", pattern="^(date_desc|score_desc|score_asc)$"),
    changed_only: bool = False,
    limit: int = Query(1000, ge=1, le=5000),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if search:
        # Ищем по тому, ЧТО ЧЕЛОВЕК ВИДИТ, и по тегу. Раньше было два расхождения:
        # (1) поиск шёл по a.title, а на экране COALESCE(c.title_ru, a.title) — русский
        #     заголовок иностранной статьи поиском НЕ находился;
        # (2) теги в поиск не входили вовсе: набрать «Бурение» и получить статьи этого
        #     направления было нельзя, хотя в сборщике дайджеста такой поиск уже есть
        #     (repository.digest_candidates). Два разных поиска в двух файлах.
        # Требование владельца 12.09: теги влияют и на парсинг, и на выдачу.
        clauses.append(
            "LOWER("
            "COALESCE(c.title_ru, '') || ' ' || a.title || ' ' "
            "|| COALESCE(a.raw_text, '') || ' ' || COALESCE(c.summary, '') || ' ' "
            "|| COALESCE(t.name, '') || ' ' || COALESCE(parent.name, '')"
            ") LIKE %s"
        )
        params.append(f"%{search.lower()}%")
    if source:
        clauses.append("s.name = %s")
        params.append(source)
    if tag:
        clauses.append("(t.name = %s OR parent.name = %s)")
        params.extend([tag, tag])
    if status:
        clauses.append("COALESCE(uas.status, 'new') = %s")
        params.append(status)
    elif not changed_only:
        # Пометка «Шум»/«Дубликат» — это «убрать с глаз», и она обязана убирать.
        # Раньше лента не фильтровала статусы вообще, поэтому помеченное продолжало
        # висеть у того, кто его пометил (замер 24.07: 104 таких статьи в ленте).
        # Фильтр ПЕР-ЮЗЕРНЫЙ: uas приджойнен по текущему пользователю, чужие пометки
        # ничего не скрывают. Явный фильтр по статусу и вкладка «Со статусом»
        # (changed_only) по-прежнему показывают помеченное — иначе его не пересмотреть.
        # `archive` добавлен 12.09: он перестал быть пустым счётчиком и означает
        # «отработано, с глаз долой» — в том числе после снятия статьи из дайджеста.
        clauses.append("COALESCE(uas.status, 'new') NOT IN ('noise', 'duplicate', 'archive')")
    if changed_only:
        clauses.append("COALESCE(uas.status, 'new') <> 'new'")
    if language:
        clauses.append("a.language = %s")
        params.append(language)
    if min_score is not None:
        # Ещё НЕ оценённые статьи (нет строки в article_scores) — это не «низкобалльные»:
        # балла у них попросту нет, и COALESCE(...,0) выдавал бы за 0, отсекая свежий приток
        # порогом. Порог применяем только к УЖЕ оценённым — иначе новые статьи исчезают из
        # ленты до прохода ИИ и она выглядит замороженной. Тот же принцип, что строкой ниже
        # для c.relevant IS NULL: необработанное не прячем.
        clauses.append("(sc.total_score IS NULL OR sc.total_score >= %s)")
        params.append(min_score)
    if max_score is not None:
        clauses.append("COALESCE(sc.total_score, 0) <= %s")
        params.append(max_score)
    if date_from:
        clauses.append("COALESCE(a.published_at::date, a.collected_at::date) >= %s")
        params.append(date_from)
    if date_to:
        clauses.append("COALESCE(a.published_at::date, a.collected_at::date) <= %s")
        params.append(date_to)
    # Скрываем отклонённые гейтом релевантности статьи (relevant=false), как это уже
    # делает дайджест. relevant IS NULL (ещё не проверенные) остаются видны.
    clauses.append("c.relevant IS NOT FALSE")
    # Скрываем помеченные на удаление (recheck --mark): исчезают из ленты, но физически
    # ещё в БД (восстановимы recheck-unmark до recheck-purge).
    clauses.append("NOT a.pending_deletion")
    # Архивный источник уносит с собой свои статьи (требование заказчика 12.09).
    # Именно этого не делало `enabled = FALSE`: сбор прекращался, а накопленный мусор
    # продолжал висеть в ленте у ВСЕХ пользователей — лента джойнит sources без условия.
    clauses.append("s.archived_at IS NULL")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    order_by = {
        "date_desc": "a.published_at DESC NULLS LAST, COALESCE(sc.total_score, 0) DESC, a.id DESC",
        "score_asc": "COALESCE(sc.total_score, 0) ASC, a.published_at DESC NULLS LAST, a.id DESC",
        "score_desc": "COALESCE(sc.total_score, 0) DESC, a.published_at DESC NULLS LAST, a.id DESC",
    }[sort]
    # user_id — первый %s (для LEFT JOIN user_article_states), затем where-параметры, затем limit.
    params.insert(0, int(user["id"]))
    params.append(limit)

    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, COALESCE(c.title_ru, a.title) AS title, a.url, a.language, length(a.raw_text) AS raw_text_chars,
                   a.published_at,
                   a.collected_at, a.text_truncated, s.name AS source_name,
                   COALESCE(c.summary, '') AS summary,
                   COALESCE(uas.status, 'new') AS status,
                   c.relevant, c.relevance_reason,
                   (COALESCE(uas.status, 'new') = 'digest') AS selected_for_digest,
                   sc.total_score, sc.score_label, sc.explanation AS score_explanation,
                   t.name AS tag_name, parent.name AS parent_tag_name,
                   at.confidence AS tag_confidence, at.rationale AS tag_rationale
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN user_article_states uas ON uas.article_id = a.id AND uas.user_id = %s
            LEFT JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_scores sc ON sc.article_id = a.id
            LEFT JOIN article_tags at ON at.article_id = a.id
            LEFT JOIN tags t ON t.id = at.tag_id
            LEFT JOIN tags parent ON parent.id = t.parent_id
            {where}
            ORDER BY {order_by}
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
        score_items = _score_items_by_article(conn, [row["id"] for row in rows])
    payloads = [_article_payload(row) for row in rows]
    for payload in payloads:
        payload["score_items"] = score_items.get(payload["id"], [])
    return payloads


@app.get("/api/stats")
def dashboard_stats(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Authoritative dashboard counters, computed over the full database."""
    return _clean(repository.dashboard_stats(int(user["id"])))


@app.get("/api/backlog")
def backlog_endpoint(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return backlog.read_backlog()


@app.post("/api/backlog/tasks")
def create_backlog_task_endpoint(payload: BacklogTaskCreate, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    try:
        return backlog.create_plan_task(payload.title, priority=payload.priority, status=payload.status, details=payload.details, due_date=payload.due_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/api/backlog/tasks/{task_id}")
def update_backlog_task_endpoint(
    task_id: str,
    payload: BacklogTaskPatch,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        updated: dict[str, Any] | None = None
        fields_set = getattr(payload, "model_fields_set", getattr(payload, "__fields_set__", set()))
        if "status" in fields_set and payload.status is not None:
            updated = backlog.update_task_status(task_id, payload.status)
        if "due_date" in fields_set:
            updated = backlog.update_task_due_date(task_id, payload.due_date)
        if updated is None:
            raise HTTPException(status_code=400, detail="Нет изменений для задачи")
        return updated
    except KeyError:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/backlog/tasks/{task_id}/comments")
def create_backlog_task_comment_endpoint(
    task_id: str,
    payload: BacklogTaskCommentCreate,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return backlog.add_task_comment(task_id, payload.text, str(user.get("email") or "Пользователь"))
    except KeyError:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/api/articles/{article_id}")
def update_article(article_id: int, patch: ArticlePatch, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    # Статус и выбор в дайджест — ПЕР-ЮЗЕРНЫЕ (#12). selected_for_digest сводится к статусу.
    target_status = patch.status
    if target_status is None and patch.selected_for_digest is not None:
        # Снятие из дайджеста ведёт в `archive` (решение владельца 12.09; раньше был
        # `review`, который ничего не делал). `archive` теперь скрывает статью из ленты.
        target_status = "digest" if patch.selected_for_digest else "archive"
    with get_connection() as conn:
        exists = conn.execute("SELECT 1 FROM articles WHERE id = %s", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Article not found")
    previous = repository.get_user_article_status(int(user["id"]), article_id)
    repository.set_user_article_status(
        int(user["id"]), article_id, status=target_status, analyst_comment=patch.analyst_comment
    )
    # Журнал обратной связи. До 12.09 таблица signal_feedback_events была мёртвой:
    # писала в неё ОДНА CLI-команда, которую никто не звал, а боевой путь пометки —
    # вот этот эндпоинт — не писал вовсе. Поэтому на вопрос заказчика «я всё что
    # выделил как шум, он на этом обучился?» честный ответ был «обучаться не на чем».
    # Старый статус читаем ДО записи: в user_article_states одна строка на пару,
    # истории переходов там нет, после UPDATE прежнее значение уже не достать.
    event = {
        "noise": "marked_noise",
        "duplicate": "marked_duplicate",
        "digest": "added_to_digest",
    }.get(target_status or "", "status_changed")
    try:
        if target_status is not None:
            repository.record_signal_feedback_event(
                article_id, event, user_id=int(user["id"]),
                old_value=previous, new_value=target_status,
                comment=patch.analyst_comment,
            )
        elif patch.analyst_comment:
            repository.record_signal_feedback_event(
                article_id, "comment_added", user_id=int(user["id"]),
                comment=patch.analyst_comment,
            )
    except Exception:  # noqa: BLE001
        # Журнал не должен ронять саму пометку: потерять событие — неприятно,
        # не дать человеку пометить статью — хуже.
        logger.exception("не удалось записать событие обратной связи article_id=%s", article_id)
    return {"ok": True}


@app.get("/api/sources")
def list_sources(
    search: str | None = None,
    limit: int = Query(300, ge=1, le=1000),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_sources(search=search, limit=limit)]


@app.get("/api/source-health")
def source_health(
    stale_days: int = Query(3, ge=1, le=30),
    limit: int = Query(500, ge=1, le=1000),
    verdict: str | None = Query(None, pattern="^(ok|stale|no_articles|disabled)$"),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.source_health_report(stale_days=stale_days, limit=limit, verdict=verdict)]


@app.get("/api/source-candidates")
def list_source_candidates(
    status: str | None = None,
    topic: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    user: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_source_candidates(status=status, topic=topic, limit=limit)]


@app.get("/api/source-candidates/triage")
def source_candidate_triage(
    limit: int = Query(20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.source_candidate_triage_report(limit=limit)]


@app.post("/api/source-discovery/discover")
def source_discovery_discover(
    payload: SourceDiscoveryDiscoverRequest,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    topic = payload.topic.strip()
    seed_url = payload.seed_url.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="topic is required")
    if not seed_url:
        raise HTTPException(status_code=400, detail="seed_url is required")
    if payload.limit < 1 or payload.limit > 50:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 50")

    from oiltech_digest.source_discovery.agent import DiscoveryConfig, discover_sources

    result = discover_sources(DiscoveryConfig(
        topic=topic,
        limit=payload.limit,
        seed_urls=(seed_url,),
        offline=payload.offline,
        dry_run=False,
        fetch_inspection=payload.fetch_inspection,
        test_parse=payload.test_parse,
    ))
    repository.record_agent_action(
        None,
        "manual_seed_source_candidate",
        input_payload={**payload.model_dump(), "user_id": int(user["id"])},
        output_payload={
            "topic": topic,
            "seed_url": seed_url,
            "candidates": len(result.get("candidates") or []),
            "candidate_ids": [item.get("id") for item in result.get("candidates") or [] if item.get("id")],
        },
        duration_ms=result.get("duration_ms"),
    )
    return {"ok": True, "result": _clean(result)}


@app.get("/api/source-discovery/plan")
def source_discovery_plan(
    days: int = Query(30, ge=1, le=365),
    target_per_topic: int = Query(10, ge=1, le=100),
    topic_limit: int = Query(5, ge=1, le=50),
    candidate_limit: int = Query(10, ge=1, le=50),
    max_actions: int = Query(5, ge=1, le=50),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    from oiltech_digest.source_discovery.planner import PlannerConfig, build_plan

    return _clean(build_plan(PlannerConfig(
        days=days,
        target_per_topic=target_per_topic,
        topic_limit=topic_limit,
        candidate_limit=candidate_limit,
        max_actions=max_actions,
        persist_memory=False,
        record_action=False,
    )))


@app.post("/api/source-discovery/plan/enqueue")
def enqueue_source_discovery_plan(
    payload: SourceDiscoveryPlanRequest,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    if payload.days < 1 or payload.days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")
    if payload.topic_limit < 1 or payload.topic_limit > 50:
        raise HTTPException(status_code=400, detail="topic_limit must be between 1 and 50")
    if payload.candidate_limit < 1 or payload.candidate_limit > 50:
        raise HTTPException(status_code=400, detail="candidate_limit must be between 1 and 50")
    if payload.max_actions < 1 or payload.max_actions > 50:
        raise HTTPException(status_code=400, detail="max_actions must be between 1 and 50")
    job = background_jobs.enqueue(
        "source_discovery_plan",
        payload.model_dump(),
        user_id=int(user["id"]),
        queue_name="default",
        execution_region="ru",
        capability="source-discovery",
        max_attempts=1,
    )
    return {"ok": True, "job": _job_payload(job)}


@app.post("/api/source-discovery/loop/enqueue")
def enqueue_source_discovery_loop(
    payload: SourceDiscoveryLoopRequest,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    if payload.days < 1 or payload.days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")
    if payload.topic_limit < 1 or payload.topic_limit > 50:
        raise HTTPException(status_code=400, detail="topic_limit must be between 1 and 50")
    if payload.candidate_limit < 1 or payload.candidate_limit > 50:
        raise HTTPException(status_code=400, detail="candidate_limit must be between 1 and 50")
    if payload.max_actions < 1 or payload.max_actions > 50:
        raise HTTPException(status_code=400, detail="max_actions must be between 1 and 50")
    if payload.max_iterations < 1 or payload.max_iterations > 10:
        raise HTTPException(status_code=400, detail="max_iterations must be between 1 and 10")
    if payload.article_limit < 1 or payload.article_limit > 20:
        raise HTTPException(status_code=400, detail="article_limit must be between 1 and 20")
    if payload.max_daily_loop_runs < 0 or payload.max_daily_candidates < 0 or payload.max_daily_evaluations < 0:
        raise HTTPException(status_code=400, detail="daily budget limits must be non-negative")
    job = background_jobs.enqueue(
        "source_discovery_loop",
        payload.model_dump(),
        user_id=int(user["id"]),
        queue_name="default",
        execution_region="ru",
        capability="source-discovery",
        max_attempts=1,
    )
    return {"ok": True, "job": _job_payload(job)}


@app.post("/api/source-discovery/loop/dry-run")
def dry_run_source_discovery_loop(
    payload: SourceDiscoveryLoopRequest,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    if payload.days < 1 or payload.days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")
    if payload.topic_limit < 1 or payload.topic_limit > 50:
        raise HTTPException(status_code=400, detail="topic_limit must be between 1 and 50")
    if payload.candidate_limit < 1 or payload.candidate_limit > 50:
        raise HTTPException(status_code=400, detail="candidate_limit must be between 1 and 50")
    if payload.max_actions < 1 or payload.max_actions > 50:
        raise HTTPException(status_code=400, detail="max_actions must be between 1 and 50")
    if payload.max_iterations < 1 or payload.max_iterations > 10:
        raise HTTPException(status_code=400, detail="max_iterations must be between 1 and 10")
    if payload.article_limit < 1 or payload.article_limit > 20:
        raise HTTPException(status_code=400, detail="article_limit must be between 1 and 20")
    if payload.max_daily_loop_runs < 0 or payload.max_daily_candidates < 0 or payload.max_daily_evaluations < 0:
        raise HTTPException(status_code=400, detail="daily budget limits must be non-negative")

    from oiltech_digest.source_discovery.loop import AgentLoopConfig, run_agent_loop

    result = run_agent_loop(AgentLoopConfig(
        goal=payload.goal,
        days=payload.days,
        target_per_topic=payload.target_per_topic,
        topic_limit=payload.topic_limit,
        candidate_limit=payload.candidate_limit,
        max_actions=payload.max_actions,
        max_iterations=payload.max_iterations,
        offline=payload.offline,
        fetch_inspection=payload.fetch_inspection,
        test_parse=payload.test_parse,
        dry_run=True,
        auto_evaluate=False,
        article_limit=payload.article_limit,
        persist_memory=False,
        max_daily_loop_runs=payload.max_daily_loop_runs,
        max_daily_candidates=payload.max_daily_candidates,
        max_daily_evaluations=payload.max_daily_evaluations,
    ))
    return {"ok": True, "result": _clean(result)}


@app.get("/api/source-discovery/memory")
def source_discovery_memory(
    memory_type: str | None = Query(None),
    status: str | None = Query("active"),
    limit: int = Query(100, ge=1, le=500),
    user: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    normalized_status = status.strip() if isinstance(status, str) and status.strip() else None
    return [_clean(row) for row in repository.list_agent_memory(memory_type=memory_type, status=normalized_status, limit=limit)]


@app.post("/api/source-discovery/memory")
def create_source_discovery_memory(
    payload: AgentMemoryCreate,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    memory_type = payload.memory_type.strip().lower()
    subject = payload.subject.strip()
    if memory_type not in {"topic", "domain", "source", "query", "strategy", "rule"}:
        raise HTTPException(status_code=400, detail="Unknown memory type")
    if payload.status not in {"active", "muted", "rejected"}:
        raise HTTPException(status_code=400, detail="Unknown memory status")
    if not subject:
        raise HTTPException(status_code=400, detail="Memory subject is required")
    normalized_subject = repository.normalize_domain(subject) if memory_type == "domain" else subject
    memory_id = repository.upsert_agent_memory(
        memory_key=f"manual:{memory_type}:{normalized_subject.lower()}",
        memory_type=memory_type,
        subject=normalized_subject,
        status=payload.status,
        score=payload.score,
        facts={**(payload.facts or {}), "manual": True, "created_by_user_id": int(user["id"])},
    )
    repository.record_agent_action(
        None,
        "create_agent_memory",
        input_payload=payload.model_dump(),
        output_payload={"memory_id": memory_id, "memory_type": memory_type, "subject": normalized_subject, "status": payload.status},
    )
    return {"ok": True, "id": memory_id}


@app.patch("/api/source-discovery/memory/{memory_id}")
def patch_source_discovery_memory(
    memory_id: int,
    payload: AgentMemoryPatch,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    if payload.status not in {"active", "muted", "rejected"}:
        raise HTTPException(status_code=400, detail="Unknown memory status")
    if not repository.update_agent_memory_status(memory_id, payload.status):
        raise HTTPException(status_code=404, detail="Agent memory row not found")
    repository.record_agent_action(
        None,
        "update_agent_memory",
        input_payload={"memory_id": memory_id, "status": payload.status},
        output_payload={"ok": True},
    )
    return {"ok": True}


@app.get("/api/source-discovery/actions")
def source_discovery_actions(
    action_type: str | None = Query(None),
    run_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    user: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_agent_actions(action_type=action_type, run_id=run_id, limit=limit)]


@app.get("/api/source-discovery/runs")
def source_discovery_runs(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    user: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_agent_runs(status=status, limit=limit)]


@app.get("/api/source-discovery/quality")
def source_discovery_quality(
    group_by: str = Query("topic"),
    limit: int = Query(20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    if group_by not in {"topic", "domain"}:
        raise HTTPException(status_code=400, detail="group_by must be topic or domain")
    return [_clean(row) for row in repository.source_candidate_quality_report(group_by=group_by, limit=limit)]


@app.get("/api/source-discovery/query-memory")
def source_discovery_query_memory(
    limit: int = Query(20, ge=1, le=100),
    status: str | None = Query("active"),
    user: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    normalized_status = status.strip() if isinstance(status, str) and status.strip() else None
    return [_clean(row) for row in repository.query_memory_report(status=normalized_status, limit=limit)]


@app.get("/api/source-discovery/evaluation")
def source_discovery_evaluation(
    limit: int = Query(500, ge=1, le=2000),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    candidates = repository.list_source_candidates(limit=limit)
    source_memory = repository.list_agent_memory(memory_type="source", status=None, limit=limit)
    actions = repository.list_agent_actions(limit=min(limit, 500))
    return _clean(_source_discovery_evaluation_report(candidates, source_memory, actions))


@app.get("/api/source-discovery/readiness")
def source_discovery_readiness_endpoint(
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    from oiltech_digest.source_discovery.readiness import source_discovery_readiness

    return _clean(source_discovery_readiness())


@app.get("/api/source-candidates/{candidate_id}/articles")
def list_source_candidate_articles(
    candidate_id: int,
    limit: int = Query(20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    if repository.get_source_candidate(candidate_id) is None:
        raise HTTPException(status_code=404, detail="Source candidate not found")
    return [_clean(row) for row in repository.list_source_candidate_articles(candidate_id, limit=limit)]


@app.patch("/api/source-candidates/{candidate_id}")
def patch_source_candidate(
    candidate_id: int,
    payload: SourceCandidatePatch,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    if payload.status is not None and payload.status not in repository.SOURCE_CANDIDATE_STATUSES:
        raise HTTPException(status_code=400, detail="Unknown source candidate status")
    if payload.recommended_action is not None and payload.recommended_action not in repository.SOURCE_CANDIDATE_ACTIONS:
        raise HTTPException(status_code=400, detail="Unknown source candidate recommended_action")
    if repository.get_source_candidate(candidate_id) is None:
        raise HTTPException(status_code=404, detail="Source candidate not found")
    repository.update_source_candidate_assessment(
        candidate_id,
        status=payload.status,
        recommended_action=payload.recommended_action,
        review_comment=payload.review_comment,
    )
    learning = None
    if payload.status in {"approved", "rejected", "paused"} or payload.recommended_action == "reject":
        learning = apply_candidate_learning(
            candidate_id,
            event_type="operator_update",
            status=payload.status,
            recommended_action=payload.recommended_action,
            review_comment=payload.review_comment,
        )
    repository.record_agent_action(
        None,
        "update_source_candidate",
        input_payload={"candidate_id": candidate_id, **payload.model_dump(exclude_none=True)},
        output_payload={"ok": True, "learning": learning},
    )
    return {"ok": True}


@app.post("/api/source-candidates/{candidate_id}/evaluate")
def evaluate_source_candidate(
    candidate_id: int,
    payload: SourceCandidateEvaluateRequest,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    from oiltech_digest.source_discovery.sandbox import evaluate_source_candidate as run_evaluation

    if payload.article_limit < 1 or payload.article_limit > 20:
        raise HTTPException(status_code=400, detail="article_limit must be between 1 and 20")
    try:
        result = run_evaluation(
            candidate_id,
            article_limit=payload.article_limit,
            offline=payload.offline,
            collect=payload.collect,
            process=payload.process,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _clean(result)


@app.post("/api/source-candidates/{candidate_id}/approve")
def approve_source_candidate(
    candidate_id: int,
    payload: SourceCandidateApproveRequest,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    if payload.parse_strategy is not None and payload.parse_strategy not in {"rss", "request", "playwright"}:
        raise HTTPException(status_code=400, detail="parse_strategy must be rss, request or playwright")
    if payload.network_region not in {"auto", "ru", "external"}:
        raise HTTPException(status_code=400, detail="network_region must be auto, ru or external")
    try:
        source_id = repository.approve_source_candidate(
            candidate_id,
            name=payload.name,
            source_type=payload.source_type,
            parse_strategy=payload.parse_strategy,
            enabled=payload.enabled,
            category=payload.category,
            priority=payload.priority,
            network_region=payload.network_region,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    initial_job = None
    if payload.scrape_after_approve:
        source = repository.get_source(source_id)
        strategy = source.get("parse_strategy") if source else None
        if source and strategy in {"request", "playwright"}:
            decision = network_policy.route_source_task(source, task_kind="scrape")
            initial_job = background_jobs.enqueue(
                "scrape_source",
                {"source_id": source_id, "reason": "approved_source_candidate", "candidate_id": candidate_id},
                user_id=int(user["id"]),
                queue_name=decision.queue_name,
                execution_region=decision.execution_region,
                capability=decision.capability,
                max_attempts=1,
            )
        elif source and strategy == "rss":
            initial_job = background_jobs.enqueue(
                "parse_source_once",
                {"source_id": source_id, "reason": "approved_source_candidate", "candidate_id": candidate_id},
                user_id=int(user["id"]),
                queue_name="default",
                execution_region="ru",
                capability="rss_parse",
                max_attempts=1,
            )
    repository.record_agent_action(
        None,
        "approve_source_candidate",
        input_payload={"candidate_id": candidate_id, **payload.model_dump()},
        output_payload={"source_id": source_id, "initial_job_id": int(initial_job["id"]) if initial_job else None},
    )
    apply_candidate_learning(
        candidate_id,
        event_type="approved",
        status="approved",
        recommended_action="add",
        source_id=source_id,
    )
    return {"ok": True, "source_id": source_id, "initial_job": _job_payload(initial_job) if initial_job else None}


@app.post("/api/sources")
def create_source(payload: SourceCreate, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    # Пользователь вставляет просто ссылку на источник — система сама ищет RSS-ленту.
    # Нашла → parse_strategy='rss' с найденным фидом; не нашла → 'request' (скрейп
    # страницы новостей). RSS можно передать и явно (тогда discover пропускается).
    site_url = (payload.url or payload.rss_url or "").strip()
    rss_url = (payload.rss_url or "").strip()
    parse_strategy = "rss"
    if not rss_url and site_url:
        from oiltech_digest.ingestion.rss_discovery import discover_feed
        found = discover_feed(site_url)
        if found:
            rss_url = found
        else:
            parse_strategy = "request"
    source_id = repository.add_rss_source(
        name=payload.name,
        rss_url=rss_url,
        url=site_url or rss_url,
        priority=payload.priority,
        category=payload.category,
        update_frequency=payload.update_frequency,
        parse_strategy=parse_strategy,
    )
    return {"ok": True, "id": source_id, "rss_url": rss_url or None, "parse_strategy": parse_strategy}


@app.post("/api/articles/import")
def import_article_by_url(
    payload: ManualArticleImportRequest,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        imported = import_manual_article(payload.url, explicit_source_id=payload.source_id)
    except ManualImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    response: dict[str, Any] = {
        "ok": True,
        "article": {
            "id": imported.article_id,
            "source_id": imported.source_id,
            "source_name": imported.source_name,
            "duplicate": imported.duplicate,
            "title": imported.title,
            "fetch_method": imported.fetch_method,
            "full_text_status": imported.full_text_status,
            "full_text_method": imported.full_text_method,
            "full_text_chars": imported.full_text_chars,
        },
    }
    if not payload.process:
        return response

    decision = network_policy.route_ai_processing()
    job = background_jobs.enqueue(
        "process_articles",
        {
            "article_ids": [imported.article_id],
            "limit": 1,
            "offline": bool(payload.offline),
        },
        user_id=int(user["id"]),
        queue_name=decision.queue_name,
        execution_region=decision.execution_region,
        capability=decision.capability,
    )
    response["job"] = _job_payload(job)
    return response


@app.patch("/api/sources/{source_id}")
def update_source(source_id: int, patch: SourcePatch, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    updates = patch.model_dump(exclude_unset=True)
    if not updates:
        return {"ok": True}
    allowed = {
        "enabled",
        "url",
        "rss_url",
        "parse_strategy",
        "update_frequency",
        "listing_url",
        "listing_strategy",
        "listing_selector",
        "article_link_selector",
        "article_date_selector",
        "network_region",
        "network_profile",
    }
    fields = [key for key in updates if key in allowed]
    if not fields:
        return {"ok": True}
    values = [updates[field] for field in fields]
    set_clause = ", ".join(f"{field} = %s" for field in fields)
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE sources SET {set_clause}, updated_at = now() WHERE id = %s RETURNING id",
            [*values, source_id],
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Source not found")
        conn.commit()
    return {"ok": True}


@app.post("/api/sources/{source_id}/archive")
def archive_source(source_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Убрать источник из работы: перестать опрашивать И убрать его статьи из ленты.

    Требование заказчика 12.09 («выключаем источник — не парсится больше, уходит в архив,
    его статьи уходят из выборки»). Жёсткого DELETE намеренно нет: articles.source_id
    ссылается на sources БЕЗ ON DELETE, поэтому удаление источника со статьями Postgres
    просто отклонит, а каскад уничтожил бы корпус и историю ИИ-затрат. Архив обратим.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE sources SET archived_at = now(), enabled = FALSE, updated_at = now() "
            "WHERE id = %s RETURNING id, name",
            (source_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Source not found")
        conn.commit()
    return {"ok": True, "id": row[0], "name": row[1], "archived": True}


@app.post("/api/sources/{source_id}/unarchive")
def unarchive_source(source_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Вернуть источник из архива. Включать сбор НЕ начинаем — это отдельное решение."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE sources SET archived_at = NULL, updated_at = now() WHERE id = %s RETURNING id, name",
            (source_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Source not found")
        conn.commit()
    return {"ok": True, "id": row[0], "name": row[1], "archived": False}


@app.post("/api/sources/{source_id}/scrape")
def scrape_source(
    source_id: int,
    background: bool = False,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    source = repository.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    strategy = source.get("parse_strategy")
    if strategy not in {"request", "playwright"}:
        raise HTTPException(status_code=400, detail="Скраппер доступен только для request/playwright-источников")
    if background:
        decision = network_policy.route_source_task(source, task_kind="scrape")
        job = background_jobs.enqueue(
            "scrape_source",
            {"source_id": source_id},
            user_id=int(user["id"]),
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        return {"ok": True, "job": _job_payload(job)}
    stats = playwright_parser.parse_source(source) if strategy == "playwright" else request_parser.parse_source(source)
    return {"ok": True, "stats": _clean(stats)}


@app.get("/api/sources/{source_id}/diagnose")
def diagnose_source_endpoint(
    source_id: int,
    limit: int = Query(5, ge=1, le=20),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    source = repository.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return _clean(diagnose_source(source, limit=limit))


@app.post("/api/sources/{source_id}/diagnose")
def diagnose_source_with_overrides(
    source_id: int,
    patch: SourcePatch,
    limit: int = Query(5, ge=1, le=20),
    background: bool = False,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    source = repository.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    overrides = patch.model_dump(exclude_unset=True)
    if background:
        decision = network_policy.route_source_task({**source, **overrides}, task_kind="diagnose")
        job = background_jobs.enqueue(
            "diagnose_source",
            {"source_id": source_id, "overrides": overrides, "limit": limit},
            user_id=int(user["id"]),
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        return {"ok": True, "job": _job_payload(job)}
    return _clean(diagnose_source({**source, **overrides}, limit=limit))


@app.get("/api/tags")
def list_tags(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_enabled_tags()]


@app.put("/api/tags")
def save_tags(items: list[TagIn], user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        result = repository.save_tags([i.model_dump() for i in items])
    except ValueError as exc:
        # Разорванная связь родитель-подтег — не 500, а внятный отказ: сохранение целиком
        # отменяется, дерево остаётся прежним.
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **result}


@app.delete("/api/tags/{tag_id}")
def delete_tag(tag_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        repository.delete_tag(tag_id)
    except ValueError as exc:
        # Служебный тег-приёмник удалить нельзя — объясняем, а не 500.
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.get("/api/scoring-criteria")
def list_scoring_criteria(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_enabled_scoring_criteria()]


@app.put("/api/scoring-criteria")
def save_scoring_criteria(items: list[ScoringCriterionIn], user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        result = repository.save_scoring_criteria([i.model_dump() for i in items])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **result}


@app.delete("/api/scoring-criteria/{criterion_id}")
def delete_scoring_criterion(criterion_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        repository.delete_scoring_criterion(criterion_id)
    except ValueError as exc:
        # Удаление, ломающее сумму весов, обрушило бы всю стадию скоринга.
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


class FeedbackIn(BaseModel):
    article_id: int | None = None
    source_id: int | None = None
    reason: str | None = None
    usefulness: int | None = None
    translation: int | None = None
    source_quality: int | None = None
    comment: str | None = None


@app.get("/api/feedback/reasons")
def feedback_reasons(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, str]]:
    """Словарь быстрых причин. Фронт не хранит свою копию — иначе списки разойдутся,
    как уже разошлись четыре независимых списка статусов статьи."""
    # Формулировки заказчика (13.09): «пару моментов, чтобы придать более официальный
    # статус платформы». Платформа выходит на корпоративный портал ГПН, и разговорный
    # тон («Уже было», «Годный сигнал») там неуместен.
    labels = {
        "off_topic": "Не соответствует тематике",
        "incomplete_text": "Неполный материал",
        "duplicate": "Повторный сигнал",
        "bad_translation": "Некорректный перевод",
        "bad_source": "Низкое качество источника",
        "good": "Ценный сигнал",
        "other": "Другое",
    }
    return [{"value": value, "label": labels[value]} for value in repository.FEEDBACK_REASONS]


@app.get("/api/feedback")
def get_feedback(article_id: int | None = None, source_id: int | None = None,
                 user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if article_id is None and source_id is None:
        raise HTTPException(status_code=400, detail="Нужен article_id или source_id")
    entry = repository.get_feedback_entry(int(user["id"]), article_id=article_id, source_id=source_id)
    return {"ok": True, "entry": _clean(entry) if entry else None}


@app.post("/api/feedback")
def save_feedback(payload: FeedbackIn, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Оставить ОС по сигналу или по источнику: оценки 1–5, быстрая причина, комментарий.

    Пер-юзерная: это мнение конкретного человека, а не общий факт. Свод по источникам
    (`/api/feedback/sources`) собирает их вместе — там и появляется общая картина.
    """
    try:
        entry = repository.save_feedback_entry(
            int(user["id"]),
            article_id=payload.article_id,
            source_id=payload.source_id,
            reason=payload.reason,
            usefulness=payload.usefulness,
            translation=payload.translation,
            source_quality=payload.source_quality,
            comment=(payload.comment or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "entry": _clean(entry)}


@app.get("/api/feedback/sources")
def feedback_sources(limit: int = Query(300, ge=1, le=1000),
                     user: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    """Свод оценок по источникам — на что люди жалуются системно."""
    return [_clean(row) for row in repository.feedback_source_summary(limit=limit)]


@app.get("/api/feedback/training-set")
def feedback_training_set(limit: int = Query(500, ge=1, le=2000), reason: str | None = None,
                          user: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    """Накопленная ОС в виде, пригодном для обучения агентов.

    Самодостаточна намеренно: ИИ на проде считается на внешнем воркере БЕЗ доступа к БД,
    поэтому выборка везёт с собой заголовок, суть и имя источника.
    """
    return [_clean(row) for row in repository.feedback_training_set(limit=limit, reason=reason)]


@app.get("/api/reports/ai-cost")
def ai_cost(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.ai_cost_report()]


@app.get("/api/reports/ai-article-cost")
def ai_article_cost(
    limit: int = Query(20, ge=1, le=200),
    include_partial: bool = False,
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.ai_article_cost_report(limit=limit, complete_only=not include_partial)]


@app.post("/api/signals/topics/seed")
def seed_signal_topics(user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    from oiltech_digest.signal_discovery import seed_default_radar_topics

    return {"ok": True, "topics": seed_default_radar_topics()}


@app.get("/api/signals")
def list_signals(
    maturity: str | None = Query(None, pattern="^(watch|shortlist|proven|reject)$"),
    theme: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    evidence_limit: int = Query(3, ge=0, le=20),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    rows = []
    for row in repository.list_signals(maturity=maturity, theme=theme, limit=limit, user_id=int(user["id"])):
        evidence = repository.list_signal_evidence(int(row["id"]), limit=evidence_limit) if evidence_limit else []
        rows.append(_clean({**row, "evidence": evidence}))
    return rows


@app.patch("/api/signals/{signal_id}")
def update_signal(signal_id: int, patch: SignalPatch, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    target_status = patch.status
    if target_status is None and patch.selected_for_digest is not None:
        target_status = "digest" if patch.selected_for_digest else "watch"
    if target_status is not None and target_status not in {"watch", "digest", "archive", "noise", "duplicate"}:
        raise HTTPException(status_code=400, detail="Unknown signal status")
    try:
        repository.set_user_signal_status(
            int(user["id"]),
            signal_id,
            status=target_status,
            analyst_comment=patch.analyst_comment,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Signal not found")
    if target_status == "digest":
        repository.record_signal_feedback_event(
            None,
            "added_to_digest",
            signal_id=signal_id,
            user_id=int(user["id"]),
            comment=patch.analyst_comment,
        )
    return {"ok": True}


@app.post("/api/signals/feedback")
def create_signal_feedback(payload: SignalFeedbackCreate, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    from oiltech_digest.signal_feedback import store_signal_feedback

    if not any([
        payload.comment.strip(),
        (payload.verdict or "").strip(),
        (payload.reason or "").strip(),
        (payload.corrected_title or "").strip(),
        (payload.corrected_thesis or "").strip(),
        payload.duplicate_of_signal_id,
    ]):
        raise HTTPException(status_code=400, detail="comment or structured feedback is required")
    if payload.article_id is None and payload.signal_id is None and not payload.source_url:
        raise HTTPException(status_code=400, detail="article_id, signal_id or source_url is required")
    result = store_signal_feedback(
        {
            "article_id": payload.article_id,
            "signal_id": payload.signal_id,
            "signal_evidence_id": payload.signal_evidence_id,
            "source_url": payload.source_url,
            "signal_title": payload.signal_title,
            "source": payload.source,
            "comment": payload.comment,
            "verdict": payload.verdict,
            "reason": payload.reason,
            "corrected_title": payload.corrected_title,
            "corrected_thesis": payload.corrected_thesis,
            "duplicate_of_signal_id": payload.duplicate_of_signal_id,
        },
        user_id=int(user["id"]),
    )
    return {"ok": True, **result}


@app.get("/api/signals/memory")
def signal_memory(
    memory_type: str | None = Query(None),
    status: str | None = Query("active"),
    limit: int = Query(100, ge=1, le=500),
    user: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    from oiltech_digest.signal_feedback import FEEDBACK_MEMORY_TYPES

    if memory_type and memory_type not in FEEDBACK_MEMORY_TYPES:
        raise HTTPException(status_code=400, detail="Unknown signal memory type")
    normalized_status = status.strip() if isinstance(status, str) and status.strip() else None
    return [_clean(row) for row in repository.list_signal_agent_memory(memory_type=memory_type, status=normalized_status, limit=limit)]


@app.post("/api/jobs/signal-discovery")
def enqueue_signal_discovery(payload: SignalDiscoveryRequest, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    if payload.days < 1 or payload.days > 90:
        raise HTTPException(status_code=400, detail="days must be between 1 and 90")
    if payload.limit < 1 or payload.limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    if payload.max_signals < 1 or payload.max_signals > 50:
        raise HTTPException(status_code=400, detail="max_signals must be between 1 and 50")
    # Не-offline прогон радара зовёт OpenAI, поэтому маршрут берём у политики:
    # прямой вызов с РФ-адреса возвращает 403 unsupported_country_region_territory.
    decision = network_policy.route_ai_processing()
    job = background_jobs.enqueue(
        "signal_discovery",
        payload.model_dump(),
        user_id=int(user["id"]),
        queue_name=decision.queue_name if not payload.offline else "default",
        execution_region=decision.execution_region if not payload.offline else "ru",
        capability=decision.capability if not payload.offline else None,
    )
    return {"ok": True, "job": _job_payload(job)}


@app.get("/api/digest-content")
def digest_content(month: str = "", limit: int = Query(100, ge=1, le=500),
                   min_score: float = 0,
                   max_score: float | None = None,
                   search: str = "",
                   top_tag: str = "",
                   user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return _clean(
        build_digest_content(
            month=month,
            limit=limit,
            min_score=min_score,
            max_score=max_score,
            search=search.strip() or None,
            top_tag=top_tag.strip() or None,
            user_id=int(user["id"]),
        )
    )


@app.get("/api/digest-branding")
def digest_branding(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return _clean(get_digest_branding())


@app.put("/api/digest-branding")
def update_digest_branding(payload: DigestBrandingIn, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return {"ok": True, "branding": _clean(save_digest_branding(payload.model_dump()))}


@app.post("/api/monthly-digests")
def create_monthly_digest(payload: DigestRequest, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return _clean(
        save_digest_draft(
            month=payload.month,
            limit=payload.limit,
            min_score=payload.min_score,
            max_score=payload.max_score,
            search=payload.search.strip() or None,
            top_tag=payload.top_tag.strip() or None,
            user_id=int(user["id"]),
        )
    )


@app.get("/api/monthly-digests/{month}")
def get_monthly_digest(month: str, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    digest = repository.get_monthly_digest(month, user_id=int(user["id"]))
    if digest is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return _clean(digest)


@app.put("/api/monthly-digests/{month}")
def update_monthly_digest(month: str, payload: MonthlyDigestUpdateRequest, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    saved = repository.save_monthly_digest(
        month=month,
        title=payload.title or f"Нефтесервисный дайджест · {month}",
        items=[item.model_dump() for item in payload.items],
        status=payload.status,
        user_id=int(user["id"]),
    )
    return _clean(saved)


@app.get("/api/digest-email", response_class=HTMLResponse)
def digest_email(month: str = "", limit: int = Query(100, ge=1, le=500),
                 min_score: float = 0,
                 max_score: float | None = None,
                 search: str = "",
                 top_tag: str = "",
                 user: dict[str, Any] = Depends(require_user)) -> HTMLResponse:
    content = build_digest_content(
        month=month,
        limit=limit,
        min_score=min_score,
        max_score=max_score,
        search=search.strip() or None,
        top_tag=top_tag.strip() or None,
        user_id=int(user["id"]),
    )
    return HTMLResponse(render_digest_email(content))


@app.get("/api/jobs")
def list_jobs(
    status: str | None = Query(None, pattern="^(queued|running|finalizing|ok|failed)$"),
    kind: str | None = None,
    queue_name: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    user_id = None if (user.get("role") or "user") == "admin" else int(user["id"])
    return [
        _job_payload(row)
        for row in repository.list_background_jobs(
            status=status,
            kind=kind,
            queue_name=queue_name,
            user_id=user_id,
            limit=limit,
        )
    ]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    job = _get_scoped_background_job(job_id, user)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_payload(job)


@app.get("/api/jobs/{job_id}/download")
def download_job_result(job_id: int, user: dict[str, Any] = Depends(require_user)) -> FileResponse:
    job = _get_scoped_background_job(job_id, user)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "ok":
        raise HTTPException(status_code=409, detail="Job is not finished")
    path = background_jobs.job_download_path(job)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Job result file not found")
    result = job.get("result_json") or {}
    return FileResponse(
        str(path),
        media_type=result.get("media_type") or "application/octet-stream",
        filename=result.get("filename") or path.name,
    )


@app.post("/api/jobs/digest-export")
def enqueue_digest_export(payload: DigestExportJobRequest, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if payload.export_format not in {"pdf", "doc", "docx", "html", "json"}:
        raise HTTPException(status_code=400, detail="Unsupported export format")
    if payload.limit < 1 or payload.limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    decision = network_policy.route_digest_export(payload.export_format)
    job = background_jobs.enqueue(
        "digest_export",
        {**payload.model_dump(), "user_id": int(user["id"])},  # дайджест пер-юзерный (#12)
        user_id=int(user["id"]),
        queue_name=decision.queue_name,
        execution_region=decision.execution_region,
        capability=decision.capability,
    )
    return {"ok": True, "job": _job_payload(job)}


@app.post("/api/jobs/process")
def enqueue_process_articles(payload: ProcessRequest, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    if payload.limit < 1 or payload.limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    decision = network_policy.route_ai_processing()
    job = background_jobs.enqueue(
        "process_articles",
        payload.model_dump(),
        user_id=int(user["id"]),
        queue_name=decision.queue_name,
        execution_region=decision.execution_region,
        capability=decision.capability,
    )
    return {"ok": True, "job": _job_payload(job)}


def require_external_worker(authorization: str | None = Header(default=None)) -> None:
    if not config.EXTERNAL_WORKER_TOKEN_HASH:
        raise HTTPException(status_code=503, detail="External worker auth is not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="External worker token required")
    if not hmac.compare_digest(_sha256_hex(token), config.EXTERNAL_WORKER_TOKEN_HASH):
        raise HTTPException(status_code=401, detail="Invalid external worker token")


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lease_seconds(value: int | None) -> int:
    requested = value or config.EXTERNAL_WORKER_DEFAULT_LEASE_SECONDS
    return min(max(int(requested), 30), 3600)


@app.post("/api/external-worker/claim")
def external_worker_claim(
    payload: ExternalWorkerClaimRequest,
    _: None = Depends(require_external_worker),
) -> dict[str, Any]:
    repository.requeue_expired_external_leases()
    lease_token = secrets.token_urlsafe(32)
    job = repository.claim_external_background_job(
        queue_names=payload.queues,
        capabilities=payload.capabilities,
        worker_id=payload.worker_id,
        lease_token_hash=_sha256_hex(lease_token),
        lease_seconds=_lease_seconds(payload.max_lease_seconds),
    )
    if job is None:
        return {"job": None}
    return {"job": {**_job_payload(job), "payload": _external_worker_payload(job), "lease_token": lease_token}}


@app.post("/api/external-worker/jobs/{job_id}/progress")
def external_worker_progress(
    job_id: int,
    payload: ExternalWorkerProgressRequest,
    _: None = Depends(require_external_worker),
) -> dict[str, Any]:
    if payload.progress < 0 or payload.progress > 100:
        raise HTTPException(status_code=400, detail="progress must be between 0 and 100")
    ok = repository.update_external_background_job_progress(
        job_id,
        lease_token_hash=_sha256_hex(payload.lease_token),
        progress=payload.progress,
        lease_seconds=_lease_seconds(payload.lease_seconds) if payload.lease_seconds is not None else None,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="Job lease is not active")
    return {"ok": True}


@app.post("/api/external-worker/jobs/{job_id}/heartbeat")
def external_worker_heartbeat(
    job_id: int,
    payload: ExternalWorkerHeartbeatRequest,
    _: None = Depends(require_external_worker),
) -> dict[str, Any]:
    ok = repository.heartbeat_external_background_job(
        job_id,
        lease_token_hash=_sha256_hex(payload.lease_token),
        lease_seconds=_lease_seconds(payload.lease_seconds),
    )
    if not ok:
        raise HTTPException(status_code=409, detail="Job lease is not active")
    return {"ok": True}


@app.post("/api/external-worker/jobs/{job_id}/complete")
def external_worker_complete(
    job_id: int,
    payload: ExternalWorkerCompleteRequest,
    _: None = Depends(require_external_worker),
) -> dict[str, Any]:
    result = payload.result
    job = repository.get_background_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    lease_token_hash = _sha256_hex(payload.lease_token)
    # Баг T2 (двойной AI-расход): застолбить завершение АТОМАРНО до применения результата.
    # Пока идёт apply (запись карточек/скоринга + биллинг ai_processing_runs), задача в статусе
    # 'finalizing', и requeue_expired_external_leases (только status='running') её НЕ переотдаст —
    # значит другой воркер не прогонит AI повторно. Лиз истёк/переотдан → 409, ничего не применяем.
    if not repository.begin_external_background_job_finalize(job_id, lease_token_hash=lease_token_hash):
        raise HTTPException(status_code=409, detail="Job lease is not active")
    try:
        if job.get("kind") == "process_articles" and result.get("external_ai"):
            result = {**result, "applied": external_ai.apply_process_result(result, job_id=job_id)}
        if job.get("kind") == "recheck_relevance" and result.get("recheck_relevance"):
            # ИМЕННО payload_json: job приходит из get_background_job (SELECT *), поэтому ключи —
            # это колонки таблицы (schema.sql:304). Ключа "payload" в строке НЕТ, и чтение его
            # молча давало {} → mark/dry_run/force всегда False → recheck удалял статьи ФИЗИЧЕСКИ
            # вопреки запрошенному мягкому режиму (баг T3, так уже потеряли ~2000 статей).
            job_payload = job.get("payload_json") or {}
            force = bool(job_payload.get("force", False))
            dry_run = bool(job_payload.get("dry_run", False))
            mark = bool(job_payload.get("mark", False))
            result = {**result, "applied": external_ai.apply_recheck_result(result, force=force, dry_run=dry_run, mark=mark, job_id=job_id)}
        if job.get("kind") == "translate_titles" and result.get("translate_titles"):
            result = {**result, "applied": external_ai.apply_translate_result(result, job_id=job_id)}
        if job.get("kind") == "source_candidate_evaluate" and result.get("source_candidate_evaluate"):
            result = {**result, "applied": external_ai.apply_source_candidate_result(result, job_id=job_id)}
        if job.get("kind") == "process_document" and result.get("process_document"):
            applied = documents_external.apply_document_result(result, job_id=job_id)
            # Конверт ЗАМЕНЯЕТСЯ вычищенным, а не дополняется: result_json уходит клиенту
            # через /api/jobs, и админ читает задачи любого пользователя. Карточка и факты
            # уже применены в таблицы документов, где проверяется владелец.
            result = {**documents_external.scrub_result(result), "applied": applied}
        if job.get("kind") == "scrape_source" and result.get("external_fetch"):
            result = {**result, "applied": external_fetch.apply_scrape_result(result)}
    except Exception:
        # apply упал — снять 'finalizing', чтобы задача не залипла (вернётся в очередь по лизу/stale)
        repository.release_external_background_job_finalize(job_id, lease_token_hash=lease_token_hash)
        raise
    ok = repository.finish_external_background_job(
        job_id,
        lease_token_hash=lease_token_hash,
        result=result,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="Job lease is not active")
    return {"ok": True}


@app.post("/api/external-worker/jobs/{job_id}/fail")
def external_worker_fail(
    job_id: int,
    payload: ExternalWorkerFailRequest,
    _: None = Depends(require_external_worker),
) -> dict[str, Any]:
    ok = repository.fail_external_background_job(
        job_id,
        lease_token_hash=_sha256_hex(payload.lease_token),
        error_message=payload.error,
        retryable=payload.retryable,
        retry_delay_seconds=payload.retry_after_seconds,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="Job lease is not active")
    return {"ok": True}


@app.get("/api/stats/monthly")
def monthly_stats(
    months: int = Query(6, ge=1, le=24),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Месячные результаты платформы + активность ВСЕХ пользователей.

    ADMIN-ONLY по решению владельца: раздел сводный, показывает работу каждого
    пользователя и общий итог. Гейт стоит здесь, на API, а не только во фронте —
    аудит изоляции 24.07 показал, что фронтовый гейт без серверного (брендинг,
    maintenance) означает, что любой залогиненный получает данные запросом в обход UI.
    """
    return _clean(
        {
            "months": months,
            "platform": repository.monthly_platform_stats(months),
            "ai_cost": repository.monthly_ai_cost(months),
            "activity": repository.monthly_user_activity(months, user_id=None),
            "activity_scope": "all",
        }
    )


@app.get("/api/maintenance/status")
def get_maintenance_status(user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return _clean(maintenance_status())


@app.post("/api/maintenance/cleanup")
def run_maintenance_cleanup(
    payload: MaintenanceCleanupRequest,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    if payload.background_job_days is not None and payload.background_job_days < 1:
        raise HTTPException(status_code=400, detail="background_job_days must be >= 1")
    if payload.export_job_days is not None and payload.export_job_days < 1:
        raise HTTPException(status_code=400, detail="export_job_days must be >= 1")
    return {"ok": True, "result": _clean(maintenance_cleanup(**payload.model_dump()))}


@app.get("/api/maintenance/benchmark")
def get_maintenance_benchmark(
    iterations: int = Query(3, ge=1, le=10),
    articles_limit: int = Query(200, ge=1, le=2000),
    source_limit: int = Query(150, ge=1, le=1000),
    jobs_limit: int = Query(100, ge=1, le=1000),
    digest_limit: int = Query(100, ge=1, le=500),
    min_score: float = 0,
    warn_ms: float = Query(800, gt=0, le=10_000),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return _clean(
        run_readiness_benchmark(
            iterations=iterations,
            articles_limit=articles_limit,
            source_limit=source_limit,
            jobs_limit=jobs_limit,
            digest_limit=digest_limit,
            min_score=min_score,
            warn_ms=warn_ms,
        )
    )


@app.get("/api/digest-export")
def digest_export(
    month: str = "",
    export_format: str = Query("pdf", pattern="^(pdf|docx?|html|json)$"),
    limit: int = Query(100, ge=1, le=500),
    min_score: float = 0,
    max_score: float | None = None,
    search: str = "",
    top_tag: str = "",
    user: dict[str, Any] = Depends(require_user),
) -> FileResponse:
    job_id = repository.create_export_job("monthly_digest", export_format)
    try:
        result = write_digest_export(
            month=month,
            export_format=export_format,
            limit=limit,
            min_score=min_score,
            max_score=max_score,
            search=search.strip() or None,
            top_tag=top_tag.strip() or None,
            user_id=int(user["id"]),
        )
        repository.finish_export_job(job_id, "ok", result["path"])
    except RuntimeError as exc:  # PDF без Chromium и т.п. — понятное сообщение, не 500
        repository.finish_export_job(job_id, "failed", error_message=str(exc))
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        repository.finish_export_job(job_id, "failed", error_message=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))
    return FileResponse(
        result["path"],
        media_type=result["media_type"],
        filename=result["filename"],
    )


@app.post("/api/process")
def process_articles(payload: ProcessRequest, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    client = make_client(payload.offline)
    if payload.article_ids:
        articles = repository.get_articles_by_ids(payload.article_ids, include_summary=True)
    else:
        articles = repository.get_articles_needing_pipeline(payload.limit)
    stats = process_pipeline_articles(articles, client, fetch_full=True)
    return {"pipeline": stats}


def _source_discovery_evaluation_report(
    candidates: list[dict[str, Any]],
    source_memory: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_eval = _candidate_recommendation_eval(candidates)
    source_eval = _source_audit_eval(source_memory)
    recent_agent_actions = _recent_agent_action_eval(actions)
    weak_rules = [
        item for item in source_eval["rules"]
        if item["confidence_low"] or item["suppressed"] or item["avg_source_score"] < -40
    ][:8]
    return {
        "summary": {
            "candidates": candidate_eval["total"],
            "candidate_decisions": candidate_eval["decided"],
            "candidate_agreement_rate": candidate_eval["agreement_rate"],
            "source_memories": source_eval["total"],
            "sources_under_watch": source_eval["under_watch"],
            "high_confidence_sources": source_eval["confidence"].get("high", 0),
            "weak_rules": len(weak_rules),
            "recent_actions": recent_agent_actions["total"],
        },
        "candidate_recommendations": candidate_eval,
        "source_audit": source_eval,
        "weak_rules": weak_rules,
        "recent_actions": recent_agent_actions,
    }


def _candidate_recommendation_eval(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(candidates)
    decided = 0
    agreements = 0
    disagreements = 0
    by_recommendation: dict[str, dict[str, Any]] = {}
    disagreements_examples: list[dict[str, Any]] = []
    for row in candidates:
        recommendation = str(row.get("recommended_action") or "unknown")
        status = str(row.get("status") or "unknown")
        bucket = by_recommendation.setdefault(recommendation, {
            "recommendation": recommendation,
            "total": 0,
            "decided": 0,
            "agreed": 0,
            "disagreed": 0,
            "agreement_rate": 0.0,
        })
        bucket["total"] += 1
        agreement = _candidate_agreement(status, recommendation)
        if agreement is None:
            continue
        decided += 1
        bucket["decided"] += 1
        if agreement:
            agreements += 1
            bucket["agreed"] += 1
        else:
            disagreements += 1
            bucket["disagreed"] += 1
            if len(disagreements_examples) < 8:
                disagreements_examples.append({
                    "candidate_id": row.get("id"),
                    "name": row.get("name") or row.get("normalized_domain") or row.get("url"),
                    "status": status,
                    "recommended_action": recommendation,
                    "topic": row.get("topic"),
                    "avg_score": row.get("avg_score"),
                })
    rows = []
    for bucket in by_recommendation.values():
        if bucket["decided"]:
            bucket["agreement_rate"] = round(bucket["agreed"] / bucket["decided"], 3)
        rows.append(bucket)
    rows.sort(key=lambda item: (item["decided"], item["total"]), reverse=True)
    return {
        "total": total,
        "decided": decided,
        "agreed": agreements,
        "disagreed": disagreements,
        "agreement_rate": round(agreements / decided, 3) if decided else 0.0,
        "by_recommendation": rows,
        "disagreements": disagreements_examples,
    }


def _candidate_agreement(status: str, recommendation: str) -> bool | None:
    if status not in {"approved", "rejected", "paused"}:
        return None
    if recommendation == "add":
        return status == "approved"
    if recommendation == "reject":
        return status == "rejected"
    if recommendation == "test_more":
        return status in {"approved", "paused"}
    if recommendation == "human_review":
        return True
    return None


def _source_audit_eval(source_memory: list[dict[str, Any]]) -> dict[str, Any]:
    by_problem: dict[str, dict[str, Any]] = {}
    by_recommendation: dict[str, dict[str, Any]] = {}
    confidence = {"low": 0, "medium": 0, "high": 0}
    severity = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    rule_stats: dict[str, dict[str, Any]] = {}
    under_watch = 0
    examples = []
    for row in source_memory:
        facts = row.get("facts_json") or {}
        recommendation = str(facts.get("recommendation") or "keep")
        problem_type = str(facts.get("problem_type") or facts.get("status") or "stable")
        confidence_key = str(facts.get("confidence") or "medium")
        severity_key = str(facts.get("severity") or "low")
        score = float(row.get("score") or 0)
        confidence[confidence_key] = confidence.get(confidence_key, 0) + 1
        severity[severity_key] = severity.get(severity_key, 0) + 1
        if recommendation != "keep":
            under_watch += 1
        problem_bucket = by_problem.setdefault(problem_type, {"problem_type": problem_type, "count": 0, "avg_source_score": 0.0})
        problem_bucket["count"] += 1
        problem_bucket["avg_source_score"] += score
        rec_bucket = by_recommendation.setdefault(recommendation, {
            "recommendation": recommendation,
            "label": str(facts.get("recommendation_label") or recommendation),
            "count": 0,
            "avg_source_score": 0.0,
        })
        rec_bucket["count"] += 1
        rec_bucket["avg_source_score"] += score
        decision_log = facts.get("decision_log") if isinstance(facts.get("decision_log"), dict) else {}
        for rule in decision_log.get("triggered_rules") or []:
            if isinstance(rule, dict):
                _bump_rule(rule_stats, str(rule.get("rule") or "unknown"), "triggered", score, confidence_key)
        for rule in decision_log.get("suppressed_rules") or []:
            if isinstance(rule, dict):
                _bump_rule(rule_stats, str(rule.get("rule") or "unknown"), "suppressed", score, confidence_key)
        if recommendation != "keep" and len(examples) < 8:
            examples.append({
                "source_id": facts.get("source_id"),
                "source_name": row.get("subject"),
                "problem_type": problem_type,
                "severity": severity_key,
                "confidence": confidence_key,
                "recommendation": recommendation,
                "recommendation_label": facts.get("recommendation_label") or recommendation,
                "score": score,
            })
    problems = _average_count_rows(by_problem.values())
    recommendations = _average_count_rows(by_recommendation.values())
    rules = []
    for row in rule_stats.values():
        total = max(int(row["triggered"]) + int(row["suppressed"]), 1)
        row["suppression_rate"] = round(int(row["suppressed"]) / total, 3)
        row["avg_source_score"] = round(float(row["avg_source_score"]) / total, 2)
        rules.append(row)
    rules.sort(key=lambda item: (item["suppressed"], item["confidence_low"], -item["avg_source_score"]), reverse=True)
    return {
        "total": len(source_memory),
        "under_watch": under_watch,
        "confidence": confidence,
        "severity": severity,
        "problems": problems,
        "recommendations": recommendations,
        "rules": rules,
        "examples": examples,
    }


def _bump_rule(rule_stats: dict[str, dict[str, Any]], rule: str, field: str, score: float, confidence: str) -> None:
    bucket = rule_stats.setdefault(rule, {
        "rule": rule,
        "triggered": 0,
        "suppressed": 0,
        "confidence_low": 0,
        "avg_source_score": 0.0,
        "suppression_rate": 0.0,
    })
    bucket[field] += 1
    if confidence == "low":
        bucket["confidence_low"] += 1
    bucket["avg_source_score"] += score


def _average_count_rows(rows: Any) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        count = max(int(row.get("count") or 0), 1)
        item = dict(row)
        item["avg_source_score"] = round(float(item.get("avg_source_score") or 0) / count, 2)
        result.append(item)
    result.sort(key=lambda item: item.get("count", 0), reverse=True)
    return result


def _recent_agent_action_eval(actions: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    learning_events = 0
    for row in actions:
        action_type = str(row.get("action_type") or "unknown")
        by_type[action_type] = by_type.get(action_type, 0) + 1
        if action_type == "source_candidate_learning":
            learning_events += 1
    return {
        "total": len(actions),
        "learning_events": learning_events,
        "by_type": [
            {"action_type": key, "count": value}
            for key, value in sorted(by_type.items(), key=lambda item: item[1], reverse=True)
        ],
    }


def _job_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "queue": row.get("queue_name") or "default",
        "execution_region": row.get("execution_region") or "ru",
        "capability": row.get("capability"),
        "agent_run_id": row.get("agent_run_id"),
        "status": row["status"],
        "progress": float(row.get("progress") or 0),
        "attempts": int(row.get("attempts") or 0),
        "max_attempts": int(row.get("max_attempts") or 0),
        "payload": _clean(row.get("payload_json") or {}),
        "result": _clean(row.get("result_json") or {}),
        "error": row.get("error_message"),
        "run_after": _clean(row.get("run_after")),
        "created_at": _clean(row.get("created_at")),
        "started_at": _clean(row.get("started_at")),
        "finished_at": _clean(row.get("finished_at")),
    }


def _get_scoped_background_job(job_id: int, user: dict[str, Any]) -> dict[str, Any] | None:
    if (user.get("role") or "user") == "admin":
        return repository.get_background_job(job_id)
    return repository.get_background_job(job_id, user_id=int(user["id"]))


def _external_worker_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row.get("payload_json") or {})
    if row.get("kind") == "process_articles" and row.get("queue_name") == "external-ai":
        return _clean(external_ai.build_process_articles_payload(payload))
    if row.get("kind") == "recheck_relevance" and row.get("queue_name") == "external-ai":
        return _clean(external_ai.build_recheck_payload(payload))
    if row.get("kind") == "translate_titles" and row.get("queue_name") == "external-ai":
        return _clean(external_ai.build_translate_payload(payload))
    if row.get("kind") == "source_candidate_evaluate" and row.get("queue_name") == "external-ai":
        return _clean(external_ai.build_source_candidate_evaluate_payload(payload))
    if row.get("kind") == "process_document" and row.get("queue_name") == "external-ai":
        return _clean(documents_external.build_document_payload(payload))
    if row.get("kind") == "scrape_source" and str(row.get("queue_name") or "").startswith("external-"):
        return _clean(external_fetch.build_scrape_source_payload(int(payload["source_id"]), payload))
    return _clean(payload)


def _score_items_by_article(conn, article_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Per-criterion scoring breakdown grouped by article id."""
    if not article_ids:
        return {}
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT s.article_id, sc.name, sc.weight, asi.final_score, asi.ai_score,
               asi.keyword_score, asi.rationale
        FROM article_score_items asi
        JOIN article_scores s ON s.id = asi.article_score_id
        JOIN scoring_criteria sc ON sc.id = asi.criterion_id
        WHERE s.article_id = ANY(%s)
        ORDER BY sc.sort_order, sc.id
        """,
        (article_ids,),
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in cur.fetchall():
        grouped.setdefault(int(row["article_id"]), []).append(
            {
                "name": row["name"],
                "weight": float(row["weight"]) if row["weight"] is not None else 0.0,
                "final_score": float(row["final_score"]) if row["final_score"] is not None else 0.0,
                "ai_score": float(row["ai_score"]) if row["ai_score"] is not None else None,
                "keyword_score": float(row["keyword_score"]) if row["keyword_score"] is not None else None,
                "rationale": row["rationale"],
            }
        )
    return grouped


def _article_payload(row: dict[str, Any]) -> dict[str, Any]:
    tag = row.get("tag_name") or "Без тега"
    if row.get("parent_tag_name"):
        tag = f"{row['parent_tag_name']} / {tag}"
    # Эмодзи снимаем НА ВЫДАЧЕ, а не при вставке (решение владельца 12.09:
    # «чтобы в любой части сигнала — название, суть и так далее — не было эмодзи»).
    # Этот сериализатор — единственный шов, через который лента, поиск и карточка
    # получают текст, поэтому чистка здесь накрывает весь пользовательский показ.
    # Исходники в БД не трогаем: content_hash остаётся прежним, бэкфилл не нужен,
    # решение обратимо. Экспорт дайджеста идёт мимо — он чистится в processing/digest.py.
    return {
        "id": row["id"],
        "title": normalize.strip_emoji(row["title"]),
        "url": row["url"],
        "source": row["source_name"],
        "language": row.get("language"),
        "date": _date(row.get("published_at") or row.get("collected_at")),
        "published_at": _date(row.get("published_at")),
        "collected": _date(row.get("collected_at")),
        "future_date": normalize.is_future_date(row.get("published_at")),
        "summary": normalize.strip_emoji(row.get("summary")),
        "tag": tag,
        "score": float(row["total_score"]) if row.get("total_score") is not None else 0,
        "rating": row.get("score_label") or "Без оценки",
        "status": row.get("status") or "new",
        "digest": (row.get("status") or "new") == "digest",
        "tag_confidence": float(row["tag_confidence"]) if row.get("tag_confidence") is not None else None,
        "tag_rationale": row.get("tag_rationale"),
        "score_explanation": row.get("score_explanation"),
        "raw_text_chars": int(row.get("raw_text_chars") or 0),
        "text_truncated": bool(row.get("text_truncated")),
        "relevant": row.get("relevant"),
        "relevance_reason": row.get("relevance_reason"),
    }


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Json):
        return value.obj
    return value


def _date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


# =========================================================================
# Приём файлов: загрузка документа, карточка, оригинал
# =========================================================================
# Разбор идёт ИНЛАЙНОМ в этом процессе, а не фоновой задачей, и это осознанное
# отступление от спеки под срок. Основание — замер: pypdf на 299-страничном документе
# держит пик 35 МБ и 5,5 секунды, а размер файла ограничен 25 МБ. Фоновая задача
# потребовала бы локального обработчика, а тип без него не откладывается, а гибнет:
# локальный воркер заберёт задачу и терминально завалит её как неизвестную.
# Долг записан в тикете; при переезде на более мощный сервер разбор уезжает в воркер.

ATTESTATION_TEXT = (
    "Подтверждаю, что вправе загрузить этот материал и передать его в обработку, "
    "и что мне известно: его текст покидает контур для разбора моделью."
)


def _documents_dir() -> Path:
    directory = Path(config.UPLOAD_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@app.post("/api/documents")
async def upload_document(
    file: UploadFile = File(...),
    attested: bool = Form(False),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    if not config.UPLOAD_DOCS_ENABLED:
        raise HTTPException(status_code=503, detail="Приём файлов временно выключен")
    # Внешний контур выключен → отклоняем НА ВХОДЕ. Поставленная задача без внешнего
    # исполнителя не откладывается, а гибнет: её заберёт локальный воркер и завалит.
    if network_policy.route_ai_processing().execution_region != "external":
        raise HTTPException(status_code=503, detail="Внешний контур обработки недоступен — загрузка отключена")
    if not attested:
        raise HTTPException(status_code=400, detail="Требуется подтверждение права на загрузку материала")

    data = await file.read()
    if len(data) > config.UPLOAD_MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Файл больше {config.UPLOAD_MAX_FILE_BYTES // 1024 // 1024} МБ",
        )
    if not data:
        raise HTTPException(status_code=400, detail="Пустой файл")

    try:
        kind = doc_parsing.detect_kind(data)
    except DocumentError as exc:
        raise HTTPException(status_code=415, detail=str(exc))

    digest = hashlib.sha256(data).hexdigest()
    existing = documents_repo.document_by_hash(int(user["id"]), digest)
    if existing is not None:
        return {"ok": True, "duplicate": True, "document": _document_brief(existing)}

    try:
        parsed = doc_parsing.parse_document(data, kind)
    except DocumentError as exc:
        # Скан, пароль, повреждение — понятный отказ, а не пустая карточка. Разбор
        # документа без текстового слоя дал бы уверенную сводку из ничего.
        raise HTTPException(status_code=422, detail=str(exc))

    # Имя пользователя НЕ участвует в пути: только сгенерированное.
    storage_path = _documents_dir() / f"{digest}.{kind}"
    storage_path.write_bytes(data)

    document_id = documents_repo.create_document(
        owner_user_id=int(user["id"]),
        filename=(file.filename or "документ")[:300],
        storage_path=str(storage_path),
        kind=kind,
        size_bytes=len(data),
        content_sha256=digest,
        attestation_text=ATTESTATION_TEXT,
    )
    documents_repo.save_anchors(
        document_id, [(a.number, a.text) for a in parsed.anchors], parsed.anchor_unit
    )

    decision = network_policy.route_ai_processing()
    job = repository.create_background_job(
        "process_document",
        {"document_id": document_id},
        user_id=int(user["id"]),  # ИМЕННО поле, а не payload: видимость задач читает колонку
        queue_name=decision.queue_name,
        execution_region=decision.execution_region,
        capability=decision.capability,
    )
    documents_repo.set_status(document_id, "processing")

    document = documents_repo.get_document(document_id, int(user["id"]))
    return {"ok": True, "duplicate": False, "document": _document_brief(document), "job_id": job["id"]}


@app.get("/api/documents")
def list_documents(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    rows = documents_repo.list_documents(int(user["id"]))
    return {"ok": True, "documents": [_document_brief(row) for row in rows]}


@app.get("/api/documents/{document_id}")
def get_document(document_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    # Владелец проверяется В ЗАПРОСЕ, а не отдельной проверкой существования: копирование
    # статейного образца (там проверяют только существование, потому что статьи общие)
    # отдало бы чужой документ.
    document = documents_repo.get_document(document_id, int(user["id"]))
    if document is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    card = documents_repo.get_card(document_id)
    facts = documents_repo.get_facts(document_id)
    return {
        "ok": True,
        "document": _document_brief(document),
        "card": _clean(card) if card else None,
        "facts": [_clean(f) for f in facts],
    }


@app.get("/api/documents/{document_id}/original")
def download_document(document_id: int, user: dict[str, Any] = Depends(require_user)):
    document = documents_repo.get_document(document_id, int(user["id"]))
    if document is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    path = Path(document["storage_path"]).resolve()
    # Проверка принадлежности каталогу. Существующая выдача файлов её НЕ делает —
    # копировать тот образец нельзя.
    if not str(path).startswith(str(_documents_dir().resolve())) or not path.exists():
        raise HTTPException(status_code=404, detail="Файл недоступен")
    return FileResponse(path, filename=document["filename"])


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    storage_path = documents_repo.delete_document(document_id, int(user["id"]))
    if storage_path is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    # Файловая система транзакцией не покрывается: строка уже удалена, файл сносим следом.
    try:
        Path(storage_path).unlink(missing_ok=True)
    except OSError:
        logger.warning("не удалось удалить файл документа %s", storage_path)
    return {"ok": True}


def _document_brief(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    return _clean({
        "id": row.get("id"),
        "filename": row.get("filename"),
        "kind": row.get("kind"),
        "size_bytes": row.get("size_bytes"),
        "status": row.get("status"),
        "error_message": row.get("error_message"),
        "anchor_unit": row.get("anchor_unit"),
        "anchor_count": row.get("anchor_count"),
        # Сколько якорей без текста. На реальном заключении экспертизы это 67 страниц
        # из 107 — пользователь обязан видеть, что часть документа не прочитана,
        # иначе полнота карточки выглядит выше, чем она есть.
        "empty_anchors": row.get("empty_anchors"),
        "text_chars": row.get("text_chars"),
        "essence": row.get("essence"),
        "doc_type": row.get("doc_type"),
        "publisher": row.get("publisher"),
        "fact_count": row.get("fact_count"),
        "created_at": row.get("created_at"),
    })
