from oiltech_digest.processing import pipeline
from oiltech_digest.processing.domain_glossary import (
    enforce_glossary_text,
    glossary_golden_cases,
    glossary_prompt_block,
    run_terminology_eval,
    terminology_warnings,
    validate_glossary,
)
from oiltech_digest.processing import digest
from oiltech_digest.processing import external_ai
from oiltech_digest.processing.openai_client import AIResponse, OfflineAIClient, _extract_output_text
from oiltech_digest.processing.seed import DEFAULT_SCORING_CRITERIA, _split_keywords, _tag_signal_enrichment


class _RecordingClient:
    """Фейк AI-клиент: пишет порядок вызовов по имени схемы + переданные model/effort/вход."""

    model = "fake"

    def __init__(self, relevant: bool = True) -> None:
        self.calls: list[dict] = []
        self.relevant = relevant

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900,
                      model=None, reasoning_effort=None):
        name = schema["name"]
        self.calls.append({
            "name": name,
            "model": model,
            "reasoning": reasoning_effort,
            "input": user_input,
            "max_output_tokens": max_output_tokens,
        })
        if name == "article_relevance":
            return AIResponse(data={"relevant": self.relevant, "reason": "x"}, model=model or "fake")
        if name == "article_summary":
            return AIResponse(data={"summary": "s"}, model="fake")
        if name == "article_tag":
            return AIResponse(data={"tag_id": 10, "confidence": 0.5, "rationale": "r"}, model="fake")
        if name == "article_score":
            return AIResponse(
                data={"total_score": 50, "score_label": "Средняя", "explanation": "e", "items": []},
                model="fake",
            )
        return AIResponse(data={}, model="fake")


def _external_payload(article_extra: dict | None = None) -> dict:
    article = {
        "id": 1,
        "title": "Война: удары по городу, есть жертвы",
        "url": "https://example.com/a",
        "language": "ru",
        "raw_text": "Военная сводка без отношения к нефтегазу.",
        "source_name": "Интерфакс ТЭК",
        "source_category": "Новости",
    }
    article.update(article_extra or {})
    return {
        "articles": [article],
        "tags": [{"id": 10, "name": "Бурение", "parent_name": "Технологии", "name_en": "Drilling",
                  "keywords_json": [], "keywords_en_json": []}],
        "criteria": [{"id": 20, "name": "Значимость", "weight": 100, "description": "",
                      "keywords_json": [], "keywords_en_json": []}],
    }


def test_external_ai_irrelevant_skips_summary_tag_score(monkeypatch):
    """Гейт релевантности первым: нерелевантную статью НЕ суммируем/тегируем/скорим."""
    client = _RecordingClient(relevant=False)
    monkeypatch.setattr(external_ai, "make_client", lambda offline: client)

    result = external_ai.process_payload(_external_payload())

    assert [c["name"] for c in client.calls] == ["article_relevance"]
    assert result["stats"]["rejected"] == 1
    assert result["stats"]["summary"] == 0
    item = result["articles"][0]
    assert item["relevance"]["relevant"] is False
    assert "summary" not in item and "scoring" not in item


def test_external_ai_relevance_runs_first_and_ignores_summary(monkeypatch):
    """Гейт идёт ПЕРВЫМ и судит по сырому тексту — AI-суть не попадает ему на вход."""
    client = _RecordingClient(relevant=True)
    monkeypatch.setattr(external_ai, "make_client", lambda offline: client)

    result = external_ai.process_payload(_external_payload({"summary": "ПОДКРУЧЕННАЯ-СУТЬ-НЕФТЕГАЗ"}))

    names = [c["name"] for c in client.calls]
    assert names[0] == "article_relevance"
    assert names == ["article_relevance", "article_summary", "article_tag", "article_score"]
    rel_input = next(c["input"] for c in client.calls if c["name"] == "article_relevance")
    assert "ПОДКРУЧЕННАЯ-СУТЬ-НЕФТЕГАЗ" not in rel_input
    assert "summary:" not in rel_input
    assert result["stats"]["relevant"] == 1


def test_relevance_article_uses_relevance_model_and_reasoning(monkeypatch):
    """Гейт зовётся с отдельной (более сильной) моделью и повышенным reasoning."""
    monkeypatch.setattr(pipeline.config, "OPENAI_RELEVANCE_MODEL", "strong-model")
    monkeypatch.setattr(pipeline.config, "OPENAI_RELEVANCE_REASONING", "high")
    client = _RecordingClient(relevant=True)

    # Суть — различимая строка, а не одна буква: в промпте гейта с 17.09 есть блок
    # тематик заказчика, и проверка на односимвольное «S» ловила бы любую тематику
    # с латинской S в названии, а не подачу сути.
    pipeline.relevance_article(
        {"title": "t", "raw_text": "x", "summary": "ПОДКРУЧЕННАЯ-СУТЬ-НЕФТЕГАЗ"}, client
    )

    call = client.calls[-1]
    assert call["name"] == "article_relevance"
    assert call["model"] == "strong-model"
    assert call["reasoning"] == "high"
    assert call["max_output_tokens"] == 2500
    assert "ПОДКРУЧЕННАЯ-СУТЬ-НЕФТЕГАЗ" not in call["input"]  # суть не в промпте гейта
    assert "summary:" not in call["input"]


def test_relevance_article_retries_with_compact_prompt_on_output_limit(monkeypatch):
    from oiltech_digest.processing.openai_client import AIClientError

    monkeypatch.setattr(pipeline.config, "OPENAI_RELEVANCE_MODEL", "gpt-5-nano")
    monkeypatch.setattr(pipeline.config, "OPENAI_RELEVANCE_REASONING", "medium")

    class FlakyClient:
        model = "fake"

        def __init__(self):
            self.calls = []

        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            self.calls.append({
                "input": user_input,
                "model": model,
                "reasoning": reasoning_effort,
                "max_output_tokens": max_output_tokens,
            })
            if len(self.calls) == 1:
                raise AIClientError("OpenAI response does not contain output text (incomplete_details={'reason': 'max_output_tokens'})")
            return AIResponse(data={"relevant": False, "reason": "нет связи с нефтегазом"}, model=model or "fake")

    client = FlakyClient()
    long_text = "бурение " * 1200

    response = pipeline.relevance_article({"title": "t", "raw_text": long_text, "summary": "S"}, client)

    assert response.data["relevant"] is False
    assert len(client.calls) == 2
    assert client.calls[0]["reasoning"] == "medium"
    assert client.calls[1]["reasoning"] == "minimal"
    assert len(client.calls[1]["input"]) < len(client.calls[0]["input"])


def test_reasoning_effort_across_gpt5_generations():
    from oiltech_digest.processing.openai_client import _reasoning_effort

    # Исходный GPT-5: minimal валиден, none → minimal.
    assert _reasoning_effort("gpt-5-mini", "minimal") == "minimal"
    assert _reasoning_effort("gpt-5-mini", "none") == "minimal"
    assert _reasoning_effort("gpt-5-nano", "") == "minimal"
    # 5.1+ (включая 5.4/5.5 с датой): minimal невалиден → none; пустое → none.
    assert _reasoning_effort("gpt-5.5-2026-04-23", "minimal") == "none"
    assert _reasoning_effort("gpt-5.5", "") == "none"
    assert _reasoning_effort("gpt-5.4-mini", "") == "none"
    assert _reasoning_effort("gpt-5.1-codex", "minimal") == "none"
    # Явные low/medium/high проходят как есть на любом поколении.
    assert _reasoning_effort("gpt-5.5", "medium") == "medium"
    assert _reasoning_effort("gpt-5-mini", "low") == "low"


def test_extract_output_text_from_responses_shape():
    raw = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": '{"summary":"ok"}'}],
            }
        ]
    }
    assert _extract_output_text(raw) == '{"summary":"ok"}'


def test_ai_response_cost_uses_configured_rates():
    response = AIResponse(data={}, model="m", input_tokens=1_000_000, output_tokens=1_000_000)
    assert response.cost_usd > 0


def test_split_keywords_semicolon_and_newline():
    assert _split_keywords("ГРП; бурение\nцементирование") == ["ГРП", "бурение", "цементирование"]


def test_signal_enrichment_adds_chinese_hse_keywords():
    extra = _tag_signal_enrichment("Экология, промышленная безопасность, HSE и устойчивое развитие")

    assert "predictive HSE" in extra["keywords_en"]
    assert "电子作业票" in extra["keywords_cn"]
    assert "防碰撞系统" in extra["keywords_cn"]


def test_default_scoring_weights_equal_100():
    assert sum(item["weight"] for item in DEFAULT_SCORING_CRITERIA) == 100


def test_keyword_tag_selects_best_tag():
    article = {
        "title": "Electric frac fleet reduces diesel consumption",
        "raw_text": "New electric frac technology improves hydraulic fracturing operations.",
    }
    tags = [
        {"id": 1, "keywords_json": [], "keywords_en_json": ["seismic"]},
        {"id": 2, "keywords_json": ["ГРП"], "keywords_en_json": ["electric frac", "hydraulic fracturing"]},
    ]
    assert pipeline.keyword_tag(article, tags)["tag_id"] == 2


def test_glossary_prompt_selects_relevant_oilfield_terms():
    article = {
        "title": "Electric frac fleet expands hydraulic fracturing operations",
        "raw_text": "The company uses electric frac units and proppant for hydraulic fracturing.",
    }

    block = glossary_prompt_block(article)

    assert "hydraulic fracturing" in block
    assert "preferred_ru: ГРП" in block
    assert "forbidden_ru: фракинг" in block
    assert "proppant" in block


def test_summary_enforces_oilfield_preferred_terms():
    class BadTranslatorClient:
        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            assert "preferred_ru: ГРП" in user_input
            return AIResponse(
                data={"summary": "Компания расширила фракинг и закупила новые флоты."},
                model="fake",
            )

    article = {
        "title": "Electric frac fleet expands hydraulic fracturing operations",
        "raw_text": "Electric frac fleet expands hydraulic fracturing operations in oilfields.",
    }

    response = pipeline.summarize_article(article, BadTranslatorClient())

    assert "ГРП" in response.data["summary"]
    assert "фракинг" not in response.data["summary"].lower()
    assert terminology_warnings(response.data["summary"], article) == []


def test_glossary_enforces_inflected_bad_terms():
    class BadTranslatorClient:
        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            return AIResponse(
                data={"summary": "После фракинга на оффшоре компания провела флоубэка анализ и ворковеров программу."},
                model="fake",
            )

    article = {
        "title": "Offshore fracking flowback and workover program",
        "raw_text": "Offshore hydraulic fracturing generated flowback and required workover operations.",
    }

    response = pipeline.summarize_article(article, BadTranslatorClient())
    summary = response.data["summary"].lower()

    assert "грп" in summary
    assert "на шельфе" in summary
    assert "анализ жидкости обратного притока" in summary
    assert "крс" in summary
    assert "фракинг" not in summary
    assert "оффшор" not in summary
    assert "флоубэк" not in summary
    assert "ворковер" not in summary


def test_glossary_catches_reservoir_and_stimulation_calques():
    class BadTranslatorClient:
        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            assert "preferred_ru: пласт" in user_input
            assert "preferred_ru: интенсификация притока" in user_input
            return AIResponse(
                data={"summary": "Оператор провёл стимуляцию скважины для резервуара."},
                model="fake",
            )

    article = {
        "title": "Well stimulation improves reservoir output",
        "raw_text": "Well stimulation improved reservoir output in the producing formation.",
    }

    response = pipeline.summarize_article(article, BadTranslatorClient())

    assert "провёл интенсификацию притока" in response.data["summary"]
    assert "для пласта" in response.data["summary"]
    assert "стимуляция скважины" not in response.data["summary"]
    assert "резервуар" not in response.data["summary"]


def test_glossary_golden_cases_pass():
    for case in glossary_golden_cases():
        fixed = enforce_glossary_text(str(case["bad"]), case["article"])
        fixed_lower = fixed.lower()
        missing = [term for term in case["must_have"] if term.lower() not in fixed_lower]
        forbidden = [term for term in case["must_not"] if term.lower() in fixed_lower]

        assert not missing, f"{case['name']}: {fixed}; missing={missing}"
        assert not forbidden, f"{case['name']}: {fixed}; forbidden={forbidden}"


def test_domain_glossary_definition_is_valid():
    assert validate_glossary() == []


def test_terminology_eval_passes_100_examples():
    report = run_terminology_eval(limit=100)

    assert report["total"] == 100
    assert report["failed"] == 0
    assert report["passed"] == 100


def test_title_translation_enforces_completion_and_workover_terms():
    class BadTitleClient:
        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            assert "preferred_ru: заканчивание скважины" in user_input
            assert "preferred_ru: КРС" in user_input
            return AIResponse(
                data={"title_ru": "Завершение скважины и ворковер увеличили добычу"},
                model="fake",
            )

    article = {
        "title": "Well completion and workover improve production",
        "raw_text": "The article describes well completion and workover operations.",
        "language": "en",
    }

    title_ru, response = pipeline.title_ru_for_article(article, BadTitleClient())

    assert response is not None
    assert "заканчивание скважины" in title_ru.lower()
    assert "КРС" in title_ru
    assert "ворковер" not in title_ru.lower()


def test_offline_summary_is_deterministic():
    client = OfflineAIClient()
    response = client.complete_json(
        "x",
        "title: Test title\ntext: First sentence. Second sentence. Third.",
        {"name": "article_summary"},
    )
    assert "Test title" in response.data["summary"]


def test_offline_pipeline_outputs_digest_ready_content(monkeypatch):
    article = {
        "id": 501,
        "title": "Electric frac fleet reduces diesel consumption",
        "source_name": "World Oil",
        "url": "https://example.com/electric-frac",
        "language": "en",
        "published_at": None,
        "raw_text": (
            "New electric frac technology improves hydraulic fracturing operations. "
            "The fleet reduces diesel consumption and lowers emissions for oilfield service crews."
        ),
        "text_truncated": False,
    }
    tags = [
        {
            "id": 10,
            "name": "ГРП",
            "parent_name": "Технологии",
            "keywords_json": [],
            "keywords_en_json": ["electric frac", "hydraulic fracturing"],
        }
    ]
    criteria = [
        {
            "id": 20,
            "name": "Технологическая значимость",
            "weight": 100,
            "keywords_json": [],
            "keywords_en_json": ["electric frac", "hydraulic fracturing", "oilfield service"],
        }
    ]
    state = {"runs": []}

    monkeypatch.setattr(pipeline.repository, "list_enabled_tags", lambda: tags)
    monkeypatch.setattr(pipeline.repository, "list_enabled_scoring_criteria", lambda: criteria)
    monkeypatch.setattr(
        pipeline.repository,
        "upsert_article_card",
        lambda article_id, summary, model=None, title_ru=None: state.update(
            {"article_id": article_id, "summary": summary, "summary_model": model, "title_ru": title_ru}
        ),
    )
    monkeypatch.setattr(
        pipeline.repository,
        "set_article_title_ru",
        lambda article_id, title_ru: state.update({"title_ru": title_ru}),
    )
    monkeypatch.setattr(
        pipeline.repository,
        "set_article_relevance",
        lambda article_id, relevant, reason, model=None: state.update(
            {"relevant": relevant, "relevance_reason": reason, "relevance_model": model}
        ),
    )
    monkeypatch.setattr(
        pipeline.repository,
        "upsert_article_tag",
        lambda article_id, tag_id, confidence, rationale, model=None: state.update(
            {"tag_id": tag_id, "tag_confidence": confidence, "tag_rationale": rationale, "tag_model": model}
        ),
    )
    monkeypatch.setattr(
        pipeline.repository,
        "replace_article_score",
        lambda article_id, total_score, score_label, explanation, items, model=None: state.update(
            {
                "score_article_id": article_id,
                "total_score": total_score,
                "score_label": score_label,
                "score_explanation": explanation,
                "score_items": items,
                "score_model": model,
            }
        ),
    )
    monkeypatch.setattr(pipeline.repository, "insert_ai_run", lambda rec: state["runs"].append(rec))

    stats = pipeline.process_pipeline_articles([article], OfflineAIClient(), fetch_full=False)

    assert stats == {
        "processed": 1,
        "fulltext": 0,
        "summary": 1,
        "relevant": 1,
        "rejected": 0,
        "tagged": 1,
        "translated": 1,
        "scored": 1,
        "errors": 0,
    }
    assert state["summary"].startswith(article["title"])
    assert state["relevant"] is True
    assert state["title_ru"]  # иностранный заголовок переведён отдельной стадией
    assert state["tag_id"] == 10
    assert state["total_score"] >= 65
    assert state["score_label"] in {"Выше средней", "Высокая"}
    # Релевантность идёт ПЕРВОЙ — гейт до суммаризации (фикс «мусор в выборке» 2026-06).
    # Перевод заголовка — отдельная стадия после сути.
    assert [run["stage"] for run in state["runs"]] == ["relevance", "summary", "translation", "tagging", "scoring"]
    assert all(run["provider"] == "offline" and run["status"] == "ok" for run in state["runs"])

    class PublishedAt:
        def date(self):
            return self

        def isoformat(self):
            return "2026-06-07"

    monkeypatch.setattr(
        digest.repository,
        "digest_candidates",
        lambda month=None, limit=20, min_score=60, user_id=None, **kwargs: [
            {
                "id": article["id"],
                "title": article["title"],
                "source_name": article["source_name"],
                "url": article["url"],
                "published_at": PublishedAt(),
                "tag_name": "ГРП",
                "parent_tag_name": "Технологии",
                "total_score": state["total_score"],
                "score_label": state["score_label"],
                "summary": state["summary"],
                "image_url": "",
            }
        ],
    )

    content = digest.build_digest_content("2026-06", min_score=65)

    assert content["news"][0]["article_id"] == 501
    assert content["news"][0]["category"] == "Технологии / ГРП"
    assert content["news"][0]["score"] == state["total_score"]
    assert content["news"][0]["summary"]
    assert not content["news"][0]["summary"].startswith(article["title"] + ":")


def test_ai_response_cost_is_per_model():
    """cost_usd должен считаться по прайсу КОНКРЕТНОЙ модели (тех-долг T4),
    а не единой ставкой nano для всех."""
    tok = dict(input_tokens=1_000_000, output_tokens=1_000_000)
    flagship = AIResponse(data={}, model="gpt-5.5-2026-04-23", **tok)
    mini = AIResponse(data={}, model="gpt-5-mini-2025-08-07", **tok)
    nano = AIResponse(data={}, model="gpt-5-nano-2025-08-07", **tok)

    # Порядок стоимости отражает реальность: флагман дороже mini дороже nano.
    assert flagship.cost_usd > mini.cost_usd > nano.cost_usd
    # gpt-5.5 = (5 + 30) USD за 1М+1М токенов; nano = (0.05 + 0.40).
    assert abs(flagship.cost_usd - 35.0) < 1e-9
    assert abs(nano.cost_usd - 0.45) < 1e-9


def test_ai_response_cost_unknown_model_falls_back():
    """Неизвестная модель → откат на дефолтные ставки config (env-независимо), а не падение."""
    from oiltech_digest import config

    response = AIResponse(data={}, model="some-future-model", input_tokens=1_000_000, output_tokens=1_000_000)
    expected = config.OPENAI_INPUT_USD_PER_MTOK + config.OPENAI_OUTPUT_USD_PER_MTOK
    assert abs(response.cost_usd - expected) < 1e-9


def test_keyword_tag_falls_back_to_unclassified_not_first_tag():
    """Статья, не совпавшая ни с одним ключевым словом, не должна молча уезжать в первый тег.

    Раньше фоллбэк брал tags[0]: при таксономии D01–D18 это была «Сейсморазведка», при
    13 тематиках заказчика стала бы «Геологоразведка». Мусор копился в одном направлении
    и выглядел как обычная классификация. Заказчик 13.09 сам предложил приёмник.
    """
    from oiltech_digest.processing import pipeline

    tags = [
        {"id": 1, "name": "Геологоразведка", "keywords_json": ["сейсморазведка"], "keywords_en_json": []},
        {"id": 2, "name": "Бурение", "keywords_json": ["бурение"], "keywords_en_json": []},
        {"id": 99, "name": pipeline.UNCLASSIFIED_TAG_NAME, "keywords_json": [], "keywords_en_json": []},
    ]
    article = {"title": "Совершенно посторонний текст", "summary": "", "raw_text": "про котиков"}
    assert pipeline.keyword_tag(article, tags)["tag_id"] == 99

    # Совпадение по ключевому слову по-прежнему выигрывает у приёмника.
    drilling = {"title": "Новое бурение на кусте", "summary": "", "raw_text": "бурение"}
    assert pipeline.keyword_tag(drilling, tags)["tag_id"] == 2


def test_keyword_tag_keeps_old_behaviour_without_unclassified_tag():
    """Если приёмник не заведён, поведение прежнее — иначе упали бы старые установки."""
    from oiltech_digest.processing import pipeline

    tags = [
        {"id": 1, "name": "Первый", "keywords_json": [], "keywords_en_json": []},
        {"id": 2, "name": "Второй", "keywords_json": [], "keywords_en_json": []},
    ]
    article = {"title": "Ничего не совпадает", "summary": "", "raw_text": ""}
    assert pipeline.keyword_tag(article, tags)["tag_id"] == 1


def test_relevance_prompt_carries_customer_topics(monkeypatch):
    """Тематики заказчика доезжают до гейта.

    До 17.09 теги влияли только на классификацию уже отобранного: заказчик правил их
    на экране и выборка не менялась вообще. Он спрашивал об этом прямо 07.09.
    """
    pipeline._TAGS_SCOPE_CACHE.update({"block": None, "at": 0.0})
    monkeypatch.setattr(
        pipeline.repository, "list_enabled_tags",
        lambda: [{
            "id": 26, "name": "Автоматизация и промышленный AI", "name_en": "Industrial AI",
            "keywords_json": ["АСУ ТП", "телеметрия"], "keywords_en_json": ["SCADA"],
            "negative_keywords_json": ["смартфон"],
        }],
    )
    prompt = pipeline._relevance_prompt({"title": "t", "raw_text": "x"})
    assert "тематики заказчика:" in prompt
    assert "Автоматизация и промышленный AI" in prompt
    assert "SCADA" in prompt
    assert "минус: смартфон" in prompt


def test_relevance_prompt_survives_tag_read_failure(monkeypatch):
    """Сбой чтения тематик не роняет гейт — судим без них, как раньше."""
    pipeline._TAGS_SCOPE_CACHE.update({"block": None, "at": 0.0})

    def boom():
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(pipeline.repository, "list_enabled_tags", boom)
    prompt = pipeline._relevance_prompt({"title": "t", "raw_text": "x"})
    assert "title: t" in prompt
    assert "тематики заказчика" not in prompt


def test_relevance_prompt_prefers_passed_tags_over_database(monkeypatch):
    """Ключевое для прода: ИИ-стадия исполняется на зарубежном воркере, у которого
    БАЗЫ НЕТ. Теги обязаны приезжать параметром, иначе чтение падает в except и
    тематики молча не влияют ни на что именно в боевом режиме."""
    def must_not_be_called():
        raise AssertionError("гейт полез в базу вместо переданных тегов")

    monkeypatch.setattr(pipeline.repository, "list_enabled_tags", must_not_be_called)
    prompt = pipeline._relevance_prompt(
        {"title": "t", "raw_text": "x"},
        tags=[{"name": "Бурение", "keywords_json": ["направленное бурение"],
               "keywords_en_json": [], "negative_keywords_json": []}],
    )
    assert "тематики заказчика:" in prompt
    assert "Бурение" in prompt


def test_tags_scope_keeps_english_keywords_when_russian_list_is_long():
    """Общий срез ключей съедался русскими (их 33-52 на тематику) и до промпта не
    доезжал ни один английский — а гейт судит и англоязычные тексты."""
    from oiltech_digest.processing.prompts import tags_scope_block

    block = tags_scope_block([{
        "name": "Автоматизация", "name_en": "Industrial AI",
        "keywords_json": [f"ключ{i}" for i in range(40)],
        "keywords_en_json": ["SCADA", "digital twin"],
        "negative_keywords_json": [],
    }])
    assert "SCADA" in block
    assert "digital twin" in block


def test_tags_scope_excludes_catch_all_tag():
    """Служебный приёмник «Не классифицировано» — не тематика заказчика: по правилу
    2б попадание в тематику это довод ЗА, и приёмник делал бы доводом ЗА всё непонятое."""
    from oiltech_digest.processing.prompts import tags_scope_block

    block = tags_scope_block([
        {"name": "Не классифицировано / новая тема", "keywords_json": [],
         "keywords_en_json": [], "negative_keywords_json": []},
        {"name": "Бурение", "keywords_json": ["ГРП-флот"], "keywords_en_json": [],
         "negative_keywords_json": []},
    ])
    assert "Не классифицировано" not in block
    assert "Бурение" in block


def test_external_refetch_rejects_substituted_body(monkeypatch):
    """Страж принадлежности обязателен и на внешнем пути: воркер отдаёт то, что выдал
    сайт, а сайт умеет отдавать пейвол или листинг на любой адрес (задача №24)."""
    from oiltech_digest.ingestion import external_fetch

    stored: list = []
    monkeypatch.setattr(external_fetch.repository, "get_article",
                        lambda aid: {"id": aid, "title": "Совсем про другое", "source_id": 1})
    monkeypatch.setattr(external_fetch.repository, "update_article_full_text",
                        lambda *a, **k: stored.append((a, k)))
    monkeypatch.setattr(
        "oiltech_digest.ingestion.article_fetcher._ownership_rejection",
        lambda article, title, text: "title does not match body",
    )

    out = external_fetch.apply_refetch_text_result(
        {"kind": "refetch_text", "results": [{"id": 5, "status": "ok", "text": "чужой текст " * 50}]}
    )
    assert out["mismatched"] == 1
    assert out["applied"] == 0


def test_reprint_primary_falls_back_to_longer_copy():
    """Модель вернула чужой id — берём копию длиннее, а не первую попавшуюся.

    В случае заказчика от 08.09 копии были 1026, 2327, 2366 и 3181 знак, и
    короткая оказалась обрывком с дефектом склейки заголовка.
    """
    from oiltech_digest.processing import reprints

    left = {"id": 10, "raw_text": "к" * 1026}
    right = {"id": 20, "raw_text": "д" * 3181}
    assert reprints._resolve_primary(999, left, right) == 20
    assert reprints._resolve_primary(None, left, right) == 20
    assert reprints._resolve_primary(10, left, right) == 10, "валидный id модели уважаем"


def test_reprint_review_dry_run_writes_nothing(monkeypatch):
    """Сухой прогон ничего не помечает: схлопывание убирает материал из ленты."""
    from oiltech_digest.processing import reprints

    written: list = []
    monkeypatch.setattr(reprints.repository, "get_article",
                        lambda aid: {"id": aid, "title": "t", "raw_text": "текст" * 50})
    monkeypatch.setattr(reprints.repository, "mark_article_reprint",
                        lambda **kw: written.append(kw))

    class _Client:
        model = "test-model"

        def complete_json(self, *a, **k):
            from oiltech_digest.processing.openai_client import AIResponse
            return AIResponse(data={"same_event": True, "primary_id": 1, "reason": "одно испытание"},
                              model="test-model")

    out = reprints.review_candidates(
        [{"a_id": 1, "b_id": 2, "a_title": "A", "b_title": "B", "overlap": 0.5}],
        _Client(), dry_run=True)
    assert out["stats"]["reprints"] == 1
    assert written == [], "сухой прогон записал пометку"


def test_reprint_apply_rejects_foreign_primary_id():
    """Модель вернула id не из пары — дублем пометили бы не ту статью.

    Падаем на длину: короткая копия обычно и есть обрывок (в случае заказчика
    копия на 1026 знаков была склейкой заголовка с лидом).
    """
    from oiltech_digest.processing import external_ai

    written: list = []
    import oiltech_digest.db.repository as repo
    orig = repo.mark_article_reprint
    repo.mark_article_reprint = lambda **kw: written.append(kw)
    try:
        external_ai.apply_reprint_review_result({
            "reprint_review": True,
            "verdicts": [{"a_id": 10, "b_id": 20, "same_event": True,
                          "primary_id": 999, "a_len": 1026, "b_len": 3181,
                          "reason": "одно испытание", "overlap": 0.5}],
        })
    finally:
        repo.mark_article_reprint = orig
    assert len(written) == 1
    assert written[0]["primary_id"] == 20, "главной должна стать длинная копия"
    assert written[0]["article_id"] == 10


def test_reprint_candidates_band_is_passed_through(monkeypatch):
    """Полосу задаёт вызывающий, а не запрос.

    Замер на живом корпусе 17.09: из 318 пар за 14 дней тривиальных (100%)
    только 57, а полоса случая заказчика (35–49%) — 132 пары. Пока выборка
    резалась по `ORDER BY overlap DESC`, спорные пары до модели не доезжали,
    и судью нечем было проверить на том, ради чего он заведён.
    """
    from oiltech_digest.db import repository
    from oiltech_digest.processing import reprints

    seen = {}
    monkeypatch.setattr(repository, "reprint_candidates",
                        lambda **kw: seen.update(kw) or [])

    reprints.find_candidates(days=14, min_overlap=0.35, max_overlap=0.6, limit=40)

    assert seen["min_overlap"] == 0.35
    assert seen["max_overlap"] == 0.6, "верхняя граница полосы должна доезжать до запроса"


def test_reprint_candidate_order_is_not_by_overlap():
    """Порядок выборки не должен коррелировать с силой совпадения."""
    import inspect
    import re
    from oiltech_digest.db import repository

    # Комментарии выкидываем: объяснение, ПОЧЕМУ так нельзя, само содержит
    # запрещённую строку — на этом тест и споткнулся в первой редакции.
    sql = inspect.getsource(repository.reprint_candidates)
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    orders = re.findall(r"ORDER BY .*", code)

    assert orders, "в запросе должен быть явный порядок"
    assert not any("overlap" in o for o in orders), (
        f"срез по overlap отдаёт модели только тривиальные пары: {orders}"
    )
    assert any(o.startswith("ORDER BY md5(") for o in orders)


def test_find_reprints_waits_for_interval_since_last_applied_run(isolated_db, monkeypatch, capsys):
    """Срок — от прошлого прогона с записью в базе: перезапуск планировщика его не
    сбивает, а сухой прогон не считается."""
    import argparse

    from oiltech_digest import cli
    from oiltech_digest.db import repository
    from oiltech_digest.processing import reprints

    looked = []
    monkeypatch.setattr(reprints, "find_candidates", lambda **kwargs: looked.append(kwargs) or [])
    args = argparse.Namespace(min_overlap=0.35, max_overlap=1.0, days=7, limit=200, max_days_apart=5, show=0,
                              candidates_only=False, offline=False, apply=True, local=False, min_interval_hours=12)

    repository.create_background_job("reprint_review", {"pairs": [], "dry_run": True}, queue_name="external-ai")
    cli.cmd_find_reprints(args)
    assert len(looked) == 1  # сухой прогон не в счёт

    job = repository.create_background_job("reprint_review", {"pairs": [], "dry_run": False}, queue_name="external-ai")
    cli.cmd_find_reprints(args)
    assert len(looked) == 1 and "пропуск" in capsys.readouterr().out

    with repository.get_connection() as conn:
        conn.execute("UPDATE background_jobs SET created_at = now() - interval '13 hours' WHERE id = %s", (job["id"],))
        conn.commit()
    cli.cmd_find_reprints(args)
    assert len(looked) == 2  # прошло больше 12 часов — снова ищем
