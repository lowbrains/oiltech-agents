from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api
from oiltech_digest.db import connection


def test_articles_api_filters_and_patch_status_against_real_db(isolated_db):
    app = api.app
    now = datetime.now(timezone.utc)

    with connection.get_connection() as conn:
        # Пер-юзерное состояние (#12) ссылается на users(id) по внешнему ключу — нужен
        # настоящий пользователь, а не выдуманный id из dependency_overrides.
        user_id = conn.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES ('test@example.com', 'salt', 'hash', 'admin')
            RETURNING id
            """
        ).fetchone()[0]
        source_id = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category)
            VALUES ('World Oil', 'News', 'https://example.com', TRUE, 'request', 'международные')
            RETURNING id
            """
        ).fetchone()[0]
        parent_tag_id = conn.execute(
            "INSERT INTO tags (name, enabled, sort_order) VALUES ('Технологии', TRUE, 1) RETURNING id"
        ).fetchone()[0]
        tag_id = conn.execute(
            "INSERT INTO tags (parent_id, name, enabled, sort_order) VALUES (%s, 'Бурение', TRUE, 1) RETURNING id",
            (parent_tag_id,),
        ).fetchone()[0]
        criterion_id = conn.execute(
            """
            INSERT INTO scoring_criteria (name, weight, enabled, sort_order)
            VALUES ('Технологическая значимость', 100, TRUE, 1)
            RETURNING id
            """
        ).fetchone()[0]
        article_id = conn.execute(
            """
            INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language)
            VALUES (%s, 'Directional drilling automation', 'https://example.com/drilling',
                    %s, %s, 'Automation improves directional drilling operations.', 'en')
            RETURNING id
            """,
            (source_id, now - timedelta(days=1), now - timedelta(days=1)),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO article_cards (article_id, summary, relevant, status, selected_for_digest)
            VALUES (%s, 'AI summary for drilling automation', TRUE, 'digest', FALSE)
            """,
            (article_id,),
        )
        # Рабочий статус статьи ПЕР-ЮЗЕРНЫЙ (#12): /api/articles фильтрует по
        # COALESCE(uas.status, 'new'), а не по article_cards.status. Без строки в
        # user_article_states статус читается как 'new' и фильтр status=digest даёт 0 строк.
        conn.execute(
            """
            INSERT INTO user_article_states (user_id, article_id, status)
            VALUES (%s, %s, 'digest')
            """,
            (user_id, article_id),
        )
        conn.execute(
            """
            INSERT INTO article_tags (article_id, tag_id, confidence, rationale)
            VALUES (%s, %s, 0.92, 'matched drilling')
            """,
            (article_id, tag_id),
        )
        score_id = conn.execute(
            """
            INSERT INTO article_scores (article_id, model, total_score, score_label, explanation)
            VALUES (%s, 'offline', 87, 'Высокая', 'strong relevance')
            RETURNING id
            """,
            (article_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO article_score_items
              (article_score_id, criterion_id, keyword_score, ai_score, final_score, rationale)
            VALUES (%s, %s, 80, 90, 86.5, 'criterion rationale')
            """,
            (score_id, criterion_id),
        )
        conn.commit()

    app.dependency_overrides[api.require_user] = lambda: {"id": user_id, "email": "test@example.com"}

    try:
        client = TestClient(app)
        response = client.get(
            "/api/articles",
            params={
                "search": "directional",
                "source": "World Oil",
                "tag": "Технологии",
                "status": "digest",
                "min_score": 80,
                "limit": 10,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["id"] == article_id
        assert payload[0]["source"] == "World Oil"
        assert payload[0]["tag"] == "Технологии / Бурение"
        assert payload[0]["score"] == 87
        assert payload[0]["status"] == "digest"
        assert payload[0]["digest"] is True
        assert payload[0]["score_items"] == [
            {
                "name": "Технологическая значимость",
                "weight": 100.0,
                "final_score": 86.5,
                "ai_score": 90.0,
                "keyword_score": 80.0,
                "rationale": "criterion rationale",
            }
        ]

        patch_response = client.patch(
            f"/api/articles/{article_id}",
            json={"status": "digest", "analyst_comment": "Include in June digest"},
        )
        assert patch_response.status_code == 200
        assert patch_response.json() == {"ok": True}

        updated = client.get("/api/articles", params={"status": "digest", "limit": 10}).json()
        assert len(updated) == 1
        assert updated[0]["digest"] is True
        assert updated[0]["status"] == "digest"

        missing_response = client.patch("/api/articles/999999", json={"status": "digest"})
        assert missing_response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_min_score_filters_scored_noise_but_keeps_unscored_visible(isolated_db):
    """Порог min_score применяется только к УЖЕ оценённым статьям.

    Регрессия деплоя 4ed8dd2: дефолт ленты стал min_score=50, а ещё не оценённые
    статьи (нет строки в article_scores → total_score NULL → COALESCE 0) отсекались
    порогом вместе с настоящим шумом. Свежий приток становился невидим до прохода
    ИИ, и лента выглядела замороженной («обработка сломалась»).
    Решение заказчика «скрывать <50» касается оценённого шума — оно сохраняется.
    """
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    now = datetime.now(timezone.utc)

    with connection.get_connection() as conn:
        source_id = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category)
            VALUES ('World Oil', 'News', 'https://example.com', TRUE, 'request', 'международные')
            RETURNING id
            """
        ).fetchone()[0]

        def add_article(title: str, url: str, score: int | None) -> int:
            article_id = conn.execute(
                """
                INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language)
                VALUES (%s, %s, %s, %s, %s, 'raw text', 'en')
                RETURNING id
                """,
                (source_id, title, url, now - timedelta(days=1), now - timedelta(days=1)),
            ).fetchone()[0]
            if score is not None:
                conn.execute(
                    """
                    INSERT INTO article_scores (article_id, model, total_score, score_label, explanation)
                    VALUES (%s, 'offline', %s, 'Высокая', 'why')
                    """,
                    (article_id, score),
                )
            return article_id

        high_id = add_article("High signal", "https://example.com/high", 80)
        noise_id = add_article("Scored noise", "https://example.com/noise", 30)
        unscored_id = add_article("Fresh unscored", "https://example.com/fresh", None)
        conn.commit()

    try:
        client = TestClient(app)
        payload = client.get(
            "/api/articles",
            params={"min_score": 50, "max_score": 100, "sort": "score_desc", "limit": 100},
        ).json()
        ids = [row["id"] for row in payload]

        # Оценённый шум (30 < 50) отсекается — решение «скрыть <50» работает.
        assert noise_id not in ids
        # Ценный сигнал виден.
        assert high_id in ids
        # Ещё НЕ оценённая статья ОСТАЁТСЯ видимой (суть фикса).
        assert unscored_id in ids

        # При score_desc неоценённая оседает вниз и не мешает «верху» ленты.
        assert ids[0] == high_id
        assert ids[-1] == unscored_id

        unscored = next(row for row in payload if row["id"] == unscored_id)
        assert unscored["score"] == 0
        assert unscored["rating"] == "Без оценки"
    finally:
        app.dependency_overrides.clear()


def test_stats_status_counts_cover_whole_db_and_respect_feed_visibility(isolated_db):
    """Пер-статусные счётчики считаются по ВСЕЙ базе и по правилам видимости ленты.

    Раньше плитки «Новые/На проверке/Шум/Дубликаты» считались на клиенте по массиву
    загруженных статей (топ-2000, суженный активным фильтром) — цифры занижали и
    «плавали», расходясь с плитками «Всего»/«Обработано», которые всегда были по базе.
    """
    app = api.app
    now = datetime.now(timezone.utc)

    with connection.get_connection() as conn:
        user_id = conn.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES ('analyst@example.com', 'salt', 'hash', 'admin')
            RETURNING id
            """
        ).fetchone()[0]
        source_id = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category)
            VALUES ('World Oil', 'News', 'https://example.com', TRUE, 'request', 'международные')
            RETURNING id
            """
        ).fetchone()[0]

        def add_article(slug: str, *, status: str | None, relevant: bool | None = True,
                        pending_deletion: bool = False) -> int:
            article_id = conn.execute(
                """
                INSERT INTO articles (source_id, title, url, published_at, collected_at,
                                      raw_text, language, pending_deletion)
                VALUES (%s, %s, %s, %s, %s, 'text', 'en', %s)
                RETURNING id
                """,
                (source_id, f"Article {slug}", f"https://example.com/{slug}",
                 now - timedelta(days=1), now - timedelta(days=1), pending_deletion),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO article_cards (article_id, summary, relevant) VALUES (%s, 'sum', %s)",
                (article_id, relevant),
            )
            if status is not None:
                conn.execute(
                    "INSERT INTO user_article_states (user_id, article_id, status) VALUES (%s, %s, %s)",
                    (user_id, article_id, status),
                )
            return article_id

        n1_id = add_article("n1", status=None)      # без строки состояния → считается 'new'
        add_article("n2", status="new")
        add_article("noise1", status="noise")
        add_article("dup1", status="duplicate")
        add_article("dup2", status="duplicate")
        add_article("arch1", status="archive")
        add_article("dig1", status="digest")
        # Невидимые в ленте — не должны попадать в счётчики.
        add_article("rejected", status="new", relevant=False)
        add_article("marked", status="new", pending_deletion=True)

        # Второй пользователь метит статью n1 (которую A оставил как 'new') статусом 'noise'.
        # Счётчики A должны это ИГНОРИРОВАТЬ — состояние статьи пер-юзерное (#12). Если из
        # GROUP BY уберут предикат uas.user_id, чужой 'noise' протечёт в счётчики A и точный
        # ассерт ниже упадёт (n1 уедет из 'new' в 'noise').
        other_user_id = conn.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES ('other@example.com', 'salt', 'hash', 'user')
            RETURNING id
            """
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO user_article_states (user_id, article_id, status) VALUES (%s, %s, 'noise')",
            (other_user_id, n1_id),
        )
        conn.commit()

    app.dependency_overrides[api.require_user] = lambda: {"id": user_id, "email": "analyst@example.com"}

    try:
        client = TestClient(app)
        counts = client.get("/api/stats").json()["status_counts"]
    finally:
        app.dependency_overrides.clear()

    # Отклонённый гейтом и помеченный на удаление в 'new' НЕ попали (их 2, оба со статусом new).
    assert counts == {
        "new": 2,
        "digest": 1,
        "archive": 1,
        "noise": 1,
        "duplicate": 2,
    }


def test_patch_article_rejects_unknown_status_and_accepts_known_ones(isolated_db):
    """Статус валидируется на границе API (422), мусор в БД не попадает.

    Колонки статуса — свободный TEXT без CHECK. Раньше ArticlePatch.status был
    просто str, поэтому любая опечатка/неизвестное значение молча записывалось в
    user_article_states, и статья исчезала из всех вкладок (они перечисляют
    известный набор статусов) — тихая потеря сигнала.
    """
    app = api.app
    now = datetime.now(timezone.utc)

    with connection.get_connection() as conn:
        user_id = conn.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES ('analyst@example.com', 'salt', 'hash', 'admin')
            RETURNING id
            """
        ).fetchone()[0]
        source_id = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category)
            VALUES ('World Oil', 'News', 'https://example.com', TRUE, 'request', 'международные')
            RETURNING id
            """
        ).fetchone()[0]
        article_id = conn.execute(
            """
            INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language)
            VALUES (%s, 'Some signal', 'https://example.com/a', %s, %s, 'text', 'en')
            RETURNING id
            """,
            (source_id, now - timedelta(days=1), now - timedelta(days=1)),
        ).fetchone()[0]
        conn.commit()

    app.dependency_overrides[api.require_user] = lambda: {"id": user_id, "email": "analyst@example.com"}

    try:
        client = TestClient(app)

        bad = client.patch(f"/api/articles/{article_id}", json={"status": "делете"})
        assert bad.status_code == 422

        with connection.get_connection() as conn:
            stored = conn.execute(
                "SELECT status FROM user_article_states WHERE user_id = %s AND article_id = %s",
                (user_id, article_id),
            ).fetchone()
        assert stored is None, "невалидный статус не должен создавать строку состояния"

        for status in ("new", "digest", "archive", "noise", "duplicate"):
            ok = client.patch(f"/api/articles/{article_id}", json={"status": status})
            assert ok.status_code == 200, f"статус {status} должен приниматься"
            with connection.get_connection() as conn:
                stored = conn.execute(
                    "SELECT status FROM user_article_states WHERE user_id = %s AND article_id = %s",
                    (user_id, article_id),
                ).fetchone()[0]
            assert stored == status
    finally:
        app.dependency_overrides.clear()


def test_digest_export_endpoint_finishes_job_and_returns_file(monkeypatch, tmp_path, isolated_db):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    exported = tmp_path / "digest.html"
    exported.write_text("<html><body>Digest file</body></html>", encoding="utf-8")

    monkeypatch.setattr(
        api,
        "write_digest_export",
        lambda month, export_format, limit, min_score, **kwargs: {
            "path": str(exported),
            "filename": exported.name,
            "media_type": "text/html; charset=utf-8",
            "items": 1,
            "format": export_format,
        },
    )

    try:
        client = TestClient(app)
        response = client.get("/api/digest-export?month=2026-06&export_format=html&limit=5&min_score=10")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.text == "<html><body>Digest file</body></html>"
    assert response.headers["content-type"].startswith("text/html")

    with connection.get_connection() as conn:
        row = conn.execute("SELECT export_type, format, status, file_path, error_message FROM export_jobs").fetchone()
    assert row == ("monthly_digest", "html", "ok", str(exported), None)


def test_digest_export_endpoint_records_pdf_runtime_error(monkeypatch, isolated_db):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}

    # Сигнатура должна принимать ВСЕ kwargs эндпоинта (max_score/search/top_tag/user_id),
    # иначе TypeError при связывании аргументов срабатывает раньше raise RuntimeError,
    # и вместо ветки 503 тест ловит generic 500.
    def fail_export(month, export_format, limit, min_score, **kwargs):
        raise RuntimeError("PDF-экспорт требует Playwright")

    monkeypatch.setattr(api, "write_digest_export", fail_export)

    try:
        client = TestClient(app)
        response = client.get("/api/digest-export?export_format=pdf")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert "Playwright" in response.json()["detail"]

    with connection.get_connection() as conn:
        row = conn.execute("SELECT export_type, format, status, file_path, error_message FROM export_jobs").fetchone()
    assert row == ("monthly_digest", "pdf", "failed", None, "PDF-экспорт требует Playwright")


def test_background_jobs_api_lists_status_and_downloads_result(tmp_path, isolated_db):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    exported = tmp_path / "digest.json"
    exported.write_text('{"ok": true}', encoding="utf-8")

    with connection.get_connection() as conn:
        job_id = conn.execute(
            """
            INSERT INTO background_jobs
              (kind, status, progress, payload_json, result_json, started_at, finished_at)
            VALUES
              ('digest_export', 'ok', 100, '{"month":"2026-06"}'::jsonb,
               %s::jsonb, now(), now())
            RETURNING id
            """,
            (
                f'{{"path": "{exported}", "filename": "digest.json", '
                '"media_type": "application/json"}',
            ),
        ).fetchone()[0]
        conn.commit()

    try:
        client = TestClient(app)
        list_response = client.get("/api/jobs?kind=digest_export")
        status_response = client.get(f"/api/jobs/{job_id}")
        download_response = client.get(f"/api/jobs/{job_id}/download")
    finally:
        app.dependency_overrides.clear()

    assert list_response.status_code == 200
    assert list_response.json()[0]["id"] == job_id
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "ok"
    assert status_response.json()["result"]["filename"] == "digest.json"
    assert download_response.status_code == 200
    assert download_response.text == '{"ok": true}'


def test_background_jobs_api_hides_other_users_jobs_and_downloads(tmp_path, isolated_db):
    app = api.app
    exported = tmp_path / "own-digest.json"
    exported.write_text('{"owner": true}', encoding="utf-8")

    with connection.get_connection() as conn:
        first_user_id = conn.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES ('first@example.com', 'salt', 'hash', 'user')
            RETURNING id
            """
        ).fetchone()[0]
        second_user_id = conn.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES ('second@example.com', 'salt', 'hash', 'user')
            RETURNING id
            """
        ).fetchone()[0]
        own_job_id = conn.execute(
            """
            INSERT INTO background_jobs
              (user_id, kind, status, progress, payload_json, result_json, started_at, finished_at)
            VALUES
              (%s, 'digest_export', 'ok', 100, '{"month":"2026-06"}'::jsonb,
               %s::jsonb, now(), now())
            RETURNING id
            """,
            (
                first_user_id,
                f'{{"path": "{exported}", "filename": "own-digest.json", '
                '"media_type": "application/json"}',
            ),
        ).fetchone()[0]
        other_job_id = conn.execute(
            """
            INSERT INTO background_jobs
              (user_id, kind, status, progress, payload_json, result_json, started_at, finished_at)
            VALUES
              (%s, 'digest_export', 'ok', 100, '{"month":"2026-06"}'::jsonb,
               '{"path": "/tmp/other-digest.json", "filename": "other-digest.json"}'::jsonb,
               now(), now())
            RETURNING id
            """,
            (second_user_id,),
        ).fetchone()[0]
        conn.commit()

    app.dependency_overrides[api.require_user] = lambda: {
        "id": first_user_id,
        "email": "first@example.com",
        "role": "user",
    }
    try:
        client = TestClient(app)
        list_response = client.get("/api/jobs?kind=digest_export")
        own_status_response = client.get(f"/api/jobs/{own_job_id}")
        other_status_response = client.get(f"/api/jobs/{other_job_id}")
        own_download_response = client.get(f"/api/jobs/{own_job_id}/download")
        other_download_response = client.get(f"/api/jobs/{other_job_id}/download")
    finally:
        app.dependency_overrides.clear()

    assert list_response.status_code == 200
    assert [job["id"] for job in list_response.json()] == [own_job_id]
    assert own_status_response.status_code == 200
    assert own_status_response.json()["id"] == own_job_id
    assert other_status_response.status_code == 404
    assert own_download_response.status_code == 200
    assert own_download_response.text == '{"owner": true}'
    assert other_download_response.status_code == 404


def test_background_job_download_rejects_unfinished_and_missing_files(isolated_db):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}

    with connection.get_connection() as conn:
        queued_job_id = conn.execute(
            """
            INSERT INTO background_jobs (kind, status, progress, payload_json)
            VALUES ('digest_export', 'queued', 0, '{}'::jsonb)
            RETURNING id
            """
        ).fetchone()[0]
        missing_file_job_id = conn.execute(
            """
            INSERT INTO background_jobs
              (kind, status, progress, payload_json, result_json, started_at, finished_at)
            VALUES
              ('digest_export', 'ok', 100, '{}'::jsonb,
               '{"path": "/tmp/definitely-missing-oiltech-digest.pdf", "filename": "missing.pdf"}'::jsonb,
               now(), now())
            RETURNING id
            """
        ).fetchone()[0]
        conn.commit()

    try:
        client = TestClient(app)
        queued_response = client.get(f"/api/jobs/{queued_job_id}/download")
        missing_file_response = client.get(f"/api/jobs/{missing_file_job_id}/download")
    finally:
        app.dependency_overrides.clear()

    assert queued_response.status_code == 409
    assert queued_response.json()["detail"] == "Job is not finished"
    assert missing_file_response.status_code == 404
    assert missing_file_response.json()["detail"] == "Job result file not found"


def test_archived_source_disappears_from_feed_and_digest(isolated_db):
    """Требование заказчика 12.09: архивный источник уносит с собой свои статьи.

    Именно этого НЕ делало `enabled = FALSE`: сбор прекращался, а накопленные статьи
    продолжали висеть в ленте у всех — 08.09 заказчик прислал пять таких источников
    («сайт всё», «тоже шляпа», «канал никто не ведёт») с просьбой их убрать.
    """
    app = api.app
    now = datetime.now(timezone.utc)
    with connection.get_connection() as conn:
        user_id = conn.execute(
            "INSERT INTO users (email, password_salt, password_hash, role) "
            "VALUES ('arch@example.com', 'salt', 'hash', 'admin') RETURNING id"
        ).fetchone()[0]
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('Мёртвый источник', 'Media', 'https://dead.example', TRUE, 'request') RETURNING id"
        ).fetchone()[0]
        article_id = conn.execute(
            "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language) "
            "VALUES (%s, 'Статья мёртвого источника', 'https://dead.example/a1', %s, %s, %s, 'ru') "
            "RETURNING id",
            (source_id, now - timedelta(days=1), now - timedelta(days=1), "Текст про бурение. " * 30),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO article_cards (article_id, summary, relevant) VALUES (%s, 'Суть', TRUE)",
            (article_id,),
        )
        conn.commit()

    app.dependency_overrides[api.require_user] = lambda: {"id": user_id, "email": "arch@example.com", "role": "admin"}
    app.dependency_overrides[api.require_admin] = lambda: {"id": user_id, "email": "arch@example.com", "role": "admin"}
    try:
        client = TestClient(app)
        before = [row["id"] for row in client.get("/api/articles", params={"limit": 5000}).json()]
        assert article_id in before, "до архивации статья обязана быть в ленте"

        assert client.post(f"/api/sources/{source_id}/archive").status_code == 200
        after = [row["id"] for row in client.get("/api/articles", params={"limit": 5000}).json()]
        assert article_id not in after, "после архивации статья обязана уйти из ленты"

        with connection.get_connection() as conn:
            enabled, archived = conn.execute(
                "SELECT enabled, archived_at FROM sources WHERE id = %s", (source_id,)
            ).fetchone()
        assert enabled is False, "архивный источник не должен опрашиваться"
        assert archived is not None

        # Обратимость: разархивация возвращает статьи в ленту.
        assert client.post(f"/api/sources/{source_id}/unarchive").status_code == 200
        restored = [row["id"] for row in client.get("/api/articles", params={"limit": 5000}).json()]
        assert article_id in restored, "разархивация обязана вернуть статьи в ленту"
    finally:
        app.dependency_overrides.clear()


def test_readding_archived_source_does_not_resurrect_it(isolated_db):
    """`ON CONFLICT ... SET enabled = TRUE` воскрешал бы архивный источник молча.

    Худшее из двух состояний: сбор идёт, а статьи скрыты — источник жжёт ИИ в никуда.
    """
    from oiltech_digest.db import repository

    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy, archived_at) "
            "VALUES ('Архивный дубль', 'Media', 'https://arch.example', FALSE, 'rss', now())"
        )
        conn.commit()

    repository.add_rss_source(
        name="Архивный дубль", rss_url="https://arch.example/feed",
        source_type="Media", url="https://arch.example",
    )

    with connection.get_connection() as conn:
        enabled, archived = conn.execute(
            "SELECT enabled, archived_at FROM sources WHERE name = 'Архивный дубль'"
        ).fetchone()
    assert enabled is False, "повторное добавление не должно включать архивный источник"
    assert archived is not None, "архивная пометка должна пережить повторное добавление"


def test_deleting_scoring_criterion_refuses_to_break_weight_sum(isolated_db):
    """Мина, найденная 11.09: «Сохранить» сумму весов проверяет, а «Удалить» — нет.

    Заказчик в тот день просил «убрать старые, не актуальные» критерии. По одному это
    оставляло сумму != 100, и следующая стадия скоринга падала ЦЕЛИКОМ, ещё до первой
    статьи (_validate_weights в pipeline). Удаление обязано отказывать, а не ломать.
    """
    app = api.app
    with connection.get_connection() as conn:
        user_id = conn.execute(
            "INSERT INTO users (email, password_salt, password_hash, role) "
            "VALUES ('score@example.com', 'salt', 'hash', 'admin') RETURNING id"
        ).fetchone()[0]
        keep_id = conn.execute(
            "INSERT INTO scoring_criteria (name, weight, enabled, sort_order) "
            "VALUES ('Технологическая новизна', 70, TRUE, 1) RETURNING id"
        ).fetchone()[0]
        drop_id = conn.execute(
            "INSERT INTO scoring_criteria (name, weight, enabled, sort_order) "
            "VALUES ('Устаревший критерий', 30, TRUE, 2) RETURNING id"
        ).fetchone()[0]
        conn.commit()

    app.dependency_overrides[api.require_admin] = lambda: {"id": user_id, "email": "score@example.com", "role": "admin"}
    try:
        client = TestClient(app)
        response = client.delete(f"/api/scoring-criteria/{drop_id}")
        assert response.status_code == 400, "удаление, ломающее сумму весов, обязано отклоняться"
        assert "100" in response.json()["detail"]

        with connection.get_connection() as conn:
            still_enabled = conn.execute(
                "SELECT enabled FROM scoring_criteria WHERE id = %s", (drop_id,)
            ).fetchone()[0]
        assert still_enabled is True, "отклонённое удаление не должно ничего менять"

        # Довели оставшийся критерий до 100 — теперь удаление разрешено.
        with connection.get_connection() as conn:
            conn.execute("UPDATE scoring_criteria SET weight = 100 WHERE id = %s", (keep_id,))
            conn.commit()
        assert client.delete(f"/api/scoring-criteria/{drop_id}").status_code == 200
    finally:
        app.dependency_overrides.clear()


def _feedback_fixture(conn, email: str = "fb@example.com"):
    user_id = conn.execute(
        "INSERT INTO users (email, password_salt, password_hash, role) "
        "VALUES (%s, 'salt', 'hash', 'admin') RETURNING id", (email,)
    ).fetchone()[0]
    source_id = conn.execute(
        "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
        "VALUES ('Neftegaz.ru', 'Media', 'https://neftegaz.example', TRUE, 'request') RETURNING id"
    ).fetchone()[0]
    article_id = conn.execute(
        "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language) "
        "VALUES (%s, 'Статья про ГРП', 'https://neftegaz.example/a1', now(), now(), 'Текст.', 'ru') "
        "RETURNING id", (source_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO article_cards (article_id, summary, relevant) VALUES (%s, 'Суть', TRUE)",
        (article_id,),
    )
    conn.commit()
    return user_id, source_id, article_id


def test_feedback_saves_scores_and_comment_and_is_editable(isolated_db):
    """ОС по сигналу: оценки 1–5 + быстрая причина + комментарий, одна карточка на пару.

    Повторное сохранение ПРАВИТ карточку, а не плодит дубли — иначе обучающая выборка
    перекосится теми статьями, которые человек открывал чаще.
    """
    app = api.app
    with connection.get_connection() as conn:
        user_id, source_id, article_id = _feedback_fixture(conn)

    app.dependency_overrides[api.require_user] = lambda: {"id": user_id, "email": "fb@example.com", "role": "admin"}
    app.dependency_overrides[api.require_admin] = lambda: {"id": user_id, "email": "fb@example.com", "role": "admin"}
    try:
        client = TestClient(app)
        first = client.post("/api/feedback", json={
            "article_id": article_id, "reason": "bad_translation",
            "usefulness": 4, "translation": 2,
            "comment": "walking island rig → шагающая буровая для искусственных островов",
        })
        assert first.status_code == 200, first.text
        entry = first.json()["entry"]
        assert entry["usefulness"] == 4 and entry["translation"] == 2
        # source_id подставился из статьи, хотя его не передавали.
        assert entry["source_id"] == source_id

        # Частичное сохранение не должно стирать уже написанный комментарий.
        second = client.post("/api/feedback", json={"article_id": article_id, "source_quality": 5})
        assert second.status_code == 200
        updated = second.json()["entry"]
        assert updated["source_quality"] == 5
        assert "шагающая буровая" in updated["comment"], "комментарий затёрся частичным сохранением"
        assert updated["id"] == entry["id"], "должна править ту же карточку, а не создавать новую"

        loaded = client.get("/api/feedback", params={"article_id": article_id}).json()["entry"]
        assert loaded["reason"] == "bad_translation"
    finally:
        app.dependency_overrides.clear()


def test_feedback_rejects_bad_scores_and_empty_target(isolated_db):
    """Границы проверяем на входе: мусорная оценка испортит обучающую выборку молча."""
    app = api.app
    with connection.get_connection() as conn:
        user_id, _source_id, article_id = _feedback_fixture(conn, "fb2@example.com")
    app.dependency_overrides[api.require_user] = lambda: {"id": user_id, "email": "fb2@example.com", "role": "admin"}
    try:
        client = TestClient(app)
        assert client.post("/api/feedback", json={"article_id": article_id, "usefulness": 9}).status_code == 400
        assert client.post("/api/feedback", json={"article_id": article_id, "reason": "неведомая"}).status_code == 400
        assert client.post("/api/feedback", json={"comment": "без цели"}).status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_marking_status_writes_feedback_event_with_old_value(isolated_db):
    """Ответ на вопрос заказчика «я всё что выделил как шум — он на этом обучился?».

    До 12.09 signal_feedback_events была мёртвой: писала в неё одна CLI-команда, а
    боевой путь пометки не писал вовсе. Теперь каждая пометка оставляет след с
    ПРЕЖНИМ значением — без него нельзя отличить «пометил шумом» от «передумал».
    """
    app = api.app
    with connection.get_connection() as conn:
        user_id, _source_id, article_id = _feedback_fixture(conn, "fb3@example.com")
    app.dependency_overrides[api.require_user] = lambda: {"id": user_id, "email": "fb3@example.com", "role": "admin"}
    try:
        client = TestClient(app)
        assert client.patch(f"/api/articles/{article_id}", json={"status": "digest"}).status_code == 200
        assert client.patch(f"/api/articles/{article_id}", json={"status": "noise"}).status_code == 200

        with connection.get_connection() as conn:
            events = conn.execute(
                "SELECT event_type, old_value, new_value FROM signal_feedback_events "
                "WHERE article_id = %s ORDER BY id", (article_id,)
            ).fetchall()
        assert [e[0] for e in events] == ["added_to_digest", "marked_noise"]
        assert events[0][1] is None, "первая пометка: прежнего статуса не было"
        assert events[1][1] == "digest", "во второй записи обязан быть прежний статус"
        assert events[1][2] == "noise"
    finally:
        app.dependency_overrides.clear()


def test_renaming_parent_tag_does_not_silently_orphan_subtags(isolated_db):
    """Тихий дефект: связь родитель-подтег хранилась ИМЕНЕМ, и переименование родителя
    делало все его подтеги КОРНЕВЫМИ — без ошибки и без предупреждения.

    Заказчик 07.09 сказал, что будет расширять теги («решил их расширить»), так что
    дерево разваливалось бы на первом же редактировании. Сохранение с разорванной
    связью теперь отклоняется целиком, а не портит дерево наполовину.
    """
    app = api.app
    with connection.get_connection() as conn:
        user_id = conn.execute(
            "INSERT INTO users (email, password_salt, password_hash, role) "
            "VALUES ('tags@example.com', 'salt', 'hash', 'admin') RETURNING id"
        ).fetchone()[0]
        conn.commit()
    app.dependency_overrides[api.require_admin] = lambda: {"id": user_id, "email": "tags@example.com", "role": "admin"}
    app.dependency_overrides[api.require_user] = lambda: {"id": user_id, "email": "tags@example.com", "role": "admin"}
    try:
        client = TestClient(app)
        saved = client.put("/api/tags", json=[
            {"name": "Бурение", "enabled": True, "sort_order": 1,
             "keywords_json": ["бурение"], "keywords_en_json": ["drilling"]},
            {"name": "Направленное бурение", "parent_name": "Бурение", "enabled": True, "sort_order": 2},
        ])
        assert saved.status_code == 200, saved.text

        with connection.get_connection() as conn:
            parent_id, = conn.execute("SELECT id FROM tags WHERE name = 'Бурение'").fetchone()
            child_parent, = conn.execute(
                "SELECT parent_id FROM tags WHERE name = 'Направленное бурение'"
            ).fetchone()
        assert child_parent == parent_id

        # Родителя переименовали, а подтег всё ещё ссылается на СТАРОЕ имя.
        broken = client.put("/api/tags", json=[
            {"id": parent_id, "name": "Бурение и заканчивание", "enabled": True, "sort_order": 1},
            {"name": "Направленное бурение", "parent_name": "Бурение", "enabled": True, "sort_order": 2},
        ])
        assert broken.status_code == 200, "старое имя обязано продолжать вести к тому же родителю"
        with connection.get_connection() as conn:
            still_child, = conn.execute(
                "SELECT parent_id FROM tags WHERE name = 'Направленное бурение'"
            ).fetchone()
        assert still_child == parent_id, "подтег не должен стать корневым после переименования"

        # А вот ссылка на родителя, которого в списке нет вовсе, — это ошибка, а не тишина.
        orphan = client.put("/api/tags", json=[
            {"name": "Совсем другой", "enabled": True, "sort_order": 1},
            {"name": "Сирота", "parent_name": "Несуществующий", "enabled": True, "sort_order": 2},
        ])
        assert orphan.status_code == 400
        assert "корневыми" in orphan.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_seed_tags_merges_keywords_instead_of_overwriting(isolated_db):
    """Сид обязан ДОПОЛНЯТЬ ключевые слова, а не заменять.

    С 12.09 их можно править в UI («Теги» → «Ключевые слова RU/EN»), а seed-tags
    запускается сам в bootstrap на каждом деплое. Прежняя перезапись молча стирала бы
    правки заказчика — и он бы об этом даже не узнал.
    """
    from oiltech_digest.db import repository

    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO tags (name, name_en, enabled, sort_order, keywords_json, keywords_en_json) "
            "VALUES ('Бурение', 'Drilling', TRUE, 1, '[\"правка Виктора\"]'::jsonb, '[\"viktor edit\"]'::jsonb)"
        )
        conn.commit()

    repository.upsert_tag({
        "parent_id": None, "name": "Бурение", "name_en": "Drilling",
        "description": "из сида",
        "keywords_json": ["бурение", "правка Виктора"],
        "keywords_en_json": ["drilling", "自动化钻机"],
        "sort_order": 1,
    })

    with connection.get_connection() as conn:
        ru, en = conn.execute(
            "SELECT keywords_json, keywords_en_json FROM tags WHERE name = 'Бурение'"
        ).fetchone()

    assert "правка Виктора" in ru, "сид затёр ручную правку"
    assert "бурение" in ru, "сид не добавил своё ключевое слово"
    assert "viktor edit" in en and "自动化钻机" in en
    # Без дублей: «правка Виктора» пришла и из базы, и из сида.
    assert ru.count("правка Виктора") == 1


def test_rescore_recompute_ignores_disabled_criteria(isolated_db):
    """Мина, найденная на проде 12.09: пересчёт джойнил критерии БЕЗ фильтра enabled.

    Заказчик 11.09 сменил профиль: пять активных дают ровно 100, но три ВЫКЛЮЧЕННЫХ
    несут ещё 75. Без фильтра сумма весов у старой статьи становилась 175 вместо 100 —
    баллы уезжали вверх без причины. И отдельно: статью, оценённую только по ныне
    выключенным критериям, пересчёт обнулил бы молча.
    """
    from oiltech_digest.db import repository

    with connection.get_connection() as conn:
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('S', 'Media', 'https://s.example', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        live = conn.execute(
            "INSERT INTO scoring_criteria (name, weight, enabled, sort_order) "
            "VALUES ('Активный', 100, TRUE, 1) RETURNING id"
        ).fetchone()[0]
        dead = conn.execute(
            "INSERT INTO scoring_criteria (name, weight, enabled, sort_order) "
            "VALUES ('Выключенный', 75, FALSE, 2) RETURNING id"
        ).fetchone()[0]

        def add_article(url: str, criteria: list[int]) -> int:
            aid = conn.execute(
                "INSERT INTO articles (source_id, title, url, collected_at, raw_text, language) "
                "VALUES (%s, 'T', %s, now(), 'x', 'ru') RETURNING id", (source_id, url)
            ).fetchone()[0]
            sid = conn.execute(
                "INSERT INTO article_scores (article_id, total_score, score_label) "
                "VALUES (%s, 50, 'Средняя') RETURNING id", (aid,)
            ).fetchone()[0]
            for cid in criteria:
                conn.execute(
                    "INSERT INTO article_score_items (article_score_id, criterion_id, ai_score, "
                    "keyword_score, final_score) VALUES (%s, %s, 80, 0, 80)", (sid, cid),
                )
            return aid

        mixed = add_article("https://s.example/mixed", [live, dead])
        orphan = add_article("https://s.example/orphan", [dead])
        conn.commit()

    repository.recompute_total_scores_from_items(keyword_weight=0.2, ai_weight=0.8)

    with connection.get_connection() as conn:
        mixed_total = conn.execute(
            "SELECT total_score FROM article_scores WHERE article_id = %s", (mixed,)
        ).fetchone()[0]
        orphan_total = conn.execute(
            "SELECT total_score FROM article_scores WHERE article_id = %s", (orphan,)
        ).fetchone()[0]

    # 80 * 100/100 = 80. С выключенным критерием было бы 80*175/100 = 140 → клампилось в 100.
    assert float(mixed_total) == 80, f"выключенный критерий всё ещё считается: {mixed_total}"
    # Статью без единого активного критерия не трогаем, а не обнуляем.
    assert float(orphan_total) == 50, "статью без активных критериев нельзя обнулять молча"


def test_seed_scoring_does_not_resurrect_disabled_criteria(isolated_db):
    """Мина, которая УЖЕ сработала на проде 11.09.

    `seed-scoring` запускается в bootstrap на КАЖДОМ деплое и делал
    `ON CONFLICT (name) DO UPDATE SET enabled = TRUE` — то есть воскрешал критерии,
    которые заказчик выключил в UI. 11.09 он утром собрал профиль из пяти критериев
    (сумма ровно 100), в 11:40 прошёл деплой, и через две минуты он написал
    «а что случилось со скорингом? там сейчас 9 параметров».

    Сумма весов при этом становится 175 вместо 100, и стадия скоринга падает целиком
    на первой же статье. Вес и флаг — территория человека, сид их не трогает.
    """
    from oiltech_digest.db import repository

    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO scoring_criteria (name, weight, enabled, sort_order, keywords_json) "
            "VALUES ('Технологическая новизна', 35, FALSE, 1, '[\"ручная правка\"]'::jsonb)"
        )
        conn.commit()

    repository.upsert_scoring_criterion({
        "name": "Технологическая новизна", "description": "из сида", "weight": 35,
        "keywords_json": ["из сида"], "keywords_en_json": [], "sort_order": 1,
    })

    with connection.get_connection() as conn:
        enabled, weight, kw = conn.execute(
            "SELECT enabled, weight, keywords_json FROM scoring_criteria "
            "WHERE name = 'Технологическая новизна'"
        ).fetchone()

    assert enabled is False, "сид воскресил выключенный заказчиком критерий"
    assert float(weight) == 35
    assert "ручная правка" in kw, "сид затёр ручные ключевые слова"
    assert "из сида" in kw, "сид не добавил своё"


def test_insert_article_dedups_url_variants(isolated_db):
    """Один и тот же материал заводился по нескольку раз: уникальность держалась на СЫРОМ url.

    Замер прода 13.09: за 90 дней 940 лишних статей, причины ровно три —
    query-хвосты (?from=main_lines_11 против ?from=newsfeed у РБК), схема (http против
    https у Ростеха) и хвостовой слэш (Wood Mackenzie). Каждая копия жгла полный
    ИИ-конвейер заново и занимала отдельную карточку — заказчик 08.09 прислал скрин
    «все 4 новости об одном».
    """
    from oiltech_digest.db import repository

    with connection.get_connection() as conn:
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('РБК', 'Media', 'https://rbc.example', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        conn.commit()

    def add(url: str, text: str = "Текст статьи." * 30) -> bool:

        return repository.insert_article({

            "source_id": source_id, "title": "Одна и та же новость", "url": url,

            "published_at": None, "raw_text": text,

            "text_truncated": False, "language": "ru",

            "content_hash": f"h-{url}", "image_url": None,

        })

    assert add("https://www.rbc.example/news/123?from=main_lines_11") is True
    # Те же три варианта, что реально встретились на проде.
    assert add("https://www.rbc.example/news/123?from=newsfeed") is False, "query-хвост"
    assert add("http://rbc.example/news/123") is False, "другая схема и без www"
    assert add("https://www.rbc.example/news/123/") is False, "хвостовой слэш"
    # Другая статья того же источника обязана пройти — со СВОИМ телом: одинаковый
    # текст у разных адресов одного источника отбивает отдельная защита ниже.
    assert add("https://www.rbc.example/news/999", "Совсем другой текст. " * 30) is True

    # Третий рубеж: тело уже есть у другой статьи этого источника. Так на проде
    # набралось 830 «статей» — страницы навигации сайта, на которые сервер отдаёт
    # одну и ту же оболочку (у Новатэка 82 из 86).
    assert add("https://www.rbc.example/about/contacts", "Совсем другой текст. " * 30) is False, "повтор тела"

    with connection.get_connection() as conn:
        total = conn.execute(
            "SELECT count(*) FROM articles WHERE source_id = %s", (source_id,)
        ).fetchone()[0]
    assert total == 2, f"должно остаться 2 статьи, а не {total}"


def test_saving_tags_survives_null_keyword_lists(isolated_db):
    """Скрин заказчика 13.09: сохранение экрана «Теги» падало 422 на девяти строках подряд —
    «Input should be a valid list», input: null.

    Причина: `negative_keywords_json` равен NULL у ВСЕХ тегов в БД, `list_enabled_tags`
    отдаёт его как есть, фронт возвращает то же самое, а значение по умолчанию в Pydantic
    срабатывает только когда ключ ОТСУТСТВУЕТ. То есть экран не сохранялся вообще никогда.
    """
    app = api.app
    with connection.get_connection() as conn:
        user_id = conn.execute(
            "INSERT INTO users (email, password_salt, password_hash, role) "
            "VALUES ('nulls@example.com', 'salt', 'hash', 'admin') RETURNING id"
        ).fetchone()[0]
        conn.commit()
    app.dependency_overrides[api.require_admin] = lambda: {"id": user_id, "email": "n@e.ru", "role": "admin"}
    app.dependency_overrides[api.require_user] = lambda: {"id": user_id, "email": "n@e.ru", "role": "admin"}
    try:
        client = TestClient(app)
        # Ровно та форма, что уходит с фронта после чтения тега из БД.
        response = client.put("/api/tags", json=[{
            "id": None, "parent_name": None, "name": "Бурение",
            "name_en": None, "description": "Описание",
            "keywords_json": None, "keywords_en_json": None,
            "negative_keywords_json": None,
            "enabled": True, "sort_order": 1,
        }])
        assert response.status_code == 200, f"сохранение тегов снова падает: {response.text}"

        with connection.get_connection() as conn:
            kw, neg = conn.execute(
                "SELECT keywords_json, negative_keywords_json FROM tags WHERE name = 'Бурение'"
            ).fetchone()
        assert kw == [] and neg == [], "null должен превращаться в пустой список, а не падать"

        # Критерии скоринга страдали тем же — проверяем и их.
        assert client.put("/api/scoring-criteria", json=[{
            "id": None, "name": "Единственный", "description": None, "weight": 100,
            "keywords_json": None, "keywords_en_json": None, "enabled": True, "sort_order": 1,
        }]).status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_seed_13_themes_replaces_old_taxonomy(isolated_db):
    """13.09: заказчик прислал список из 13 тематик — оставить только их.

    Прежние теги ВЫКЛЮЧАЮТСЯ, а не удаляются: на них ссылается article_tags со всей
    историей классификации корпуса.
    """
    from oiltech_digest.db import repository
    from oiltech_digest.processing.seed import seed_tags_13, UNCLASSIFIED_TAG

    with connection.get_connection() as conn:
        conn.execute("INSERT INTO tags (name, enabled, sort_order) VALUES ('Старое направление', TRUE, 1)")
        conn.commit()

    from oiltech_digest.processing.seed import retire_old_tags

    stats = seed_tags_13()
    assert stats["tags"] == 14, "13 тематик заказчика плюс приёмник для неклассифицированного"
    # Выключение прежней таксономии — ОТДЕЛЬНАЯ команда: сид идёт на каждом деплое и не
    # имеет права отменять правки заказчика (см. test_seed_does_not_disable_tags_renamed…).
    assert stats["disabled"] == 0
    assert retire_old_tags()["disabled"] >= 1, "разовая миграция обязана выключить прежние"

    with connection.get_connection() as conn:
        old_enabled, = conn.execute(
            "SELECT enabled FROM tags WHERE name = 'Старое направление'"
        ).fetchone()
        enabled_names = [r[0] for r in conn.execute("SELECT name FROM tags WHERE enabled ORDER BY sort_order")]
        stops = conn.execute(
            "SELECT COALESCE(SUM(jsonb_array_length(COALESCE(negative_keywords_json,'[]'::jsonb))),0) "
            "FROM tags WHERE enabled"
        ).fetchone()[0]

    assert old_enabled is False, "старый тег выключен, но не удалён"
    assert len(enabled_names) == 14
    assert UNCLASSIFIED_TAG in enabled_names, "нужен явный приёмник, иначе статья молча уедет в первый тег"
    # Стоп-слова из файла заказчика обязаны доехать: раньше upsert_tag их не писал ВООБЩЕ,
    # и механизм подавления шума стоял пустым на всём проде.
    assert stops > 0, "стоп-слова из файла заказчика не доехали до базы"

    with connection.get_connection() as conn:
        neg = conn.execute(
            "SELECT negative_keywords_json FROM tags WHERE name LIKE 'Бурение%' AND enabled"
        ).fetchone()[0]
    assert "oil painting" in neg, "конкретное стоп-слово из файла не сохранилось"
    # Разделитель «---» из конца файла не должен попасть в стоп-слова: как подстрока
    # он отбивал бы статьи пачками.
    assert not any(str(w).strip("—-– ") == "" for w in neg)


def test_retag_reset_returns_articles_to_tagging_queue(isolated_db):
    """Смена таксономии оставляет 11 тысяч статей с классификацией по мёртвому справочнику.

    Выборка тегирования берёт статьи БЕЗ тега, поэтому снятие связи — и есть способ
    вернуть статью в очередь. Снимаются только связи с ВЫКЛЮЧЕННЫМИ тегами.
    """
    from oiltech_digest.db import repository

    with connection.get_connection() as conn:
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('S', 'Media', 'https://s.example', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        old_tag = conn.execute(
            "INSERT INTO tags (name, enabled, sort_order) VALUES ('Мёртвый тег', FALSE, 1) RETURNING id"
        ).fetchone()[0]
        live_tag = conn.execute(
            "INSERT INTO tags (name, enabled, sort_order) VALUES ('Живой тег', TRUE, 2) RETURNING id"
        ).fetchone()[0]

        def add(url: str, tag_id: int) -> int:
            aid = conn.execute(
                "INSERT INTO articles (source_id, title, url, collected_at, raw_text, language) "
                "VALUES (%s, 'T', %s, now(), 'x', 'ru') RETURNING id", (source_id, url)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO article_tags (article_id, tag_id, confidence) VALUES (%s, %s, 0.9)",
                (aid, tag_id),
            )
            return aid

        stale = add("https://s.example/stale", old_tag)
        fresh = add("https://s.example/fresh", live_tag)
        conn.commit()

    removed = repository.clear_article_tags_for_disabled()
    assert removed == 1, "снять нужно только связь с выключенным тегом"

    with connection.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM article_tags WHERE article_id = %s", (stale,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM article_tags WHERE article_id = %s", (fresh,)).fetchone()[0] == 1
        # Сами статьи не тронуты.
        assert conn.execute("SELECT count(*) FROM articles WHERE id IN (%s,%s)", (stale, fresh)).fetchone()[0] == 2


def test_seed_does_not_disable_tags_renamed_by_customer(isolated_db):
    """Сид идёт на КАЖДОМ деплое и не должен отменять правки заказчика.

    Если бы он гасил всё, чего нет в файле, первое же переименование тега в UI
    отменялось бы следующей выкаткой: сид создал бы тег с исходным именем, а
    переименованный выключил. Этот класс ошибки уже сработал с критериями скоринга 11.09.
    """
    from oiltech_digest.processing.seed import seed_tags_13, retire_old_tags

    with connection.get_connection() as conn:
        conn.execute("INSERT INTO tags (name, enabled, sort_order) VALUES ('Тег Виктора', TRUE, 1)")
        conn.commit()

    stats = seed_tags_13()
    assert stats["disabled"] == 0, "сид не имеет права ничего выключать"

    with connection.get_connection() as conn:
        alive, = conn.execute("SELECT enabled FROM tags WHERE name = 'Тег Виктора'").fetchone()
    assert alive is True, "сид погасил тег, которого нет в его файле"

    # Смена таксономии — отдельная осознанная команда.
    assert retire_old_tags()["disabled"] >= 1
    with connection.get_connection() as conn:
        retired, = conn.execute("SELECT enabled FROM tags WHERE name = 'Тег Виктора'").fetchone()
    assert retired is False


def test_saving_incomplete_tag_list_is_refused(isolated_db):
    """Страховка от потери таксономии одним нажатием «Сохранить».

    Сохранение выключает всё, чего нет в присланном списке. 13.09 проверочный запрос
    с ОДНИМ тегом выключил разом все 14 — неполный список (обрыв загрузки, частичный
    рендер) стирал бы справочник молча. Порог мягкий: сокращение набора законно
    (мы сами ужали 18 направлений до 13), запрещаем только обвал больше чем наполовину.
    """
    app = api.app
    with connection.get_connection() as conn:
        user_id = conn.execute(
            "INSERT INTO users (email, password_salt, password_hash, role) "
            "VALUES ('guard@example.com', 'salt', 'hash', 'admin') RETURNING id"
        ).fetchone()[0]
        for i in range(1, 7):
            conn.execute(
                "INSERT INTO tags (name, enabled, sort_order) VALUES (%s, TRUE, %s)",
                (f"Тема {i}", i),
            )
        conn.commit()

    app.dependency_overrides[api.require_admin] = lambda: {"id": user_id, "email": "g@e.ru", "role": "admin"}
    try:
        client = TestClient(app)
        # Прислали один тег вместо шести — это выключило бы пять из шести.
        response = client.put("/api/tags", json=[
            {"id": None, "parent_name": None, "name": "Тема 1", "name_en": None,
             "description": None, "keywords_json": None, "keywords_en_json": None,
             "negative_keywords_json": None, "enabled": True, "sort_order": 1},
        ])
        assert response.status_code == 400, "неполный список обязан отклоняться"
        assert "больше половины" in response.json()["detail"]

        with connection.get_connection() as conn:
            still_on = conn.execute("SELECT count(*) FROM tags WHERE enabled").fetchone()[0]
        assert still_on == 6, "отклонённое сохранение не должно ничего менять"

        # Законное сокращение (4 из 6) по-прежнему проходит.
        ok = client.put("/api/tags", json=[
            {"id": None, "parent_name": None, "name": f"Тема {i}", "name_en": None,
             "description": None, "keywords_json": None, "keywords_en_json": None,
             "negative_keywords_json": None, "enabled": True, "sort_order": i}
            for i in range(1, 5)
        ])
        assert ok.status_code == 200, ok.text
    finally:
        app.dependency_overrides.clear()


def test_system_tag_survives_save_and_delete(isolated_db):
    """Тег-приёмник «Не классифицировано» нельзя выключить обычной работой с экраном.

    13.09 он выключился молча: сохранение прислало 13 тематик, его среди них не было,
    и страховка «больше половины» не сработала — один тег из четырнадцати. Итог:
    pipeline._fallback_tag не находил приёмник и уводил весь непонятый поток в первый
    тег списка, то есть в «Геологоразведку». Это ровно тот дефект, ради которого
    приёмник и заводили.
    """
    from oiltech_digest.db import repository

    app = api.app
    with connection.get_connection() as conn:
        user_id = conn.execute(
            "INSERT INTO users (email, password_salt, password_hash, role) "
            "VALUES ('systag@example.com', 'salt', 'hash', 'admin') RETURNING id"
        ).fetchone()[0]
        for i in range(1, 5):
            conn.execute(
                "INSERT INTO tags (name, enabled, sort_order) VALUES (%s, TRUE, %s)",
                (f"Тема {i}", i),
            )
        system_id = conn.execute(
            "INSERT INTO tags (name, enabled, sort_order) VALUES (%s, TRUE, 99) RETURNING id",
            (repository.SYSTEM_TAG_UNCLASSIFIED,),
        ).fetchone()[0]
        conn.commit()

    app.dependency_overrides[api.require_admin] = lambda: {"id": user_id, "email": "s@e.ru", "role": "admin"}
    try:
        client = TestClient(app)
        # Экран прислал только тематики — приёмника в списке нет.
        response = client.put("/api/tags", json=[
            {"id": None, "parent_name": None, "name": f"Тема {i}", "name_en": None,
             "description": None, "keywords_json": None, "keywords_en_json": None,
             "negative_keywords_json": None, "enabled": True, "sort_order": i}
            for i in range(1, 5)
        ])
        assert response.status_code == 200, response.text

        with connection.get_connection() as conn:
            still_on = conn.execute(
                "SELECT enabled FROM tags WHERE id = %s", (system_id,)
            ).fetchone()[0]
        assert still_on is True, "приёмник обязан пережить сохранение без него в списке"

        # И удалить его руками тоже нельзя — это не тематика.
        deleted = client.delete(f"/api/tags/{system_id}")
        assert deleted.status_code == 400, deleted.text
        assert "служебный приёмник" in deleted.json()["detail"]
        with connection.get_connection() as conn:
            assert conn.execute(
                "SELECT enabled FROM tags WHERE id = %s", (system_id,)
            ).fetchone()[0] is True
    finally:
        app.dependency_overrides.clear()


def test_full_text_refetch_skips_external_sources(isolated_db):
    """Источники зарубежного контура пропускаются локальной дозагрузкой.

    Попытка одна и навсегда: 403 с РФ-адреса пометил бы статью failed, и она больше
    никогда не переспрашивалась бы — даже когда тело уже добрал зарубежный воркер.
    """
    from oiltech_digest.db import repository

    with connection.get_connection() as conn:
        ru = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy, network_region) "
            "VALUES ('РФ', 'Media', 'https://ru.example', TRUE, 'rss', 'auto') RETURNING id"
        ).fetchone()[0]
        ext = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy, network_region) "
            "VALUES ('Запад', 'Media', 'https://west.example', TRUE, 'rss', 'external') RETURNING id"
        ).fetchone()[0]
        for sid, slug in ((ru, "ru"), (ext, "ext")):
            conn.execute(
                "INSERT INTO articles (source_id, title, url, raw_text, text_truncated, language) "
                "VALUES (%s, 't', %s, 'коротко', TRUE, 'ru')",
                (sid, f"https://{slug}.example/a"),
            )
        conn.commit()

    rows = repository.get_articles_needing_full_text(limit=50)
    source_ids = {row["source_id"] for row in rows}
    assert ru in source_ids, "локальный источник должен попасть в дозагрузку"
    assert ext not in source_ids, "внешний источник дозагружается воркером, не локально"


def test_external_refetch_candidates_only_external_stubs(isolated_db):
    """Кандидаты на дозаполнение — только обрывки внешних источников.

    Локальные берёт обычная дозагрузка; полные статьи трогать незачем.
    Уже помеченные failed берём: пометка ставилась локальной попыткой и отражает
    недоступность с РФ-адреса, а не непригодность статьи.
    """
    from oiltech_digest.db import repository

    with connection.get_connection() as conn:
        ext = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy, network_region) "
            "VALUES ('Запад', 'Media', 'https://w.example', TRUE, 'rss', 'external') RETURNING id"
        ).fetchone()[0]
        ru = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy, network_region) "
            "VALUES ('РФ', 'Media', 'https://r.example', TRUE, 'rss', 'auto') RETURNING id"
        ).fetchone()[0]
        rows = [
            (ext, "https://w.example/stub", "коротко", None),
            (ext, "https://w.example/failed", "коротко", "failed"),
            (ext, "https://w.example/full", "длинный текст " * 200, None),
            (ru, "https://r.example/stub", "коротко", None),
        ]
        for sid, url, text, status in rows:
            conn.execute(
                "INSERT INTO articles (source_id, title, url, raw_text, language, full_text_status) "
                "VALUES (%s, 't', %s, %s, 'ru', %s)", (sid, url, text, status))
        conn.commit()

    urls = {r["url"] for r in repository.external_refetch_candidates(limit=50)}
    assert "https://w.example/stub" in urls
    assert "https://w.example/failed" in urls, "failed ставила локальная попытка, повторяем через воркер"
    assert "https://w.example/full" not in urls, "полная статья не нуждается в дозаполнении"
    assert "https://r.example/stub" not in urls, "локальный источник берёт обычная дозагрузка"


def test_feed_and_digest_skip_reprints(isolated_db):
    """Пометка перепечатки должна что-то значить: копия уходит из ленты и из выпуска.

    Без этого условия таблица была бы мёртвой записью — заполняется, а заказчик
    по-прежнему видит четыре карточки одной новости, ровно как 08.09.
    """
    with connection.get_connection() as conn:
        s1 = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('A', 'Media', 'https://a.example', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        s2 = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('B', 'Media', 'https://b.example', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        main_id = conn.execute(
            "INSERT INTO articles (source_id, title, url, raw_text, language) "
            "VALUES (%s, 'Главная копия', 'https://a.example/x', 'текст', 'ru') RETURNING id",
            (s1,),
        ).fetchone()[0]
        copy_id = conn.execute(
            "INSERT INTO articles (source_id, title, url, raw_text, language) "
            "VALUES (%s, 'Перепечатка', 'https://b.example/x', 'текст', 'ru') RETURNING id",
            (s2,),
        ).fetchone()[0]
        conn.commit()

    from oiltech_digest.db import repository

    repository.mark_article_reprint(
        article_id=copy_id, primary_id=main_id, similarity=0.7,
        reason="одно событие", decided_by="test",
    )

    with connection.get_connection() as conn:
        hidden = conn.execute(
            "SELECT count(*) FROM articles a "
            "WHERE a.id = %s AND NOT EXISTS "
            "(SELECT 1 FROM article_reprints ar WHERE ar.article_id = a.id)",
            (copy_id,),
        ).fetchone()[0]
        kept = conn.execute(
            "SELECT count(*) FROM articles a "
            "WHERE a.id = %s AND NOT EXISTS "
            "(SELECT 1 FROM article_reprints ar WHERE ar.article_id = a.id)",
            (main_id,),
        ).fetchone()[0]
    assert hidden == 0, "копия обязана уйти из выборки"
    assert kept == 1, "главная копия обязана остаться"


def test_reprint_rejected_when_primary_is_invisible(isolated_db):
    """Главной копией не может стать статья, которой в ленте нет.

    Замер на проде 17.09 (71 пара, признанная судьёй дублем): в 6 парах главной
    становилась невидимая статья — архивный источник или отбитая гейтом. Пометка
    тогда не схлопывает дубль, а убирает новость из ленты целиком: видимую копию
    прячем, а взамен не показывается ничего. Это ровно та жалоба заказчика
    («материалы исчезают»), ради которой перепечатки вообще помечаются, а не
    удаляются.
    """
    from oiltech_digest.db import repository

    with connection.get_connection() as conn:
        live = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('Живой', 'Media', 'https://live.example', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        archived = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy, archived_at) "
            "VALUES ('Архивный', 'Media', 'https://arch.example', TRUE, 'rss', now()) RETURNING id"
        ).fetchone()[0]
        visible_id = conn.execute(
            "INSERT INTO articles (source_id, title, url, raw_text, language) "
            "VALUES (%s, 'Видимая копия', 'https://live.example/x', 'короткий', 'ru') RETURNING id",
            (live,),
        ).fetchone()[0]
        hidden_id = conn.execute(
            "INSERT INTO articles (source_id, title, url, raw_text, language) "
            "VALUES (%s, 'Копия из архива', 'https://arch.example/x', 'текст длиннее', 'ru') RETURNING id",
            (archived,),
        ).fetchone()[0]
        conn.commit()

    with pytest.raises(ValueError, match="не видна в ленте"):
        repository.mark_article_reprint(
            article_id=visible_id, primary_id=hidden_id, similarity=0.7,
            reason="одно событие", decided_by="test",
        )

    with connection.get_connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM article_reprints WHERE article_id = %s", (visible_id,)
        ).fetchone()[0] == 0, "видимая копия обязана остаться в ленте"


def test_reprint_candidates_skip_invisible_articles(isolated_db):
    """Правило не должно предлагать судье статьи, которых никто не видит.

    Там же, в замере 17.09: 39 пар из 71 состояли ИЗ ДВУХ невидимых статей —
    модель звали и платили за неё впустую, схлопывать было нечего.
    """
    from oiltech_digest.db import repository

    title_a = "Газпром нефть испытала российские буровые установки на Ямале"
    title_b = "Газпром нефть испытала российские буровые установки в Арктике"
    with connection.get_connection() as conn:
        live = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('Живой2', 'Media', 'https://live2.example', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        archived = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy, archived_at) "
            "VALUES ('Архивный2', 'Media', 'https://arch2.example', TRUE, 'rss', now()) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO articles (source_id, title, url, raw_text, language, published_at) "
            "VALUES (%s, %s, 'https://live2.example/x', 'текст', 'ru', now())",
            (live, title_a),
        )
        conn.execute(
            "INSERT INTO articles (source_id, title, url, raw_text, language, published_at) "
            "VALUES (%s, %s, 'https://arch2.example/x', 'текст', 'ru', now())",
            (archived, title_b),
        )
        conn.commit()

    pairs = repository.reprint_candidates(days=7, min_overlap=0.3, limit=50)
    titles = {str(p["a_title"]) for p in pairs} | {str(p["b_title"]) for p in pairs}
    assert title_b not in titles, "статья из архивного источника не должна попадать в кандидаты"


def test_marking_article_as_its_own_reprint_is_rejected(isolated_db):
    from oiltech_digest.db import repository

    with pytest.raises(ValueError):
        repository.mark_article_reprint(
            article_id=1, primary_id=1, similarity=None, reason=None)


def test_reprint_chain_resolves_to_group_root(isolated_db):
    """Пометки попарные, поэтому главной назначается КОРЕНЬ группы, а не сосед.

    Без этого получалась бы цепочка C→A→D, и фильтр ленты унёс бы из выборки всю
    группу вместе с оригиналом.
    """
    from oiltech_digest.db import repository

    with connection.get_connection() as conn:
        src = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('S', 'Media', 'https://s.example', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        ids = []
        for n in ("A", "B", "C"):
            ids.append(conn.execute(
                "INSERT INTO articles (source_id, title, url, raw_text, language) "
                "VALUES (%s, %s, %s, 'текст', 'ru') RETURNING id",
                (src, n, f"https://s.example/{n}"),
            ).fetchone()[0])
        conn.commit()
    a, b, c = ids

    repository.mark_article_reprint(article_id=b, primary_id=a, similarity=0.6,
                                    reason="одно событие", decided_by="test")
    # Просим сделать главной B, которая сама уже копия A → корень остаётся A.
    repository.mark_article_reprint(article_id=c, primary_id=b, similarity=0.6,
                                    reason="одно событие", decided_by="test")

    with connection.get_connection() as conn:
        rows = dict(conn.execute(
            "SELECT article_id, primary_id FROM article_reprints").fetchall())
    assert rows[b] == a
    assert rows[c] == a, "цепочка не разрешена до корня — оригинал исчез бы из ленты"
    assert a not in rows, "корень группы не может быть помечен копией"


def test_unmark_returns_article_to_feed(isolated_db):
    """Решение принимает модель — у человека должен быть способ его отменить."""
    from oiltech_digest.db import repository

    with connection.get_connection() as conn:
        src = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('S2', 'Media', 'https://s2.example', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        a = conn.execute("INSERT INTO articles (source_id, title, url, raw_text, language) "
                         "VALUES (%s,'A','https://s2.example/a','t','ru') RETURNING id",
                         (src,)).fetchone()[0]
        b = conn.execute("INSERT INTO articles (source_id, title, url, raw_text, language) "
                         "VALUES (%s,'B','https://s2.example/b','t','ru') RETURNING id",
                         (src,)).fetchone()[0]
        conn.commit()

    repository.mark_article_reprint(article_id=b, primary_id=a, similarity=0.5,
                                    reason="r", decided_by="test")
    assert repository.unmark_article_reprint(b) is True
    assert repository.unmark_article_reprint(b) is False, "повторное снятие — не ошибка, но и не успех"


def _create_source_with_probe(monkeypatch, probe: dict, name: str) -> dict:
    monkeypatch.setattr(api, "probe_strategies", lambda url: probe)
    app = api.app
    app.dependency_overrides[api.require_admin] = lambda: {"id": 1, "email": "admin@example.com", "role": "admin"}
    try:
        response = TestClient(app).post("/api/sources", json={"name": name, "url": "https://site.example/news"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    body = response.json()
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT parse_strategy, listing_url, network_region, enabled FROM sources WHERE id = %s",
            (body["id"],),
        ).fetchone()
    return {"body": body, "row": row}


def test_create_source_stores_what_the_strategy_probe_chose(monkeypatch, isolated_db):
    """Ручной ввод ссылки: к ней пробуется каждая стратегия, и в источник пишется
    выбор — стратегия, лента и маршрут. До 18.09 без RSS молча ставился `request`."""
    probe = {"url": "https://site.example/news", "attempts": [],
             "chosen": {"parse_strategy": "playwright", "listing_url": "https://site.example/news",
                        "network_region": "external"}}
    got = _create_source_with_probe(monkeypatch, probe, "Probe chose external")
    assert got["row"] == ("playwright", "https://site.example/news", "external", True)
    assert got["body"]["probe"]["chosen"]["network_region"] == "external"


def test_create_source_is_disabled_when_no_strategy_found_articles(monkeypatch, isolated_db):
    """Сайт открылся, но статей не дала ни одна стратегия — включённым такой
    источник опрашивался бы вечно впустую."""
    got = _create_source_with_probe(monkeypatch, {"url": "https://site.example/news", "attempts": [],
                                                  "chosen": None}, "Probe found nothing")
    assert got["row"][3] is False
    assert got["body"]["enabled"] is False


def test_manual_import_holder_source_is_not_polled(isolated_db):
    """Держатель вручную внесённой статьи — не подписка на сайт. Включённым он
    опрашивался `request` по главной вечно (223 статьи научпопа 17.09)."""
    from oiltech_digest.ingestion import manual_import

    source = manual_import.find_or_create_source("https://new-domain.example/news/1", None)
    assert source["enabled"] is False
    assert source["name"] == "Manual import: new-domain.example"
