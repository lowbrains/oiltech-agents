"""Слова из двух алфавитов в сохранённых карточках: починка без ИИ и перегенерация.

Двойники и склейку (normalize_scripts) можно чинить по всему корпусу: меняется только
алфавит буквы и пробел на стыке. Остальные правила словаря так не гоняются — они
заменяют слова и в старых карточках без ревью ломали бы падеж (ревью 25.09). Полуперевод
(«управляego», «наshore») лечит только новый ответ модели — отсюда выборка на
перегенерацию сути.
"""

from __future__ import annotations

from typing import Any

from oiltech_digest import network_policy
# Модулем, а не функцией: тестовая фикстура подменяет connection.get_connection.
from oiltech_digest.db import connection, repository
from oiltech_digest.processing.domain_glossary import mixed_script_words, normalize_scripts

# Что перегенерирует задача process_articles с пометкой only (external_ai.stages_to_write).
RESUMMARIZE_STAGES = ["summary", "translation"]


def _cards(conn, article_ids: list[int] | None) -> list[tuple[int, str | None, str | None, str | None]]:
    query = (
        "SELECT c.article_id, c.title_ru, c.summary, a.title FROM article_cards c "
        "JOIN articles a ON a.id = c.article_id "
        "WHERE (COALESCE(c.summary, '') <> '' OR COALESCE(c.title_ru, '') <> '')"
    )
    params: list[Any] = []
    if article_ids:
        query += " AND c.article_id = ANY(%s)"
        params.append(list(article_ids))
    return conn.execute(query + " ORDER BY c.article_id", params).fetchall()


def repair_cards(*, apply: bool = False, article_ids: list[int] | None = None) -> dict[str, Any]:
    """normalize_scripts по title_ru и summary. Запись — только если поле не менялось с чтения."""
    changes = []
    with connection.get_connection() as conn:
        for article_id, title_ru, summary, _title in _cards(conn, article_ids):
            for field, before in (("title_ru", title_ru), ("summary", summary)):
                after = normalize_scripts(before or "")
                if before and after != before:
                    changes.append({"article_id": int(article_id), "field": field, "before": before, "after": after})
        if apply:
            for change in changes:
                conn.execute(
                    f"UPDATE article_cards SET {change['field']} = %s, updated_at = now() "
                    f"WHERE article_id = %s AND {change['field']} = %s",
                    (change["after"], change["article_id"], change["before"]),
                )
            conn.commit()
    return {"changed_fields": len(changes), "applied": apply, "changes": changes}


def resummarize_selection(article_ids: list[int] | None = None) -> dict[str, list[int]]:
    """Статьи, где слово из двух алфавитов останется и после normalize_scripts (или явный список).

    Брак в сути — перегенерация сути (и перевода заголовка вместе с ней); только в
    переведённом заголовке — перевод заголовка. Русский заголовок — копия исходника
    (перевод не нужен), брак в нём самом переводом не лечится: `source_title`, в задачи
    не идёт.
    """
    selection: dict[str, list[int]] = {"summary": [], "title": [], "source_title": []}
    with connection.get_connection() as conn:
        for article_id, title_ru, summary, title in _cards(conn, article_ids):
            # Явный список — «перегенерировать эти»: суть — без проверки алфавита (так же
            # чинятся, например, 3 карточки со следом старой замены «в шельфовый», 25.09).
            if summary and (article_ids or mixed_script_words(normalize_scripts(summary))):
                selection["summary"].append(int(article_id))
            elif title_ru and mixed_script_words(normalize_scripts(title_ru)):
                copied = normalize_scripts(title_ru) == normalize_scripts((title or "")[:200])
                selection["source_title" if copied else "title"].append(int(article_id))
    return selection


def enqueue_resummarize(summary_ids: list[int], title_ids: list[int], *, batch_size: int = 20) -> list[int]:
    """Задачи внешнего контура: суть — process_articles с пометкой only, заголовок — translate_titles."""
    decision = network_policy.route_ai_bulk()
    if decision.execution_region != "external":
        # Локальный конвейер пометки only не знает, а готовые стадии пропускает: у статьи
        # с сутью он ничего не перегенерирует — задача прошла бы молча впустую.
        raise RuntimeError("перегенерация сути идёт только через внешний контур ИИ")
    batch = max(1, batch_size)
    jobs = []
    for kind, ids in (("process_articles", summary_ids), ("translate_titles", title_ids)):
        for start in range(0, len(ids), batch):
            chunk = ids[start : start + batch]
            payload: dict[str, Any] = {"article_ids": chunk}
            if kind == "process_articles":
                payload.update({"limit": len(chunk), "offline": False, "only": RESUMMARIZE_STAGES})
            job = repository.create_background_job(
                kind,
                payload,
                queue_name=decision.queue_name,
                execution_region=decision.execution_region,
                capability=decision.capability,
            )
            jobs.append(int(job["id"]))
    return jobs
