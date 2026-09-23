"""Месячная аналитика платформы (экран «Статистика»): что считается и как сравнивается."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from oiltech_digest import api
from oiltech_digest.db import analytics, repository

MSK = ZoneInfo("Europe/Moscow")
# Опорные моменты — от начала текущего и прошлого месяца по Москве, чтобы тест не
# зависел от даты запуска: «сейчас минус час» первого числа попал бы в прошлый месяц.
THIS_MONTH = "(date_trunc('month', now() AT TIME ZONE 'Europe/Moscow') + interval '2 hours') AT TIME ZONE 'Europe/Moscow'"
PREV_DAY1 = "(date_trunc('month', now() AT TIME ZONE 'Europe/Moscow') - interval '1 month' + interval '2 hours') AT TIME ZONE 'Europe/Moscow'"
PREV_DAY28 = "(date_trunc('month', now() AT TIME ZONE 'Europe/Moscow') - interval '1 month' + interval '27 days 2 hours') AT TIME ZONE 'Europe/Moscow'"


def _exec(sql, params=()):
    with repository.get_connection() as conn:
        cur = conn.execute(sql, params)
        row = cur.fetchone() if "RETURNING" in sql else None
        conn.commit()
        return row


def _article(source_id, url, collected_sql, *, language="ru", relevant=True, score=None, tag_id=None,
             published_hours_before=None, full_text="ok"):
    published = f"{collected_sql} - interval '{published_hours_before} hours'" if published_hours_before else "NULL"
    article_id = _exec(
        f"""INSERT INTO articles (source_id, title, url, collected_at, published_at, language, full_text_status)
            VALUES (%s, %s, %s, {collected_sql}, {published}, %s, %s) RETURNING id""",
        (source_id, url, url, language, full_text),
    )[0]
    _exec("INSERT INTO article_cards (article_id, summary, relevant) VALUES (%s, %s, %s)",
          (article_id, "суть" if relevant else "", relevant))
    if score is not None:
        # Балл готов через 2 часа после сбора — от публикации это published_hours_before + 2.
        _exec(f"INSERT INTO article_scores (article_id, total_score, created_at) VALUES (%s, %s, {collected_sql} + interval '2 hours')",
              (article_id, score))
    if tag_id is not None:
        _exec("INSERT INTO article_tags (article_id, tag_id) VALUES (%s, %s)", (article_id, tag_id))
    return article_id


def test_monthly_analytics_funnel_same_period_themes_and_sources(isolated_db):
    src_a = _exec("INSERT INTO sources (name, source_type, url, enabled) VALUES ('A', 'Media', 'https://a.example', TRUE) RETURNING id")[0]
    src_b = _exec("INSERT INTO sources (name, source_type, url, enabled) VALUES ('B', 'Media', 'https://b.example', TRUE) RETURNING id")[0]
    root = _exec("INSERT INTO tags (name, enabled) VALUES ('Бурение', TRUE) RETURNING id")[0]
    child = _exec("INSERT INTO tags (name, parent_id, enabled) VALUES ('Долота', %s, TRUE) RETURNING id", (root,))[0]
    old = _exec("INSERT INTO tags (name, enabled) VALUES ('Старое направление', FALSE) RETURNING id")[0]

    strong = _article(src_a, "https://a.example/1", THIS_MONTH, language="en", score=75, tag_id=child, published_hours_before=4)
    _article(src_a, "https://a.example/2", THIS_MONTH, score=55, tag_id=old)
    _article(src_b, "https://b.example/3", THIS_MONTH, relevant=False, full_text="too_short")
    _article(src_b, "https://b.example/4", PREV_DAY1, score=65)
    _article(src_b, "https://b.example/5", PREV_DAY28, score=40)
    _exec("INSERT INTO article_reprints (article_id, primary_id) VALUES (%s, %s)",
          (_article(src_b, "https://b.example/6", THIS_MONTH), strong))

    data = analytics.monthly_analytics(3)

    current = data["months"][-1]
    assert current["complete"] is False and data["current_month"] == current["month"]
    assert (current["collected"], current["relevant"], current["rejected"]) == (4, 3, 1)
    assert (current["en"], current["full_text"], current["strong"], current["top"]) == (1, 3, 1, 1)
    assert (current["reprints"], current["sources_active"], current["sources_relevant"], current["sources_strong"]) == (1, 2, 2, 1)
    assert round(current["speed_p50_hours"]) == 6  # публикация → балл: 4 ч до сбора + 2 ч обработки

    # Прошлый месяц целиком и «те же дни» — честная база для незаконченного текущего.
    day = datetime.now(MSK).day
    assert data["months"][-2]["collected"] == 2
    assert data["previous_same_period"]["collected"] == (2 if day >= 28 else 1)
    assert data["previous_same_period"]["days"] == min(day, data["previous_same_period"]["days"])

    # Темы — по корню действующей таксономии; старое направление не подмешивается.
    themes = [(row["tag"], row["relevant"], row["strong"]) for row in data["themes"] if row["month"] == current["month"]]
    assert themes == [("Бурение", 1, 1)]
    top = [row["source"] for row in data["top_sources"] if row["month"] == current["month"]]
    assert top[0] == "A"
    assert "ai_cost" not in data


def test_analytics_is_admin_only(monkeypatch):
    """Решение владельца 19.09: «показываем только админам» — гейт на API."""
    seen = []
    monkeypatch.setattr(api.analytics, "monthly_analytics",
                        lambda months, include_cost=False: seen.append(include_cost) or {"months": []})
    app = api.app
    try:
        app.dependency_overrides[api.require_user] = lambda: {"id": 7, "email": "u@e.ru", "role": "user"}
        assert TestClient(app).get("/api/analytics/monthly").status_code == 403
        app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "a@e.ru", "role": "admin"}
        assert TestClient(app).get("/api/analytics/monthly?months=12").status_code == 200
    finally:
        app.dependency_overrides.clear()
    assert seen == [True]  # до данных не дошёл никто, кроме админа


def test_cost_months_use_cbr_rate_of_their_own_last_day(isolated_db, monkeypatch):
    asked = []
    monkeypatch.setattr(analytics.fx, "usd_rub",
                        lambda on, today=None: asked.append(on) or {"rate": 80.0 + on.month, "date": on.isoformat(), "source": "ЦБ РФ"})

    data = analytics.monthly_analytics(2, include_cost=True)

    now = datetime.now(MSK).date()
    current, previous = data["ai_cost"][-1], data["ai_cost"][-2]
    assert current["usd_rub_date"] == now.isoformat()  # текущий месяц — по курсу на сегодня
    prev_last = datetime.strptime(previous["month"] + "-01", "%Y-%m-%d").date()
    assert previous["usd_rub_date"].startswith(previous["month"]) and previous["usd_rub_date"] >= prev_last.isoformat()
    assert previous["usd_rub"] == 80.0 + int(previous["month"][5:])
    same = data["ai_cost_previous_same_period"]
    assert same["usd_rub_date"] == f"{same['month']}-{same['days']:02d}"  # база «те же дни» — по курсу её последнего дня
