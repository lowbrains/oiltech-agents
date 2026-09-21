# Хендоф 21.09.2026 — research-loop pivot, полный текст evidence, batch review сигналов

Коммит `655cdbe` (запушен в `origin/main`). Не задеплоено ни на РФ-ядро, ни на NL-воркер —
это инструкция, что сделать перед следующим ежедневным прогоном радара (07:15 МСК).

---

## Что изменилось

Все три пункта — в `oiltech_digest/signal_discovery.py`, работают внутри `run_discovery`,
который реально выполняется **на NL external worker** (очередь `external-ai`), а не на РФ-ядре.

1. **Adaptive pivot в research-loop.** Раньше после любого раунда с результатами агент всегда
   делал follow-up (сужал запрос до найденной компании/технологии) — на слабой выдаче это
   просто повторяло тот же шум другими словами. Теперь после каждого раунда считается
   `_round_signal_quality`: есть ли нефтегазовый контекст и признак события (контракт/пилот/
   KPI/запуск). Если нет — следующий раунд идёт в pivot (свежая пара термин×угол), а не в
   follow-up по мусору.
2. **Полный текст страницы вместо сниппета поиска.** Судья и кластеризация видели только
   title+snippet поисковика (1-2 обрубленных предложения). Теперь для первых
   `web_fulltext_limit` кандидатов (дефолт 20, `0` отключает) докачивается страница целиком
   через `probe_url` + `parse_article_page` — той же парой, что использует `source_discovery`
   (включая RU/external-роутинг через прокси). Неудача (404/антибот/короткий текст) мягко
   откатывается на исходный сниппет, кандидат не теряется. `published_at` теперь виден судье
   в промпте — раньше customer-правило «событие старое → reject» нечем было проверить.
3. **Batch review пачки сигналов темы.** Judge оценивает каждый кластер темы в изоляции и не
   видит остальных кандидатов — не может заметить, что кандидат №3 пересказывает то же
   событие, что №1, или что кандидат не тянет на сигнал именно на фоне остальных. Новый
   LLM-вызов на тему (после всех кластеров, до записи) убирает такие дубли/шум и оценивает
   `interest_score` — насколько сигнал интересен на фоне пачки, а не сам по себе. Финальная
   сортировка в `apply_discovery` теперь по `interest_score`, а не по сырому score
   судьи-в-изоляции. Offline-режим (`--offline`) — грубый rules-фоллбэк без сравнения пачки
   по смыслу (дедуп по Jaccard заголовка+компаний, interest_score = score судьи как есть).

Новые env-переменные (см. `.env.example`), обе с рабочими дефолтами — трогать не обязательно:

```bash
SIGNAL_DISCOVERY_RESEARCH_ROUNDS=2       # уже было; pivot работает внутри тех же раундов
SIGNAL_DISCOVERY_WEB_FULLTEXT_LIMIT=20   # 0 отключает докачку полного текста
```

---

## Как обновить

### 1. РФ-ядро (`/root/oiltech-agents`, проект `oiltech-agents`)

> ⚠️ **Исправлено 21.09 после инцидента.** Прежняя команда `up -d --build` без имени
> сервиса подняла ВСЕ сервисы агентов, включая планировщик, который намеренно не
> запускался: за 8,5 ч он задублировал сбор MVP-1 ($4,48 ИИ, 208 задач без потребителя)
> и выполнил радар дня прямо на РФ-ядре → OpenAI 403 → радар 21.09 потерян.
> Катить ядро агентов — ТОЛЬКО `agents-app`; конвейер (`scheduler`, `tasks`, `worker`,
> `playwright-worker`) агентам не нужен и теперь стоит под профилем `pipeline`.

```bash
cd /root/oiltech-agents
git fetch origin && git reset --hard origin/main
docker compose -f docker-compose.yml -f docker-compose.server.yml build agents-app
docker compose -f docker-compose.yml -f docker-compose.server.yml up -d --no-deps agents-app
docker ps --format '{{.Names}}' | grep oiltech_agents_   # только app, pg, docs — scheduler/tasks/worker НЕТ
```

`--no-deps` не запускает `bootstrap`, поэтому новые колонки схемы накатываются отдельно и
ДО выката кода, который в них пишет — точечным `ALTER TABLE … ADD COLUMN IF NOT EXISTS`
из конца `schema.sql` через `psql` в `oiltech_agents_pg` (так же 18.09 ставили
`merged_into_signal_id`). Полный `init-db` на живой базе 17.09 уже ловил дедлок с воркером.
Ядро только ставит задачу радара (`signal_discovery.build_external_payload`) и применяет
результат (`apply_external_result`) — само не ищет и не зовёт OpenAI.

### 2. NL external worker — обязателен, без него новый код не выполнится

Здесь реально крутится research-loop, докачка текста и все LLM-вызовы (judge + batch review).
Если не обновить воркер, задачи продолжат идти на старой сборке образа — молча, без ошибки.

SSH на NL-сервер нет у этой сессии (см. `CLAUDE.md`) — пересобирает владелец воркера:

```bash
cd /root/oiltech-agents
git fetch origin && git reset --hard origin/main
docker compose -p oiltech-agents-worker -f docker-compose.external-worker.yml up -d --build
```

### 3. Проверка после обоих обновлений

> ⚠️ **Исправлено 21.09.** `discover-signals` гоняет радар ВНУТРИ процесса на РФ-ядре →
> OpenAI отвечает 403 `unsupported_country_region_territory` всегда. Проверять только
> через очередь — задачу возьмёт NL-воркер:

```bash
docker exec oiltech_agents_app python -m oiltech_digest.cli enqueue-signal-discovery \
  --topic "Бурение, направленное бурение, буровые растворы и буровое оборудование" \
  --web --web-only --limit 10 --max-signals 2 --no-offline --dry-run
# затем в базе агентов:
# SELECT status, result_json->'applied'->'topics' FROM background_jobs WHERE id = <id>;
```

В `applied.topics[0]` итога задачи: `research_modes` (например `["initial","pivot"]`),
`fulltext` (`attempted/fetched/too_short/failed/skipped_budget`), `batch_review`
(`status/source/reviewed/dropped/duplicates`). Ниже — те же поля в полном ответе CLI
(локальный прогон на машине с доступом к OpenAI; пути — внутри `topic_results[0]`):

- `topic_results[0].web_search.research_rounds` — у каждого раунда есть
  `mode`/`quality`/`next_mode`; на слабой выдаче `next_mode` должен быть `pivot`, а не
  всегда `followup`.
- `topic_results[0].web_search.fulltext` — `{attempted, fetched, too_short, failed}`;
  `fetched > 0`, если докачка вообще смогла что-то достать.
- `topic_results[0].batch_review` — `{status: "ok", source: "ai", reviewed, dropped,
  interest_scores}`; `source` должен быть `"ai"` (не `"rules"`) при `--no-offline`.
- `topic_results[0].signals[*].interest_score` / `why_interesting` — заполнены у сигналов,
  переживших batch review.

Если `SOURCE_DISCOVERY_SEARCH_PROVIDER`/`BRAVE_SEARCH_API_KEY` не настроены на воркере —
`web_search.status` будет `not_configured`, и весь остальной конвейер (pivot, fulltext,
batch review) просто не получит входных данных для проверки; это не баг новой сборки.

---

## Что НЕ трогали в этом заходе

- Ограничение доли одной компании в финальной суточной выдаче (`config.max_signals`) —
  отложено, следующий кандидат в очереди.
- Верификация вендорских цифр независимым источником — отдельная задача, не начата.
