"""Ключ адреса после 13.09: номер статьи из query — обратно в ключ, спрятанное склейкой — в ленту.

13.09 ключ тождества статьи (`articles.url_key`) стал host+path БЕЗ query, и схема в тот же
день спрятала (pending_deletion) все статьи с совпавшим ключом, кроме одной. У сайтов, где
номер статьи живёт в query, это склеило всё: у РГУ Губкина спрятано 427 статей из 430, у
Минэнерго 90 из 91, у Новатэка 73 из 74, у EIA 50 из 51, у Лукойла 11 из 12 (замер 25.09).

Склейка отличима точно: ручная и гейтовая пометка (`mark_article_for_deletion`) всегда
ставит `marked_for_deletion_at`, схема 13.09 — ни его, ни причины. В ленту возвращаем только
спрятанное схемой и только если по новому ключу статья действительно своя: номер в query
есть, ключ не занят видимой статьёй, тело не повторяет видимую статью того же источника
(рубеж №24 — иначе вернулись бы копии страниц навигации). Статья, которую убрал человек или
гейт, блокирует и своих двойников: иначе она вернулась бы в ленту под другим написанием адреса.

По умолчанию сухой прогон — план без записи. С apply план и запись идут в ОДНОЙ транзакции под
блокировкой таблицы от вставок: иначе вставка сбора между планом и записью роняла бы всё на
уникальном индексе.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from oiltech_digest.db import connection
from oiltech_digest.ingestion import normalize

# Спрятано схемой 13.09, а не человеком или гейтом. То же правило — в UPDATE возврата.
_COLLAPSED_SQL = "pending_deletion AND marked_for_deletion_at IS NULL AND deletion_reason IS NULL"


def _old_rule_key(key: str) -> str:
    """Ключ по правилу 13.09 — без query: так их считала склейка."""
    return key.split("?", 1)[0]


def plan_repair(conn) -> dict:
    """Что пересчитать и что вернуть в ленту. Только чтение."""
    visible = conn.execute(
        "SELECT id, source_id, url, url_key, body_hash FROM articles "
        "WHERE NOT pending_deletion ORDER BY id"
    ).fetchall()
    hidden = conn.execute(
        f"SELECT a.id, a.source_id, a.url, a.url_key, a.body_hash, ({_COLLAPSED_SQL}), s.name "
        "FROM articles a JOIN sources s ON s.id = a.source_id "
        "WHERE a.pending_deletion AND a.url LIKE '%%?%%' ORDER BY a.id"
    ).fetchall()

    key_updates: list[tuple[int, str]] = []
    key_conflicts: list[dict] = []
    held = {key: article_id for article_id, _, _, key, _ in visible if key}
    visible_bodies = {(source_id, body_hash) for _, source_id, _, _, body_hash in visible if body_hash}
    for article_id, _, url, key, _ in visible:
        new_key = normalize.url_key(url)
        if new_key == key:
            continue
        if new_key in held:
            # Другое написание той же статьи уже в ленте под новым ключом — вставилось между
            # выкатом кода и этой починкой. Ключ не отдаём (упало бы на уникальном индексе),
            # показываем владельцу: это видимый дубль.
            key_conflicts.append({"id": article_id, "url": url, "holder_id": held[new_key]})
            continue
        held.pop(key, None)
        held[new_key] = article_id
        key_updates.append((article_id, new_key))

    marked_keys: set[str] = set()
    marked_bodies: set[tuple[int, str]] = set()
    for article_id, source_id, url, key, body_hash, collapsed, _ in hidden:
        if not collapsed:
            marked_keys.add(normalize.url_key(url))
            if body_hash:
                marked_bodies.add((source_id, body_hash))

    unhide: list[int] = []
    kept: Counter = Counter()
    by_source: dict[str, Counter] = defaultdict(Counter)
    for article_id, source_id, url, key, body_hash, collapsed, source_name in hidden:
        new_key = normalize.url_key(url)
        if new_key != key:
            key_updates.append((article_id, new_key))  # скрытые не в уникальном индексе
        if not collapsed:
            verdict = "marked_by_user"
        elif new_key == _old_rule_key(new_key):
            verdict = "tracking_duplicate"  # в query только хвосты — склейка была верной
        elif new_key in held:
            verdict = "key_taken"
        elif body_hash and (source_id, body_hash) in visible_bodies:
            verdict = "same_body"
        elif new_key in marked_keys or (body_hash and (source_id, body_hash) in marked_bodies):
            verdict = "twin_of_marked"
        else:
            verdict = "unhide"
            unhide.append(article_id)
            held[new_key] = article_id
            if body_hash:
                visible_bodies.add((source_id, body_hash))
        if verdict != "unhide":
            kept[verdict] += 1
        by_source[f"{source_id} {source_name}"][verdict] += 1

    return {
        "key_updates": key_updates,
        "key_conflicts": key_conflicts,
        "unhide_ids": unhide,
        "kept_hidden": dict(kept),
        "by_source": {name: dict(counts) for name, counts in sorted(by_source.items())},
    }


def repair_url_keys(apply: bool = False) -> dict:
    """Пересчитать ключи и вернуть спрятанное склейкой. Без apply — только отчёт."""
    with connection.get_connection() as conn:
        if apply:
            # Вставки ждут секунды починки; чтение ленты не блокируется.
            conn.execute("LOCK TABLE articles IN SHARE ROW EXCLUSIVE MODE")
        plan = plan_repair(conn)
        if apply and (plan["key_updates"] or plan["unhide_ids"]):
            with conn.cursor() as cur:
                # Сначала ключи видимых: каждый получает ещё свободный ключ (занятые ушли в
                # key_conflicts), потом возврат — уникальный индекс проверит его по новым ключам.
                cur.executemany("UPDATE articles SET url_key = %s WHERE id = %s",
                                [(key, article_id) for article_id, key in plan["key_updates"]])
            if plan["unhide_ids"]:
                conn.execute(
                    f"UPDATE articles SET pending_deletion = FALSE, updated_at = now() "
                    f"WHERE id = ANY(%s) AND {_COLLAPSED_SQL}",
                    (plan["unhide_ids"],),
                )
            conn.commit()
        else:
            conn.rollback()
    return {
        "apply": apply,
        "key_updates": len(plan["key_updates"]),
        "unhidden": len(plan["unhide_ids"]),
        "kept_hidden": plan["kept_hidden"],
        "key_conflicts": plan["key_conflicts"],
        "by_source": plan["by_source"],
    }
