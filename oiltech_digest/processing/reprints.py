"""Перепечатки: одна публикация, разошедшаяся по нескольким изданиям (№21).

Разделение труда между правилом и моделью здесь не вкусовое, а вынужденное —
его продиктовал замер на реальном случае заказчика от 08.09 («все 4 новости об
одном»):

    «Газпром нефть» испытала НОВЫЕ буровые установки…      EnergyLand   2327 зн.
    Газпром нефть ВНЕДРЯЕТ НОВОЕ ПОКОЛЕНИЕ сейсмических…   ROGTEC       2366 зн.
    Газпром нефть испытала РОССИЙСКИЕ буровые установки…   Neftegaz.ru  1026 зн.
    Газпром нефть испытала РОССИЙСКИЕ буровые установки…   Neftegaz.ru  3181 зн.

Совпадение значимых слов заголовка между ЭТИМИ парами — от 36% до 78%, по телу
ещё ниже (23–42%). То есть одного порога, который ловит все четыре и не ловит
лишнего, не существует: настоящие дубли размазаны по всей шкале. Это же показала
июльская проба — лексика провалила кейс владельца (0.11–0.47).

Поэтому:

* ПРАВИЛО отвечает за полноту. Оно дешёвое, SQL, без ИИ, и намеренно взято
  широким (порог 35%): его задача — сузить 30 000 статей до десятков пар в сутки,
  а не решать.
* МОДЕЛЬ отвечает за точность, и ей задаётся УЗКИЙ вопрос — «одно ли это
  событие», а не «похожи ли». Свободная формулировка развалилась бы так же, как
  развалился гейт релевантности, когда его спросили про степень вместо
  принадлежности.

Решение записывается как пометка, а не удаление: заказчик уже видел, как у него
исчезают материалы, и второй раз так делать нельзя.
"""

from __future__ import annotations

import logging
from typing import Any

from oiltech_digest.db import repository
from oiltech_digest.processing.openai_client import AIResponse
from oiltech_digest.processing.prompts import REPRINT_INSTRUCTIONS, REPRINT_SCHEMA

logger = logging.getLogger(__name__)

# Порог взят не из общих соображений, а по нижней границе настоящих дублей в
# случае заказчика (36%) с запасом вниз. Выше — теряем перепечатки, которые он
# видит глазами; ниже — растёт счёт модели, но не ошибка: решает всё равно она.
DEFAULT_MIN_OVERLAP = 0.35
DEFAULT_MAX_DAYS = 5
BODY_LIMIT = 2500


def find_candidates(days: int = 14, min_overlap: float = DEFAULT_MIN_OVERLAP,
                    max_days_apart: int = DEFAULT_MAX_DAYS, limit: int = 200,
                    max_overlap: float = 1.0) -> list[dict]:
    """Пары-кандидаты: разные источники, близкие даты, пересечение значимых слов.

    Разные источники — обязательное условие, а не настройка. Внутри одного
    издания повтор заголовка это серийная сводка («Топ-10 новостей дня»), и
    схлопывание таких пар уже уничтожало сотни статей при попытке дедупа по
    заголовку в июле.
    """
    return repository.reprint_candidates(
        days=days, min_overlap=min_overlap, max_overlap=max_overlap,
        max_days_apart=max_days_apart, limit=limit,
    )


def judge_pair(left: dict, right: dict, client) -> AIResponse:
    """Спросить модель, одно ли это событие. Тела режем: решение принимается по
    факту, а не по объёму, и длинный хвост только удорожает вызов."""
    return client.complete_json(
        REPRINT_INSTRUCTIONS,
        _pair_prompt(left, right),
        REPRINT_SCHEMA,
        max_output_tokens=700,
    )


def review_candidates(candidates: list[dict], client, *, dry_run: bool = True) -> dict[str, Any]:
    """Прогнать кандидатов через судью и, если не сухой прогон, записать решения."""
    stats = {"checked": 0, "reprints": 0, "distinct": 0, "errors": 0, "skipped": 0}
    decisions: list[dict[str, Any]] = []
    for pair in candidates:
        left = repository.get_article(int(pair["a_id"]))
        right = repository.get_article(int(pair["b_id"]))
        if left is None or right is None:
            continue
        stats["checked"] += 1
        try:
            response = judge_pair(left, right, client)
        except Exception as exc:  # noqa: BLE001 - одна пара не валит прогон
            logger.warning("reprint_judge_failed a=%s b=%s: %s", pair["a_id"], pair["b_id"], exc)
            stats["errors"] += 1
            continue

        same = bool(response.data.get("same_event"))
        reason = str(response.data.get("reason") or "")[:500]
        if not same:
            stats["distinct"] += 1
            decisions.append({**pair, "same_event": False, "reason": reason})
            continue

        primary_id = _resolve_primary(response.data.get("primary_id"), left, right)
        duplicate_id = int(right["id"]) if primary_id == int(left["id"]) else int(left["id"])
        stats["reprints"] += 1
        decisions.append({
            **pair, "same_event": True, "primary_id": primary_id,
            "duplicate_id": duplicate_id, "reason": reason,
        })
        if not dry_run:
            try:
                repository.mark_article_reprint(
                    article_id=duplicate_id, primary_id=primary_id,
                    similarity=pair.get("overlap"), reason=reason,
                    decided_by="ai", model=response.model,
                )
            except ValueError as exc:
                # Инварианты пометки отбивают одну пару, а не весь прогон.
                logger.warning("reprint_mark_skipped a=%s b=%s: %s",
                               pair["a_id"], pair["b_id"], exc)
                stats["skipped"] += 1
                stats["reprints"] -= 1
    return {"stats": stats, "decisions": decisions, "dry_run": dry_run}


def resolve_primary(raw: Any, a_id: int, a_len: int, b_id: int, b_len: int) -> int:
    """Главная копия — та, что полнее как самостоятельный материал.

    ЕДИНСТВЕННОЕ место этого правила: раньше оно жило двумя копиями — здесь и в
    external_ai.apply_reprint_review_result, — и молча разъехалось бы при первой
    же правке.

    Ответ модели принимаем, только если это один из двух наших id: с посторонним
    числом мы пометили бы дублем не ту статью. Иначе решаем длиной — в случае
    заказчика копии были 1026, 2327, 2366 и 3181 знак, и короткая оказалась
    обрывком с дефектом склейки заголовка.
    """
    try:
        candidate = int(raw)
    except (TypeError, ValueError):
        candidate = 0
    if candidate in (int(a_id), int(b_id)):
        return candidate
    return int(a_id) if int(a_len) >= int(b_len) else int(b_id)


def _resolve_primary(raw: Any, left: dict, right: dict) -> int:
    return resolve_primary(
        raw,
        int(left["id"]), len(left.get("raw_text") or ""),
        int(right["id"]), len(right.get("raw_text") or ""),
    )


def _pair_prompt(left: dict, right: dict) -> str:
    return "\n".join([
        _one(left, "A"),
        "",
        _one(right, "B"),
    ])


def _one(article: dict, label: str) -> str:
    text = (article.get("raw_text") or "")[:BODY_LIMIT]
    return "\n".join([
        f"[{label}] id: {article.get('id')}",
        f"источник: {article.get('source_name') or ''}",
        f"дата: {article.get('published_at') or ''}",
        f"заголовок: {article.get('title') or ''}",
        f"текст: {text}",
    ])
