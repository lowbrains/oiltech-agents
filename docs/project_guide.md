# OilTech Digest: гид по проекту

Этот документ нужен, чтобы быстро понять, что уже работает в проекте, как данные
двигаются по системе, где смотреть код и как запускать основные сценарии.

## 1. Что делает проект

`OilTech Digest` собирает новости из каталога источников, сохраняет статьи в PostgreSQL,
отсеивает очевидный шум, при необходимости догружает полный текст статьи по ссылке,
прогоняет AI-обработку и дает интерфейс для ручного отбора материалов в месячный дайджест.

Текущий продукт состоит из трех рабочих частей:

1. ingestion-пайплайн для источников и статей;
2. AI-пайплайн `summary -> relevance -> tag -> score`;
3. FastAPI + Admin UI для просмотра и управления данными.

## 2. Что уже реально работает

Сейчас в проекте есть:

- каталог источников из Excel;
- автообнаружение RSS для сайтов;
- парсинг RSS в таблицу `articles`;
- listing-based monitoring для `request`-источников без RSS;
- Telegram public-preview ingestion для открытых каналов через `https://t.me/s/<channel>`;
- предфильтр явного нерелевантного шума до записи в БД;
- дозагрузка полного текста статьи по `url`, если RSS дал только анонс;
- AI summary на русском;
- AI-проверка релевантности нефтесервису;
- AI-тегирование по иерархии D01-D18;
- AI-скоринг по настраиваемым критериям;
- учет токенов и стоимости по каждому AI-этапу;
- HTML email-шаблон дайджеста и JSON-черновик дайджеста;
- UI для статей, источников, тегов, скоринга и дайджеста;
- Docker Compose с `db`, `app`, `scheduler`.

Пока не реализовано как рабочий продукт:

- Telegram ingestion через Telegram API/авторизованную сессию для закрытых каналов;
- автоматическая публикация PDF/DOCX;
- сохранение редакторского дайджеста в `monthly_digests` как основного потока UI.

Эти сущности в БД есть частично "впрок", но основная работа сейчас идет через
`sources`, `articles`, `article_cards`, `article_tags`, `article_scores`,
`article_score_items`, `tags`, `scoring_criteria`, `ai_processing_runs`.

## 3. Как движутся данные

Текущий боевой поток такой:

```text
sources
  -> discover-rss
  -> parse
     (rss / request / public Telegram preview)
  -> deterministic relevance prefilter
  -> articles
  -> fetch-full-text
  -> summarize
  -> relevance
  -> tag
  -> score
  -> UI / digest-content / digest-email
```

Разница между двумя фильтрами релевантности:

- `ingestion/relevance_filter.py` — дешевый детерминированный отсев совсем явного мусора
  еще до вставки статьи в БД;
- AI-этап `relevance` — более тонкая семантическая проверка уже после summary.

## 4. Основные директории

### `oiltech_digest/`

- `config.py` — переменные окружения, пути, общие настройки;
- `db/connection.py` — подключение к PostgreSQL и `init_db()`;
- `db/schema.sql` — вся схема БД;
- `db/repository.py` — SQL-операции и выборки для CLI/API/pipeline;
- `ingestion/excel_seed.py` — загрузка источников из Excel;
- `ingestion/rss_discovery.py` — поиск RSS-лент у источников;
- `ingestion/rss_parser.py` — чтение RSS и вставка статей;
- `ingestion/request_parser.py` — мониторинг listing-страниц и парсинг non-RSS источников;
- `ingestion/relevance_filter.py` — предфильтр шума;
- `ingestion/article_fetcher.py` — попытка извлечь полный текст со страницы статьи;
- `processing/openai_client.py` — клиент OpenAI Responses API и offline-заглушка;
- `processing/prompts.py` — схемы и инструкции для summary/relevance/tag/score;
- `processing/pipeline.py` — бизнес-логика AI-этапов;
- `processing/seed.py` — seed тегов и критериев;
- `processing/digest.py` — сбор контента дайджеста и рендер HTML;
- `processing/digest_email_template.html` — email-шаблон;
- `api.py` — FastAPI API и отдача UI;
- `cli.py` — точка входа для ручных команд.

### `web/`

- `app.html` — весь текущий Admin UI без отдельного frontend build step.

### `scripts/`

- `docker-scheduler.sh` — бесконечный цикл ingestion + AI в Docker.

### `docs/`

- `architecture.md` — техническая архитектура;
- `testing.md` — как проверять систему;
- `reference/` — фирменный референс дайджеста (email_template + образец);
- `archive/` — исторические отчёты и планы (work_report, handoff, status).

## 5. Режимы запуска

### Вариант A: все сразу через Docker

Это основной способ понять проект целиком.

Поднимаются:

- `db` — PostgreSQL;
- `app` — FastAPI и UI на `127.0.0.1:8000`;
- `scheduler` — циклический ingestion и AI-processing (профиль `pipeline`).

Команда:

```bash
docker compose --profile pipeline up -d --build
```

### Вариант B: локально по шагам

Подходит для разработки и диагностики:

1. поднять только БД;
2. вручную запускать CLI-команды;
3. отдельно поднять `uvicorn`.

## 6. Что делает scheduler

Файл: [scripts/docker-scheduler.sh](../scripts/docker-scheduler.sh)

При старте scheduler:

1. делает `init-db`;
2. делает `seed-sources`;
3. делает `seed-tags`;
4. делает `seed-scoring`.

Дальше в цикле:

1. периодически запускает `discover-rss`;
2. запускает `parse`;
3. запускает `fetch-full-text`;
4. запускает `process` или `process --offline`;
5. печатает `stats`;
6. засыпает на `CYCLE_INTERVAL_SECONDS`.

Главные env-параметры для scheduler:

- `CYCLE_INTERVAL_SECONDS`
- `RUN_DISCOVER_ON_START`
- `DISCOVER_EVERY_CYCLES`
- `DISCOVER_TIMEOUT`
- `DISCOVER_WORKERS`
- `PARSE_WORKERS`
- `FULL_TEXT_LIMIT`
- `FULL_TEXT_MIN_CHARS`
- `AI_PROCESS_LIMIT`
- `AI_OFFLINE`

## 7. База данных простыми словами

### Источники и сырье

- `sources` — каталог источников, который видит пользователь, включая `listing_url`,
  `last_seen_article_url`, `last_seen_published_at` для non-RSS мониторинга;
- `articles` — сырые статьи после RSS/полного текста.

### Рабочий слой AI

- `article_cards` — summary, статус, комментарии, selected flag, AI relevance;
- `article_tags` — итоговый тег статьи;
- `article_scores` — итоговый score;
- `article_score_items` — детализация score по критериям;
- `ai_processing_runs` — токены, стоимость, статус каждого AI-вызова.

### Настройки

- `tags` — иерархия тематик;
- `scoring_criteria` — критерии скоринга и веса.

### Заготовки на будущее

- `monthly_digests`, `monthly_digest_items`;
- `export_jobs`.

Сейчас UI-дайджест собирается на лету из обработанных статей, а не из сохраненного
объекта `monthly_digests`.

## 8. CLI-команды по смыслу

### Подготовка БД

- `init-db`
- `seed-sources`
- `seed-tags`
- `seed-scoring`

### Сбор данных

- `discover-rss`
- `parse`
- `fetch-full-text`
- `stats`

### AI-обработка

- `summarize`
- `relevance`
- `tag`
- `score`
- `process`
- `process-articles`

### Аналитика по стоимости

- `ai-cost-report`
- `ai-article-cost-report`

### Работа с источниками

- `sources`
- `source-health`
- `source-enable`
- `source-add-rss`
- `source-diagnose`
- `source-probe` — путь сбора источника с вердиктом рубежей вставки по каждому кандидату, только чтение

### Дайджест

- `digest-content`
- `digest-save`

## 9. API-эндпоинты

Файл: [api.py](../oiltech_digest/api.py)

Основные ручки:

- `GET /` — UI;
- `GET /api/health` — быстрая проверка живости;
- `GET /api/articles` — список статей;
- `PATCH /api/articles/{id}` — статус/отбор/комментарий статьи;
- `GET /api/sources` — список источников;
- `GET /api/source-health` — вердикты покрытия источников;
- `POST /api/sources` — добавить RSS-источник;
- `POST /api/auth/register` / `POST /api/auth/login` / `POST /api/auth/logout`;
- `GET /api/auth/me`;
- `POST /api/sources/{source_id}/scrape` — вручную проверить listing request-источника;
- `GET/POST /api/sources/{source_id}/diagnose` — read-only диагностика источника; POST принимает временные селекторы без сохранения;
- `PATCH /api/sources/{id}` — обновить источник;
- `GET /api/tags`, `PUT /api/tags` — теги;
- `GET /api/scoring-criteria`, `PUT /api/scoring-criteria` — критерии скоринга;
- `GET /api/reports/ai-cost` — агрегированный cost report;
- `GET /api/reports/ai-article-cost` — стоимость полного прогона статьи;
- `GET /api/digest-content` — JSON дайджеста;
- `POST /api/monthly-digests` — сохранить draft дайджеста за месяц;
- `GET /api/monthly-digests/{month}` — получить сохраненный draft;
- `GET /api/digest-email` — HTML email-шаблон;
- `POST /api/process` — AI-обработка батча статей.

## 10. Что видит пользователь в UI

### Все статьи

Главный рабочий экран:

- поиск;
- фильтры;
- статусы;
- score;
- summary;
- tag;
- статус статьи `digest`, который и определяет попадание в месячный дайджест;
- запуск AI-обработки выбранных статей.

### Месячный дайджест

- фильтрация статей по месяцу и score;
- JSON-черновик;
- открытие HTML email-шаблона.

### Источники

- каталог источников;
- включение/выключение;
- изменение RSS URL;
- настройка `listing_url` и селекторов для `request`-источников;
- ручное добавление RSS-источника;
- вход и регистрация по email/password.

### Теги и Скоринг

- редактируемые списки настроек для AI pipeline.

## 11. Как проект обычно использовать

### Если нужно просто собрать систему и посмотреть UI

```bash
cp .env.example .env
docker compose up -d --build
```

### Если нужно вручную прогнать полный ingestion

```bash
python -m oiltech_digest.cli init-db
python -m oiltech_digest.cli seed-sources
python -m oiltech_digest.cli discover-rss
python -m oiltech_digest.cli parse
python -m oiltech_digest.cli fetch-full-text
python -m oiltech_digest.cli stats
```

### Если нужно вручную прогнать AI

```bash
python -m oiltech_digest.cli seed-tags
python -m oiltech_digest.cli seed-scoring
python -m oiltech_digest.cli process --limit 20
```

### Если нужно посмотреть экономику

```bash
python -m oiltech_digest.cli ai-cost-report
python -m oiltech_digest.cli ai-article-cost-report
```

### Если нужно собрать дайджест

```bash
python -m oiltech_digest.cli digest-content 2026-05 \
  --output digest-2026-05.json \
  --html-output digest-2026-05.html
```

## 12. Частые вопросы

### Почему в БД может быть статья с пустым или коротким текстом?

Потому что часть RSS отдает только анонс. Для этого есть `fetch-full-text`, который
пытается заменить анонс на полный текст страницы.

### Почему статья может не попасть в БД вообще?

Если предфильтр распознал ее как явный шум, она отсекается еще на этапе `parse`
и в БД не сохраняется.

### Почему статья не дошла до тегов и скоринга?

Обычно по одной из причин:

- еще не был запущен `process`;
- AI relevance пометил статью как нерелевантную;
- стоит лимит батча и статья ждет следующего прохода.

### Откуда берется стоимость?

Из таблицы `ai_processing_runs`, где после каждого вызова OpenAI сохраняются
`input_tokens`, `output_tokens`, `total_tokens`, `cost_usd`, `model`, `status`.

## 13. С чего читать код новому человеку

Лучший порядок чтения:

1. [README.md](../README.md)
2. [project_guide.md](project_guide.md)
3. [architecture.md](architecture.md)
4. [cli.py](../oiltech_digest/cli.py)
5. [api.py](../oiltech_digest/api.py)
6. [repository.py](../oiltech_digest/db/repository.py)
7. `ingestion/*`
8. `processing/*`

Этого достаточно, чтобы понять проект сверху вниз: продукт -> запуск -> данные -> SQL -> бизнес-логика.
