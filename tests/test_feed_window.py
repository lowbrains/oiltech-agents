"""Окно месяца ленты бизнес-сигналов (ADR 0001, п. 6; сессия B, 23.09).

Часы замораживаются подменой feed_window._now: правило считается «на сегодня по МСК»,
и без заморозки тест 05.10 00:00 проверял бы не смену окна, а сегодняшнюю дату.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, config, feed_window
from oiltech_digest.db import connection, repository

MSK = feed_window.MSK


def _msk(*args: int) -> datetime:
    return datetime(*args, tzinfo=MSK)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
#  Само правило — без базы
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("now", "months"),
    [
        (_msk(2026, 9, 23, 12, 0), ["2026-09"]),
        (_msk(2026, 10, 3, 12, 0), ["2026-09", "2026-10"]),
        (_msk(2026, 10, 4, 23, 59, 59), ["2026-09", "2026-10"]),
        (_msk(2026, 10, 5, 0, 0), ["2026-10"]),
        # Тот же момент, пришедший в UTC: 04.10 21:00 UTC — это уже 05.10 00:00 по МСК.
        (_utc(2026, 10, 4, 21, 0), ["2026-10"]),
        # Переход через год.
        (_msk(2027, 1, 2, 9, 0), ["2026-12", "2027-01"]),
    ],
)
def test_open_months_follow_msk_calendar_and_rollover_day(now, months):
    assert feed_window.current(now=now).open_months == months


def test_rollover_day_is_configurable(monkeypatch):
    monkeypatch.setattr(config, "FEED_ROLLOVER_DAY", 1)
    assert feed_window.current(now=_msk(2026, 10, 1, 0, 30)).open_months == ["2026-10"]
    monkeypatch.setattr(config, "FEED_ROLLOVER_DAY", 10)
    assert feed_window.current(now=_msk(2026, 10, 9, 23, 0)).open_months == ["2026-09", "2026-10"]


def test_past_month_is_read_only_and_open_month_is_not():
    on_23_09 = _msk(2026, 9, 23, 12, 0)
    assert feed_window.current("2026-08", now=on_23_09).read_only is True
    assert feed_window.current("2026-09", now=on_23_09).read_only is False
    # Пока идёт зазор, прошлый месяц ещё не архив: выпуск за август собирают 1–4 сентября.
    assert feed_window.current("2026-08", now=_msk(2026, 9, 3, 12, 0)).read_only is False


def test_window_sql_uses_the_digest_period_expression():
    """Окно обязано делить статьи по месяцам тем же выражением, что сборщик выпуска."""
    window = feed_window.current(now=_msk(2026, 9, 23, 12, 0))
    expr = "to_char(COALESCE(a.published_at, a.collected_at), 'YYYY-MM')"
    assert window.sql("a") == f"{expr} BETWEEN '2026-09' AND '2026-09'"
    rollover = feed_window.current(now=_msk(2026, 10, 3, 12, 0))
    assert rollover.sql("a") == f"{expr} BETWEEN '2026-09' AND '2026-10'"
    assert feed_window.current("2026-08", now=_msk(2026, 9, 23)).sql("a") == f"{expr} = '2026-08'"
    assert window.is_open("2026-09") and window.is_open("2026-11")
    assert not window.is_open("2026-08")


@pytest.mark.parametrize("bad", ["2026-13", "2026-8", "26-08", "2026-08-01", "август", "", "2026-00"])
def test_parse_month_rejects_anything_but_yyyy_mm(bad):
    with pytest.raises(ValueError):
        feed_window.parse_month(bad)


def test_month_label_is_russian():
    assert feed_window.month_label(date(2026, 8, 1)) == "август 2026"


# ---------------------------------------------------------------------------
#  Лента, счётчики и архив через API — на настоящей базе
# ---------------------------------------------------------------------------

def _freeze(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr(feed_window, "_now", lambda: moment)


def _user(conn, email: str, role: str) -> int:
    return conn.execute(
        "INSERT INTO users (email, password_salt, password_hash, role) "
        "VALUES (%s, 'salt', 'hash', %s) RETURNING id",
        (email, role),
    ).fetchone()[0]


def _source(conn) -> int:
    return conn.execute(
        "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
        "VALUES ('Neftegaz.ru', 'Media', 'https://neftegaz.example', TRUE, 'request') RETURNING id"
    ).fetchone()[0]


def _article(conn, source_id: int, slug: str, *, published: datetime | None,
             collected: datetime, score: int = 70) -> int:
    article_id = conn.execute(
        "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language) "
        "VALUES (%s, %s, %s, %s, %s, 'Текст про бурение.', 'ru') RETURNING id",
        (source_id, f"Статья {slug}", f"https://neftegaz.example/{slug}", published, collected),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO article_cards (article_id, summary, relevant) VALUES (%s, 'Суть', TRUE)",
        (article_id,),
    )
    conn.execute(
        "INSERT INTO article_scores (article_id, model, total_score, score_label, explanation) "
        "VALUES (%s, 'offline', %s, 'Средняя', 'почему')",
        (article_id, score),
    )
    return article_id


@pytest.fixture()
def feed(isolated_db):
    """Август, сентябрь и октябрь; по статье без даты публикации в августе и сентябре.

    Даты — середина суток по UTC, чтобы разница UTC/МСК не решала за тест. Граница
    суток проверяется отдельным тестом ниже.
    """
    with connection.get_connection() as conn:
        admin_id = _user(conn, "admin@example.com", "admin")
        user_id = _user(conn, "user@example.com", "user")
        source_id = _source(conn)
        ids = {
            "aug": _article(conn, source_id, "aug", published=_utc(2026, 8, 20, 12), collected=_utc(2026, 8, 21, 12)),
            # Без даты публикации статья относится к месяцу сбора, как в digest_candidates.
            "aug_nopub": _article(conn, source_id, "aug-nopub", published=None, collected=_utc(2026, 8, 25, 12)),
            "sep": _article(conn, source_id, "sep", published=_utc(2026, 9, 20, 12), collected=_utc(2026, 9, 20, 13)),
            "sep_nopub": _article(conn, source_id, "sep-nopub", published=None, collected=_utc(2026, 9, 15, 12)),
            # Опубликована в августе, собрана в сентябре: месяц — по публикации.
            "aug_late": _article(conn, source_id, "aug-late", published=_utc(2026, 8, 30, 12), collected=_utc(2026, 9, 2, 12)),
            "oct": _article(conn, source_id, "oct", published=_utc(2026, 10, 2, 12), collected=_utc(2026, 10, 2, 13)),
        }
        conn.commit()
    yield {"admin": admin_id, "user": user_id, "ids": ids}
    api.app.dependency_overrides.clear()


def _as(user_id: int, role: str) -> TestClient:
    api.app.dependency_overrides[api.require_user] = lambda: {"id": user_id, "email": f"{role}@example.com", "role": role}
    return TestClient(api.app)


def _feed_ids(client: TestClient, **params) -> set[int]:
    response = client.get("/api/articles", params={"limit": 5000, **params})
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()}


def test_on_23_09_feed_shows_only_september(feed, monkeypatch):
    _freeze(monkeypatch, _msk(2026, 9, 23, 12, 0))
    ids = feed["ids"]
    visible = _feed_ids(_as(feed["user"], "user"))
    # «oct» относительно замороженных часов — в будущем. Сверху окно закрыто текущим
    # месяцем: будущих дат сбор не пропускает (замер 23.09), и правило «только текущий
    # месяц» — буквальное.
    assert visible == {ids["sep"], ids["sep_nopub"]}
    assert ids["aug"] not in visible and ids["aug_late"] not in visible


def test_on_03_10_feed_shows_september_and_october(feed, monkeypatch):
    _freeze(monkeypatch, _msk(2026, 10, 3, 12, 0))
    ids = feed["ids"]
    assert _feed_ids(_as(feed["user"], "user")) == {ids["sep"], ids["sep_nopub"], ids["oct"]}


def test_at_05_10_msk_midnight_feed_switches_to_october(feed, monkeypatch):
    ids = feed["ids"]
    client = _as(feed["user"], "user")
    _freeze(monkeypatch, _msk(2026, 10, 4, 23, 59, 59))
    assert _feed_ids(client) == {ids["sep"], ids["sep_nopub"], ids["oct"]}
    _freeze(monkeypatch, _msk(2026, 10, 5, 0, 0))
    assert _feed_ids(client) == {ids["oct"]}


def test_article_without_publication_date_falls_into_collection_month(feed, monkeypatch):
    _freeze(monkeypatch, _msk(2026, 9, 23, 12, 0))
    ids = feed["ids"]
    client = _as(feed["user"], "user")
    assert ids["sep_nopub"] in _feed_ids(client)
    assert ids["aug_nopub"] in _feed_ids(client, month="2026-08")
    assert ids["aug_nopub"] not in _feed_ids(client)


def test_admin_gets_exactly_the_same_window_as_user(feed, monkeypatch):
    _freeze(monkeypatch, _msk(2026, 9, 23, 12, 0))
    as_user = _feed_ids(_as(feed["user"], "user"))
    as_admin = _feed_ids(_as(feed["admin"], "admin"))
    assert as_admin == as_user
    assert _feed_ids(_as(feed["admin"], "admin"), month="2026-08") == _feed_ids(
        _as(feed["user"], "user"), month="2026-08"
    )


def test_august_archive_is_viewable_but_statuses_cannot_change(feed, monkeypatch):
    _freeze(monkeypatch, _msk(2026, 9, 23, 12, 0))
    ids = feed["ids"]
    client = _as(feed["user"], "user")

    assert _feed_ids(client, month="2026-08") == {ids["aug"], ids["aug_nopub"], ids["aug_late"]}
    stats = client.get("/api/stats", params={"month": "2026-08"}).json()
    assert stats["window"] == {"months": ["2026-09"], "month": "2026-08", "read_only": True, "rollover_day": 5}

    for body in ({"status": "digest"}, {"selected_for_digest": True}, {"status": "noise"}):
        refused = client.patch(f"/api/articles/{ids['aug']}", json=body)
        assert refused.status_code == 409, body
        assert "архиву за август 2026" in refused.json()["detail"]
    with connection.get_connection() as conn:
        rows = conn.execute("SELECT count(*) FROM user_article_states WHERE article_id = %s", (ids["aug"],)).fetchone()[0]
    assert rows == 0, "отказ не должен оставлять следов в базе"

    # Открытый месяц правится как раньше.
    assert client.patch(f"/api/articles/{ids['sep']}", json={"status": "digest"}).status_code == 200


def test_previous_month_is_still_editable_during_the_gap(feed, monkeypatch):
    """Ради зазора и сделано окно: 1–4 числа выпуск за прошлый месяц ещё собирается."""
    _freeze(monkeypatch, _msk(2026, 9, 3, 12, 0))
    client = _as(feed["user"], "user")
    assert client.patch(f"/api/articles/{feed['ids']['aug']}", json={"status": "digest"}).status_code == 200


def test_unknown_article_is_still_404_and_bad_month_is_422(feed, monkeypatch):
    _freeze(monkeypatch, _msk(2026, 9, 23, 12, 0))
    client = _as(feed["user"], "user")
    assert client.patch("/api/articles/999999", json={"status": "digest"}).status_code == 404
    assert client.get("/api/articles", params={"month": "2026-13"}).status_code == 422
    assert client.get("/api/stats", params={"month": "август"}).status_code == 422


def test_stats_count_only_the_window(feed, monkeypatch):
    ids = feed["ids"]
    with connection.get_connection() as conn:
        # Выбор «в дайджест» в сентябре и в августе, отсев и пометка на удаление в сентябре.
        conn.execute(
            "INSERT INTO user_article_states (user_id, article_id, status) VALUES (%s, %s, 'digest'), (%s, %s, 'digest')",
            (feed["user"], ids["sep"], feed["user"], ids["aug"]),
        )
        conn.execute("UPDATE article_cards SET relevant = FALSE WHERE article_id = %s", (ids["sep_nopub"],))
        conn.commit()
    _freeze(monkeypatch, _msk(2026, 9, 23, 12, 0))
    client = _as(feed["user"], "user")

    stats = client.get("/api/stats").json()
    assert stats["window"] == {"months": ["2026-09"], "month": None, "read_only": False, "rollover_day": 5}
    # В окне: sep и sep_nopub (отсеяна гейтом) → «Всего» 2, сигналов 1.
    assert stats["all_articles"] == 2
    assert stats["total_articles"] == 1
    assert stats["selected_for_digest"] == 1, "августовский выбор в дайджест не считается в сентябре"
    assert stats["status_counts"]["digest"] == 1
    assert stats["status_counts"]["new"] == 0

    august = client.get("/api/stats", params={"month": "2026-08"}).json()
    assert august["all_articles"] == 3
    assert august["selected_for_digest"] == 1


def test_counters_and_archive_count_equal_what_the_feed_shows(feed, monkeypatch):
    """23.09 на проде: над лентой из 1 934 статей висело 2 205 «сигналов» — счётчики
    считали перепечатки (144) и статьи архивных источников (131), но не видели свежих
    статей без карточки (4). Теперь у ленты, счётчиков и архива одна выборка."""
    ids = feed["ids"]
    with connection.get_connection() as conn:
        source_id = _source_id(conn)
        for month, day in (("sep", 9), ("aug", 8)):
            base = _utc(2026, day, 12, 12)
            # Перепечатка: копия скрыта, главная остаётся.
            copy_id = _article(conn, source_id, f"{month}-copy", published=base, collected=base)
            conn.execute(
                "INSERT INTO article_reprints (article_id, primary_id) VALUES (%s, %s)",
                (copy_id, ids[month]),
            )
            # Помеченная на удаление перепроверкой.
            pending_id = _article(conn, source_id, f"{month}-pending", published=base, collected=base)
            conn.execute("UPDATE articles SET pending_deletion = TRUE WHERE id = %s", (pending_id,))
            # Свежая статья без карточки: ИИ её ещё не видел, а лента уже показывает.
            conn.execute(
                "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language) "
                "VALUES (%s, %s, %s, %s, %s, 'Текст.', 'ru')",
                (source_id, f"Без карточки {month}", f"https://neftegaz.example/{month}-nocard", base, base),
            )
        archived_source = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy, archived_at) "
            "VALUES ('Архивный', 'Media', 'https://archived.example', FALSE, 'request', now()) RETURNING id"
        ).fetchone()[0]
        _article(conn, archived_source, "sep-archived", published=_utc(2026, 9, 14, 12), collected=_utc(2026, 9, 14, 12))
        conn.commit()
    _freeze(monkeypatch, _msk(2026, 9, 23, 12, 0))
    client = _as(feed["user"], "user")

    in_feed = _feed_ids(client)
    assert client.get("/api/stats").json()["total_articles"] == len(in_feed) == 3  # sep, sep_nopub, без карточки

    in_august = _feed_ids(client, month="2026-08")
    august_stats = client.get("/api/stats", params={"month": "2026-08"}).json()
    picker = client.get("/api/feed-window").json()["archive"]
    assert august_stats["total_articles"] == len(in_august) == 4  # aug, aug_nopub, aug_late, без карточки
    assert picker == [{"month": "2026-08", "articles": len(in_august), "digest": 0}]


def test_issue_skips_article_pending_deletion(feed):
    """Лента прячет помеченное на удаление, а сборщик выпуска до 23.09 его брал (на проде —
    одна такая статья среди 22 выбранных «в дайджест»)."""
    aug = feed["ids"]["aug"]
    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO user_article_states (user_id, article_id, status) VALUES (%s, %s, 'digest')",
            (feed["user"], aug),
        )
        conn.commit()

    def issue() -> set[int]:
        rows = repository.digest_candidates(month="2026-08", limit=50, min_score=0, user_id=feed["user"])
        return {row["id"] for row in rows}

    assert aug in issue()
    with connection.get_connection() as conn:
        conn.execute("UPDATE articles SET pending_deletion = TRUE WHERE id = %s", (aug,))
        conn.commit()
    assert aug not in issue()


def test_issue_with_article_without_publication_date_does_not_crash(feed):
    """С 12.09 (3b2a880) сборщик выпуска сортировал общий список через
    `datetime.min.replace(tzinfo=timezone.utc)`, а `timezone` в repository.py не был
    импортирован: статья без даты публикации в выборе «в дайджест» роняла превью и выгрузку
    NameError'ом. Режим «Все месяцы» — первый, что открывает конструктор; найдено сверкой
    на проде 23.09 (в сентябре 815 статей без даты публикации)."""
    ids = feed["ids"]
    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO user_article_states (user_id, article_id, status) VALUES (%s, %s, 'digest'), (%s, %s, 'digest')",
            (feed["user"], ids["aug_nopub"], feed["user"], ids["aug"]),
        )
        conn.commit()
    rows = repository.digest_candidates(month=None, limit=50, min_score=0, user_id=feed["user"])
    assert {row["id"] for row in rows} == {ids["aug_nopub"], ids["aug"]}


def test_saved_issue_export_follows_the_same_visibility(feed):
    """Решение владельца 23.09 «одно правило везде»: выгрузка сохранённого выпуска = то, что
    видно в конструкторе. До этого черновик выгружался мимо правил ленты — статья, которую
    перепроверка потом пометила на удаление, оставалась в PDF (на проде: август — 1 из 7,
    июль — 2 из 5). То же с перепечаткой."""
    from oiltech_digest.processing import digest as digest_module

    ids = feed["ids"]
    repository.save_monthly_digest(
        month="2026-08", title="Август", status="draft", user_id=feed["user"],
        items=[{"article_id": ids["aug"]}, {"article_id": ids["aug_late"]}, {"article_id": ids["aug_nopub"]}],
    )

    def exported() -> list[int]:
        content = digest_module.build_digest_content(month="2026-08", limit=50, min_score=0, user_id=feed["user"])
        return [item["article_id"] for item in content["news"]]

    assert exported() == [ids["aug"], ids["aug_late"], ids["aug_nopub"]]
    with connection.get_connection() as conn:
        conn.execute("UPDATE articles SET pending_deletion = TRUE WHERE id = %s", (ids["aug"],))
        conn.execute(
            "INSERT INTO article_reprints (article_id, primary_id) VALUES (%s, %s)",
            (ids["aug_nopub"], ids["aug_late"]),
        )
        conn.commit()
    assert exported() == [ids["aug_late"]]


def test_feed_window_endpoint_lists_archive_months_with_counts(feed, monkeypatch):
    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO user_article_states (user_id, article_id, status) VALUES (%s, %s, 'digest')",
            (feed["user"], feed["ids"]["aug"]),
        )
        conn.commit()
    _freeze(monkeypatch, _msk(2026, 9, 23, 12, 0))
    payload = _as(feed["user"], "user").get("/api/feed-window").json()
    assert payload["months"] == ["2026-09"]
    assert payload["read_only"] is False
    assert payload["archive"] == [{"month": "2026-08", "articles": 3, "digest": 1}]
    # Выбор «в дайджест» личный: у админа тот же месяц без выбранных статей.
    admin_view = _as(feed["admin"], "admin").get("/api/feed-window").json()
    assert admin_view["archive"] == [{"month": "2026-08", "articles": 3, "digest": 0}]


def test_archived_issue_can_be_viewed_but_its_draft_cannot_change(feed, monkeypatch):
    """Решение владельца 23.09: прошлый выпуск — просмотр и выгрузка, без правки черновика."""
    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO user_article_states (user_id, article_id, status) VALUES (%s, %s, 'digest')",
            (feed["user"], feed["ids"]["aug"]),
        )
        conn.commit()
    _freeze(monkeypatch, _msk(2026, 9, 23, 12, 0))
    client = _as(feed["user"], "user")

    # Просмотр: выбранное за август отдаётся архивом, превью выпуска собирается.
    assert _feed_ids(client, month="2026-08", status="digest") == {feed["ids"]["aug"]}
    preview = client.get("/api/digest-content", params={"month": "2026-08"})
    assert preview.status_code == 200

    body = {"title": "Август", "status": "draft", "items": [{"article_id": feed["ids"]["aug"]}]}
    refused = client.put("/api/monthly-digests/2026-08", json=body)
    assert refused.status_code == 409
    assert "Выпуск за август 2026 в архиве" in refused.json()["detail"]
    assert client.post("/api/monthly-digests", json={"month": "2026-08"}).status_code == 409
    with connection.get_connection() as conn:
        drafts = conn.execute("SELECT count(*) FROM monthly_digests WHERE month = '2026-08'").fetchone()[0]
    assert drafts == 0, "отказ не должен сохранять черновик"

    # Выпуск открытого месяца сохраняется как раньше.
    open_body = {"title": "Сентябрь", "status": "draft", "items": [{"article_id": feed["ids"]["sep"]}]}
    assert client.put("/api/monthly-digests/2026-09", json=open_body).status_code == 200


def test_archived_article_cannot_enter_an_open_month_issue(feed, monkeypatch):
    """PUT принимает готовый список статей: без проверки «из архива в дайджест» проходило бы
    запросом в обход ленты — в выпуск сентября вписали бы августовскую статью."""
    _freeze(monkeypatch, _msk(2026, 9, 23, 12, 0))
    client = _as(feed["user"], "user")
    body = {"title": "Сентябрь", "status": "draft",
            "items": [{"article_id": feed["ids"]["sep"]}, {"article_id": feed["ids"]["aug"]}]}
    refused = client.put("/api/monthly-digests/2026-09", json=body)
    assert refused.status_code == 409
    assert "статьи из архива (август 2026)" in refused.json()["detail"]
    with connection.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM monthly_digests").fetchone()[0] == 0


def test_issue_month_must_be_yyyy_mm(feed, monkeypatch):
    _freeze(monkeypatch, _msk(2026, 9, 23, 12, 0))
    client = _as(feed["user"], "user")
    assert client.put("/api/monthly-digests/2026-9", json={"title": "x", "items": []}).status_code == 422
    assert client.post("/api/monthly-digests", json={"month": ""}).status_code == 422


def test_publication_month_wins_over_collection_month_in_feed_and_issue(feed, monkeypatch):
    """«aug_late»: опубликована 30.08, собрана 02.09. Лента и сборщик выпуска обязаны
    отнести её к одному месяцу — августу (COALESCE берёт дату публикации)."""
    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO user_article_states (user_id, article_id, status) VALUES (%s, %s, 'digest')",
            (feed["user"], feed["ids"]["aug_late"]),
        )
        conn.commit()
    _freeze(monkeypatch, _msk(2026, 9, 23, 12, 0))
    client = _as(feed["user"], "user")
    aug_late = feed["ids"]["aug_late"]
    assert aug_late in _feed_ids(client, month="2026-08")
    assert aug_late not in _feed_ids(client, month="2026-09")

    def issue(month: str) -> set[int]:
        rows = repository.digest_candidates(month=month, limit=50, min_score=0, user_id=feed["user"])
        return {row["id"] for row in rows}

    assert aug_late in issue("2026-08")
    assert aug_late not in issue("2026-09")


def test_previous_issue_draft_is_still_saved_during_the_gap(feed, monkeypatch):
    _freeze(monkeypatch, _msk(2026, 9, 4, 18, 0))
    client = _as(feed["user"], "user")
    body = {"title": "Август", "status": "draft", "items": [{"article_id": feed["ids"]["aug"]}]}
    assert client.put("/api/monthly-digests/2026-08", json=body).status_code == 200


def test_feed_month_matches_the_digest_month_at_the_day_boundary(feed, monkeypatch):
    """31.08 22:30 UTC — это уже 01.09 по МСК. Сборщик выпуска считает такую статью
    августовской (месяц в поясе сессии БД), значит и лента обязана: иначе статья видна
    в сентябрьской ленте, а отмеченная «в дайджест» уходит в августовский выпуск.

    Граница взята в прошлом намеренно: у digest_candidates есть отсечка «публикация не
    позже now() + 2 дня» по часам базы, а их тест не замораживает."""
    with connection.get_connection() as conn:
        boundary = _article(
            conn, _source_id(conn), "boundary",
            published=_utc(2026, 8, 31, 22, 30), collected=_utc(2026, 8, 31, 23, 0),
        )
        conn.execute(
            "INSERT INTO user_article_states (user_id, article_id, status) VALUES (%s, %s, 'digest')",
            (feed["user"], boundary),
        )
        conn.commit()
    _freeze(monkeypatch, _msk(2026, 9, 10, 12, 0))
    client = _as(feed["user"], "user")

    assert boundary not in _feed_ids(client)
    assert boundary in _feed_ids(client, month="2026-08")
    august_issue = repository.digest_candidates(month="2026-08", limit=50, min_score=0, user_id=feed["user"])
    assert boundary in {row["id"] for row in august_issue}


def _source_id(conn) -> int:
    return conn.execute("SELECT id FROM sources ORDER BY id LIMIT 1").fetchone()[0]
