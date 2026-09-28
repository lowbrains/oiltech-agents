import pytest

from oiltech_digest.processing import external_ai


def test_external_ai_process_payload_offline_returns_structured_result():
    payload = {
        "offline": True,
        "articles": [
            {
                "id": 1,
                "title": "Directional drilling automation",
                "url": "https://example.com/a",
                "language": "en",
                "raw_text": "Automation improves drilling efficiency and well construction quality.",
                "source_name": "Example",
                "source_category": "Drilling",
            }
        ],
        "tags": [
            {
                "id": 10,
                "name": "Бурение",
                "parent_name": "Технологии",
                "name_en": "Drilling",
                "keywords_json": ["бурение"],
                "keywords_en_json": ["drilling", "well construction"],
            }
        ],
        "criteria": [
            {
                "id": 20,
                "name": "Технологическая значимость",
                "weight": 100,
                "description": "Technology impact",
                "keywords_json": ["технология"],
                "keywords_en_json": ["automation", "drilling"],
            }
        ],
    }

    result = external_ai.process_payload(payload)

    assert result["external_ai"] is True
    assert result["stats"]["processed"] == 1
    assert result["stats"]["summary"] == 1
    assert result["stats"]["tagged"] == 1
    assert result["stats"]["scored"] == 1
    article = result["articles"][0]
    assert article["article_id"] == 1
    assert article["summary"]["summary"]
    assert result["stats"]["translated"] == 1                # #2: перевод — отдельная стадия
    assert article["translation"]["title_ru"]                # иностранный заголовок переведён
    assert article["relevance"]["relevant"] is True
    assert article["tagging"]["tag_id"] == 10
    assert article["scoring"]["items"][0]["criterion_id"] == 20


def test_external_ai_process_payload_heartbeats_per_article():
    article = {
        "id": 1,
        "title": "Directional drilling automation",
        "url": "https://example.com/a",
        "language": "en",
        "raw_text": "Automation improves drilling efficiency and well construction quality.",
        "source_name": "Example",
        "source_category": "Drilling",
    }
    payload = {
        "offline": True,
        "articles": [dict(article, id=1), dict(article, id=2), dict(article, id=3)],
        "tags": [{"id": 10, "name": "Бурение", "parent_name": "Технологии", "name_en": "Drilling",
                  "keywords_json": ["бурение"], "keywords_en_json": ["drilling"]}],
        "criteria": [{"id": 20, "name": "Значимость", "weight": 100, "description": "x",
                      "keywords_json": [], "keywords_en_json": ["automation"]}],
    }
    beats = []
    # Колбэк, который один раз бросает — обработка не должна падать (heartbeat защищён).
    def heartbeat(done=None):
        beats.append(1)
        if len(beats) == 2:
            raise RuntimeError("transient heartbeat failure")

    result = external_ai.process_payload(payload, heartbeat=heartbeat)

    assert len(beats) == 3              # по разу на каждую из 3 статей
    assert result["stats"]["processed"] == 3   # сбой heartbeat не прервал батч


def test_heartbeat_that_cannot_take_the_result_fails_loudly():
    """23.09: циклы ИИ передают heartbeat итог на границе шага (его отдаст воркер, если шаг
    зависнет на остановке). Колбэк старой сигнатуры проглоченным TypeError тихо отключил бы
    и остановку, и отзыв аренды — класс 24.07. Поэтому это громкая ошибка."""
    payload = {
        "offline": True,
        "articles": [{"id": 1, "title": "Drilling", "url": "https://example.com/a", "raw_text": "drilling"}],
        "tags": [{"id": 10, "name": "Бурение"}],
        "criteria": [{"id": 20, "name": "Значимость", "weight": 100}],
    }

    with pytest.raises(TypeError):
        external_ai.process_payload(payload, heartbeat=lambda: None)
    with pytest.raises(TypeError):
        external_ai.process_recheck_payload({"articles": payload["articles"], "tags": []}, heartbeat=lambda: None)
    with pytest.raises(TypeError):
        external_ai.process_translate_payload({"articles": payload["articles"]}, heartbeat=lambda: None)


def test_external_ai_apply_process_result_calls_repository(monkeypatch):
    calls = []

    monkeypatch.setattr(external_ai.repository, "upsert_article_card", lambda *args: calls.append(("summary", args)))
    monkeypatch.setattr(external_ai.repository, "set_article_relevance", lambda *args: calls.append(("relevance", args)))
    monkeypatch.setattr(external_ai.repository, "upsert_article_tag", lambda *args: calls.append(("tagging", args)))
    monkeypatch.setattr(external_ai.repository, "replace_article_score", lambda *args: calls.append(("scoring", args)))
    monkeypatch.setattr(external_ai.repository, "insert_ai_run", lambda rec: calls.append(("run", rec["stage"])))
    monkeypatch.setattr(external_ai.repository, "get_articles_by_ids", lambda ids, **kwargs: [])

    stats = external_ai.apply_process_result(
        {
            "external_ai": True,
            "articles": [
                {
                    "article_id": 1,
                    "summary": {"summary": "Short", "model": "offline", "provider": "offline"},
                    "relevance": {"relevant": True, "reason": "ok", "model": "offline", "provider": "offline"},
                    "tagging": {"tag_id": 10, "confidence": 0.5, "rationale": "ok", "model": "offline", "provider": "offline"},
                    "scoring": {
                        "total_score": 50,
                        "score_label": "Средняя",
                        "explanation": "ok",
                        "items": [{"criterion_id": 20, "final_score": 50}],
                        "model": "offline",
                        "provider": "offline",
                    },
                }
            ],
        }
    )

    assert stats == {"articles": 1, "summary": 1, "relevance": 1, "translation": 0, "tagging": 1, "scoring": 1, "errors": 0}
    assert [item[0] for item in calls[:4]] == ["summary", "run", "relevance", "run"]
    assert ("run", "scoring") in calls



SPUD_ARTICLE = {
    "id": 1,
    "title": "Global Land Drilling Rigs Tracker",
    "raw_text": "In Egypt, NDC 9 spudded the vertical well T-200. The tracker adds more granular field data.",
    "language": "en",
}


def _stub_writes(monkeypatch, articles):
    written = {}
    monkeypatch.setattr(external_ai.repository, "get_articles_by_ids", lambda ids, **kwargs: articles(ids))
    monkeypatch.setattr(
        external_ai.repository, "upsert_article_card", lambda article_id, summary, model: written.update(summary=summary)
    )
    monkeypatch.setattr(
        external_ai.repository, "set_article_title_ru", lambda article_id, title_ru: written.update(title_ru=title_ru)
    )
    monkeypatch.setattr(external_ai, "_insert_run", lambda *args, **kwargs: None)
    return written


def test_core_applies_glossary_to_worker_result(monkeypatch):
    """Замечание заказчика 22.09: «спудрил вертикальную скважину», «более granularными».

    Словарь на воркере — его версия кода, NL пересобирает владелец; без прохода на ядре
    правка словаря не дошла бы до новых карточек до пересборки NL.
    """
    written = _stub_writes(monkeypatch, lambda ids: [SPUD_ARTICLE])

    external_ai.apply_process_result(
        {
            "articles": [
                {
                    "article_id": 1,
                    "summary": {
                        "summary": "В Египте NDC 9 спудрил вертикальную скважину T-200; трекер даёт более granularные данные.",
                        "model": "gpt",
                    },
                    "translation": {"title_ru": "NDC 9 спудрил скважину T-200", "model": "gpt"},
                }
            ]
        }
    )

    assert written["summary"] == "В Египте NDC 9 забурил вертикальную скважину T-200; трекер даёт более детальные данные."
    assert written["title_ru"] == "NDC 9 забурил скважину T-200"


def test_core_applies_glossary_to_translation_result(monkeypatch):
    written = _stub_writes(monkeypatch, lambda ids: [SPUD_ARTICLE])

    external_ai.apply_translate_result(
        {"articles": [{"article_id": 1, "translation": {"title_ru": "NDC 9 спудрил скважину T-200", "model": "gpt"}}]}
    )

    assert written["title_ru"] == "NDC 9 забурил скважину T-200"


def test_core_writes_paid_result_as_is_when_glossary_context_fails(monkeypatch):
    def broken(ids):
        raise RuntimeError("база недоступна")

    written = _stub_writes(monkeypatch, broken)

    stats = external_ai.apply_process_result(
        {"articles": [{"article_id": 1, "summary": {"summary": "NDC 9 спудрил скважину.", "model": "gpt"}}]}
    )

    assert stats["summary"] == 1
    assert written["summary"] == "NDC 9 спудрил скважину."


def test_apply_only_writes_summary_and_translation_but_bills_every_stage(monkeypatch):
    """Перегенерация сути (enqueue-resummarize): воркер NL старой сборки гоняет весь конвейер —
    пометки он не знает, — а ядро пишет только суть и перевод. Гейт, передумав, не уберёт статью
    из ленты, балл не сдвинется у отобранного в выпуск; оплаченные вызовы учтены все."""
    writes, runs = [], []
    monkeypatch.setattr(external_ai.repository, "get_articles_by_ids", lambda ids, **kwargs: [])
    monkeypatch.setattr(external_ai.repository, "upsert_article_card", lambda *args: writes.append("summary"))
    monkeypatch.setattr(external_ai.repository, "set_article_title_ru", lambda *args: writes.append("translation"))
    monkeypatch.setattr(external_ai.repository, "set_article_relevance", lambda *args: writes.append("relevance"))
    monkeypatch.setattr(external_ai.repository, "upsert_article_tag", lambda *args: writes.append("tagging"))
    monkeypatch.setattr(external_ai.repository, "replace_article_score", lambda *args: writes.append("scoring"))
    monkeypatch.setattr(external_ai, "_insert_run", lambda article_id, stage, payload, **kwargs: runs.append(stage))
    item = {
        "article_id": 1,
        "relevance": {"relevant": False, "reason": "передумал", "model": "gpt-5.5"},
        "summary": {"summary": "Новая суть", "model": "gpt-5-mini"},
        "translation": {"title_ru": "Новый заголовок", "model": "gpt-5-mini"},
        "tagging": {"tag_id": 10, "confidence": 0.5, "model": "gpt-5-mini"},
        "scoring": {"total_score": 10, "score_label": "Низкая", "items": [], "model": "gpt-5-mini"},
    }

    stats = external_ai.apply_process_result({"articles": [item]}, only=["summary", "translation"])

    assert writes == ["summary", "translation"]
    assert sorted(runs) == ["relevance", "scoring", "summary", "tagging", "translation"]
    assert (stats["summary"], stats["translation"], stats["relevance"], stats["scoring"]) == (1, 1, 0, 0)


def test_apply_rejects_malformed_only_before_any_write(monkeypatch):
    """payload_json — граница: строка «summary» дала бы множество букв, пустой список — все стадии."""
    import pytest

    writes = []
    monkeypatch.setattr(external_ai.repository, "get_articles_by_ids", lambda ids, **kwargs: [])
    monkeypatch.setattr(external_ai.repository, "upsert_article_card", lambda *args: writes.append(args))
    result = {"articles": [{"article_id": 1, "summary": {"summary": "Суть", "model": "gpt"}}]}

    for bad in ("summary", [], ["summary", "gate"], [1]):
        with pytest.raises(ValueError, match="only"):
            external_ai.apply_process_result(result, only=bad)
    assert writes == []


def test_resummarize_payload_drops_the_old_broken_summary(monkeypatch):
    """Старая суть попадала в промпт (_article_prompt кладёт summary) — модель повторяла брак."""
    article = {"id": 7, "title": "Power prices", "summary": "Цены на электроэнergyю выросли.", "raw_text": "x"}
    monkeypatch.setattr(external_ai.repository, "reserve_process_articles", lambda job_id, **kwargs: [7])
    monkeypatch.setattr(external_ai.repository, "get_articles_by_ids", lambda ids, **kwargs: [dict(article)])
    monkeypatch.setattr(external_ai.repository, "list_enabled_tags", lambda: [])
    monkeypatch.setattr(external_ai.repository, "list_enabled_scoring_criteria", lambda: [])

    regular = external_ai.build_process_articles_payload({"article_ids": [7]}, job_id=1)
    regen = external_ai.build_process_articles_payload({"article_ids": [7], "only": ["summary", "translation"]}, job_id=2)
    rescore = external_ai.build_process_articles_payload({"article_ids": [7], "only": ["scoring"]}, job_id=3)

    assert regular["articles"][0]["summary"] == article["summary"]
    assert regen["articles"][0]["summary"] is None
    # Пометку исполняет воркер (28.09): балл без перегенерации сути считается по записанной сути —
    # новой, как раньше, у него уже не будет.
    assert rescore["articles"][0]["summary"] == article["summary"]
    assert (regular.get("only"), regen["only"], rescore["only"]) == (None, ["summary", "translation"], ["scoring"])


def test_malformed_only_is_rejected_before_articles_are_reserved(monkeypatch):
    """Ревью 27.09: пометка проверялась только при записи — когда ответ модели уже оплачен."""
    import pytest

    reserved = []
    monkeypatch.setattr(external_ai.repository, "reserve_process_articles", lambda job_id, **kwargs: reserved.append(job_id) or [7])

    with pytest.raises(external_ai.InvalidJobPayload, match="only"):
        external_ai.build_process_articles_payload({"article_ids": [7], "only": "summary"}, job_id=3)
    assert reserved == []


def test_core_passes_only_from_job_payload_to_apply(monkeypatch):
    from oiltech_digest import api

    seen = {}
    monkeypatch.setattr(api.external_ai, "apply_process_result",
                        lambda result, *, job_id=None, only=None: seen.update(only=only, job_id=job_id) or {})
    job = {"kind": "process_articles", "payload_json": {"article_ids": [1], "only": ["summary", "translation"]}}

    api._apply_external_result(job, {"external_ai": True, "articles": []}, 42)

    assert seen == {"only": ["summary", "translation"], "job_id": 42}
    api._apply_external_result({"kind": "process_articles", "payload_json": {}}, {"external_ai": True, "articles": []}, 43)
    assert seen["only"] is None


def _gate_says_no(calls: list):
    """Офлайн-модель, чей гейт отвергает любую статью, — и список схем, за которые её звали."""
    from oiltech_digest.processing.openai_client import AIResponse, OfflineAIClient

    class GateSaysNo(OfflineAIClient):
        def complete_json(self, instructions, user_input, schema, **kwargs):
            calls.append(schema["name"])
            if schema["name"] == "article_relevance":
                return AIResponse(data={"relevant": False, "reason": "передумал"}, model="gate", input_tokens=1)
            return super().complete_json(instructions, user_input, schema, **kwargs)

    return GateSaysNo()


def _regeneration_payload(only) -> dict:
    return {
        "offline": True,
        "only": only,
        "articles": [
            # Стоп-слово родительского тега в заголовке: гейт отсёк бы статью без модели.
            {"id": 1, "title": "Oil prices rally", "url": "https://e.com/1", "language": "en",
             "raw_text": "Oil prices rally after drilling news.", "source_name": "S"},
            {"id": 2, "title": "Бурение на шельфе", "url": "https://e.ru/2", "language": "ru",
             "raw_text": "Бурение на шельфе Сахалина.", "source_name": "S"},
        ],
        "tags": [{"id": 10, "name": "Бурение", "parent_id": None, "negative_keywords_json": ["rally"],
                  "keywords_json": ["бурение"], "keywords_en_json": ["drilling"]}],
        "criteria": [{"id": 20, "name": "Значимость", "weight": 100, "keywords_json": [], "keywords_en_json": []}],
    }


def test_worker_runs_only_the_stages_the_core_will_write(monkeypatch):
    """Перегенерация сути (27.09): воркер гонял весь конвейер, а ядро писало только суть и
    перевод — один гейт был 71 % расхода ($0,59 из $0,83), и статью, которую он отверг, воркер
    оставлял без новой сути. Пометка only теперь приходит воркеру: не запрошенное не зовётся,
    гейт (и стоп-слова) суть не отменяет."""
    calls: list = []
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _gate_says_no(calls))

    result = external_ai.process_payload(_regeneration_payload(["summary", "translation"]))

    assert sorted(calls) == ["article_summary", "article_summary", "article_title_translation"]
    for item in result["articles"]:
        assert set(item) == {"article_id", "errors", "summary", "translation"}, item
    assert result["stats"]["summary"] == 2 and result["stats"]["rejected"] == 0


def test_gate_still_gates_when_only_asks_for_it(monkeypatch):
    """Пометка с гейтом — гейт работает, как в полном конвейере: отвергнутое дальше не зовётся."""
    calls: list = []
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _gate_says_no(calls))

    result = external_ai.process_payload(_regeneration_payload(["relevance", "summary"]))

    assert calls == ["article_relevance"]  # первую отсекло стоп-слово, вторую — модель
    assert [item["relevance"]["model"] for item in result["articles"]] == ["negative-keyword", "gate"]
    assert all("summary" not in item for item in result["articles"])


def test_worker_rejects_malformed_only_before_any_model_call(monkeypatch):
    """payload — граница: строка «summary» дала бы множество букв, и воркер не сделал бы ничего."""
    calls: list = []
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _gate_says_no(calls))

    with pytest.raises(external_ai.InvalidJobPayload, match="only"):
        external_ai.process_payload(_regeneration_payload("summary"))
    assert calls == []


def _recheck_result(relevant: bool) -> dict:
    return {
        "articles": [
            {
                "article_id": 42,
                "relevance": {
                    "relevant": relevant,
                    "reason": "тест",
                    "model": "gpt-5.5-2026-04-23",
                    "input_tokens": 1800,
                    "output_tokens": 120,
                    "total_tokens": 1920,
                    "cost_usd": 0.0126,
                },
            }
        ]
    }


def test_recheck_records_ai_run_for_rejected_articles(monkeypatch):
    """Регресс: вызов гейта оплачен OpenAI и при relevant=false — прогон обязан попасть
    в ai_processing_runs. Раньше _insert_run стоял только в ветке relevant=True, из-за чего
    отклонённые (~40% базы) были невидимы для экрана «AI-затраты» (разрыв с дашбордом до 4.5×)."""
    runs: list[tuple] = []
    monkeypatch.setattr(external_ai, "_insert_run", lambda *a, **k: runs.append(a))
    monkeypatch.setattr(external_ai.repository, "mark_article_for_deletion", lambda *a, **k: "marked")

    stats = external_ai.apply_recheck_result(_recheck_result(relevant=False), mark=True)

    assert stats["marked"] == 1
    assert len(runs) == 1, "прогон по отклонённой статье не записан — счёт снова занижен"
    assert runs[0][1] == "relevance"


def test_recheck_records_ai_run_for_kept_articles(monkeypatch):
    """Симметрия: у релевантных запись прогона тоже сохраняется (не сломали прежнее поведение)."""
    runs: list[tuple] = []
    monkeypatch.setattr(external_ai, "_insert_run", lambda *a, **k: runs.append(a))
    monkeypatch.setattr(external_ai.repository, "set_article_relevance", lambda *a, **k: None)

    stats = external_ai.apply_recheck_result(_recheck_result(relevant=True))

    assert stats["kept"] == 1
    assert len(runs) == 1


def test_recheck_skips_run_for_negative_keyword_block(monkeypatch):
    """Детерминированный стоп-слово-отсев не ходит в OpenAI → прогон писать НЕ надо."""
    runs: list[tuple] = []
    monkeypatch.setattr(external_ai, "_insert_run", lambda *a, **k: runs.append(a))
    monkeypatch.setattr(external_ai.repository, "mark_article_for_deletion", lambda *a, **k: "marked")
    result = _recheck_result(relevant=False)
    result["articles"][0]["relevance"]["model"] = "negative-keyword"

    external_ai.apply_recheck_result(result, mark=True)

    assert runs == []


def test_lease_loss_aborts_batch_instead_of_burning_money():
    """Инцидент 24.07: core отозвал lease (heartbeat 409), но воркер продолжал звать
    OpenAI по каждой статье. Результат уже не примут — деньги в мусор (~$11/час,
    80 минут). Теперь LeaseLost обязан прервать батч на ПЕРВОЙ же статье."""
    calls = {"heartbeats": 0}

    def failing_heartbeat(done=None):
        calls["heartbeats"] += 1
        raise external_ai.LeaseLost("lease lost for job 1181")

    payload = {
        "offline": True,
        "articles": [{"id": i, "title": f"A{i}", "url": f"https://e.ru/{i}",
                      "language": "ru", "raw_text": "бурение скважин", "source_name": "S"}
                     for i in range(1, 6)],
        "tags": [{"id": 10, "name": "Бурение", "keywords_json": ["бурение"], "keywords_en_json": []}],
        "criteria": [{"id": 1, "name": "Тех", "weight": 100, "keywords_json": [], "keywords_en_json": []}],
    }

    try:
        external_ai.process_payload(payload, heartbeat=failing_heartbeat)
        raise AssertionError("LeaseLost проглочен — батч продолжил работу и жжёт деньги")
    except external_ai.LeaseLost:
        pass

    assert calls["heartbeats"] == 1, "прервались не на первой статье — часть вызовов уже оплачена"


def test_transient_heartbeat_failure_does_not_abort_batch():
    """Обратная сторона: обычный сбой сети НЕ должен ронять батч — иначе любое моргание
    связи будет терять уже оплаченную работу. Прерываемся только на потере lease."""
    calls = {"heartbeats": 0}

    def flaky_heartbeat(done=None):
        calls["heartbeats"] += 1
        raise ConnectionError("сеть моргнула")

    payload = {
        "offline": True,
        "articles": [{"id": i, "title": f"A{i}", "url": f"https://e.ru/{i}",
                      "language": "ru", "raw_text": "бурение скважин", "source_name": "S"}
                     for i in range(1, 4)],
        "tags": [{"id": 10, "name": "Бурение", "keywords_json": ["бурение"], "keywords_en_json": []}],
        "criteria": [{"id": 1, "name": "Тех", "weight": 100, "keywords_json": [], "keywords_en_json": []}],
    }

    result = external_ai.process_payload(payload, heartbeat=flaky_heartbeat)

    assert calls["heartbeats"] == 3, "батч оборвался на временном сбое"
    assert result["stats"]["processed"] == 3
