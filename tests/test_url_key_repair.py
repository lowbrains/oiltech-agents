"""Ключ адреса после 13.09: бэкфилл в схеме ≡ Python и починка спрятанного склейкой.

13.09 ключ `articles.url_key` срезал query целиком, и схема спрятала «копии» — у сайтов, где
номер статьи в query, это все статьи, кроме одной (у Минэнерго 90 из 91, 25.09).
"""

from __future__ import annotations

from oiltech_digest.db import connection
from oiltech_digest.ingestion import normalize, url_key_repair


def _source(conn, name: str) -> int:
    return conn.execute(
        "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
        "VALUES (%s, 'Company', 'https://site.example', TRUE, 'request') RETURNING id", (name,)
    ).fetchone()[0]


def test_schema_backfill_computes_url_key_like_python(isolated_db):
    """Бэкфилл `url_key` в schema.sql (строки без ключа — например, после восстановления
    старого дампа) обязан давать тот же ключ, что `normalize.url_key` на вставке: иначе
    уникальный индекс пропустит копию или отобьёт чужую статью. Адреса строятся из самого
    списка трекинговых параметров: имя, добавленное только в Python, даст красный тест."""
    urls = [
        "https://lukoil.ru/PressCenter/Pressreleases/Pressrelease?rid=740957",
        "https://en.antonoil.com/index.php?m=content&c=index&a=show&catid=90&id=4023",
        "https://www.eia.gov/todayinenergy/detail.php?id=68184",
        "https://www.rbc.ru/politics/24/08/2026/6a8c1725?from=newsfeed",
        "http://www.rostec.ru/news/abc/",
        "https://gubkin.ru/news/detail.php?ID=57931#comments",
        "https://minenergo.gov.ru/press-center/news-and-events?news-item=%D0%BC%D0%B8%D0%BD",
        "https://site.example/a?b=2?c=3",
        "\thttps://site.example/trimmed?id=7\n",
    ]
    urls += [f"https://site.example/p/{name}?id=1&{name}=x" for name in sorted(normalize._TRACKING_QUERY_PARAMS)]
    urls += [f"https://site.example/q/{prefix}?{prefix}foo=y&id=2" for prefix in normalize._TRACKING_QUERY_PREFIXES]
    with connection.get_connection() as conn:
        source_id = _source(conn, "Микс")
        for i, url in enumerate(urls):
            conn.execute("INSERT INTO articles (source_id, title, url, raw_text) VALUES (%s, %s, %s, %s)",
                         (source_id, f"Статья {i}", url, f"Тело {i}"))
        conn.commit()

    connection.init_db()  # бэкфилл идёт на каждом init-db для строк без ключа

    with connection.get_connection() as conn:
        rows = conn.execute("SELECT url, url_key FROM articles WHERE source_id = %s", (source_id,)).fetchall()
    assert len(rows) == len(urls)
    for url, key in rows:
        assert key == normalize.url_key(url), repr(url)


def test_repair_url_keys_unhides_only_what_the_13_09_collapse_hid(isolated_db):
    """Возвращаем только спрятанное склейкой (без отметки пометки) и только если статья по
    новому ключу своя; видимый дубль, вставленный между выкатом и починкой, не роняет её."""
    base = "https://lukoil.ru/PressCenter/Pressreleases/Pressrelease"
    old = "lukoil.ru/presscenter/pressreleases/pressrelease"
    with connection.get_connection() as conn:
        source_id = _source(conn, "Лукойл")

        def put(url, key, body, hidden=False, marked=False):
            return conn.execute(
                "INSERT INTO articles (source_id, title, url, url_key, raw_text, body_hash, "
                "pending_deletion, deletion_reason, marked_for_deletion_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END) RETURNING id",
                (source_id, f"Статья {url[-12:]}", url, key, body, normalize.compute_body_hash(body),
                 hidden, "шум" if marked else None, marked),
            ).fetchone()[0]

        survivor = put(f"{base}?rid=739353", old, "Релиз о заводе.")
        collapsed = put(f"{base}?rid=731755", old, "Релиз о бурении.", hidden=True)
        by_user = put(f"{base}?rid=700000", old, "Релиз-шум.", hidden=True, marked=True)
        same_body = put(f"{base}?rid=731734", old, "Релиз о заводе.", hidden=True)
        # Вставлен уже новым кодом до починки — держит новый ключ.
        fresh = put(f"{base}?rid=741000", f"{old}?rid=741000", "Свежий релиз.")
        taken = put(f"http://lukoil.ru/PressCenter/Pressreleases/Pressrelease?rid=741000", old,
                    "Свежий релиз, другое написание.", hidden=True)
        twin_a = put(f"{base}?rid=742000", old, "Релиз о газе.", hidden=True)
        twin_b = put(f"{base}?rid=742000&utm_source=tg", old, "Релиз о газе, с хвостом.", hidden=True)
        gate_removed = put(f"{base}?rid=743000", old, "Релиз, отклонённый гейтом.", hidden=True, marked=True)
        its_twin = put(f"http://www.lukoil.ru/PressCenter/Pressreleases/Pressrelease?rid=743000", old,
                       "Тот же релиз, другое написание.", hidden=True)
        rbc_visible = put("https://www.rbc.ru/news/123?from=main_lines_11", "rbc.ru/news/123", "Новость РБК.")
        rbc_copy = put("https://www.rbc.ru/news/123?from=newsfeed", "rbc.ru/news/123", "Новость РБК, копия.",
                       hidden=True)
        # Выжившая после склейки и её другое написание, вставленное между выкатом и починкой.
        old_survivor = put("https://novatek.ru/press/index.php?id_4=7335", "novatek.ru/press/index.php",
                           "Релиз Новатэка.")
        dup_holder = put("http://novatek.ru/press/index.php?id_4=7335", "novatek.ru/press/index.php?id_4=7335",
                         "Анонс того же релиза.")
        conn.commit()

    dry = url_key_repair.repair_url_keys(apply=False)
    assert dry["apply"] is False and dry["unhidden"] == 2
    assert dry["kept_hidden"] == {"marked_by_user": 2, "same_body": 1, "key_taken": 2,
                                  "twin_of_marked": 1, "tracking_duplicate": 1}
    assert dry["key_conflicts"] == [{"id": old_survivor, "url": "https://novatek.ru/press/index.php?id_4=7335",
                                     "holder_id": dup_holder}]
    with connection.get_connection() as conn:
        assert conn.execute("SELECT pending_deletion FROM articles WHERE id = %s",
                            (collapsed,)).fetchone()[0] is True, "сухой прогон ничего не пишет"

    done = url_key_repair.repair_url_keys(apply=True)
    assert done["unhidden"] == 2
    with connection.get_connection() as conn:
        hidden = dict(conn.execute("SELECT id, pending_deletion FROM articles").fetchall())
        keys = dict(conn.execute("SELECT id, url_key FROM articles").fetchall())
    assert {i for i, h in hidden.items() if not h} == {
        survivor, collapsed, fresh, twin_a, rbc_visible, old_survivor, dup_holder}
    assert {by_user, same_body, taken, twin_b, gate_removed, its_twin, rbc_copy} <= {i for i, h in hidden.items() if h}
    assert keys[survivor] == f"{old}?rid=739353"
    assert keys[collapsed] == f"{old}?rid=731755"
    assert keys[rbc_visible] == "rbc.ru/news/123", "трекинговый хвост в ключ не попадает"
    assert keys[old_survivor] == "novatek.ru/press/index.php", "занятый ключ не отдаётся — индекс цел"

    again = url_key_repair.repair_url_keys(apply=True)
    assert again["unhidden"] == 0 and again["key_updates"] == 0, "повторный прогон — пустой"
