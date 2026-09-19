"""Дубли радара: одно событие — одна карточка.

Перенос механизма перепечаток MVP-1 (`processing/reprints.py`): дешёвое правило
отбирает пары-кандидаты, судья отвечает на один вопрос «одно ли это событие»,
а группы и главную карточку выбирает код.

Ключа сигнала для этого не хватало: он хеширует адрес или первые слова факта, а одно
событие приходит разными статьями и разными кластерами. Прогон 18.09 дал четыре
карточки про покупку Velocity Geomatics компанией ZenaTech в одной теме, две — про
завод сорбентов «Татнефти», четыре — про FleetRabbit, пять обзоров лития из пластовых вод.

Судья зовёт OpenAI, поэтому работает на внешнем воркере (`run_discovery`);
ядро только пишет решения (`apply_discovery`)."""

from __future__ import annotations

import itertools
import re
from typing import Any, Callable
from urllib.parse import urlsplit

SIGNAL_DUPLICATE_INSTRUCTIONS = """Ты сверяешь две карточки технологического радара и отвечаешь на ОДИН вопрос:
про одно ли они событие.

Одно событие — один конкретный факт: одна сделка, один запуск, один контракт, одно
внедрение, одно испытание, один пилот, одна новая норма. Карточки про него собирает
разная выдача: другой заголовок, другая тема радара, другие ссылки, другая оценка.
Это НЕ делает его другим событием.

same_event=true, если совпадают ГЛАВНЫЕ действующие лица и сам факт: кто, что сделал,
с чем. Различия в формулировке, теме радара, оценке и числе ссылок значения не имеют.
Две карточки-обзора одного и того же явления без своего конкретного события
(«перспективы добычи лития из пластовых вод в России», «дроны для инспекции
трубопроводов») — тоже true: это один сигнал, разложенный по разным темам.

same_event=false, если:
- это разные эпизоды одной истории (план и начало строительства; пилот и промышленный
  запуск; подписание и исполнение контракта);
- совпадает только компания или технология, а факты разные (два разных контракта одной
  компании; одна технология у разных компаний; разные месторождения);
- одна карточка — обзор явления, а другая — конкретное событие внутри него: обзор НЕ
  дубль события, даже если его упоминает;
- числа по одному параметру противоречат друг другу (объём, сумма, мощность, число
  единиц техники) — признак разных событий.

reason — одна короткая фраза по-русски: что за событие и почему решение такое.
Ответ строго по JSON Schema."""

SIGNAL_DUPLICATE_SCHEMA = {
    "name": "signal_duplicate_verdict",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["same_event", "reason"],
        "properties": {
            "same_event": {"type": "boolean"},
            "reason": {"type": "string"},
        },
    },
}

# Замер 18.09 на 70 сигналах прода: пересечение основ заголовка от 0,25 или общая
# компания дают 68 пар и находят все пять известных групп дублей; при 0,35 теряется
# половина обзоров по литию. Точность держит судья, порог отвечает только за охват.
PAIR_MIN_OVERLAP = 0.25
# Потолок расхода на прогон. Замер 19.09: 120 пар — $0,040 (gpt-5-mini), то есть
# $0,00033 за пару; 400 пар — около $0,13. При потолке 120 из 322 пар непроверенными
# остались как раз дубли: ZenaTech и FleetRabbit прошли новыми карточками. Ядро может
# передать свой потолок в снимке (dedup_max_pairs) — без пересборки воркера.
MAX_JUDGED_PAIRS = 400

# Положительные вердикты Виктора — те же, что питают подсказки поиска (signal_feedback).
_POSITIVE_VERDICTS = {"strong_signal", "approved", "watch_later", "needs_better_source", "bad_translation"}

_STEM_LEN = 4
_STOP_WORDS = {
    "для", "как", "или", "что", "это", "при", "над", "под", "после", "the", "and", "for",
    "with", "from", "нефтегаз", "нефтегазов", "нефтегазовой", "нефтегаза", "систем",
    "системы", "решени", "технолог", "мониторинг",
}
# Модель пишет издателя или заглушку вместо компании: «не указаны в источниках»,
# «bigchallenges.ru (источник)», «kissflow (publisher)». Общий издатель — не общий участник.
_PLACEHOLDER_COMPANY = re.compile(r"не указ|упомин|источник|публикатор|publisher|публикует|публицист")


def title_stems(signal: dict[str, Any]) -> set[str]:
    text = str(signal.get("title_ru") or signal.get("title") or "").lower()
    words = re.findall(r"[a-zа-яё0-9]{4,}", text)
    return {word[:_STEM_LEN] for word in words if word not in _STOP_WORDS and not word.isdigit()}


def company_keys(signal: dict[str, Any]) -> set[str]:
    keys = set()
    for company in signal.get("companies") or []:
        raw = str(company).strip().lower()
        if not raw or _PLACEHOLDER_COMPANY.search(raw):
            continue
        key = re.sub(r"\(.*?\)", "", raw)
        key = re.sub(r"[«»\"'?]", "", key)
        key = re.sub(r"\s+", " ", key).strip()
        if len(re.findall(r"[a-zа-яё]", key)) >= 2:
            keys.add(key)
    return keys


def url_keys(urls: list[str] | set[str]) -> set[str]:
    keys = set()
    for url in urls or []:
        value = re.sub(r"^https?://", "", str(url or "").strip().lower())
        value = value.split("#", 1)[0].split("?", 1)[0].rstrip("/").removeprefix("www.")
        if value:
            keys.add(value)
    return keys


def _overlap(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def _eligible(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Пару стоит судить, только если в ней есть что решать.

    Свежая карточка из этого прогона — всегда. Две сохранённые — только если одна из
    них свежая и не разобрана: иначе каждый прогон заново оплачивал бы одни и те же
    пары, а две разобранные Виктором сверялись бы вопреки его решению."""
    if a["kind"] == "new" or b["kind"] == "new":
        return True
    return any(node["fresh"] and not node["reviewed"] for node in (a, b))


def find_pairs(nodes: list[dict[str, Any]], *, min_overlap: float = PAIR_MIN_OVERLAP) -> list[tuple[int, int, float]]:
    """Пары-кандидаты: похожий заголовок, общая компания или общая ссылка.

    Порядок — для потолка: сначала пары со свежими карточками (решать надо сейчас),
    среди них — с общей компанией или ссылкой, потом по убыванию сходства заголовков.
    Общая компания — сильный признак даже при низком сходстве: английский заголовок
    против русского (ZenaTech, 19.09) даёт почти ноль общих основ."""
    features = [
        (title_stems(node["signal"]), company_keys(node["signal"]), url_keys(node.get("urls") or []))
        for node in nodes
    ]
    pairs = []
    for i, j in itertools.combinations(range(len(nodes)), 2):
        if not _eligible(nodes[i], nodes[j]):
            continue
        stems_i, companies_i, urls_i = features[i]
        stems_j, companies_j, urls_j = features[j]
        overlap = _overlap(stems_i, stems_j)
        shared = bool(companies_i & companies_j or urls_i & urls_j)
        if overlap >= min_overlap or shared:
            pairs.append((i, j, overlap, shared))
    pairs.sort(key=lambda pair: (
        0 if "new" in (nodes[pair[0]]["kind"], nodes[pair[1]]["kind"]) else 1,
        0 if pair[3] else 1,
        -pair[2],
    ))
    return [(i, j, overlap) for i, j, overlap, _shared in pairs]


def _hosts(urls: list[str] | set[str]) -> str:
    hosts = []
    for url in urls or []:
        host = urlsplit(str(url) if "://" in str(url) else f"https://{url}").netloc.lower().removeprefix("www.")
        if host and host not in hosts:
            hosts.append(host)
    return ", ".join(hosts[:5]) or "не указаны"


def pair_prompt(a: dict[str, Any], b: dict[str, Any]) -> str:
    blocks = []
    for label, node in (("A", a), ("B", b)):
        signal = node["signal"]
        blocks.append("\n".join([
            f"Карточка {label}",
            f"заголовок: {signal.get('title_ru') or signal.get('title') or ''}",
            f"компании: {', '.join(str(c) for c in signal.get('companies') or []) or 'не указаны'}",
            f"тема радара: {signal.get('theme') or ''}",
            f"суть: {str(signal.get('summary') or '')[:600]}",
            f"источники: {_hosts(node.get('urls') or [])}",
        ]))
    return "\n\n".join(blocks)


def _rank(node: dict[str, Any]) -> tuple:
    """Кто остаётся главной карточкой группы.

    Разобранная Виктором (сначала одобренная) — её он уже видел и оценил; затем уже
    сохранённая, и из них — появившаяся раньше: номер карточки Виктор использует как
    ссылку (19.09 вчерашняя №55 FleetRabbit ушла в сегодняшнюю №99 — у той было на
    одну ссылку больше); и только потом свежая из прогона."""
    signal = node["signal"]
    day = ""
    if node["kind"] == "existing" and node["reviewed"]:
        tier = 0 if node.get("verdict") in _POSITIVE_VERDICTS else 1
    elif node["kind"] == "existing":
        tier = 2
        day = str(signal.get("first_seen_day") or "")
    else:
        tier = 3
    return (
        tier,
        day,
        -float(signal.get("score") or 0),
        -int(signal.get("evidence_count") or 0),
        int(node.get("id") or 0),
        node.get("order", 0),
    )


def assign_duplicates(nodes: list[dict[str, Any]], edges: list[tuple[int, int, str]]) -> dict[int, tuple[int, str]]:
    """Звезда, а не цепочка: дублем становится только карточка, которую судья прямо
    сравнил с главной. Транзитивная склейка (A~B, B~C → A~C) собрала бы обзор лития
    и конкретный пилот в одну группу через третью карточку.

    Разобранная Виктором карточка не становится дублем никогда."""
    neighbours: dict[int, dict[int, str]] = {index: {} for index in range(len(nodes))}
    for i, j, reason in edges:
        neighbours[i][j] = reason
        neighbours[j][i] = reason
    order = sorted(range(len(nodes)), key=lambda index: _rank(nodes[index]))
    position = {index: place for place, index in enumerate(order)}
    assigned: dict[int, tuple[int, str]] = {}
    for primary in order:
        if primary in assigned:
            continue
        for other in sorted(neighbours[primary], key=lambda index: position[index]):
            if other in assigned or position[other] < position[primary]:
                continue
            if nodes[other]["kind"] == "existing" and nodes[other]["reviewed"]:
                continue
            assigned[other] = (primary, neighbours[primary][other])
    return assigned


def dedupe(
    nodes: list[dict[str, Any]],
    *,
    client_factory: Callable[[], Any],
    heartbeat: Callable[[], None] | None = None,
    max_pairs: int = MAX_JUDGED_PAIRS,
) -> dict[str, Any]:
    """Отобрать пары, спросить судью, собрать группы. В базу не ходит.

    Клиент создаётся, только если есть что судить."""
    beat = heartbeat or (lambda: None)
    pairs = find_pairs(nodes)
    judged = pairs[:max_pairs]
    edges: list[tuple[int, int, str]] = []
    stats = {"pairs": len(pairs), "judged": 0, "same": 0, "errors": 0,
             "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    client = client_factory() if judged else None
    for i, j, _overlap_value in judged:
        beat()
        try:
            response = client.complete_json(
                SIGNAL_DUPLICATE_INSTRUCTIONS,
                pair_prompt(nodes[i], nodes[j]),
                SIGNAL_DUPLICATE_SCHEMA,
                max_output_tokens=700,
            )
        except Exception:  # noqa: BLE001 - сбой одной пары не должен ронять прогон: пара остаётся «разные»
            stats["errors"] += 1
            continue
        stats["judged"] += 1
        stats["input_tokens"] += int(getattr(response, "input_tokens", 0) or 0)
        stats["output_tokens"] += int(getattr(response, "output_tokens", 0) or 0)
        try:
            stats["cost_usd"] += float(response.cost_usd)
        except Exception:  # noqa: BLE001 - цена неизвестной модели не повод терять решение
            pass
        data = response.data or {}
        if data.get("same_event") is True:
            stats["same"] += 1
            edges.append((i, j, str(data.get("reason") or "")[:300]))
    stats["cost_usd"] = round(stats["cost_usd"], 4)
    return {"assigned": assign_duplicates(nodes, edges), "stats": stats}
