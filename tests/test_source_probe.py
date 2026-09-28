"""Проба источника (`source-probe`): путь сбора, вердикт рубежей вставки, ни одной записи.

25.09 молчащие источники объяснила разовая проба «только чтение» (docs/handoff_2026-09-25_
stale-sources.md): путь сбора, но вместо вставки — вердикт по каждому кандидату. Здесь она
становится командой, и главное требование — вердикт считают ТЕ ЖЕ функции, что и настоящая
вставка. Поэтому каждый тест пути здесь же прогоняет настоящий сбор на той же базе и
сверяет: вставилось ровно то, про что проба сказала WOULD_INSERT.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import psycopg
import pytest

from oiltech_digest import cli, config
from oiltech_digest.db import connection, repository
from oiltech_digest.ingestion import (
    playwright_parser,
    relevance_filter,
    request_parser,
    rss_parser,
    source_probe,
    telegram_parser,
    verdicts,
)

DRILLING = "Компания начала бурение новой скважины на месторождении и ввела буровую установку в работу. "
FOOTBALL = "Футбольный клуб выиграл матч чемпионата страны. Болельщики праздновали победу на стадионе. "
WIDGET = "Чтобы сменить тему, очистите чат и задайте новый вопрос помощнику сайта. "


@pytest.fixture(autouse=True)
def _static_prefilter(monkeypatch):
    """Предфильтр — на статическом словаре: тематики, прочитанные другими тестами (кэш
    на 5 минут), не должны менять вердикт."""
    monkeypatch.setattr(relevance_filter, "tag_keywords", lambda: ((), ()))


def _source(**fields) -> dict:
    cols = {"name": "Проба", "source_type": "Company", "url": "https://site.example",
            "enabled": True, "parse_strategy": "request", **fields}
    with connection.get_connection() as conn:
        source_id = conn.execute(
            f"INSERT INTO sources ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
            list(cols.values()),
        ).fetchone()[0]
        conn.commit()
    return repository.get_source(source_id)


def _article(source: dict, url: str, title: str, body: str, *, hidden: bool = False) -> None:
    """Статья в базе — той же вставкой, что у сбора; скрытая — как после пометки гейта."""
    assert repository.insert_article({
        "source_id": source["id"], "title": title, "url": url, "published_at": None,
        "raw_text": body, "text_truncated": False, "language": "ru", "content_hash": f"h-{url}",
    })
    if hidden:
        with connection.get_connection() as conn:
            conn.execute("UPDATE articles SET pending_deletion = TRUE WHERE url = %s", (url,))
            conn.commit()


def _page(title: str, body: str) -> bytes:
    return (f"<html><head><meta property='og:title' content='{title}'></head>"
            f"<body><article><p>{body}</p></article></body></html>").encode()


def _listing(paths: list[str]) -> bytes:
    links = "".join(f'<li><a class="news" href="{path}">Новость номер {i} о работе на месторождении</a></li>'
                    for i, path in enumerate(paths, start=1))
    return f"<html><body><ul>{links}</ul></body></html>".encode()


def _feed(items: list[tuple[str, str, str]]) -> bytes:
    body = "".join(f"<item><title>{title}</title><link>{link}</link><description>{text}</description></item>"
                   for title, link, text in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Лента</title>{body}</channel></rss>'.encode()


def _telegram(posts: list[tuple[int, str, str]]) -> str:
    return "".join(
        f'<div class="tgme_widget_message" data-post="probechan/{n}">'
        f'<div class="tgme_widget_message_text js-message_text">{text}</div>'
        f'<a class="tgme_widget_message_date"><time datetime="{when}"></time></a></div>'
        for n, text, when in posts
    )


def _snapshot() -> tuple:
    """Всё, что пишет сбор: статьи и состояние источников."""
    with connection.get_connection() as conn:
        articles = conn.execute(
            "SELECT id, url, url_key, body_hash, pending_deletion FROM articles ORDER BY id").fetchall()
        sources = conn.execute(
            "SELECT id, last_parsed_at, last_seen_article_url, last_seen_published_at, last_listing_hash, "
            "updated_at FROM sources ORDER BY id").fetchall()
    return articles, sources


def _urls() -> set[str]:
    with connection.get_connection() as conn:
        return {row[0] for row in conn.execute("SELECT url FROM articles").fetchall()}


def _verdicts(report: dict) -> list[tuple[str, str]]:
    return [(row["url"], row["verdict"]) for row in report["rows"]]


def _would_insert(report: dict) -> set[str]:
    return {url for url, verdict in _verdicts(report) if verdict == verdicts.WOULD_INSERT}


def test_insert_article_decides_by_the_verdict_the_probe_reads(isolated_db, monkeypatch):
    """Вердикт пробы и решение настоящей вставки — одна функция, insert_verdict.

    По каждому рубежу: сперва вердикт в соединении только для чтения (путь пробы), затем
    настоящая insert_article на той же базе — она обязана решить ТЕМ ЖЕ вердиктом и
    вставить ровно тогда, когда проба сказала WOULD_INSERT. Вставка знает только «ключ
    занят» (DUP_URL_KEY); та же ли это статья — разметка пробы, её тесты ниже."""
    source = _source()
    other = _source(name="Другой", url="https://other.example")
    _article(source, "https://site.example/news/3", "Третья статья", "скрытое тело " * 20, hidden=True)
    decided = []
    real = repository.insert_verdict
    monkeypatch.setattr(repository, "insert_verdict",
                        lambda conn, rec, pending=(): decided.append(real(conn, rec, pending)) or decided[-1])

    def rec(src: dict, url: str, title: str, body: str) -> dict:
        return {"source_id": src["id"], "title": title, "url": url, "published_at": None,
                "raw_text": body, "text_truncated": False, "language": "ru", "content_hash": f"h-{url}"}

    base = "https://site.example/news"
    cases = [
        (rec(source, f"{base}/1?from=main", "Первая статья", DRILLING), verdicts.WOULD_INSERT),
        # Та же статья в другом написании: схема, www, хвостовой слэш.
        (rec(source, "http://www.site.example/news/1/?from=main", "Первая", "другое тело " * 20),
         verdicts.DUP_URL_KEY),
        # Ключ тот же, а адрес и заголовок другие: для вставки — тот же отказ.
        (rec(source, f"{base}/1?from=feed", "Совсем другая статья", "четвёртое тело " * 20),
         verdicts.DUP_URL_KEY),
        # То же тело у видимой статьи того же источника.
        (rec(source, f"{base}/2", "Вторая статья", DRILLING), verdicts.DUP_BODY_HASH),
        # То же тело у ДРУГОГО источника — не рубеж: перепечатки — задача дедупа.
        (rec(other, "https://other.example/2", "Перепечатка", DRILLING), verdicts.WOULD_INSERT),
        # Адрес уже занят скрытой статьёй: ключ и тело свободны, держит уникальность url.
        (rec(source, f"{base}/3", "Третья статья", "новое тело " * 20), verdicts.KNOWN),
    ]
    for record, expected in cases:
        with connection.read_only_connection() as ro:
            probed = real(ro, record)
        inserted = repository.insert_article(record)

        assert probed.verdict == expected, record["url"]
        assert decided[-1] == probed, f"insert_article решил не тем вердиктом: {record['url']}"
        assert inserted is (expected == verdicts.WOULD_INSERT), record["url"]


def test_request_probe_gives_every_verdict_and_matches_real_collection(isolated_db, monkeypatch):
    source = _source(listing_url="https://site.example/news", article_link_selector="a.news")
    base = "https://site.example/news"
    twin_page = _page("Статья с телом четвёртой", DRILLING * 3)
    _article(source, f"{base}/1", "Знакомая статья", "знакомое тело " * 20)
    _article(source, "http://www.site.example/news/2/", "Вторая статья в старом написании", "второе тело " * 20)
    _article(source, f"{base}/3?from=main", "Прошлогодний отчёт о бурении", "третье тело " * 20)
    _article(source, f"{base}/4", "Четвёртая статья", request_parser.parse_article_page(twin_page)[2])
    _article(source, f"{base}/5", "Скрытая гейтом", "пятое тело " * 20, hidden=True)
    paths = ["/news/1", "/news/2", "/news/3?from=feed", "/news/40", "/news/5", "/news/6",
             "/news/7", "/news/8", "/news/9", "/news/10", "/news/11"]
    pages = {
        base: _listing(paths),
        # /news/1 и /news/5 здесь нет: знакомый адрес сбор не качает.
        f"{base}/2": _page("Вторая статья в новом написании", "Новый текст второй статьи про бурение. " * 6),
        f"{base}/3?from=feed": _page("Новая буровая установка на шельфе", "Текст про буровую на шельфе. " * 8),
        f"{base}/40": twin_page,
        # /news/6 нет — страница не скачивается.
        f"{base}/7": _page("Короткая", "Коротко."),
        f"{base}/8": _page("Футбольный клуб выиграл матч чемпионата", FOOTBALL * 3),
        f"{base}/9": _page("Новая скважина дала первую нефть", DRILLING * 2),
        f"{base}/10": _page("Виджет вместо статьи десять", WIDGET * 4),
        f"{base}/11": _page("Виджет вместо статьи одиннадцать", WIDGET * 4),
    }
    monkeypatch.setattr(request_parser, "fetch", pages.get)
    before = _snapshot()

    report = source_probe.probe_source(source)

    assert report["read_only"] is True
    assert _snapshot() == before, "проба записала в базу"
    assert _verdicts(report) == [
        (f"{base}/1", verdicts.KNOWN),
        (f"{base}/2", verdicts.DUP_URL_KEY_SAME),
        (f"{base}/3?from=feed", verdicts.DUP_URL_KEY_OTHER),
        (f"{base}/40", verdicts.DUP_BODY_HASH),
        (f"{base}/5", verdicts.KNOWN),
        (f"{base}/6", verdicts.FETCH_FAILED),
        (f"{base}/7", verdicts.TOO_SHORT),
        (f"{base}/8", verdicts.PREFILTER),
        (f"{base}/9", verdicts.WOULD_INSERT),
        (f"{base}/10", verdicts.WOULD_INSERT),
        # Тело как у /news/10 из этого же прогона: сбор вставит десятую раньше.
        (f"{base}/11", verdicts.DUP_BODY_HASH),
    ]
    rows = {row["url"]: row for row in report["rows"]}
    assert rows[f"{base}/3?from=feed"]["holder"]["url"] == f"{base}/3?from=main"
    assert rows[f"{base}/5"]["holder"]["hidden"] is True
    assert rows[f"{base}/11"]["holder"]["position"] == 10
    assert rows[f"{base}/7"]["text_chars"] < config.MIN_ARTICLE_TEXT_CHARS
    assert "футбол" in rows[f"{base}/8"]["detail"]
    assert report["counts"][verdicts.WOULD_INSERT] == 2

    urls_before = _urls()
    stats = request_parser.parse_source(repository.get_source(source["id"]))

    assert _urls() - urls_before == _would_insert(report)
    assert stats["added"] == 2


def test_probe_shows_article_redirected_to_home_page_as_its_own_verdict(isolated_db, monkeypatch):
    """Сколково Energy: сайт отдаёт 301 со ЛЮБОЙ статьи на свою главную. Это не сбой загрузки —
    fetch_failed отправил бы искать маршрут (NL, браузер), а лечится сменой ленты или архивом.
    Проба показывает причину отдельным вердиктом с конечным адресом, сбор такое не вставляет."""
    source = _source(listing_url="https://site.example/news", article_link_selector="a.news")
    base = "https://site.example/news"
    home = _page("Школа управления — бизнес-образование", DRILLING * 3)
    pages = {base: _listing(["/news/1", "/news/2"]), f"{base}/1": home,
             f"{base}/2": _page("Новая скважина дала первую нефть", DRILLING * 2)}
    monkeypatch.setattr(request_parser, "fetch", pages.get)
    monkeypatch.setattr(request_parser, "final_url_of",
                        lambda url: "https://www.site.example/" if url == f"{base}/1" else url)

    report = source_probe.probe_source(source)

    assert _verdicts(report) == [(f"{base}/1", verdicts.REDIRECTED_HOME), (f"{base}/2", verdicts.WOULD_INSERT)]
    assert report["rows"][0]["detail"] == "→ https://www.site.example/"
    assert report["counts"][verdicts.REDIRECTED_HOME] == 1
    assert "redirected_home" in source_probe.format_report(report)

    urls_before = _urls()
    stats = request_parser.parse_source(repository.get_source(source["id"]))

    assert _urls() - urls_before == _would_insert(report) == {f"{base}/2"}
    assert stats["added"] == 1


def test_rss_probe_matches_real_collection(isolated_db, monkeypatch):
    source = _source(parse_strategy="rss", rss_url="https://feed.example/rss", url="https://feed.example")
    base = "https://feed.example/a"
    stub = "Анонс-заглушка ленты, одинаковый у всех пунктов про нефть."
    _article(source, f"{base}/1", "Статья один о добыче", "первое тело " * 20)
    _article(source, f"{base}/2", "Статья два о добыче", "второе тело " * 20, hidden=True)
    _article(source, f"{base}/3?from=main", "Статья три о бурении", "третье тело " * 20)
    _article(source, f"{base}/4", "Статья четыре", stub)
    feed = _feed([
        ("Статья один о добыче", f"{base}/1", "Анонс один про нефть."),                # тот же адрес, видима
        ("Статья два о добыче", f"{base}/2", "Анонс два про нефть."),                  # тот же адрес, скрыта
        ("Статья три о бурении", f"{base}/3?from=rss", "Анонс три про нефть."),        # хвост, тот же заголовок
        ("Совсем другая статья о шельфе", f"{base}/1?from=rss", "Анонс про шельф."),   # ключ статьи 1, статья другая
        ("Статья пять", f"{base}/5", stub),                                             # тело статьи 4
        ("Футбольный клуб выиграл матч чемпионата", f"{base}/6", FOOTBALL),
        ("Новая скважина дала нефть", f"{base}/7", DRILLING),
        ("Новая скважина дала нефть: повтор пункта", f"{base}/7", "Другой анонс про нефть."),
    ])
    monkeypatch.setattr(rss_parser, "fetch", lambda url: feed if url == source["rss_url"] else None)
    before = _snapshot()

    report = source_probe.probe_source(source)

    assert _snapshot() == before, "проба записала в базу"
    assert [verdict for _, verdict in _verdicts(report)] == [
        verdicts.DUP_URL_KEY_SAME, verdicts.KNOWN, verdicts.DUP_URL_KEY_SAME, verdicts.DUP_URL_KEY_OTHER,
        verdicts.DUP_BODY_HASH, verdicts.PREFILTER, verdicts.WOULD_INSERT, verdicts.DUP_URL_KEY_SAME,
    ]
    assert report["rows"][1]["holder"]["hidden"] is True
    assert report["rows"][7]["holder"]["position"] == 7, "повтор пункта упирается в пункт этого же прогона"

    urls_before = _urls()
    stats = rss_parser.parse_source(repository.get_source(source["id"]))

    assert _urls() - urls_before == _would_insert(report) == {f"{base}/7"}
    assert stats["added"] == 1


@pytest.mark.parametrize("broken", [
    "https://exa[mple.com/news/1",          # urlsplit: Invalid IPv6 URL
    "https://exa＃mple.com/news/1",     # «＃» по NFKC становится «#»: urlsplit отказывает
])
def test_malformed_link_breaks_neither_collection_nor_probe(isolated_db, monkeypatch, capsys, broken):
    """Ревью #72: разметка SAME/OTHER звала urlsplit без except ValueError внутри
    insert_verdict — то есть и в настоящей вставке. Статья с кривым адресом, собранная в
    прошлом цикле, в следующем роняла бы parse_source источника целиком (и complete итогов
    NL — 500). Прежний insert_article на ней просто возвращал False."""
    source = _source(parse_strategy="rss", rss_url="https://feed.example/rss", url="https://feed.example")
    _article(source, broken, "Статья с кривым адресом о бурении", "тело кривой статьи " * 20)
    feed = _feed([
        ("Статья с кривым адресом о бурении", broken, "Анонс про нефть."),
        ("Новая скважина дала нефть", "https://feed.example/a/2", DRILLING),
    ])
    monkeypatch.setattr(rss_parser, "fetch", lambda url: feed)

    stats = rss_parser.parse_source(repository.get_source(source["id"]))

    assert stats["added"] == 1, "сбор источника не должен падать на знакомой статье с кривым адресом"
    assert "https://feed.example/a/2" in _urls()

    cli.main(["source-probe", str(source["id"])])

    lines = capsys.readouterr().out.splitlines()
    assert any(verdicts.DUP_URL_KEY_SAME in line and broken in line for line in lines)
    assert lines[-1] == "итого 2: WOULD_INSERT=0 · DUP_URL_KEY_SAME=2"


@pytest.mark.parametrize("stored, fresh, expected", [
    # Ключ срезает якорь: две новости одной страницы пресс-центра — разные статьи.
    ("https://site.example/press/#n1", "https://site.example/press/#n2", verdicts.DUP_URL_KEY_OTHER),
    # Ключ снижает регистр всего адреса, а номер статьи в query бывает с регистром.
    ("https://site.example/news?id=AbC", "https://site.example/news?id=aBc", verdicts.DUP_URL_KEY_OTHER),
    ("https://site.example/News/Item", "https://site.example/news/item", verdicts.DUP_URL_KEY_OTHER),
    # Схема, www, регистр хоста и хвостовой слэш — оформление одного адреса.
    ("https://site.example/news/7", "http://www.SITE.example/news/7/", verdicts.DUP_URL_KEY_SAME),
])
def test_same_article_means_same_address_up_to_scheme_www_and_slash(isolated_db, monkeypatch, stored, fresh,
                                                                     expected):
    """Ревью #72: разметка повторяла нормализации ключа (регистр всего адреса, якорь), и
    разные статьи, склеенные ключом по якорю или регистру, получали SAME — потеря выглядела
    нормой. Заголовки здесь разные, так что вердикт решает только адрес."""
    source = _source(parse_strategy="rss", rss_url="https://feed.example/rss", url="https://feed.example")
    _article(source, stored, "Первая новость о бурении", "первое тело " * 20)
    feed = _feed([("Вторая новость о бурении", fresh, "Анонс про нефть.")])
    monkeypatch.setattr(rss_parser, "fetch", lambda url: feed)

    report = source_probe.probe_source(source)

    assert _verdicts(report) == [(fresh, expected)]
    assert report["rows"][0]["holder"]["url"] == stored


def test_telegram_probe_follows_last_seen_rules_of_collection(isolated_db, monkeypatch):
    """Состояние прошлого сбора собрано так, чтобы пройти все ветки: последний увиденный
    пост (11) в ленте есть, а его дата — позже поста 12 (так бывает, когда пост правили)."""
    source = _source(
        name="Канал", source_type="Telegram", url="https://t.me/probechan", parse_strategy="telegram",
        last_seen_article_url="https://t.me/probechan/11",
        last_seen_published_at=datetime(2026, 9, 20, 12, 30, tzinfo=timezone.utc),
    )
    _article(source, "https://t.me/probechan/13", "Пост тринадцать", "тело поста тринадцать " * 10)
    html = _telegram([
        (15, DRILLING, "2026-09-20T15:00:00+00:00"),
        (14, FOOTBALL, "2026-09-20T14:00:00+00:00"),
        (13, DRILLING + "Тринадцатый.", "2026-09-20T13:00:00+00:00"),
        (12, DRILLING + "Двенадцатый.", "2026-09-20T12:00:00+00:00"),
        (11, DRILLING + "Одиннадцатый.", "2026-09-20T11:00:00+00:00"),
        (10, DRILLING + "Десятый.", "2026-09-20T10:00:00+00:00"),
    ])
    monkeypatch.setattr(telegram_parser, "fetch", lambda url: html if url == "https://t.me/s/probechan" else None)

    report = source_probe.probe_source(source)

    assert [verdict for _, verdict in _verdicts(report)] == [
        verdicts.WOULD_INSERT, verdicts.PREFILTER, verdicts.KNOWN, verdicts.OLD, verdicts.KNOWN, verdicts.KNOWN,
    ]
    assert "последн" in report["rows"][4]["detail"] and "ниже" in report["rows"][5]["detail"]

    urls_before = _urls()
    stats = telegram_parser.parse_source(repository.get_source(source["id"]))

    assert _urls() - urls_before == _would_insert(report) == {"https://t.me/probechan/15"}
    assert stats["added"] == 1


def test_telegram_probe_shows_unchanged_listing_as_known(isolated_db, monkeypatch):
    html = _telegram([(21, DRILLING, "2026-09-21T10:00:00+00:00"), (20, DRILLING + "!", "2026-09-20T10:00:00+00:00")])
    posts = telegram_parser.extract_posts(html)
    source = _source(name="Канал", source_type="Telegram", url="https://t.me/probechan",
                     parse_strategy="telegram", last_listing_hash=telegram_parser._listing_hash(posts))
    monkeypatch.setattr(telegram_parser, "fetch", lambda url: html)

    report = source_probe.probe_source(source)

    assert [verdict for _, verdict in _verdicts(report)] == [verdicts.KNOWN, verdicts.KNOWN]
    assert "не изменилось" in report["rows"][0]["detail"]
    assert telegram_parser.parse_source(repository.get_source(source["id"]))["added"] == 0


def test_playwright_probe_goes_through_browser_path(isolated_db, monkeypatch):
    source = _source(parse_strategy="playwright", url="https://js.example",
                     listing_url="https://js.example/news", article_link_selector="a.news")
    base = "https://js.example/news"
    rendered = {
        base: _listing(["/news/1", "/news/2", "/news/3"]),
        f"{base}/1": _page("Страница без текста", "Загрузка…"),
        f"{base}/2": _page("Буровая установка вышла на новую скважину", DRILLING * 2),
    }
    calls = []
    monkeypatch.setattr(playwright_parser, "is_available", lambda: True)
    monkeypatch.setattr(playwright_parser, "fetch_rendered",
                        lambda url, settle_ms=0, **_: calls.append(url) or rendered.get(url))

    report = source_probe.probe_source(source)

    assert _verdicts(report) == [
        (f"{base}/1", verdicts.TOO_SHORT), (f"{base}/2", verdicts.WOULD_INSERT), (f"{base}/3", verdicts.FETCH_FAILED),
    ]
    assert calls.count(f"{base}/1") == 2, "короткий текст — вторая попытка с ожиданием дольше, как у сбора"

    urls_before = _urls()
    stats = playwright_parser.parse_source(repository.get_source(source["id"]))

    assert _urls() - urls_before == _would_insert(report)
    assert stats["added"] == 1


def test_probe_warns_when_source_is_collected_by_external_worker(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "FETCH_EXTERNAL_ENABLED", True)
    monkeypatch.setattr(config, "EXTERNAL_WORKERS_ENABLED", True)
    source = _source(parse_strategy="rss", rss_url="https://feed.example/rss", network_region="external")
    monkeypatch.setattr(rss_parser, "fetch", lambda url: None)

    report = source_probe.probe_source(source)

    assert report["listing"]["status"] == "fetch_failed"
    assert report["rows"] == []
    assert any("зарубежн" in note for note in report["notes"])


def test_source_probe_command_opens_only_read_only_connections(isolated_db, monkeypatch, capsys):
    source = _source(listing_url="https://site.example/news", article_link_selector="a.news")
    base = "https://site.example/news"
    _article(source, f"{base}/1", "Знакомая статья", "знакомое тело " * 20)
    pages = {base: _listing(["/news/1", "/news/2"]),
             f"{base}/2": _page("Новая скважина дала первую нефть", DRILLING * 2)}
    monkeypatch.setattr(request_parser, "fetch", pages.get)
    modes = []
    real_connect = repository.get_connection

    def connect():
        conn = real_connect()
        modes.append(conn.execute("SHOW transaction_read_only").fetchone()[0])
        return conn

    before = _snapshot()
    # Все соединения процесса: и репозитория, и модуля connection (соединение самой пробы).
    monkeypatch.setattr(repository, "get_connection", connect)
    monkeypatch.setattr(connection, "get_connection", connect)

    cli.main(["source-probe", str(source["id"])])

    out = capsys.readouterr().out
    assert modes and set(modes) == {"on"}, "проба открыла соединение, в котором можно писать"
    assert _snapshot() == before
    lines = out.splitlines()
    assert any(verdicts.KNOWN in line and f"{base}/1" in line for line in lines)
    assert any(verdicts.WOULD_INSERT in line and f"{base}/2" in line for line in lines)
    assert lines[-1] == "итого 2: WOULD_INSERT=1 · known=1"

    cli.main(["source-probe", str(source["id"]), "--json"])

    report = json.loads(capsys.readouterr().out)
    assert [row["verdict"] for row in report["rows"]] == [verdicts.KNOWN, verdicts.WOULD_INSERT]


def test_read_only_process_refuses_writes_and_lets_go_after(isolated_db):
    source = _source()

    with connection.read_only_process():
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            repository.touch_last_parsed(source["id"])

    repository.touch_last_parsed(source["id"])
    assert repository.get_source(source["id"])["last_parsed_at"] is not None


def test_report_table_caps_rows_per_verdict_but_not_rare_ones():
    def row(i: int, verdict: str) -> dict:
        return {"position": i, "verdict": verdict, "url": f"https://f.example/{i}", "title": f"Статья {i}",
                "text_chars": 100, "published_at": None, "detail": "",
                "holder": {"id": 1000 + i, "url": f"https://f.example/{i}", "title": f"Статья {i}",
                           "source_id": 1, "hidden": False, "position": None}}

    rows = [row(i, verdicts.DUP_URL_KEY_SAME) for i in range(1, 31)] + [row(31, verdicts.DUP_URL_KEY_OTHER)]
    report = {"source": {"id": 1, "name": "Лента", "parse_strategy": "rss", "enabled": True},
              "listing": {"url": "https://f.example/rss", "status": "ok"}, "notes": [], "read_only": True,
              "rows": rows, "counts": {verdicts.WOULD_INSERT: 0, verdicts.DUP_URL_KEY_SAME: 30,
                                       verdicts.DUP_URL_KEY_OTHER: 1}}

    lines = source_probe.format_report(report, rows_per_verdict=5).splitlines()

    assert sum(verdicts.DUP_URL_KEY_SAME in line and "https://f.example/" in line for line in lines) == 5
    assert any(verdicts.DUP_URL_KEY_OTHER in line for line in lines), "редкий вердикт не тонет в частом"
    assert any("ещё 25" in line for line in lines)
    assert lines[-1] == "итого 31: WOULD_INSERT=0 · DUP_URL_KEY_SAME=30 · DUP_URL_KEY_OTHER=1"
