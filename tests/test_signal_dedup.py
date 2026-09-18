"""Дедуп радара: одно событие — одна карточка (перенос механизма перепечаток MVP-1).

Данные — с прода 18.09: четыре карточки про покупку Velocity Geomatics компанией
ZenaTech, две — про завод сорбентов «Татнефти», пять обзоров лития из пластовых вод."""

import json

from oiltech_digest import signal_dedup, signal_discovery
from oiltech_digest.db import repository
from oiltech_digest.processing.openai_client import AIResponse

GEO = "Геологоразведка, сейсморазведка, геофизика и изучение недр"
MARKET = "Рынок, бизнес-модели, сервисные модели, партнёрства и M&A"


def _existing(signal_id, title, companies, *, verdict=None, fresh=True, score=60, urls=()):
    return {
        "kind": "existing", "id": signal_id, "reviewed": bool(verdict), "verdict": verdict,
        "fresh": fresh, "urls": list(urls),
        "signal": {"id": signal_id, "title_ru": title, "companies": companies, "score": score,
                   "evidence_count": len(urls) or 1, "signal_key": f"key-{signal_id}"},
    }


def _new(title, companies, *, score=60, key="k", order=1, urls=()):
    return {
        "kind": "new", "reviewed": False, "fresh": True, "order": order, "urls": list(urls),
        "signal": {"title_ru": title, "companies": companies, "score": score,
                   "evidence_count": len(urls) or 1, "signal_key": key},
    }


class _Judge:
    """Судья-заглушка: «одно событие», если обе карточки содержат маркер."""

    def __init__(self, marker, *, fail_on=None):
        self.marker = marker
        self.fail_on = fail_on
        self.prompts = []

    def complete_json(self, instructions, prompt, schema, max_output_tokens=900):
        assert schema["name"] == "signal_duplicate_verdict"
        self.prompts.append(prompt)
        if self.fail_on and self.fail_on in prompt:
            raise RuntimeError("OpenAI 500")
        card_a, card_b = prompt.split("Карточка B")
        same = self.marker in card_a and self.marker in card_b
        return AIResponse(data={"same_event": same, "reason": "одна сделка" if same else "разные факты"},
                          model="gpt-5-mini", input_tokens=500, output_tokens=40)


def test_pairs_found_by_title_or_company_but_never_between_two_reviewed_cards():
    nodes = [
        _existing(18, "Acquisition of Velocity Geomatics by ZenaTech", ["ZenaTech", "Velocity Geomatics (Velocity Group)"],
                  verdict="approved", fresh=False),
        _existing(19, "ZenaTech покупает Velocity Geomatics", ["ZenaTech"], verdict="reject", fresh=False),
        _existing(40, "Приобретение Velocity Geomatics компанией ZenaTech — DaaS", ["ZenaTech", "Velocity Geomatics"]),
        # Старые неразобранные между собой не сверяются: иначе пара оплачивалась бы каждый день.
        _existing(7, "Литий из пластовых вод: перспективы", [], fresh=False),
        _existing(8, "Перспективы лития из пластовых вод", [], fresh=False),
        # Издатель вместо компании — не общий участник.
        _existing(50, "Беспилотники над трубопроводом", ["bigchallenges.ru (источник)"]),
        _new("Дроны на буровой площадке", ["bigchallenges.ru (источник)"], key="drone"),
    ]
    pairs = {(nodes[i]["id"] if "id" in nodes[i] else "new", nodes[j].get("id", "new")) for i, j, _ in signal_dedup.find_pairs(nodes)}

    assert (18, 40) in pairs and (19, 40) in pairs
    assert (18, 19) not in pairs  # оба разобраны Виктором
    assert (7, 8) not in pairs  # оба старые и неразобранные
    assert (50, "new") not in pairs  # общий только издатель


def test_star_not_chain_and_reviewed_card_is_never_hidden():
    nodes = [
        _existing(18, "A", ["ZenaTech"], verdict="approved", fresh=False, score=30),
        _existing(40, "B", ["ZenaTech"], score=70),
        _existing(41, "C", ["ZenaTech"], score=60),
        _existing(26, "D", ["Татнефть"], verdict="reject", fresh=False),
        _new("E", ["Татнефть"], key="e"),
    ]
    edges = [(0, 1, "та же сделка"), (1, 2, "та же сделка"), (3, 4, "тот же завод"), (0, 3, "ошибка судьи")]

    assigned = signal_dedup.assign_duplicates(nodes, edges)

    # Одобренная — главная, даже с меньшим баллом; 41 связана с ней только через 40 — не склеена.
    assert assigned[1] == (0, "та же сделка")
    assert 2 not in assigned
    # Отклонённая Виктором остаётся на месте, свежий повтор уходит в неё.
    assert 3 not in assigned
    assert assigned[4] == (3, "тот же завод")


def test_judge_failure_leaves_pair_unmerged_and_run_goes_on():
    nodes = [
        _new("ZenaTech покупает Velocity Geomatics", ["ZenaTech"], key="a", order=1),
        _new("Velocity Geomatics куплена ZenaTech", ["ZenaTech"], key="b", order=2),
        _new("Татнефть строит завод сорбентов", ["Татнефть"], key="c", order=3),
        _new("Завод сорбентов Татнефти", ["Татнефть"], key="d", order=4),
    ]
    judge = _Judge("ZenaTech", fail_on="Татнефть строит")
    made = []

    result = signal_dedup.dedupe(nodes, client_factory=lambda: made.append(1) or judge)

    assert made == [1]
    assert result["stats"]["errors"] == 1 and result["stats"]["same"] == 1
    assert set(result["assigned"]) == {1}  # Татнефть без решения судьи не склеена
    assert result["stats"]["cost_usd"] > 0


def test_no_pairs_no_client():
    made = []
    result = signal_dedup.dedupe([_new("Одна карточка", ["X"])], client_factory=lambda: made.append(1))
    assert made == [] and result["stats"]["pairs"] == 0


def _candidate(title, companies, *, key, url, score=70, rejected=False):
    signal = signal_discovery._normalize_signal_payload(
        {"title": title, "title_ru": title, "summary": title, "maturity": "reject" if rejected else "shortlist",
         "score": score, "companies": companies},
        GEO,
    )
    signal.update({"signal_key": key, "evidence_count": 1,
                   "evidence": [{"source_url": url, "title": title}]})
    return signal


def test_worker_marks_repeats_of_existing_and_in_run_duplicates(monkeypatch):
    existing = [{"id": 18, "signal_key": "old", "title": "Acquisition of Velocity Geomatics by ZenaTech",
                 "title_ru": "Покупка Velocity Geomatics компанией ZenaTech", "theme": MARKET,
                 "summary": "", "companies": ["ZenaTech"], "score": 30.0, "evidence_count": 1,
                 "fresh": False, "verdict": "approved", "evidence_urls": ["https://old.example/zena"]}]
    snapshot = {"topics": [{"name": GEO}, {"name": MARKET}], "article_evidence": {}, "known_urls": [],
                "existing_signals": existing}
    by_topic = {
        GEO: [_candidate("ZenaTech покупает Velocity Geomatics", ["ZenaTech"], key="z1", url="https://a.example/1"),
              _candidate("Татнефть строит завод сорбентов лития", ["Татнефть"], key="t1", url="https://a.example/2")],
        MARKET: [_candidate("Завод сорбентов Татнефти для лития", ["Татнефть"], key="t2", url="https://b.example/3",
                            score=60)],
    }
    monkeypatch.setattr(signal_discovery, "_cluster_evidence", lambda evidence, topic: [[{"t": topic, "i": i}] for i in range(len(by_topic[topic]))])
    monkeypatch.setattr(signal_discovery, "_dedupe_evidence", lambda evidence: evidence)
    monkeypatch.setattr(signal_discovery, "_search_web_evidence", lambda topic, config: {"evidence": [{}], "status": "ok"})
    monkeypatch.setattr(signal_discovery, "judge_signal_snapshot",
                        lambda cluster, topic, offline=True: (dict(by_topic[topic][cluster[0]["i"]]), {}))
    monkeypatch.setattr(signal_discovery, "_signal_key", lambda signal, cluster: signal["signal_key"])
    judge = _Judge("ZenaTech")
    judge_tatneft = _Judge("сорбент")

    class _Both:
        def complete_json(self, *args, **kwargs):
            first = judge.complete_json(*args, **kwargs)
            return first if first.data["same_event"] else judge_tatneft.complete_json(*args, **kwargs)

    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: _Both())
    config = signal_discovery.SignalDiscoveryConfig(offline=False, web_only=True, dry_run=False)

    run = signal_discovery.run_discovery(config, snapshot)

    json.dumps(run)  # уходит ядру по HTTP
    geo, market = run["topics"]
    assert geo["candidates"][0]["duplicate_of"] == {"signal_id": 18}  # повтор одобренного
    assert "duplicate_of" not in geo["candidates"][1]  # главная Татнефти — выше балл
    assert market["candidates"][0]["duplicate_of"] == {"signal_key": "t1"}
    assert run["dedup"]["duplicates_new"] == 2 and run["dedup"]["existing_merges"] == []


def test_worker_offline_skips_dedup():
    config = signal_discovery.SignalDiscoveryConfig(offline=True, web_only=False)
    run = signal_discovery.run_discovery(config, {"topics": [], "existing_signals": []})
    assert run["dedup"] == {"skipped": "offline"}


def test_core_writes_duplicate_links_into_main_card_and_hides_old_repeats(monkeypatch):
    upserted, evidence, merged, touched, examples = [], [], [], [], []
    monkeypatch.setattr(repository, "upsert_signal", lambda signal: upserted.append(signal["signal_key"]) or 101)
    monkeypatch.setattr(repository, "upsert_signal_evidence", lambda sid, item: evidence.append((sid, item["source_url"])) or 1)
    monkeypatch.setattr(repository, "refresh_signal_evidence_count", lambda sid: 2)
    monkeypatch.setattr(repository, "resolve_signal_merge_root", lambda sid: sid)
    monkeypatch.setattr(repository, "touch_signal", lambda sid: touched.append(sid))
    monkeypatch.setattr(repository, "mark_signal_merged",
                        lambda sid, into, reason="": merged.append((sid, into, reason)) or True)
    monkeypatch.setattr(repository, "create_signal_training_example", lambda **kwargs: examples.append(kwargs) or 1)
    monkeypatch.setattr(repository, "signal_key_owners", lambda keys: {})
    main = _candidate("Татнефть строит завод сорбентов", ["Татнефть"], key="t1", url="https://a.example/2")
    repeat = _candidate("Завод сорбентов Татнефти", ["Татнефть"], key="t2", url="https://b.example/3")
    old = _candidate("ZenaTech покупает Velocity", ["ZenaTech"], key="z1", url="https://a.example/1")
    run = {
        "topics": [
            # Дубль стоит РАНЬШЕ своей главной карточки — порядок записи не должен это ломать.
            {"topic": MARKET, "candidates": [
                {"signal": repeat, "rejected": False, "duplicate_of": {"signal_key": "t1"}, "duplicate_reason": "тот же завод"},
                {"signal": old, "rejected": False, "duplicate_of": {"signal_id": 18}, "duplicate_reason": "та же сделка"},
            ]},
            {"topic": GEO, "candidates": [{"signal": main, "rejected": False}]},
        ],
        "dedup": {"pairs": 3, "judged": 3, "same": 2, "existing_merges": [{"signal_id": 41, "into": 40, "reason": "та же сделка"}]},
    }
    config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=False)

    result = signal_discovery.apply_discovery(config, run, generation_run_id=5)

    assert upserted == ["t1"]  # новой карточки у дублей нет
    assert (101, "https://b.example/3") in evidence and (18, "https://a.example/1") in evidence
    assert sorted(touched) == [18, 101]
    assert merged == [(41, 40, "та же сделка")]
    assert result["dedup"]["merged_new"] == 2 and result["dedup"]["merged_existing"] == 1
    assert result["all_signals"] == 1 and result["topic_results"][0]["duplicates"] == 2
    assert sorted(example["pipeline_verdict"] for example in examples) == ["accepted", "duplicate", "duplicate"]


def test_core_keeps_signal_when_main_card_is_gone(monkeypatch):
    upserted = []
    monkeypatch.setattr(repository, "upsert_signal", lambda signal: upserted.append(signal["signal_key"]) or 7)
    monkeypatch.setattr(repository, "upsert_signal_evidence", lambda sid, item: 1)
    monkeypatch.setattr(repository, "refresh_signal_evidence_count", lambda sid: 1)
    monkeypatch.setattr(repository, "resolve_signal_merge_root", lambda sid: None)
    monkeypatch.setattr(repository, "signal_key_owners", lambda keys: {})
    repeat = _candidate("ZenaTech покупает Velocity", ["ZenaTech"], key="z1", url="https://a.example/1")
    run = {"topics": [{"topic": GEO, "candidates": [
        {"signal": repeat, "rejected": False, "duplicate_of": {"signal_id": 999}}]}], "dedup": {"pairs": 1}}
    config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=False, persist_training_examples=False)

    result = signal_discovery.apply_discovery(config, run)

    assert upserted == ["z1"] and result["all_signals"] == 1


def _signal_row(key, title):
    return {"signal_key": key, "title": title, "title_ru": title, "theme": GEO, "maturity": "watch",
            "score": 60, "companies": ["ZenaTech"]}


def test_merge_hides_repeat_counts_it_and_spares_reviewed(isolated_db):
    main = repository.upsert_signal(_signal_row("main", "Покупка Velocity Geomatics"))
    repeat = repository.upsert_signal(_signal_row("repeat", "ZenaTech купила Velocity Geomatics"))
    reviewed = repository.upsert_signal(_signal_row("reviewed", "ZenaTech и Velocity"))
    later = repository.upsert_signal(_signal_row("later", "Сделка ZenaTech"))
    repository.upsert_signal_evidence(repeat, {"source_url": "https://a.example/1", "title": "t"})
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO signal_feedback_events (signal_id, event_type, verdict) VALUES (%s, 'verdict', 'approved')",
                     (reviewed,))
        conn.commit()

    assert repository.mark_signal_merged(repeat, main, "та же сделка") is True
    assert repository.mark_signal_merged(repeat, main) is False  # уже скрыта
    assert repository.mark_signal_merged(reviewed, main) is False  # вердикт Виктора — не прячем
    assert repository.mark_signal_merged(main, repeat) is False  # в свою же группу не уходит
    # Главная, указанная через скрытую карточку, — корень группы.
    assert repository.mark_signal_merged(later, repeat) is True
    assert repository.resolve_signal_merge_root(later) == main

    visible = {row["id"]: row for row in repository.list_signals(limit=10)}
    assert set(visible) == {main, reviewed}
    assert visible[main]["merged_count"] == 2

    snapshot = {row["id"]: row for row in repository.list_signals_for_dedup()}
    assert set(snapshot) == {main, reviewed}
    assert snapshot[reviewed]["verdict"] == "approved" and snapshot[main]["fresh"] is True
    json.dumps(snapshot, default=str)


# --- Регрессии ревью 18.09: повторная находка, ссылки скрытых дублей, чужая ссылка, дайджест ---


def _apply(run):
    config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=False, persist_training_examples=False)
    return signal_discovery.apply_discovery(config, run)


def _verdict(signal_id, verdict="approved"):
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO signal_feedback_events (signal_id, event_type, verdict) VALUES (%s, 'verdict', %s)",
                     (signal_id, verdict))
        conn.commit()


def _urls(signal_id):
    return {row["source_url"] for row in repository.list_signal_evidence(signal_id)}


def test_refound_link_of_hidden_card_goes_to_main_card_not_into_hidden_row(isolated_db):
    main = repository.upsert_signal(_signal_row("main", "Покупка Velocity Geomatics"))
    hidden = repository.upsert_signal(_signal_row("hidden", "ZenaTech купила Velocity"))
    repository.upsert_signal_evidence(hidden, {"source_url": "https://a.example/1", "title": "t"})
    assert repository.mark_signal_merged(hidden, main, "та же сделка")
    again = _candidate("ZenaTech купила Velocity Geomatics", ["ZenaTech"], key="hidden", url="https://a.example/9")

    # Результат без дедупа (старая сборка воркера, офлайн, судья ошибся) — ключ скрытой карточки.
    result = _apply({"topics": [{"topic": GEO, "candidates": [{"signal": again, "rejected": False}]}]})

    assert {row["id"] for row in repository.list_signals(limit=10)} == {main}
    assert {"https://a.example/1", "https://a.example/9"} <= _urls(main)
    assert result["all_signals"] == 0 and result["dedup"]["merged_new"] == 1


def test_hidden_duplicate_links_show_in_main_card_and_count_as_reviewed(isolated_db):
    main = repository.upsert_signal(_signal_row("main", "Покупка Velocity Geomatics"))
    dup = repository.upsert_signal(_signal_row("dup", "ZenaTech купила Velocity"))
    repository.upsert_signal_evidence(main, {"source_url": "https://m.example/1", "title": "t"})
    for url in ("https://a.example/1", "https://a.example/2"):
        repository.upsert_signal_evidence(dup, {"source_url": url, "title": "t"})
    assert repository.mark_signal_merged(dup, main)

    assert _urls(main) == {"https://m.example/1", "https://a.example/1", "https://a.example/2"}
    _verdict(main)
    assert {"https://a.example/1", "https://a.example/2"} <= set(repository.list_reviewed_signal_urls())


def test_duplicate_carrying_key_of_visible_card_hides_that_card_too(isolated_db):
    target = repository.upsert_signal(_signal_row("target", "Покупка Velocity Geomatics"))
    _verdict(target)
    owner = repository.upsert_signal(_signal_row("owner", "ZenaTech купила Velocity"))
    repository.upsert_signal_evidence(owner, {"source_url": "https://a.example/u", "title": "t"})
    again = _candidate("ZenaTech купила Velocity", ["ZenaTech"], key="owner", url="https://a.example/u")

    _apply({"topics": [{"topic": GEO, "candidates": [
        {"signal": again, "rejected": False, "duplicate_of": {"signal_id": target}, "duplicate_reason": "та же сделка"}]}],
        "dedup": {"pairs": 1}})

    # Карточка-владелец ссылки не остаётся видимой пустышкой: она тот же материал.
    assert {row["id"] for row in repository.list_signals(limit=10)} == {target}
    assert "https://a.example/u" in _urls(target)


def test_duplicate_carrying_key_of_reviewed_card_updates_that_card(isolated_db):
    target = repository.upsert_signal(_signal_row("target", "Покупка Velocity Geomatics"))
    owner = repository.upsert_signal(_signal_row("owner", "ZenaTech купила Velocity"))
    repository.upsert_signal_evidence(owner, {"source_url": "https://a.example/u", "title": "t"})
    _verdict(owner, "too_generic")
    again = _candidate("ZenaTech купила Velocity", ["ZenaTech"], key="owner", url="https://a.example/u")

    result = _apply({"topics": [{"topic": GEO, "candidates": [
        {"signal": again, "rejected": False, "duplicate_of": {"signal_id": target}}]}], "dedup": {"pairs": 1}})

    # Разобранная остаётся собой и при ссылке — судья не переспорит разбор.
    assert {row["id"] for row in repository.list_signals(limit=10)} == {target, owner}
    assert _urls(owner) == {"https://a.example/u"} and "https://a.example/u" not in _urls(target)
    assert result["all_signals"] == 1


def test_card_selected_for_digest_is_never_hidden(isolated_db):
    user = repository.create_user("digest-editor@example.com", "password123", role="admin")
    main = repository.upsert_signal(_signal_row("main", "Покупка Velocity Geomatics"))
    chosen = repository.upsert_signal(_signal_row("chosen", "ZenaTech купила Velocity"))
    repository.set_user_signal_status(int(user["id"]), chosen, status="digest")

    assert repository.mark_signal_merged(chosen, main) is False
    snapshot = {row["id"]: row for row in repository.list_signals_for_dedup()}
    assert snapshot[chosen]["reviewed"] is True and snapshot[main]["reviewed"] is False
