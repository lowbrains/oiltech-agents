"""Feedback learning for technology signal discovery."""

from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path
from typing import Any

from oiltech_digest.db import repository


FEEDBACK_MEMORY_TYPES = {
    "signal_glossary",
    "signal_quality_rule",
    "signal_query_hint",
    "signal_source_preference",
    "signal_title_correction",
    "signal_verdict",
    "signal_duplicate",
}


def import_signal_feedback_csv(path: str | Path, *, user_id: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    rows = _read_feedback_csv(path)
    events = 0
    memories = 0
    extracted: list[dict[str, Any]] = []
    for row in rows:
        comment = str(row.get("Комментарий") or "").strip()
        if not comment:
            continue
        extracted_items = extract_feedback_memories(row)
        extracted.extend(extracted_items)
        if dry_run:
            events += 1
            memories += len(extracted_items)
            continue
        store_signal_feedback(row, user_id=user_id, import_source=str(path), extracted_items=extracted_items)
        events += 1
        memories += len(extracted_items)
    return {
        "rows": len(rows),
        "feedback_events": events,
        "memories": memories,
        "dry_run": dry_run,
        "extracted": extracted[:100],
    }


def store_signal_feedback(
    row: dict[str, Any],
    *,
    user_id: int | None = None,
    import_source: str | None = None,
    extracted_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    comment = str(row.get("Комментарий") or row.get("comment") or "").strip()
    signal_title = str(row.get("Сигнал") or row.get("signal_title") or "").strip()
    source_url = str(row.get("URL") or row.get("source_url") or "").strip()
    verdict = _normalize_verdict(row.get("verdict"))
    reason = str(row.get("reason") or "").strip()
    corrected_title = str(row.get("corrected_title") or "").strip()
    corrected_thesis = str(row.get("corrected_thesis") or "").strip()
    duplicate_of_signal_id = _optional_int(row.get("duplicate_of_signal_id"))
    article_id = _optional_int(row.get("article_id"))
    signal_id = _optional_int(row.get("signal_id"))
    signal_evidence_id = _optional_int(row.get("signal_evidence_id"))
    if not any([comment, verdict, reason, corrected_title, corrected_thesis, duplicate_of_signal_id]):
        raise ValueError("signal feedback comment or structured verdict is required")
    if article_id is None and signal_id is None and not source_url:
        raise ValueError("signal feedback requires article_id, signal_id or source_url")

    event_id = repository.record_signal_feedback_event(
        article_id,
        "comment_added",
        signal_id=signal_id,
        signal_evidence_id=signal_evidence_id,
        source_url=source_url or None,
        signal_title=signal_title or None,
        user_id=user_id,
        comment=comment,
        verdict=verdict,
        reason=reason or None,
        corrected_title=corrected_title or None,
        corrected_thesis=corrected_thesis or None,
        duplicate_of_signal_id=duplicate_of_signal_id,
    )
    facts = {
        "row_number": row.get("#") or row.get("row_number"),
        "signal_title": signal_title,
        "source_url": source_url,
        "source": row.get("Источник") or row.get("source"),
        "comment": comment,
        "verdict": verdict,
        "reason": reason,
        "corrected_title": corrected_title,
        "corrected_thesis": corrected_thesis,
        "duplicate_of_signal_id": duplicate_of_signal_id,
        "import_source": import_source,
        "feedback_event_id": event_id,
    }
    repository.attach_feedback_to_signal_training_examples(event_id, signal_id=signal_id)
    extracted_items = extracted_items if extracted_items is not None else extract_feedback_memories({
        "Комментарий": comment,
        "Сигнал": signal_title,
        "Источник": facts["source"] or "",
        "URL": source_url,
        "verdict": verdict or "",
        "reason": reason,
        "corrected_title": corrected_title,
        "corrected_thesis": corrected_thesis,
        "duplicate_of_signal_id": str(duplicate_of_signal_id or ""),
    })
    memory_ids = []
    for item in extracted_items:
        subject = _memory_subject(item["subject"])
        memory_ids.append(repository.upsert_signal_agent_memory(
            memory_key=_memory_key(item["memory_type"], subject, item.get("topic") or signal_title),
            memory_type=item["memory_type"],
            subject=subject,
            status=item.get("status") or "active",
            score=float(item.get("score") or 0),
            facts={**facts, **(item.get("facts") or {}), "raw_subject": item["subject"]},
        ))
    return {"event_id": event_id, "memory_ids": memory_ids, "memories": len(memory_ids)}


def extract_feedback_memories(row: dict[str, str]) -> list[dict[str, Any]]:
    comment = str(row.get("Комментарий") or "").strip()
    signal_title = str(row.get("Сигнал") or "").strip()
    source = str(row.get("Источник") or "").strip()
    source_url = str(row.get("URL") or "").strip()
    verdict = _normalize_verdict(row.get("verdict"))
    reason = str(row.get("reason") or "").strip()
    corrected_title = str(row.get("corrected_title") or "").strip()
    corrected_thesis = str(row.get("corrected_thesis") or "").strip()
    duplicate_of_signal_id = _optional_int(row.get("duplicate_of_signal_id"))
    memories: list[dict[str, Any]] = []

    memories.extend(_extract_glossary(comment, signal_title))
    corrected_title = corrected_title or _extract_title_correction(comment)
    if corrected_title:
        memories.append({
            "memory_type": "signal_title_correction",
            "subject": signal_title,
            "score": 80,
            "facts": {"preferred_title": corrected_title},
        })
    if corrected_thesis:
        memories.append({
            "memory_type": "signal_quality_rule",
            "subject": f"Для сигнала '{signal_title}' использовать исправленную суть: {corrected_thesis}",
            "score": 80,
            "facts": {"signal_title": signal_title, "corrected_thesis": corrected_thesis},
        })
    if verdict:
        memories.append({
            "memory_type": "signal_verdict",
            "subject": verdict,
            "score": _verdict_score(verdict),
            "facts": {
                "signal_title": signal_title,
                "source_url": source_url,
                "reason": reason or comment,
                "duplicate_of_signal_id": duplicate_of_signal_id,
            },
        })
    if verdict == "merge_duplicate" and duplicate_of_signal_id:
        memories.append({
            "memory_type": "signal_duplicate",
            "subject": signal_title,
            "score": 90,
            "facts": {
                "duplicate_of_signal_id": duplicate_of_signal_id,
                "source_url": source_url,
                "reason": reason or comment,
            },
        })
    for rule in _extract_quality_rules(comment):
        memories.append({
            "memory_type": "signal_quality_rule",
            "subject": rule,
            "score": 70,
            "facts": {"signal_title": signal_title},
        })
    for query in _derive_query_hints(comment, signal_title):
        memories.append({
            "memory_type": "signal_query_hint",
            "subject": query,
            "score": 65,
            "facts": {"signal_title": signal_title},
        })
    if _positive_source_comment(comment) and source_url:
        memories.append({
            "memory_type": "signal_source_preference",
            "subject": repository.normalize_domain(source_url),
            "score": 75,
            "facts": {"source": source, "source_url": source_url, "reason": "positive_feedback"},
        })
    return memories


def signal_feedback_memory_context(topic: str | None = None, *, limit: int = 80) -> dict[str, list[dict[str, Any]]]:
    result = {memory_type: [] for memory_type in FEEDBACK_MEMORY_TYPES}
    topic_l = (topic or "").lower()
    for memory_type in FEEDBACK_MEMORY_TYPES:
        rows = repository.list_signal_agent_memory(memory_type=memory_type, status="active", limit=limit)
        for row in rows:
            facts = row.get("facts_json") or {}
            haystack = " ".join(
                str(value or "")
                for value in (row.get("subject"), facts.get("signal_title"), facts.get("topic"), facts.get("comment"))
            ).lower()
            if topic_l and topic_l not in haystack and len(result[memory_type]) >= max(5, limit // 10):
                continue
            result[memory_type].append(row)
    return result


def _normalize_verdict(value: Any) -> str | None:
    verdict = str(value or "").strip().lower()
    if not verdict:
        return None
    aliases = {
        "good": "approved",
        "useful": "approved",
        "полезно": "approved",
        "ok": "approved",
        "bad": "reject",
        "noise": "reject",
        "шум": "reject",
        "дубль": "merge_duplicate",
        "duplicate": "merge_duplicate",
        "merge": "merge_duplicate",
    }
    verdict = aliases.get(verdict, verdict)
    allowed = {
        # Шкала заказчика (список Виктора от 13.09): оценка человека, а не модели.
        "strong_signal",
        "approved",
        "watch_later",
        "background_material",
        "reject",
        "wrong_domain",
        "merge_duplicate",
        # Ниже — вердикты прежней шкалы. С экрана убраны (заказчик их не просил),
        # но приём оставлен: в signal_feedback_events уже лежат строки с ними,
        # и запрет сделал бы прошлую разметку невалидной задним числом.
        "needs_better_source",
        "bad_translation",
        "too_generic",
    }
    return verdict if verdict in allowed else None


def _verdict_score(verdict: str) -> float:
    """Вес вердикта в памяти агента: чему учить на этой пометке.

    Веса прежних вердиктов НЕ трогаем. Они уже проставлены разметке, лежащей в
    signal_feedback_events, и пересчёт задним числом переписал бы смысл прошлых
    оценок. Новые значения шкалы заказчика добавлены рядом.

    «Наблюдать» и «фоновый материал» — положительные, но слабые: это не брак, а
    «рано» и «полезно как контекст». Отрицательными их делать нельзя, иначе агент
    выучит, что такие находки искать не надо.
    """
    if verdict == "strong_signal":
        return 100
    if verdict == "approved":
        return 90
    if verdict == "merge_duplicate":
        return 85
    if verdict == "watch_later":
        return 40
    if verdict == "background_material":
        return 25
    if verdict in {"reject", "wrong_domain", "too_generic"}:
        return -80
    if verdict in {"bad_translation", "needs_better_source"}:
        return 45
    return 0


def feedback_query_hints(topic: str | None, *, limit: int = 8) -> list[str]:
    memory = signal_feedback_memory_context(topic, limit=120)
    hints = [str(row.get("subject") or "").strip() for row in memory["signal_query_hint"]]
    preferred_domains = [str(row.get("subject") or "").strip() for row in memory["signal_source_preference"]]
    queries = []
    for hint in hints:
        if hint:
            queries.append(hint)
    for domain in preferred_domains[:3]:
        if domain:
            queries.append(f"site:{domain} {topic or 'oil gas technology'}")
    return _dedupe(queries)[:limit]


def feedback_prompt_block(topic: str | None = None, *, limit: int = 20) -> str:
    memory = signal_feedback_memory_context(topic, limit=120)
    lines = []
    if memory["signal_verdict"]:
        lines.append("feedback_verdict_examples:")
        for row in memory["signal_verdict"][:limit]:
            facts = row.get("facts_json") or {}
            reason = str(facts.get("reason") or "").strip()
            signal_title = str(facts.get("signal_title") or "").strip()
            lines.append(f"- verdict={row.get('subject')} signal={signal_title} reason={reason}")
    if memory["signal_duplicate"]:
        lines.append("feedback_duplicate_examples:")
        for row in memory["signal_duplicate"][:limit]:
            facts = row.get("facts_json") or {}
            lines.append(f"- duplicate signal={row.get('subject')} duplicate_of={facts.get('duplicate_of_signal_id')}")
    if memory["signal_quality_rule"]:
        lines.append("feedback_quality_rules:")
        for row in memory["signal_quality_rule"][:limit]:
            lines.append(f"- {row.get('subject')}")
    if memory["signal_glossary"]:
        lines.append("feedback_glossary:")
        for row in memory["signal_glossary"][:limit]:
            facts = row.get("facts_json") or {}
            lines.append(f"- {row.get('subject')} -> {facts.get('preferred_ru')}")
    return "\n".join(lines)


def apply_feedback_glossary(text: str, topic: str | None = None) -> str:
    result = text or ""
    memory = signal_feedback_memory_context(topic, limit=200)
    for row in memory["signal_glossary"]:
        facts = row.get("facts_json") or {}
        preferred = str(facts.get("preferred_ru") or "").strip()
        source_terms = [str(row.get("subject") or "").strip(), *[str(x).strip() for x in facts.get("source_terms") or []]]
        if not preferred:
            continue
        for source in source_terms:
            if source:
                result = _replace_term(result, source, preferred)
    return result


def _read_feedback_csv(path: str | Path) -> list[dict[str, str]]:
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = [line for line in text.splitlines() if not line.startswith(",,,,")]
    reader = csv.DictReader(lines)
    return [dict(row) for row in reader if row.get("Комментарий")]


def _extract_glossary(comment: str, signal_title: str) -> list[dict[str, Any]]:
    memories = []
    for source, preferred in _arrow_entries(comment):
        source = source.strip(" .;:\"'“”")
        preferred = preferred.strip(" .;:\"'“”")
        if not source or not preferred or len(source) > 80 or len(preferred) > 180:
            continue
        memories.append({
            "memory_type": "signal_glossary",
            "subject": source,
            "score": 85,
            "facts": {"preferred_ru": preferred, "source_terms": [source], "signal_title": signal_title},
        })
    return memories


def _extract_title_correction(comment: str) -> str | None:
    patterns = [
        r"Корректировка названия[^:\"“”]*[:：]\s*[\"“”]?([^\"\n“”]+)",
        r"Заголовок нужно[^:]*[:：]\s*([^.\n]+)",
        r"Лучше название[^:]*[:：]\s*([^.\n]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, comment, flags=re.I)
        if match:
            return match.group(1).strip(" .\"“”")
    return None


def _extract_quality_rules(comment: str) -> list[str]:
    rules = []
    markers = ("не путать", "всегда", "лучше", "не переводить", "сохранять", "проверять", "маркировать")
    normalized = re.sub(r"(?<!\d)(\d+)\.\s*", r"\n\1. ", comment)
    parts = []
    for raw_line in normalized.splitlines():
        line = re.sub(r"^\s*\d+\.\s*", "", raw_line).strip()
        parts.extend(part.strip() for part in re.split(r";|(?<=[.!?])\s+", line) if part.strip())
    for part in parts:
        if any(marker in part.lower() for marker in markers) and len(part) >= 20:
            rules.append(_memory_subject(part))
    return _dedupe(rules)[:8]


def _derive_query_hints(comment: str, signal_title: str) -> list[str]:
    hints = []
    glossary_terms = [
        str(memory["subject"])
        for memory in _extract_glossary(comment, signal_title)
        if memory.get("subject")
    ]
    for term in glossary_terms[:8]:
        term = term.strip(" .:\"'“”")
        if term and not re.search(r"[а-яё]", term, flags=re.I):
            hints.append(f"2026 {term} oil gas technology deployment")
    title_words = " ".join(re.findall(r"[A-Za-zА-Яа-яЁё0-9]{4,}", signal_title)[:6])
    if title_words:
        hints.append(f"2026 {title_words} oil gas")
    return _dedupe(hints)[:8]


def _arrow_entries(comment: str) -> list[tuple[str, str]]:
    arrow_matches = list(re.finditer(r"→|->", comment))
    entries = []
    for index, match in enumerate(arrow_matches):
        source = _source_before_arrow(comment, match.start())
        if not source:
            continue
        next_start = arrow_matches[index + 1].start() if index + 1 < len(arrow_matches) else len(comment)
        preferred_window = comment[match.end():next_start]
        if index + 1 < len(arrow_matches):
            next_source = _source_before_arrow(comment, arrow_matches[index + 1].start())
            if next_source and preferred_window.rstrip().endswith(next_source):
                preferred_window = preferred_window.rstrip()[:-len(next_source)]
            else:
                preferred_window = re.sub(
                    r"([A-Za-z][A-Za-z0-9+()/-]*(?:[ -][A-Za-z0-9+()/-]+){1,8}|[\u3400-\u9fff]{2,20})\s*$",
                    "",
                    preferred_window,
                )
        preferred = re.split(r"(?:\n\s*\d+\.|[。.!?]\s+\d+\.)", preferred_window, maxsplit=1)[0]
        entries.append((source, preferred))
    return entries


def _source_before_arrow(comment: str, arrow_start: int) -> str | None:
    window = comment[max(0, arrow_start - 120):arrow_start]
    ascii_match = re.search(r"([A-Za-z][A-Za-z0-9+()/-]*(?:[ -][A-Za-z0-9+()/-]+){0,8})\s*$", window)
    if ascii_match:
        return ascii_match.group(1).strip()
    cjk_match = re.search(r"([\u3400-\u9fff]{2,20})\s*$", window)
    if cjk_match:
        return cjk_match.group(1).strip()
    delimited_match = re.search(r"(?:^|[\n;:,])\s*(?:\d+\.\s*)?([^:;\n]{2,80})\s*$", window)
    if delimited_match:
        return delimited_match.group(1).strip()
    return None


def _positive_source_comment(comment: str) -> bool:
    normalized = comment.lower()
    return any(marker in normalized for marker in ("источник отличный", "источник хороший", "первичный источник", "статья интересная"))


def _replace_term(text: str, source: str, preferred: str) -> str:
    if re.search(r"[\u3400-\u9fff]", source):
        return text.replace(source, preferred)
    pattern = re.compile(rf"\b{re.escape(source)}\b", flags=re.I)
    return pattern.sub(preferred, text)


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        normalized = re.sub(r"\s+", " ", value).strip()
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _memory_subject(value: Any, *, max_chars: int = 420) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


def _memory_key(memory_type: str, subject: str, topic: str | None = None) -> str:
    digest = hashlib.sha1(f"{memory_type}:{topic or ''}:{subject}".lower().encode("utf-8")).hexdigest()[:20]
    return f"{memory_type}:{digest}"
