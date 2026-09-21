"""Подсказки поиска радара — только в своей теме (21.09, «в выдаче один литий»).

Три одобренные литиевые карточки 13–15.09 дали подсказки без темы, а фильтр пускал
подсказку в любую тему с одним общим словом: 21.09 радар искал литий 13 раз из 52 в
шести темах. Каждый тест падает на коде до правки."""

from oiltech_digest import signal_discovery, signal_feedback
from oiltech_digest.db import repository

MINING = "Добыча, механизированный фонд и внутрискважинное оборудование"
CHEMISTRY = "Химия, материалы, вода и извлечение ценных компонентов"
DRILLING = "Бурение, направленное бурение, буровые растворы и буровое оборудование"
LITHIUM_HINT = "2026 Ученые нашли способ добычи лития нефтегазовых oil gas"


def _memory(*rows: dict) -> dict:
    return {"signal_query_hint": list(rows), "signal_source_preference": []}


def test_hint_goes_only_to_its_own_topic_even_sharing_a_word():
    # «добычи» в подсказке больше не тянет её в тему «Добыча».
    memory = _memory({"subject": LITHIUM_HINT, "score": 65, "facts_json": {"topic": CHEMISTRY}})

    with signal_feedback.use_memory_snapshot(memory):
        assert signal_feedback.feedback_query_hints(CHEMISTRY) == [LITHIUM_HINT]
        assert signal_feedback.feedback_query_hints(MINING) == []


def test_hint_without_topic_is_not_searched_anywhere():
    memory = _memory({"subject": LITHIUM_HINT, "score": 65, "facts_json": {"verdict": "approved"}})

    with signal_feedback.use_memory_snapshot(memory):
        assert [signal_feedback.feedback_query_hints(topic) for topic in (MINING, CHEMISTRY, DRILLING)] == [[], [], []]


def test_feedback_on_card_gives_hint_of_the_card_topic(isolated_db):
    signal_id = repository.upsert_signal(
        {"signal_key": "k-li", "title": "Tatneft lithium", "theme": CHEMISTRY, "maturity": "watch", "score": 70}
    )

    signal_feedback.store_signal_feedback({
        "signal_id": signal_id,
        "signal_title": "Татнефть строит завод сорбентов для извлечения лития",
        "verdict": "approved",
        "comment": "Сильный сигнал",
    })

    hints = repository.list_signal_agent_memory(memory_type="signal_query_hint", status="active", limit=10)
    assert hints and {row["facts_json"]["topic"] for row in hints} == {CHEMISTRY}
    assert signal_feedback.feedback_query_hints(CHEMISTRY)
    assert signal_feedback.feedback_query_hints(MINING) == []


def _topics(monkeypatch):
    topics = [{"name": MINING}, {"name": CHEMISTRY}, {"name": DRILLING}]
    keywords = {
        MINING: ["механизированная добыча", "насосы", "внутрискважинное оборудование"],
        CHEMISTRY: ["литий", "лития", "извлечение лития", "попутная вода", "пластовая вода", "реагенты"],
        DRILLING: ["буровые растворы", "долота", "направленное бурение"],
    }
    monkeypatch.setattr(signal_discovery, "_radar_topics", lambda: topics)
    monkeypatch.setattr(
        signal_discovery,
        "_topic_tag_context",
        lambda name: {"keywords_ru": keywords[name], "keywords_en": [], "tags": [], "negative_keywords": [], "descriptions": []},
    )


def test_legacy_hints_get_one_topic_or_none_and_dry_run_writes_nothing(isolated_db, monkeypatch):
    _topics(monkeypatch)
    for key, subject in (
        ("li", "2026 Извлечение лития попутной воды местах добычи oil gas"),
        ("vague", "2026 технологии нефтегаз oil gas"),
    ):
        repository.upsert_signal_agent_memory(
            memory_key=f"legacy-{key}", memory_type="signal_query_hint", subject=subject, facts={"verdict": "approved"}
        )

    dry = signal_discovery.assign_query_hint_topics(dry_run=True)

    assert [(item["subject"].split(" попутной")[0], item["topic"]) for item in dry["assigned"]] == [
        ("2026 Извлечение лития", CHEMISTRY)
    ]
    assert [item["subject"] for item in dry["unassigned"]] == ["2026 технологии нефтегаз oil gas"]
    assert signal_feedback.feedback_query_hints(CHEMISTRY) == []  # сухой прогон ничего не записал

    signal_discovery.assign_query_hint_topics(dry_run=False)

    assert signal_feedback.feedback_query_hints(CHEMISTRY) == ["2026 Извлечение лития попутной воды местах добычи oil gas"]
    assert signal_feedback.feedback_query_hints(MINING) == []
