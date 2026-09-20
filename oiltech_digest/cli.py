"""CLI: init-db / seed-sources / discover-rss / parse / stats.

Запуск: python -m oiltech_digest.cli <command> [options]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _setup_logging(verbose: bool) -> None:
    from oiltech_digest.logging_utils import setup_logging

    setup_logging("cli", verbose=verbose, force=True)


def cmd_init_db(args: argparse.Namespace) -> None:
    from oiltech_digest.db import connection, repository

    tables = connection.init_db()
    print(f"БД инициализирована. Таблиц в схеме: {len(tables)}")
    for t in tables:
        print(f"  - {t}")
    # Если админов нет, а пользователи есть — назначить админом первого (бутстрап #12).
    admin_id = repository.ensure_admin_bootstrap()
    if admin_id is not None:
        print(f"Бутстрап ролей: пользователь id={admin_id} назначен администратором")


def cmd_create_user(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    role = "admin" if args.admin else "user"
    user = repository.create_user(args.email, args.password, role)
    print(f"Создан пользователь id={user['id']} {user['email']} роль={user['role']}")


def cmd_set_role(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    target = next((u for u in repository.list_users() if u["email"].lower() == args.email.strip().lower()), None)
    if target is None:
        print(f"Пользователь {args.email} не найден")
        return
    repository.set_user_role(int(target["id"]), args.role)
    print(f"Роль пользователя {args.email} → {args.role}")


def cmd_list_users(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    for u in repository.list_users():
        print(f"  id={u['id']:>3}  {u['role']:<6}  {u['email']}")


def cmd_migrate_digest_to_user(args: argparse.Namespace) -> None:
    """Разовый перенос текущих глобальных статусов (article_cards.status != 'new')
    в личное состояние пользователя — чтобы его дайджест сохранился при переходе на
    пер-юзерную модель (#12)."""
    from oiltech_digest.db import repository

    target = next((u for u in repository.list_users() if u["email"].lower() == args.email.strip().lower()), None)
    if target is None:
        print(f"Пользователь {args.email} не найден")
        return
    n = repository.migrate_global_status_to_user(int(target["id"]))
    print(f"Перенесено статусов: {n} → {args.email}")


def _utc_period_from_days(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    return end - timedelta(days=days), end


def cmd_schema_check(args: argparse.Namespace) -> None:
    from oiltech_digest.readiness import schema_check

    report = schema_check()
    if report["ok"]:
        print(f"schema-check: ok, required_tables={len(report['required_tables'])}")
        return
    print(
        "schema-check: missing tables: "
        + ", ".join(report["missing_tables"])
    )
    raise SystemExit(1)


def cmd_seed_sources(args: argparse.Namespace) -> None:
    from oiltech_digest.ingestion.excel_seed import seed_sources_from_excel

    stats = seed_sources_from_excel()
    print(
        f"Seed источников: всего={stats['total']} "
        f"(вставлено={stats['inserted']}, обновлено={stats['updated']}, "
        f"telegram={stats['telegram_flagged']})"
    )


def cmd_discover_rss(args: argparse.Namespace) -> None:
    from oiltech_digest.config import RSS_PROBE_TIMEOUT
    from oiltech_digest.db import repository
    from oiltech_digest.ingestion.rss_discovery import discover_all

    stats = discover_all(
        only_missing=not args.force,
        source_id=args.source_id,
        workers=args.workers,
        dry_run=args.dry_run,
        limit=args.limit,
        timeout=args.timeout or RSS_PROBE_TIMEOUT,
    )
    print(
        f"discover-rss: проверено={stats['checked']}, "
        f"найден RSS={stats['rss']}, без RSS (request)={stats['request']}"
        + (" [dry-run, без записи]" if args.dry_run else "")
    )
    print("\nОтчёт применимости (распределение в БД):")
    for row in repository.sources_by_strategy():
        print(f"  {row['parse_strategy'] or '—'}: {row['n']}")


def cmd_parse(args: argparse.Namespace) -> None:
    from oiltech_digest.ingestion.rss_parser import parse_all

    stats = parse_all(
        max_age_days=args.max_age_days, workers=args.workers, source_id=args.source_id
    )
    print(
        f"parse: добавлено={stats['added']}, дублей={stats['duplicates']}, "
        f"пропущено по возрасту={stats['skipped_old']}, "
        f"отсеяно как шум={stats['skipped_irrelevant']}, "
        f"источников ок={stats['sources_ok']}, ошибок={stats['errors']}"
    )


def cmd_fetch_full_text(args: argparse.Namespace) -> None:
    from oiltech_digest.ingestion.article_fetcher import fetch_full_text

    stats = fetch_full_text(
        limit=args.limit,
        min_chars=args.min_chars,
        retry_too_short=args.retry_too_short,
    )
    print(
        f"fetch-full-text: проверено={stats['processed']}, обновлено={stats['updated']}, "
        f"слишком коротких={stats['too_short']}, "
        # Отдельно от «слишком коротких»: текст у статьи ЕСТЬ и он не хуже нового,
        # перезапись просто не нужна. Под общим именем это читалось как поломка.
        f"без прироста={stats.get('no_gain', 0)}, "
        # Отдельной строкой: это не сбой, а сработавшая защита от подмены текста (№24).
        # Без своего счётчика она была невидима и терялась в общей арифметике.
        f"отклонено стражем={stats.get('mismatch', 0)}, ошибок={stats['failed']}"
    )


def cmd_backfill_images(args: argparse.Namespace) -> None:
    from oiltech_digest.ingestion.article_fetcher import backfill_images

    stats = backfill_images(limit=args.limit)
    print(
        f"backfill-images: проверено={stats['processed']}, обновлено={stats['updated']}, "
        f"без картинки={stats['no_image']}, ошибок={stats['failed']}"
    )


def cmd_stats(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    print(f"Источников: {repository.count_sources()}")
    for row in repository.sources_by_strategy():
        print(f"  {row['parse_strategy'] or '—'}: {row['n']}")
    print(f"Статей: {repository.count_articles()}")
    print(f"Кандидатов кросс-дублей (один content_hash у разных URL): {repository.cross_dup_candidates()}")
    top = repository.top_sources()
    if top:
        print("Топ источников по числу статей:")
        for row in top:
            print(f"  {row['name']}: {row['n']}")


def cmd_cleanup_future_dates(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    n = repository.clear_future_published_dates(tolerance_days=args.tolerance_days)
    print(f"Обнулено будущих дат публикации (анонсы-события): {n}")


def cmd_seed_tags(args: argparse.Namespace) -> None:
    # 13.09: перешли с 18 направлений D01–D18 из xlsx на 13 тематик заказчика
    # (data/seed/tags_13_tematik.md). Прежние теги выключаются, а не удаляются:
    # на них ссылается article_tags со всей историей классификации.
    from oiltech_digest.processing.seed import seed_tags_13

    stats = seed_tags_13()
    print(f"Seed тегов: заведено {stats['tags']}, выключено прежних {stats['disabled']}")


def cmd_seed_scoring(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.seed import seed_default_scoring_criteria

    stats = seed_default_scoring_criteria()
    print(f"Seed критериев скоринга: {stats['criteria']}, сумма весов={stats['weight_sum']}")


def cmd_apply_source_overrides(args: argparse.Namespace) -> None:
    from oiltech_digest.ingestion.source_overrides import apply_overrides

    stats = apply_overrides()
    print(f"Оверрайды источников: изменено={stats['changed']}, без изменений={stats['unchanged']}, "
          f"не найдено={stats['not_found']}, неоднозначно={stats['ambiguous']}")
    # Промах по имени = «починил, а источник всё так же молчит». В bootstrap вызов обёрнут
    # в `|| true`, поэтому кода возврата никто не увидит — единственный сигнал деплою это
    # напечатанное ИМЯ, а не счётчик, который легко прочитать как 0.
    problems = (
        [(name, "не найден в БД (проверьте sources.name)") for name in stats["missing_names"]]
        + [(name, "несколько строк с этим именем (добавьте source_type в запись реестра)")
           for name in stats["ambiguous_names"]]
    )
    for name, reason in problems:
        print(f"  ВНИМАНИЕ: оверрайд НЕ применён — {name}: {reason}")


def cmd_summarize(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_summaries

    stats = process_summaries(limit=args.limit, offline=args.offline)
    print(f"summary: обработано={stats['processed']}, ошибок={stats['errors']}")


def cmd_translate(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_translations

    stats = process_translations(limit=args.limit, offline=args.offline)
    print(
        f"translate-titles: обработано={stats['processed']}, переведено AI={stats['ai']}, "
        f"ошибок={stats['errors']}"
    )


def _terminology_audit_rows(limit: int, article_id: int | None = None) -> list[dict]:
    from oiltech_digest.db import repository
    from oiltech_digest.processing.domain_glossary import terminology_warnings

    rows = []
    for article in repository.list_article_texts_for_terminology_audit(limit=limit, article_id=article_id):
        for field in ("title_ru", "summary"):
            value = article.get(field) or ""
            warnings = terminology_warnings(value, article)
            if warnings:
                rows.append({
                    "article_id": int(article["id"]),
                    "field": field,
                    "title": article.get("title"),
                    "source": article.get("source_name"),
                    "warnings": warnings,
                    "text": value,
                })
    return rows


def cmd_audit_terminology(args: argparse.Namespace) -> None:
    rows = _terminology_audit_rows(args.limit, article_id=args.article_id)
    if args.json:
        print(json.dumps({"issues": len(rows), "rows": rows}, ensure_ascii=False, default=str))
        return
    print(f"terminology-audit: проблемных полей={len(rows)}")
    for row in rows[: args.show]:
        warning = row["warnings"][0]
        print(
            f"  article={row['article_id']} field={row['field']} source={row.get('source') or '—'} "
            f"bad={warning['forbidden_ru']} -> {warning['preferred_ru']}"
        )
        print(f"    {row['text'][:220]}")


def cmd_repair_terminology(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository
    from oiltech_digest.processing.domain_glossary import enforce_glossary_text

    scanned = changed = 0
    changes = []
    for article in repository.list_article_texts_for_terminology_audit(limit=args.limit, article_id=args.article_id):
        scanned += 1
        updates: dict[str, str] = {}
        for field in ("title_ru", "summary"):
            before = article.get(field)
            if not before:
                continue
            after = enforce_glossary_text(str(before), article)
            if after != before:
                updates[field] = after
                changes.append({
                    "article_id": int(article["id"]),
                    "field": field,
                    "before": before,
                    "after": after,
                })
        if updates:
            changed += len(updates)
            if not args.dry_run:
                repository.update_article_terminology_texts(
                    int(article["id"]),
                    summary=updates.get("summary"),
                    title_ru=updates.get("title_ru"),
                )
    if args.json:
        print(json.dumps({"dry_run": args.dry_run, "scanned": scanned, "changed_fields": changed, "changes": changes[: args.show]}, ensure_ascii=False, default=str))
        return
    suffix = " [dry-run]" if args.dry_run else ""
    print(f"terminology-repair{suffix}: статей проверено={scanned}, полей к исправлению={changed}")
    for item in changes[: args.show]:
        print(f"  article={item['article_id']} field={item['field']}")
        print(f"    before: {str(item['before'])[:180]}")
        print(f"    after:  {str(item['after'])[:180]}")


def cmd_validate_terminology(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.domain_glossary import (
        GLOSSARY,
        PHRASE_REPAIRS,
        glossary_golden_cases,
        validate_glossary,
    )

    errors = validate_glossary()
    payload = {
        "ok": not errors,
        "terms": len(GLOSSARY),
        "phrase_repairs": len(PHRASE_REPAIRS),
        "golden_cases": len(glossary_golden_cases()),
        "errors": errors,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return
    print(
        f"terminology-validate: ok={payload['ok']} terms={payload['terms']} "
        f"phrase_repairs={payload['phrase_repairs']} golden_cases={payload['golden_cases']}"
    )
    for error in errors:
        print(f"  - {error}")
    if errors:
        raise SystemExit(1)


def cmd_eval_terminology(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.domain_glossary import run_terminology_eval

    report = run_terminology_eval(limit=args.limit)
    rows = report["rows"]
    if args.csv_path:
        csv_path = Path(args.csv_path)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "number",
                    "source",
                    "case",
                    "original_en",
                    "before",
                    "after",
                    "must_have",
                    "must_not",
                    "status",
                    "issues",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)
    if args.markdown_path:
        markdown_path = Path(args.markdown_path)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        sample_rows = rows[: min(args.show, len(rows))]
        table = [
            "| # | Кейс | Оригинал EN | Было плохо | Стало хорошо | Статус |",
            "|---:|---|---|---|---|---|",
        ]
        for row in sample_rows:
            table.append(
                "| {number} | {case} | {original_en} | {before} | {after} | {status} |".format(
                    **{key: _markdown_cell(value) for key, value in row.items()}
                )
            )
        markdown_path.write_text(
            "\n".join(
                [
                    "# Отчет по нефтегазовой терминологии",
                    "",
                    "## Итог",
                    "",
                    f"- Проверено примеров: {report['total']}",
                    f"- Успешно: {report['passed']}",
                    f"- Ошибок: {report['failed']}",
                    "",
                    "## Что сделано",
                    "",
                    "- Нефтегазовый словарь вынесен в `oiltech_digest/processing/domain_glossary.json`.",
                    "- Summary и перевод заголовков получают компактный блок релевантных терминов в prompt.",
                    "- После ответа модели включен детерминированный слой исправления плохих терминов.",
                    "- Сборка дайджеста дополнительно нормализует старые summary/title перед PDF/DOCX/HTML.",
                    "- Добавлены команды `validate-terminology`, `audit-terminology`, `repair-terminology`, `eval-terminology`.",
                    "- Добавлены golden-кейсы и автоматическая проверка словаря в тестах.",
                    "",
                    "## Примеры",
                    "",
                    *table,
                    "",
                ]
            ),
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, default=str))
        return
    print(
        f"terminology-eval: total={report['total']} passed={report['passed']} failed={report['failed']}"
    )
    if args.csv_path:
        print(f"csv: {args.csv_path}")
    if args.markdown_path:
        print(f"markdown: {args.markdown_path}")
    if report["failed"]:
        raise SystemExit(1)


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def cmd_tag(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_tags

    stats = process_tags(limit=args.limit, offline=args.offline)
    print(f"tagging: обработано={stats['processed']}, ошибок={stats['errors']}")


def cmd_retire_tags(args: argparse.Namespace) -> None:
    """Разовая миграция таксономии: выключить теги, которых нет в списке заказчика."""
    from oiltech_digest.processing.seed import retire_old_tags

    stats = retire_old_tags()
    print(f"retire-tags: выключено прежних тегов={stats['disabled']}")


def cmd_retag_reset(args: argparse.Namespace) -> None:
    """Снять классификацию по выключенным тегам, чтобы статьи перетегировались заново.

    Сама по себе ничего не тегирует и ничего не тратит: возвращает статьи в очередь,
    а разметку делает обычная стадия `tag` (или планировщик). Разделено намеренно —
    перетегирование 11 тысяч статей платное, и запускать его надо осознанно.
    """
    from oiltech_digest.db import repository

    removed = repository.clear_article_tags_for_disabled(limit=args.limit)
    print(f"retag-reset: снято тегов={removed} (статьи вернулись в очередь тегирования)")


def cmd_score(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_scores

    stats = process_scores(limit=args.limit, offline=args.offline)
    print(f"scoring: обработано={stats['processed']}, ошибок={stats['errors']}")


def cmd_rescore_recompute(args: argparse.Namespace) -> None:
    """Пересчитать баллы из УЖЕ сохранённых ai_score новым блендингом — без OpenAI и без воркера.
    Гоняется на ядре (РФ) прямо по БД. Полезно, когда менялась только формула блендинга, а внешний
    AI-воркер недоступен/дорог. Полный AI-перепрогон (новая модель+промпт) делается отдельно."""
    from oiltech_digest.db import repository
    from oiltech_digest.processing.pipeline import SCORE_AI_WEIGHT, SCORE_KEYWORD_WEIGHT

    updated = repository.recompute_total_scores_from_items(SCORE_KEYWORD_WEIGHT, SCORE_AI_WEIGHT)
    print(json.dumps(
        {"recomputed_article_scores": updated,
         "keyword_weight": SCORE_KEYWORD_WEIGHT, "ai_weight": SCORE_AI_WEIGHT},
        ensure_ascii=False))


def cmd_process_full(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_full

    stats = process_full(limit=args.limit, offline=args.offline)
    print(
        f"pipeline: статей={stats['processed']}, full-text={stats['fulltext']}, "
        f"суть={stats['summary']}, релевантно={stats['relevant']}, отсев={stats['rejected']}, "
        f"теги={stats['tagged']}, скоринг={stats['scored']}, ошибок={stats['errors']}"
    )


def cmd_relevance(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_relevance

    stats = process_relevance(limit=args.limit, offline=args.offline)
    print(
        f"relevance: обработано={stats['processed']}, релевантно={stats['relevant']}, "
        f"отклонено={stats['rejected']}, ошибок={stats['errors']}"
    )


def cmd_process(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_full

    # Канонический pipeline: full-text → relevance → summary → translate → tag → score.
    # Релевантность идёт до сути, чтобы summary не bias-ила гейт и чтобы не тратить AI
    # на нерелевантные материалы.
    stats = process_full(limit=args.limit, offline=args.offline)
    print(
        "process: "
        f"статей={stats['processed']}, full-text={stats['fulltext']}, "
        f"релевантно={stats['relevant']}, отклонено={stats['rejected']}, "
        f"summary={stats['summary']}, translate={stats['translated']}, "
        f"tagging={stats['tagged']}, scoring={stats['scored']}, "
        f"errors={stats['errors']}"
    )


def cmd_process_articles(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository
    from oiltech_digest.processing.pipeline import (
        make_client,
        process_pipeline_articles,
    )

    client = make_client(args.offline)
    articles = repository.get_articles_by_ids(args.article_id, include_summary=True)
    stats = process_pipeline_articles(articles, client, fetch_full=True)
    print(
        "process-articles: "
        f"статей={stats['processed']}, full-text={stats['fulltext']}, "
        f"релевантно={stats['relevant']}, отклонено={stats['rejected']}, "
        f"summary={stats['summary']}, translate={stats['translated']}, "
        f"tagging={stats['tagged']}, scoring={stats['scored']}, "
        f"errors={stats['errors']}"
    )


def cmd_enqueue_process(args: argparse.Namespace) -> None:
    """Поставить AI-обработку в очередь, НЕ выполняя её локально.

    route_ai_processing() решает куда: при AI_EXECUTION_REGION=external и включённом
    внешнем контуре — в очередь external-ai (её разбирает зарубежный worker), иначе —
    в локальную очередь ai. Пустой article_ids → worker берёт следующие N статей без
    суммы (выбор делается на стороне core в момент claim). Используется scheduler'ом,
    чтобы РФ-core не звал OpenAI напрямую.
    """
    from oiltech_digest import network_policy
    from oiltech_digest.db import repository

    decision = network_policy.route_ai_processing()
    payload = {"limit": int(args.limit), "offline": bool(args.offline)}
    job = repository.create_background_job(
        "process_articles",
        payload,
        queue_name=decision.queue_name,
        execution_region=decision.execution_region,
        capability=decision.capability,
    )
    print(
        f"enqueue-process: job id={job['id']} queue={decision.queue_name} "
        f"region={decision.execution_region} limit={args.limit} ({decision.reason})"
    )


def cmd_enqueue_recheck(args: argparse.Namespace) -> None:
    """Поставить в очередь перепрогон релевантности по всей базе батчами.

    Нерелевантные статьи будут УДАЛЕНЫ физически при применении результата на core
    (статьи из сохранённых дайджестов пропускаются, если не задан --force). На проде
    задачи разбирает внешний NL-воркер (OpenAI). limit — опциональный потолок числа статей."""
    from oiltech_digest import network_policy
    from oiltech_digest.db import repository

    decision = network_policy.route_ai_processing()
    dry_run = bool(getattr(args, "dry_run", False))
    ids = repository.all_article_ids()
    if args.limit and len(ids) > args.limit:
        if dry_run:
            # dry-run: представительная СЛУЧАЙНАЯ выборка по всей базе. Первые id —
            # уже выжившие после реального recheck (нерелевантное удалено) → дали бы 0
            # отклонений. Случайная выборка ловит и ещё не перепроверенные «сырые» статьи.
            import random
            ids = random.sample(ids, args.limit)
        else:
            ids = ids[: args.limit]
    batch = max(1, args.batch_size)
    mark = bool(getattr(args, "mark", False))
    chunks = [ids[i : i + batch] for i in range(0, len(ids), batch)]
    job_ids = []
    for chunk in chunks:
        job = repository.create_background_job(
            "recheck_relevance",
            {"article_ids": chunk, "force": bool(args.force), "dry_run": dry_run, "mark": mark},
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        job_ids.append(job["id"])
    if dry_run:
        suffix = "  ← DRY-RUN: ничего не удаляется, смотри `recheck-dry-show <job_id>`"
    elif mark:
        suffix = "  ← MARK: нерелевантные ПОМЕЧАЮТСЯ (не удаляются); смотри `recheck-marked`, затем `recheck-purge`"
    else:
        suffix = "  ← УДАЛЕНИЕ физическое"
    print(
        f"enqueue-recheck: статей={len(ids)}, задач={len(job_ids)} {job_ids if dry_run else ''}, батч={batch}, "
        f"queue={decision.queue_name} region={decision.execution_region} force={bool(args.force)} dry_run={dry_run} mark={mark} ({decision.reason}){suffix}"
    )


def cmd_recheck_marked(args: argparse.Namespace) -> None:
    """Показать помеченные на удаление (recheck --mark): счётчик + выборка с причинами.
    Ничего не удаляет — только смотрим, что накопилось на удаление."""
    from oiltech_digest.db import repository

    total = repository.count_pending_deletion()
    print(f"помечено на удаление: {total}")
    if not total:
        return
    print(f"--- первые {args.limit} (заголовок · источник · причина) ---")
    for r in repository.list_pending_deletion(args.limit):
        title = (r.get("title") or "")[:90]
        print(f"  [{r.get('source_name') or '—'}] {title}\n      причина: {r.get('deletion_reason') or '—'}")


def cmd_recheck_purge(args: argparse.Namespace) -> None:
    """РАЗОВО физически удалить все помеченные (pending_deletion) статьи. Необратимо.
    Статьи из сохранённых дайджестов пропускаются (без --force)."""
    from oiltech_digest.db import repository

    total = repository.count_pending_deletion()
    if total == 0:
        print("recheck-purge: помеченных нет — нечего удалять")
        return
    if not args.yes:
        raise SystemExit(f"recheck-purge: будет физически удалено {total} статей. "
                         "Перепроверь `recheck-marked`, затем повтори с флагом --yes.")
    deleted = repository.purge_pending_deletion(force=bool(args.force))
    print(f"recheck-purge: физически удалено={deleted} из помеченных={total} "
          f"(пропущены в дайджесте={total - deleted}, force={bool(args.force)})")


def cmd_recheck_unmark(args: argparse.Namespace) -> None:
    """Снять пометку «на удаление» со ВСЕХ статей (вернуть в ленту). Если гейт срезал лишнее."""
    from oiltech_digest.db import repository

    n = repository.unmark_all_pending_deletion()
    print(f"recheck-unmark: возвращено в строй={n}")


def cmd_recheck_dry_show(args: argparse.Namespace) -> None:
    """Показать вердикты dry-run recheck-задачи: что БЫ срезалось (заголовок + причина).
    Ничего не меняет — читает result уже выполненной задачи из background_jobs."""
    from oiltech_digest.db import repository

    job = repository.get_background_job(args.job_id)
    if job is None:
        raise SystemExit(f"задача {args.job_id} не найдена")
    # ИМЕННО result_json: get_background_job делает SELECT *, ключи = колонки (schema.sql:305).
    # Чтение несуществующего "result" молча давало {} → команда всегда ругалась «нет применённого
    # результата», то есть превью dry-run было недоступно (тот же класс бага, что T3).
    applied = ((job.get("result_json") or {}).get("applied")) or {}
    if "checked" not in applied:
        raise SystemExit(f"у задачи {args.job_id} нет применённого результата "
                         f"(status={job.get('status')}; это завершённая dry-run задача?)")
    preview = applied.get("rejected_preview") or []
    print(f"DRY-RUN job {args.job_id}: проверено={applied.get('checked')}, "
          f"оставили={applied.get('kept')}, СРЕЗАЛОСЬ БЫ={applied.get('deleted')}, ошибок={applied.get('errors')}")
    if not preview:
        print("  В этой выборке гейт ничего не срезал (0 отклонено) — вероятно, это уже "
              "выжившие статьи. Запусти dry-run заново (теперь выборка случайная по всей базе).")
        return
    print("--- что бы удалилось (заголовок · источник · причина) ---")
    for r in preview[: args.limit]:
        title = (r.get("title") or "")[:90]
        print(f"  [{r.get('source') or '—'}] {title}\n      причина: {r.get('reason') or '—'}")


def cmd_enqueue_translate(args: argparse.Namespace) -> None:
    """Поставить в очередь бэкфилл перевода заголовков (статьи без title_ru) батчами."""
    from oiltech_digest import network_policy
    from oiltech_digest.db import repository

    decision = network_policy.route_ai_processing()
    ids = repository.article_ids_needing_title_ru()
    if args.limit:
        ids = ids[: args.limit]
    batch = max(1, args.batch_size)
    chunks = [ids[i : i + batch] for i in range(0, len(ids), batch)]
    job_ids = []
    for chunk in chunks:
        job = repository.create_background_job(
            "translate_titles",
            {"article_ids": chunk},
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        job_ids.append(job["id"])
    print(
        f"enqueue-translate: без перевода={len(ids)}, задач={len(job_ids)}, батч={batch}, "
        f"queue={decision.queue_name} region={decision.execution_region} ({decision.reason})"
    )


def cmd_enqueue_external_scrape(args: argparse.Namespace) -> None:
    """Поставить в очередь фетч источников network_region='external' через зарубежный воркер.

    Эти источники недоступны с РФ-сервера (WAF/таймаут/гео-блок) — их рендер/фетч идёт
    из NL. Действует ТОЛЬКО при EXTERNAL_WORKERS_ENABLED=1 и FETCH_EXTERNAL_ENABLED=1,
    иначе no-op (источники парсятся локально как обычно). Вызывается планировщиком
    каждый цикл; можно и руками для разового прогона."""
    from oiltech_digest import config, network_policy
    from oiltech_digest.db import repository

    if not (config.EXTERNAL_WORKERS_ENABLED and config.FETCH_EXTERNAL_ENABLED):
        print("enqueue-external-scrape: внешний фетч-контур выключен "
              "(нужны EXTERNAL_WORKERS_ENABLED=1 и FETCH_EXTERNAL_ENABLED=1) — пропуск")
        return

    sources = [
        s for s in repository.get_enabled_sources()
        if str(s.get("network_region") or "auto").strip().lower() == "external"
        and s.get("parse_strategy") in {"request", "playwright", "rss"}
    ]
    enq = 0
    queues: dict[str, int] = {}
    for source in sources:
        decision = network_policy.route_source_task(source, task_kind="scrape")
        if decision.execution_region != "external":
            continue
        repository.create_background_job(
            "scrape_source",
            {"source_id": source["id"], "max_age_days": args.max_age_days},
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        enq += 1
        queues[decision.queue_name] = queues.get(decision.queue_name, 0) + 1
    detail = ", ".join(f"{q}={n}" for q, n in sorted(queues.items())) or "—"
    print(f"enqueue-external-scrape: external-источников={len(sources)}, задач={enq} ({detail})")


def cmd_enqueue_source_discovery(args: argparse.Namespace) -> None:
    """Поставить в очередь автоматический цикл поиска кандидатов источников."""
    from oiltech_digest.db import repository

    topics = [item.strip() for item in (args.topic or []) if item.strip()]
    seed_urls = [item.strip() for item in (args.seed_url or []) if item.strip()]
    payload = {
        "topics": topics,
        "seed_urls": seed_urls,
        "topic_limit": int(args.topic_limit),
        "limit": int(args.limit),
        "offline": bool(args.offline),
        "fetch_inspection": bool(args.fetch_inspection),
        "test_parse": bool(getattr(args, "test_parse", True)),
        "auto_evaluate": bool(args.evaluate),
        "article_limit": int(args.article_limit),
    }
    job = repository.create_background_job(
        "discover_source_candidates",
        payload,
        queue_name="default",
        execution_region="ru",
        capability="source-discovery",
        max_attempts=1,
    )
    topic_label = str(len(topics)) if topics else f"auto:{args.topic_limit}"
    print(
        f"enqueue-source-discovery: job id={job['id']} "
        f"topics={topic_label} limit={args.limit} evaluate={args.evaluate}"
    )


def cmd_set_source_region(args: argparse.Namespace) -> None:
    """Проставить network_region (auto|ru|external) источникам по списку id.

    Для гео-падающих западных источников из source-audit, чьи имена в БД неточны для
    реестра (Deloitte/QatarEnergy/Weatherford и т.п.). external берётся в работу
    enqueue-external-scrape при включённом внешнем контуре."""
    from oiltech_digest.db import repository

    region = args.region.strip().lower()
    if region not in {"auto", "ru", "external"}:
        raise SystemExit("region должен быть auto|ru|external")
    ids = [int(x) for x in str(args.ids).split(",") if x.strip()]
    if not ids:
        raise SystemExit("укажите --ids 1,2,3")
    n = repository.set_sources_network_region(ids, region)
    print(f"set-source-region: обновлено={n}, region={region}, ids={ids}")


def cmd_source_dump_listing(args: argparse.Namespace) -> None:
    """Выгрузить анкеры листинга источника — для подбора listing_selector у no_candidates.

    Уважает стратегию (playwright → рендер, иначе http_client.fetch с боевыми SSL-фоллбэками).
    Печатает href + текст + контейнер (тег.class родителя) — этого достаточно, чтобы понять,
    каким селектором цеплять ссылки на статьи."""
    from lxml import html as lxml_html

    from oiltech_digest.db import repository
    from oiltech_digest.ingestion import http_client

    source = repository.get_source(args.source_id)
    if source is None:
        raise SystemExit(f"источник {args.source_id} не найден")
    listing_url = getattr(args, "url", None) or source.get("listing_url") or source.get("rss_url") or source.get("url")
    strategy = (source.get("parse_strategy") or "").lower()
    render = getattr(args, "render", False)
    mode = "playwright-render" if (render or strategy == "playwright") else strategy
    print(f"#{source['id']} {source.get('name')} [{mode}] → {listing_url}")
    if render or strategy == "playwright":
        from oiltech_digest.ingestion import playwright_parser
        if not playwright_parser.is_available():
            raise SystemExit("playwright недоступен в этом контейнере")
        # больший settle — JS-SPA успевают дорисовать листинг
        content = playwright_parser.fetch_rendered(listing_url, settle_ms=8000)
    else:
        content = http_client.fetch(listing_url)
    if not content:
        raise SystemExit("листинг не получен (см. логи fetch выше)")
    print(f"получено байт: {len(content)}")

    doc = lxml_html.fromstring(content)
    try:
        doc.make_links_absolute(listing_url)
    except Exception:  # noqa: BLE001 — относительные ссылки тоже информативны
        pass
    anchors = doc.xpath("//a[@href]")
    print(f"всего <a> с href: {len(anchors)}; показываю до {args.limit} с непустым текстом:")
    shown = 0
    for a in anchors:
        text = " ".join(a.text_content().split())[:70]
        if not text:
            continue
        parent = a.getparent()
        ctx = f"{parent.tag}.{(parent.get('class') or '')[:45]}" if parent is not None else "?"
        print(f"  [{ctx}] {text}  →  {a.get('href')}")
        shown += 1
        if shown >= args.limit:
            break


def cmd_recheck_relevance(args: argparse.Namespace) -> None:
    """Локальный перепрогон релевантности (для тестов/дампа; на проде — enqueue-recheck)."""
    from oiltech_digest.processing.pipeline import process_recheck

    stats = process_recheck(
        limit=args.limit, offline=args.offline, force=args.force, max_articles=args.max_articles
    )
    print(
        f"recheck-relevance: проверено={stats['checked']}, оставлено={stats['kept']}, "
        f"удалено={stats['deleted']}, пропущено(в дайджесте)={stats['skipped_in_digest']}, "
        f"ошибок={stats['errors']}"
    )


def cmd_ai_cost_report(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    rows = repository.ai_cost_report()
    if not rows:
        print("Метрик AI-обработки пока нет.")
        return
    for row in rows:
        print(
            f"{row['stage']} · {row['language']}: runs={row['runs']}, "
            f"input={row['input_tokens']}, output={row['output_tokens']}, "
            f"total={row['total_tokens']}, avg={row['avg_tokens_per_run']}, "
            f"cost=${row['cost_usd']}"
        )


def cmd_ai_article_cost_report(args: argparse.Namespace) -> None:
    from decimal import Decimal

    from oiltech_digest.db import repository

    rows = repository.ai_article_cost_report(limit=args.limit, complete_only=not args.include_partial)
    if not rows:
        print("Полных AI-циклов по статьям пока нет.")
        return

    total_cost = Decimal("0")
    total_tokens = 0
    for row in rows:
        cost = row["cost_usd"] or Decimal("0")
        tokens = row["total_tokens"] or 0
        total_cost += cost
        total_tokens += tokens
        title = row["title"]
        if len(title) > 90:
            title = title[:87] + "..."
        print(
            f"article={row['article_id']} · {row['language'] or 'unknown'} · "
            f"stages={row['stages']}/3 · tokens={tokens} · cost=${cost} · {title}"
        )

    avg_cost = total_cost / len(rows)
    avg_tokens = total_tokens / len(rows)
    print(f"\nСредний полный прогон 1 статьи: tokens={avg_tokens:.1f}, cost=${avg_cost:.6f}")


def cmd_sources(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    for row in repository.list_sources(search=args.search, limit=args.limit):
        status = "on" if row["enabled"] else "off"
        print(
            f"{row['id']:>4} {status:<3} {row.get('parse_strategy') or '-':<8} "
            f"{row['name']} · {row.get('update_frequency') or 'частота —'} · "
            f"{row.get('rss_url') or row.get('url') or '-'}"
        )


def cmd_source_health(args: argparse.Namespace) -> None:
    from collections import Counter

    from oiltech_digest.db import repository

    rows = repository.source_health_report(stale_days=args.stale_days, limit=args.limit, verdict=args.verdict)
    counts = Counter(row["verdict"] for row in rows)
    print(
        "source-health: "
        + ", ".join(f"{name}={counts.get(name, 0)}" for name in ("no_articles", "stale", "ok", "disabled"))
    )
    for row in rows:
        last = row.get("last_article_at")
        last_s = last.date().isoformat() if hasattr(last, "date") else "—"
        print(
            f"{row['id']:>4} {row['verdict']:<11} {row.get('parse_strategy') or '-':<8} "
            f"{int(row['articles'] or 0):>5} last={last_s} · {row['name']}"
        )


def cmd_article_candidates(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    rows = repository.find_article_candidates(args.query, limit=args.limit)
    if not rows:
        print("Кандидатов не найдено.")
        return
    for row in rows:
        print(f"{row['id']:>5} · {row['language'] or 'unknown'} · {row['source_name']} · {row['title']}")
        if row.get("snippet"):
            print(f"      {row['snippet']}")


def cmd_source_enable(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    repository.set_source_enabled(args.source_id, args.enabled)
    print(f"Источник {args.source_id}: {'включён' if args.enabled else 'выключен'}")


def cmd_source_add_rss(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    source_id = repository.add_rss_source(
        name=args.name,
        rss_url=args.rss_url,
        url=args.url,
        priority=args.priority,
        category=args.category,
        update_frequency=args.frequency,
    )
    print(f"RSS-источник сохранён: id={source_id}")


def cmd_source_diagnose(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository
    from oiltech_digest.ingestion.source_diagnostics import diagnose_source

    source = repository.get_source(args.source_id)
    if source is None:
        raise SystemExit(f"Источник не найден: {args.source_id}")
    result = diagnose_source(source, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _audit_row(source: dict, diag: dict) -> dict:
    """Нормализовать вывод diagnose_source в одну компактную строку аудита."""
    probe = (diag.get("listing_probe") or diag.get("rss_probe")
             or diag.get("preview_probe") or {})
    count = (diag.get("candidate_count") if diag.get("candidate_count") is not None
             else diag.get("entry_count") if diag.get("entry_count") is not None
             else diag.get("post_count") or 0)
    error = diag.get("error") or probe.get("error")
    if not error:
        for check in diag.get("article_checks") or []:
            ap = check.get("article_probe") or {}
            if ap.get("error"):
                error = ap["error"]
                break
    return {
        "id": source.get("id"),
        "name": source.get("name"),
        "enabled": bool(source.get("enabled")),
        "strategy": source.get("parse_strategy") or "—",
        "link": diag.get("url") or source.get("listing_url") or source.get("rss_url") or source.get("url"),
        "verdict": diag.get("verdict"),
        "count": count or 0,
        "probe_status": probe.get("status"),
        "probe_bytes": probe.get("bytes"),
        "error": (error or "")[:200] or None,
    }


def cmd_source_audit(args: argparse.Namespace) -> None:
    """Аудит ВСЕХ источников: какая ссылка установлена сейчас и почему не парсится.

    По каждому источнику — read-only diagnose_source (живая проба + кандидаты). Запускать
    там, где правильное гео и установлен playwright (прод), иначе иностранные/JS-источники
    дадут ложные вердикты. Сетевые пробы идут параллельно."""
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor

    from oiltech_digest.db import repository
    from oiltech_digest.ingestion.source_diagnostics import diagnose_source

    sources = repository.list_sources(limit=args.limit)
    if args.strategy:
        sources = [s for s in sources if (s.get("parse_strategy") or "") == args.strategy]
    if args.enabled_only:
        sources = [s for s in sources if s.get("enabled")]

    def run(source: dict) -> dict:
        try:
            diag = diagnose_source(source, limit=args.probe_limit)
        except Exception as exc:  # noqa: BLE001 - одна поломка не валит аудит
            diag = {"verdict": "diagnose_error", "url": source.get("url"), "error": str(exc)}
        return _audit_row(source, diag)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(run, sources))

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return

    counts = Counter(row["verdict"] for row in rows)
    print("source-audit: " + ", ".join(f"{verdict}={n}" for verdict, n in counts.most_common()))
    for row in sorted(rows, key=lambda r: (r["verdict"] or "", r["id"])):
        flag = "on " if row["enabled"] else "off"
        probe = f"{row['probe_status']}/{row['probe_bytes']}b" if row["probe_status"] is not None else "—"
        line = (f"{row['id']:>4} {flag} {row['strategy']:<10} {str(row['verdict']):<22} "
                f"cand={row['count']:<3} probe={probe:<14} {row['name']}")
        print(line)
        print(f"      link: {row['link'] or '—'}")
        if row["error"]:
            print(f"      err:  {row['error']}")


def cmd_parse_process(args: argparse.Namespace) -> None:
    """Стриминг-пайплайн: parse идёт в фоне, process обрабатывает новые статьи по мере их появления."""
    from oiltech_digest.db import repository
    from oiltech_digest.ingestion.rss_parser import parse_all
    from oiltech_digest.processing.pipeline import make_client, process_pipeline_articles

    logger = logging.getLogger(__name__)
    client = make_client(args.offline)

    # Checkpoint: статьи с ID строго больше этого значения считаем «новыми».
    checkpoint_id: int = repository.max_article_id() or 0
    logger.info("parse-process: checkpoint article_id=%d", checkpoint_id)

    parse_stats: dict = {}
    parse_done = threading.Event()

    def _run_parse() -> None:
        parse_stats.update(
            parse_all(
                max_age_days=args.max_age_days,
                workers=args.workers,
                source_id=getattr(args, "source_id", None),
            )
        )
        parse_done.set()

    parse_thread = threading.Thread(target=_run_parse, daemon=True)
    parse_thread.start()

    process_totals = {"processed": 0, "fulltext": 0, "summary": 0,
                      "relevant": 0, "rejected": 0, "tagged": 0, "scored": 0, "errors": 0}
    poll_interval = getattr(args, "poll_interval", 10)
    batch_limit = getattr(args, "process_limit", 20)

    while not parse_done.is_set() or True:
        new_articles = repository.get_articles_needing_summary_after(
            after_id=checkpoint_id, limit=batch_limit
        )
        if new_articles:
            logger.info("parse-process: обрабатываю %d новых статей", len(new_articles))
            batch_stats = process_pipeline_articles(new_articles, client, fetch_full=True)
            for key in process_totals:
                process_totals[key] += batch_stats.get(key, 0)
            # Двигаем checkpoint чтобы не перечитывать уже обработанные.
            checkpoint_id = max(int(a["id"]) for a in new_articles)

        if parse_done.is_set() and not new_articles:
            break
        if not parse_done.is_set():
            time.sleep(poll_interval)

    parse_thread.join(timeout=5)
    print(
        f"parse-process parse: добавлено={parse_stats.get('added', '?')}, "
        f"ошибок={parse_stats.get('errors', '?')}"
    )
    print(
        f"parse-process pipeline: статей={process_totals['processed']}, "
        f"full-text={process_totals['fulltext']}, суть={process_totals['summary']}, "
        f"релевантно={process_totals['relevant']}, отсев={process_totals['rejected']}, "
        f"теги={process_totals['tagged']}, скоринг={process_totals['scored']}, "
        f"ошибок={process_totals['errors']}"
    )


def cmd_source_retry(args: argparse.Namespace) -> None:
    """Force-parse sources with verdict stale or no_articles."""
    from oiltech_digest.db import repository
    from oiltech_digest.ingestion.rss_parser import parse_all

    verdicts = set(args.verdict) if args.verdict else {"stale", "no_articles"}
    rows = repository.source_health_report(stale_days=args.stale_days, limit=1000)
    source_ids = [r["id"] for r in rows if r["verdict"] in verdicts]
    if not source_ids:
        print("source-retry: нет источников с указанными вердиктами.")
        return
    print(f"source-retry: источников для повтора = {len(source_ids)} ({', '.join(sorted(verdicts))})")
    total = {"added": 0, "duplicates": 0, "skipped_old": 0,
             "skipped_irrelevant": 0, "sources_ok": 0, "errors": 0}
    for sid in source_ids:
        stats = parse_all(
            max_age_days=args.max_age_days,
            workers=1,
            source_id=sid,
        )
        for key in total:
            total[key] += stats.get(key, 0)
    print(
        f"source-retry итог: добавлено={total['added']}, дублей={total['duplicates']}, "
        f"источников ок={total['sources_ok']}, ошибок={total['errors']}"
    )


def cmd_digest_content(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.digest import write_digest_content

    stats = write_digest_content(
        path=args.output,
        month=args.month,
        limit=args.limit,
        min_score=args.min_score,
        html_path=args.html_output,
    )
    suffix = f", html={stats['html_path']}" if stats.get("html_path") else ""
    print(f"digest-content: файл={stats['path']}, статей={stats['items']}{suffix}")


def cmd_digest_save(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.digest import save_digest_draft

    stats = save_digest_draft(month=args.month, limit=args.limit, min_score=args.min_score)
    print(
        f"digest-save: id={stats['id']}, month={stats['month']}, "
        f"items={stats['items']}, status={stats['status']}"
    )


def cmd_jobs_worker(args: argparse.Namespace) -> None:
    from oiltech_digest import background_jobs

    background_jobs.worker_loop(
        poll_seconds=args.poll_seconds,
        once=args.once,
        stale_minutes=args.stale_minutes,
        queue_names=args.queue,
    )


def cmd_external_worker(args: argparse.Namespace) -> None:
    from oiltech_digest import external_worker

    external_worker.run_loop(
        core_api_url=args.core_api_url,
        token=args.token,
        worker_id=args.worker_id,
        queues=args.queue,
        capabilities=args.capability,
        poll_seconds=args.poll_seconds,
        once=args.once,
    )


def cmd_jobs_requeue_stale(args: argparse.Namespace) -> None:
    from oiltech_digest import config
    from oiltech_digest.db import repository

    stale_minutes = (
        config.BACKGROUND_JOB_STALE_MINUTES
        if args.stale_minutes is None
        else args.stale_minutes
    )
    local = repository.requeue_stale_background_jobs(stale_minutes)
    external = repository.requeue_expired_external_leases()
    print(
        f"jobs-requeue-stale: requeued={local.requeued}, exhausted={local.exhausted}, "
        f"stale_minutes={stale_minutes}"
    )
    print(
        f"external-leases: requeued={external.requeued}, exhausted={external.exhausted}"
    )


def cmd_external_queues_status(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    status = repository.external_queue_status()
    if args.json:
        print(json.dumps(status, ensure_ascii=False, default=str, indent=2))
        return
    totals = status["totals"]
    print(
        "external-queues: "
        f"queued={totals.get('queued') or 0}, "
        f"running={totals.get('running') or 0}, "
        f"failed={totals.get('failed') or 0}, "
        f"expired_leases={totals.get('expired_leases') or 0}, "
        f"oldest_queued_at={totals.get('oldest_queued_at') or '-'}, "
        f"last_heartbeat_at={totals.get('last_heartbeat_at') or '-'}"
    )
    for row in status["queues"]:
        print(
            f"  {row['queue_name']}: "
            f"queued={row.get('queued') or 0}, "
            f"running={row.get('running') or 0}, "
            f"failed={row.get('failed') or 0}, "
            f"oldest_queued_at={row.get('oldest_queued_at') or '-'}, "
            f"last_heartbeat_at={row.get('last_heartbeat_at') or '-'}"
        )


def cmd_maintenance_cleanup(args: argparse.Namespace) -> None:
    from oiltech_digest import config
    from oiltech_digest.db import repository

    background_job_days = (
        config.BACKGROUND_JOB_RETENTION_DAYS
        if args.background_job_days is None
        else args.background_job_days
    )
    export_job_days = (
        config.EXPORT_JOB_RETENTION_DAYS
        if args.export_job_days is None
        else args.export_job_days
    )
    deleted_sessions = repository.delete_expired_user_sessions()
    deleted_background_jobs = repository.cleanup_finished_background_jobs(background_job_days)
    deleted_export_jobs = repository.cleanup_finished_export_jobs(export_job_days)
    print(
        "maintenance-cleanup: "
        f"expired_sessions={deleted_sessions}, "
        f"background_jobs={deleted_background_jobs}, "
        f"background_job_days={background_job_days}, "
        f"export_jobs={deleted_export_jobs}, "
        f"export_job_days={export_job_days}"
    )


def cmd_bench_readiness(args: argparse.Namespace) -> None:
    from oiltech_digest.benchmarks import format_benchmark_report, run_readiness_benchmark

    report = run_readiness_benchmark(
        iterations=args.iterations,
        articles_limit=args.articles_limit,
        source_limit=args.source_limit,
        jobs_limit=args.jobs_limit,
        month=args.month,
        digest_limit=args.digest_limit,
        min_score=args.min_score,
        warn_ms=args.warn_ms,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_benchmark_report(report))


def cmd_source_candidate_add(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    candidate_id = repository.upsert_source_candidate({
        "url": args.url,
        "name": args.name,
        "candidate_type": args.type,
        "topic": args.topic,
        "discovered_by": "manual",
        "discovery_reason": args.reason,
        "confidence": args.confidence,
        "status": args.status,
        "expected_tags_json": args.expected_tag or [],
        "review_comment": args.comment,
    })
    print(f"source-candidate-add: id={candidate_id} url={args.url}")


def cmd_source_candidates(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    rows = repository.list_source_candidates(
        status=args.status,
        topic=args.topic,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return
    for row in rows:
        print(
            f"#{row['id']} [{row['status']}] {row.get('name') or row['normalized_domain']} "
            f"url={row['url']} topic={row.get('topic') or '-'} "
            f"tested={row['tested_articles']} relevant={row['relevant_articles']} "
            f"avg_score={row.get('avg_score') or '-'} action={row.get('recommended_action') or '-'}"
        )


def cmd_source_candidate_triage(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    rows = repository.source_candidate_triage_report(limit=args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return
    if not rows:
        print("source-candidate-triage: очередь решений пустая")
        return
    print(f"source-candidate-triage: rows={len(rows)}")
    for row in rows:
        print(
            f"{row.get('triage_priority', 0):>5.1f}  #{row['id']}  "
            f"{row.get('recommended_action') or '-':<12}  {row.get('status'):<18}  "
            f"rel={row.get('relevant_articles', 0)}/{row.get('tested_articles', 0)} "
            f"score={row.get('avg_score') or '-'}  {row.get('topic') or 'без темы'}  "
            f"{row.get('normalized_domain') or row.get('url')}"
        )


def cmd_source_candidate_test(args: argparse.Namespace) -> None:
    from oiltech_digest.source_discovery.agent import test_source_candidate

    result = test_source_candidate(
        args.candidate_id,
        article_limit=args.article_limit,
        offline=not args.ai_recommendation,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    metrics = result["metrics"]
    mode = "dry-run" if result["dry_run"] else "saved"
    print(f"source-candidate-test: id={result['candidate_id']} {mode}")
    print(f"url={result['url']}")
    print(
        "metrics: "
        f"tested={metrics['tested_articles']} "
        f"relevant={metrics['relevant_articles']} "
        f"avg_score={metrics.get('avg_score') or '-'} "
        f"noise={metrics['noise_count']} "
        f"duplicates={metrics['duplicate_count']}"
    )
    print(f"recommendation: action={result['recommended_action']} next_status={result['next_status']}")
    print(f"comment: {result['review_comment']}")


def cmd_source_candidate_evaluate(args: argparse.Namespace) -> None:
    from oiltech_digest.source_discovery.sandbox import evaluate_source_candidate

    result = evaluate_source_candidate(
        args.candidate_id,
        article_limit=args.article_limit,
        offline=args.offline,
        collect=args.collect,
        process=args.process,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    metrics = result["metrics"]
    print(f"source-candidate-evaluate: id={result['candidate_id']} task_id={result['task_id']}")
    print(f"url={result['url']}")
    print(
        "sandbox: "
        f"collected={result['collected']['inserted_or_updated']} "
        f"processed={result['processed']['processed']} "
        f"relevant={result['processed']['relevant']} "
        f"rejected={result['processed']['rejected']} "
        f"errors={result['processed']['errors'] + result['collected']['errors']}"
    )
    print(
        "metrics: "
        f"tested={metrics['tested_articles']} "
        f"relevant={metrics['relevant_articles']} "
        f"avg_score={metrics.get('avg_score') or '-'} "
        f"noise={metrics['noise_count']} "
        f"duplicates={metrics['duplicate_count']}"
    )
    print(f"recommendation: action={result['recommended_action']} next_status={result['next_status']}")
    print(f"comment: {result['review_comment']}")


def cmd_source_candidate_approve(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    source_id = repository.approve_source_candidate(
        args.candidate_id,
        name=args.name,
        source_type=args.source_type,
        parse_strategy=args.parse_strategy,
        enabled=args.enabled,
        category=args.category,
        priority=args.priority,
        network_region=args.network_region,
    )
    repository.record_agent_action(
        None,
        "approve_source_candidate",
        input_payload={
            "candidate_id": args.candidate_id,
            "name": args.name,
            "source_type": args.source_type,
            "parse_strategy": args.parse_strategy,
            "enabled": args.enabled,
            "category": args.category,
            "priority": args.priority,
            "network_region": args.network_region,
        },
        output_payload={"source_id": source_id},
    )
    print(
        f"source-candidate-approve: candidate_id={args.candidate_id} "
        f"source_id={source_id} enabled={args.enabled}"
    )


def cmd_seed_signal_topics(args: argparse.Namespace) -> None:
    from oiltech_digest.signal_discovery import seed_default_radar_topics

    changed = seed_default_radar_topics()
    print(f"seed-signal-topics: topics={changed}")


def cmd_discover_signals(args: argparse.Namespace) -> None:
    from oiltech_digest.signal_discovery import SignalDiscoveryConfig, discover_signals

    result = discover_signals(SignalDiscoveryConfig(
        topic=args.topic,
        days=args.days,
        limit=args.limit,
        min_score=args.min_score,
        offline=args.offline,
        dry_run=args.dry_run,
        max_signals=args.max_signals,
        web_search=args.web,
        web_only=args.web_only,
        web_query_limit=args.web_query_limit,
        research_rounds=args.research_rounds,
        web_fulltext_limit=args.web_fulltext_limit,
    ))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    suffix = " [dry-run]" if result["dry_run"] else ""
    print(
        f"discover-signals{suffix}: topics={len(result['topics'])} "
        f"signals={len(result['signals'])} days={result['days']} "
        f"offline={result['offline']} web={result.get('web_search')} web_only={result.get('web_only')}"
    )
    if not result["signals"]:
        print("  сигналов не найдено: расширь topic/days или снизь --min-score")
        return
    for item in result["signals"]:
        print(
            f"  [{item['maturity']}] score={item['score']:.1f} "
            f"evidence={item.get('evidence_count', 0)} theme={item['theme']}"
        )
        print(f"    {item['title']}")
        if item.get("thesis"):
            print(f"    thesis: {item['thesis'][:220]}")


def cmd_enqueue_signal_discovery(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    payload = {
        "topic": args.topic,
        "days": args.days,
        "limit": args.limit,
        "min_score": args.min_score,
        "offline": args.offline,
        "dry_run": args.dry_run,
        "max_signals": args.max_signals,
        "web_search": args.web,
        "web_only": args.web_only,
        "web_query_limit": args.web_query_limit,
        "research_rounds": args.research_rounds,
        "web_fulltext_limit": args.web_fulltext_limit,
    }
    # См. background_jobs.enqueue_daily_signal_discovery: маршрут решает политика,
    # иначе задача уезжает на РФ-адрес и OpenAI отвечает 403 по географии.
    from oiltech_digest import network_policy

    decision = network_policy.route_ai_processing()
    job = repository.create_background_job(
        "signal_discovery",
        payload,
        queue_name=decision.queue_name if not args.offline else "default",
        execution_region=decision.execution_region if not args.offline else "ru",
        capability=decision.capability if not args.offline else None,
    )
    print(f"enqueue-signal-discovery: job id={job['id']} queue={job['queue_name']}")


def cmd_enqueue_daily_signal_discovery(args: argparse.Namespace) -> None:
    from oiltech_digest import background_jobs

    result = background_jobs.enqueue_daily_signal_discovery(force=args.force)
    if not result["enqueued"]:
        print(f"enqueue-daily-signal-discovery: skipped reason={result['reason']}")
        return
    job = result["job"]
    print(
        f"enqueue-daily-signal-discovery: job id={job['id']} "
        f"queue={job['queue_name']} max_signals={job['payload_json']['max_signals']}"
    )


def cmd_signals(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    rows = repository.list_signals(maturity=args.maturity, theme=args.theme, limit=args.limit)
    if args.json:
        payload = []
        for row in rows:
            evidence = repository.list_signal_evidence(int(row["id"]), limit=args.evidence_limit)
            payload.append({**row, "evidence": evidence})
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if not rows:
        print("signals: пусто")
        return
    for row in rows:
        print(
            f"#{row['id']} [{row['maturity']}] score={float(row['score']):.1f} "
            f"evidence={row['evidence_count']} theme={row['theme']}"
        )
        print(f"  {row['title']}")
        if row.get("thesis"):
            print(f"  {str(row['thesis'])[:220]}")
        for evidence in repository.list_signal_evidence(int(row["id"]), limit=args.evidence_limit):
            print(f"    - {evidence.get('publisher') or 'source'}: {evidence['source_url']}")


def cmd_signal_feedback(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    event_id = repository.record_signal_feedback_event(
        args.article_id,
        args.event_type,
        signal_id=args.signal_id,
        signal_evidence_id=args.signal_evidence_id,
        source_url=args.source_url,
        signal_title=args.signal_title,
        user_id=args.user_id,
        old_value=args.old_value,
        new_value=args.new_value,
        comment=args.comment,
        verdict=args.verdict,
        reason=args.reason,
        corrected_title=args.corrected_title,
        corrected_thesis=args.corrected_thesis,
        duplicate_of_signal_id=args.duplicate_of_signal_id,
    )
    target = args.article_id or args.signal_id or args.source_url
    print(f"signal-feedback: id={event_id} target={target} event={args.event_type}")


def cmd_import_signal_feedback(args: argparse.Namespace) -> None:
    from oiltech_digest.signal_feedback import import_signal_feedback_csv

    result = import_signal_feedback_csv(args.path, user_id=args.user_id, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    print(
        "import-signal-feedback: "
        f"rows={result['rows']} events={result['feedback_events']} "
        f"memories={result['memories']} dry_run={result['dry_run']}"
    )


def cmd_retire_signal_query_hints(args: argparse.Namespace) -> None:
    from oiltech_digest.signal_feedback import retire_query_hints_from_negative_feedback

    result = retire_query_hints_from_negative_feedback(dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    print(
        "retire-signal-query-hints: "
        f"active_before={result['active_before']} kept={result['kept']} "
        f"retired={result['retired']} dry_run={result['dry_run']}"
    )


def cmd_export_signal_training_jsonl(args: argparse.Namespace) -> None:
    from oiltech_digest.signal_training import export_signal_training_jsonl

    result = export_signal_training_jsonl(
        args.path,
        limit=args.limit,
        with_feedback_only=not args.all,
        verdict=args.verdict,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    print(
        "export-signal-training-jsonl: "
        f"examples={result['examples']} path={result['path']} "
        f"with_feedback_only={result['with_feedback_only']}"
    )


def cmd_export_signal_context(args: argparse.Namespace) -> None:
    from oiltech_digest.signal_training import export_signal_context_bundle

    result = export_signal_context_bundle(
        args.path,
        include_rejected=args.include_rejected,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    print(
        "export-signal-context: "
        f"path={result['path']} signals={result['signals']} evidence={result['evidence']} "
        f"memories={result['memories']} feedback_events={result['feedback_events']}"
    )


def cmd_import_signal_context(args: argparse.Namespace) -> None:
    from oiltech_digest.signal_training import import_signal_context_bundle

    result = import_signal_context_bundle(args.path, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    suffix = " [dry-run]" if result["dry_run"] else ""
    print(
        "import-signal-context"
        f"{suffix}: path={result['path']} signals={result['signals']} evidence={result['evidence']} "
        f"memories={result['memories']} feedback_events={result['feedback_events']}"
    )


def cmd_source_quality(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    period_from, period_to = _utc_period_from_days(args.days)
    rows = repository.compute_source_quality_rows(period_from, period_to)
    if args.snapshot:
        saved = repository.snapshot_source_quality(period_from, period_to)
        print(f"source-quality: snapshot saved rows={saved} period_days={args.days}")
    if args.json:
        print(json.dumps(rows[:args.limit], ensure_ascii=False, indent=2, default=str))
        return
    for row in rows[:args.limit]:
        print(
            f"{row['quality_score']:>5}  {row['source_name']}  "
            f"found={row['articles_found']} processed={row['articles_processed']} "
            f"relevant={row['relevant_count']} rejected={row['rejected_count']} "
            f"avg_score={row.get('avg_score') or '-'} digest={row['digest_count']} "
            f"noise={row['noise_count']} dup={row['duplicate_count']}"
        )


def cmd_agent_query_memory(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    status = None if args.status == "all" else args.status
    rows = repository.query_memory_report(status=status, limit=args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return
    if not rows:
        print("agent-query-memory: записей нет")
        return
    print(f"agent-query-memory: status={args.status} rows={len(rows)}")
    for row in rows:
        label = "пустой" if row.get("empty_result") else "рабочий"
        print(
            f"{row.get('score', 0):>5.1f}  {row.get('status'):<6}  {label:<7}  "
            f"{row.get('found_candidates', 0)} канд.  {row.get('relevance_rate', 0):.0%} релев.  "
            f"{row.get('topic') or 'без темы'} :: {row.get('query')}"
        )


def cmd_agent_readiness(args: argparse.Namespace) -> None:
    from oiltech_digest.source_discovery.readiness import source_discovery_readiness

    report = source_discovery_readiness()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return
    print(f"agent-readiness: status={report['status']} ok={report['ok']}")
    for name, check in report["checks"].items():
        print(f"  {name}: ok={check['ok']}")
    if report["issues"]:
        print("Проблемы:")
        for issue in report["issues"]:
            print(f"  - [{issue['severity']}] {issue['code']}: {issue['message']}")
    if report["recommendations"]:
        print("Что сделать:")
        for item in report["recommendations"]:
            print(f"  - {item}")


def cmd_discover_sources(args: argparse.Namespace) -> None:
    from oiltech_digest.source_discovery.agent import DiscoveryConfig, discover_sources

    result = discover_sources(DiscoveryConfig(
        topic=args.topic,
        limit=args.limit,
        seed_urls=tuple(args.seed_url or []),
        offline=args.offline,
        dry_run=args.dry_run,
        fetch_inspection=args.fetch_inspection,
        test_parse=args.test_parse,
    ))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    mode = "dry-run" if result["dry_run"] else f"task_id={result.get('task_id')}"
    print(f"discover-sources: topic={result['topic']} {mode}")
    print("\nПоисковые запросы:")
    for query in result["queries"]:
        print(f"  - {query}")
    print("\nПоиск:")
    search = result["search"]
    reason = f" reason={search['reason']}" if search.get("reason") else ""
    print(
        f"  status={search.get('status')} provider={search.get('provider') or '-'} "
        f"results={len(search.get('results') or [])}{reason}"
    )
    if result["topic_gaps"]:
        print("\nТемы с дефицитом:")
        for gap in result["topic_gaps"][:5]:
            print(f"  - {gap['topic']}: signals={gap['signals']}/{gap['target_signals']} gap={gap['gap']}")
    if result["candidates"]:
        print("\nКандидаты:")
        for item in result["candidates"]:
            print(
                f"  - {item.get('id', 'dry')} {item['url']} "
                f"type={item.get('candidate_type')} action={item.get('recommended_action')} "
                f"tested={item.get('tested_articles', 0)} relevant={item.get('relevant_articles', 0)} "
                f"comment={item.get('review_comment')}"
            )
    else:
        print("\nКандидатов пока нет. Для MVP передайте --seed-url или подключите поисковый провайдер.")


def cmd_agent_plan(args: argparse.Namespace) -> None:
    from oiltech_digest.source_discovery.planner import PlannerConfig, build_plan, enqueue_plan_actions

    from oiltech_digest.db import repository

    run_id = None
    if args.enqueue:
        run_id = repository.create_agent_run(
            "source_discovery_cycle",
            trigger="cli",
            payload={
                "days": args.days,
                "target_per_topic": args.target_per_topic,
                "topic_limit": args.topic_limit,
                "candidate_limit": args.candidate_limit,
                "max_actions": args.max_actions,
                "offline": args.offline,
                "evaluate": args.evaluate,
            },
        )
    plan = build_plan(PlannerConfig(
        days=args.days,
        target_per_topic=args.target_per_topic,
        topic_limit=args.topic_limit,
        candidate_limit=args.candidate_limit,
        max_actions=args.max_actions,
        persist_memory=not args.no_memory,
        run_id=run_id,
    ))
    queued = {"queued": 0, "jobs": []}
    if args.enqueue:
        queued = enqueue_plan_actions(plan, offline=args.offline, evaluate=args.evaluate, run_id=run_id)
        repository.finish_agent_run(run_id, status="ok", result={**plan, "run_id": run_id, "queued": queued})
    if args.json:
        print(json.dumps({**plan, "run_id": run_id, "queued": queued}, ensure_ascii=False, indent=2, default=str))
        return

    print(
        "agent-plan: "
        f"run_id={run_id or '-'} actions={len(plan['actions'])} memory_updates={len(plan['memory_updates'])} "
        f"queued={queued['queued']}"
    )
    for index, action in enumerate(plan["actions"], start=1):
        label = action["action_type"]
        target = action.get("topic") or action.get("source_name") or action.get("url") or action.get("candidate_id")
        print(f"{index}. [{action['priority']}] {label}: {target}")
        print(f"   reason: {action['reason']}")
    for job in queued["jobs"]:
        print(f"queued job={job['job_id']} topic={job['topic']} priority={job['priority']}")


def cmd_enqueue_agent_plan(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    payload = {
        "days": int(args.days),
        "target_per_topic": int(args.target_per_topic),
        "topic_limit": int(args.topic_limit),
        "candidate_limit": int(args.candidate_limit),
        "max_actions": int(args.max_actions),
        "persist_memory": not args.no_memory,
        "offline": bool(args.offline),
        "evaluate": bool(args.evaluate),
    }
    job = repository.create_background_job(
        "source_discovery_plan",
        payload,
        queue_name="default",
        execution_region="ru",
        capability="source-discovery",
        max_attempts=1,
    )
    print(
        f"enqueue-agent-plan: job id={job['id']} "
        f"topics={args.topic_limit} max_actions={args.max_actions}"
    )


def _agent_loop_payload(args: argparse.Namespace) -> dict:
    return {
        "goal": str(args.goal),
        "days": int(args.days),
        "target_per_topic": int(args.target_per_topic),
        "topic_limit": int(args.topic_limit),
        "candidate_limit": int(args.candidate_limit),
        "max_actions": int(args.max_actions),
        "max_iterations": int(args.max_iterations),
        "offline": bool(args.offline),
        "fetch_inspection": bool(args.fetch_inspection),
        "test_parse": bool(getattr(args, "test_parse", True)),
        "dry_run": bool(args.dry_run),
        "auto_evaluate": bool(args.evaluate),
        "article_limit": int(args.article_limit),
        "persist_memory": not bool(args.no_memory),
        "max_daily_loop_runs": int(args.max_daily_loop_runs),
        "max_daily_candidates": int(args.max_daily_candidates),
        "max_daily_evaluations": int(args.max_daily_evaluations),
    }


def cmd_agent_loop(args: argparse.Namespace) -> None:
    from oiltech_digest.source_discovery.loop import AgentLoopConfig, run_agent_loop

    payload = _agent_loop_payload(args)
    result = run_agent_loop(AgentLoopConfig(**payload))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    print(
        f"agent-loop: run_id={result['run_id']} iterations={len(result['iterations'])} "
        f"candidates={result['total_candidates']} reason={result['terminal_reason']}"
    )
    for item in result["iterations"]:
        print(
            f"  iter={item['iteration']} actions={item['action_count']} "
            f"auto={item['auto_action_count']} review={item['human_review_count']}"
        )
        for observation in item["observations"]:
            print(
                f"    - {observation['topic']}: candidates={observation['candidate_count']} "
                f"strategy={observation.get('query_strategy') or '-'} search={observation['search_status']}"
            )


def cmd_enqueue_agent_loop(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    if not bool(getattr(args, "allow_parallel", False)):
        counts = repository.background_job_status_counts(kind_prefix="source_discovery_loop")
        active_count = sum(int(counts.get(status) or 0) for status in ("queued", "running", "finalizing"))
        if active_count:
            print(f"enqueue-agent-loop: skipped active_jobs={active_count}")
            return

    payload = _agent_loop_payload(args)
    job = repository.create_background_job(
        "source_discovery_loop",
        payload,
        queue_name="default",
        execution_region="ru",
        capability="source-discovery",
        max_attempts=1,
    )
    print(
        f"enqueue-agent-loop: job id={job['id']} "
        f"iterations={args.max_iterations} max_actions={args.max_actions}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oiltech_digest.cli", description="OilTech Digest — сбор RSS")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог (INFO)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_agent_loop_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--goal", default="Найти новые полезные источники сигналов")
        p.add_argument("--days", type=int, default=30, help="период анализа базы")
        p.add_argument("--target-per-topic", type=int, default=10)
        p.add_argument("--topic-limit", type=int, default=5)
        p.add_argument("--candidate-limit", type=int, default=10)
        p.add_argument("--max-actions", type=int, default=5)
        p.add_argument("--max-iterations", type=int, default=3)
        p.add_argument("--offline", action=argparse.BooleanOptionalAction, default=True)
        p.add_argument("--fetch-inspection", action="store_true")
        p.add_argument("--test-parse", action=argparse.BooleanOptionalAction, default=True)
        p.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
        p.add_argument("--evaluate", action=argparse.BooleanOptionalAction, default=True)
        p.add_argument("--article-limit", type=int, default=5)
        p.add_argument("--no-memory", action="store_true")
        p.add_argument("--allow-parallel", action="store_true", help="разрешить несколько одновременных agent-loop задач")
        p.add_argument("--max-daily-loop-runs", type=int, default=4, help="суточный лимит agent-loop запусков; 0 = без лимита")
        p.add_argument("--max-daily-candidates", type=int, default=100, help="суточный лимит новых кандидатов; 0 = без лимита")
        p.add_argument("--max-daily-evaluations", type=int, default=100, help="суточный лимит AI-оценок кандидатов; 0 = без лимита")

    sub.add_parser("init-db", help="создать схему БД").set_defaults(func=cmd_init_db)
    sub.add_parser("schema-check", help="проверить наличие обязательных таблиц").set_defaults(func=cmd_schema_check)
    sub.add_parser("seed-sources", help="загрузить источники из Excel").set_defaults(func=cmd_seed_sources)

    p_cu = sub.add_parser("create-user", help="создать пользователя админки")
    p_cu.add_argument("--email", required=True)
    p_cu.add_argument("--password", required=True)
    p_cu.add_argument("--admin", action="store_true", help="назначить администратором")
    p_cu.set_defaults(func=cmd_create_user)

    p_sr = sub.add_parser("set-role", help="сменить роль пользователя (admin|user)")
    p_sr.add_argument("--email", required=True)
    p_sr.add_argument("--role", choices=["admin", "user"], required=True)
    p_sr.set_defaults(func=cmd_set_role)

    sub.add_parser("list-users", help="список пользователей и ролей").set_defaults(func=cmd_list_users)

    p_md = sub.add_parser("migrate-digest-to-user", help="перенести текущий глобальный дайджест/статусы на пользователя (#12)")
    p_md.add_argument("--email", required=True)
    p_md.set_defaults(func=cmd_migrate_digest_to_user)

    p_disc = sub.add_parser("discover-rss", help="автообнаружение RSS-лент")
    p_disc.add_argument("--force", action="store_true", help="перепроверить все, а не только без rss_url")
    p_disc.add_argument("--source-id", type=int, default=None, help="только указанный источник")
    p_disc.add_argument("--workers", type=int, default=10)
    p_disc.add_argument("--limit", type=int, default=None, help="проверить только первые N кандидатов")
    p_disc.add_argument("--timeout", type=int, default=None, help="таймаут одного probe-запроса, сек")
    p_disc.add_argument("--dry-run", action="store_true", help="не записывать в БД")
    p_disc.set_defaults(func=cmd_discover_rss)

    p_parse = sub.add_parser("parse", help="спарсить ленты в articles")
    p_parse.add_argument("--max-age-days", type=int, default=None, help="не сохранять статьи старше N дней")
    p_parse.add_argument("--source-id", type=int, default=None)
    p_parse.add_argument("--workers", type=int, default=10)
    p_parse.set_defaults(func=cmd_parse)

    p_full = sub.add_parser("fetch-full-text", help="дозагрузить полный текст статей по URL")
    p_full.add_argument("--limit", type=int, default=50)
    p_full.add_argument("--min-chars", type=int, default=800)
    p_full.add_argument("--retry-too-short", action="store_true",
                        help="повторить попытку для статей со статусом too_short или no_gain")
    p_full.set_defaults(func=cmd_fetch_full_text)

    p_backfill_images = sub.add_parser("backfill-images", help="дозаполнить og:image у статей без картинки")
    p_backfill_images.add_argument("--limit", type=int, default=200)
    p_backfill_images.set_defaults(func=cmd_backfill_images)

    sub.add_parser("stats", help="диагностика БД").set_defaults(func=cmd_stats)

    p_cf = sub.add_parser("cleanup-future-dates",
                          help="обнулить published_at из будущего (анонсы-события календаря)")
    p_cf.add_argument("--tolerance-days", type=int, default=2,
                      help="сколько дней вперёд считать допустимыми")
    p_cf.set_defaults(func=cmd_cleanup_future_dates)

    sub.add_parser("seed-tags", help="загрузить 13 тематик заказчика").set_defaults(func=cmd_seed_tags)
    sub.add_parser("seed-scoring", help="создать базовые критерии скоринга").set_defaults(func=cmd_seed_scoring)
    sub.add_parser("apply-source-overrides", help="применить playwright/listing-оверрайды источников").set_defaults(func=cmd_apply_source_overrides)

    def add_ai_args(p):
        p.add_argument("--limit", type=int, default=20)
        p.add_argument("--offline", action="store_true", help="детерминированная локальная заглушка без OpenAI API")

    p_summary = sub.add_parser("summarize", help="сформировать AI-суть статей")
    add_ai_args(p_summary)
    p_summary.set_defaults(func=cmd_summarize)

    p_relevance = sub.add_parser("relevance", help="AI-фильтр релевантности (отсев нерелевантных)")
    add_ai_args(p_relevance)
    p_relevance.set_defaults(func=cmd_relevance)

    p_translate = sub.add_parser("translate-titles", help="перевести заголовки на русский (отдельная стадия, AI только для иностранных)")
    add_ai_args(p_translate)
    p_translate.set_defaults(func=cmd_translate)

    p_audit_terms = sub.add_parser("audit-terminology", help="найти плохие нефтегазовые термины в сохранённых title_ru/summary")
    p_audit_terms.add_argument("--limit", type=int, default=500)
    p_audit_terms.add_argument("--article-id", type=int, default=None)
    p_audit_terms.add_argument("--show", type=int, default=30)
    p_audit_terms.add_argument("--json", action="store_true")
    p_audit_terms.set_defaults(func=cmd_audit_terminology)

    p_repair_terms = sub.add_parser("repair-terminology", help="исправить плохие нефтегазовые термины в сохранённых title_ru/summary без OpenAI")
    p_repair_terms.add_argument("--limit", type=int, default=500)
    p_repair_terms.add_argument("--article-id", type=int, default=None)
    p_repair_terms.add_argument("--show", type=int, default=30)
    p_repair_terms.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    p_repair_terms.add_argument("--json", action="store_true")
    p_repair_terms.set_defaults(func=cmd_repair_terminology)

    p_validate_terms = sub.add_parser("validate-terminology", help="проверить нефтегазовый словарь, regex и golden-кейсы")
    p_validate_terms.add_argument("--json", action="store_true")
    p_validate_terms.set_defaults(func=cmd_validate_terminology)

    p_eval_terms = sub.add_parser("eval-terminology", help="прогнать оценку нефтегазовой терминологии и собрать отчет")
    p_eval_terms.add_argument("--limit", type=int, default=100)
    p_eval_terms.add_argument("--show", type=int, default=30)
    p_eval_terms.add_argument("--csv-path", default="docs/translation_terminology_100_eval.csv")
    p_eval_terms.add_argument("--markdown-path", default="docs/translation_terminology_work_report.md")
    p_eval_terms.add_argument("--json", action="store_true")
    p_eval_terms.set_defaults(func=cmd_eval_terminology)

    p_tag = sub.add_parser("tag", help="присвоить статьи тегам")
    add_ai_args(p_tag)
    p_tag.set_defaults(func=cmd_tag)

    sub.add_parser(
        "retire-tags",
        help="разово выключить теги вне списка заказчика (смена таксономии)",
    ).set_defaults(func=cmd_retire_tags)

    p_retag = sub.add_parser(
        "retag-reset",
        help="снять классификацию по выключенным тегам — статьи вернутся в очередь тегирования",
    )
    p_retag.add_argument("--limit", type=int, default=None,
                         help="сколько связей снять за прогон (по умолчанию все)")
    p_retag.set_defaults(func=cmd_retag_reset)

    p_score = sub.add_parser("score", help="рассчитать скоринг статей")
    add_ai_args(p_score)
    p_score.set_defaults(func=cmd_score)

    p_rescore_recompute = sub.add_parser(
        "rescore-recompute",
        help="пересчитать total_score из сохранённых ai_score новым блендингом (без OpenAI/воркера, на ядре)")
    p_rescore_recompute.set_defaults(func=cmd_rescore_recompute)

    p_process = sub.add_parser("process", help="summary → tagging → scoring")
    add_ai_args(p_process)
    p_process.set_defaults(func=cmd_process)

    p_process_full = sub.add_parser("process-full", help="по-статейный конвейер: full-text→суть→релевантность→тег→скоринг")
    add_ai_args(p_process_full)
    p_process_full.set_defaults(func=cmd_process_full)

    p_process_articles = sub.add_parser("process-articles", help="summary → tagging → scoring для выбранных article_id")
    p_process_articles.add_argument("article_id", nargs="+", type=int)
    p_process_articles.add_argument("--offline", action="store_true", help="детерминированная локальная заглушка без OpenAI API")
    p_process_articles.set_defaults(func=cmd_process_articles)

    p_enqueue_process = sub.add_parser("enqueue-process", help="поставить AI-обработку в очередь (external-ai при внешнем контуре), не выполняя локально")
    add_ai_args(p_enqueue_process)
    p_enqueue_process.set_defaults(func=cmd_enqueue_process)

    p_enqueue_recheck = sub.add_parser("enqueue-recheck", help="перепрогон релевантности по всей базе батчами через воркер (нерелевантные удаляются)")
    p_enqueue_recheck.add_argument("--batch-size", type=int, default=100, help="статей в одной задаче")
    p_enqueue_recheck.add_argument("--limit", type=int, default=0, help="потолок числа статей (0 = вся база)")
    p_enqueue_recheck.add_argument("--force", action="store_true", help="удалять даже статьи из сохранённых дайджестов")
    p_enqueue_recheck.add_argument("--dry-run", action="store_true", help="НЕ удалять — только посчитать и собрать превью отклонённого (recheck-dry-show)")
    p_enqueue_recheck.add_argument("--mark", action="store_true", help="нерелевантные ПОМЕЧАТЬ на удаление (pending_deletion), не удалять; потом recheck-purge")
    p_enqueue_recheck.set_defaults(func=cmd_enqueue_recheck)

    p_dry_show = sub.add_parser("recheck-dry-show", help="показать вердикты dry-run recheck-задачи (что бы срезалось)")
    p_dry_show.add_argument("job_id", type=int, help="id выполненной dry-run задачи")
    p_dry_show.add_argument("--limit", type=int, default=80, help="сколько отклонённых показать")
    p_dry_show.set_defaults(func=cmd_recheck_dry_show)

    p_marked = sub.add_parser("recheck-marked", help="показать помеченные на удаление (recheck --mark): счётчик + выборка")
    p_marked.add_argument("--limit", type=int, default=80, help="сколько показать")
    p_marked.set_defaults(func=cmd_recheck_marked)

    p_purge = sub.add_parser("recheck-purge", help="РАЗОВО физически удалить все помеченные (pending_deletion) статьи")
    p_purge.add_argument("--yes", action="store_true", help="подтвердить необратимое удаление")
    p_purge.add_argument("--force", action="store_true", help="удалять даже статьи из сохранённых дайджестов")
    p_purge.set_defaults(func=cmd_recheck_purge)

    p_unmark = sub.add_parser("recheck-unmark", help="снять пометку «на удаление» со всех статей (вернуть в ленту)")
    p_unmark.set_defaults(func=cmd_recheck_unmark)

    p_enqueue_translate = sub.add_parser("enqueue-translate", help="бэкфилл перевода заголовков (title_ru) по всей базе батчами через воркер")
    p_enqueue_translate.add_argument("--batch-size", type=int, default=100, help="статей в одной задаче")
    p_enqueue_translate.add_argument("--limit", type=int, default=0, help="потолок числа статей (0 = все без перевода)")
    p_enqueue_translate.set_defaults(func=cmd_enqueue_translate)

    p_enqueue_external = sub.add_parser("enqueue-external-scrape", help="фетч источников network_region=external через зарубежный воркер (no-op без FETCH_EXTERNAL_ENABLED)")
    p_enqueue_external.add_argument("--max-age-days", type=int, default=None, help="окно свежести статей (по умолчанию без ограничения)")
    p_enqueue_external.set_defaults(func=cmd_enqueue_external_scrape)

    p_enqueue_source_discovery = sub.add_parser(
        "enqueue-source-discovery",
        help="поставить автоматический поиск кандидатов источников в очередь",
    )
    p_enqueue_source_discovery.add_argument(
        "--topic",
        action="append",
        default=None,
        help="тема разведки; можно указать несколько раз",
    )
    p_enqueue_source_discovery.add_argument(
        "--topic-limit",
        type=int,
        default=3,
        help="сколько слабых тем взять автоматически, если --topic не задан",
    )
    p_enqueue_source_discovery.add_argument("--limit", type=int, default=10, help="кандидатов на тему")
    p_enqueue_source_discovery.add_argument(
        "--seed-url",
        action="append",
        default=None,
        help="URL-кандидат для проверки; можно указать несколько раз",
    )
    p_enqueue_source_discovery.add_argument(
        "--offline",
        action="store_true",
        help="не вызывать OpenAI для генерации поисковых запросов",
    )
    p_enqueue_source_discovery.add_argument(
        "--fetch-inspection",
        action="store_true",
        help="проверять найденные/seed URL HTTP-запросом",
    )
    p_enqueue_source_discovery.add_argument(
        "--test-parse",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="проверять, что из кандидата реально извлекаются статьи",
    )
    p_enqueue_source_discovery.add_argument(
        "--evaluate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="сразу прогонять найденных кандидатов через песочницу",
    )
    p_enqueue_source_discovery.add_argument(
        "--article-limit",
        type=int,
        default=5,
        help="сколько материалов кандидата брать для песочницы",
    )
    p_enqueue_source_discovery.set_defaults(func=cmd_enqueue_source_discovery)

    p_set_region = sub.add_parser("set-source-region", help="проставить network_region (auto|ru|external) источникам по id")
    p_set_region.add_argument("--ids", required=True, help="список id через запятую, напр. 16,84,64")
    p_set_region.add_argument("--region", required=True, help="auto|ru|external")
    p_set_region.set_defaults(func=cmd_set_source_region)

    p_dump_listing = sub.add_parser("source-dump-listing", help="выгрузить анкеры листинга источника (для подбора listing_selector у no_candidates)")
    p_dump_listing.add_argument("source_id", type=int, help="id источника")
    p_dump_listing.add_argument("--limit", type=int, default=40, help="сколько анкеров показать")
    p_dump_listing.add_argument("--render", action="store_true", help="форсить playwright-рендер (проверить, даёт ли JS-SPA статьи)")
    p_dump_listing.add_argument("--url", default=None, help="протестить произвольный URL вместо listing_url источника (не меняет БД)")
    p_dump_listing.set_defaults(func=cmd_source_dump_listing)

    p_recheck = sub.add_parser("recheck-relevance", help="локальный перепрогон релевантности (тесты/дамп; на проде — enqueue-recheck)")
    add_ai_args(p_recheck)
    p_recheck.add_argument("--force", action="store_true", help="удалять даже статьи из сохранённых дайджестов")
    p_recheck.add_argument("--max-articles", type=int, default=None, help="ограничить число проверенных статей")
    p_recheck.set_defaults(func=cmd_recheck_relevance)

    sub.add_parser("ai-cost-report", help="отчёт по токенам/стоимости AI-этапов").set_defaults(func=cmd_ai_cost_report)

    p_article_cost = sub.add_parser("ai-article-cost-report", help="стоимость полного AI-прогона одной статьи")
    p_article_cost.add_argument("--limit", type=int, default=20)
    p_article_cost.add_argument("--include-partial", action="store_true", help="показывать статьи с неполным циклом")
    p_article_cost.set_defaults(func=cmd_ai_article_cost_report)

    p_sources = sub.add_parser("sources", help="список источников")
    p_sources.add_argument("--search", default=None)
    p_sources.add_argument("--limit", type=int, default=50)
    p_sources.set_defaults(func=cmd_sources)

    p_source_candidate_add = sub.add_parser("source-candidate-add", help="добавить кандидата источника для разведки")
    p_source_candidate_add.add_argument("url")
    p_source_candidate_add.add_argument("--name", default=None)
    p_source_candidate_add.add_argument("--type", default="unknown", help="newsroom/media/company/rss/blog/unknown")
    p_source_candidate_add.add_argument("--topic", default=None)
    p_source_candidate_add.add_argument("--reason", default=None)
    p_source_candidate_add.add_argument("--confidence", type=float, default=None)
    p_source_candidate_add.add_argument("--status", default="new")
    p_source_candidate_add.add_argument("--expected-tag", action="append", default=None)
    p_source_candidate_add.add_argument("--comment", default=None)
    p_source_candidate_add.set_defaults(func=cmd_source_candidate_add)

    p_source_candidates = sub.add_parser("source-candidates", help="список кандидатов источников")
    p_source_candidates.add_argument("--status", default=None)
    p_source_candidates.add_argument("--topic", default=None)
    p_source_candidates.add_argument("--limit", type=int, default=50)
    p_source_candidates.add_argument("--json", action="store_true")
    p_source_candidates.set_defaults(func=cmd_source_candidates)

    p_source_candidate_triage = sub.add_parser("source-candidate-triage", help="очередь решений по кандидатам источников")
    p_source_candidate_triage.add_argument("--limit", type=int, default=20)
    p_source_candidate_triage.add_argument("--json", action="store_true")
    p_source_candidate_triage.set_defaults(func=cmd_source_candidate_triage)

    p_source_candidate_test = sub.add_parser(
        "source-candidate-test",
        help="проверить кандидата источника пробным read-only парсингом",
    )
    p_source_candidate_test.add_argument("candidate_id", type=int)
    p_source_candidate_test.add_argument("--article-limit", type=int, default=5)
    p_source_candidate_test.add_argument("--ai-recommendation", action="store_true",
                                         help="попросить OpenAI уточнить рекомендацию; по умолчанию не вызывается")
    p_source_candidate_test.add_argument("--dry-run", action="store_true",
                                         help="не обновлять кандидата в БД")
    p_source_candidate_test.add_argument("--json", action="store_true")
    p_source_candidate_test.set_defaults(func=cmd_source_candidate_test)

    p_source_candidate_evaluate = sub.add_parser(
        "source-candidate-evaluate",
        help="собрать тестовые материалы кандидата в песочницу и прогнать AI pipeline",
    )
    p_source_candidate_evaluate.add_argument("candidate_id", type=int)
    p_source_candidate_evaluate.add_argument("--article-limit", type=int, default=5)
    p_source_candidate_evaluate.add_argument("--offline", action=argparse.BooleanOptionalAction, default=True,
                                             help="offline AI по умолчанию; --no-offline вызовет OpenAI")
    p_source_candidate_evaluate.add_argument("--collect", action=argparse.BooleanOptionalAction, default=True,
                                             help="собирать материалы кандидата в песочницу")
    p_source_candidate_evaluate.add_argument("--process", action=argparse.BooleanOptionalAction, default=True,
                                             help="прогонять материалы песочницы через pipeline")
    p_source_candidate_evaluate.add_argument("--json", action="store_true")
    p_source_candidate_evaluate.set_defaults(func=cmd_source_candidate_evaluate)

    p_source_candidate_approve = sub.add_parser(
        "source-candidate-approve",
        help="одобрить кандидата и создать/обновить настоящий источник",
    )
    p_source_candidate_approve.add_argument("candidate_id", type=int)
    p_source_candidate_approve.add_argument("--name", default=None)
    p_source_candidate_approve.add_argument("--source-type", default="Discovered")
    p_source_candidate_approve.add_argument("--parse-strategy", choices=["rss", "request", "playwright"], default=None)
    p_source_candidate_approve.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=False,
                                            help="по умолчанию источник создается выключенным")
    p_source_candidate_approve.add_argument("--category", default=None)
    p_source_candidate_approve.add_argument("--priority", type=float, default=1.0)
    p_source_candidate_approve.add_argument("--network-region", choices=["auto", "ru", "external"], default="auto")
    p_source_candidate_approve.set_defaults(func=cmd_source_candidate_approve)

    sub.add_parser("seed-signal-topics", help="создать дефолтные темы радара сигналов").set_defaults(func=cmd_seed_signal_topics)

    p_discover_signals = sub.add_parser("discover-signals", help="найти технологические сигналы поверх обработанных статей")
    p_discover_signals.add_argument("--topic", default=None, help="тема радара; по умолчанию все активные темы")
    p_discover_signals.add_argument("--days", type=int, default=14)
    p_discover_signals.add_argument("--limit", type=int, default=80, help="сколько evidence-статей взять на тему")
    p_discover_signals.add_argument("--min-score", type=float, default=40)
    p_discover_signals.add_argument("--max-signals", type=int, default=10)
    p_discover_signals.add_argument("--web", action="store_true",
                                    help="искать evidence через web search provider, без привязки к sources")
    p_discover_signals.add_argument("--web-only", action="store_true",
                                    help="использовать только web evidence и не брать статьи из локальных sources")
    p_discover_signals.add_argument("--web-query-limit", type=int, default=8,
                                    help="сколько поисковых запросов сделать на тему в --web режиме")
    p_discover_signals.add_argument("--research-rounds", type=int, default=2,
                                    help="сколько раундов research-loop: 1=только широкий поиск, 2=поиск+follow-up")
    p_discover_signals.add_argument("--web-fulltext-limit", type=int, default=20,
                                    help="сколько web-результатов докачивать целиком вместо сниппета поиска; 0 отключает")
    p_discover_signals.add_argument("--offline", action=argparse.BooleanOptionalAction, default=True,
                                    help="offline heuristic вместо LLM-judge")
    p_discover_signals.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True,
                                    help="не писать signals/signal_evidence в БД")
    p_discover_signals.add_argument("--json", action="store_true")
    p_discover_signals.set_defaults(func=cmd_discover_signals)

    p_enqueue_signals = sub.add_parser("enqueue-signal-discovery", help="поставить радар сигналов в background_jobs")
    p_enqueue_signals.add_argument("--topic", default=None)
    p_enqueue_signals.add_argument("--days", type=int, default=14)
    p_enqueue_signals.add_argument("--limit", type=int, default=80)
    p_enqueue_signals.add_argument("--min-score", type=float, default=40)
    p_enqueue_signals.add_argument("--max-signals", type=int, default=10)
    p_enqueue_signals.add_argument("--web", action="store_true",
                                   help="искать evidence через web search provider, без привязки к sources")
    p_enqueue_signals.add_argument("--web-only", action="store_true",
                                   help="использовать только web evidence и не брать статьи из локальных sources")
    p_enqueue_signals.add_argument("--web-query-limit", type=int, default=8)
    p_enqueue_signals.add_argument("--research-rounds", type=int, default=2)
    p_enqueue_signals.add_argument("--web-fulltext-limit", type=int, default=20)
    p_enqueue_signals.add_argument("--offline", action=argparse.BooleanOptionalAction, default=True)
    p_enqueue_signals.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
    p_enqueue_signals.set_defaults(func=cmd_enqueue_signal_discovery)

    p_enqueue_daily_signals = sub.add_parser(
        "enqueue-daily-signal-discovery",
        help="поставить ежедневный web-only радар сигналов в очередь один раз за окно",
    )
    p_enqueue_daily_signals.add_argument("--force", action="store_true", help="игнорировать daily-idempotency")
    p_enqueue_daily_signals.set_defaults(func=cmd_enqueue_daily_signal_discovery)

    p_signals = sub.add_parser("signals", help="список найденных технологических сигналов")
    p_signals.add_argument("--maturity", choices=["watch", "shortlist", "proven", "reject"], default=None)
    p_signals.add_argument("--theme", default=None)
    p_signals.add_argument("--limit", type=int, default=50)
    p_signals.add_argument("--evidence-limit", type=int, default=3)
    p_signals.add_argument("--json", action="store_true")
    p_signals.set_defaults(func=cmd_signals)

    p_signal_feedback = sub.add_parser("signal-feedback", help="записать событие обратной связи по сигналу")
    p_signal_feedback.add_argument("event_type", choices=[
        "added_to_digest",
        "marked_noise",
        "marked_duplicate",
        "tag_changed",
        "score_changed",
        "status_changed",
        "comment_added",
    ])
    p_signal_feedback.add_argument("--article-id", type=int, default=None)
    p_signal_feedback.add_argument("--signal-id", type=int, default=None)
    p_signal_feedback.add_argument("--signal-evidence-id", type=int, default=None)
    p_signal_feedback.add_argument("--source-url", default=None)
    p_signal_feedback.add_argument("--signal-title", default=None)
    p_signal_feedback.add_argument("--user-id", type=int, default=None)
    p_signal_feedback.add_argument("--old-value", default=None)
    p_signal_feedback.add_argument("--new-value", default=None)
    p_signal_feedback.add_argument("--comment", default=None)
    p_signal_feedback.add_argument("--verdict", choices=["approved", "reject", "duplicate", "needs_context"], default=None)
    p_signal_feedback.add_argument("--reason", default=None)
    p_signal_feedback.add_argument("--corrected-title", default=None)
    p_signal_feedback.add_argument("--corrected-thesis", default=None)
    p_signal_feedback.add_argument("--duplicate-of-signal-id", type=int, default=None)
    p_signal_feedback.set_defaults(func=cmd_signal_feedback)

    p_import_signal_feedback = sub.add_parser("import-signal-feedback", help="импортировать ОС по сигналам из CSV")
    p_import_signal_feedback.add_argument("path")
    p_import_signal_feedback.add_argument("--user-id", type=int, default=None)
    p_import_signal_feedback.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    p_import_signal_feedback.add_argument("--json", action="store_true")
    p_import_signal_feedback.set_defaults(func=cmd_import_signal_feedback)

    p_retire_hints = sub.add_parser(
        "retire-signal-query-hints",
        help="погасить поисковые подсказки радара, выведенные из отклонённых сигналов",
    )
    p_retire_hints.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    p_retire_hints.add_argument("--json", action="store_true")
    p_retire_hints.set_defaults(func=cmd_retire_signal_query_hints)

    p_export_signal_training = sub.add_parser(
        "export-signal-training-jsonl",
        help="выгрузить snapshot'ы сигналов с ОС в JSONL для eval/training",
    )
    p_export_signal_training.add_argument("--path", default=None)
    p_export_signal_training.add_argument("--limit", type=int, default=1000)
    p_export_signal_training.add_argument("--all", action="store_true", help="включить примеры без человеческой ОС")
    p_export_signal_training.add_argument("--verdict", choices=["approved", "reject", "duplicate", "needs_context"], default=None)
    p_export_signal_training.add_argument("--json", action="store_true")
    p_export_signal_training.set_defaults(func=cmd_export_signal_training_jsonl)

    p_export_signal_context = sub.add_parser(
        "export-signal-context",
        help="выгрузить переносимый bundle сигналов/evidence/памяти/ОС для переноса на сервер",
    )
    p_export_signal_context.add_argument("--path", default=None)
    p_export_signal_context.add_argument("--include-rejected", action="store_true")
    p_export_signal_context.add_argument("--json", action="store_true")
    p_export_signal_context.set_defaults(func=cmd_export_signal_context)

    p_import_signal_context = sub.add_parser(
        "import-signal-context",
        help="импортировать переносимый bundle сигналов/evidence/памяти/ОС",
    )
    p_import_signal_context.add_argument("path")
    p_import_signal_context.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    p_import_signal_context.add_argument("--json", action="store_true")
    p_import_signal_context.set_defaults(func=cmd_import_signal_context)

    p_legacy_signal_feedback = sub.add_parser("article-signal-feedback", help="legacy: записать ОС по article_id")
    p_legacy_signal_feedback.add_argument("article_id", type=int)
    p_legacy_signal_feedback.add_argument("event_type", choices=[
        "added_to_digest",
        "marked_noise",
        "marked_duplicate",
        "tag_changed",
        "score_changed",
        "status_changed",
        "comment_added",
    ])
    p_legacy_signal_feedback.add_argument("--signal-id", type=int, default=None)
    p_legacy_signal_feedback.add_argument("--signal-evidence-id", type=int, default=None)
    p_legacy_signal_feedback.add_argument("--source-url", default=None)
    p_legacy_signal_feedback.add_argument("--signal-title", default=None)
    p_legacy_signal_feedback.add_argument("--user-id", type=int, default=None)
    p_legacy_signal_feedback.add_argument("--old-value", default=None)
    p_legacy_signal_feedback.add_argument("--new-value", default=None)
    p_legacy_signal_feedback.add_argument("--comment", default=None)
    p_legacy_signal_feedback.add_argument("--verdict", choices=["approved", "reject", "duplicate", "needs_context"], default=None)
    p_legacy_signal_feedback.add_argument("--reason", default=None)
    p_legacy_signal_feedback.add_argument("--corrected-title", default=None)
    p_legacy_signal_feedback.add_argument("--corrected-thesis", default=None)
    p_legacy_signal_feedback.add_argument("--duplicate-of-signal-id", type=int, default=None)
    p_legacy_signal_feedback.set_defaults(func=cmd_signal_feedback)

    p_source_quality = sub.add_parser("source-quality", help="посчитать качество источников за период")
    p_source_quality.add_argument("--days", type=int, default=30)
    p_source_quality.add_argument("--limit", type=int, default=30)
    p_source_quality.add_argument("--snapshot", action="store_true", help="сохранить срез в source_quality_snapshots")
    p_source_quality.add_argument("--json", action="store_true")
    p_source_quality.set_defaults(func=cmd_source_quality)

    p_agent_query_memory = sub.add_parser("agent-query-memory", help="показать память поисковых формулировок агента")
    p_agent_query_memory.add_argument("--status", choices=["active", "muted", "all"], default="active")
    p_agent_query_memory.add_argument("--limit", type=int, default=20)
    p_agent_query_memory.add_argument("--json", action="store_true")
    p_agent_query_memory.set_defaults(func=cmd_agent_query_memory)

    p_agent_readiness = sub.add_parser("agent-readiness", help="проверить готовность агента разведки источников")
    p_agent_readiness.add_argument("--json", action="store_true")
    p_agent_readiness.set_defaults(func=cmd_agent_readiness)

    p_discover_sources = sub.add_parser("discover-sources", help="MVP агента разведки источников по теме")
    p_discover_sources.add_argument("--topic", required=True, help="тема разведки, например 'роботизация бурения'")
    p_discover_sources.add_argument("--limit", type=int, default=20)
    p_discover_sources.add_argument("--seed-url", action="append", default=None,
                                    help="URL кандидата для проверки; можно указать несколько раз")
    p_discover_sources.add_argument("--offline", action="store_true",
                                    help="не вызывать OpenAI, сгенерировать детерминированные запросы")
    p_discover_sources.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True,
                                    help="не писать кандидатов/действия в БД (по умолчанию включено)")
    p_discover_sources.add_argument("--fetch-inspection", action="store_true",
                                    help="попробовать HTTP-проверку seed-url")
    p_discover_sources.add_argument("--test-parse", action="store_true",
                                    help="read-only пробный парсинг кандидатов без записи в articles")
    p_discover_sources.add_argument("--json", action="store_true")
    p_discover_sources.set_defaults(func=cmd_discover_sources)

    p_agent_plan = sub.add_parser("agent-plan", help="построить план действий агента разведки источников")
    p_agent_plan.add_argument("--days", type=int, default=30, help="период анализа базы")
    p_agent_plan.add_argument("--target-per-topic", type=int, default=10, help="целевое число релевантных сигналов на тему")
    p_agent_plan.add_argument("--topic-limit", type=int, default=5, help="сколько тем рассмотреть")
    p_agent_plan.add_argument("--candidate-limit", type=int, default=10, help="кандидатов на тему в discovery-задаче")
    p_agent_plan.add_argument("--max-actions", type=int, default=5, help="максимум действий в плане")
    p_agent_plan.add_argument("--no-memory", action="store_true", help="не сохранять обновления памяти агента")
    p_agent_plan.add_argument("--enqueue", action="store_true", help="поставить discover-задачи из плана в очередь")
    p_agent_plan.add_argument("--offline", action=argparse.BooleanOptionalAction, default=True,
                              help="offline-режим для discovery-задач")
    p_agent_plan.add_argument("--evaluate", action=argparse.BooleanOptionalAction, default=True,
                              help="оценивать найденных кандидатов")
    p_agent_plan.add_argument("--json", action="store_true")
    p_agent_plan.set_defaults(func=cmd_agent_plan)

    p_enqueue_agent_plan = sub.add_parser("enqueue-agent-plan", help="поставить планирование агента в очередь")
    p_enqueue_agent_plan.add_argument("--days", type=int, default=30, help="период анализа базы")
    p_enqueue_agent_plan.add_argument("--target-per-topic", type=int, default=10, help="целевое число релевантных сигналов на тему")
    p_enqueue_agent_plan.add_argument("--topic-limit", type=int, default=5, help="сколько тем рассмотреть")
    p_enqueue_agent_plan.add_argument("--candidate-limit", type=int, default=10, help="кандидатов на тему в discovery-задаче")
    p_enqueue_agent_plan.add_argument("--max-actions", type=int, default=5, help="максимум действий в плане")
    p_enqueue_agent_plan.add_argument("--no-memory", action="store_true", help="не сохранять обновления памяти агента")
    p_enqueue_agent_plan.add_argument("--offline", action=argparse.BooleanOptionalAction, default=True,
                                      help="offline-режим для discovery-задач")
    p_enqueue_agent_plan.add_argument("--evaluate", action=argparse.BooleanOptionalAction, default=True,
                                      help="оценивать найденных кандидатов")
    p_enqueue_agent_plan.set_defaults(func=cmd_enqueue_agent_plan)

    p_agent_loop = sub.add_parser("agent-loop", help="запустить итеративный agent loop разведки источников")
    add_agent_loop_args(p_agent_loop)
    p_agent_loop.add_argument("--json", action="store_true")
    p_agent_loop.set_defaults(func=cmd_agent_loop)

    p_enqueue_agent_loop = sub.add_parser("enqueue-agent-loop", help="поставить agent loop разведки источников в очередь")
    add_agent_loop_args(p_enqueue_agent_loop)
    p_enqueue_agent_loop.set_defaults(func=cmd_enqueue_agent_loop)

    p_source_health = sub.add_parser("source-health", help="вердикты покрытия источников: ok/stale/no_articles/disabled")
    p_source_health.add_argument("--stale-days", type=int, default=3)
    p_source_health.add_argument("--limit", type=int, default=300)
    p_source_health.add_argument("--verdict", choices=["ok", "stale", "no_articles", "disabled"], default=None)
    p_source_health.set_defaults(func=cmd_source_health)

    p_candidates = sub.add_parser("article-candidates", help="найти статьи-кандидаты по ключевым словам")
    p_candidates.add_argument("query", help="ключевые слова через пробел")
    p_candidates.add_argument("--limit", type=int, default=20)
    p_candidates.set_defaults(func=cmd_article_candidates)

    p_source_enable = sub.add_parser("source-enable", help="включить/выключить источник")
    p_source_enable.add_argument("source_id", type=int)
    p_source_enable.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    p_source_enable.set_defaults(func=cmd_source_enable)

    p_source_add = sub.add_parser("source-add-rss", help="добавить RSS-источник вручную")
    p_source_add.add_argument("name")
    p_source_add.add_argument("rss_url")
    p_source_add.add_argument("--url", default=None)
    p_source_add.add_argument("--priority", type=float, default=1.0)
    p_source_add.add_argument("--category", default=None)
    p_source_add.add_argument("--frequency", default=None, help="частота мониторинга")
    p_source_add.set_defaults(func=cmd_source_add_rss)

    p_source_audit = sub.add_parser("source-audit", help="аудит ВСЕХ источников: текущая ссылка + почему не парсится (read-only)")
    p_source_audit.add_argument("--limit", type=int, default=1000, help="максимум источников")
    p_source_audit.add_argument("--probe-limit", type=int, default=3, help="сколько статей-кандидатов пробовать на источник")
    p_source_audit.add_argument("--workers", type=int, default=8, help="параллельных проб")
    p_source_audit.add_argument("--strategy", help="фильтр по стратегии (request/rss/playwright/telegram)")
    p_source_audit.add_argument("--enabled-only", action="store_true", help="только включённые источники")
    p_source_audit.add_argument("--json", action="store_true", help="полный JSON-отчёт")
    p_source_audit.set_defaults(func=cmd_source_audit)

    p_source_diag = sub.add_parser("source-diagnose", help="read-only диагностика источника по source_id")
    p_source_diag.add_argument("source_id", type=int)
    p_source_diag.add_argument("--limit", type=int, default=5, help="сколько кандидатов/постов проверить")
    p_source_diag.set_defaults(func=cmd_source_diagnose)

    p_digest = sub.add_parser("digest-content", help="собрать digest_content.json из обработанных статей")
    p_digest.add_argument("month", help="YYYY-MM")
    p_digest.add_argument("--output", default="digest_content.generated.json")
    p_digest.add_argument("--html-output", default=None, help="дополнительно собрать email-ready HTML")
    p_digest.add_argument("--limit", type=int, default=20)
    p_digest.add_argument("--min-score", type=float, default=60)
    p_digest.set_defaults(func=cmd_digest_content)

    p_digest_save = sub.add_parser("digest-save", help="сохранить draft monthly_digest из текущих digest-кандидатов")
    p_digest_save.add_argument("month", help="YYYY-MM")
    p_digest_save.add_argument("--limit", type=int, default=20)
    p_digest_save.add_argument("--min-score", type=float, default=60)
    p_digest_save.set_defaults(func=cmd_digest_save)

    p_jobs_worker = sub.add_parser("jobs-worker", help="обрабатывать background_jobs из БД")
    p_jobs_worker.add_argument("--poll-seconds", type=float, default=None)
    p_jobs_worker.add_argument("--stale-minutes", type=int, default=None)
    p_jobs_worker.add_argument("--queue", action="append", default=None,
                               help="очередь для обработки; можно указать несколько раз")
    p_jobs_worker.add_argument("--once", action="store_true", help="забрать одну доступную пачку и выйти, если задач нет")
    p_jobs_worker.set_defaults(func=cmd_jobs_worker)

    p_external_worker = sub.add_parser("external-worker", help="обрабатывать external-* задачи через HTTP API core")
    p_external_worker.add_argument("--core-api-url", default=None)
    p_external_worker.add_argument("--token", default=None)
    p_external_worker.add_argument("--worker-id", default=None)
    p_external_worker.add_argument("--queue", action="append", default=None)
    p_external_worker.add_argument("--capability", action="append", default=None)
    p_external_worker.add_argument("--poll-seconds", type=float, default=None)
    p_external_worker.add_argument("--once", action="store_true")
    p_external_worker.set_defaults(func=cmd_external_worker)

    p_jobs_requeue = sub.add_parser("jobs-requeue-stale", help="вернуть зависшие running-задачи обратно в queued")
    p_jobs_requeue.add_argument("--stale-minutes", type=int, default=None)
    p_jobs_requeue.set_defaults(func=cmd_jobs_requeue_stale)

    p_external_status = sub.add_parser("external-queues-status", help="показать состояние external-* очередей")
    p_external_status.add_argument("--json", action="store_true")
    p_external_status.set_defaults(func=cmd_external_queues_status)

    p_maintenance_cleanup = sub.add_parser(
        "maintenance-cleanup",
        help="удалить истекшие сессии и старые terminal-записи служебных таблиц",
    )
    p_maintenance_cleanup.add_argument("--background-job-days", type=int, default=None)
    p_maintenance_cleanup.add_argument("--export-job-days", type=int, default=None)
    p_maintenance_cleanup.set_defaults(func=cmd_maintenance_cleanup)

    p_bench = sub.add_parser(
        "bench-readiness",
        help="read-only benchmark основных prod-запросов без парсинга и AI",
    )
    p_bench.add_argument("--iterations", type=int, default=5)
    p_bench.add_argument("--articles-limit", type=int, default=1000)
    p_bench.add_argument("--source-limit", type=int, default=300)
    p_bench.add_argument("--jobs-limit", type=int, default=100)
    p_bench.add_argument("--month", default=None, help="YYYY-MM для digest_candidates; пусто = все")
    p_bench.add_argument("--digest-limit", type=int, default=100)
    p_bench.add_argument("--min-score", type=float, default=0)
    p_bench.add_argument("--warn-ms", type=float, default=800)
    p_bench.add_argument("--json", action="store_true", help="вывести машинно-читаемый JSON")
    p_bench.set_defaults(func=cmd_bench_readiness)

    p_source_retry = sub.add_parser(
        "source-retry",
        help="форс-парсинг источников с вердиктом stale/no_articles",
    )
    p_source_retry.add_argument(
        "--verdict", nargs="+", choices=["stale", "no_articles"],
        default=None, help="вердикты для обработки (по умолчанию: stale no_articles)",
    )
    p_source_retry.add_argument("--stale-days", type=int, default=3)
    p_source_retry.add_argument("--max-age-days", type=int, default=None)
    p_source_retry.set_defaults(func=cmd_source_retry)

    p_pp = sub.add_parser(
        "parse-process",
        help="стриминг-пайплайн: parse + AI-обработка новых статей параллельно",
    )
    p_pp.add_argument("--max-age-days", type=int, default=None)
    p_pp.add_argument("--source-id", type=int, default=None)
    p_pp.add_argument("--workers", type=int, default=5, help="воркеры парсинга (осторожно с RAM)")
    p_pp.add_argument("--process-limit", type=int, default=20, help="статей за один батч обработки")
    p_pp.add_argument("--poll-interval", type=int, default=10, help="секунд между опросами новых статей")
    p_pp.add_argument("--offline", action="store_true")
    p_pp.set_defaults(func=cmd_parse_process)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
