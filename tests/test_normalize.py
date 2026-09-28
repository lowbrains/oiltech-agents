"""Тесты нормализации — чистые функции, без сети и БД."""

from datetime import datetime, timedelta, timezone

from oiltech_digest.ingestion import normalize


def test_clean_html_strips_tags_and_entities():
    assert normalize.clean_html("<p>Привет&nbsp;&amp; мир</p>") == "Привет & мир"
    assert normalize.clean_html("  a   b  ") == "a b"
    assert normalize.clean_html("") == ""
    assert normalize.clean_html(None) == ""


def test_content_hash_is_deterministic_and_normalizing():
    # Разный регистр заголовка, схема, utm и хвостовой слэш → одинаковый хеш
    h1 = normalize.compute_content_hash("Заголовок", "https://example.com/news/1?utm_source=x")
    h2 = normalize.compute_content_hash("заголовок", "http://example.com/news/1/")
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_content_hash_differs_for_different_content():
    a = normalize.compute_content_hash("A", "https://example.com/1")
    b = normalize.compute_content_hash("B", "https://example.com/2")
    assert a != b


def test_parse_date_valid_and_invalid():
    dt = normalize.parse_date({"published": "Wed, 07 May 2026 12:00:00 +0000"})
    assert dt is not None and dt.tzinfo is not None

    # Нет полей даты или мусор → None (статья всё равно сохранится)
    assert normalize.parse_date({"foo": "bar"}) is None
    assert normalize.parse_date({"published": "не дата вовсе"}) is None


def test_parse_date_naive_gets_utc():
    dt = normalize.parse_date({"published": "2026-05-07 12:00:00"})
    assert dt is not None and dt.tzinfo is not None


def test_is_future_date_flags_far_future_only():
    now = datetime.now(timezone.utc)
    assert normalize.is_future_date(now + timedelta(days=30)) is True
    assert normalize.is_future_date(now - timedelta(days=1)) is False
    assert normalize.is_future_date(now + timedelta(hours=1)) is False  # в пределах допуска
    assert normalize.is_future_date(None) is False


def test_parse_date_rejects_future_event_dates():
    # Дата-анонс из будущего (как Equinor «Q3 results — analyst conference») → None,
    # чтобы такие «события» не выдавались за дату публикации.
    future = (datetime.now(timezone.utc) + timedelta(days=120)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    assert normalize.parse_date({"published": future}) is None


# --- Страж принадлежности текста статье (задача №24) -------------------------------

def test_title_matches_body_accepts_own_text():
    assert normalize.title_matches_body(
        "Роснефть вложила 53 млрд рублей в проекты использования попутного газа",
        "Компания Роснефть направила 53 млрд рублей на проекты полезного использования "
        "попутного нефтяного газа в 2025 году, говорится в отчёте.",
    )


def test_title_matches_body_rejects_foreign_text():
    """Кейс владельца 28.07: заголовок про суд над TotalEnergies, тело — про клапан."""
    assert not normalize.title_matches_body(
        "TotalEnergies обжалует решение суда Франции о климатических целях компании",
        "Исследователи Пермского Политеха разработали цифровую модель обратного клапана "
        "для расчёта мощности нагревателя при предотвращении обледенения на промыслах.",
    )


def test_title_matches_body_ignores_publisher_suffix():
    """Регресс на ЛОЖНОЕ срабатывание, найденное замером 28.07: хвост издания
    («— Новости о нефти и газе в России и мире») топил долю совпадения и заставлял
    стража резать ВЕРНЫЕ статьи OilCapital — 19.6% его ленты."""
    assert normalize.title_matches_body(
        "Сахалин надеется построить свой НПЗ к 2030 году — Новости о нефти и газе в России и мире",
        "На Сахалине планируется создание собственного нефтеперерабатывающего завода, "
        "который должен быть построен к 2030 году, сообщил губернатор.",
    )


def test_title_matches_body_skips_too_short_titles():
    """«SOCAR» или «May 2026» судить не позволяют — страж обязан пропускать, а не резать."""
    assert normalize.title_matches_body("SOCAR", "Совершенно посторонний длинный текст про Баку.")
    assert normalize.title_matches_body("May 2026", "Unrelated content about pipelines and rigs.")


def test_compute_body_hash_ignores_whitespace_but_separates_texts():
    a = normalize.compute_body_hash("Текст статьи   про\nнефть")
    assert a == normalize.compute_body_hash("Текст статьи про нефть")
    assert a != normalize.compute_body_hash("Другой текст про газ")
    assert normalize.compute_body_hash("") is None
    assert normalize.compute_body_hash(None) is None


def test_strip_emoji_removes_default_emoji_from_title():
    """Телеграм-заголовок лепится из первого предложения поста, и «🔥» едет в дайджест."""
    assert normalize.strip_emoji("🔥 Срочно! Роснефть запустила установку") == (
        "Срочно! Роснефть запустила установку"
    )
    assert normalize.strip_emoji("Итоги ✅ и 🚀 планы") == "Итоги и планы"


def test_strip_emoji_keeps_meaningful_symbols():
    """Замер 12.09: наивная регулярка на \\p{Emoji} дала 100% ложных именно на этих знаках.

    Стрелки и градусы несут смысл в отраслевых формулировках — резать их нельзя.
    """
    legit = "Добыча ↓ 3% при ±5 °C — Baker Hughes © 2026, ГОСТ™ и ® знак, проверено ✓"
    assert normalize.strip_emoji(legit) == legit


def test_strip_emoji_drops_modifiers_but_keeps_base_glyph():
    """VS16/ZWJ/тон кожи уходят всегда; базовый текстовый символ остаётся читаемым."""
    assert normalize.strip_emoji("Итоги ⚠️ риски") == "Итоги ⚠ риски"
    assert normalize.strip_emoji("1️⃣ Первый пункт") == "1 Первый пункт"
    assert normalize.strip_emoji("Команда 👨‍👩‍👧‍👦 и 👍🏽 результат") == (
        "Команда и результат"
    )


def test_strip_emoji_collapses_gap_and_handles_empty():
    """На месте вырезанного не должно оставаться двойных пробелов и краевого мусора."""
    assert normalize.strip_emoji("Роснефть 🔥 — запустила") == "Роснефть — запустила"
    assert normalize.strip_emoji("") == ""
    assert normalize.strip_emoji(None) == ""


# Адреса — с прода 25.09: у этих источников статья опознаётся ТОЛЬКО по query, а ключ
# без query склеивал все их статьи в одну. Проба на ядре: Минэнерго — 25 пунктов ленты
# из 25 отбиты как «дубль» чужой статьи, EIA — 17 из 18, РГУ Губкина — 257 из 260,
# свежие релизы Лукойла и Новатэка — тоже.
QUERY_IDENTIFIED = [
    ("https://lukoil.ru/PressCenter/Pressreleases/Pressrelease?rid=740957",
     "https://lukoil.ru/PressCenter/Pressreleases/Pressrelease?rid=739353"),
    ("https://www.eia.gov/todayinenergy/detail.php?id=68184",
     "https://www.eia.gov/todayinenergy/detail.php?id=68164"),
    ("https://minenergo.gov.ru/press-center/news-and-events?news-item=minenergo-rossii-i-obshchestvo-znanie",
     "https://minenergo.gov.ru/press-center/news-and-events?news-item=sergey-tsivilev-voprosy-osvoeniya"),
    ("https://www.novatek.ru/ru/press/releases/index.php?id_4=7882",
     "https://www.novatek.ru/ru/press/releases/index.php?id_4=7850"),
    ("https://en.antonoil.com/index.php?m=content&c=index&a=show&catid=90&id=4023",
     "https://en.antonoil.com/index.php?m=content&c=index&a=show&catid=88&id=4022"),
    ("https://gubkin.ru/news/detail.php?ID=57931", "https://gubkin.ru/news/detail.php?ID=57075"),
]


def test_url_key_separates_articles_identified_by_query():
    for first, second in QUERY_IDENTIFIED:
        assert normalize.url_key(first) != normalize.url_key(second), first


def test_url_key_still_collapses_spellings_of_one_article():
    """Три причины 940 копий (13.09) по-прежнему дают один ключ, и к ним — хвосты,
    найденные замером параметров прода 25.09: `ysclid` (Яндекс) и подписи `gaa_*`."""
    same = [
        "https://www.rbc.ru/politics/24/08/2026/6a8c1725?from=newsfeed",
        "https://www.rbc.ru/politics/24/08/2026/6a8c1725?from=main_lines_11",
        "http://rbc.ru/politics/24/08/2026/6a8c1725/",
        "https://rbc.ru/politics/24/08/2026/6a8c1725?utm_source=telegram&utm_medium=social",
        "https://rbc.ru/politics/24/08/2026/6a8c1725?ysclid=m3c6ls3kqj386163690",
        "https://rbc.ru/politics/24/08/2026/6a8c1725?gaa_at=eafs&gaa_n=AWEts&gaa_ts=69c4&gaa_sig=I_rv",
        "https://rbc.ru/politics/24/08/2026/6a8c1725#comments",
    ]
    assert len({normalize.url_key(url) for url in same}) == 1


def test_url_key_ignores_parameter_order_and_tracking_next_to_identity():
    assert normalize.url_key("https://en.antonoil.com/index.php?id=4023&catid=90&a=show") == \
        normalize.url_key("https://en.antonoil.com/index.php?a=show&catid=90&id=4023")
    assert normalize.url_key("https://lukoil.ru/Pressrelease?rid=740957&utm_source=tg") == \
        normalize.url_key("https://lukoil.ru/Pressrelease?rid=740957")


def test_fetch_url_tracking_params_are_also_stripped_from_identity_key():
    """`request_parser` чистит адрес для скачивания своим списком. Имя, которое там срезается,
    а в ключе остаётся, развело бы одну статью из ленты и из листинга на два ключа."""
    from oiltech_digest.ingestion import request_parser

    for name in request_parser._TRACKING_PARAMS:
        assert name in normalize._TRACKING_QUERY_PARAMS or name.startswith(normalize._TRACKING_QUERY_PREFIXES), name
