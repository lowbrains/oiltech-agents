"""Article processing services: summary, tagging, scoring and cost reports."""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any

import logging

from oiltech_digest import config
from oiltech_digest.db import repository
from oiltech_digest.ingestion import article_fetcher
from oiltech_digest.processing.domain_glossary import enforce_glossary_text, glossary_prompt_block
from oiltech_digest.processing.openai_client import AIClientError, AIResponse, OfflineAIClient, OpenAIResponsesClient
from oiltech_digest.processing.prompts import (
    RELEVANCE_INSTRUCTIONS,
    RELEVANCE_SCHEMA,
    SCORING_INSTRUCTIONS,
    SCORE_SCHEMA,
    SUMMARY_INSTRUCTIONS,
    SUMMARY_SCHEMA,
    TAG_SCHEMA,
    TAGGING_INSTRUCTIONS,
    TRANSLATE_INSTRUCTIONS,
    TRANSLATE_SCHEMA,
    tags_scope_block,
)


logger = logging.getLogger(__name__)


def make_client(offline: bool = False):
    return OfflineAIClient() if offline else OpenAIResponsesClient()


def process_summaries(limit: int = 20, offline: bool = False) -> dict:
    client = make_client(offline)
    return process_summary_articles(repository.get_articles_needing_summary(limit), client)


def process_summary_articles(articles: list[dict], client) -> dict:
    stats = {"processed": 0, "errors": 0}
    for article in articles:
        try:
            response = summarize_article(article, client)
            repository.upsert_article_card(article["id"], response.data["summary"], response.model)
            _record_run(article, "summary", client, response)
            stats["processed"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            _record_error(article, "summary", client, exc)
            stats["errors"] += 1
    return stats


def process_translations(limit: int = 20, offline: bool = False) -> dict:
    client = make_client(offline)
    return process_translation_articles(repository.get_articles_needing_title_ru(limit), client)


def process_translation_articles(articles: list[dict], client) -> dict:
    """Отдельная стадия перевода заголовков: проставляет title_ru статьям без него.

    Русские заголовки переводить не нужно — берём их как есть (без AI-вызова). AI
    дёргается ТОЛЬКО для иностранных заголовков. Так бэкфилл по всей базе дешёвый:
    платим лишь за реальный перевод. `ai` в статистике — сколько раз ходили в модель."""
    stats = {"processed": 0, "ai": 0, "errors": 0}
    for article in articles:
        try:
            title_ru, response = title_ru_for_article(article, client)
            if title_ru is None:
                stats["processed"] += 1
                continue
            repository.set_article_title_ru(article["id"], title_ru)
            if response is not None:
                _record_run(article, "translation", client, response)
                stats["ai"] += 1
            stats["processed"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            _record_error(article, "translation", client, exc)
            stats["errors"] += 1
    return stats


def process_relevance(limit: int = 20, offline: bool = False) -> dict:
    client = make_client(offline)
    return process_relevance_articles(repository.get_articles_needing_relevance(limit), client)


def process_relevance_articles(articles: list[dict], client) -> dict:
    """AI-фильтр: помечает статьи как релевантные/нерелевантные нефтесервису.
    Нерелевантные получают status='rejected' и дальше не тегируются/не скорятся.
    Перед AI-вызовом отсекаем статьи со стоп-словами родительских тегов (бэклог #6)."""
    tags = repository.list_enabled_tags()
    stats = {"processed": 0, "relevant": 0, "rejected": 0, "errors": 0}
    for article in articles:
        try:
            blocked_reason = _negative_keyword_block(article, tags)
            if blocked_reason:
                repository.set_article_relevance(article["id"], False, blocked_reason, "negative-keyword")
                stats["processed"] += 1
                stats["rejected"] += 1
                continue
            response = relevance_article(article, client, tags=tags)
            relevant = bool(response.data.get("relevant"))
            repository.set_article_relevance(
                article["id"], relevant, response.data.get("reason"), response.model
            )
            _record_run(article, "relevance", client, response)
            stats["processed"] += 1
            stats["relevant" if relevant else "rejected"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            _record_error(article, "relevance", client, exc)
            stats["errors"] += 1
    return stats


def process_recheck(limit: int = 100, offline: bool = False, force: bool = False,
                    max_articles: int | None = None) -> dict:
    """Локальный перепрогон релевантности по ВСЕЙ базе (для тестов/дампа; на проде
    OpenAI доступен только через воркер — там используется enqueue-recheck).
    Идёт по возрастанию id с чекпоинтом; нерелевантные удаляются физически."""
    client = make_client(offline)
    totals = {"checked": 0, "kept": 0, "deleted": 0, "skipped_in_digest": 0, "errors": 0}
    after_id = 0
    while True:
        batch = repository.get_articles_for_recheck(after_id, limit)
        if not batch:
            break
        after_id = max(int(article["id"]) for article in batch)
        stats = recheck_relevance_articles(batch, client, force=force)
        for key in totals:
            totals[key] += stats[key]
        if max_articles is not None and totals["checked"] >= max_articles:
            break
    return totals


def recheck_relevance_articles(articles: list[dict], client, *, force: bool = False) -> dict:
    """Прогнать гейт релевантности по сырому тексту; релевантные — персист,
    нерелевантные — УДАЛИТЬ физически (delete_article защищает сохранённые дайджесты)."""
    tags = repository.list_enabled_tags()
    stats = {"checked": 0, "kept": 0, "deleted": 0, "skipped_in_digest": 0, "errors": 0}
    for article in articles:
        stats["checked"] += 1
        try:
            blocked_reason = _negative_keyword_block(article, tags)
            if blocked_reason:
                relevant, reason, model = False, blocked_reason, "negative-keyword"
            else:
                resp = relevance_article(article, client, tags=tags)
                relevant = bool(resp.data.get("relevant"))
                reason, model = resp.data.get("reason"), resp.model
                _record_run(article, "relevance", client, resp)
            if relevant:
                repository.set_article_relevance(article["id"], True, reason, model)
                stats["kept"] += 1
            else:
                deleted = repository.delete_article(int(article["id"]), force=force)
                stats["deleted" if deleted else "skipped_in_digest"] += 1
        except Exception as exc:  # noqa: BLE001 - одна плохая статья не валит батч
            _record_error(article, "relevance", client, exc)
            stats["errors"] += 1
    return stats


def process_tags(limit: int = 20, offline: bool = False) -> dict:
    client = make_client(offline)
    return process_tag_articles(repository.get_articles_needing_tags(limit), client)


def process_tag_articles(articles: list[dict], client) -> dict:
    tags = repository.list_enabled_tags()
    stats = {"processed": 0, "errors": 0}
    if not tags:
        raise ValueError("Нет активных тегов. Запустите seed-tags.")
    for article in articles:
        try:
            response = tag_article(article, tags, client)
            tag_id = _valid_tag_id(response.data.get("tag_id"), tags)
            if tag_id == 0:
                tag_id = keyword_tag(article, tags)["tag_id"]
            repository.upsert_article_tag(
                article["id"],
                tag_id,
                _clamp(float(response.data.get("confidence") or 0), 0, 1),
                response.data.get("rationale"),
                response.model,
            )
            _record_run(article, "tagging", client, response)
            stats["processed"] += 1
        except Exception as exc:  # noqa: BLE001
            _record_error(article, "tagging", client, exc)
            stats["errors"] += 1
    return stats


def process_scores(limit: int = 20, offline: bool = False) -> dict:
    client = make_client(offline)
    return process_score_articles(repository.get_articles_needing_scores(limit), client)


def process_score_articles(articles: list[dict], client) -> dict:
    criteria = repository.list_enabled_scoring_criteria()
    _validate_weights(criteria)
    stats = {"processed": 0, "errors": 0}
    for article in articles:
        try:
            response = score_article(article, criteria, client)
            payload = normalize_score_payload(article, criteria, response.data)
            repository.replace_article_score(
                article["id"],
                payload["total_score"],
                payload["score_label"],
                payload["explanation"],
                payload["items"],
                response.model,
            )
            _record_run(article, "scoring", client, response)
            stats["processed"] += 1
        except Exception as exc:  # noqa: BLE001
            _record_error(article, "scoring", client, exc)
            stats["errors"] += 1
    return stats


def process_full(limit: int = 20, offline: bool = False) -> dict:
    """Запустить по-статейный конвейер на статьях с незавершённым AI pipeline."""
    client = make_client(offline)
    return process_pipeline_articles(repository.get_articles_needing_pipeline(limit), client)


def process_pipeline_articles(articles: list[dict], client, fetch_full: bool = True) -> dict:
    """Полный конвейер по одной статье целиком: full-text → релевантность → суть → тег → скоринг.

    Каждая статья проходит все этапы до конца, прежде чем берётся следующая, —
    готовые карточки появляются по мере обработки, не нужно ждать прогона всего
    батча на каждом этапе. Нерелевантные дальше не тегируются и не скорятся.
    """
    tags = repository.list_enabled_tags()
    if not tags:
        raise ValueError("Нет активных тегов. Запустите seed-tags.")
    criteria = repository.list_enabled_scoring_criteria()
    _validate_weights(criteria)
    stats = {"processed": 0, "fulltext": 0, "summary": 0, "relevant": 0,
             "rejected": 0, "tagged": 0, "translated": 0, "scored": 0, "errors": 0}
    for article in articles:
        stats["processed"] += 1
        try:
            # 1. Полный текст из HTML, если RSS отдал только сниппет.
            if fetch_full and article.get("text_truncated") and article.get("url"):
                result = article_fetcher.fetch_article_text(article)
                if result.status == "ok":
                    repository.update_article_full_text(
                        int(article["id"]), raw_text=result.text, text_truncated=False,
                        status="ok", method=result.method, error=None,
                    )
                    article["raw_text"] = result.text
                    article["text_truncated"] = False
                    stats["fulltext"] += 1

            # 2. Релевантность ПЕРВОЙ — на сыром тексте, до сути (без bias и без лишних
            #    AI-вызовов на нерелевантном).
            blocked_reason = _negative_keyword_block(article, tags)
            if blocked_reason:
                repository.set_article_relevance(article["id"], False, blocked_reason, "negative-keyword")
                stats["rejected"] += 1
                continue

            if article.get("relevant") is True:
                relevant = True
            elif article.get("relevant") is False:
                relevant = False
            else:
                rel_resp = relevance_article(article, client, tags=tags)
                relevant = bool(rel_resp.data.get("relevant"))
                repository.set_article_relevance(article["id"], relevant, rel_resp.data.get("reason"), rel_resp.model)
                _record_run(article, "relevance", client, rel_resp)
            stats["relevant" if relevant else "rejected"] += 1
            if not relevant:
                continue

            # 3. Суть.
            if not article.get("summary"):
                summary_resp = summarize_article(article, client)
                repository.upsert_article_card(article["id"], summary_resp.data["summary"], summary_resp.model)
                _record_run(article, "summary", client, summary_resp)
                article["summary"] = summary_resp.data["summary"]
                stats["summary"] += 1

            # 3b. Перевод заголовка — отдельная стадия (AI только для иностранных).
            if not article.get("title_ru"):
                title_ru, translate_resp = title_ru_for_article(article, client)
                if title_ru is not None:
                    repository.set_article_title_ru(article["id"], title_ru)
                    if translate_resp is not None:
                        _record_run(article, "translation", client, translate_resp)
                        stats["translated"] += 1

            # 4. Тег.
            if article.get("existing_tag_id") is None:
                tag_resp = tag_article(article, tags, client)
                tag_id = _valid_tag_id(tag_resp.data.get("tag_id"), tags)
                if tag_id == 0:
                    tag_id = keyword_tag(article, tags)["tag_id"]
                repository.upsert_article_tag(
                    article["id"], tag_id,
                    _clamp(float(tag_resp.data.get("confidence") or 0), 0, 1),
                    tag_resp.data.get("rationale"), tag_resp.model,
                )
                _record_run(article, "tagging", client, tag_resp)
                stats["tagged"] += 1

            # 5. Скоринг.
            if article.get("existing_score_id") is None:
                score_resp = score_article(article, criteria, client)
                payload = normalize_score_payload(article, criteria, score_resp.data)
                repository.replace_article_score(
                    article["id"], payload["total_score"], payload["score_label"],
                    payload["explanation"], payload["items"], score_resp.model,
                )
                _record_run(article, "scoring", client, score_resp)
                stats["scored"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            _record_error(article, "pipeline", client, exc)
            stats["errors"] += 1
    return stats


def summarize_article(article: dict, client) -> AIResponse:
    response = client.complete_json(
        SUMMARY_INSTRUCTIONS,
        _article_prompt(article),
        SUMMARY_SCHEMA,
        max_output_tokens=1200,
    )
    return AIResponse(
        data={**response.data, "summary": enforce_glossary_text(str(response.data.get("summary") or ""), article)},
        model=response.model,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )


def relevance_article(article: dict, client, tags: list[dict] | None = None) -> AIResponse:
    # Гейт судит по СЫРОМУ тексту (title+source+text), БЕЗ AI-сути: суммаризатор
    # обязан притягивать любую статью к нефтегазу, и подача его сути на вход гейта
    # давала самосбывающуюся релевантность (мусор проходил). Модель/effort — отдельные,
    # обычно сильнее основных: вызов дешёвый, цена ошибки высокая.
    try:
        return client.complete_json(
            RELEVANCE_INSTRUCTIONS,
            _relevance_prompt(article, tags=tags),
            RELEVANCE_SCHEMA,
            max_output_tokens=2500,
            model=config.OPENAI_RELEVANCE_MODEL,
            reasoning_effort=config.OPENAI_RELEVANCE_REASONING,
        )
    except AIClientError as exc:
        if "max_output_tokens" not in str(exc):
            raise
        return client.complete_json(
            RELEVANCE_INSTRUCTIONS,
            _relevance_prompt(article, tags=tags, text_limit=1500),
            RELEVANCE_SCHEMA,
            max_output_tokens=2500,
            model=config.OPENAI_RELEVANCE_MODEL,
            reasoning_effort="minimal",
        )


def translate_article(article: dict, client) -> AIResponse:
    """AI-перевод заголовка на русский. Отдельная стадия (раньше был частью summary).
    Модель/effort — собственные (обычно дешёвые: ответ короткий), фолбэк на основные."""
    response = client.complete_json(
        TRANSLATE_INSTRUCTIONS,
        _title_prompt(article),
        TRANSLATE_SCHEMA,
        max_output_tokens=300,
        model=config.OPENAI_TRANSLATE_MODEL,
        reasoning_effort=config.OPENAI_TRANSLATE_REASONING,
    )
    return AIResponse(
        data={**response.data, "title_ru": enforce_glossary_text(str(response.data.get("title_ru") or ""), article)},
        model=response.model,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )


def title_ru_for_article(article: dict, client) -> tuple[str | None, AIResponse | None]:
    """Вернуть (title_ru, ai_response). Русский заголовок не переводим (ai_response=None);
    иностранный — переводим через AI. Пустой заголовок → (None, None)."""
    title = (article.get("title") or "").strip()
    if not title:
        return None, None
    if not _needs_translation(title):
        return title[:200], None
    response = translate_article(article, client)
    translated = (response.data.get("title_ru") or title).strip()
    return (translated or title)[:200], response


def _needs_translation(title: str) -> bool:
    """Заголовок считаем требующим перевода, если кириллицы в нём меньше половины букв."""
    letters = [ch for ch in title if ch.isalpha()]
    if not letters:
        return False
    cyrillic = sum(1 for ch in letters if "Ѐ" <= ch <= "ӿ")
    return cyrillic / len(letters) < 0.5


def tag_article(article: dict, tags: list[dict], client) -> AIResponse:
    tag_lines = []
    for tag in tags:
        path = f"{tag.get('parent_name')} / {tag['name']}" if tag.get("parent_name") else tag["name"]
        tag_lines.append(
            f"{tag['id']}: {path} | EN: {tag.get('name_en') or ''} | "
            f"keywords: {', '.join((tag.get('keywords_en_json') or [])[:12])}"
        )
    return client.complete_json(
        TAGGING_INSTRUCTIONS,
        _article_prompt(article) + "\n\navailable_tags:\n" + "\n".join(tag_lines),
        TAG_SCHEMA,
        max_output_tokens=1000,
    )


def score_article(article: dict, criteria: list[dict], client) -> AIResponse:
    criterion_lines = []
    for criterion in criteria:
        criterion_lines.append(
            f"{criterion['id']}: {criterion['name']} | weight={criterion['weight']} | "
            f"description={criterion.get('description') or ''} | "
            f"keywords={', '.join((criterion.get('keywords_en_json') or [])[:12])}"
        )
    return client.complete_json(
        SCORING_INSTRUCTIONS,
        _article_prompt(article) + "\n\ncriteria:\n" + "\n".join(criterion_lines),
        SCORE_SCHEMA,
        max_output_tokens=1800,
        model=config.OPENAI_SCORE_MODEL,
        reasoning_effort=config.OPENAI_SCORE_REASONING,
    )


# Куда падает статья, не совпавшая НИ С ОДНИМ ключевым словом. Раньше — в tags[0],
# то есть в первый тег по порядку: при таксономии D01–D18 это была «Сейсморазведка»,
# при 13 тематиках заказчика стала бы «Геологоразведка». Мусор копился в одном
# направлении и выглядел как обычная классификация.
# Заказчик 13.09 сам предложил решение: «если статья не совпадает ни с одной тематикой,
# она должна попадать в Unclassified / потенциально новая тема, чтобы Discovery Agent
# не был ограничен текущей taxonomy».
# Имя одно на всю систему: repository защищает этот тег от выключения, сид его создаёт,
# классификация на него падает. Три копии строки разъехались бы молча.
UNCLASSIFIED_TAG_NAME = repository.SYSTEM_TAG_UNCLASSIFIED


def _fallback_tag(tags: list[dict]) -> dict:
    """Тег-приёмник, если он заведён; иначе — прежнее поведение (первый по порядку)."""
    for tag in tags:
        if tag.get("name") == UNCLASSIFIED_TAG_NAME:
            return tag
    return tags[0]


def keyword_tag(article: dict, tags: list[dict]) -> dict:
    text = _search_text(article)
    best = {"tag_id": _fallback_tag(tags)["id"], "confidence": 0.15, "matches": 0}
    for tag in tags:
        keywords = (tag.get("keywords_json") or []) + (tag.get("keywords_en_json") or [])
        matches = sum(1 for keyword in keywords if _contains_keyword(text, keyword))
        if matches > best["matches"]:
            best = {"tag_id": tag["id"], "confidence": min(0.9, 0.35 + matches * 0.08), "matches": matches}
    return best


# Блендинг балла критерия. Оценка модели (ai_score) — ОСНОВНОЙ сигнал и НИЖНЯЯ граница:
# модель читает суть статьи. Ключевые слова (keyword_score) могут только ПОДНЯТЬ балл, но
# никогда не топят — узкие списки ключей (нужно 3+ точных совпадения из ~10 на критерий)
# почти всегда дают низкий keyword_score, и в прежней схеме (0.35·kw + 0.65·ai) он тянул
# итог вниз: статья с ai=80 давала total ~57 (< порога 65), а 70+ требовал ai≈100 — поэтому
# до 65+ дотягивали единицы (инцидент 2026-06). Теперь:
#   blended = SCORE_KEYWORD_WEIGHT·kw + SCORE_AI_WEIGHT·ai
#   final   = max(ai, blended)   ← keyword добавляет вес только когда совпадений много (kw>ai)
# Веса вынесены в константы — подстраивай после пилота на проде.
SCORE_KEYWORD_WEIGHT = 0.2
SCORE_AI_WEIGHT = 0.8


def normalize_score_payload(article: dict, criteria: list[dict], payload: dict[str, Any]) -> dict:
    by_id = {int(c["id"]): c for c in criteria}
    ai_items = {int(item["criterion_id"]): item for item in payload.get("items", []) if item.get("criterion_id") in by_id}
    items = []
    weighted_total = 0.0
    for criterion in criteria:
        criterion_id = int(criterion["id"])
        weight = float(criterion["weight"])
        keyword_score = keyword_score_for_criterion(article, criterion)
        ai_item = ai_items.get(criterion_id)
        # Явная проверка наличия: легитимный ai_score=0 — это оценка, а не «нет ответа».
        # Только если модель вообще не вернула критерий — падаем на детерминистский keyword.
        if ai_item is not None and ai_item.get("ai_score") is not None:
            ai_score = _clamp(float(ai_item["ai_score"]), 0, 100)
        else:
            ai_score = keyword_score
        blended = (keyword_score * SCORE_KEYWORD_WEIGHT) + (ai_score * SCORE_AI_WEIGHT)
        final_score = round(max(ai_score, blended), 2)
        weighted_total += final_score * weight / 100
        items.append(
            {
                "criterion_id": criterion_id,
                "keyword_score": keyword_score,
                "ai_score": ai_score,
                "final_score": final_score,
                "rationale": (ai_items.get(criterion_id) or {}).get("rationale") or "Keyword/AI blended score",
            }
        )
    total_score = round(_clamp(weighted_total, 0, 100), 2)
    return {
        "total_score": total_score,
        "score_label": score_label(total_score),
        "explanation": payload.get("explanation") or "",
        "items": items,
    }


def keyword_score_for_criterion(article: dict, criterion: dict) -> float:
    text = _search_text(article)
    keywords = (criterion.get("keywords_json") or []) + (criterion.get("keywords_en_json") or [])
    if not keywords:
        return 0
    matches = sum(1 for keyword in keywords if _contains_keyword(text, keyword))
    return round(_clamp(matches / max(3, math.sqrt(len(keywords))) * 100, 0, 100), 2)


def score_label(score: float) -> str:
    if score >= 80:
        return "Высокая"
    if score >= 65:
        return "Выше средней"
    if score >= 40:
        return "Средняя"
    return "Низкая"


def _article_prompt(article: dict) -> str:
    base = "\n".join(
        [
            f"title: {article.get('title') or ''}",
            f"source: {article.get('source_name') or ''}",
            f"url: {article.get('url') or ''}",
            f"language: {article.get('language') or 'unknown'}",
            f"published_at: {article.get('published_at') or ''}",
            f"summary: {article.get('summary') or ''}",
            f"text: {_compact(article.get('raw_text') or '', 6000)}",
        ]
    )
    glossary = glossary_prompt_block(article)
    return f"{base}\n\n{glossary}" if glossary else base


_TAGS_SCOPE_CACHE: dict[str, Any] = {"block": None, "at": 0.0}
_TAGS_SCOPE_TTL_SECONDS = 300


def _tags_scope() -> str:
    """Блок тематик заказчика для гейта, с коротким кэшем.

    Гейт зовётся на КАЖДОЙ статье, а справочник тегов меняется редко и руками, так
    что ходить в базу каждый раз незачем. TTL короткий намеренно: заказчик правит
    тематики на экране и ждёт, что новая выборка поедет по ним, а не после деплоя.
    Сбой чтения не должен ронять гейт — тогда просто судим без тематик, как раньше.
    """
    now = time.monotonic()
    cached = _TAGS_SCOPE_CACHE.get("block")
    if cached is not None and now - float(_TAGS_SCOPE_CACHE.get("at") or 0) < _TAGS_SCOPE_TTL_SECONDS:
        return cached
    try:
        block = tags_scope_block(repository.list_enabled_tags())
    except Exception:  # noqa: BLE001 - тематики это подсказка, а не обязательный вход
        logger.warning("не удалось прочитать тематики для гейта релевантности")
        block = ""
    _TAGS_SCOPE_CACHE["block"] = block
    _TAGS_SCOPE_CACHE["at"] = now
    return block


def _relevance_prompt(article: dict, *, tags: list[dict] | None = None, text_limit: int = 6000) -> str:
    """Вход гейта релевантности — БЕЗ AI-сути (намеренно): только сырые поля статьи,
    чтобы суждение шло по реальному содержанию, а не по подкрученной нефтегаз-сути.

    С 17.09 сюда добавлен блок тематик заказчика: до этого теги влияли только на
    классификацию уже отобранного, и заказчик, расширяя их, не менял выборку вообще.
    Блок идёт в пользовательскую часть, а не в инструкции, чтобы не ломать кэш
    префикса: инструкции у всех статей одни и те же.
    """
    lines = [
        f"title: {article.get('title') or ''}",
        f"source: {article.get('source_name') or ''}",
        f"url: {article.get('url') or ''}",
        f"language: {article.get('language') or 'unknown'}",
        f"published_at: {article.get('published_at') or ''}",
        f"text: {_compact(article.get('raw_text') or '', text_limit)}",
    ]
    # Тематики берём из переданного списка, если он есть, и только иначе идём в базу.
    # Это не оптимизация: на проде стадия исполняется на зарубежном воркере, у
    # которого БАЗЫ НЕТ (docker-compose.external-worker.yml без DATABASE_URL).
    # Там чтение падало бы в except и блок молча уезжал пустым — то есть тематики
    # не влияли бы ни на что именно в боевом режиме. Теги в payload воркера уже
    # кладутся для стадии тегирования (external_ai.build_process_articles_payload).
    scope = tags_scope_block(tags) if tags else _tags_scope()
    if scope:
        lines.append("")
        lines.append(scope)
    return "\n".join(lines)


def _title_prompt(article: dict) -> str:
    """Вход переводчика — только заголовок и контекст источника (дёшево, без полного текста)."""
    base = "\n".join(
        [
            f"title: {article.get('title') or ''}",
            f"source: {article.get('source_name') or ''}",
            f"language: {article.get('language') or 'unknown'}",
            f"context: {_compact(article.get('raw_text') or article.get('summary') or '', 900)}",
        ]
    )
    glossary = glossary_prompt_block(article, limit=8)
    return f"{base}\n\n{glossary}" if glossary else base


def _negative_keyword_block(article: dict, tags: list[dict]) -> str | None:
    """Если текст статьи содержит стоп-слово родительского тега — вернуть причину, иначе None.
    Стоп-слова задаются только у родительских тегов (parent_id IS NULL) — бэклог заказчика #6."""
    text = _search_text(article)
    for tag in tags:
        if tag.get("parent_id"):
            continue
        for keyword in tag.get("negative_keywords_json") or []:
            if _contains_keyword(text, keyword):
                return f"стоп-слово «{keyword}» (тег «{tag.get('name')}»)"
    return None


def _search_text(article: dict) -> str:
    return " ".join(
        str(article.get(field) or "")
        for field in ("title", "summary", "raw_text", "source_category")
    ).lower()


def _contains_keyword(text: str, keyword: str) -> bool:
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return False
    if len(keyword) <= 4 or re.search(r"\s", keyword):
        return keyword in text
    return re.search(rf"\b{re.escape(keyword)}\b", text, flags=re.IGNORECASE) is not None


def _compact(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _valid_tag_id(value, tags: list[dict]) -> int:
    try:
        tag_id = int(value)
    except (TypeError, ValueError):
        return 0
    return tag_id if any(int(t["id"]) == tag_id for t in tags) else 0


def _validate_weights(criteria: list[dict]) -> None:
    if not criteria:
        raise ValueError("Нет активных критериев скоринга. Запустите seed-scoring.")
    total = sum(float(c["weight"]) for c in criteria)
    if round(total, 2) != 100:
        raise ValueError(f"Сумма весов критериев должна быть 100, сейчас {total}")


def _record_run(article: dict, stage: str, client, response: AIResponse) -> None:
    repository.insert_ai_run(
        {
            "article_id": article.get("id"),
            "stage": stage,
            "provider": getattr(client, "provider", "openai"),
            "model": response.model,
            "language": article.get("language"),
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "total_tokens": response.total_tokens,
            "cost_usd": response.cost_usd,
            "status": "ok",
            "error_message": None,
        }
    )


def _record_error(article: dict, stage: str, client, exc: Exception) -> None:
    repository.insert_ai_run(
        {
            "article_id": article.get("id"),
            "stage": stage,
            "provider": getattr(client, "provider", "openai"),
            "model": getattr(client, "model", None),
            "language": article.get("language"),
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0,
            "status": "error",
            "error_message": str(exc)[:1000],
        }
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def dumps_for_debug(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
