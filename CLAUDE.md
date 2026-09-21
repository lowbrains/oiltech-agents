# OilTech Agents

Агентная ветвь проекта мониторинга нефтесервисных публикаций.
Заказчик — ООО «Нефтесервисные решения» (Газпром нефть).

## Чем этот репозиторий отличается от `oiltech-digest`

Это **форк MVP-1, сделанный 15.09.2026** — полная копия кода и истории
(272 коммита, авторство Германа сохранено). С этой точки два проекта расходятся:

| | `electromop/oiltech-digest` | `lowbrains/oiltech-agents` (здесь) |
|---|---|---|
| Роль | MVP-1: рабочая платформа заказчика | этап 2: агентная система |
| Что меняем | сопровождение и правки Виктора | пайплайн **поэтапно заменяется агентами** |
| Стабильность | не ломать, на нём смотрит заказчик | площадка для перестройки |

Правки Виктора по текущей платформе (теги, скоринг, источники, лента)
идут в `oiltech-digest`, а не сюда. Сюда — всё, что заменяет пайплайн агентами.

## Что уже агентное (перенесено форком целиком)

- `oiltech_digest/source_discovery/` — агент поиска источников (11 файлов)
- `oiltech_digest/signal_discovery.py`, `signal_feedback.py`, `signal_training.py` — радар сигналов и обучение на ОС
- `oiltech_digest/processing/domain_glossary.py` + `.json` — инженерный перевод по глоссарию
- `frontend/src/features/sources/SourceAgentPage.tsx`, `SourceCandidatesPage.tsx`
- `frontend/src/features/signals/SignalRadarPage.tsx`
- 15 тестовых файлов из 46: `test_source_discovery_*`, `test_signal_*`, `test_source_health/quality/regularity`

## ⚠️ Выкат ядра агентов на РФ — только так (инцидент 20–21.09)

```bash
cd /root/oiltech-agents && git fetch origin && git reset --hard origin/main
docker compose -f docker-compose.yml -f docker-compose.server.yml build agents-app
docker compose -f docker-compose.yml -f docker-compose.server.yml up -d --no-deps agents-app
```

- **Никогда** `up -d --build` без имени сервиса: 20.09 так поднялся планировщик агентов,
  за 8,5 ч задублировал сбор MVP-1 ($4,48 ИИ, 208 задач без потребителя) и выполнил радар
  дня на РФ-ядре (OpenAI 403 → радар 21.09 потерян). Теперь конвейер (`tasks`, `worker`,
  `playwright-worker`, `scheduler`) — под профилем `pipeline`, `docs` — под `docs`: голый
  `up` поднимает только `db`, `bootstrap`, `agents-app` (сторож — `tests/test_signal_radar_robustness.py`).
- Внешние очереди (`external-*`) не исполняются в процессе, который ставит задачу, даже при
  `BACKGROUND_JOB_INLINE=1` (`background_jobs.runs_inline`).
- `--no-deps` не запускает `bootstrap`: новые колонки — точечным `ALTER … IF NOT EXISTS`
  через `psql` ДО выката кода, который в них пишет.
- Проверка радара — только через очередь (`enqueue-signal-discovery … --dry-run`), не
  `discover-signals`: тот исполняется на РФ и всегда получает 403 от OpenAI.
- NL-воркер пересобирает владелец (команда — в разделе «Радар сигналов через NL»).

## Развязка и выкат (18.09)

**Своя БД — сделано.** Отдельный экземпляр Postgres (`oiltech_agents_pg`, база
`oiltech_agents`), корпус перенесён дампом MVP-1 18.09: 31 101 статья, 173 источника,
45 сигналов, 59 оценок Виктора, 18 пользователей — счётчики сверены с MVP-1 один в один.
Внешние ключи агент↔ядро теперь смотрят в свою копию `articles`/`sources`/`tags`.

**Развёрнуто на РФ-сервере рядом с MVP-1** (`/root/oiltech-agents`): имя проекта
`oiltech-agents`, контейнеры `oiltech_agents_*`, приложение на `127.0.0.1:8100`.
Свой `.env` (база агентов, **свой** хеш токена воркера; сам токен — в
`/root/oiltech-agents/.worker-token`, права 600, в чат не выводится).

⚠️ **Имена в сети MVP-1 обязаны быть уникальными.** Сервис называется `agents-app`, а
не `app`, база — по имени контейнера `oiltech_agents_pg`, а не `db`: compose даёт
контейнеру имя сервиса псевдонимом в каждой сети, и два `app` в сети MVP-1 заставили бы
Caddy раскидывать запросы заказчика между MVP-1 и агентами.

**Поддомен работает с 18.09: https://agents.oiltech-digest.ru** — сертификат Let's
Encrypt, отдаётся фронтенд агентов с новым радаром (8 вердиктов, ID сигнала,
источник). Проверено снаружи; основной сайт MVP-1 не задет. Сделано:

1. DNS A-запись `agents` → 109.68.213.12 (владелец, Timeweb).
2. `agents-app` подключён к сети MVP-1 (`docker-compose.server.yml`); проверено, что
   `getent hosts app` в контейнере Caddy — один адрес, приложение MVP-1.
3. Блок в `Caddyfile` MVP-1 (коммит `1119aa7` в electromop/oiltech-digest).
   ⚠️ `Caddyfile` смонтирован ОДНИМ ФАЙЛОМ: после `git reset` контейнер видит старую
   копию, и `caddy reload` перечитал бы её. Грузить без простоя так:
   `docker exec -i oiltech_caddy caddy reload --config /dev/stdin --adapter caddyfile < Caddyfile`.

## Радар сигналов через NL (18.09 вечер, `3b08319`…`e29b473`)

Радар не отработал по расписанию ни разу с 13.09 (134 запуска, все 403 с РФ-адреса):
задачу отправляли в `external-ai`, но у воркера не было ни этого вида задач, ни базы.
Теперь разведка в три слоя: снимок базы на ядре при выдаче задачи
(`signal_discovery.build_external_payload`) → прогон без базы на воркере
(`process_external_payload`) → запись на ядре при complete (`apply_external_result`).

- Темы радара = 13 корневых тегов (`SIGNAL_RADAR_TOPIC_SOURCE=tags`, по умолчанию), тема
  сигнала = тег. Таблица `signal_radar_topics` — запасной путь (сид её перезаписывает).
- Обучение на ОС: подсказки поиска — не из брака и только своей темы
  (`retire-signal-query-hints` гасит накопленные); судье — и одобренные, и отклонённые
  примеры; разобранные адреса не возвращаются; правила заказчика — в инструкции судьи.
- Расход: 6 кластеров на тему (до 78 вызовов судьи на прогон), 4 запроса Brave на тему.
  Упавшая ежедневная задача в те же сутки заново не ставится.
- Ежедневный запуск — **крон РФ-сервера** 07:15 МСК
  (`docker exec oiltech_agents_app python -m oiltech_digest.cli enqueue-daily-signal-discovery`);
  планировщик агентов по-прежнему НЕ запущен (удвоил бы сбор и расход).

**Воркер агентов на NL поднят владельцем 18.09** (SSH туда нет — пересобирает он же): очередь
только `external-ai`, первый прогон по расписанию — задача 4469, 32 сигнала. В env обязательны
`CORE_API_URL=https://agents.oiltech-digest.ru`, токен из `/root/oiltech-agents/.worker-token`,
`SOURCE_DISCOVERY_SEARCH_PROVIDER=brave` и ключ Brave — без них поиск вернёт 0 и радар молча
не найдёт ничего. Пересборка после правок воркерной части (например, дедупа):
`cd /root/oiltech-agents && git fetch origin && git reset --hard origin/main && docker compose -p oiltech-agents-worker -f docker-compose.external-worker.yml up -d --build`.

## Дедуп радара (18.09, `b1a73df`)

Одно событие — одна карточка (перенос `reprints.py` MVP-1, `signal_dedup.py`): правило пар
(основы заголовка от 0,25, общая компания или ссылка) → судья «одно ли событие» на
NL-воркере, до 120 пар за прогон → звезда, а не цепочка. Дубль не удаляется:
`signals.merged_into_signal_id` + `merge_reason`, его ссылки видны в главной карточке.
Разобранную карточку (вердикт, комментарий, статус не «наблюдать») не прячет никогда.
Снять пометку: `UPDATE signals SET merged_into_signal_id = NULL, merge_reason = NULL WHERE id = …`.
Судья работает только в новой сборке NL-воркера агентов; итог прогона — `dedup` в `result_json`.

## Агент источников — известные дефекты (разбор 18.09; открыты 1, 3, 4, 7)

Описание для заказчика — `docs/agent_istochnikov_dlya_zakazchika.md`.

1. Снятый флажок «Без ИИ» → поиск зовёт OpenAI **прямо на РФ-ядре** (`agent.py:277`, `:838`,
   цикл в очереди default/ru `api.py:962-969`) → 403, цикл failed. Тот же класс, что радар.
2. ~~«Поставить в очередь» не смотрит на флажок~~ — **исправлено `eb4eb71`** (18.09): в платную
   очередь ИИ оценка кандидатов уходит только при снятом флажке «Без ИИ».
3. Без ИИ релевантность — заглушка «всегда да» (`openai_client.py:125-126`): вердикты завышены.
4. Бюджет только у цикла; «9/6» считает и остановленные лимитом нажатия (`loop.py:48-71`).
5. ~~Одна ошибка 4xx навсегда исключает домен из поиска~~ — **исправлено** (18.09): любая ошибка —
   отсрочка на сутки, исключение после трёх сбоев (404 — мёртвая ссылка, 403/451 — блок адреса ядра).
6. ~~Одобрение перезапишет другой источник с тем же именем~~ — **исправлено** (18.09): имя из
   заголовка страницы бывает общим («Press Releases»); на чужом домене к имени добавляется домен.
7. Одобренный источник попадает только в базу агентов — в основную ленту его переносят руками.

## Agent skills

### Issue tracker

Задачи и спеки этого репозитория — в GitHub Issues `lowbrains/oiltech-agents`,
операции через `gh`. См. `docs/agents/issue-tracker.md`.
Тикеты MVP-1 остаются в `electromop/oiltech-digest` и сюда не переносятся.

### Triage labels

Пять канонических ролей, строка метки равна имени роли. См. `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` в корне + `docs/adr/`. См. `docs/agents/domain.md`.
