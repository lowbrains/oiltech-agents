"""HTTP client with retries, per-host pacing, and soft cooldowns after blocks.

The goal is reliability from a server environment without looking overly aggressive:
  - thread-local sessions for connection reuse;
  - minimum interval + small jitter per host;
  - respect Retry-After for 429/503 when present;
  - temporary cooldown for hosts returning 403/429 repeatedly;
  - the same cooldown for hosts that time out on every attempt;
  - SSL fallback only for certificate failures.
"""

from __future__ import annotations

import atexit
import logging
import os
import random
import tempfile
import threading
import time
from urllib.parse import urlsplit

import requests

from oiltech_digest.config import (
    HTTP_BLOCK_COOLDOWN_SECONDS,
    HTTP_DEAD_HOST_COOLDOWN_SECONDS,
    HTTP_JITTER_SECONDS,
    HTTP_MIN_INTERVAL_SECONDS,
    PROXY_HOST_OVERRIDES,
    PROXY_TIMEOUT,
    PROXY_URL,
    REQUEST_TIMEOUT,
    RETRY_ATTEMPTS,
    RETRY_BACKOFF_BASE,
)

logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36 OilTechDigest/1.0"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
}

_thread_local = threading.local()
_host_lock = threading.Lock()
_host_next_allowed: dict[str, float] = {}
_host_cooldown_until: dict[str, float] = {}

# Подсчёт HTTP-ответов по статусам — для итоговой строки в конце процесса.
# Помогает видеть реальное соотношение OK/403 в логах scheduler, а не только WARNING'и.
_counts_lock = threading.Lock()
_status_counts: dict[str, int] = {}


def fetch(url: str, timeout: int = REQUEST_TIMEOUT) -> bytes | None:
    """GET with retries, soft pacing and cooldown handling."""
    return _request(url, timeout=timeout, quiet=False, retries=RETRY_ATTEMPTS)


def probe(url: str, timeout: int = 10) -> bytes | None:
    """Single-pass GET for RSS discovery and candidate testing."""
    return _request(url, timeout=timeout, quiet=True, retries=1)


def _request(url: str, timeout: int, quiet: bool, retries: int) -> bytes | None:
    host = _host(url)
    if _is_host_cooling_down(host):
        logger.debug("HTTP %s — host cooldown active, skip", url)
        return None

    proxies = _proxy_for(host)
    if proxies:
        timeout = max(timeout, PROXY_TIMEOUT)
        _maybe_log_proxy(proxies)

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        _wait_for_host_slot(host)
        try:
            resp = _get_session().get(
                url,
                timeout=timeout,
                headers=_DEFAULT_HEADERS,
                allow_redirects=True,
                proxies=proxies,
            )
            _tally(resp.status_code)
            if resp.status_code in {403, 429, 503}:
                _register_block(host, resp)
                # 403/429: хост сам попросил паузу — cooldown уже выставлен, поэтому
                # повторять в этом же запросе бессмысленно (иначе следующая попытка
                # залипнет в _wait_for_host_slot на весь cooldown). 503 (временная
                # ошибка сервера) — оставляем на обычные ретраи.
                if resp.status_code in {403, 429}:
                    return None
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.SSLError as exc:
            last_err = exc
            content = _fetch_insecure(url, timeout, quiet=quiet, proxies=proxies)
            if content is not None:
                return content
            break
        except requests.RequestException as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(_retry_delay(attempt))

    # Хост не ответил НИ РАЗУ за все попытки — значит он недоступен отсюда, а не
    # занят именно этим адресом. Без этой паузы пакет из 25 статей одного издания
    # стоил 25x63 с: каждая статья заново выясняла то, что уже известно с первой.
    # Громкий отказ (403) такую паузу получал всегда, молчаливый (таймаут) — нет,
    # и эта асимметрия съедала lease внешней задачи целиком.
    #
    # Только для настоящей загрузки: probe ходит с retries=1, и одна неудачная
    # проба не должна закрывать хост для диагностики источника.
    if retries > 1 and _is_unreachable(last_err):
        _set_cooldown(_host(url), HTTP_DEAD_HOST_COOLDOWN_SECONDS, f"no answer in {retries} attempts")

    if quiet:
        logger.debug("HTTP %s — отказ после %d попыток: %s", url, retries, last_err)
    else:
        logger.warning("HTTP %s — отказ после %d попыток: %s", url, retries, last_err)
    return None


def _is_unreachable(err: Exception | None) -> bool:
    """Недоступность хоста, а не отказ по конкретному адресу.

    404/500 прилетают как HTTPError и означают проблему со страницей — закрывать
    из-за них всё издание нельзя.
    """
    return isinstance(err, (requests.exceptions.Timeout, requests.exceptions.ConnectionError))


_ext_bundle_lock = threading.Lock()
# None = ещё не инициализирован; "" = extra_ca.pem отсутствует; иначе — путь к bundle.
_ext_bundle_path: str | None = None


def _extended_ca_bundle() -> str | None:
    """Путь к объединённому CA-bundle (certifi + локальные промежуточные из
    extra_ca.pem). Нужен для серверов, присылающих неполную цепочку (leaf без
    intermediate): добавив недостающие промежуточные локально, мы достраиваем
    цепочку и проходим верификацию ЧЕСТНО, без verify=False. Создаётся один раз
    во временном файле. None — если extra_ca.pem отсутствует/не читается."""
    global _ext_bundle_path
    if _ext_bundle_path is not None:
        return _ext_bundle_path or None
    with _ext_bundle_lock:
        if _ext_bundle_path is not None:
            return _ext_bundle_path or None
        extra = os.path.join(os.path.dirname(__file__), "extra_ca.pem")
        if not os.path.exists(extra):
            _ext_bundle_path = ""
            return None
        try:
            import certifi

            with open(certifi.where(), "rb") as f:
                base = f.read()
            with open(extra, "rb") as f:
                extra_data = f.read()
            fd, path = tempfile.mkstemp(suffix="-oiltech-ca.pem")
            with os.fdopen(fd, "wb") as out:
                out.write(base)
                out.write(b"\n")
                out.write(extra_data)
            _ext_bundle_path = path
            logger.info("extended CA-bundle готов: %s", path)
        except Exception as exc:  # noqa: BLE001 - не должно ронять парсинг
            logger.warning("extended CA-bundle не создан: %s", exc)
            _ext_bundle_path = ""
            return None
    return _ext_bundle_path or None


def _fetch_insecure(
    url: str, timeout: int, quiet: bool = False, proxies: dict[str, str] | None = None
) -> bytes | None:
    """Fallback после SSLError на обычном пути.

    Шаг 1 — повтор с расширенным CA-bundle (certifi + недостающие промежуточные).
    Верификация остаётся ВКЛючённой — просто подсовываем промежуточные локально.
    Шаг 2 — только если и это не помогло, идём через verify=False (крайний случай).
    """
    bundle = _extended_ca_bundle()
    if bundle:
        try:
            resp = _get_session().get(
                url,
                timeout=timeout,
                headers=_DEFAULT_HEADERS,
                allow_redirects=True,
                verify=bundle,
                proxies=proxies,
            )
            _tally(resp.status_code)
            if resp.status_code in {403, 429, 503}:
                _register_block(_host(url), resp)
            resp.raise_for_status()
            logger.info("HTTP %s — SSL достроен через extended CA-bundle", url)
            return resp.content
        except requests.exceptions.SSLError:
            pass  # цепочка всё равно не строится → ниже verify=False
        except requests.RequestException as exc:
            # не-SSL ошибка (таймаут/connreset) — verify=False не поможет
            if not quiet:
                logger.warning("HTTP %s — fallback (CA-bundle) не удался: %s", url, exc)
            return None

    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = _get_session().get(
            url,
            timeout=timeout,
            headers=_DEFAULT_HEADERS,
            allow_redirects=True,
            verify=False,
            proxies=proxies,
        )
        _tally(resp.status_code)
        if resp.status_code in {403, 429, 503}:
            _register_block(_host(url), resp)
        resp.raise_for_status()
        logger.info("HTTP %s — SSL обойдён через verify=False (крайний fallback)", url)
        return resp.content
    except requests.exceptions.SSLError:
        # verify=False НЕ лечит UNSAFE_LEGACY_RENEGOTIATION_DISABLED (это не сертификат,
        # а рукопожатие): OpenSSL 3 рвёт старые серверы без RFC 5746 (напр. belorusneft.by).
        # Последний шаг — отдельная сессия с OP_LEGACY_SERVER_CONNECT.
        content = _fetch_legacy_tls(url, timeout, proxies=proxies)
        if content is not None:
            return content
        if not quiet:
            logger.warning("HTTP %s — SSL-fallback (incl. legacy TLS) не удался", url)
        return None
    except requests.RequestException as exc:
        if quiet:
            logger.debug("HTTP %s — SSL-fallback не удался: %s", url, exc)
        else:
            logger.warning("HTTP %s — SSL-fallback не удался: %s", url, exc)
        return None


class _LegacyTLSAdapter(requests.adapters.HTTPAdapter):
    """Крайний fallback для старых серверов: разрешает unsafe legacy renegotiation
    (OP_LEGACY_SERVER_CONNECT) и отключает верификацию. Нужен там, где OpenSSL 3 рвёт
    рукопожатие с UNSAFE_LEGACY_RENEGOTIATION_DISABLED, а verify=False не помогает (это
    не проблема сертификата). Применяется ТОЛЬКО после провала обычного verify=False."""

    def init_poolmanager(self, *args, **kwargs):
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


_legacy_session: requests.Session | None = None


def _get_legacy_session() -> requests.Session:
    global _legacy_session
    if _legacy_session is None:
        with _ext_bundle_lock:
            if _legacy_session is None:
                session = requests.Session()
                session.mount("https://", _LegacyTLSAdapter())
                _legacy_session = session
    return _legacy_session


def _fetch_legacy_tls(url: str, timeout: int, proxies: dict[str, str] | None = None) -> bytes | None:
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = _get_legacy_session().get(
            url,
            timeout=timeout,
            headers=_DEFAULT_HEADERS,
            allow_redirects=True,
            verify=False,
            proxies=proxies,
        )
        _tally(resp.status_code)
        if resp.status_code in {403, 429, 503}:
            _register_block(_host(url), resp)
        resp.raise_for_status()
        logger.info("HTTP %s — взят через legacy-TLS renegotiation fallback", url)
        return resp.content
    except requests.RequestException:
        return None


def _get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _thread_local.session = session
    return session


def _host(url: str) -> str:
    return (urlsplit(url).netloc or "").lower()


def _proxy_for(host: str) -> dict[str, str] | None:
    """requests-style proxies mapping for a host, or None for a direct request.

    Per-host overrides win over the global PROXY_URL — this is the hook for the
    future RU/INTL routing task (PROXY_HOST_OVERRIDES is empty for now).
    """
    url = ""
    if host:
        for suffix, override in sorted(PROXY_HOST_OVERRIDES.items(), key=lambda item: len(item[0]), reverse=True):
            if host == suffix or host.endswith("." + suffix):
                url = override
                break
    url = url or PROXY_URL
    if not url:
        return None
    return {"http": url, "https": url}


_proxy_logged = False


def _maybe_log_proxy(proxies: dict[str, str]) -> None:
    """Log proxy activation once per process, with credentials masked."""
    global _proxy_logged
    if not _proxy_logged:
        _proxy_logged = True
        logger.info("HTTP — запросы идут через прокси %s", _mask_proxy(next(iter(proxies.values()))))


def _mask_proxy(url: str) -> str:
    """Hide credentials in a proxy URL so it is safe to log."""
    try:
        parts = urlsplit(url)
        cred = "***@" if (parts.username or parts.password) else ""
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{cred}{parts.hostname or ''}{port}"
    except Exception:  # noqa: BLE001
        return "***"


def _wait_for_host_slot(host: str) -> None:
    if not host:
        return
    while True:
        with _host_lock:
            now = time.monotonic()
            cooldown_until = _host_cooldown_until.get(host, 0.0)
            next_allowed = _host_next_allowed.get(host, 0.0)
            target = max(cooldown_until, next_allowed)
            if target <= now:
                delay = HTTP_MIN_INTERVAL_SECONDS + random.uniform(0, HTTP_JITTER_SECONDS)
                _host_next_allowed[host] = now + delay
                return
            sleep_for = min(max(target - now, 0.0), 5.0)
        time.sleep(sleep_for)


def _is_host_cooling_down(host: str) -> bool:
    if not host:
        return False
    with _host_lock:
        return _host_cooldown_until.get(host, 0.0) > time.monotonic()


def _register_block(host: str, response: requests.Response) -> None:
    retry_after = _retry_after_seconds(response)
    cooldown = max(retry_after, HTTP_BLOCK_COOLDOWN_SECONDS if response.status_code in {403, 429} else 120)
    _set_cooldown(host, cooldown, f"status {response.status_code}")


def _set_cooldown(host: str, seconds: int, why: str) -> None:
    """Единственное место, где хост закрывается на паузу — чтобы правила для
    громкого и молчаливого отказа не разъехались при следующей правке."""
    if not host:
        return
    with _host_lock:
        _host_cooldown_until[host] = time.monotonic() + seconds
    logger.warning("HTTP %s — host cooldown %ss (%s)", host, seconds, why)


def _retry_after_seconds(response: requests.Response) -> int:
    raw = response.headers.get("Retry-After")
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _retry_delay(attempt: int) -> float:
    base = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
    return base + random.uniform(0, HTTP_JITTER_SECONDS)


def _tally(status: object) -> None:
    """Count one HTTP response by status code for the end-of-process summary."""
    key = str(status)
    with _counts_lock:
        _status_counts[key] = _status_counts.get(key, 0) + 1


def _log_http_summary() -> None:
    """Log the tally of HTTP statuses once, at process exit (registered via atexit)."""
    with _counts_lock:
        if not _status_counts:
            return
        total = sum(_status_counts.values())
        parts = ", ".join(
            f"{k}×{v}" for k, v in sorted(_status_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
    logger.info("HTTP итог за процесс: %s (всего %d запросов)", parts, total)


atexit.register(_log_http_summary)
