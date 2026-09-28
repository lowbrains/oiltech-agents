import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from oiltech_digest.ingestion import playwright_parser

ROOT = Path(__file__).resolve().parents[1]


LISTING_HTML = b"""
<html>
  <body>
    <a href="/news/2026/06/js-rendered-drilling-automation">
      JS rendered drilling automation platform improves oilfield operations
    </a>
  </body>
</html>
"""

ARTICLE_HTML = b"""
<html>
  <head>
    <meta property="og:title" content="JS rendered drilling automation platform improves oilfield operations">
    <meta property="article:published_time" content="2026-06-05T08:30:00Z">
  </head>
  <body>
    <article>
      <p>The company deployed a drilling automation platform across oilfield service crews.</p>
      <p>The system improves well construction, equipment uptime, and production operations.</p>
      <p>Additional industrial context keeps this article above teaser length for downstream AI stages.</p>
    </article>
  </body>
</html>
"""


def test_parse_source_renders_listing_and_article_pages(monkeypatch):
    rendered_urls = []
    inserted = []
    state = {}

    def fake_fetch_rendered(url, **kwargs):
        rendered_urls.append(url)
        if url == "https://example.com/news":
            return LISTING_HTML
        if url == "https://example.com/news/2026/06/js-rendered-drilling-automation":
            return ARTICLE_HTML
        return None

    monkeypatch.setattr(playwright_parser, "is_available", lambda: True)
    monkeypatch.setattr(playwright_parser, "fetch_rendered", fake_fetch_rendered)
    monkeypatch.setattr(playwright_parser.repository, "insert_article", lambda article: inserted.append(article) or True)
    monkeypatch.setattr(playwright_parser.repository, "article_exists", lambda url: False)
    monkeypatch.setattr(playwright_parser.repository, "touch_last_parsed", lambda source_id: state.setdefault("touched", source_id))
    monkeypatch.setattr(
        playwright_parser.repository,
        "update_source_request_state",
        lambda source_id, **kwargs: state.update({"source_id": source_id, **kwargs}),
    )

    stats = playwright_parser.parse_source(
        {
            "id": 17,
            "name": "Rendered Example",
            "parse_strategy": "playwright",
            "listing_url": "https://example.com/news",
            "category": "международные",
        },
        article_limit=5,
    )

    assert stats["added"] == 1
    assert rendered_urls == [
        "https://example.com/news",
        "https://example.com/news/2026/06/js-rendered-drilling-automation",
    ]
    assert inserted[0]["url"] == "https://example.com/news/2026/06/js-rendered-drilling-automation"
    assert inserted[0]["language"] == "en"
    assert state["source_id"] == 17
    assert state["last_seen_article_url"] == "https://example.com/news/2026/06/js-rendered-drilling-automation"


def test_parse_source_reports_unavailable_without_rendering(monkeypatch):
    monkeypatch.setattr(playwright_parser, "is_available", lambda: False)
    monkeypatch.setattr(
        playwright_parser,
        "fetch_rendered",
        lambda url: (_ for _ in ()).throw(AssertionError("fetch_rendered should not be called")),
    )

    stats = playwright_parser.parse_source({"id": 18, "name": "No browser", "listing_url": "https://example.com/news"})

    assert stats["added"] == 0
    assert stats["attempted"] == 0


def test_playwright_proxy_for_only_overridden_hosts(monkeypatch):
    """Через прокси идут только хосты из PROXY_HOST_OVERRIDES (точечно для Hard-WAF);
    остальные playwright-источники — напрямую. PROXY_URL парсится в playwright-формат."""
    from oiltech_digest.ingestion import http_client

    monkeypatch.setattr(
        http_client,
        "_proxy_for",
        lambda host: (
            {"http": "http://u:p@eu.proxy.2captcha.com:2334",
             "https": "http://u:p@eu.proxy.2captcha.com:2334"}
            if host == "www.energyvoice.com" else None
        ),
    )

    assert playwright_parser._playwright_proxy_for("https://www.energyvoice.com/news") == {
        "server": "http://eu.proxy.2captcha.com:2334",
        "username": "u",
        "password": "p",
    }
    assert playwright_parser._playwright_proxy_for("https://www.slb.com/news-and-insights") is None


def test_listing_gets_a_second_longer_attempt_when_first_is_empty(monkeypatch):
    """«Пока не получится — пара попыток»: у ядра повтор был, а NL-воркер его не
    унаследовал. Теперь оба зовут одну функцию."""
    settles: list[int] = []

    def fake_render(url, settle_ms=0, **_):
        settles.append(settle_ms)
        return LISTING_HTML if len(settles) == 2 else b"<html><body>loading...</body></html>"
    monkeypatch.setattr(playwright_parser, "fetch_rendered", fake_render)

    candidates = playwright_parser.render_listing_candidates({"name": "S"}, "https://example.com/news", limit=5)

    assert settles == list(playwright_parser.LISTING_SETTLE_MS)
    assert settles[1] > settles[0], "вторая попытка ждёт дольше"
    assert len(candidates) == 1


def test_article_gets_a_second_longer_attempt_when_text_is_short(monkeypatch):
    settles: list[int] = []

    def fake_render(url, settle_ms=0, **_):
        settles.append(settle_ms)
        return ARTICLE_HTML if len(settles) == 2 else b"<html><head><title>x</title></head><body>...</body></html>"
    monkeypatch.setattr(playwright_parser, "fetch_rendered", fake_render)
    from oiltech_digest.ingestion.request_parser import CandidateLink

    record = playwright_parser.rendered_article(
        CandidateLink("https://example.com/news/a", "JS rendered drilling automation platform", 5), {"id": 3})

    assert settles == list(playwright_parser.ARTICLE_SETTLE_MS)
    assert record is not None and record["source_id"] == 3


def test_blocked_article_is_not_retried(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(playwright_parser, "fetch_rendered", lambda url, settle_ms=0, **_: calls.append(1))
    from oiltech_digest.ingestion.request_parser import CandidateLink

    assert playwright_parser.rendered_article(CandidateLink("https://e.com/a", "t" * 30, 5), {"id": 1}) is None
    assert len(calls) == 1, "блок (403/429/503) ожиданием не лечится"


# --- Срок одного рендера (инцидент 24.09) -------------------------------------------------

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # macOS так отвечает про зомби, которого ещё не подобрали
        return True
    return True


def _group_gone(pgid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_render_deadline_kills_the_whole_browser_process_group():
    """Chromium идёт лидером своей группы вместе с хелперами — снимается вся группа."""
    helper = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
    browser = subprocess.Popen([sys.executable, "-c", helper], start_new_session=True)
    try:
        deadline = playwright_parser._RenderDeadline(0.3, "https://example.com/slow", locate=lambda: None)
        deadline.watch(browser.pid)
        browser.wait(timeout=10)
        assert deadline.fired
        assert browser.returncode == -signal.SIGKILL
        assert _group_gone(browser.pid), "хелпер из группы браузера пережил срок"
    finally:
        try:
            os.killpg(browser.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_render_deadline_finds_the_browser_itself_when_launch_never_returned():
    """Срок вышел раньше, чем стал известен PID (launch не вернулся): сторож ищет браузер сам."""
    browser = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        deadline = playwright_parser._RenderDeadline(0.3, "https://example.com/launch", locate=lambda: browser.pid)
        browser.wait(timeout=10)
        assert deadline.fired
        assert browser.returncode == -signal.SIGKILL
    finally:
        if browser.poll() is None:
            browser.kill()
            browser.wait()


def test_render_deadline_cancelled_in_time_kills_nothing():
    browser = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        deadline = playwright_parser._RenderDeadline(0.3, "https://example.com/fast", locate=lambda: browser.pid)
        deadline.watch(browser.pid)
        deadline.cancel()
        time.sleep(0.8)
        assert browser.poll() is None
        assert not deadline.fired
    finally:
        browser.kill()
        browser.wait()


def test_render_whose_browser_is_not_found_kills_nothing():
    deadline = playwright_parser._RenderDeadline(0.01, "https://example.com", locate=lambda: None)
    time.sleep(0.1)
    assert not deadline.fired
    deadline.cancel()


def test_browser_pid_comes_from_the_marker_in_its_command_line():
    """Метка рендера — только в командной строке его браузера: чужой рендер (пул потоков API)
    со своей меткой не заденет. Проверка на настоящей таблице процессов этой системы."""
    marker = f"--oiltech-render={uuid.uuid4().hex}"
    browser = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--disable-gpu", marker])
    try:
        deadline = time.monotonic() + 5
        while playwright_parser._browser_pid(marker) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert playwright_parser._browser_pid(marker) == browser.pid
        assert playwright_parser._browser_pid(f"--oiltech-render={uuid.uuid4().hex}") is None
    finally:
        browser.kill()
        browser.wait()


def test_linux_process_table_is_read_from_proc(monkeypatch, tmp_path):
    """В образах ядра и NL `ps` нет — таблица читается из /proc: аргументы там через NUL."""
    for pid, cmdline in (("19", b"/root/.cache/ms-playwright/chromium_headless_shell-1234/chrome-linux/headless_shell"
                                b"\0--no-sandbox\0--oiltech-render=abc\0--remote-debugging-pipe\0"),
                         ("21", b"headless_shell\0--type=zygote\0--oiltech-render=abc\0")):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "cmdline").write_bytes(cmdline)
    (tmp_path / "self").mkdir()  # не процесс
    (tmp_path / "77").mkdir()  # процесс вышел между обходом и чтением: cmdline нет
    monkeypatch.setattr(playwright_parser, "_PROC", tmp_path)

    assert playwright_parser._browser_pid("--oiltech-render=abc") == 19


def test_browser_pid_lookup_failure_or_doubt_is_not_fatal(monkeypatch):
    """Без PID рендер идёт, только без сторожа — сбой поиска не роняет его до try/finally с close()."""
    marker = "--oiltech-render=abc"

    def unreadable():
        raise PermissionError("таблица процессов недоступна")

    monkeypatch.setattr(playwright_parser, "_process_command_lines", unreadable)
    assert playwright_parser._browser_pid(marker) is None

    def table(*rows):
        monkeypatch.setattr(playwright_parser, "_process_command_lines", lambda: list(rows))

    table((1234, f"chrome --headless {marker}"), (1235, f"chrome --type=renderer {marker}"))
    assert playwright_parser._browser_pid(marker) == 1234  # хелпер с меткой браузером не считается
    table((1234, f"chrome {marker}"), (1300, f"chrome {marker}"))
    assert playwright_parser._browser_pid(marker) is None  # два браузера с одной меткой — не угадываем
    table((1234, f"chrome {marker}x"))
    assert playwright_parser._browser_pid(marker) is None  # метка сверяется целиком
    # killpg(0) снял бы группу самого шага — скрипт и сторожа, killpg(1) бьёт в init.
    for pid in (0, 1):
        table((pid, f"chrome {marker}"))
        assert playwright_parser._browser_pid(marker) is None


# Настоящий Chromium: срок рендера — это снятый браузер, подделка вызова его не проверит.
RENDER_CHILD = r"""
import json, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from oiltech_digest.ingestion import playwright_parser as pp

PAGES = {
    "/ok": b"<!doctype html><html><head><title>ok</title></head><body><p>ready</p></body></html>",
    # Главный поток страницы занят навсегда: page.content() не вернётся, как у JPT 24.09.
    "/wedge": b"<!doctype html><html><head><title>w</title></head><body><p>w</p>"
              b"<script>setTimeout(function () { for (;;) {} }, 200);</script></body></html>",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_address[1]}"

pp.RENDER_DEADLINE_SLACK_SECONDS = 2
pids = []
# getattr — чтобы на коде до правки тест падал по делу (рендер не вернулся), а не на AttributeError.
real_browser_pid = getattr(pp, "_browser_pid", None)
if real_browser_pid is not None:
    def spy(marker):
        pids.append(real_browser_pid(marker))
        return pids[-1]
    pp._browser_pid = spy

for page, settle_ms in (("ok", 0), ("wedge", 1000)):
    started = time.monotonic()
    html = pp.fetch_rendered(f"{base}/{page}", timeout_ms=5000, settle_ms=settle_ms)
    print(json.dumps({"page": page, "html": (html or b"").decode(), "status": pp.last_fetch_status(),
                      "seconds": time.monotonic() - started, "pids": list(pids)}), flush=True)
"""


@pytest.fixture(scope="module")
def chromium():
    """Есть ли Chromium — проверка мимо кода продукта, чтобы его поломка не выглядела пропуском."""
    probe = ("from playwright.sync_api import sync_playwright\n"
             "with sync_playwright() as pw:\n"
             "    pw.chromium.launch(headless=True, args=['--no-sandbox']).close()\n")
    try:
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        pytest.skip("Chromium не запустился за 60 с")
    if result.returncode != 0:
        pytest.skip(f"Chromium недоступен: {result.stderr.strip()[-200:]}")


def test_render_that_never_returns_is_cut_by_the_deadline(chromium):
    """24.09 page.content() у листинга JPT не вернулся 20 ч 45 мин и держал весь шаг parse.
    Теперь рендер идёт под сроком: по истечении браузер снимается, и вызов отпускает."""
    child = subprocess.Popen(
        [sys.executable, "-c", RENDER_CHILD],
        cwd=str(ROOT),
        env={**os.environ, "PROXY_URL": "", "PROXY_HOST_OVERRIDES": ""},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        out, err = child.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        # Драйвер Playwright — в группе ребёнка; Chromium выходит сам, когда закрывается его труба.
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        pytest.fail("рендер не вернулся за 60 с: срока у него нет")
    rows = {row["page"]: row for row in (json.loads(line) for line in out.splitlines() if line.startswith("{"))}
    assert set(rows) == {"ok", "wedge"}, err[-2000:]

    assert rows["ok"]["status"] == "ok:200"
    assert "ready" in rows["ok"]["html"]

    wedge = rows["wedge"]
    assert wedge["status"] == "error:render_timeout", err[-2000:]
    assert wedge["html"] == ""
    assert wedge["seconds"] < 20  # срок 5 + 1 + 2 = 8 с от запуска браузера
    assert len(wedge["pids"]) == 2 and all(wedge["pids"])
    assert not any(_alive(pid) for pid in wedge["pids"]), "браузер пережил рендер"


# Брешь #67: срок взводился после launch() и после вызова CDP за PID браузера — зависание там
# не снималось. «Браузер», который запускается, но на рукопожатие по трубе не отвечает:
FAKE_BROWSER = """#!/usr/bin/env python3
import os, time
with open(os.environ["BROWSER_PIDS"], "a") as fh:
    fh.write(f"{os.getpid()}\\n")
time.sleep(600)
"""

HANG_CHILD = r"""
import json, os, signal, sys, time
from playwright.sync_api._generated import BrowserType
from oiltech_digest.ingestion import playwright_parser as pp

pp.RENDER_DEADLINE_SLACK_SECONDS = 2
mode, fake = sys.argv[1], sys.argv[2]
real_launch = BrowserType.launch


def launch(self, **kwargs):
    if mode == "no-handshake":
        return real_launch(self, **{**kwargs, "executable_path": fake})
    browser = real_launch(self, **kwargs)
    # Браузер жив, но сразу после запуска перестаёт отвечать: следующий вызов не вернётся.
    session = browser.new_browser_cdp_session()
    info = session.send("SystemInfo.getProcessInfo")["processInfo"]
    session.detach()
    pid = next(int(p["id"]) for p in info if p["type"] == "browser")
    with open(os.environ["BROWSER_PIDS"], "a") as fh:
        fh.write(f"{pid}\n")
    os.kill(pid, signal.SIGSTOP)
    return browser


BrowserType.launch = launch
started = time.monotonic()
html = pp.fetch_rendered("http://127.0.0.1:9/never", timeout_ms=5000, settle_ms=0)
print(json.dumps({"html": html is not None, "status": pp.last_fetch_status(),
                  "seconds": time.monotonic() - started}), flush=True)
"""


def _dies(pid: int, timeout: float = 5.0) -> bool:
    """Процесс ушёл: зомби подбирает его родитель (драйвер) или launchd — на это нужен миг."""
    deadline = time.monotonic() + timeout
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


def _kill_groups(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@pytest.mark.parametrize("mode", ["no-handshake", "stopped"])
def test_render_hung_before_the_browser_pid_is_known_is_cut_too(chromium, tmp_path, mode):
    """no-handshake: launch() ждёт браузер, который не отвечает, — у Playwright 1.62 это 180 с
    (замер 28.09), а не 30, как думали 25.09. stopped: браузер замолчал сразу после запуска —
    вызов CDP за его PID не вернётся никогда: срока у него нет, а снятие браузера его не
    отпускает (замер 28.09 — держал, пока жив драйвер). Теперь срок взведён до запуска, а PID
    ищется по метке в командной строке браузера, без вызова, который может не вернуться."""
    fake = tmp_path / "fake-browser"
    fake.write_text(FAKE_BROWSER)
    fake.chmod(0o755)
    pids_log = tmp_path / "browser.pids"
    child = subprocess.Popen(
        [sys.executable, "-c", HANG_CHILD, mode, str(fake)],
        cwd=str(ROOT),
        env={**os.environ, "PROXY_URL": "", "PROXY_HOST_OVERRIDES": "", "BROWSER_PIDS": str(pids_log)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        out, err = child.communicate(timeout=40)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)  # драйвер — в группе ребёнка
        child.communicate()
        pytest.fail(f"{mode}: рендер не вернулся за 40 с — зависание до срока не снимается")
    finally:
        # Остановленный браузер и подделка сами не выйдут: у них своя группа (detached у Playwright).
        browser_pids = [int(line) for line in pids_log.read_text().split()] if pids_log.exists() else []
        survivors = [pid for pid in browser_pids if not _dies(pid)]
        _kill_groups(browser_pids)
    rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert len(rows) == 1, err[-2000:]

    assert rows[0]["status"] == "error:render_timeout", err[-2000:]
    assert rows[0]["html"] is False
    assert rows[0]["seconds"] < 20  # срок 5 + 0 + 2 = 7 с — теперь от запуска браузера
    assert len(browser_pids) == 1 and survivors == [], "браузер пережил срок рендера"
