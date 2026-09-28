from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oiltech_digest.db import connection
from oiltech_digest.ingestion import source_overrides


def _add_source(conn, name: str, **fields) -> int:
    columns = {"name": name, "source_type": "News", "enabled": True, **fields}
    keys = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    return conn.execute(
        f"INSERT INTO sources ({keys}) VALUES ({placeholders}) RETURNING id",
        tuple(columns.values()),
    ).fetchone()[0]


def test_apply_overrides_changes_only_config_and_resets_dedup_state(isolated_db, monkeypatch):
    """Оверрайд меняет конфиг источника, сбрасывает дедуп-состояние и НЕ трогает статьи.

    Реестр применяется на КАЖДОМ деплое (bootstrap → apply-source-overrides), поэтому три
    свойства критичны:
    1) статьи не затрагиваются — механизм не может потерять корпус;
    2) при смене listing_url/стратегии сбрасываются last_listing_hash и last_seen_* — иначе
       первый парс по новому URL закоротит на старом хэше от прежней попытки и добавит 0
       (ровно эти «замораживатели» уже ломали сбор, см. историю анти-заморозки);
    3) идемпотентность — повторный прогон ничего не трогает (иначе каждый деплой сбрасывал бы
       дедуп-состояние всем источникам и провоцировал перезагрузку старых статей).
    """
    now = datetime.now(timezone.utc)

    with connection.get_connection() as conn:
        source_id = _add_source(
            conn,
            "Тестовый источник",
            url="https://example.com",
            parse_strategy="request",
            listing_url="https://example.com",          # старый (неверный) листинг — главная
            last_listing_hash="старый-хэш",
            last_seen_article_url="https://example.com/old",
            last_seen_published_at=now - timedelta(days=5),
        )
        article_id = conn.execute(
            """
            INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language)
            VALUES (%s, 'Статья', 'https://example.com/a', %s, %s, 'text', 'ru')
            RETURNING id
            """,
            (source_id, now, now),
        ).fetchone()[0]
        conn.commit()

    monkeypatch.setattr(
        source_overrides,
        "SOURCE_OVERRIDES",
        {"Тестовый источник": {"parse_strategy": "request",
                               "listing_url": "https://example.com/press-center"}},
    )

    stats = source_overrides.apply_overrides()
    assert stats["changed"] == 1
    assert stats["not_found"] == 0

    with connection.get_connection() as conn:
        row = conn.execute(
            """
            SELECT listing_url, parse_strategy, last_listing_hash,
                   last_seen_article_url, last_seen_published_at
            FROM sources WHERE id = %s
            """,
            (source_id,),
        ).fetchone()
        articles_left = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        article_still_there = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE id = %s", (article_id,)
        ).fetchone()[0]

    listing_url, strategy, listing_hash, seen_url, seen_at = row
    assert listing_url == "https://example.com/press-center"   # конфиг применён
    assert strategy == "request"
    # Дедуп-состояние сброшено — иначе новый листинг не дал бы ни одной статьи.
    assert listing_hash is None
    assert seen_url is None
    assert seen_at is None
    # Статьи не тронуты — механизм не может потерять корпус.
    assert articles_left == 1
    assert article_still_there == 1

    # Идемпотентность: второй прогон ничего не меняет.
    stats_again = source_overrides.apply_overrides()
    assert stats_again["changed"] == 0
    assert stats_again["unchanged"] == 1


def test_apply_overrides_reports_unknown_source_name(isolated_db, monkeypatch):
    """Неизвестное имя не роняет применение, но и НЕ применяется молча — считается в not_found.

    Реестр ключуется точным sources.name: опечатка в имени = оверрайд просто не сработает.
    На проде это выглядит как «починил, а источник всё так же молчит», поэтому счётчик
    not_found — единственный сигнал. В bootstrap-логе деплоя он должен быть 0.
    """
    monkeypatch.setattr(
        source_overrides,
        "SOURCE_OVERRIDES",
        {"Источник Которого Нет": {"parse_strategy": "rss", "rss_url": "https://example.com/rss"}},
    )

    stats = source_overrides.apply_overrides()

    assert stats["not_found"] == 1
    assert stats["changed"] == 0


def test_source_overrides_registry_is_well_formed():
    """Гигиена самого реестра — ловит опечатки до деплоя.

    Реестр правится руками и применяется на проде без ревью данных, поэтому проверяем:
    у каждой записи есть parse_strategy из известного набора, URL-поля выглядят как URL,
    имена не задублированы (dict-литерал молча схлопнул бы дубль ключа).
    """
    registry = source_overrides.SOURCE_OVERRIDES
    assert registry, "реестр не должен быть пустым"

    allowed_strategies = {"rss", "request", "playwright", "telegram", "none"}
    for name, fields in registry.items():
        assert name.strip() == name, f"{name!r}: лишние пробелы в имени — не совпадёт с sources.name"
        assert "parse_strategy" in fields, f"{name!r}: parse_strategy обязателен"
        assert fields["parse_strategy"] in allowed_strategies, f"{name!r}: неизвестная стратегия"
        for key in ("listing_url", "rss_url", "url"):
            value = fields.get(key)
            if value is not None:
                assert value.startswith(("http://", "https://")), f"{name!r}.{key}: не URL"
        region = fields.get("network_region")
        if region is not None:
            assert region in {"auto", "ru", "external"}, f"{name!r}: неизвестный network_region"
        source_type = fields.get("source_type")
        if source_type is not None:
            assert source_type and source_type.strip() == source_type, \
                f"{name!r}: source_type пустой или с лишними пробелами"


def test_source_overrides_agree_with_the_seed_sheet():
    """Реестр сверяется с ЕДИНСТВЕННЫМ источником правды об источниках — Excel-сидером.

    Две проверки, обе против одного и того же дефекта: оверрайд, который молча не применится.
    1) Имя, у которого в сидере ДВЕ строки (издание живёт сайтом и telegram-каналом под общим
       именем), обязано нести `source_type` — иначе apply_overrides отбросит его как
       неоднозначный. Список таких имён НЕ захардкожен, а вычисляется из книги: появится
       третий двойник — тест потребует source_type и для него.
    2) Пара (name, source_type) обязана существовать в книге. Опечатка в типе («media»,
       «News» вместо «Media») даёт not_found, то есть тихий no-op на проде.

    Захардкоженный по памяти список допустимых типов здесь был бы хуже бесполезного: в
    колонке «Тип» 33 различных значения («Company / NOC», «University / R&D», «Analytics»…),
    и такой список отбраковывал бы валидные записи, а не ловил ошибки.
    """
    import collections

    import openpyxl

    registry = source_overrides.SOURCE_OVERRIDES
    workbook = openpyxl.load_workbook(
        Path(__file__).resolve().parent.parent
        / "data" / "seed" / "1_Список_источников_для_дайджеста.xlsx"
    )
    rows = list(workbook["Sources_Expanded"].iter_rows(values_only=True))
    header = rows[0]
    name_col, type_col = header.index("Источник"), header.index("Тип")

    seeded_types: dict[str, list[str]] = collections.defaultdict(list)
    for row in rows[1:]:
        if row[name_col] and row[type_col]:
            seeded_types[str(row[name_col]).strip()].append(str(row[type_col]).strip())

    for name, fields in registry.items():
        variants = seeded_types.get(name)
        if not variants:
            continue  # источник заведён не через сидер — сверять не с чем
        if len(variants) > 1:
            assert "source_type" in fields, (
                f"{name!r}: в сидере {len(variants)} строки ({', '.join(sorted(variants))}) — "
                f"без source_type оверрайд будет отброшен как неоднозначный"
            )
        declared = fields.get("source_type")
        if declared is not None:
            assert declared in variants, (
                f"{name!r}: source_type={declared!r} не встречается в сидере "
                f"(есть: {', '.join(sorted(variants))}) — оверрайд не найдёт строку"
            )

    # Дубль ключа в dict-литерале молча теряется — ловим по исходнику.
    source = Path(source_overrides.__file__).read_text(encoding="utf-8")
    body = source[source.index("SOURCE_OVERRIDES"):]
    keys = re.findall(r'^\s{4}"([^"]+)":\s*\{', body, re.M)
    duplicates = {key for key in keys if keys.count(key) > 1}
    assert not duplicates, f"дубли ключей в реестре: {sorted(duplicates)}"


def test_apply_overrides_picks_row_by_source_type_when_name_is_not_unique(isolated_db, monkeypatch):
    """При неуникальном `name` оверрайд обязан лечь на строку, выбранную по `source_type`.

    Естественный ключ таблицы — пара `(name, source_type)`: одно издание живёт двумя
    строками (сайт + telegram-канал) под ОДНИМ именем — «РБК Энергетика», «Интерфакс ТЭК»,
    «Neftegaz.ru». Пока в реестре не было ни одного такого имени, дефект был латентным:
    поиск шёл `WHERE name = %s` c `.fetchone()` — без `ORDER BY` и без `source_type`, то есть
    какая строка вернётся, решал план запроса. Прогон на временной БД с seq scan клал
    оверрайд на ВЫКЛЮЧЕННУЮ telegram-строку: `changed=1`, а починен не тот источник.

    Здесь проверяется обе половины: нужная строка изменена И телеграм-двойник не тронут
    (его `url`, `parse_strategy` и `enabled` — это отдельный рабочий источник, порча
    которого выглядела бы как «оверрайд применился успешно»).
    """
    with connection.get_connection() as conn:
        site_id = _add_source(
            conn, "РБК Энергетика", source_type="Media",
            url="https://www.rbc.ru", parse_strategy="request", enabled=True,
        )
        telegram_id = _add_source(
            conn, "РБК Энергетика", source_type="Telegram",
            url="https://t.me/rbc_energy", parse_strategy="telegram", enabled=False,
        )
        conn.commit()

    monkeypatch.setattr(
        source_overrides,
        "SOURCE_OVERRIDES",
        {"РБК Энергетика": {"source_type": "Media", "parse_strategy": "request",
                            "listing_url": "https://www.rbc.ru/tags/?tag=нефть+и+газ"}},
    )

    stats = source_overrides.apply_overrides()
    assert stats["changed"] == 1
    assert stats["not_found"] == 0
    assert stats["ambiguous"] == 0

    with connection.get_connection() as conn:
        site = conn.execute(
            "SELECT listing_url, parse_strategy, url, enabled FROM sources WHERE id = %s",
            (site_id,),
        ).fetchone()
        telegram = conn.execute(
            "SELECT listing_url, parse_strategy, url, enabled FROM sources WHERE id = %s",
            (telegram_id,),
        ).fetchone()

    # Строка сайта — починена.
    assert site == ("https://www.rbc.ru/tags/?tag=нефть+и+газ", "request", "https://www.rbc.ru", True)
    # Телеграм-двойник — нетронут целиком.
    assert telegram == (None, "telegram", "https://t.me/rbc_energy", False)


def test_apply_overrides_refuses_ambiguous_name_instead_of_guessing(isolated_db, monkeypatch):
    """Имя без `source_type`, совпавшее с НЕСКОЛЬКИМИ строками, не применяется вслепую.

    Молчаливый выбор «какой-нибудь» строки — худший исход: счётчик покажет `changed=1`,
    а починен будет не тот источник (и заодно испорчен второй). Поэтому неоднозначность —
    это отказ с именем в отчёте, а не монетка.
    """
    with connection.get_connection() as conn:
        site_id = _add_source(conn, "Neftegaz.ru", source_type="Media",
                              url="https://neftegaz.ru", parse_strategy="rss")
        telegram_id = _add_source(conn, "Neftegaz.ru", source_type="Telegram",
                                  url="https://t.me/neftegazchannel", parse_strategy="telegram")
        conn.commit()

    monkeypatch.setattr(
        source_overrides,
        "SOURCE_OVERRIDES",
        {"Neftegaz.ru": {"parse_strategy": "request", "listing_url": "https://neftegaz.ru/news/"}},
    )

    stats = source_overrides.apply_overrides()

    assert stats["ambiguous"] == 1
    assert stats["changed"] == 0
    assert "Neftegaz.ru" in stats["ambiguous_names"]

    with connection.get_connection() as conn:
        rows = conn.execute(
            "SELECT parse_strategy, listing_url FROM sources WHERE id IN (%s, %s) ORDER BY id",
            (site_id, telegram_id),
        ).fetchall()
    # Ни одна из двух строк не тронута.
    assert rows == [("rss", None), ("telegram", None)]


def test_apply_overrides_reports_missing_names_not_just_a_counter(isolated_db, monkeypatch):
    """Не найденное имя возвращается СПИСКОМ, а не только числом.

    Вызов в bootstrap обёрнут в `|| true`, а CLI печатал лишь счётчик — промах по имени
    выглядел как успешный деплой. Чтобы промах было видно, нужно само имя.
    """
    monkeypatch.setattr(
        source_overrides,
        "SOURCE_OVERRIDES",
        {"Источник Которого Нет": {"parse_strategy": "rss", "rss_url": "https://example.com/rss"}},
    )

    stats = source_overrides.apply_overrides()

    assert stats["not_found"] == 1
    assert stats["missing_names"] == ["Источник Которого Нет"]


def test_apply_overrides_source_type_mismatch_is_reported_as_missing(isolated_db, monkeypatch):
    """Запись с `source_type`, которого нет у источника, — промах, а не тихое применение.

    Опечатка в `source_type` («Мedia», «news») не должна деградировать до поиска по одному
    имени: иначе сужение ключа, добавленное ради безопасности, само стало бы источником
    случайного попадания.
    """
    with connection.get_connection() as conn:
        source_id = _add_source(conn, "Интерфакс ТЭК", source_type="Media",
                                url="https://www.interfax.ru", parse_strategy="rss")
        conn.commit()

    monkeypatch.setattr(
        source_overrides,
        "SOURCE_OVERRIDES",
        {"Интерфакс ТЭК": {"source_type": "Telegram", "parse_strategy": "telegram",
                           "url": "https://t.me/interfax_energy"}},
    )

    stats = source_overrides.apply_overrides()

    assert stats["not_found"] == 1
    assert stats["changed"] == 0
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT parse_strategy, url FROM sources WHERE id = %s", (source_id,)
        ).fetchone()
    assert row == ("rss", "https://www.interfax.ru")


def test_apply_overrides_sets_listing_selector(isolated_db, monkeypatch):
    """Реестр умеет задавать `listing_selector` — без него JS-листинг чинится наполовину.

    Случай РБК (#61): выдача тега рисуется на JS, поэтому нужен playwright. Но на
    отрендеренной странице ДВА блока ссылок — сама выдача тега (`div.search-item__wrap`,
    нефтегазовые заголовки) и сквозной сайдбар общей ленты (`div.js-news-feed-list`,
    Марадона и теннисист). Кандидаты собираются из обоих, и по очкам сайдбар выигрывает —
    замер на проде 24.08 дал 8 кандидатов из сайдбара и ноль из выдачи тега.

    `extract_candidate_links` при заданном селекторе возвращает ТОЛЬКО узлы из него
    (`explicit[:limit]`), то есть селектор — единственный способ отсечь сайдбар. Раз чинить
    источник положено через реестр, реестр обязан уметь и это поле.
    """
    with connection.get_connection() as conn:
        source_id = _add_source(
            conn, "РБК Энергетика", source_type="Media",
            url="https://www.rbc.ru", parse_strategy="request",
        )
        conn.commit()

    monkeypatch.setattr(
        source_overrides,
        "SOURCE_OVERRIDES",
        {"РБК Энергетика": {"source_type": "Media", "parse_strategy": "playwright",
                            "listing_url": "https://www.rbc.ru/tags/?tag=нефть",
                            "listing_selector": ".search-item__wrap"}},
    )

    stats = source_overrides.apply_overrides()
    assert stats["changed"] == 1

    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT parse_strategy, listing_url, listing_selector FROM sources WHERE id = %s",
            (source_id,),
        ).fetchone()
    assert row == ("playwright", "https://www.rbc.ru/tags/?tag=нефть", ".search-item__wrap")

    # Идемпотентность: селектор не должен провоцировать «изменено» на каждом деплое.
    assert source_overrides.apply_overrides()["changed"] == 0


def test_apply_overrides_sets_listing_strategy(isolated_db, monkeypatch):
    """Реестр умеет задавать `listing_strategy` — иначе порядок ленты Petronas не починить.

    Свежайший релиз Petronas стоит первым в ленте, но без даты, и общее правило порядка
    (датированные вперёд) уводило его за лимит 12. Лечится признаком источника «порядок
    страницы и есть свежесть» — `listing_strategy='page_order'`. Колонка в `sources` есть
    давно (правится и в админке), но реестр её не знал: прописанная, она молча не доехала
    бы до базы — так уже было с селекторами JPT.
    """
    with connection.get_connection() as conn:
        source_id = _add_source(
            conn, "Petronas", source_type="Company / NOC", url="https://www.petronas.com",
            parse_strategy="request", listing_url="https://www.petronas.com/media/media-releases",
            last_listing_hash="старый-хэш",
        )
        conn.commit()

    monkeypatch.setattr(
        source_overrides,
        "SOURCE_OVERRIDES",
        {"Petronas": {"parse_strategy": "request",
                      "listing_url": "https://www.petronas.com/media/media-releases",
                      "article_link_selector": 'div[role="article"] a[href*="/media/media-releases/"]',
                      "listing_strategy": "page_order"}},
    )

    stats = source_overrides.apply_overrides()
    assert stats["changed"] == 1

    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT listing_strategy, article_link_selector, last_listing_hash FROM sources WHERE id = %s",
            (source_id,),
        ).fetchone()
    assert row == ("page_order", 'div[role="article"] a[href*="/media/media-releases/"]', None)

    # Идемпотентность: повторный деплой не сбрасывает дедуп-состояние заново.
    assert source_overrides.apply_overrides()["changed"] == 0


def test_page_order_in_registry_always_comes_with_a_selector():
    """`page_order` без селектора ничего не делает: без него первыми на странице идут
    шапка и меню, и разбор его намеренно не слушает. Запись без селектора — ошибка реестра."""
    for name, fields in source_overrides.SOURCE_OVERRIDES.items():
        strategy = fields.get("listing_strategy")
        if strategy is None:
            continue
        assert strategy == "page_order", f"{name!r}: неизвестный listing_strategy {strategy!r}"
        assert fields.get("listing_selector") or fields.get("article_link_selector"), (
            f"{name!r}: page_order без селектора ленты не работает")
