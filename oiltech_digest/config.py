"""Конфигурация: пути, строка подключения к БД, константы парсера."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Корень репозитория (на уровень выше пакета oiltech_digest/)
REPO_ROOT = Path(__file__).resolve().parents[1]

# Загружаем .env из корня репозитория (если есть)
load_dotenv(REPO_ROOT / ".env")

# --- База данных ---
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://oiltech:oiltech_local_dev@localhost:5432/oiltech_digest",
)

# --- Данные-источники ---
SOURCES_XLSX = REPO_ROOT / "data" / "seed" / "1_Список_источников_для_дайджеста.xlsx"
DIRECTIONS_XLSX = REPO_ROOT / "data" / "seed" / "2_Направления_и_ключевые_слова.xlsx"
SOURCES_SHEET = "Sources_Expanded"
EXPORTS_DIR = REPO_ROOT / "exports"

# --- Параметры HTTP / парсинга ---
REQUEST_TIMEOUT = 20          # сек на один HTTP-запрос
RSS_PROBE_TIMEOUT = int(os.environ.get("RSS_PROBE_TIMEOUT", "4"))
MAX_WORKERS = 10              # параллелизм при обходе лент / автообнаружении
RETRY_ATTEMPTS = 3            # попыток HTTP с экспоненциальным backoff
RETRY_BACKOFF_BASE = 1.0      # базовая задержка backoff (1с, 2с, 4с)
HTTP_MIN_INTERVAL_SECONDS = float(os.environ.get("HTTP_MIN_INTERVAL_SECONDS", "1.5"))
HTTP_JITTER_SECONDS = float(os.environ.get("HTTP_JITTER_SECONDS", "0.4"))
HTTP_BLOCK_COOLDOWN_SECONDS = int(os.environ.get("HTTP_BLOCK_COOLDOWN_SECONDS", "900"))
REQUEST_ARTICLE_LIMIT = int(os.environ.get("REQUEST_ARTICLE_LIMIT", "6"))
# Минимум значимого текста для первичной вставки request/playwright-статей.
# Корпоративные новости и press release бывают короткими; старый порог 200 символов
# отбрасывал часть релевантных заметок ещё до AI-гейта.
MIN_ARTICLE_TEXT_CHARS = int(os.environ.get("MIN_ARTICLE_TEXT_CHARS", "120"))
# Минимум для записи дозагруженного full-text. Ownership guard ниже защищает от
# листингов/пейволов, поэтому порог можно держать ниже прежних 800 и не терять
# короткие, но полноценные материалы.
MIN_FULL_TEXT_CHARS = int(os.environ.get("MIN_FULL_TEXT_CHARS", "500"))
BACKGROUND_JOB_WORKERS = int(os.environ.get("BACKGROUND_JOB_WORKERS", "2"))
BACKGROUND_JOB_INLINE = os.environ.get("BACKGROUND_JOB_INLINE", "1").lower() not in {"0", "false", "no"}
BACKGROUND_JOB_POLL_SECONDS = float(os.environ.get("BACKGROUND_JOB_POLL_SECONDS", "2"))
BACKGROUND_JOB_STALE_MINUTES = int(os.environ.get("BACKGROUND_JOB_STALE_MINUTES", "60"))
# Залипший 'finalizing' (применение результата внешней задачи, баг T2) восстанавливаем по
# своему, более короткому таймауту: apply длится секунды, а 60 мин — слишком долго. Благодаря
# идемпотентности биллинга (H1) даже ложная переотдача не двоит счёт, поэтому таймаут безопасен.
FINALIZE_STALE_MINUTES = int(os.environ.get("FINALIZE_STALE_MINUTES", "10"))
BACKGROUND_JOB_RETENTION_DAYS = int(os.environ.get("BACKGROUND_JOB_RETENTION_DAYS", "30"))
BACKGROUND_JOB_QUEUES = [
    item.strip()
    for item in os.environ.get("BACKGROUND_JOB_QUEUES", "default").split(",")
    if item.strip()
]
BACKGROUND_JOB_RETRY_BASE_SECONDS = int(os.environ.get("BACKGROUND_JOB_RETRY_BASE_SECONDS", "30"))
EXPORT_JOB_RETENTION_DAYS = int(os.environ.get("EXPORT_JOB_RETENTION_DAYS", "30"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# --- Геораспределенное исполнение ---
# По умолчанию внешний контур выключен: routing helper сохраняет старые локальные
# очереди, чтобы обновление кода не остановило текущий single-server deployment.
EXTERNAL_WORKERS_ENABLED = os.environ.get("EXTERNAL_WORKERS_ENABLED", "0").lower() in {"1", "true", "yes"}
AI_EXECUTION_REGION = os.environ.get("AI_EXECUTION_REGION", "ru").strip().lower()
FETCH_EXTERNAL_ENABLED = os.environ.get("FETCH_EXTERNAL_ENABLED", "0").lower() in {"1", "true", "yes"}
EXTERNAL_WORKER_TOKEN_HASH = os.environ.get("EXTERNAL_WORKER_TOKEN_HASH", "").strip()
EXTERNAL_WORKER_DEFAULT_LEASE_SECONDS = int(os.environ.get("EXTERNAL_WORKER_DEFAULT_LEASE_SECONDS", "600"))
CORE_API_URL = os.environ.get("CORE_API_URL", "").strip().rstrip("/")
EXTERNAL_WORKER_TOKEN = os.environ.get("EXTERNAL_WORKER_TOKEN", "").strip()
EXTERNAL_WORKER_ID = os.environ.get("EXTERNAL_WORKER_ID", "external-worker-1").strip()
EXTERNAL_WORKER_QUEUES = [
    item.strip()
    for item in os.environ.get("EXTERNAL_WORKER_QUEUES", "external-ai").split(",")
    if item.strip()
]
EXTERNAL_WORKER_CAPABILITIES = [
    item.strip()
    for item in os.environ.get("EXTERNAL_WORKER_CAPABILITIES", "openai").split(",")
    if item.strip()
]
EXTERNAL_WORKER_POLL_SECONDS = float(os.environ.get("EXTERNAL_WORKER_POLL_SECONDS", "3"))

# --- Прокси для парсинга (residential, напр. 2captcha) ---
# PROXY_URL — полная строка подключения: "http://user:pass@host:port"
# (у 2captcha HTTP/HTTPS-прокси с авторизацией порт обычно 8080).
# Если переменная задана, ВЕСЬ парсинг (RSS discovery, ленты, full-text статей)
# идёт через прокси. Пусто (по умолчанию) — прямые запросы, как при локальной
# разработке. OpenAI через прокси НЕ ходит (у него отдельный клиент) — намеренно:
# чтобы не жечь платный трафик и не ловить блок OpenAI за подозрительный IP.
PROXY_URL = os.environ.get("PROXY_URL", "").strip()

# Таймаут (сек) для запросов через прокси: residential заметно медленнее прямого,
# поэтому при активном прокси берём max(обычный таймаут, PROXY_TIMEOUT).
PROXY_TIMEOUT = int(os.environ.get("PROXY_TIMEOUT", "40"))

def _parse_proxy_host_overrides(raw: str) -> dict[str, str]:
    """Parse 'host=proxy_url,host2=proxy_url2' from env into a suffix map."""
    overrides: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        if not chunk.strip() or "=" not in chunk:
            continue
        host, proxy_url = chunk.split("=", 1)
        host = host.strip().lower().lstrip(".")
        proxy_url = proxy_url.strip()
        if host and proxy_url:
            overrides[host] = proxy_url
    return overrides


# Карта "домен → строка прокси". Совпавший суффикс хоста имеет приоритет
# над PROXY_URL: например, override для "rbc.ru" сработает и для "www.rbc.ru".
PROXY_HOST_OVERRIDES: dict[str, str] = _parse_proxy_host_overrides(
    os.environ.get("PROXY_HOST_OVERRIDES", "")
)

# --- OpenAI / AI processing ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-nano")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_TIMEOUT = int(os.environ.get("OPENAI_TIMEOUT", "60"))
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "minimal")

# Гейт релевантности — критическая защита от мусора в выборке. Ему можно дать
# модель сильнее основной, но reasoning по умолчанию минимальный: ответ строго JSON,
# и при medium/high Responses API иногда тратит весь output budget на рассуждение.
# Если переменные не заданы — откат на основную модель с надежным коротким ответом.
# ВАЖНО (инцидент 2026-06): эти override'ы НЕ в git — прописывать в .env воркера,
# где реально вызывается OpenAI, иначе гейт тихо откатится на слабую модель.
OPENAI_RELEVANCE_MODEL = os.environ.get("OPENAI_RELEVANCE_MODEL", "").strip() or OPENAI_MODEL
OPENAI_RELEVANCE_REASONING = os.environ.get("OPENAI_RELEVANCE_REASONING", "").strip() or "minimal"

# Переводчик заголовков — отдельная стадия. Ответ короткий (один заголовок), поэтому
# по умолчанию хватает основной (дешёвой) модели и минимального reasoning. Можно
# переопределить в .env воркера, если потребуется качество посильнее.
OPENAI_TRANSLATE_MODEL = os.environ.get("OPENAI_TRANSLATE_MODEL", "").strip() or OPENAI_MODEL
OPENAI_TRANSLATE_REASONING = os.environ.get("OPENAI_TRANSLATE_REASONING", "").strip() or OPENAI_REASONING_EFFORT

# Скоринг (бизнес-эффект и пр. критерии) — балл напрямую определяет отбор в дайджест.
# Раньше скоринг ТИХО ехал на основной OPENAI_MODEL с минимальным reasoning: при смене
# основной модели на слабую/быструю (или откате на nano) балл систематически проседал
# (инцидент 2026-06: средний total_score ~30, до 65+ дотягивали единицы). Даём скорингу
# СВОЮ модель и НЕ minimal reasoning по умолчанию (minimal даёт терсые, заниженные баллы).
# Если override не задан — основная модель, но reasoning по умолчанию medium, а не minimal.
OPENAI_SCORE_MODEL = os.environ.get("OPENAI_SCORE_MODEL", "").strip() or OPENAI_MODEL
OPENAI_SCORE_REASONING = os.environ.get("OPENAI_SCORE_REASONING", "").strip() or "medium"

# Модель разбора ДОКУМЕНТОВ. Своя переменная, и намеренно БЕЗ отката на OPENAI_MODEL.
# Пер-стадийные переменные читаются из окружения того процесса, который реально зовёт
# модель, — это внешний воркер, чей .env не в git. Тихий откат на основную модель уже
# случался (инцидент 2026-06): дефолт — самая дешёвая gpt-5-nano, она ЕСТЬ в таблице
# ставок, поэтому счёт бы сошёлся, а корпус разобрала бы слабейшая модель, и никто
# не заметил бы. Пусто → стадия падает с явной ошибкой, а не работает молча.
OPENAI_DOC_MODEL = os.environ.get("OPENAI_DOC_MODEL", "").strip() or None
OPENAI_DOC_REASONING = os.environ.get("OPENAI_DOC_REASONING", "").strip() or "medium"

# Приём файлов: переключатель и границы. Лимиты подтверждены владельцем 20.08.
UPLOAD_DOCS_ENABLED = os.environ.get("UPLOAD_DOCS_ENABLED", "true").strip().lower() in ("1", "true", "yes")
UPLOAD_MAX_FILE_BYTES = int(os.environ.get("UPLOAD_MAX_FILE_BYTES", str(25 * 1024 * 1024)))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "").strip() or "/data/documents"

# USD per 1M tokens. Fallback-ставки, если модель не найдена в таблице ниже.
# Дефолт (0.05/0.40) — это прайс gpt-5-nano; для конкретных моделей берётся
# OPENAI_MODEL_PRICES, иначе экран «AI-затраты» занижает стоимость в разы.
OPENAI_INPUT_USD_PER_MTOK = float(os.environ.get("OPENAI_INPUT_USD_PER_MTOK", "0.05"))
OPENAI_OUTPUT_USD_PER_MTOK = float(os.environ.get("OPENAI_OUTPUT_USD_PER_MTOK", "0.40"))

# Пер-модельные ставки USD за 1М токенов (input, output). РАНЬШЕ cost_usd считался
# ЕДИНЫМ прайсом (ставкой nano) для всех моделей → gpt-5-mini занижался ~15×,
# gpt-5.5 ~100× (тех-долг T4). Матчинг по префиксу имени: в БД модель хранится
# с датой-суффиксом ("gpt-5.5-2026-04-23"), поэтому сравниваем startswith, и более
# длинный/специфичный префикс выигрывает ("gpt-5-mini" раньше гипотетического "gpt-5").
# Ставки сверены с прайс-листом OpenAI 21.08.2026 (developers.openai.com/api/docs/pricing).
# Тогда же вскрылись две ошибки: gpt-5-mini стоял как 0.75/4.5 при публикуемых 0.25/2.00
# (счёт завышался втрое), а gpt-5.4-mini и gpt-5.4-nano отсутствовали вовсе — и по
# префиксному матчингу попадали бы под "gpt-5.4" со ставкой 2.5/15, то есть mini
# считался бы втрое дороже себя. Более длинный префикс выигрывает, поэтому обе
# записи обязаны стоять здесь явно.
OPENAI_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4": (2.5, 15.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.40),
}

# --- Source discovery ---
# По умолчанию внешний поиск выключен: MVP можно гонять через --seed-url без ключей.
# Поддержанные провайдеры: none / brave / serpapi.
SOURCE_DISCOVERY_SEARCH_PROVIDER = os.environ.get("SOURCE_DISCOVERY_SEARCH_PROVIDER", "none").strip().lower()
SOURCE_DISCOVERY_SEARCH_TIMEOUT = int(os.environ.get("SOURCE_DISCOVERY_SEARCH_TIMEOUT", "20"))
# Агент поиска источников должен отсеивать старые архивы и разделы без живого
# потока. Порог намеренно отдельный от основного парсинга: тут мы оцениваем новый
# источник, а не историческую догрузку уже принятого источника.
SOURCE_DISCOVERY_FRESHNESS_DAYS = int(os.environ.get("SOURCE_DISCOVERY_FRESHNESS_DAYS", "180"))
SOURCE_DISCOVERY_STALE_RESULT_YEAR_GRACE = int(os.environ.get("SOURCE_DISCOVERY_STALE_RESULT_YEAR_GRACE", "1"))
BRAVE_SEARCH_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
SERPAPI_API_KEY = os.environ.get("SERPAPI_API_KEY", "").strip()

# --- Signal discovery ---
# Ежедневный радар сигналов ставится scheduler'ом в очередь один раз за окно.
# Дефолты намеренно web-only и не-offline: это новый агент поиска сигналов, который
# ищет гибко по web/китайским запросам, а не только по уже заведённым sources.
SIGNAL_DISCOVERY_DAILY_ENABLED = os.environ.get("SIGNAL_DISCOVERY_DAILY_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SIGNAL_DISCOVERY_DAYS = int(os.environ.get("SIGNAL_DISCOVERY_DAYS", "14"))
SIGNAL_DISCOVERY_LIMIT = int(os.environ.get("SIGNAL_DISCOVERY_LIMIT", "120"))
SIGNAL_DISCOVERY_MIN_SCORE = float(os.environ.get("SIGNAL_DISCOVERY_MIN_SCORE", "40"))
# MAX_SIGNALS — сколько кластеров КАЖДОЙ темы уходит судье (а не сколько сигналов вернуть):
# 13 тем × 6 = до 78 вызовов модели на прогон. Прежние 20 на 21 тему давали до 420.
SIGNAL_DISCOVERY_MAX_SIGNALS = int(os.environ.get("SIGNAL_DISCOVERY_MAX_SIGNALS", "6"))
# Запросов к поиску на тему: 13 × 4 = 52 на прогон, ~1600 в месяц при ежедневном запуске.
SIGNAL_DISCOVERY_WEB_QUERY_LIMIT = int(os.environ.get("SIGNAL_DISCOVERY_WEB_QUERY_LIMIT", "4"))
# Раунды research-loop: 1 = только широкий поиск, 2 = широкий поиск + уточнение по найденным зацепкам.
SIGNAL_DISCOVERY_RESEARCH_ROUNDS = int(os.environ.get("SIGNAL_DISCOVERY_RESEARCH_ROUNDS", "2"))
# Сколько web-результатов на тему докачивать целиком (страница, а не сниппет поисковика)
# перед кластеризацией и судьёй. 0 отключает докачку и оставляет только сниппеты.
SIGNAL_DISCOVERY_WEB_FULLTEXT_LIMIT = int(os.environ.get("SIGNAL_DISCOVERY_WEB_FULLTEXT_LIMIT", "20"))
# Откуда брать темы радара: tags — корневые тематики заказчика (пункт 12), table —
# таблица signal_radar_topics (прежние 21 тема из сида).
SIGNAL_RADAR_TOPIC_SOURCE = os.environ.get("SIGNAL_RADAR_TOPIC_SOURCE", "tags").strip().lower()
# Сколько пар «одно ли событие» судит дедуп радара за прогон (signal_dedup). Едет
# воркеру в снимке задачи — менять можно без пересборки NL.
SIGNAL_DEDUP_MAX_PAIRS = int(os.environ.get("SIGNAL_DEDUP_MAX_PAIRS", "400"))


def price_for_model(model: str | None) -> tuple[float, float]:
    """USD/1М-токенов (input, output) для модели по префиксу имени.
    Откат на OPENAI_INPUT/OUTPUT_USD_PER_MTOK, если модель не в таблице."""
    if model:
        for prefix in sorted(OPENAI_MODEL_PRICES, key=len, reverse=True):
            if model.startswith(prefix):
                return OPENAI_MODEL_PRICES[prefix]
    return (OPENAI_INPUT_USD_PER_MTOK, OPENAI_OUTPUT_USD_PER_MTOK)


# --- Брендинг дайджеста ---
# Путь к digest_branding.json. Пусто — файл берётся из пакета (локальная разработка,
# тесты). На сервере ОБЯЗАН указывать на общий том, смонтированный во ВСЕ контейнеры.
# Инцидент 03.08: файл лежал внутри образа, тома не было — админка (контейнер app)
# записывала правки в свою копию, а выгрузку HTML/PDF делают воркеры и читали свою,
# нетронутую. Пользователь видел изменения в превью и не видел в выгрузке. Плюс любая
# пересборка образа возвращала git-версию поверх правок.
DIGEST_BRANDING_PATH = os.environ.get("DIGEST_BRANDING_PATH", "").strip()

# --- Auth ---
AUTH_COOKIE_NAME = os.environ.get("AUTH_COOKIE_NAME", "oiltech_session")
AUTH_SESSION_DAYS = int(os.environ.get("AUTH_SESSION_DAYS", "30"))
# Флаг Secure на сессионной cookie. Прод за HTTPS (Caddy) → должно быть True (тех-долг T8).
# Для локальной разработки по http:// выставить AUTH_COOKIE_SECURE=0, иначе браузер
# не сохранит cookie и вход не сработает.
AUTH_COOKIE_SECURE = os.environ.get("AUTH_COOKIE_SECURE", "true").strip().lower() in ("1", "true", "yes", "on")
