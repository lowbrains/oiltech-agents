# OilTech Digest

**Система мониторинга нефтесервисных новостей: автоматический сбор → AI-обработка →
фирменный месячный дайджест в PDF/Word.**

Сервис непрерывно собирает публикации из ~120 отраслевых источников (сайты, RSS,
Telegram), очищает и оценивает их с помощью AI, а редактор через веб-интерфейс
отбирает лучшие материалы и выгружает готовый дайджест в оформлении Блока развития
бизнеса.

```text
источники → поиск RSS → парсинг → полный текст → AI-суть → релевантность → теги → скоринг → дайджест
```

> 📋 **Задачи, замечания и пожелания** — в [`BACKLOG.md`](BACKLOG.md). Впишите комментарий, команда возьмёт в работу и обновит статус.

---

## Архитектура (гео-распределённая)

Продукт работает на **двух серверах** — это решает две географические границы одновременно: РФ-госсайты недоступны из-за рубежа, а OpenAI не работает с РФ-IP.

- **🇷🇺 РФ-сервер (core)** — БД (PostgreSQL), веб-админка, домен, планировщик, парсинг РФ-источников. Пользователи работают с ним **локально, без VPN**; данные — в РФ.
- **🌍 Зарубежный сервер (external worker)** — stateless-воркер: OpenAI-обработка и парсинг заблокированных из РФ источников. Без БД и UI.

Связь — **не прямой коннект к БД и не VPN** (их режут блокировки), а **HTTPS-очередь задач**: core кладёт задачу, зарубежный воркер забирает её по HTTPS (`/api/external-worker/claim`), выполняет и возвращает результат.

```text
Пользователи → 🇷🇺 РФ-core (БД + UI + РФ-парсинг)  ⇄ HTTPS task-API ⇄  🌍 NL-воркер (OpenAI + иностранные сайты)
```

Всё за флагами (`EXTERNAL_WORKERS_ENABLED`, `AI_EXECUTION_REGION`, `FETCH_EXTERNAL_ENABLED`) — по умолчанию **single-server** режим, когда один сервер делает всё. Детали: [`docs/archive/geodistributed_architecture_plan.md`](docs/archive/geodistributed_architecture_plan.md), [`docs/external_worker_deploy.md`](docs/external_worker_deploy.md).

---

## Возможности

- **Сбор без участия человека** — RSS, HTML-листинги и Telegram-каналы, с защитой от
  банов (паузы между запросами, прокси, cooldown на 403/429).
- **AI-конвейер** — краткая суть, фильтр релевантности, авто-тегирование по
  направлениям и скоринг каждой статьи (0–100) по настраиваемым критериям.
- **Веб-панель администратора** (React) — каталог статей с серверным поиском по всей
  базе, фильтрами и вкладкой «Со статусом», ручная модерация, здоровье источников,
  редактор тегов (со **стоп-словами** для исключения ненужных статей) и весов скоринга.
- **Фирменный дайджест** — выгрузка отобранных статей в **PDF** и **Word** (с фото,
  кликабельными ссылками, шапкой и футером) в дизайне корпоративного референса.

---

## Быстрый старт (Docker)

```bash
cp .env.example .env
```

В `.env` задайте минимум:

```bash
POSTGRES_PASSWORD=сложный_пароль
OPENAI_API_KEY=sk-...        # без ключа сбор работает, AI-обработка пропускается (AI_OFFLINE=1)
```

Запуск всего стека (БД, админка, авто-сбор по расписанию). Конвейер (`scheduler`,
`worker`, `playwright-worker`) стоит за профилем `pipeline` — без него поднимаются
только БД, админка и Caddy (архивные `tasks` и `docs` — профиль `archive`):

```bash
docker compose --profile pipeline up -d --build
```

Откройте интерфейс: **http://127.0.0.1:8000** (при первом входе зарегистрируйтесь —
панель защищена `email + пароль`). Через Caddy интерфейс также доступен на **http://127.0.0.1/**.

Документация работает отдельным контейнером `docs` на MkDocs Material: **http://127.0.0.1/help** или напрямую **http://127.0.0.1:8081/help/**.

```bash
docker compose ps               # db/app/docs/scheduler = running, bootstrap = exited(0)
docker compose logs -f scheduler # лог авто-сбора
docker compose down             # остановить   (down -v — со сбросом БД)
```

> **PDF-экспорт** рендерится headless-Chromium (Playwright) — он ставится в образ при
> сборке. На сервере с малым объёмом RAM собирайте образ при остановленном стеке
> (`docker compose down` перед `build`), иначе возможен OOM.

---

## Как пользоваться интерфейсом

Слева — пять разделов.

### 📰 Все статьи
Весь поток, прошедший pipeline. Фильтры по тексту, тегу, источнику, статусу, скорингу,
дате и языку; сортировка по score/дате. Карточки сгруппированы по направлению; большие
выборки подгружаются кнопкой **«Показать ещё»**. Раскрыв статью, видно AI-суть и разбор
скоринга по критериям. Статус **«В дайджест»** добавляет статью в выпуск.

- «Дата попадания» — это дата появления статьи в системе. Если у публикации стоит дата
  из будущего (анонс события), статья помечается отдельным ярлыком и не попадает в дайджест.

### 🗂 Месячный дайджест
Итоговая выборка статей со статусом «В дайджест». Здесь можно отфильтровать выпуск,
посмотреть превью и:

- **Открыть HTML** — фирменное письмо в браузере;
- **Скопировать JSON** — структуру выпуска;
- **Сохранить draft** — зафиксировать выпуск месяца;
- **Скачать экспорт → PDF или Word** — готовый дайджест в корпоративном оформлении.

Если выбрать «Все месяцы», в экспорт попадут все отобранные статьи (а не только за один месяц).

### 🌐 Источники
Каталог из ~120 источников: тип, периодичность, RSS/listing-URL, вкл/выкл. Для
non-RSS источников настраиваются селекторы листинга, есть **диагностика** (HTTP-проба и
извлечение статей) и индикатор **здоровья** (ОК / застой / 0 статей / выкл).

### ⚖️ Скоринг
Критерии оценки и их веса (сумма строго 100%). Русские ключевые слова автоматически
нормализуются в английские подсказки для зарубежных источников.

### 🏷 Теги
Иерархия направлений (родительские теги и подтеги) с описаниями для AI.

---

## Дайджест: оформление

Дизайн дайджеста зафиксирован в фирменном шаблоне
[`oiltech_digest/processing/digest_email_template.html`](oiltech_digest/processing/digest_email_template.html)
(референс — `digest_email_claude_pack`). Один и тот же шаблон используется для HTML-превью,
PDF и Word — выпуск везде выглядит одинаково. Картинки берутся из статей (og:image); если
картинки нет — подставляется фирменная заглушка.

```text
GET /api/digest-email?month=2026-05            # HTML-превью
GET /api/digest-export?month=2026-05&export_format=pdf   # PDF
GET /api/digest-export?export_format=doc                 # Word (все выбранные)
```

---

## Для разработчиков

### Ручной запуск

```bash
docker compose up -d db                       # PostgreSQL
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium         # для PDF-экспорта

python -m oiltech_digest.cli init-db          # схема БД (идемпотентно)
python -m oiltech_digest.cli seed-sources     # источники из Excel
python -m oiltech_digest.cli parse            # собрать ленты в articles
python -m oiltech_digest.cli fetch-full-text  # дозагрузить полный текст
python -m oiltech_digest.cli process --limit 20  # summary → relevance → tag → score

uvicorn oiltech_digest.api:app --reload --port 8000
```

Для прогона без OpenAI-ключа добавляйте `--offline`.

### Основные CLI-команды

| Команда | Назначение |
|---|---|
| `init-db` / `seed-sources` / `seed-tags` / `seed-scoring` | инициализация схемы и справочников |
| `discover-rss` | автообнаружение RSS по сайту источника |
| `parse` | собрать `rss`/`request`/`telegram`-источники в `articles` |
| `fetch-full-text` | заменить RSS-анонс на полный текст статьи |
| `process` | `summarize → relevance → tag → score` одной командой (локально) |
| `enqueue-process` | поставить AI-обработку в очередь зарубежного воркера (гео-режим) |
| `external-worker` | клиент-воркер на зарубежном сервере (берёт задачи по HTTPS) |
| `external-queues-status` | состояние внешних очередей и heartbeat воркера |
| `digest-content` / `digest-save` | собрать/сохранить выпуск дайджеста |
| `source-health` / `source-diagnose` | диагностика покрытия источников |
| `source-probe <id>` | почему источник молчит: путь сбора и вердикт рубежей вставки по каждому кандидату (`known`, `too_short`, `prefilter`, `DUP_URL_KEY_SAME/OTHER`, `DUP_BODY_HASH`, `WOULD_INSERT`); ничего не пишет, `--json` — полный список |
| `stats` / `ai-cost-report` | статистика и стоимость AI |

`python -m oiltech_digest.cli <команда> --help` — подробности по любой команде.

### Тесты

```bash
PYTHONPATH=. python -m pytest          # весь набор
docker compose run --rm test           # тот же набор в Docker с dev-зависимостями
```

### Структура

```text
oiltech_digest/
  config.py          # пути, DATABASE_URL, флаги гео-контура, константы парсера
  db/                # schema.sql, connection, repository
  ingestion/         # сбор: rss/request/telegram, http_client, full-text, external_fetch
  processing/        # AI-конвейер (OpenAI), дайджест (PDF/Word), external_ai
  network_policy.py  # маршрутизация задач: локально (РФ) или внешний воркер
  external_worker.py # клиент-воркер для зарубежного сервера (HTTP-pull)
  api.py             # FastAPI backend + external-worker task-API + статика
  cli.py             # командная строка
frontend/            # React-админка (Vite + TS); web/app.html — fallback
tests/               # pytest
docs/                # архитектура, гайды, runbook'и
docker-compose.yml                   # РФ-core: db + app + worker + scheduler + caddy
docker-compose.external-worker.yml   # зарубежный stateless-воркер
```

### Документация

- [`docs/project_guide.md`](docs/project_guide.md) — главный гид по проекту и коду;
- [`docs/architecture.md`](docs/architecture.md) — архитектура и поток данных;
- [`docs/testing.md`](docs/testing.md) — сценарии проверки и smoke-тесты.

### Настройка сбора (`.env`)

| Переменная | Значение |
|---|---|
| `CYCLE_INTERVAL_SECONDS=21600` | период цикла scheduler (6 ч) |
| `RUN_MAINTENANCE_ON_START=1` | запуск service-cleanup на первом цикле scheduler |
| `MAINTENANCE_EVERY_CYCLES=24` | как часто scheduler повторяет service-cleanup |
| `HTTP_MIN_INTERVAL_SECONDS=1.5` | пауза между запросами к одному хосту |
| `HTTP_BLOCK_COOLDOWN_SECONDS=900` | cooldown после 403/429 |
| `REQUEST_ARTICLE_LIMIT=6` | статей с одного listing за цикл |
| `FULL_TEXT_LIMIT=80` / `AI_PROCESS_LIMIT=50` | объёмы дозагрузки и AI за цикл |
| `PROXY_URL=` / `PROXY_HOST_OVERRIDES=` | глобальный / точечный прокси для парсинга |
| `OPENAI_MODEL=gpt-5-nano` | модель и цены (`OPENAI_*_USD_PER_MTOK`) |
| `AI_OFFLINE=0` | `1` = тестовый режим без OpenAI API |

PostgreSQL проброшен только на `127.0.0.1:5432` и наружу не открыт.
