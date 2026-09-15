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

## Развязка, которая ещё не сделана

На момент форка агент и MVP-1 сидели в **одной базе**, связанные внешними ключами
в обе стороны:

```
агент → ядро:  signal_feedback_events.article_id    → articles
               source_quality_snapshots.source_id   → sources
               source_candidates.approved_source_id → sources
               source_candidate_articles.tag_id     → tags
               signal_evidence.article_id           → articles
ядро → агент:  background_jobs.agent_run_id         → agent_runs
```

Решение владельца: **своя БД и свой поддомен**, корпус переносится дампом на старте.
До этого не деплоить — два деплоя по одной схеме удвоят класс бага, который чинили
13.09 (сид воскрешал выключенные критерии; порядок блоков в `schema.sql` load-bearing).

Пока развязки нет, `docker-compose.yml` и `.env.example` унаследованы от MVP-1
и указывают на ту же базу и те же порты. Это первое, что меняется перед выкатом.

## Agent skills

### Issue tracker

Задачи и спеки этого репозитория — в GitHub Issues `lowbrains/oiltech-agents`,
операции через `gh`. См. `docs/agents/issue-tracker.md`.
Тикеты MVP-1 остаются в `electromop/oiltech-digest` и сюда не переносятся.

### Triage labels

Пять канонических ролей, строка метки равна имени роли. См. `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` в корне + `docs/adr/`. См. `docs/agents/domain.md`.
