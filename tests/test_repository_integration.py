from __future__ import annotations

from datetime import datetime, timedelta, timezone

from oiltech_digest.db import connection
from oiltech_digest.db import repository


def test_monthly_digests_are_scoped_by_user(isolated_db):
    with connection.get_connection() as conn:
        user_rows = conn.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES
              ('first@example.com', 'salt', 'hash', 'user'),
              ('second@example.com', 'salt', 'hash', 'user')
            RETURNING id, email
            """
        ).fetchall()
        conn.commit()
    user_ids = {email: user_id for user_id, email in user_rows}

    first = repository.save_monthly_digest(
        "2026-06",
        "Digest for first user",
        [],
        user_id=user_ids["first@example.com"],
    )
    second = repository.save_monthly_digest(
        "2026-06",
        "Digest for second user",
        [],
        user_id=user_ids["second@example.com"],
    )

    assert first["id"] != second["id"]
    assert first["user_id"] == user_ids["first@example.com"]
    assert second["user_id"] == user_ids["second@example.com"]
    assert repository.get_monthly_digest("2026-06", user_id=user_ids["first@example.com"])["title"] == "Digest for first user"
    assert repository.get_monthly_digest("2026-06", user_id=user_ids["second@example.com"])["title"] == "Digest for second user"


def test_repository_dashboard_health_and_digest_queries_use_real_schema(isolated_db):
    now = datetime.now(timezone.utc)

    with connection.get_connection() as conn:
        # Выбор в дайджест ПЕР-ЮЗЕРНЫЙ (#12): dashboard_stats считает selected_for_digest
        # из user_article_states для конкретного user_id, а не из article_cards.
        analyst_id = conn.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES ('analyst@example.com', 'salt', 'hash', 'admin')
            RETURNING id
            """
        ).fetchone()[0]
        source_rows = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category)
            VALUES
              ('No Articles', 'News', 'https://example.com/empty', TRUE, 'request', 'международные'),
              ('Stale Source', 'News', 'https://example.com/stale', TRUE, 'rss', 'международные'),
              ('Healthy Source', 'News', 'https://example.com/ok', TRUE, 'playwright', 'международные'),
              ('Disabled Source', 'News', 'https://example.com/off', FALSE, 'telegram', 'telegram')
            RETURNING id, name
            """
        ).fetchall()
        source_ids = {name: source_id for source_id, name in source_rows}

        tag_parent = conn.execute(
            """
            INSERT INTO tags (name, enabled, sort_order)
            VALUES ('Технологии', TRUE, 1)
            RETURNING id
            """
        ).fetchone()[0]
        tag_child = conn.execute(
            """
            INSERT INTO tags (parent_id, name, enabled, sort_order)
            VALUES (%s, 'ГРП', TRUE, 1)
            RETURNING id
            """,
            (tag_parent,),
        ).fetchone()[0]

        article_rows = conn.execute(
            """
            INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language, content_hash)
            VALUES
              (%s, 'Old article', 'https://example.com/old', %s, %s, 'Old text', 'en', 'old-hash'),
              (%s, 'Digest candidate', 'https://example.com/digest', %s, %s, 'Electric frac text', 'en', 'digest-hash')
            RETURNING id, title
            """,
            (
                source_ids["Stale Source"],
                now - timedelta(days=10),
                now - timedelta(days=10),
                source_ids["Healthy Source"],
                now - timedelta(days=1),
                now - timedelta(days=1),
            ),
        ).fetchall()
        article_ids = {title: article_id for article_id, title in article_rows}

        conn.execute(
            """
            INSERT INTO article_cards (article_id, summary, relevant, status, selected_for_digest)
            VALUES
              (%s, 'Old summary', TRUE, 'new', FALSE),
              (%s, 'Digest candidate: Useful compact summary', TRUE, 'digest', TRUE)
            """,
            (article_ids["Old article"], article_ids["Digest candidate"]),
        )
        # Пер-юзерный выбор в дайджест (#12) — источник правды для selected_for_digest.
        conn.execute(
            """
            INSERT INTO user_article_states (user_id, article_id, status)
            VALUES (%s, %s, 'digest')
            """,
            (analyst_id, article_ids["Digest candidate"]),
        )
        conn.execute(
            """
            INSERT INTO article_tags (article_id, tag_id, confidence, rationale)
            VALUES (%s, %s, 0.9, 'keyword match')
            """,
            (article_ids["Digest candidate"], tag_child),
        )
        conn.execute(
            """
            INSERT INTO scoring_criteria (name, weight, enabled, sort_order)
            VALUES ('Технологическая значимость', 100, TRUE, 1)
            RETURNING id
            """
        ).fetchone()
        conn.execute(
            """
            INSERT INTO article_scores (article_id, model, total_score, score_label, explanation)
            VALUES
              (%s, 'offline', 50, 'Средняя', 'old'),
              (%s, 'offline', 90, 'Высокая', 'strong')
            """,
            (article_ids["Old article"], article_ids["Digest candidate"]),
        )
        conn.commit()

    stats = repository.dashboard_stats(user_id=analyst_id)
    assert stats == {
        "total_articles": 2,
        "with_summary": 2,
        "processed_articles": 2,
        # Весь объём базы под плитку «Всего». В этой фикстуре обе статьи — видимые сигналы,
        # поэтому all_articles совпадает с total_articles; семантику «считает и отсев,
        # и вычищенные» проверяет отдельный тест ниже.
        "all_articles": 2,
        # Плитка «Почищено» = статьи с pending_deletion. В фикстуре таких нет, поэтому 0.
        "cleaned_articles": 0,
        "selected_for_digest": 1,
        "avg_score": 70,
        "sources": 4,
        # Пер-статусные счётчики по всей базе: «Old article» без строки состояния читается
        # как 'new', «Digest candidate» выбран аналитиком в дайджест.
        "status_counts": {
            "new": 1,
            "digest": 1,
            "archive": 0,
            "noise": 0,
            "duplicate": 0,
        },
    }

    health = repository.source_health_report(stale_days=3, limit=10)
    assert [row["verdict"] for row in health] == ["no_articles", "stale", "ok", "disabled"]
    assert health[0]["name"] == "No Articles"
    assert repository.source_health_report(stale_days=3, verdict="stale")[0]["name"] == "Stale Source"

    # digest_candidates тоже пер-юзерная (#12): без user_id выбор в дайджест не виден.
    digest_rows = repository.digest_candidates(month=now.strftime("%Y-%m"), min_score=60, user_id=analyst_id)
    assert len(digest_rows) == 1
    assert digest_rows[0]["id"] == article_ids["Digest candidate"]
    assert digest_rows[0]["tag_name"] == "ГРП"
    assert digest_rows[0]["parent_tag_name"] == "Технологии"

    assert repository.digest_candidates(month=now.strftime("%Y-%m"), min_score=95) == []


def test_source_health_report_separates_archive_and_counts_last_30_days(isolated_db):
    """Архив — свой вердикт, а не «Выкл».

    Архив выключает сбор (enabled = FALSE), и в «Выкл» он тонул: экран считал плитки
    по отчёту с архивом, а список — без него, и 19.09 заказчик видел «133 источника» и
    «173» на одном экране. «Материалов за 30 дней» — по дате сбора: колонка таблицы
    источников отвечает на вопрос «что источник принёс за месяц».
    """
    now = datetime.now(timezone.utc)
    with connection.get_connection() as conn:
        rows = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category, archived_at)
            VALUES
              ('Working', 'News', 'https://example.com/w', TRUE, 'rss', 'международные', NULL),
              ('Switched off', 'News', 'https://example.com/off', FALSE, 'rss', 'международные', NULL),
              ('Archived', 'News', 'https://example.com/arch', FALSE, 'rss', 'международные', now())
            RETURNING id, name
            """
        ).fetchall()
        ids = {name: source_id for source_id, name in rows}
        conn.execute(
            """
            INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language, content_hash)
            VALUES
              (%s, 'Fresh', 'https://example.com/w/1', %s, %s, 'Text one', 'en', 'health-1'),
              (%s, 'Month old', 'https://example.com/w/2', %s, %s, 'Text two', 'en', 'health-2'),
              (%s, 'Archived fresh', 'https://example.com/arch/1', %s, %s, 'Text three', 'en', 'health-3')
            """,
            (
                ids["Working"], now - timedelta(days=1), now - timedelta(days=1),
                ids["Working"], now - timedelta(days=40), now - timedelta(days=40),
                ids["Archived"], now - timedelta(days=2), now - timedelta(days=2),
            ),
        )
        conn.commit()

    report = repository.source_health_report(stale_days=3, limit=10)
    by_name = {row["name"]: row for row in report}
    assert by_name["Working"]["verdict"] == "ok"
    assert by_name["Switched off"]["verdict"] == "disabled"
    assert by_name["Archived"]["verdict"] == "archived"
    assert (by_name["Working"]["articles"], by_name["Working"]["articles_30d"]) == (2, 1)
    assert by_name["Archived"]["articles_30d"] == 1
    assert by_name["Archived"]["archived_at"] is not None
    # Архив — в конце выдачи, после выключенных, и фильтруется своим вердиктом.
    assert [row["verdict"] for row in report] == ["ok", "disabled", "archived"]
    assert [row["name"] for row in repository.source_health_report(verdict="archived")] == ["Archived"]
    assert repository.source_health_report(verdict="disabled")[0]["name"] == "Switched off"


def test_dashboard_all_articles_counts_whole_base_not_just_signals(isolated_db):
    """Плитка «Всего» (all_articles) считает ВЕСЬ объём базы, а total_articles — только сигналы.

    Решение владельца 25.07: первая плитка показывает всё собранное, включая отсев по
    релевантности и вычищенное перепроверкой (pending_deletion). Разделение полей нужно,
    чтобы это не задело total_articles («сигналы»), на котором держатся соседние плитки.
    """
    now = datetime.now(timezone.utc)
    with connection.get_connection() as conn:
        source_id = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category)
            VALUES ('S', 'News', 'https://example.com/s', TRUE, 'request', 'международные')
            RETURNING id
            """
        ).fetchone()[0]
        article_ids = {}
        for key, url, chash in [
            ("signal", "https://example.com/a1", "h1"),
            ("rejected", "https://example.com/a2", "h2"),
            ("cleaned", "https://example.com/a3", "h3"),
        ]:
            article_ids[key] = conn.execute(
                """
                INSERT INTO articles (source_id, title, url, published_at, collected_at,
                                      raw_text, language, content_hash, pending_deletion)
                VALUES (%s, %s, %s, %s, %s, 'text', 'ru', %s, %s)
                RETURNING id
                """,
                (source_id, key, url, now, now, chash, key == "cleaned"),
            ).fetchone()[0]
        # signal — прошёл гейт (relevant TRUE); rejected — отсеян (relevant FALSE);
        # cleaned — relevant, но помечен на вычистку (pending_deletion выше).
        conn.execute(
            """
            INSERT INTO article_cards (article_id, summary, relevant, status)
            VALUES (%s, 'sig', TRUE, 'new'), (%s, 'rej', FALSE, 'new'), (%s, 'cln', TRUE, 'new')
            """,
            (article_ids["signal"], article_ids["rejected"], article_ids["cleaned"]),
        )
        conn.commit()

    stats = repository.dashboard_stats(user_id=None)
    assert stats["all_articles"] == 3      # все три статьи базы
    assert stats["total_articles"] == 1    # только один видимый сигнал
    assert stats["cleaned_articles"] == 1  # одна вычищена


def test_insert_article_is_idempotent_by_url_against_real_db(isolated_db):
    with connection.get_connection() as conn:
        source_id = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy)
            VALUES ('Dedup Source', 'News', 'https://example.com', TRUE, 'request')
            RETURNING id
            """
        ).fetchone()[0]
        conn.commit()

    rec = {
        "source_id": source_id,
        "title": "First title",
        "url": "https://example.com/same-url",
        "published_at": datetime.now(timezone.utc),
        "raw_text": "Original article body",
        "text_truncated": False,
        "language": "en",
        "content_hash": "same-hash",
        "image_url": "https://example.com/image.jpg",
    }

    assert repository.insert_article(rec) is True
    assert repository.insert_article({**rec, "title": "Changed title"}) is False
    assert repository.article_exists("https://example.com/same-url") is True

    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*), MIN(title) FROM articles WHERE url = %s",
            ("https://example.com/same-url",),
        ).fetchone()

    assert row == (1, "First title")


def test_repository_cleanup_removes_only_expired_and_old_terminal_records(isolated_db):
    now = datetime.now(timezone.utc)

    with connection.get_connection() as conn:
        user_id = conn.execute(
            """
            INSERT INTO users (email, password_salt, password_hash)
            VALUES ('cleanup@example.com', 'salt', 'hash')
            RETURNING id
            """
        ).fetchone()[0]

        conn.execute(
            """
            INSERT INTO user_sessions (user_id, session_token, expires_at, created_at, last_seen_at)
            VALUES
              (%s, 'expired-session', %s, %s, %s),
              (%s, 'active-session', %s, %s, %s)
            """,
            (
                user_id,
                now - timedelta(days=1),
                now - timedelta(days=2),
                now - timedelta(days=1),
                user_id,
                now + timedelta(days=10),
                now,
                now,
            ),
        )

        conn.execute(
            """
            INSERT INTO background_jobs
              (kind, status, progress, payload_json, finished_at, created_at)
            VALUES
              ('old_ok', 'ok', 100, '{}'::jsonb, %s, %s),
              ('fresh_ok', 'ok', 100, '{}'::jsonb, %s, %s),
              ('running_job', 'running', 20, '{}'::jsonb, NULL, %s)
            """,
            (
                now - timedelta(days=40),
                now - timedelta(days=40),
                now - timedelta(days=5),
                now - timedelta(days=5),
                now - timedelta(days=40),
            ),
        )

        conn.execute(
            """
            INSERT INTO export_jobs (export_type, format, status, started_at, finished_at)
            VALUES
              ('monthly_digest', 'pdf', 'failed', %s, %s),
              ('monthly_digest', 'html', 'ok', %s, %s)
            """,
            (
                now - timedelta(days=50),
                now - timedelta(days=50),
                now - timedelta(days=3),
                now - timedelta(days=3),
            ),
        )
        conn.commit()

    assert repository.delete_expired_user_sessions() == 1
    assert repository.cleanup_finished_background_jobs(retention_days=30) == 1
    assert repository.cleanup_finished_export_jobs(retention_days=30) == 1

    with connection.get_connection() as conn:
        session_tokens = conn.execute("SELECT session_token FROM user_sessions ORDER BY session_token").fetchall()
        background_statuses = conn.execute(
            "SELECT kind, status FROM background_jobs ORDER BY kind"
        ).fetchall()
        export_statuses = conn.execute(
            "SELECT format, status FROM export_jobs ORDER BY format"
        ).fetchall()

    assert session_tokens == [("active-session",)]
    assert background_statuses == [("fresh_ok", "ok"), ("running_job", "running")]
    assert export_statuses == [("html", "ok")]


def test_needing_summary_skips_rejected_articles(isolated_db):
    """Отклонённые гейтом статьи НЕ возвращаются в AI-обработку.

    На проде (внешний путь) гейт релевантности идёт ПЕРВЫМ, и отклонённой статье суть
    НЕ пишется (external_ai.process_payload: `if not relevant: continue`). Раз выборка
    в обработку шла по `WHERE c.summary IS NULL` без фильтра по relevant, такая статья
    возвращалась в очередь КАЖДЫЙ цикл — навсегда.

    Последствия (баг «ИИ не работает» + утечка денег из аудита 09.07):
    - гейт режет ~78%, значит бюджет цикла (AI_PROCESS_LIMIT) съедали ПОВТОРНЫЕ отказы
      одних и тех же статей, а свежие вытеснялись из топа (ORDER BY published_at DESC);
    - каждый цикл заново жёг дорогой гейт (gpt-5.5) на уже отклонённых статьях.
    """
    now = datetime.now(timezone.utc)

    with connection.get_connection() as conn:
        source_id = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category)
            VALUES ('World Oil', 'News', 'https://example.com', TRUE, 'request', 'международные')
            RETURNING id
            """
        ).fetchone()[0]

        def add_article(slug: str) -> int:
            return conn.execute(
                """
                INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language)
                VALUES (%s, %s, %s, %s, %s, 'text', 'en')
                RETURNING id
                """,
                (source_id, f"Article {slug}", f"https://example.com/{slug}", now, now),
            ).fetchone()[0]

        fresh_id = add_article("fresh")           # карточки нет вовсе — ИИ её ещё не видел
        rejected_id = add_article("rejected")     # гейт отклонил: relevant=false, сути нет
        done_id = add_article("done")             # суть уже есть
        retry_id = add_article("retry")           # релевантна, но суть не записалась (сбой)

        conn.execute(
            "INSERT INTO article_cards (article_id, summary, relevant) VALUES (%s, NULL, FALSE)",
            (rejected_id,),
        )
        conn.execute(
            "INSERT INTO article_cards (article_id, summary, relevant) VALUES (%s, 'есть суть', TRUE)",
            (done_id,),
        )
        conn.execute(
            "INSERT INTO article_cards (article_id, summary, relevant) VALUES (%s, NULL, TRUE)",
            (retry_id,),
        )
        conn.commit()

    ids = [row["id"] for row in repository.get_articles_needing_summary(limit=50)]

    # Никогда не обработанная — берём.
    assert fresh_id in ids
    # Релевантная, но без сути (сбой записи) — берём, это честный повтор.
    assert retry_id in ids
    # Уже с сутью — не трогаем.
    assert done_id not in ids
    # ГЛАВНОЕ: отклонённую гейтом НЕ переспрашиваем — иначе она жрёт бюджет цикла вечно.
    assert rejected_id not in ids
