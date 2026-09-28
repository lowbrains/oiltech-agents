from __future__ import annotations

from datetime import datetime, timedelta, timezone

from oiltech_digest.db import connection
from oiltech_digest.ingestion import telegram_titles

# Частоты «в скольких статьях встречается слово»: склеенное слово — только в своей статье.
FREQUENCY = {
    "директоров": 40, "президент": 300, "иллинойсе": 3, "exxonmobil": 50, "exxon": 4, "mobil": 4,
    "казмунайгаз": 120, "каз": 1, "мунай": 1, "газ": 900, "квт": 60, "к": 5000,
    "татнефть": 90, "утроилась": 1, "рус": 2, "гидро": 30, "фото": 40, "дня": 400, "откуда": 50,
    "мега": 12, "фон": 25, "оператор": 80, "компании": 900, "казмунай": 0,
}


def test_glued_title_is_cut_at_the_restored_line_break():
    title = "Газпрому разрешили не соблюдать требования по числу независимых директоровПрезидент РФ В."

    assert telegram_titles.repaired_title(title, FREQUENCY) == (
        "Газпрому разрешили не соблюдать требования по числу независимых директоров"
    )


def test_brands_and_units_are_not_cut():
    for title in (
        "КазМунайГаз увеличил добычу на месторождениях Западного Казахстана",
        "Канадец заплатил за зарядку 4,91 кВт⋅ч электроэнергии 22 000 долларов",
        "ExxonMobil остановила НПЗ в Иллинойсе после аварии на установке",
    ):
        assert telegram_titles.repaired_title(title, FREQUENCY) == title


def test_glue_after_a_brand_is_found_inside_the_same_word():
    title = "❗️ ExxonMobil остановила НПЗ в ИллинойсеExxonMobil полностью остановила"

    assert telegram_titles.repaired_title(title, FREQUENCY) == "❗️ ExxonMobil остановила НПЗ в Иллинойсе"


def test_rare_left_word_is_cut_when_the_next_line_starts_with_a_common_word():
    title = "Добыча сланцевой нефти в Пермском бассейне утроиласьТатнефть сообщила о планах"

    assert telegram_titles.repaired_title(title, FREQUENCY) == "Добыча сланцевой нефти в Пермском бассейне утроилась"


def test_short_heading_is_joined_with_the_next_line_by_a_space():
    title = "#ФотоДняОткуда на Урале старинный замок из красного кирпича?"

    # Хэштег рубрики («#ФотоДня») слитный с заглавными — режем только последний стык.
    assert telegram_titles.split_glued_lines(title, FREQUENCY) == ["#ФотоДня", "Откуда на Урале старинный замок из красного кирпича?"]
    assert telegram_titles.repaired_title(title, FREQUENCY) == "#ФотоДня Откуда на Урале старинный замок из красного кирпича?"


def test_link_glued_to_a_word_ends_the_title():
    title = "Метрологическое обеспечение нефтегазохимического комплекса обсудили в Казаниhttps://t.me/x"

    assert telegram_titles.repaired_title(title, FREQUENCY) == (
        "Метрологическое обеспечение нефтегазохимического комплекса обсудили в Казани"
    )


def test_brand_before_the_line_break_is_kept_whole():
    """Ревью 25.09: брался первый стык — «КазМунайГазПрезидент» резался после «КазМунай»."""
    title = "Добыча на месторождениях выросла, сообщил КазМунайГазПрезидент компании"

    assert telegram_titles.repaired_title(title, FREQUENCY) == "Добыча на месторождениях выросла, сообщил КазМунайГаз"


def test_longest_known_brand_before_the_line_break_is_kept():
    """Ревью 27.09: «Газпром» известно, но строка кончается на «ГазпромБанк»."""
    frequency = {**FREQUENCY, "газпром": 800, "газпромбанк": 60, "банк": 700}
    title = "Кредитное соглашение подписано с ГазпромБанкПрезидент банка сообщил"

    assert telegram_titles.repaired_title(title, frequency) == "Кредитное соглашение подписано с ГазпромБанк"


def test_missing_space_after_a_short_word_is_not_a_line_break():
    """Ревью 27.09: «добычу вЯНАО» резалось после «в» — строка не кончается предлогом."""
    frequency = {**FREQUENCY, "в": 9000, "на": 9000, "для": 3000, "e": 50, "янао": 200, "ямале": 100}
    for title in (
        "Компания нарастила добычу вЯНАО на 12% за первое полугодие",
        "Новые проекты по добыче газа запущены наЯмале в этом году",
        "Началась сертификация аэротакси дляЯНАО и для eVTOL в этом году",
    ):
        assert telegram_titles.repaired_title(title, frequency) == title
    # А короткой единицей строка кончается (прогон на проде 27.09), и вопросом-рубрикой тоже.
    headings = {**frequency, "когда": 900, "рассказываем": 300, "как": 5000, "эксперты": 200}
    assert telegram_titles.repaired_title("Выставки осени: где и когдаРассказываем о главных событиях", headings) == (
        "Выставки осени: где и когда"
    )
    assert telegram_titles.repaired_title("Новые правила для нефтяников: что и какЭксперты оценили", headings) == (
        "Новые правила для нефтяников: что и как"
    )
    units = {**frequency, "т": 900, "км": 700}
    assert telegram_titles.repaired_title("Установлен кормовой блок массой 215 тМасса установки выросла", units) == (
        "Установлен кормовой блок массой 215 т"
    )
    assert telegram_titles.repaired_title("ТНПА испытали для работы на глубине до 3 кмВ Санкт-Петербурге прошли", units) == (
        "ТНПА испытали для работы на глубине до 3 км"
    )


def test_short_capitalized_prefix_is_a_brand_not_a_line_end():
    # «Мега» известна корпусу, но «Мега|Фон» — бренд, а не конец строки.
    title = "Оператор МегаФон запустил связь на нефтяном месторождении"

    assert telegram_titles.repaired_title(title, FREQUENCY) == title


def test_document_frequency_counts_articles_not_occurrences(isolated_db):
    with connection.get_connection() as conn:
        source = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('S', 'News', 'https://example.com', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        for index, text in enumerate(("Нефть, нефть и ещё раз нефть", "Нефть подорожала")):
            conn.execute(
                "INSERT INTO articles (source_id, title, url, raw_text, language, content_hash) VALUES (%s, %s, %s, %s, 'ru', %s)",
                (source, f"Заголовок {index}", f"https://example.com/{index}", text, f"h{index}"),
            )
        conn.commit()
        frequency = telegram_titles.document_frequency(conn)

    assert frequency["нефть"] == 2
    assert frequency["заголовок"] == 2


def test_repair_updates_title_and_copied_title_ru_only(isolated_db):
    glued = "Газпрому разрешили не соблюдать требования по числу независимых директоровПрезидент РФ В."
    fixed = "Газпрому разрешили не соблюдать требования по числу независимых директоров"
    with connection.get_connection() as conn:
        tg = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('Канал', 'Telegram', 'https://t.me/s/chan', TRUE, 'telegram') RETURNING id"
        ).fetchone()[0]
        rss = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('Лента', 'RSS', 'https://example.com', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        ids = [
            conn.execute(
                "INSERT INTO articles (source_id, title, url, raw_text, language, content_hash) "
                "VALUES (%s, %s, %s, 'текст', 'ru', %s) RETURNING id",
                (source, title, url, url),
            ).fetchone()[0]
            for source, title, url in (
                (tg, glued, "https://t.me/chan/1"),
                (tg, glued, "https://t.me/chan/2"),
                (rss, glued, "https://example.com/1"),
            )
        ]
        conn.execute("INSERT INTO article_cards (article_id, title_ru) VALUES (%s, %s)", (ids[0], glued))
        conn.execute("INSERT INTO article_cards (article_id, title_ru) VALUES (%s, 'Свой перевод')", (ids[1],))
        conn.commit()

    dry = telegram_titles.repair(apply=False, frequency=FREQUENCY)
    assert (dry["scanned"], dry["changed"]) == (2, 2)
    with connection.get_connection() as conn:
        assert conn.execute("SELECT title FROM articles WHERE id = %s", (ids[0],)).fetchone()[0] == glued
        # title_ru отличается от заголовка только двойником (так бывает между заголовком и
        # карточкой после repair-terminology --scripts-only) — это всё ещё копия заголовка.
        conn.execute("UPDATE article_cards SET title_ru = %s WHERE article_id = %s", (glued.replace("о", "o", 1), ids[0]))
        conn.commit()

    earlier = telegram_titles.repair(
        apply=False, frequency=FREQUENCY, collected_before=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert earlier["scanned"] == 0  # собранное новым парсером эвристикой не трогаем

    telegram_titles.repair(apply=True, frequency=FREQUENCY)

    with connection.get_connection() as conn:
        titles = dict(conn.execute("SELECT id, title FROM articles").fetchall())
        cards = dict(conn.execute("SELECT article_id, title_ru FROM article_cards").fetchall())
    assert titles[ids[0]] == fixed and titles[ids[1]] == fixed
    assert titles[ids[2]] == glued  # не Telegram — не наша починка
    assert cards[ids[0]] == fixed  # копия заголовка — вместе с ним
    assert cards[ids[1]] == "Свой перевод"  # свой перевод не трогаем
