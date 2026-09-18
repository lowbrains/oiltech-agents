"""Controlled source discovery agent tools.

MVP design: the agent can plan, generate queries, inspect seed URLs and write
source candidates. It does not autonomously crawl the web or activate sources.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from oiltech_digest import config as app_config
from oiltech_digest.db import repository
from oiltech_digest.ingestion import request_parser
from oiltech_digest.ingestion.relevance_filter import should_keep_article
from oiltech_digest.ingestion.source_diagnostics import probe_url
from oiltech_digest.processing.pipeline import make_client
from oiltech_digest.source_discovery.prompts import (
    SEARCH_QUERY_INSTRUCTIONS,
    SEARCH_QUERY_SCHEMA,
    SOURCE_RECOMMENDATION_INSTRUCTIONS,
    SOURCE_RECOMMENDATION_SCHEMA,
)


DEFAULT_MAX_QUERIES = 8
TEMPORARY_UNAVAILABLE_COOLDOWN_HOURS = 24
TEMPORARY_UNAVAILABLE_REJECT_AFTER = 3


@dataclass(frozen=True)
class DiscoveryConfig:
    topic: str
    limit: int = 20
    seed_urls: tuple[str, ...] = ()
    offline: bool = False
    dry_run: bool = True
    fetch_inspection: bool = False
    test_parse: bool = False
    run_id: int | None = None
    query_strategy: str = "balanced"


def discover_sources(config: DiscoveryConfig) -> dict[str, Any]:
    """Run one safe source-discovery iteration.

    Without a configured search provider this command is still useful: it creates
    a concrete search plan and can turn explicit seed URLs into source candidates.
    """
    started = time.monotonic()
    task_id = None
    if not config.dry_run:
        task_id = repository.create_agent_task(
            "discover_sources",
            topic=config.topic,
            payload={
                "limit": config.limit,
                "seed_urls": list(config.seed_urls),
                "fetch_inspection": config.fetch_inspection,
                "test_parse": config.test_parse,
                "query_strategy": config.query_strategy,
            },
            budget={
                "max_queries": DEFAULT_MAX_QUERIES,
                "max_candidates": config.limit,
                "requires_approval_for_activation": True,
            },
            status="running",
        )

    gaps = get_topic_gaps(limit=10)
    queries = generate_search_queries(
        config.topic,
        offline=config.offline,
        limit=DEFAULT_MAX_QUERIES,
        strategy=config.query_strategy,
    )
    if config.seed_urls:
        search_results = {
            "status": "seed_only",
            "provider": "operator",
            "reason": "explicit seed URLs supplied; web search skipped",
            "queries": queries,
            "limit": config.limit,
            "results": [],
        }
    else:
        search_results = search_web(queries, limit=config.limit)

    candidates = []
    rejected_domains = _rejected_memory_domains()
    cooldown_domains = _temporary_unavailable_domains()
    learning_policy = _learning_memory_policy(config.topic)
    source_inventory = _source_inventory_index()
    cooldown_sources_skipped = []
    quality_gate_sources_skipped = []
    unavailable_sources_skipped = []
    parse_failed_sources_skipped = []
    candidate_pool_limit = max(config.limit, config.limit * 3)
    candidate_urls, skipped_existing_sources, skipped_cooldown_sources = _candidate_urls(
        config.seed_urls,
        search_results.get("results") or [],
        candidate_pool_limit,
        rejected_domains=rejected_domains,
        cooldown_domains=cooldown_domains,
        source_inventory=source_inventory,
        learning_policy=learning_policy,
    )
    cooldown_sources_skipped.extend(skipped_cooldown_sources)
    for url_info in candidate_urls:
        url = url_info["url"]
        url_gate_reason = _search_result_quality_gate_reason(url_info) or _url_quality_gate_reason(url)
        if url_gate_reason:
            quality_gate_sources_skipped.append({
                "url": url,
                "domain": repository.normalize_domain(url),
                "reason": url_gate_reason,
                "stage": "url",
            })
            continue
        inspection = inspect_source(url, fetch=config.fetch_inspection)
        content_gate_reason = inspection.get("quality_gate_reason")
        if content_gate_reason:
            quality_gate_sources_skipped.append({
                "url": url,
                "domain": repository.normalize_domain(url),
                "reason": content_gate_reason,
                "stage": "content",
                "probe": inspection.get("probe"),
            })
            continue
        skip_reason = _inspection_skip_reason(inspection)
        if skip_reason:
            if not config.dry_run:
                _record_unavailable_domain(url, skip_reason, inspection)
            unavailable_sources_skipped.append({
                "url": url,
                "domain": repository.normalize_domain(url),
                "reason": skip_reason,
                "probe": inspection.get("probe"),
            })
            continue
        parse_result = test_parse_source(url, article_limit=5, topic=config.topic) if config.test_parse else None
        parse_skip_reason = _parse_skip_reason(parse_result)
        if parse_skip_reason:
            parse_failed_sources_skipped.append({
                "url": url,
                "domain": repository.normalize_domain(url),
                "reason": parse_skip_reason,
                "verdict": parse_result.get("verdict") if parse_result else None,
                "metrics": parse_result.get("metrics") if parse_result else None,
            })
            continue
        metrics = parse_result["metrics"] if parse_result else {
            "tested_articles": 0,
            "relevant_articles": 0,
            "avg_score": None,
            "duplicate_count": 0,
            "noise_count": 0,
        }
        recommendation = recommend_source_action(
            {**metrics, "inspection": inspection},
            offline=config.offline,
            evidence=(parse_result or {}).get("candidates") or [],
        )
        if recommendation.get("recommended_action") == "reject":
            parse_failed_sources_skipped.append({
                "url": url,
                "domain": repository.normalize_domain(url),
                "reason": "recommendation_reject",
                "verdict": parse_result.get("verdict") if parse_result else None,
                "metrics": metrics,
                "review_comment": recommendation.get("reason"),
            })
            continue
        candidate = {
            "url": url,
            "normalized_domain": repository.normalize_domain(url),
            "name": inspection.get("name"),
            "candidate_type": inspection.get("candidate_type"),
            "status": "needs_human_review",
            "discovered_by": "source-discovery-agent",
            "discovery_reason": url_info.get("reason") or f"Candidate for topic: {config.topic}",
            "discovery_query": url_info.get("query"),
            "query_strategy": config.query_strategy,
            "topic": config.topic,
            "confidence": inspection.get("confidence"),
            "tested_articles": metrics.get("tested_articles", 0),
            "relevant_articles": metrics.get("relevant_articles", 0),
            "avg_score": metrics.get("avg_score"),
            "duplicate_count": metrics.get("duplicate_count", 0),
            "noise_count": metrics.get("noise_count", 0),
            "recommended_action": recommendation["recommended_action"],
            "review_comment": recommendation["reason"],
            "inspection": inspection,
            "test_parse": parse_result,
        }
        if not config.dry_run:
            candidate_id = repository.upsert_source_candidate(candidate)
            repository.record_agent_action(
                task_id,
                "create_source_candidate",
                input_payload={"url": url, "topic": config.topic},
                output_payload={"candidate_id": candidate_id, **candidate},
            )
            candidate["id"] = candidate_id
        candidates.append(candidate)
        if len(candidates) >= config.limit:
            break

    result = {
        "dry_run": config.dry_run,
        "task_id": task_id,
        "topic": config.topic,
        "topic_gaps": gaps,
        "queries": queries,
        "query_strategy": config.query_strategy,
        "learning_policy": _public_learning_policy(learning_policy),
        "search": search_results,
        "rejected_domains_skipped": sorted(rejected_domains),
        "existing_sources_skipped": skipped_existing_sources,
        "cooldown_sources_skipped": cooldown_sources_skipped,
        "quality_gate_sources_skipped": quality_gate_sources_skipped,
        "unavailable_sources_skipped": unavailable_sources_skipped,
        "parse_failed_sources_skipped": parse_failed_sources_skipped,
        "candidates": candidates,
        "limits": {
            "max_queries": DEFAULT_MAX_QUERIES,
            "max_candidates": config.limit,
            "activation_requires_approval": True,
        },
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if not config.dry_run:
        _persist_query_memory(config.topic, queries, search_results.get("status"), candidate_urls, candidates)
        repository.record_agent_action(
            task_id,
            "discover_sources_finished",
            run_id=config.run_id,
            input_payload={"topic": config.topic},
            output_payload=result,
            duration_ms=result["duration_ms"],
        )
    return result


def get_topic_gaps(*, days: int = 30, target_per_topic: int = 10, limit: int = 10) -> list[dict]:
    period_to = datetime.now(timezone.utc)
    period_from = period_to - timedelta(days=days)
    return repository.compute_topic_gap_rows(period_from, period_to, target_per_topic, limit)


def generate_search_queries(
    topic: str,
    *,
    offline: bool = False,
    limit: int = DEFAULT_MAX_QUERIES,
    strategy: str = "balanced",
) -> list[str]:
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic is required")
    remembered = _remembered_queries(topic, limit=limit)
    policy = _learning_memory_policy(topic)
    combo_queries = [str(item.get("query") or "").strip() for item in policy.get("promoted_combos", []) if str(item.get("query") or "").strip()]
    muted = _muted_queries(topic) | set(str(item).lower() for item in policy.get("muted_queries", []))
    if offline:
        return _merge_queries(combo_queries + remembered, _offline_queries(topic, DEFAULT_MAX_QUERIES, strategy=strategy), limit=limit, exclude=muted)
    client = make_client(False)
    response = client.complete_json(
        SEARCH_QUERY_INSTRUCTIONS,
        f"topic: {topic}\nstrategy: {strategy}\nlimit: {limit}\n{_topic_search_context(topic)}",
        SEARCH_QUERY_SCHEMA,
        max_output_tokens=600,
    )
    queries = [str(item).strip() for item in response.data.get("queries") or [] if str(item).strip()]
    generated = _merge_queries(queries, _offline_queries(topic, DEFAULT_MAX_QUERIES, strategy=strategy), limit=DEFAULT_MAX_QUERIES)
    return _merge_queries(combo_queries + remembered, generated, limit=limit, exclude=muted)


def search_web(queries: list[str], *, limit: int = 20) -> dict[str, Any]:
    """Search candidate sources through a configured provider.

    Supported providers:
    - none: explicit no-op for seed-url MVP checks;
    - brave: Brave Search API;
    - serpapi: SerpAPI Google results.
    """
    provider = app_config.SOURCE_DISCOVERY_SEARCH_PROVIDER
    if provider in {"", "none", "disabled"}:
        return {
            "status": "not_configured",
            "provider": "none",
            "reason": "search provider is not connected yet; use --seed-url for MVP checks",
            "queries": queries,
            "limit": limit,
            "results": [],
        }
    if provider == "brave":
        return _search_brave(queries, limit=limit)
    if provider == "serpapi":
        return _search_serpapi(queries, limit=limit)
    return {
        "status": "unsupported_provider",
        "provider": provider,
        "reason": f"unsupported SOURCE_DISCOVERY_SEARCH_PROVIDER={provider}",
        "queries": queries,
        "limit": limit,
        "results": [],
    }


def inspect_source(url: str, *, fetch: bool = False) -> dict[str, Any]:
    domain = repository.normalize_domain(url)
    candidate_type = _candidate_type(url)
    result: dict[str, Any] = {
        "url": url,
        "domain": domain,
        "name": _name_from_domain(domain),
        "candidate_type": candidate_type,
        "confidence": _inspection_confidence(candidate_type),
        "fetch_checked": False,
    }
    if not fetch:
        return result

    probe, content = probe_url(url)
    result["fetch_checked"] = True
    result["probe"] = {
        "status": probe.status,
        "bytes": probe.bytes,
        "seconds": probe.seconds,
        "error": probe.error,
    }
    if content:
        text = content[:5000].decode("utf-8", errors="ignore").lower()
        result["has_rss_hint"] = "rss" in text or "application/rss+xml" in text
        result["has_news_hint"] = any(word in text for word in ("news", "press release", "новости", "пресс-релиз"))
        gate_reason = _content_quality_gate_reason(text)
        if gate_reason:
            result["quality_gate_reason"] = gate_reason
    return result


def _url_quality_gate_reason(url: str) -> str | None:
    parsed = urlsplit(_normalize_candidate_url(url))
    path = (parsed.path or "/").lower()
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return None

    last_segment = segments[-1]
    if re.search(r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|7z)(?:$|\?)", path):
        return "bad_url_type:document"
    if any(segment in {
        "tag",
        "tags",
        "author",
        "authors",
        "search",
        "category",
        "categories",
        "rubric",
        "rubrics",
        "topic",
        "topics",
    } for segment in segments):
        return "bad_url_type:index_noise"
    if last_segment in {"tag", "tags", "author", "authors", "search", "category", "rubric", "topic"}:
        return "bad_url_type:index_noise"
    if any(segment in {"report", "reports", "market-report", "market-reports", "research"} for segment in segments[:-1]):
        if last_segment not in {"report", "reports", "research", "publications", "insights"}:
            return "bad_url_type:market_report"
    if _looks_like_single_article_path(segments):
        return "single_article_url"
    return None


def _search_result_quality_gate_reason(result: dict[str, Any], *, now: datetime | None = None) -> str | None:
    """Reject search hits that are clearly stale before fetching them.

    Search APIs often return old archive pages for narrow technical topics. For
    source discovery this is usually noise: the agent needs sources with a
    current publication flow, not a single historical item.
    """
    now = now or datetime.now(timezone.utc)
    cutoff_year = now.year - max(0, int(app_config.SOURCE_DISCOVERY_STALE_RESULT_YEAR_GRACE))
    url = str(result.get("url") or "")
    parsed = urlsplit(_normalize_candidate_url(url))
    url_years = _years_in_text(parsed.path)
    stale_url_years = [year for year in url_years if year < cutoff_year]
    if stale_url_years:
        return f"stale_search_result:url_year_{max(stale_url_years)}"

    title = str(result.get("title") or "")
    snippet = str(result.get("snippet") or "")
    metadata_years = _years_in_text(f"{title} {snippet}")
    if metadata_years and max(metadata_years) < cutoff_year:
        return f"stale_search_result:metadata_year_{max(metadata_years)}"

    path_segments = [segment for segment in (parsed.path or "").lower().split("/") if segment]
    if any(segment in {"archive", "archives"} for segment in path_segments) and metadata_years:
        currentish_years = [year for year in metadata_years if year >= cutoff_year]
        if not currentish_years:
            return "stale_search_result:archive"
    return None


def _years_in_text(text: str) -> list[int]:
    return [int(match) for match in re.findall(r"\b20\d{2}\b", text or "")]


def _looks_like_single_article_path(segments: list[str]) -> bool:
    if len(segments) < 2:
        return False
    collection_markers = {
        "news",
        "novosti",
        "новости",
        "press",
        "press-releases",
        "press_release",
        "pressroom",
        "newsroom",
        "media",
        "blog",
        "articles",
        "events",
        "archive",
        "publications",
        "insights",
    }
    last = segments[-1]
    if last in collection_markers:
        return False
    if len(segments) == 2 and last in {"releases", "press-releases", "newsroom", "media-center"}:
        return False
    has_date_segment = any(re.fullmatch(r"20\d{2}", segment) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", segment) for segment in segments)
    slug_tokens = [token for token in re.split(r"[-_]+", last) if token]
    if has_date_segment and len(slug_tokens) >= 2:
        return True
    if re.fullmatch(r"\d{4,}", last) and any(
        segment in {"news", "newsitem", "article", "articles", "press", "press-release", "press-releases"}
        for segment in segments[:-1]
    ):
        return True
    if len(slug_tokens) >= 5:
        return True
    if len(segments) >= 3 and len(slug_tokens) >= 5:
        return True
    return False


def _content_quality_gate_reason(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not normalized:
        return None
    anti_bot_patterns = (
        "cf-challenge",
        "cloudflare",
        "checking your browser",
        "verify you are human",
        "captcha",
        "access denied",
        "enable javascript",
        "please enable javascript",
        "доступ запрещен",
        "подтвердите, что вы человек",
        "включите javascript",
        "проверка браузера",
    )
    if any(pattern in normalized for pattern in anti_bot_patterns):
        return "anti_bot"
    semantic_404_patterns = (
        "page not found",
        "not found",
        "page does not exist",
        "this page could not be found",
        "404",
        "страница не найдена",
        "страница не существует",
        "материал не найден",
        "ошибка 404",
    )
    if any(pattern in normalized for pattern in semantic_404_patterns):
        news_hints = ("news", "press release", "новости", "пресс-релиз", "article", "статья")
        if len(normalized) < 2500 or not any(hint in normalized for hint in news_hints):
            return "semantic_404"
    return None


def _inspection_skip_reason(inspection: dict[str, Any]) -> str | None:
    if not inspection.get("fetch_checked"):
        return None
    probe = inspection.get("probe") or {}
    status = probe.get("status")
    error = probe.get("error")
    if error:
        return f"fetch_failed: {error}"
    if status is None:
        return "fetch_failed"
    try:
        status_int = int(status)
    except (TypeError, ValueError):
        return None
    if status_int >= 500:
        return f"http_{status_int}"
    if status_int in {401, 403, 404, 410, 451}:
        return f"http_{status_int}"
    return None


def _parse_skip_reason(parse_result: dict[str, Any] | None) -> str | None:
    if not parse_result:
        return None
    verdict = parse_result.get("verdict")
    if verdict in {"listing_fetch_failed", "no_candidates", "no_useful_articles", "stale_articles"}:
        return str(verdict)
    return None


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        normalized = value.strip()
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _is_stale_article_date(value: Any, *, now: datetime | None = None) -> bool:
    published_at = _coerce_datetime(value)
    if published_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    freshness_days = max(1, int(app_config.SOURCE_DISCOVERY_FRESHNESS_DAYS))
    return published_at < now - timedelta(days=freshness_days)


def _article_topic_gate_reason(title: str, text: str, topic: str = "") -> str | None:
    topic_norm = (topic or "").lower().replace("ё", "е")
    body = f"{title or ''} {text or ''}".lower().replace("ё", "е")
    if not topic_norm:
        return None

    oil_core = (
        "oil", "gas", "petroleum", "upstream", "oilfield", "wellbore", "reservoir",
        "нефт", "газ", "скважин", "месторожд", "добыч",
    )
    drilling_terms = ("drilling", "drill", "borehole", "rig", "бурени", "буров", "скважин")
    robot_terms = ("robot", "robotic", "autonomous", "automation", "автоматизац", "робот", "автоном")
    frac_terms = ("fracturing", "fracking", "frac", "stimulation", "hydraulic fracturing", "грп", "гидроразрыв", "стимуляц")

    def has_any(terms: tuple[str, ...]) -> bool:
        return any(term in body for term in terms)

    if any(term in topic_norm for term in ("грп", "гидроразрыв", "стимуляц")):
        if not has_any(frac_terms):
            return "topic_mismatch:frac_context_required"
        return None

    if any(term in topic_norm for term in ("бурени", "буров", "drilling")):
        if not has_any(oil_core) or not has_any(drilling_terms):
            return "topic_mismatch:oilfield_drilling_context_required"
        if any(term in topic_norm for term in ("робот", "автоном", "automation", "robot")) and not has_any(robot_terms):
            return "topic_mismatch:robotic_drilling_context_required"
    return None


def test_parse_source(url: str, *, article_limit: int = 5, topic: str = "") -> dict[str, Any]:
    """Read-only test parse of a source candidate.

    This does not insert articles. It only checks whether the page looks like a
    usable listing and whether candidate article pages produce meaningful text.
    """
    source = {
        "id": 0,
        "name": _name_from_domain(repository.normalize_domain(url)),
        "url": url,
        "listing_url": url,
        "category": "source-discovery",
        "source_type": "candidate",
    }
    listing_probe, listing_content = probe_url(url)
    result: dict[str, Any] = {
        "url": url,
        "listing_probe": {
            "status": listing_probe.status,
            "bytes": listing_probe.bytes,
            "seconds": listing_probe.seconds,
            "error": listing_probe.error,
        },
        "candidates": [],
        "metrics": {
            "tested_articles": 0,
            "relevant_articles": 0,
            "avg_score": None,
            "duplicate_count": 0,
            "noise_count": 0,
        },
    }
    if listing_content is None:
        result["verdict"] = "listing_fetch_failed"
        return result

    candidates = request_parser.extract_candidate_links(source, url, listing_content, limit=article_limit)
    checks = []
    seen_urls: set[str] = set()
    relevant_count = 0
    noise_count = 0
    duplicate_count = 0
    pseudo_scores = []
    for candidate in candidates[:article_limit]:
        if candidate.url in seen_urls:
            duplicate_count += 1
            continue
        seen_urls.add(candidate.url)
        article_probe, article_content = probe_url(candidate.url)
        check: dict[str, Any] = {
            "url": candidate.url,
            "title": candidate.title,
            "listing_score": candidate.score,
            "probe_status": article_probe.status,
            "probe_error": article_probe.error,
        }
        if article_content is None:
            check["verdict"] = "article_fetch_failed"
            checks.append(check)
            continue
        title, published_at, raw_text = request_parser.parse_article_page(article_content, candidate.title)
        final_published_at = published_at or candidate.published_at
        if _is_stale_article_date(final_published_at):
            noise_count += 1
            check.update({
                "verdict": "stale_article",
                "title": title,
                "published_at": final_published_at,
                "text_chars": len(raw_text or ""),
            })
            checks.append(check)
            continue
        pre_filter = should_keep_article(title, raw_text, source)
        text_chars = len(raw_text or "")
        topic_gate_reason = _article_topic_gate_reason(title, raw_text, topic)
        is_relevant_like = pre_filter.keep and not topic_gate_reason and text_chars >= app_config.MIN_ARTICLE_TEXT_CHARS
        if is_relevant_like:
            relevant_count += 1
            pseudo_scores.append(min(85, 45 + candidate.score * 5))
        else:
            noise_count += 1
        check.update({
            "verdict": "ok" if is_relevant_like else "not_useful",
            "title": title,
            "published_at": final_published_at,
            "text_chars": text_chars,
            "prefilter_keep": pre_filter.keep,
            "prefilter_reason": pre_filter.reason,
            "prefilter_noise": list(pre_filter.matched_noise[:5]),
            "prefilter_keywords": list(pre_filter.matched_keywords[:5]),
            "topic_gate_reason": topic_gate_reason,
        })
        checks.append(check)

    tested = len(checks)
    avg_score = round(sum(pseudo_scores) / len(pseudo_scores), 2) if pseudo_scores else None
    result["candidates"] = checks
    result["metrics"] = {
        "tested_articles": tested,
        "relevant_articles": relevant_count,
        "avg_score": avg_score,
        "duplicate_count": duplicate_count,
        "noise_count": noise_count,
    }
    if relevant_count:
        result["verdict"] = "ok"
    elif not candidates:
        result["verdict"] = "no_candidates"
    elif checks and all(item.get("verdict") == "stale_article" for item in checks):
        result["verdict"] = "stale_articles"
    else:
        result["verdict"] = "no_useful_articles"
    return result


def test_source_candidate(
    candidate_id: int,
    *,
    article_limit: int = 5,
    offline: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Test an already saved source candidate and update its assessment."""
    candidate = repository.get_source_candidate(candidate_id)
    if candidate is None:
        raise ValueError(f"source candidate id={candidate_id} not found")

    started = time.monotonic()
    parse_result = test_parse_source(str(candidate["url"]), article_limit=article_limit)
    metrics = parse_result["metrics"]
    recommendation = recommend_source_action(
        metrics,
        offline=offline,
        evidence=parse_result.get("candidates") or [],
    )
    next_status = _status_for_recommendation(recommendation["recommended_action"])
    result = {
        "candidate_id": candidate_id,
        "url": candidate["url"],
        "parse": parse_result,
        "metrics": metrics,
        "recommended_action": recommendation["recommended_action"],
        "review_comment": recommendation["reason"],
        "next_status": next_status,
        "dry_run": dry_run,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if not dry_run:
        repository.update_source_candidate_assessment(
            candidate_id,
            status=next_status,
            tested_articles=metrics.get("tested_articles"),
            relevant_articles=metrics.get("relevant_articles"),
            avg_score=metrics.get("avg_score"),
            duplicate_count=metrics.get("duplicate_count"),
            noise_count=metrics.get("noise_count"),
            recommended_action=recommendation["recommended_action"],
            review_comment=recommendation["reason"],
        )
        repository.record_agent_action(
            None,
            "test_source_candidate",
            input_payload={"candidate_id": candidate_id, "article_limit": article_limit},
            output_payload=result,
            duration_ms=result["duration_ms"],
        )
    return result


def score_source_candidate(metrics: dict[str, Any]) -> dict[str, Any]:
    tested = int(metrics.get("tested_articles") or 0)
    relevant = int(metrics.get("relevant_articles") or 0)
    avg_score = float(metrics.get("avg_score") or 0)
    high_score = int(
        metrics.get("high_score_articles")
        if metrics.get("high_score_articles") is not None
        else (relevant if avg_score >= 50 else 0)
    )
    processed = int(metrics.get("processed_articles") or max(relevant, high_score))
    duplicate = int(metrics.get("duplicate_count") or 0)
    noise = int(metrics.get("noise_count") or 0)
    denominator = max(tested, 1)
    processed_denominator = max(processed, tested, 1)
    quality_score = (
        (processed / denominator) * 10
        + (relevant / processed_denominator) * 35
        + (high_score / processed_denominator) * 25
        + (avg_score / 100) * 20
        - (duplicate / denominator) * 10
        - (noise / denominator) * 15
    )
    return {
        "tested_articles": tested,
        "processed_articles": processed,
        "relevant_articles": relevant,
        "high_score_articles": high_score,
        "avg_score": avg_score if metrics.get("avg_score") is not None else None,
        "duplicate_count": duplicate,
        "noise_count": noise,
        "quality_score": round(max(0, min(100, quality_score)), 2),
    }


def recommend_source_action(
    metrics: dict[str, Any],
    *,
    offline: bool = True,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    score = score_source_candidate(metrics)
    tested = score["tested_articles"]
    processed = score["processed_articles"]
    relevant = score["relevant_articles"]
    high_score = score["high_score_articles"]
    quality = score["quality_score"]
    if tested == 0:
        fallback = {
            "recommended_action": "human_review",
            "reason": "Источник ещё не проверялся на реальных материалах; нужна ручная проверка или пробный парсинг.",
        }
    elif tested < 5:
        fallback = {
            "recommended_action": "test_more",
            "reason": "Материалов мало для уверенного решения; источник нужно протестировать ещё.",
        }
    elif quality >= 55 and relevant >= max(3, tested // 2):
        fallback = {
            "recommended_action": "add",
            "reason": "Источник дал достаточно релевантных материалов и выглядит полезным.",
        }
    elif processed >= 3 and high_score >= 2 and quality >= 45:
        fallback = {
            "recommended_action": "test_more",
            "reason": "Источник уже дал несколько сильных сигналов, но выборки пока мало для автоматического добавления.",
        }
    elif quality <= 20:
        fallback = {
            "recommended_action": "reject",
            "reason": "Источник даёт мало релевантных материалов или слишком много шума.",
        }
    else:
        fallback = {
            "recommended_action": "human_review",
            "reason": "Качество неоднозначное; решение лучше принять человеку.",
        }
    if offline:
        return {
            **fallback,
            "source": "rules",
            "confidence": _rule_confidence(score),
            "strengths": [],
            "risks": [],
        }

    client = make_client(False)
    response = client.complete_json(
        SOURCE_RECOMMENDATION_INSTRUCTIONS,
        json.dumps(
            {
                "metrics": metrics,
                "score": score,
                "fallback_recommendation": fallback,
                "article_evidence": _compact_source_evidence(evidence or metrics.get("evidence") or []),
            },
            ensure_ascii=False,
        ),
        SOURCE_RECOMMENDATION_SCHEMA,
        max_output_tokens=800,
    )
    reason = str(response.data.get("reason") or fallback["reason"]).strip()
    strengths = [str(item).strip() for item in response.data.get("strengths") or [] if str(item).strip()]
    risks = [str(item).strip() for item in response.data.get("risks") or [] if str(item).strip()]
    confidence = _safe_float(response.data.get("confidence"), default=_rule_confidence(score))
    return {
        "recommended_action": response.data.get("recommended_action") or fallback["recommended_action"],
        "reason": _format_ai_source_review(reason, strengths=strengths, risks=risks, confidence=confidence, model=response.model),
        "source": "ai",
        "model": response.model,
        "confidence": confidence,
        "strengths": strengths,
        "risks": risks,
    }


def _rule_confidence(score: dict[str, Any]) -> float:
    tested = int(score.get("tested_articles") or 0)
    quality = float(score.get("quality_score") or 0)
    if tested == 0:
        return 0.25
    if tested < 5:
        return 0.45
    if quality >= 55 or quality <= 20:
        return 0.75
    return 0.55


def _safe_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return round(max(0.0, min(1.0, parsed)), 2)


def _format_ai_source_review(
    reason: str,
    *,
    strengths: list[str],
    risks: list[str],
    confidence: float,
    model: str,
) -> str:
    parts = [reason]
    if strengths:
        parts.append("Сильные стороны: " + "; ".join(strengths[:4]) + ".")
    if risks:
        parts.append("Риски: " + "; ".join(risks[:4]) + ".")
    parts.append(f"AI confidence: {confidence:.2f}; model: {model}.")
    return " ".join(part for part in parts if part).strip()


def _compact_source_evidence(evidence: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    compact = []
    for item in evidence[:limit]:
        compact.append({
            "title": str(item.get("title") or item.get("title_ru") or "")[:240],
            "url": str(item.get("url") or "")[:400],
            "relevant": item.get("relevant"),
            "relevance_reason": str(item.get("relevance_reason") or item.get("prefilter_reason") or "")[:500],
            "summary": str(item.get("summary") or "")[:700],
            "tag": item.get("tag") or item.get("tag_id"),
            "score": item.get("total_score") if item.get("total_score") is not None else item.get("listing_score"),
            "score_label": item.get("score_label"),
            "score_explanation": str(item.get("score_explanation") or "")[:500],
            "verdict": item.get("verdict") or item.get("processing_status"),
            "text_chars": item.get("text_chars"),
        })
    return compact


def _offline_queries(topic: str, limit: int, *, strategy: str = "balanced") -> list[str]:
    strategy = (strategy or "balanced").strip().lower()
    topic_norm = (topic or "").lower().replace("ё", "е")
    if any(term in topic_norm for term in ("грп", "гидроразрыв", "стимуляц")):
        base = [
            "hydraulic fracturing technology newsroom oilfield",
            "frac stimulation technology press release upstream",
            "well stimulation hydraulic fracturing news oil gas",
            "ГРП гидроразрыв пласта технологии новости нефтесервис",
            "технологии ГРП нефтегаз пресс-релиз",
            "hydraulic fracturing innovation oilfield news",
            "frac fleet automation stimulation technology news",
            "proppant hydraulic fracturing technology upstream",
        ]
        return base[:limit]
    strategy_queries = {
        "newsroom": [
            f"{topic} newsroom press release oil gas",
            f"{topic} media center upstream company",
            f"{topic} новости компании нефтегаз",
            f"{topic} пресс-релиз нефтесервис",
        ],
        "technical": [
            f"{topic} technology case study oilfield",
            f"{topic} technical paper upstream",
            f"{topic} SPE drilling production technology",
            f"{topic} patent oil gas technology",
        ],
        "company": [
            f"{topic} vendor oilfield solution",
            f"{topic} service company launch",
            f"{topic} нефтесервис технология компания",
            f"{topic} industrial supplier oil gas news",
        ],
    }
    base = strategy_queries.get(strategy, []) + [
        f"{topic} нефтегаз новости",
        f"{topic} нефтесервис пресс-релиз",
        f"{topic} технологии добычи нефти",
        f"{topic} upstream news",
        f"{topic} oilfield technology news",
        f"{topic} press release oil gas",
        f"{topic} drilling production technology",
        f"{topic} energy industry newsroom",
    ]
    return base[:limit]


def _topic_search_context(topic: str) -> str:
    topic_norm = (topic or "").lower().replace("ё", "е")
    if any(term in topic_norm for term in ("грп", "гидроразрыв", "стимуляц")):
        return (
            "domain glossary: ГРП means гидроразрыв пласта / hydraulic fracturing / frac / "
            "well stimulation in oilfield services. Avoid gas distribution station, "
            "Gas Rising Pressure, fiberglass GRP, plastics, and civil engineering meanings."
        )
    if any(term in topic_norm for term in ("бурени", "буров")):
        return (
            "domain glossary: бурение means oilfield well drilling, drilling rigs, wellbore "
            "construction, upstream drilling operations. Avoid construction drilling, data "
            "center concrete drilling, mining-only drilling and generic factory drilling."
        )
    return ""


def _candidate_type(url: str) -> str:
    lowered = url.lower()
    if "rss" in lowered or lowered.endswith(".xml"):
        return "rss"
    if any(part in lowered for part in ("newsroom", "press", "media", "releases")):
        return "newsroom"
    if any(part in lowered for part in ("news", "novosti", "новости")):
        return "media"
    return "unknown"


def _name_from_domain(domain: str) -> str:
    if not domain:
        return "Unknown source"
    return domain.split(".")[0].replace("-", " ").title()


def _inspection_confidence(candidate_type: str) -> float:
    if candidate_type == "rss":
        return 0.75
    if candidate_type == "newsroom":
        return 0.65
    if candidate_type == "media":
        return 0.55
    return 0.35


def _status_for_recommendation(action: str) -> str:
    if action == "add":
        return "needs_human_review"
    if action == "test_more":
        return "test_parsing"
    if action == "reject":
        return "rejected"
    return "needs_human_review"


def _search_brave(queries: list[str], *, limit: int) -> dict[str, Any]:
    if not app_config.BRAVE_SEARCH_API_KEY:
        return {
            "status": "missing_api_key",
            "provider": "brave",
            "reason": "BRAVE_SEARCH_API_KEY is empty",
            "queries": queries,
            "limit": limit,
            "results": [],
        }
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    per_query = max(1, min(10, limit))
    for query in queries:
        try:
            response = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": app_config.BRAVE_SEARCH_API_KEY,
                },
                params={"q": query, "count": per_query},
                timeout=app_config.SOURCE_DISCOVERY_SEARCH_TIMEOUT,
            )
            if response.status_code >= 400:
                errors.append(f"{query}: HTTP {response.status_code} {response.text[:200]}")
                continue
            for item in ((response.json().get("web") or {}).get("results") or []):
                url = item.get("url")
                if not url:
                    continue
                results.append({
                    "query": query,
                    "url": url,
                    "title": item.get("title"),
                    "snippet": item.get("description"),
                    "provider": "brave",
                })
        except Exception as exc:  # noqa: BLE001 - one query must not kill the run
            errors.append(f"{query}: {exc}")
    return _search_payload("brave", queries, limit, results, errors)


def _search_serpapi(queries: list[str], *, limit: int) -> dict[str, Any]:
    if not app_config.SERPAPI_API_KEY:
        return {
            "status": "missing_api_key",
            "provider": "serpapi",
            "reason": "SERPAPI_API_KEY is empty",
            "queries": queries,
            "limit": limit,
            "results": [],
        }
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    per_query = max(1, min(10, limit))
    for query in queries:
        try:
            response = requests.get(
                "https://serpapi.com/search.json",
                params={
                    "engine": "google",
                    "q": query,
                    "api_key": app_config.SERPAPI_API_KEY,
                    "num": per_query,
                },
                timeout=app_config.SOURCE_DISCOVERY_SEARCH_TIMEOUT,
            )
            if response.status_code >= 400:
                errors.append(f"{query}: HTTP {response.status_code} {response.text[:200]}")
                continue
            for item in response.json().get("organic_results") or []:
                url = item.get("link")
                if not url:
                    continue
                results.append({
                    "query": query,
                    "url": url,
                    "title": item.get("title"),
                    "snippet": item.get("snippet"),
                    "provider": "serpapi",
                })
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{query}: {exc}")
    return _search_payload("serpapi", queries, limit, results, errors)


def _search_payload(
    provider: str,
    queries: list[str],
    limit: int,
    results: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    deduped = _dedupe_results(results, limit)
    return {
        "status": "ok" if deduped else ("error" if errors else "empty"),
        "provider": provider,
        "queries": queries,
        "limit": limit,
        "results": deduped,
        "errors": errors[:10],
    }


def _dedupe_results(results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped = []
    for item in results:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def _rejected_memory_domains() -> set[str]:
    rows = repository.list_agent_memory(memory_type="domain", status="rejected", limit=500)
    return {str(row.get("subject") or "").strip().lower() for row in rows if row.get("subject")}


def _temporary_unavailable_domains(now: datetime | None = None) -> dict[str, dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    try:
        rows = repository.list_agent_memory(memory_type="domain", status="temporary_unavailable", limit=500)
    except Exception:  # noqa: BLE001 - cooldown memory must not break discovery
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        domain = str(row.get("subject") or "").strip().lower()
        facts = row.get("facts_json") or {}
        retry_after = _parse_datetime(facts.get("retry_after"))
        if not domain or retry_after is None or retry_after <= now:
            continue
        result[domain] = {
            "retry_after": retry_after.isoformat(),
            "failure_count": int(facts.get("failure_count") or 0),
            "last_reason": facts.get("last_reason"),
        }
    return result


def _record_unavailable_domain(url: str, reason: str, inspection: dict[str, Any]) -> None:
    domain = repository.normalize_domain(url).lower()
    if not domain:
        return
    status = _domain_unavailable_status(reason)
    existing = _domain_memory_facts(domain)
    previous_count = int(existing.get("failure_count") or 0)
    failure_count = previous_count + 1
    now = datetime.now(timezone.utc)
    retry_after = now + timedelta(hours=TEMPORARY_UNAVAILABLE_COOLDOWN_HOURS)
    if status == "temporary_unavailable" and failure_count >= TEMPORARY_UNAVAILABLE_REJECT_AFTER:
        status = "rejected"
    facts = {
        **existing,
        "last_url": url,
        "last_reason": reason,
        "failure_count": failure_count,
        "retry_after": retry_after.isoformat(),
        "last_probe": inspection.get("probe") or {},
        "updated_by": "source-discovery-agent",
    }
    score = -float(failure_count)
    try:
        repository.upsert_agent_memory(
            memory_key=f"domain:{domain}",
            memory_type="domain",
            subject=domain,
            status=status,
            score=score,
            facts=facts,
        )
    except Exception:  # noqa: BLE001 - memory write must not fail source discovery
        return


def _domain_unavailable_status(reason: str) -> str:
    """Одна ошибка — не приговор домену, какой бы она ни была.

    404/410 — мёртвая ССЫЛКА из выдачи, а не плохой сайт; 403/451 чаще всего режут
    адрес российского ядра, откуда идёт проверка, а не любого читателя; 5xx и сбой
    сети — временные по природе. Раньше любая 4xx с первого раза навсегда выкидывала
    домен из поиска. Теперь — отсрочка, а в «rejected» домен уходит после
    TEMPORARY_UNAVAILABLE_REJECT_AFTER сбоев (_record_unavailable_domain)."""
    return "temporary_unavailable"


def _domain_memory_facts(domain: str) -> dict[str, Any]:
    try:
        rows = repository.list_agent_memory(memory_type="domain", status=None, limit=1000)
    except Exception:  # noqa: BLE001 - memory read must not fail source discovery
        return {}
    for row in rows:
        if str(row.get("subject") or "").strip().lower() == domain:
            facts = row.get("facts_json") or {}
            return facts if isinstance(facts, dict) else {}
    return {}


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _source_inventory_index() -> dict[str, dict]:
    try:
        return repository.source_inventory_index()
    except Exception:  # noqa: BLE001 - inventory dedupe must not break discovery
        return {"by_url": {}, "by_domain": {}}


def _remembered_queries(topic: str, *, limit: int) -> list[str]:
    try:
        rows = repository.list_agent_memory(memory_type="query", status="active", limit=100)
    except Exception:  # noqa: BLE001 - query memory must not break source discovery
        return []
    topic_key = topic.strip().lower()
    queries = []
    for row in rows:
        facts = row.get("facts_json") or {}
        if str(facts.get("topic") or "").strip().lower() != topic_key:
            continue
        query = str(row.get("subject") or "").strip()
        if query:
            queries.append(query)
        if len(queries) >= limit:
            break
    return queries


def _muted_queries(topic: str) -> set[str]:
    try:
        rows = repository.list_agent_memory(memory_type="query", status="muted", limit=500)
    except Exception:  # noqa: BLE001 - query memory must not break source discovery
        return set()
    topic_key = topic.strip().lower()
    muted = set()
    for row in rows:
        facts = row.get("facts_json") or {}
        if str(facts.get("topic") or "").strip().lower() != topic_key:
            continue
        query = str(row.get("subject") or "").strip().lower()
        if query:
            muted.add(query)
    return muted


def _learning_memory_policy(topic: str) -> dict[str, Any]:
    topic_key = topic.strip().lower()
    policy: dict[str, Any] = {
        "topic": topic,
        "promoted_combos": [],
        "muted_combos": [],
        "promoted_combo_keys": set(),
        "blocked_combo_keys": set(),
        "muted_queries": set(),
        "domain_scores": {},
        "query_scores": {},
    }
    try:
        combo_rows = repository.list_agent_memory(memory_type="topic_query_domain", status=None, limit=500)
    except Exception:  # noqa: BLE001 - learning policy must not break discovery
        combo_rows = []
    for row in combo_rows:
        facts = row.get("facts_json") or {}
        if str(facts.get("topic") or "").strip().lower() != topic_key:
            continue
        query = str(facts.get("query") or "").strip()
        domain = str(facts.get("domain") or "").strip().lower()
        if not query or not domain:
            continue
        score = float(row.get("score") or 0)
        status = str(row.get("status") or "")
        combo = {
            "query": query,
            "domain": domain,
            "score": score,
            "status": status,
            "reason": facts.get("last_reason"),
        }
        key = _combo_policy_key(query, domain)
        if status == "active" and score > 0:
            policy["promoted_combos"].append(combo)
            policy["promoted_combo_keys"].add(key)
        elif status in {"muted", "rejected"} or score < 0:
            policy["muted_combos"].append(combo)
            policy["blocked_combo_keys"].add(key)
    policy["promoted_combos"].sort(key=lambda item: float(item.get("score") or 0), reverse=True)

    try:
        query_rows = repository.list_agent_memory(memory_type="query", status=None, limit=500)
    except Exception:  # noqa: BLE001
        query_rows = []
    for row in query_rows:
        facts = row.get("facts_json") or {}
        if str(facts.get("topic") or "").strip().lower() != topic_key:
            continue
        query = str(row.get("subject") or "").strip().lower()
        if not query:
            continue
        score = float(row.get("score") or 0)
        policy["query_scores"][query] = score
        if str(row.get("status") or "") == "muted" or score < 0:
            policy["muted_queries"].add(query)

    try:
        domain_rows = repository.list_agent_memory(memory_type="domain", status=None, limit=500)
    except Exception:  # noqa: BLE001
        domain_rows = []
    for row in domain_rows:
        domain = str(row.get("subject") or "").strip().lower()
        if domain:
            policy["domain_scores"][domain] = float(row.get("score") or 0)
    return policy


def _public_learning_policy(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "topic": policy.get("topic"),
        "promoted_combos": list(policy.get("promoted_combos") or [])[:10],
        "muted_combos": list(policy.get("muted_combos") or [])[:10],
        "muted_queries": sorted(policy.get("muted_queries") or [])[:20],
        "domain_scores": dict(list((policy.get("domain_scores") or {}).items())[:20]),
        "query_scores": dict(list((policy.get("query_scores") or {}).items())[:20]),
    }


def _combo_policy_key(query: str, domain: str) -> str:
    return f"{str(query or '').strip().lower()}::{str(domain or '').strip().lower()}"


def _rank_search_results(results: list[dict[str, Any]], learning_policy: dict[str, Any]) -> list[dict[str, Any]]:
    if not results:
        return []
    promoted_combo_keys = set(learning_policy.get("promoted_combo_keys") or set())
    blocked_combo_keys = set(learning_policy.get("blocked_combo_keys") or set())
    muted_queries = set(learning_policy.get("muted_queries") or set())
    query_scores = learning_policy.get("query_scores") or {}
    domain_scores = learning_policy.get("domain_scores") or {}

    ranked = []
    for index, item in enumerate(results):
        url = str(item.get("url") or "")
        query = str(item.get("query") or "").strip().lower()
        domain = repository.normalize_domain(url).lower()
        combo_key = _combo_policy_key(query, domain)
        if query and query in muted_queries:
            continue
        if query and domain and combo_key in blocked_combo_keys:
            continue
        score = 0.0
        if query and domain and combo_key in promoted_combo_keys:
            score += 120.0
        score += float(query_scores.get(query) or 0) * 0.6
        score += float(domain_scores.get(domain) or 0) * 0.4
        score += _freshness_rank_bonus(item)
        ranked.append((score, -index, item))
    ranked.sort(reverse=True, key=lambda row: (row[0], row[1]))
    return [item for _, _, item in ranked]


def _freshness_rank_bonus(result: dict[str, Any], *, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    years = _years_in_text(" ".join([
        str(result.get("url") or ""),
        str(result.get("title") or ""),
        str(result.get("snippet") or ""),
    ]))
    if not years:
        return 0.0
    newest = max(years)
    if newest >= now.year:
        return 12.0
    if newest == now.year - 1:
        return 5.0
    return -12.0


def _merge_queries(primary: list[str], fallback: list[str], *, limit: int, exclude: set[str] | None = None) -> list[str]:
    result = []
    seen = set()
    exclude = exclude or set()
    for query in [*primary, *fallback]:
        normalized = str(query).strip()
        key = normalized.lower()
        if not normalized or key in seen or key in exclude:
            continue
        seen.add(key)
        result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _persist_query_memory(
    topic: str,
    queries: list[str],
    search_status: str | None,
    candidate_urls: list[dict[str, str]],
    candidates: list[dict[str, Any]],
) -> None:
    by_url = {str(item.get("url") or ""): item for item in candidates}
    can_trust_empty_queries = search_status in {"ok", "empty"}
    by_query: dict[str, dict[str, Any]] = {
        str(query).strip(): {
            "found_candidates": 0,
            "relevant_articles": 0,
            "tested_articles": 0,
            "avg_scores": [],
        }
        for query in queries
        if str(query).strip() and can_trust_empty_queries
    }
    for item in candidate_urls:
        query = str(item.get("query") or "").strip()
        if not query:
            continue
        candidate = by_url.get(str(item.get("url") or ""))
        if not candidate:
            continue
        bucket = by_query.setdefault(query, {
            "found_candidates": 0,
            "relevant_articles": 0,
            "tested_articles": 0,
            "avg_scores": [],
        })
        bucket["found_candidates"] += 1
        bucket["relevant_articles"] += int(candidate.get("relevant_articles") or 0)
        bucket["tested_articles"] += int(candidate.get("tested_articles") or 0)
        if candidate.get("avg_score") is not None:
            bucket["avg_scores"].append(float(candidate["avg_score"]))

    for query, facts in by_query.items():
        tested = int(facts["tested_articles"] or 0)
        relevant = int(facts["relevant_articles"] or 0)
        avg_scores = facts.pop("avg_scores")
        avg_score = round(sum(avg_scores) / len(avg_scores), 2) if avg_scores else None
        empty_result = int(facts["found_candidates"] or 0) == 0
        score = 0.0 if empty_result else min(
            100.0,
            facts["found_candidates"] * 10 + (relevant / max(tested, 1)) * 60 + (avg_score or 0) * 0.2,
        )
        try:
            repository.upsert_agent_memory(
                memory_key=_query_memory_key(topic, query),
                memory_type="query",
                subject=query,
                status="muted" if empty_result else "active",
                score=round(score, 2),
                facts={
                    **facts,
                    "topic": topic,
                    "avg_score": avg_score,
                    "empty_result": empty_result,
                },
            )
        except Exception:  # noqa: BLE001 - memory write must not fail discovery
            continue


def _query_memory_key(topic: str, query: str) -> str:
    digest = hashlib.sha1(f"{topic.strip().lower()}::{query.strip().lower()}".encode("utf-8")).hexdigest()[:16]
    return f"query:{digest}"


def _candidate_urls(
    seed_urls: tuple[str, ...],
    search_results: list[dict[str, Any]],
    limit: int,
    *,
    rejected_domains: set[str] | None = None,
    cooldown_domains: dict[str, dict[str, Any]] | None = None,
    source_inventory: dict[str, dict] | None = None,
    learning_policy: dict[str, Any] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    skipped_existing: list[dict[str, Any]] = []
    skipped_cooldown: list[dict[str, Any]] = []
    rejected_domains = rejected_domains or set()
    cooldown_domains = cooldown_domains or {}
    source_inventory = source_inventory or {"by_url": {}, "by_domain": {}}
    learning_policy = learning_policy or {}
    blocked_combo_keys = set(learning_policy.get("blocked_combo_keys") or set())
    promoted_combo_keys = set(learning_policy.get("promoted_combo_keys") or set())
    muted_queries = set(learning_policy.get("muted_queries") or set())

    def existing_source_for(url: str) -> dict | None:
        exact = source_inventory.get("by_url", {}).get(_url_key(url))
        if exact:
            return exact
        domain = repository.normalize_domain(url).lower()
        return source_inventory.get("by_domain", {}).get(domain)

    def add_item(
        url: str,
        reason: str,
        query: str = "",
        *,
        title: str = "",
        snippet: str = "",
        provider: str = "",
        bypass_cooldown: bool = False,
    ) -> None:
        normalized_url = _normalize_candidate_url(url)
        key = _url_key(normalized_url)
        if not normalized_url or key in seen:
            return
        seen.add(key)
        existing_source = existing_source_for(normalized_url)
        domain = repository.normalize_domain(normalized_url)
        if existing_source:
            skipped_existing.append({
                "url": normalized_url,
                "domain": domain,
                "source_id": existing_source.get("id"),
                "source_name": existing_source.get("name"),
                "reason": "already_exists_in_sources",
            })
            return
        cooldown = None if bypass_cooldown else cooldown_domains.get(domain.lower())
        if cooldown:
            skipped_cooldown.append({
                "url": normalized_url,
                "domain": domain,
                "reason": "temporary_unavailable_cooldown",
                "retry_after": cooldown.get("retry_after"),
                "failure_count": cooldown.get("failure_count"),
                "last_reason": cooldown.get("last_reason"),
            })
            return
        domain_key = repository.normalize_domain(normalized_url).lower()
        query_key = str(query or "").strip().lower()
        if query_key and not bypass_cooldown and query_key in muted_queries:
            return
        if query_key and domain_key and not bypass_cooldown and _combo_policy_key(query_key, domain_key) in blocked_combo_keys:
            return
        items.append({
            "url": normalized_url,
            "reason": reason,
            "query": query,
            "title": title,
            "snippet": snippet,
            "provider": provider,
        })

    for url in seed_urls:
        add_item(str(url), "Seed URL supplied by operator", bypass_cooldown=True)
    for result in _rank_search_results(search_results, learning_policy):
        normalized = str(result.get("url") or "").strip()
        if not normalized or _url_key(normalized) in seen:
            continue
        domain = repository.normalize_domain(normalized).lower()
        if domain and domain in rejected_domains:
            continue
        reason = f"Search result for query: {result.get('query')}" if result.get("query") else "Search result"
        add_item(
            normalized,
            reason,
            str(result.get("query") or ""),
            title=str(result.get("title") or ""),
            snippet=str(result.get("snippet") or ""),
            provider=str(result.get("provider") or ""),
        )
        if len(items) >= limit:
            break
    return items[:limit], skipped_existing, skipped_cooldown


def _normalize_candidate_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if not parsed.scheme and parsed.path and "." in parsed.path.split("/")[0]:
        parsed = urlsplit(f"https://{value}")
    if parsed.scheme not in {"http", "https"} or not (parsed.netloc or parsed.path):
        return ""
    host = parsed.netloc.lower()
    path = parsed.path or ""
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ""))


def _url_key(url: str) -> str:
    return _normalize_candidate_url(url).rstrip("/").lower()
