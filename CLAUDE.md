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

**Не сделано — поддомен и живая генерация:**

1. DNS: A-запись `agents.oiltech-digest.ru → 109.68.213.12` (панель Timeweb, владелец).
   18.09 не резолвится, общей записи `*.oiltech-digest.ru` нет.
2. Подключить `agents-app` к сети MVP-1:
   `docker compose -f docker-compose.yml -f docker-compose.server.yml up -d --no-deps agents-app`,
   проверить `docker exec oiltech_caddy getent hosts app` — должен вернуть ОДИН адрес (MVP-1).
3. В `Caddyfile` MVP-1 блок `agents.oiltech-digest.ru { encode zstd gzip
   reverse_proxy agents-app:8000 }`, затем `docker compose exec caddy caddy reload
   --config /etc/caddy/Caddyfile` — только после п.1, иначе Caddy будет безуспешно
   выпускать сертификат.
4. NL: второй контейнер внешнего воркера для агентов — `CORE_API_URL=https://agents.oiltech-digest.ru`,
   токен из `.worker-token`. Без него агент не зовёт модель (с РФ-адреса OpenAI — 403).
5. Планировщик агентов не запущен намеренно: второй полный конвейер удвоит сбор и расход
   на ИИ (~$74 → ~$150/мес) на сервере с 1,9 ГБ. Включать после замера памяти.

## Agent skills

### Issue tracker

Задачи и спеки этого репозитория — в GitHub Issues `lowbrains/oiltech-agents`,
операции через `gh`. См. `docs/agents/issue-tracker.md`.
Тикеты MVP-1 остаются в `electromop/oiltech-digest` и сюда не переносятся.

### Triage labels

Пять канонических ролей, строка метки равна имени роли. См. `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` в корне + `docs/adr/`. См. `docs/agents/domain.md`.
