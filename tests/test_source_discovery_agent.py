from datetime import datetime, timedelta, timezone

from oiltech_digest.source_discovery import agent
from oiltech_digest.ingestion.source_diagnostics import ProbeResult
from oiltech_digest.processing.openai_client import AIResponse


def test_generate_search_queries_offline_uses_topic():
    queries = agent.generate_search_queries("роботизация бурения", offline=True, limit=3)

    assert len(queries) == 3
    assert all("роботизация бурения" in query for query in queries)


def test_generate_search_queries_reuses_successful_query_memory(monkeypatch):
    def fake_memory(**kwargs):
        if kwargs.get("status") == "muted":
            return []
        return [
            {
                "subject": "robotic drilling automation newsroom",
                "facts_json": {"topic": "роботизация бурения"},
            },
            {
                "subject": "unrelated query",
                "facts_json": {"topic": "другая тема"},
            },
        ]

    monkeypatch.setattr(agent.repository, "list_agent_memory", fake_memory)

    queries = agent.generate_search_queries("роботизация бурения", offline=True, limit=3)

    assert queries[0] == "robotic drilling automation newsroom"
    assert len(queries) == 3


def test_generate_search_queries_skips_muted_queries(monkeypatch):
    muted_query = "роботизация бурения нефтегаз новости"

    def fake_memory(**kwargs):
        if kwargs.get("status") == "muted":
            return [{"subject": muted_query, "facts_json": {"topic": "роботизация бурения"}}]
        return []

    monkeypatch.setattr(agent.repository, "list_agent_memory", fake_memory)

    queries = agent.generate_search_queries("роботизация бурения", offline=True, limit=3)

    assert muted_query not in queries
    assert len(queries) == 3


def test_generate_search_queries_uses_successful_topic_query_domain_memory(monkeypatch):
    def fake_memory(**kwargs):
        if kwargs.get("memory_type") == "topic_query_domain":
            return [{
                "subject": "роботизация бурения | autonomous drilling source | useful.example.com",
                "status": "active",
                "score": 40,
                "facts_json": {
                    "topic": "роботизация бурения",
                    "query": "autonomous drilling source",
                    "domain": "useful.example.com",
                },
            }]
        return []

    monkeypatch.setattr(agent.repository, "list_agent_memory", fake_memory)

    queries = agent.generate_search_queries("роботизация бурения", offline=True, limit=3)

    assert queries[0] == "autonomous drilling source"


def test_generate_search_queries_uses_strategy_specific_templates(monkeypatch):
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])

    queries = agent.generate_search_queries("роботизация бурения", offline=True, limit=2, strategy="technical")

    assert queries[0] == "роботизация бурения technology case study oilfield"
    assert queries[1] == "роботизация бурения technical paper upstream"


def test_generate_search_queries_uses_oilfield_meaning_for_grp(monkeypatch):
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])

    queries = agent.generate_search_queries("ГРП новые технологии", offline=True, limit=4)
    joined = " ".join(queries).lower()

    assert "hydraulic fracturing" in joined
    assert "гидроразрыв" in joined
    assert "gas distribution" not in joined
    assert "gas rising pressure" not in joined


def test_rank_search_results_prefers_fresh_hits(monkeypatch):
    monkeypatch.setattr(agent.repository, "normalize_domain", lambda url: "example.com")
    results = [
        {"url": "https://example.com/news/2023/old", "title": "Old drilling 2023", "query": "q"},
        {"url": "https://example.com/news", "title": "Fresh drilling 2026", "query": "q"},
    ]

    ranked = agent._rank_search_results(results, {})

    assert ranked[0]["title"] == "Fresh drilling 2026"


def test_score_source_candidate_rewards_relevance():
    strong = agent.score_source_candidate({
        "tested_articles": 10,
        "relevant_articles": 8,
        "avg_score": 75,
        "duplicate_count": 1,
        "noise_count": 1,
    })
    weak = agent.score_source_candidate({
        "tested_articles": 10,
        "relevant_articles": 1,
        "avg_score": 20,
        "duplicate_count": 3,
        "noise_count": 5,
    })

    assert strong["quality_score"] > weak["quality_score"]


def test_recommend_source_action_requires_human_review_without_test_articles():
    rec = agent.recommend_source_action({"tested_articles": 0}, offline=True)

    assert rec["recommended_action"] == "human_review"
    assert "провер" in rec["reason"].lower()


def test_discover_sources_dry_run_does_not_write(monkeypatch):
    calls = []

    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [{"topic": "t", "signals": 1, "target_signals": 10, "gap": 9}])
    monkeypatch.setattr(agent, "search_web", lambda queries, limit=20: {"status": "not_configured", "reason": "x", "queries": queries, "limit": limit, "results": []})
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "source_inventory_index", lambda: {"by_url": {}, "by_domain": {}})
    monkeypatch.setattr(agent.repository, "upsert_source_candidate", lambda rec: calls.append(rec) or 1)

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="роботизация бурения",
        seed_urls=("https://www.slb.com/newsroom",),
        offline=True,
        dry_run=True,
    ))

    assert result["dry_run"] is True
    assert result["candidates"][0]["url"] == "https://www.slb.com/newsroom"
    assert calls == []


def test_search_web_none_provider_is_explicit_noop(monkeypatch):
    monkeypatch.setattr(agent.app_config, "SOURCE_DISCOVERY_SEARCH_PROVIDER", "none")

    result = agent.search_web(["robotic drilling newsroom"], limit=5)

    assert result["status"] == "not_configured"
    assert result["provider"] == "none"
    assert result["results"] == []


def test_search_web_brave_maps_results(monkeypatch):
    monkeypatch.setattr(agent.app_config, "SOURCE_DISCOVERY_SEARCH_PROVIDER", "brave")
    monkeypatch.setattr(agent.app_config, "BRAVE_SEARCH_API_KEY", "token")

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {
                "web": {
                    "results": [
                        {"url": "https://example.com/news", "title": "News", "description": "Desc"},
                        {"url": "https://example.com/news", "title": "Duplicate", "description": "Desc"},
                    ]
                }
            }

    monkeypatch.setattr(agent.requests, "get", lambda *a, **k: Response())

    result = agent.search_web(["q"], limit=10)

    assert result["status"] == "ok"
    assert result["provider"] == "brave"
    assert result["results"] == [
        {
            "query": "q",
            "url": "https://example.com/news",
            "title": "News",
            "snippet": "Desc",
            "provider": "brave",
        }
    ]


def test_discover_sources_uses_search_results_as_candidates(monkeypatch):
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "source_inventory_index", lambda: {"by_url": {}, "by_domain": {}})
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "ok",
            "reason": "",
            "queries": queries,
            "limit": limit,
            "results": [{"url": "https://example.com/news", "query": "q"}],
        },
    )

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="роботизация бурения",
        offline=True,
        dry_run=True,
    ))

    assert result["candidates"][0]["url"] == "https://example.com/news"
    assert "Search result" in result["candidates"][0]["discovery_reason"]


def test_discover_sources_persists_query_memory(monkeypatch):
    memory = []
    actions = []
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "ok",
            "queries": queries,
            "limit": limit,
            "results": [{"url": "https://example.com/news", "query": "robotic drilling automation"}],
        },
    )
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "source_inventory_index", lambda: {"by_url": {}, "by_domain": {}})
    monkeypatch.setattr(agent.repository, "create_agent_task", lambda *args, **kwargs: 11)
    monkeypatch.setattr(agent.repository, "upsert_source_candidate", lambda rec: 22)
    monkeypatch.setattr(agent.repository, "record_agent_action", lambda *args, **kwargs: actions.append((args, kwargs)) or 1)
    monkeypatch.setattr(agent.repository, "upsert_agent_memory", lambda **kwargs: memory.append(kwargs) or 1)

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="роботизация бурения",
        offline=True,
        dry_run=False,
    ))

    assert result["candidates"][0]["id"] == 22
    query_memory = {item["subject"]: item for item in memory if item["memory_type"] == "query"}
    assert query_memory["robotic drilling automation"]["facts"]["topic"] == "роботизация бурения"
    assert query_memory["robotic drilling automation"]["facts"]["empty_result"] is False
    assert actions[-1][0][1] == "discover_sources_finished"


def test_persist_query_memory_ignores_urls_rejected_before_candidate(monkeypatch):
    memory = []
    monkeypatch.setattr(agent.repository, "upsert_agent_memory", lambda **kwargs: memory.append(kwargs) or 1)

    agent._persist_query_memory(
        "бурение",
        ["q"],
        "ok",
        [{"url": "https://old.example.com/news/2023/item", "query": "q"}],
        [],
    )

    assert memory[0]["status"] == "muted"
    assert memory[0]["facts"]["found_candidates"] == 0
    assert memory[0]["facts"]["empty_result"] is True


def test_discover_sources_mutes_empty_queries_after_successful_search(monkeypatch):
    memory = []
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "empty",
            "queries": queries,
            "limit": limit,
            "results": [],
        },
    )
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "create_agent_task", lambda *args, **kwargs: 11)
    monkeypatch.setattr(agent.repository, "record_agent_action", lambda *args, **kwargs: 1)
    monkeypatch.setattr(agent.repository, "upsert_agent_memory", lambda **kwargs: memory.append(kwargs) or 1)

    agent.discover_sources(agent.DiscoveryConfig(
        topic="роботизация бурения",
        offline=True,
        dry_run=False,
    ))

    assert memory
    assert {item["status"] for item in memory} == {"muted"}
    assert all(item["score"] == 0 for item in memory)
    assert all(item["facts"]["empty_result"] is True for item in memory)


def test_discover_sources_does_not_mute_queries_when_search_is_not_configured(monkeypatch):
    memory = []
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "not_configured",
            "queries": queries,
            "limit": limit,
            "results": [],
        },
    )
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "create_agent_task", lambda *args, **kwargs: 11)
    monkeypatch.setattr(agent.repository, "record_agent_action", lambda *args, **kwargs: 1)
    monkeypatch.setattr(agent.repository, "upsert_agent_memory", lambda **kwargs: memory.append(kwargs) or 1)

    agent.discover_sources(agent.DiscoveryConfig(
        topic="роботизация бурения",
        offline=True,
        dry_run=False,
    ))

    assert memory == []


def test_candidate_urls_skip_rejected_search_domains_but_keep_seed_urls():
    result, skipped, cooldown = agent._candidate_urls(
        ("https://bad.example.com/news",),
        [
            {"url": "https://bad.example.com/press", "query": "q"},
            {"url": "https://good.example.com/news", "query": "q"},
        ],
        10,
        rejected_domains={"bad.example.com"},
    )

    assert [item["url"] for item in result] == [
        "https://bad.example.com/news",
        "https://good.example.com/news",
    ]
    assert skipped == []
    assert cooldown == []


def test_candidate_urls_skip_existing_source_domains():
    result, skipped, cooldown = agent._candidate_urls(
        (),
        [
            {"url": "https://neftegaz.ru/news/", "query": "q"},
            {"url": "https://new.example.com/news", "query": "q"},
        ],
        10,
        source_inventory={
            "by_url": {},
            "by_domain": {
                "neftegaz.ru": {"id": 12, "name": "Neftegaz"},
            },
        },
    )

    assert [item["url"] for item in result] == ["https://new.example.com/news"]
    assert cooldown == []
    assert skipped == [{
        "url": "https://neftegaz.ru/news",
        "domain": "neftegaz.ru",
        "source_id": 12,
        "source_name": "Neftegaz",
        "reason": "already_exists_in_sources",
    }]


def test_candidate_urls_normalizes_url_keys_and_keeps_single_candidate():
    result, skipped, cooldown = agent._candidate_urls(
        ("example.com/news/#section", "https://EXAMPLE.com/news/"),
        [],
        10,
    )

    assert skipped == []
    assert cooldown == []
    assert result[0]["url"] == "https://example.com/news"
    assert result[0]["reason"] == "Seed URL supplied by operator"
    assert result[0]["query"] == ""


def test_candidate_urls_matches_existing_source_without_www():
    result, skipped, cooldown = agent._candidate_urls(
        ("https://www.slb.com/news-and-insights",),
        [],
        10,
        source_inventory={
            "by_url": {},
            "by_domain": {"slb.com": {"id": 22, "name": "SLB"}},
        },
    )

    assert result == []
    assert cooldown == []
    assert skipped == [{
        "url": "https://www.slb.com/news-and-insights",
        "domain": "slb.com",
        "source_id": 22,
        "source_name": "SLB",
        "reason": "already_exists_in_sources",
    }]


def test_candidate_urls_skip_temporary_unavailable_domains():
    result, skipped, cooldown = agent._candidate_urls(
        (),
        [
            {"url": "https://flaky.example.com/news", "query": "q"},
            {"url": "https://good.example.com/news", "query": "q"},
        ],
        10,
        cooldown_domains={
            "flaky.example.com": {
                "retry_after": "2026-08-24T10:00:00+00:00",
                "failure_count": 2,
                "last_reason": "http_502",
            }
        },
    )

    assert [item["url"] for item in result] == ["https://good.example.com/news"]
    assert skipped == []
    assert cooldown == [{
        "url": "https://flaky.example.com/news",
        "domain": "flaky.example.com",
        "reason": "temporary_unavailable_cooldown",
        "retry_after": "2026-08-24T10:00:00+00:00",
        "failure_count": 2,
        "last_reason": "http_502",
    }]


def test_candidate_urls_keep_seed_urls_even_when_domain_is_on_cooldown():
    result, skipped, cooldown = agent._candidate_urls(
        ("https://flaky.example.com/news",),
        [{"url": "https://flaky.example.com/press", "query": "q"}],
        10,
        cooldown_domains={
            "flaky.example.com": {
                "retry_after": "2026-08-24T10:00:00+00:00",
                "failure_count": 2,
                "last_reason": "http_502",
            }
        },
    )

    assert [item["url"] for item in result] == ["https://flaky.example.com/news"]
    assert skipped == []
    assert cooldown == [{
        "url": "https://flaky.example.com/press",
        "domain": "flaky.example.com",
        "reason": "temporary_unavailable_cooldown",
        "retry_after": "2026-08-24T10:00:00+00:00",
        "failure_count": 2,
        "last_reason": "http_502",
    }]


def test_candidate_urls_skip_bad_topic_query_domain_combo():
    result, skipped, cooldown = agent._candidate_urls(
        (),
        [
            {"url": "https://bad.example.com/news", "query": "bad drilling query"},
            {"url": "https://good.example.com/news", "query": "bad drilling query"},
        ],
        10,
        learning_policy={
            "blocked_combo_keys": {agent._combo_policy_key("bad drilling query", "bad.example.com")},
            "promoted_combo_keys": set(),
            "muted_queries": set(),
        },
    )

    assert [item["url"] for item in result] == ["https://good.example.com/news"]
    assert skipped == []
    assert cooldown == []


def test_rank_search_results_boosts_successful_combo_and_skips_bad_combo():
    result = agent._rank_search_results(
        [
            {"url": "https://neutral.example.com/news", "query": "drilling news"},
            {"url": "https://bad.example.com/news", "query": "bad query"},
            {"url": "https://useful.example.com/news", "query": "good query"},
        ],
        {
            "promoted_combo_keys": {agent._combo_policy_key("good query", "useful.example.com")},
            "blocked_combo_keys": {agent._combo_policy_key("bad query", "bad.example.com")},
            "muted_queries": set(),
            "query_scores": {"good query": 20},
            "domain_scores": {"useful.example.com": 30},
        },
    )

    assert [item["url"] for item in result] == [
        "https://useful.example.com/news",
        "https://neutral.example.com/news",
    ]


def test_discover_sources_with_seed_urls_skips_web_search(monkeypatch):
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "source_inventory_index", lambda: {"by_url": {}, "by_domain": {}})
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(agent, "search_web", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("search must be skipped")))
    monkeypatch.setattr(
        agent,
        "inspect_source",
        lambda url, fetch=False: {
            "url": url,
            "domain": agent.repository.normalize_domain(url),
            "name": "Seed",
            "candidate_type": "media",
            "confidence": 0.55,
            "fetch_checked": fetch,
            "probe": {"status": 200, "error": None},
        },
    )
    monkeypatch.setattr(
        agent,
        "test_parse_source",
        lambda url, article_limit=5, **kwargs: {
            "url": url,
            "verdict": "ok",
            "metrics": {
                "tested_articles": 2,
                "relevant_articles": 1,
                "avg_score": 70,
                "duplicate_count": 0,
                "noise_count": 0,
            },
        },
    )

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="бурение",
        seed_urls=("https://seed.example.com/news",),
        offline=True,
        dry_run=True,
        fetch_inspection=True,
        test_parse=True,
    ))

    assert result["search"]["status"] == "seed_only"
    assert [item["url"] for item in result["candidates"]] == ["https://seed.example.com/news"]


def test_discover_sources_skips_unavailable_candidates_when_fetching(monkeypatch):
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "source_inventory_index", lambda: {"by_url": {}, "by_domain": {}})
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "ok",
            "queries": queries,
            "limit": limit,
            "results": [
                {"url": "https://bad.example.com/news", "query": "q"},
                {"url": "https://good.example.com/news", "query": "q"},
            ],
        },
    )
    monkeypatch.setattr(
        agent,
        "inspect_source",
        lambda url, fetch=False: {
            "url": url,
            "domain": agent.repository.normalize_domain(url),
            "name": "Example",
            "candidate_type": "media",
            "confidence": 0.55,
            "fetch_checked": fetch,
            "probe": {"status": 502 if "bad" in url else 200, "error": None},
        },
    )

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="бурение",
        offline=True,
        dry_run=True,
        fetch_inspection=True,
    ))

    assert [item["url"] for item in result["candidates"]] == ["https://good.example.com/news"]
    assert result["unavailable_sources_skipped"][0]["reason"] == "http_502"


def test_discover_sources_records_temporary_unavailable_memory(monkeypatch):
    memory = []
    actions = []
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "source_inventory_index", lambda: {"by_url": {}, "by_domain": {}})
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(agent.repository, "create_agent_task", lambda *args, **kwargs: 11)
    monkeypatch.setattr(agent.repository, "record_agent_action", lambda *args, **kwargs: actions.append((args, kwargs)) or 1)
    monkeypatch.setattr(agent.repository, "upsert_agent_memory", lambda **kwargs: memory.append(kwargs) or 1)
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "ok",
            "queries": queries,
            "limit": limit,
            "results": [{"url": "https://flaky.example.com/news", "query": "q"}],
        },
    )
    monkeypatch.setattr(
        agent,
        "inspect_source",
        lambda url, fetch=False: {
            "url": url,
            "domain": agent.repository.normalize_domain(url),
            "name": "Flaky",
            "candidate_type": "media",
            "confidence": 0.55,
            "fetch_checked": fetch,
            "probe": {"status": 502, "error": None},
        },
    )

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="бурение",
        offline=True,
        dry_run=False,
        fetch_inspection=True,
    ))

    assert result["candidates"] == []
    assert memory[0]["memory_key"] == "domain:flaky.example.com"
    assert memory[0]["status"] == "temporary_unavailable"
    assert memory[0]["facts"]["failure_count"] == 1
    assert memory[0]["facts"]["last_reason"] == "http_502"
    assert memory[0]["facts"]["retry_after"]


def test_record_unavailable_domain_rejects_after_threshold(monkeypatch):
    memory = []

    def fake_memory(**kwargs):
        return [{
            "subject": "flaky.example.com",
            "facts_json": {"failure_count": agent.TEMPORARY_UNAVAILABLE_REJECT_AFTER - 1},
        }]

    monkeypatch.setattr(agent.repository, "list_agent_memory", fake_memory)
    monkeypatch.setattr(agent.repository, "upsert_agent_memory", lambda **kwargs: memory.append(kwargs) or 1)

    agent._record_unavailable_domain(
        "https://flaky.example.com/news",
        "fetch_failed: timeout",
        {"probe": {"status": None, "error": "timeout"}},
    )

    assert memory[0]["status"] == "rejected"
    assert memory[0]["facts"]["failure_count"] == agent.TEMPORARY_UNAVAILABLE_REJECT_AFTER


def test_temporary_unavailable_domains_honors_retry_after(monkeypatch):
    monkeypatch.setattr(
        agent.repository,
        "list_agent_memory",
        lambda **kwargs: [
            {
                "subject": "wait.example.com",
                "facts_json": {
                    "retry_after": "2026-08-24T10:00:00+00:00",
                    "failure_count": 1,
                    "last_reason": "http_502",
                },
            },
            {
                "subject": "expired.example.com",
                "facts_json": {
                    "retry_after": "2026-08-22T10:00:00+00:00",
                    "failure_count": 1,
                    "last_reason": "http_502",
                },
            },
        ],
    )

    result = agent._temporary_unavailable_domains(datetime(2026, 8, 23, tzinfo=timezone.utc))

    assert set(result) == {"wait.example.com"}
    assert result["wait.example.com"]["failure_count"] == 1


def test_discover_sources_skips_candidates_without_parseable_articles(monkeypatch):
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "source_inventory_index", lambda: {"by_url": {}, "by_domain": {}})
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "ok",
            "queries": queries,
            "limit": limit,
            "results": [
                {"url": "https://empty.example.com/news", "query": "q"},
                {"url": "https://useful.example.com/news", "query": "q"},
            ],
        },
    )
    monkeypatch.setattr(
        agent,
        "inspect_source",
        lambda url, fetch=False: {
            "url": url,
            "domain": agent.repository.normalize_domain(url),
            "name": "Example",
            "candidate_type": "media",
            "confidence": 0.55,
            "fetch_checked": fetch,
            "probe": {"status": 200, "error": None},
        },
    )
    monkeypatch.setattr(
        agent,
        "test_parse_source",
        lambda url, article_limit=5, **kwargs: {
            "url": url,
            "verdict": "no_candidates" if "empty" in url else "ok",
            "metrics": {
                "tested_articles": 0 if "empty" in url else 2,
                "relevant_articles": 0 if "empty" in url else 1,
                "avg_score": None if "empty" in url else 70,
                "duplicate_count": 0,
                "noise_count": 0,
            },
        },
    )

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="бурение",
        offline=True,
        dry_run=True,
        fetch_inspection=True,
        test_parse=True,
    ))

    assert [item["url"] for item in result["candidates"]] == ["https://useful.example.com/news"]
    assert result["parse_failed_sources_skipped"][0]["reason"] == "no_candidates"


def test_discover_sources_hides_rejected_recommendations_from_candidates(monkeypatch):
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "source_inventory_index", lambda: {"by_url": {}, "by_domain": {}})
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "ok",
            "queries": queries,
            "limit": limit,
            "results": [{"url": "https://noisy.example.com/news", "query": "q"}],
        },
    )
    monkeypatch.setattr(
        agent,
        "inspect_source",
        lambda url, fetch=False: {
            "url": url,
            "domain": agent.repository.normalize_domain(url),
            "name": "Noisy",
            "candidate_type": "media",
            "confidence": 0.55,
            "fetch_checked": fetch,
            "probe": {"status": 200, "error": None},
        },
    )
    monkeypatch.setattr(
        agent,
        "test_parse_source",
        lambda url, article_limit=5, **kwargs: {
            "url": url,
            "verdict": "ok",
            "metrics": {
                "tested_articles": 5,
                "relevant_articles": 1,
                "avg_score": 30,
                "duplicate_count": 0,
                "noise_count": 4,
            },
            "candidates": [],
        },
    )
    monkeypatch.setattr(agent, "recommend_source_action", lambda *args, **kwargs: {"recommended_action": "reject", "reason": "too noisy"})

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="бурение",
        offline=True,
        dry_run=True,
        fetch_inspection=True,
        test_parse=True,
    ))

    assert result["candidates"] == []
    assert result["parse_failed_sources_skipped"][0]["reason"] == "recommendation_reject"


def test_url_quality_gate_skips_bad_source_entrypoints():
    assert agent._url_quality_gate_reason("https://example.com/files/report.pdf") == "bad_url_type:document"
    assert agent._url_quality_gate_reason("https://example.com/tag/drilling") == "bad_url_type:index_noise"
    assert agent._url_quality_gate_reason("https://example.com/search?q=drilling") == "bad_url_type:index_noise"
    assert agent._url_quality_gate_reason("https://example.com/category/drilling") == "bad_url_type:index_noise"
    assert agent._url_quality_gate_reason("https://example.com/topics/drilling") == "bad_url_type:index_noise"
    assert agent._url_quality_gate_reason("https://example.com/reports/global-robotic-drilling-market") == "bad_url_type:market_report"
    assert agent._url_quality_gate_reason("https://example.com/news/2026/robotic-drilling-system-improves-oilfield-operations") == "single_article_url"
    assert agent._url_quality_gate_reason("https://example.com/news/very-long-one-off-story-about-drilling-robot-for-data-center.html") == "single_article_url"
    assert agent._url_quality_gate_reason("https://example.com/newsitem/117091") == "single_article_url"
    assert agent._url_quality_gate_reason("https://example.com/news") is None
    assert agent._url_quality_gate_reason("https://example.com/newsroom") is None


def test_search_result_quality_gate_rejects_stale_hits(monkeypatch):
    monkeypatch.setattr(agent.app_config, "SOURCE_DISCOVERY_STALE_RESULT_YEAR_GRACE", 1)
    now = datetime(2026, 9, 9, tzinfo=timezone.utc)

    assert agent._search_result_quality_gate_reason(
        {"url": "https://example.com/news/2023/robotic-drilling", "title": "Robotic drilling", "snippet": ""},
        now=now,
    ) == "stale_search_result:url_year_2023"
    assert agent._search_result_quality_gate_reason(
        {"url": "https://example.com/news", "title": "Best drilling news 2024", "snippet": "Archive 2024"},
        now=now,
    ) == "stale_search_result:metadata_year_2024"
    assert agent._search_result_quality_gate_reason(
        {"url": "https://example.com/news", "title": "Best drilling news 2026", "snippet": "Archive 2024"},
        now=now,
    ) is None


def test_content_quality_gate_detects_semantic_404_and_antibot():
    assert agent._content_quality_gate_reason("<html><title>Page not found</title><body>This page does not exist.</body></html>") == "semantic_404"
    assert agent._content_quality_gate_reason("<html><body>Checking your browser before accessing. Cloudflare cf-challenge</body></html>") == "anti_bot"
    assert agent._content_quality_gate_reason("<html><body>Новости компании и пресс-релизы по бурению</body></html>") is None


def test_discover_sources_skips_quality_gate_candidates(monkeypatch):
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "source_inventory_index", lambda: {"by_url": {}, "by_domain": {}})
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "ok",
            "queries": queries,
            "limit": limit,
            "results": [
                {"url": "https://example.com/tag/drilling", "query": "q"},
                {"url": "https://blocked.example.com/news", "query": "q"},
                {"url": "https://good.example.com/news", "query": "q"},
            ],
        },
    )

    def fake_inspect(url, fetch=False):
        return {
            "url": url,
            "domain": agent.repository.normalize_domain(url),
            "name": "Example",
            "candidate_type": "media",
            "confidence": 0.55,
            "fetch_checked": fetch,
            "probe": {"status": 200, "error": None},
            **({"quality_gate_reason": "anti_bot"} if "blocked" in url else {}),
        }

    monkeypatch.setattr(agent, "inspect_source", fake_inspect)

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="бурение",
        offline=True,
        dry_run=True,
        fetch_inspection=True,
    ))

    assert [item["url"] for item in result["candidates"]] == ["https://good.example.com/news"]
    assert [item["reason"] for item in result["quality_gate_sources_skipped"]] == ["bad_url_type:index_noise", "anti_bot"]


def test_discover_sources_skips_stale_search_results(monkeypatch):
    monkeypatch.setattr(agent.app_config, "SOURCE_DISCOVERY_STALE_RESULT_YEAR_GRACE", 1)
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "source_inventory_index", lambda: {"by_url": {}, "by_domain": {}})
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "ok",
            "queries": queries,
            "limit": limit,
            "results": [
                {"url": "https://old.example.com/news/2023/robotic-drilling", "query": "q", "title": "Old news"},
                {"url": "https://good.example.com/news", "query": "q", "title": "Fresh news 2026"},
            ],
        },
    )
    monkeypatch.setattr(
        agent,
        "inspect_source",
        lambda url, fetch=False: {
            "url": url,
            "domain": agent.repository.normalize_domain(url),
            "name": "Example",
            "candidate_type": "media",
            "confidence": 0.55,
            "fetch_checked": fetch,
            "probe": {"status": 200, "error": None},
        },
    )

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="бурение",
        offline=True,
        dry_run=True,
        fetch_inspection=True,
    ))

    assert [item["url"] for item in result["candidates"]] == ["https://good.example.com/news"]
    assert result["quality_gate_sources_skipped"][0]["reason"] == "stale_search_result:url_year_2023"


def test_discover_sources_looks_past_initial_rejected_search_results(monkeypatch):
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "source_inventory_index", lambda: {"by_url": {}, "by_domain": {}})
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "ok",
            "queries": queries,
            "limit": limit,
            "results": [
                {"url": "https://bad1.example.com/news/2023/old-robotic-drilling-story", "query": "q"},
                {"url": "https://bad2.example.com/news/2023/old-robotic-drilling-story", "query": "q"},
                {"url": "https://good.example.com/news", "query": "q"},
            ],
        },
    )
    monkeypatch.setattr(
        agent,
        "inspect_source",
        lambda url, fetch=False: {
            "url": url,
            "domain": agent.repository.normalize_domain(url),
            "name": "Example",
            "candidate_type": "media",
            "confidence": 0.55,
            "fetch_checked": fetch,
            "probe": {"status": 200, "error": None},
        },
    )

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="бурение",
        limit=1,
        offline=True,
        dry_run=True,
        fetch_inspection=True,
    ))

    assert [item["url"] for item in result["candidates"]] == ["https://good.example.com/news"]


def test_test_parse_source_scores_useful_candidate(monkeypatch):
    listing = b"""
    <html><body>
      <a href="/news/drilling-robot">Robotic drilling system improves oilfield operations</a>
    </body></html>
    """
    article = b"""
    <html><head><meta property="og:title" content="Robotic drilling system improves oilfield operations"></head>
    <body><article>
      <p>The company deployed a robotic drilling system for oilfield well construction.</p>
      <p>The technology improves uptime, safety and operational control for upstream teams.</p>
    </article></body></html>
    """

    def fake_probe(url, timeout=20):
        if url == "https://example.com/news":
            return ProbeResult(url=url, status=200, bytes=len(listing)), listing
        return ProbeResult(url=url, status=200, bytes=len(article)), article

    monkeypatch.setattr(agent, "probe_url", fake_probe)

    result = agent.test_parse_source("https://example.com/news", article_limit=3)

    assert result["verdict"] == "ok"
    assert result["metrics"]["tested_articles"] == 1
    assert result["metrics"]["relevant_articles"] == 1
    assert result["metrics"]["avg_score"] is not None


def test_test_parse_source_rejects_sources_with_only_stale_articles(monkeypatch):
    now = datetime.now(timezone.utc)
    stale_date = now - timedelta(days=400)
    listing = b"<html><body><a href='/news/old'>Old robotic drilling news</a></body></html>"
    article = b"""
    <html><head><meta property="og:title" content="Old robotic drilling news"></head>
    <body><article>
      <time datetime="2025-01-01T10:00:00+00:00">2025</time>
      <p>The company deployed a robotic drilling system for oilfield well construction.</p>
      <p>The technology improves uptime, safety and operational control for upstream teams.</p>
    </article></body></html>
    """

    monkeypatch.setattr(agent.app_config, "SOURCE_DISCOVERY_FRESHNESS_DAYS", 180)
    monkeypatch.setattr(
        agent.request_parser,
        "extract_candidate_links",
        lambda *args, **kwargs: [
            agent.request_parser.CandidateLink("https://example.com/news/old", "Old robotic drilling news", 5, stale_date)
        ],
    )
    monkeypatch.setattr(
        agent.request_parser,
        "parse_article_page",
        lambda content, fallback_title="": ("Old robotic drilling news", stale_date, "robotic drilling oilfield technology " * 20),
    )

    def fake_probe(url, timeout=20):
        if url == "https://example.com/news":
            return ProbeResult(url=url, status=200, bytes=len(listing)), listing
        return ProbeResult(url=url, status=200, bytes=len(article)), article

    monkeypatch.setattr(agent, "probe_url", fake_probe)

    result = agent.test_parse_source("https://example.com/news", article_limit=3)

    assert result["verdict"] == "stale_articles"
    assert result["metrics"]["relevant_articles"] == 0
    assert result["metrics"]["noise_count"] == 1
    assert result["candidates"][0]["verdict"] == "stale_article"


def test_test_parse_source_rejects_generic_construction_drilling_for_oilfield_topic(monkeypatch):
    listing = b"<html><body><a href='/news/robot-drilling'>Robot drilling for data centers</a></body></html>"
    article = b"""
    <html><head><meta property="og:title" content="Robot drilling for data centers"></head>
    <body><article>
      <p>DEWALT launched a downward drilling robot for concrete holes at data center construction sites.</p>
      <p>The robot improves construction productivity and coordinates multiple drills on large building projects.</p>
    </article></body></html>
    """

    def fake_probe(url, timeout=20):
        if url == "https://example.com/news":
            return ProbeResult(url=url, status=200, bytes=len(listing)), listing
        return ProbeResult(url=url, status=200, bytes=len(article)), article

    monkeypatch.setattr(agent, "probe_url", fake_probe)

    result = agent.test_parse_source("https://example.com/news", article_limit=3, topic="роботизация бурения")

    assert result["verdict"] == "no_useful_articles"
    assert result["metrics"]["relevant_articles"] == 0
    assert result["candidates"][0]["topic_gate_reason"] == "topic_mismatch:oilfield_drilling_context_required"


def test_test_parse_source_keeps_oilfield_robotic_drilling_for_topic(monkeypatch):
    listing = b"<html><body><a href='/news/robotic-rig'>Robotic drilling rig improves well construction</a></body></html>"
    article = b"""
    <html><head><meta property="og:title" content="Robotic drilling rig improves well construction"></head>
    <body><article>
      <p>The oilfield operator deployed an autonomous drilling rig for upstream well construction.</p>
      <p>The robotic system improves safety, wellbore quality and drilling performance.</p>
    </article></body></html>
    """

    def fake_probe(url, timeout=20):
        if url == "https://example.com/news":
            return ProbeResult(url=url, status=200, bytes=len(listing)), listing
        return ProbeResult(url=url, status=200, bytes=len(article)), article

    monkeypatch.setattr(agent, "probe_url", fake_probe)

    result = agent.test_parse_source("https://example.com/news", article_limit=3, topic="роботизация бурения")

    assert result["verdict"] == "ok"
    assert result["metrics"]["relevant_articles"] == 1


def test_test_source_candidate_updates_assessment(monkeypatch):
    updates = []
    actions = []

    monkeypatch.setattr(
        agent.repository,
        "get_source_candidate",
        lambda candidate_id: {"id": candidate_id, "url": "https://example.com/news"},
    )
    monkeypatch.setattr(
        agent,
        "test_parse_source",
        lambda url, article_limit=5: {
            "url": url,
            "verdict": "ok",
            "metrics": {
                "tested_articles": 6,
                "relevant_articles": 5,
                "avg_score": 75,
                "duplicate_count": 0,
                "noise_count": 1,
            },
            "candidates": [],
        },
    )
    monkeypatch.setattr(
        agent.repository,
        "update_source_candidate_assessment",
        lambda candidate_id, **kwargs: updates.append({"candidate_id": candidate_id, **kwargs}),
    )
    monkeypatch.setattr(
        agent.repository,
        "record_agent_action",
        lambda task_id, action_type, **kwargs: actions.append({"task_id": task_id, "action_type": action_type, **kwargs}),
    )

    result = agent.test_source_candidate(42, article_limit=6, offline=True, dry_run=False)

    assert result["recommended_action"] == "add"
    assert result["next_status"] == "needs_human_review"
    assert updates == [{
        "candidate_id": 42,
        "status": "needs_human_review",
        "tested_articles": 6,
        "relevant_articles": 5,
        "avg_score": 75,
        "duplicate_count": 0,
        "noise_count": 1,
        "recommended_action": "add",
        "review_comment": "Источник дал достаточно релевантных материалов и выглядит полезным.",
    }]
    assert actions[0]["action_type"] == "test_source_candidate"


def test_recommend_source_action_uses_ai_evidence(monkeypatch):
    calls = []

    class Client:
        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            calls.append({
                "instructions": instructions,
                "user_input": user_input,
                "schema": schema,
                "max_output_tokens": max_output_tokens,
            })
            return AIResponse(
                data={
                    "recommended_action": "add",
                    "reason": "Источник дает релевантные материалы по бурению.",
                    "strengths": ["Есть статьи с высоким score", "Тематика совпадает"],
                    "risks": ["Выборка пока небольшая"],
                    "confidence": 0.82,
                },
                model="fake-ai",
            )

    monkeypatch.setattr(agent, "make_client", lambda offline: Client())

    result = agent.recommend_source_action(
        {
            "tested_articles": 5,
            "relevant_articles": 4,
            "avg_score": 72,
            "duplicate_count": 0,
            "noise_count": 1,
        },
        offline=False,
        evidence=[
            {
                "title": "Robotic drilling system",
                "summary": "Компания запустила роботизированную буровую.",
                "relevant": True,
                "total_score": 80,
                "score_label": "Высокая",
            }
        ],
    )

    assert result["recommended_action"] == "add"
    assert result["source"] == "ai"
    assert result["model"] == "fake-ai"
    assert result["confidence"] == 0.82
    assert "Сильные стороны" in result["reason"]
    assert "Риски" in result["reason"]
    assert "Robotic drilling system" in calls[0]["user_input"]
    assert calls[0]["schema"]["name"] == "source_candidate_recommendation"


def test_test_source_candidate_dry_run_does_not_update(monkeypatch):
    updates = []

    monkeypatch.setattr(
        agent.repository,
        "get_source_candidate",
        lambda candidate_id: {"id": candidate_id, "url": "https://example.com/news"},
    )
    monkeypatch.setattr(
        agent,
        "test_parse_source",
        lambda url, article_limit=5: {
            "url": url,
            "verdict": "no_useful_articles",
            "metrics": {
                "tested_articles": 5,
                "relevant_articles": 0,
                "avg_score": None,
                "duplicate_count": 0,
                "noise_count": 5,
            },
            "candidates": [],
        },
    )
    monkeypatch.setattr(
        agent.repository,
        "update_source_candidate_assessment",
        lambda candidate_id, **kwargs: updates.append(kwargs),
    )

    result = agent.test_source_candidate(42, offline=True, dry_run=True)

    assert result["recommended_action"] == "reject"
    assert result["next_status"] == "rejected"
    assert updates == []


def test_single_4xx_does_not_reject_domain_forever(monkeypatch):
    memory = []
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "upsert_agent_memory", lambda **kwargs: memory.append(kwargs) or 1)

    for reason in ("http_403", "http_404", "http_451"):
        agent._record_unavailable_domain(f"https://site.example/{reason}", reason, {"probe": {"status": int(reason[5:])}})

    # 404 — мёртвая ссылка, 403/451 — блок адреса ядра: домен остаётся в поиске после отсрочки.
    assert [row["status"] for row in memory] == ["temporary_unavailable"] * 3
