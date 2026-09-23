"""Выпуск со статьёй без даты публикации (23.09).

С 12.09 (3b2a880) digest_candidates сортирует общий список статей и сигналов через
`datetime.min.replace(tzinfo=timezone.utc)`, а `timezone` в repository.py не был
импортирован. Любая выбранная «в дайджест» статья без даты публикации роняла превью и
выгрузку выпуска NameError'ом — в том числе режим «Все месяцы», который конструктор
открывает первым. В агентном контуре на 23.09 — 8 таких падений в логах приложения,
у двух пользователей есть выбранные статьи без даты публикации.
"""

from __future__ import annotations

from datetime import datetime, timezone

from oiltech_digest.db import connection, repository


def test_issue_with_article_without_publication_date_does_not_crash(isolated_db):
    with connection.get_connection() as conn:
        user_id = conn.execute(
            "INSERT INTO users (email, password_salt, password_hash, role) "
            "VALUES ('u@example.com', 'salt', 'hash', 'user') RETURNING id"
        ).fetchone()[0]
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('Neftegaz.ru', 'Media', 'https://neftegaz.example', TRUE, 'request') RETURNING id"
        ).fetchone()[0]
        ids = []
        for slug, published in (("dated", datetime(2026, 9, 10, 12, tzinfo=timezone.utc)), ("undated", None)):
            article_id = conn.execute(
                "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language) "
                "VALUES (%s, %s, %s, %s, %s, 'Текст.', 'ru') RETURNING id",
                (source_id, f"Статья {slug}", f"https://neftegaz.example/{slug}", published,
                 datetime(2026, 9, 11, 12, tzinfo=timezone.utc)),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO article_cards (article_id, summary, relevant) VALUES (%s, 'Суть', TRUE)",
                (article_id,),
            )
            conn.execute(
                "INSERT INTO user_article_states (user_id, article_id, status) VALUES (%s, %s, 'digest')",
                (user_id, article_id),
            )
            ids.append(article_id)
        conn.commit()

    rows = repository.digest_candidates(month=None, limit=50, min_score=0, user_id=user_id)
    assert {row["id"] for row in rows} == set(ids)
