"""Проба источника настоящим путём сбора — вердикт рубежей вставки по каждому кандидату.

Идёт ровно тем путём, что сбор по `parse_strategy` источника (лента/листинг → кандидаты
→ статья → предфильтр), теми же генераторами шагов, что и парсеры, а вместо insert_article
спрашивает его рубежи (`repository.insert_verdict`) в соединении только для чтения. Второй
реализации рубежей нет — вердикт пробы и есть то, что сделал бы сбор.

Зачем: 25.09 молчащие источники объяснила разовая проба такого рода (docs/handoff_2026-
09-25_stale-sources.md). `source-diagnose` показывает «статья скачалась и прошла
предфильтр» и молчит о рубежах вставки — а именно там терялись статьи Минэнерго, EIA,
Лукойла (ключ адреса склеил разные статьи) и Eni (то же тело у всех страниц).

Ничего не пишет: соединение пробы — только для чтения; CLI вдобавок включает этот режим
всему процессу (connection.read_only_process), так что отказ базы получит и любая запись
глубже по стеку.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from urllib.parse import urlsplit

import feedparser

from oiltech_digest.db import connection, repository
from oiltech_digest.ingestion import playwright_parser, request_parser, rss_parser, telegram_parser, verdicts
from oiltech_digest.ingestion.verdicts import Step


def probe_source(source: dict) -> dict:
    """Пройти путь сбора источника и вернуть отчёт: вердикт, адрес, заголовок, длина тела."""
    listing: dict = {}
    report = {
        "source": {key: source.get(key) for key in ("id", "name", "parse_strategy", "enabled",
                                                   "network_region", "archived_at")},
        "listing": listing,
        "notes": _notes(source),
        "rows": [],
    }
    with connection.read_only_connection() as conn:
        report["read_only"] = conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        report["rows"] = _judge(conn, _steps(source, listing))
    counts = Counter(row["verdict"] for row in report["rows"])
    report["counts"] = {name: counts[name] for name in verdicts.ORDER
                        if counts[name] or name == verdicts.WOULD_INSERT}
    return report


def _steps(source: dict, listing: dict) -> Iterable[Step]:
    """Шаги сбора по стратегии источника — теми же функциями, что у parse_source парсеров."""
    strategy = (source.get("parse_strategy") or "").strip()
    if strategy == "rss":
        url = listing["url"] = source.get("rss_url")
        content = rss_parser.fetch(url) if url else None
        if content is None:
            listing["status"] = "fetch_failed" if url else "no_rss_url"
            return ()
        listing.update(status="ok", entries=len(feedparser.parse(content).entries))
        return rss_parser.feed_entry_steps(source, content)
    if strategy == "request":
        url = listing["url"] = source.get("listing_url") or source.get("url")
        candidates = request_parser.listing_candidates(source)
        if candidates is None:
            listing["status"] = "fetch_failed" if url else "no_listing_url"
            return ()
        listing.update(status="ok", candidates=len(candidates))
        return request_parser.candidate_steps(source, candidates)
    if strategy == "playwright":
        url = listing["url"] = source.get("listing_url") or source.get("url")
        if not url:
            listing["status"] = "no_listing_url"
            return ()
        if not playwright_parser.is_available():
            listing["status"] = "playwright_unavailable"
            return ()
        candidates = playwright_parser.render_listing_candidates(source, url)
        # Статус рендера отличает «стена защиты» (blocked:403) от «на странице нет ссылок».
        render = playwright_parser.last_fetch_status()
        listing.update(status="ok" if candidates else "no_candidates" + (f" ({render})" if render else ""),
                       candidates=len(candidates))
        return request_parser.candidate_steps(source, candidates, article_fetcher=playwright_parser.render_article)
    if strategy == "telegram":
        url = listing["url"] = telegram_parser.preview_url_for_source(source)
        content = telegram_parser.fetch(url) if url else None
        if content is None:
            listing["status"] = "fetch_failed" if url else "no_channel"
            return ()
        posts = telegram_parser.extract_posts(content, limit=telegram_parser.POST_LIMIT)
        listing.update(status="ok", candidates=len(posts))
        if telegram_parser.listing_unchanged(source, posts):
            # parse_source тогда возвращается сразу и считает все посты знакомыми.
            return [Step(verdicts.KNOWN, post.url, post.title, post.published_at, len(post.text),
                         detail="превью не изменилось с прошлого сбора — посты не смотрятся")
                    for post in posts]
        return telegram_parser.post_steps(source, posts)
    listing["status"] = f"unsupported_strategy ({strategy or '—'})"
    return ()


def _judge(conn, steps: Iterable[Step]) -> list[dict]:
    """Вердикт по каждому шагу: рубежи сбора — как есть, дошедшее до вставки — insert_verdict;
    занятый ключ адреса проба делит на SAME и OTHER (key_verdict)."""
    rows: list[dict] = []
    pending: list[dict] = []
    positions: dict[str, int] = {}
    for position, step in enumerate(steps, start=1):
        verdict, holder = step.stage, None
        if step.stage == verdicts.READY:
            result = repository.insert_verdict(conn, step.record, pending)
            verdict, holder = result.verdict, result.holder
            if verdict == verdicts.DUP_URL_KEY:
                verdict = key_verdict(holder, step.record.get("url") or "", step.record.get("title"))
            if verdict == verdicts.WOULD_INSERT:
                pending.append(step.record)
                positions.setdefault(step.record.get("url") or "", position)
            elif holder is not None and holder["id"] is None:
                holder = {**holder, "position": positions.get(holder["url"])}
        rows.append({"position": position, "verdict": verdict, "url": step.url, "title": step.title,
                     "text_chars": step.text_chars, "published_at": step.published_at,
                     "detail": step.detail, "holder": holder})
    # Кто держит знакомый адрес — для отчёта: видна ли статья в ленте или скрыта.
    known = [row for row in rows if row["verdict"] == verdicts.KNOWN and row["holder"] is None]
    holders = repository.articles_by_urls(conn, [row["url"] for row in known])
    for row in known:
        row["holder"] = holders.get(row["url"])
    return rows


def key_verdict(holder: dict, url: str, title: str | None) -> str:
    """Ключ адреса занят — этой же статьёй (SAME) или другой (OTHER: ключ склеил разные
    статьи — потеря)? Без различителя «дубль по ключу» выглядит одинаково для нормы и для
    потери: 25.09 так молча терялись все новые статьи Минэнерго, EIA, Губкина, Лукойла.

    Проверка самого ключа, поэтому не через url_key: та же статья — если адрес тот же с
    точностью до схемы, www, регистра хоста и хвостового слэша, или тот же заголовок.
    Якорь и регистр пути/query — различия: по ним ключ как раз и склеивает.
    """
    if _plain_address(holder.get("url")) == _plain_address(url):
        return verdicts.DUP_URL_KEY_SAME
    same_title = " ".join((holder.get("title") or "").lower().split()) == " ".join((title or "").lower().split())
    return verdicts.DUP_URL_KEY_SAME if same_title else verdicts.DUP_URL_KEY_OTHER


def _plain_address(url: str | None) -> str:
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw  # кривой адрес (Invalid IPv6 URL, NFKC) не разобрать — сравниваем как есть
    address = parts.netloc.lower().removeprefix("www.") + parts.path.rstrip("/")
    if parts.query:
        address += f"?{parts.query}"
    if parts.fragment:
        address += f"#{parts.fragment}"
    return address


def _notes(source: dict) -> list[str]:
    notes = []
    if not source.get("enabled"):
        notes.append("источник выключен — сбор его не опрашивает; проба показывает, что было бы при включении")
    if source.get("archived_at"):
        notes.append("источник в архиве")
    if rss_parser.collected_externally(source):
        notes.append("сбор идёт через зарубежный воркер (network_region = external): там страницы качаются "
                     "из NL, знакомые адреса берутся из списка ядра, тело RSS добирается со страницы — "
                     "здесь всё качается с этого адреса, вердикты могут отличаться")
    return notes


def format_report(report: dict, rows_per_verdict: int = 15) -> str:
    """Таблица: строка на кандидата (не больше N на вердикт) и строка счётчиков в конце."""
    source, listing = report["source"], report["listing"]
    lines = [f"source-probe #{source['id']} «{source.get('name')}» · {source.get('parse_strategy') or '—'} · "
             f"база: {'только чтение' if report.get('read_only') else 'РЕЖИМ ЧТЕНИЯ НЕ ПОДТВЕРЖДЁН'}"]
    labels = (("entries", "пунктов"), ("candidates", "кандидатов"))
    extent = ", ".join(f"{label} {listing[key]}" for key, label in labels if key in listing)
    lines.append(f"  лента: {listing.get('url') or '—'} — {listing.get('status')}" + (f", {extent}" if extent else ""))
    lines.extend(f"  ! {note}" for note in report["notes"])
    shown: Counter = Counter()
    for row in report["rows"]:
        shown[row["verdict"]] += 1
        if shown[row["verdict"]] > rows_per_verdict:
            continue
        chars = "—" if row["text_chars"] is None else row["text_chars"]
        date = row["published_at"].date().isoformat() if row["published_at"] else "—"
        lines.append(f"{row['position']:>4} {row['verdict']:<17} {chars:>6} {date:<10} {row['url']}  "
                     f"«{(row['title'] or '')[:70]}»{_holder_text(row, source.get('id'))}"
                     + (f"  [{row['detail']}]" if row["detail"] else ""))
    hidden = {name: count - rows_per_verdict for name, count in shown.items() if count > rows_per_verdict}
    if hidden:
        lines.append("  … не показаны: " + ", ".join(f"{name} ещё {count}" for name, count in hidden.items())
                     + " — полный список: --json")
    total = len(report["rows"])
    lines.append(f"итого {total}: " + " · ".join(f"{name}={count}" for name, count in report["counts"].items()))
    return "\n".join(lines)


def _holder_text(row: dict, source_id) -> str:
    holder = row.get("holder")
    if not holder:
        return ""
    if holder.get("id") is None:
        parts = [f"№{holder.get('position')} этого прогона"]
    else:
        parts = [f"#{holder['id']}"]
    if holder.get("source_id") not in (None, source_id):
        parts.append(f"источник {holder['source_id']}")
    if holder.get("hidden"):
        parts.append("скрыта")
    if row["verdict"] in (verdicts.DUP_URL_KEY_OTHER, verdicts.DUP_BODY_HASH) and holder.get("url") != row["url"]:
        parts.append(holder.get("url") or "")
    if row["verdict"] == verdicts.DUP_URL_KEY_OTHER:
        parts.append(f"«{(holder.get('title') or '')[:60]}»")
    return " → " + " ".join(parts)
