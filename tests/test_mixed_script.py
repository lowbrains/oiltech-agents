from __future__ import annotations

import time

from fastapi.testclient import TestClient

from oiltech_digest import api
from oiltech_digest.db import connection
from oiltech_digest.network_policy import ExecutionDecision
from oiltech_digest.processing import external_ai, mixed_script
from oiltech_digest.processing.openai_client import AIResponse, OfflineAIClient

AUTH = {"Authorization": "Bearer secret"}


def _card(conn, source_id, url, title, title_ru, summary):
    article_id = conn.execute(
        "INSERT INTO articles (source_id, title, url, raw_text, language, content_hash) "
        "VALUES (%s, %s, %s, 'текст', 'ru', %s) RETURNING id",
        (source_id, title, url, url),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO article_cards (article_id, title_ru, summary) VALUES (%s, %s, %s)",
        (article_id, title_ru, summary),
    )
    return article_id


def _seed(conn):
    source_id = conn.execute(
        "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
        "VALUES ('S', 'News', 'https://example.com', TRUE, 'rss') RETURNING id"
    ).fetchone()[0]
    return {
        "twins": _card(conn, source_id, "https://example.com/1", "Title", "Компания вхoдит в топ", "Суть совместно сExxonMobil."),
        "half": _card(conn, source_id, "https://example.com/2", "Power prices", "Цены растут", "Цены на электроэнergyю выросли."),
        "title": _card(conn, source_id, "https://example.com/3", "Permian output", "Добыча в Пермian растёт", "Чисто."),
        "source": _card(conn, source_id, "https://example.com/4", "Биrol сказал", "Биrol сказал", "Чисто."),
        "clean": _card(conn, source_id, "https://example.com/5", "Clean", "Чисто", "Чисто."),
    }


def test_scripts_only_repair_fixes_twins_and_glue_and_writes_only_changed_fields(isolated_db):
    with connection.get_connection() as conn:
        ids = _seed(conn)
        conn.commit()

    dry = mixed_script.repair_cards(apply=False)
    assert {(c["article_id"], c["field"]) for c in dry["changes"]} == {(ids["twins"], "title_ru"), (ids["twins"], "summary")}

    mixed_script.repair_cards(apply=True)

    with connection.get_connection() as conn:
        cards = {row[0]: row[1:] for row in conn.execute("SELECT article_id, title_ru, summary FROM article_cards")}
    assert cards[ids["twins"]] == ("Компания входит в топ", "Суть совместно с ExxonMobil.")
    assert cards[ids["half"]] == ("Цены растут", "Цены на электроэнergyю выросли.")  # полуперевод — не наша починка


def test_resummarize_selection_separates_summary_title_and_source_defects(isolated_db):
    with connection.get_connection() as conn:
        ids = _seed(conn)
        conn.commit()

    selection = mixed_script.resummarize_selection()

    assert selection == {"summary": [ids["half"]], "title": [ids["title"]], "source_title": [ids["source"]]}


def test_explicit_articles_are_regenerated_without_the_script_check(isolated_db):
    with connection.get_connection() as conn:
        ids = _seed(conn)
        conn.commit()

    selection = mixed_script.resummarize_selection([ids["clean"], ids["half"]])

    assert sorted(selection["summary"]) == sorted([ids["clean"], ids["half"]])


class _GateSaysNo(OfflineAIClient):
    """Офлайн-модель, чей гейт отвергает любую статью, — и список схем, за которые её звали."""

    def __init__(self, calls: list) -> None:
        self.calls = calls

    def complete_json(self, instructions, user_input, schema, **kwargs):
        self.calls.append(schema["name"])
        if schema["name"] == "article_relevance":
            return AIResponse(data={"relevant": False, "reason": "передумал"}, model="gate", input_tokens=1)
        return super().complete_json(instructions, user_input, schema, **kwargs)


def _claim(core: TestClient) -> dict:
    # Повтор до 2 с: часы ВМ colima подводятся назад (~200 мс), и задача на миг «из будущего».
    deadline = time.monotonic() + 2.0
    while True:
        job = core.post("/api/external-worker/claim", headers=AUTH,
                        json={"worker_id": "nl-ai-1", "queues": ["external-ai"], "capabilities": ["openai"]}).json()["job"]
        if job is not None or time.monotonic() > deadline:
            return job
        time.sleep(0.05)


def test_resummarize_reaches_the_worker_with_only_and_rewrites_every_summary(isolated_db, monkeypatch):
    """Перегенерация 27.09 (задачи 11034–11038): воркер NL пометки only не знал — гонял гейт и
    балл, которые ядро не пишет ($0,59 из $0,83 — гейт), а статьи, которые его гейт счёл
    нерелевантными, пропускал: 7 из 52 сутей остались с браком. Теперь пометка доходит до
    воркера: гейт (и стоп-слова), теги и балл не зовутся, суть переписана у каждой статьи."""
    with connection.get_connection() as conn:
        ids = _seed(conn)
        # Стоп-слово родительского тега бьёт по заголовку «Power prices»: отсев гейта без модели.
        conn.execute("INSERT INTO tags (name, enabled, sort_order, negative_keywords_json) "
                     "VALUES ('Бурение', TRUE, 1, '[\"prices\"]')")
        conn.execute("INSERT INTO scoring_criteria (name, weight, enabled, sort_order) VALUES ('Значимость', 100, TRUE, 1)")
        conn.execute("UPDATE article_cards SET relevant = TRUE, relevance_reason = 'в ленте'")
        conn.commit()
    regenerate = [ids["twins"], ids["half"]]
    monkeypatch.setattr(mixed_script.network_policy, "route_ai_bulk",
                        lambda: ExecutionDecision("external-ai", "external", "openai", "test"))
    (job_id,) = mixed_script.enqueue_resummarize(regenerate, [])
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    core = TestClient(api.app)

    job = _claim(core)
    assert job is not None and job["id"] == job_id
    assert job["payload"]["only"] == ["summary", "translation"]  # пометка доходит до воркера

    calls: list = []
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _GateSaysNo(calls))
    result = external_ai.process_payload(job["payload"])
    # Только суть и перевод (оба заголовка иностранные): ни гейта, ни тегов, ни балла.
    assert sorted(calls) == ["article_summary"] * 2 + ["article_title_translation"] * 2
    assert all("relevance" not in item and "tagging" not in item and "scoring" not in item
               for item in result["articles"])

    response = core.post(f"/api/external-worker/jobs/{job_id}/complete", headers=AUTH,
                         json={"lease_token": job["lease_token"], "result": result})
    assert response.status_code == 200
    with connection.get_connection() as conn:
        cards = {row[0]: row[1:] for row in conn.execute(
            "SELECT article_id, summary, relevant, relevance_reason FROM article_cards WHERE article_id = ANY(%s)",
            (regenerate,))}
        stages = {row[0] for row in conn.execute("SELECT stage FROM ai_processing_runs WHERE job_id = %s", (job_id,))}
    assert cards[ids["twins"]][0] != "Суть совместно сExxonMobil."
    assert cards[ids["half"]][0] != "Цены на электроэнergyю выросли."  # стоп-слово суть не остановило
    assert all(card[1:] == (True, "в ленте") for card in cards.values())  # вердикт гейта не тронут
    assert stages == {"summary", "translation"}  # оплачено и учтено только запрошенное

