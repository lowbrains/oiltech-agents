"""Playwright-based parser for JS-rendered listing and article pages.

Используется для источников с parse_strategy='playwright' — сайты, где контент
рендерится JavaScript или стоит WAF-проверка (Cloudflare challenge и т.п.),
которую lxml/requests не проходят.

Зависимость: playwright (опциональная).
  pip install playwright && playwright install chromium

На сервере с 1.9 ГБ RAM запускать строго последовательно (1 инстанс Chromium):
достаточно выставить PLAYWRIGHT_WORKERS=1 и не включать в общий thread-pool.

Логика извлечения ссылок и дедупликации наследует request_parser, но HTML
листинга и самих статей получается через headless Chromium.
"""

from __future__ import annotations

import html as html_lib
import logging
import os
import re
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from oiltech_digest.db import repository
from oiltech_digest.config import MIN_ARTICLE_TEXT_CHARS, REQUEST_ARTICLE_LIMIT
from oiltech_digest.ingestion import normalize, verdicts
from oiltech_digest.ingestion.verdicts import ArticleFetch

logger = logging.getLogger(__name__)


def is_available() -> bool:
    """True если playwright установлен и Chromium доступен."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _playwright_proxy_for(url: str) -> dict[str, str] | None:
    """Прокси в playwright-формате ({server, username, password}) для URL, или None.

    Переиспользует логику http_client (PROXY_HOST_OVERRIDES имеет приоритет над
    глобальным PROXY_URL): через прокси идут только хосты из overrides — это и есть
    точечный канал для Hard-WAF (Cloudflare/Akamai), чтобы не гонять платный
    резидентский трафик для всех playwright-источников. Headless Chromium с
    резидентским IP нужен сайтам, блокирующим и по JS-challenge, и по IP-репутации.
    """
    from oiltech_digest.ingestion.http_client import _host, _proxy_for

    proxies = _proxy_for(_host(url))
    if not proxies:
        return None
    raw = proxies.get("https") or proxies.get("http")
    if not raw:
        return None
    parts = urlsplit(raw)
    if not parts.hostname:
        return None
    server = f"{parts.scheme or 'http'}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    pw_proxy: dict[str, str] = {"server": server}
    if parts.username:
        pw_proxy["username"] = unquote(parts.username)
    if parts.password:
        pw_proxy["password"] = unquote(parts.password)
    return pw_proxy


_BLOCK_STATUSES = {403, 429, 503}

# Чем закончилась последняя загрузка в этом потоке: ok:200, blocked:403, error:TimeoutError.
# fetch_rendered возвращает None и на блок, и на сбой — а задача внешнего сбора до 18.09
# завершалась «ok» с нулями, и стена защиты (S&P: 403) выглядела как «нового нет».
_last_fetch = threading.local()


def last_fetch_status() -> str | None:
    return getattr(_last_fetch, "status", None)


def with_base_href(html_text: str, final_url: str) -> str:
    """Вписать в отрисованную страницу `<base href>` с КОНЕЧНЫМ адресом.

    Относительные ссылки браузер разрешает от адреса, на котором страница оказалась
    после переадресации, а разбор листинга — от адреса из настройки. У CNOOC лента
    `/zxzx/gsxw/` скриптом уходит на `/zxzx/gsxw/gsxw/`, ссылки на ней вида
    `./202609/t….html` — и парсер собирал адреса без одного звена пути: все 404.
    Если `<base>` на странице уже есть — не трогаем, он главнее.
    """
    if not final_url or re.search(r"<base\s", html_text[:20000], re.I):
        return html_text
    tag = f'<base href="{html_lib.escape(final_url, quote=True)}">'
    match = re.search(r"<head[^>]*>", html_text, re.I)
    if match:
        return html_text[:match.end()] + tag + html_text[match.end():]
    return tag + html_text


# Срок рендера сверх его собственных сроков: goto ограничен timeout_ms, пауза — settle_ms,
# а page.content() и browser.close() у Playwright не ограничены ничем. 24.09 content() у
# листинга JPT не вернулся 20 ч 45 мин и держал весь шаг parse; 25.09 его отпустило
# убийство chrome («Target page, context or browser has been closed»). Запас — на запуск,
# new_page, content() и close(): в норме это секунды.
RENDER_DEADLINE_SLACK_SECONDS = 30.0

# Метка рендера в командной строке его браузера: Chromium чужой ключ пропускает и хелперам
# не передаёт — она есть только у процесса браузера, лидера его группы (проверено 28.09 на
# headless shell 1.62: macOS и Linux).
_RENDER_MARKER = "--oiltech-render"
_PROC = Path("/proc")


def _process_command_lines() -> list[tuple[int, str]]:
    """(PID, командная строка) процессов системы: /proc на Linux (в образах ядра и NL `ps` нет),
    `ps` на macOS (разработка)."""
    if _PROC.is_dir():
        rows = []
        for entry in _PROC.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue  # процесс успел выйти
            rows.append((int(entry.name), raw.replace(b"\0", b" ").decode("utf-8", "replace")))
        return rows
    listing = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True,
                             timeout=10, check=True).stdout
    rows = []
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), command))
    return rows


def _browser_pid(marker: str) -> int | None:
    """PID браузера этого рендера — по метке в его командной строке. Сбой — None, не исключение.

    В API Playwright PID нет. До 28.09 его спрашивали у CDP (SystemInfo.getProcessInfo), но у
    вызова CDP-сессии нет срока, а снятие браузера его не отпускает — дочерние сессии Playwright
    при обрыве связи не закрывает (замер 28.09: вызов к замолчавшему браузеру держался, пока жив
    драйвер). Из-за этого и срок нельзя было взвести раньше: чтобы снять браузер, нужен PID.
    Таблица процессов не зависит ни от браузера, ни от драйвера."""
    try:
        pids = [pid for pid, command in _process_command_lines()
                if marker in command.split() and "--type=" not in command]
    except Exception as exc:  # noqa: BLE001 - без PID рендер идёт; сторож поищет браузер ещё раз по сроку
        logger.warning("playwright: PID браузера не получен (%s)", exc)
        return None
    # 0 и 1 — не браузер: killpg(0) снял бы группу самого шага (скрипт, сторож), 1 — init.
    if len(pids) != 1 or pids[0] <= 1:
        logger.warning("playwright: PID браузера не получен — по метке найдено %s", pids)
        return None
    return pids[0]


def _kill_browser(pid: int) -> None:
    """SIGKILL браузеру вместе с хелперами: Playwright запускает Chromium лидером своей группы."""
    try:
        os.killpg(pid, signal.SIGKILL)
        return
    except ProcessLookupError:
        pass  # не лидер группы — тогда хотя бы сам процесс
    except PermissionError as exc:
        logger.warning("playwright: группу браузера %s не снять: %s", pid, exc)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class _RenderDeadline:
    """Сторож срока одного рендера: по истечении снимает браузер.

    Вызов Playwright, который ждёт мёртвый браузер, отпускает с ошибкой «closed» — так
    25.09 и сняли зависший parse руками; так же отпускает и зависший launch() (замер 28.09).
    Взводится до запуска браузера: launch() без ответа браузера Playwright ждёт 180 с. PID
    сообщает рендер после запуска (watch); если срок вышел раньше — сторож ищет браузер по
    метке сам (locate). Бьёт только в свой браузер: рендеры идут и параллельно (API — пул
    потоков), и «все chrome процесса» задели бы чужие. После cancel() сторож не убьёт уже
    ничего: снятие и отмена идут под одним замком.
    """

    def __init__(self, seconds: float, url: str, locate: Callable[[], int | None]) -> None:
        self.fired = False
        self._pid: int | None = None
        self._locate = locate
        self._seconds = seconds
        self._url = url
        self._lock = threading.Lock()
        self._done = False
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def watch(self, pid: int | None) -> None:
        with self._lock:
            self._pid = pid

    def _expire(self) -> None:
        with self._lock:
            if self._done:
                return
            pid = self._pid if self._pid is not None else self._locate()
            if pid is None:
                logger.warning("playwright %s — рендер не уложился в %.0f с, но браузер не найден: снять нечего",
                               self._url, self._seconds)
                return
            self.fired = True
            logger.warning("playwright %s — рендер не уложился в %.0f с: браузер снят (pid %s)",
                           self._url, self._seconds, pid)
            _kill_browser(pid)

    def cancel(self) -> None:
        with self._lock:
            self._done = True
        self._timer.cancel()


def fetch_rendered(url: str, timeout_ms: int = 30_000, wait_until: str = "domcontentloaded",
                   settle_ms: int = 3500) -> bytes | None:
    """Загрузить страницу через headless Chromium, вернуть HTML как bytes.

    wait_until='domcontentloaded' (НЕ 'networkidle'): networkidle зависает на сайтах
    с постоянной сетевой активностью (аналитика/реклама/websockets) и упирается в
    timeout (наблюдалось на bcg.com). После загрузки даём JS дорендериться фиксированной
    паузой settle_ms (важно для JS-листингов и прохождения лёгких challenge).
    Возвращает None при блокировке (403/429/503) — чтобы не разбирать challenge-страницу.
    Весь рендер от запуска браузера до его закрытия идёт под сроком (_RenderDeadline).
    """
    _last_fetch.status = None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("playwright не установлен: pip install playwright && playwright install chromium")
        _last_fetch.status = "error:playwright_missing"
        return None

    deadline_seconds = (timeout_ms + settle_ms) / 1000 + RENDER_DEADLINE_SLACK_SECONDS
    deadline: _RenderDeadline | None = None
    try:
        with sync_playwright() as pw:
            # По метке сторож найдёт браузер этого рендера среди чужих, не спрашивая его самого.
            marker = f"{_RENDER_MARKER}={uuid.uuid4().hex}"
            # --no-sandbox: Chromium под root в Docker; --disable-dev-shm-usage: малый /dev/shm
            # на сервере 1.9 ГБ RAM (иначе краши вкладок). Те же флаги, что в PDF-экспорте.
            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage", marker],
            }
            proxy = _playwright_proxy_for(url)
            if proxy:
                launch_kwargs["proxy"] = proxy
                logger.info("playwright %s — через прокси %s", url, proxy.get("server"))
            # Срок — с запуска: зависший launch() снимается так же, как зависший content().
            deadline = _RenderDeadline(deadline_seconds, url, lambda: _browser_pid(marker))
            try:
                browser = pw.chromium.launch(**launch_kwargs)
                try:
                    deadline.watch(_browser_pid(marker))
                    page = browser.new_page(
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        )
                    )
                    response = page.goto(url, timeout=timeout_ms, wait_until=wait_until)
                    if response is not None and response.status in _BLOCK_STATUSES:
                        logger.warning("playwright %s — статус %s (WAF/блок), пропуск", url, response.status)
                        _last_fetch.status = f"blocked:{response.status}"
                        return None
                    _last_fetch.status = f"ok:{response.status if response is not None else '-'}"
                    if settle_ms:
                        page.wait_for_timeout(settle_ms)
                    html_content = with_base_href(page.content(), page.url)
                finally:
                    # close() тоже под сроком: у зависшего браузера висит и он.
                    browser.close()
            finally:
                deadline.cancel()
        return html_content.encode("utf-8") if isinstance(html_content, str) else html_content
    except Exception as exc:  # noqa: BLE001
        if deadline is not None and deadline.fired:
            logger.warning("playwright fetch failed for %s: срок рендера истёк — %s", url, exc)
            _last_fetch.status = "error:render_timeout"
            return None
        logger.warning("playwright fetch failed for %s: %s", url, exc)
        _last_fetch.status = f"error:{type(exc).__name__}"
        return None


def parse_source(source: dict, max_age_days: int | None = None, article_limit: int = REQUEST_ARTICLE_LIMIT) -> dict:
    """Parse a JS-rendered listing page via Playwright, insert new articles.

    Delegates link extraction and dedup logic to request_parser after fetching
    the rendered DOM — same pipeline, different fetch backend.
    """
    if not is_available():
        logger.error(
            "Playwright not available — source %s (%s) skipped. "
            "Install: pip install playwright && playwright install chromium",
            source.get("name"),
            source.get("id"),
        )
        return _empty_stats()

    from oiltech_digest.ingestion.request_parser import insert_candidates

    listing_url = source.get("listing_url") or source.get("url")
    if not listing_url:
        return _empty_stats()

    candidates = render_listing_candidates(source, listing_url, limit=article_limit)
    if not candidates:
        logger.info("playwright: no candidates found for source %s (%s)", source.get("name"), listing_url)
        return _empty_stats()

    stats = insert_candidates(
        source,
        candidates,
        max_age_days=max_age_days,
        article_fetcher=render_article,
    )
    repository.touch_last_parsed(source["id"])
    return stats


# Сколько ждать, пока страница дорисуется: первая попытка и вторая, если первая дала
# пусто. Одна попытка — это то, на чём сидел NL-воркер: у ядра повтор для листинга
# был (замечено на bakerhughes.com: «то 6, то 0 кандидатов»), а воркер его не
# унаследовал, как и heartbeat 17.09. Страница статьи повтора не имела нигде.
LISTING_SETTLE_MS = (5000, 12000)
ARTICLE_SETTLE_MS = (3500, 9000)


def render_listing_candidates(source: dict, listing_url: str, limit: int = REQUEST_ARTICLE_LIMIT) -> list:
    """Отрисовать листинг и извлечь кандидатов; пусто — ещё раз с ожиданием дольше.

    Единственное место этой логики: ядро, NL-воркер и диагностика зовут её, а не свою
    копию, — иначе одна из копий снова останется без повтора.
    """
    from oiltech_digest.ingestion.request_parser import extract_candidate_links

    for attempt, settle_ms in enumerate(LISTING_SETTLE_MS, start=1):
        content = fetch_rendered(listing_url, settle_ms=settle_ms)
        candidates = extract_candidate_links(source, listing_url, content, limit=limit) if content else []
        if candidates:
            return candidates
        if attempt < len(LISTING_SETTLE_MS):
            logger.info("playwright: 0 кандидатов у %s за %d мс — ещё попытка", source.get("name"), settle_ms)
    return []


def render_article(candidate, source: dict) -> ArticleFetch:
    """Статья через браузер. Текст короче порога — ещё попытка с ожиданием дольше.

    Блок (403/429/503) повтором не лечится — тогда выходим сразу. Возвращает
    ArticleFetch: запись для вставки или причину, почему её нет.
    """
    from oiltech_digest.ingestion.request_parser import parse_article_page

    outcome = ArticleFetch(None, verdicts.FETCH_FAILED)
    for settle_ms in ARTICLE_SETTLE_MS:
        content = fetch_rendered(candidate.url, settle_ms=settle_ms)
        if content is None:
            return ArticleFetch(None, verdicts.FETCH_FAILED, detail=last_fetch_status() or "")
        title, published_at, raw_text = parse_article_page(content, candidate.title)
        if title and len(raw_text) >= MIN_ARTICLE_TEXT_CHARS:
            return ArticleFetch({
                "source_id": source["id"],
                "title": title[:500],
                "url": candidate.url,
                "published_at": published_at or candidate.published_at,
                "raw_text": raw_text,
                "text_truncated": normalize.is_truncated(raw_text),
                "language": _guess_language(source),
                "content_hash": normalize.compute_content_hash(title, candidate.url),
            }, None, title, len(raw_text))
        outcome = ArticleFetch(None, verdicts.TOO_SHORT, title, len(raw_text))
    return outcome


def rendered_article(candidate, source: dict) -> dict | None:
    """Запись статьи или None — для кода, которому причина не нужна (зарубежный воркер)."""
    return render_article(candidate, source).article


def _empty_stats() -> dict[str, Any]:
    return {"added": 0, "attempted": 0, "skipped_old": 0,
            "skipped_irrelevant": 0, "skipped_known": 0}


def _guess_language(source: dict) -> str | None:
    category = (source.get("category") or "").lower()
    if any(marker in category for marker in ("рф", "снг", "россий", "telegram")):
        return "ru"
    if "международ" in category:
        return "en"
    return None
