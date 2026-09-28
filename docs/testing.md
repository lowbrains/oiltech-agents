# Тестирование OilTech Digest

Этот документ описывает, как проверять проект по слоям: код, БД, ingestion,
AI и UI/API.

## 1. Unit-тесты

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Без локального Python-окружения можно прогнать тот же набор в Docker:

```bash
docker compose run --rm test
```

Фоновые API-задачи покрываются тестами `tests/test_background_jobs.py`,
`tests/test_api.py` и `tests/test_api_integration.py`.

Что покрыто сейчас:

- full-text fetcher;
- deterministic relevance prefilter;
- HTML rendering дайджеста.

## 2. Быстрый smoke через Docker

Самый короткий сценарий:

```bash
cp .env.example .env
docker compose --profile pipeline up -d --build
docker compose logs -f scheduler
```

Ожидаемо:

- `db`, `app`, `scheduler` стартуют;
- scheduler проходит `init-db`, `seed-sources`, `seed-tags`, `seed-scoring`;
- дальше начинается цикл `discover-rss -> parse -> fetch-full-text -> process -> stats`.

Проверка:

```bash
docker compose ps
curl http://127.0.0.1:8000/api/health
```

## 3. Локальный smoke без OpenAI

Подходит, если нужно проверить связность системы без расходов на API.

```bash
cp .env.example .env
docker compose up -d db
source .venv/bin/activate

python -m oiltech_digest.cli init-db
python -m oiltech_digest.cli seed-sources
python -m oiltech_digest.cli seed-tags
python -m oiltech_digest.cli seed-scoring
python -m oiltech_digest.cli stats
```

Ожидаемо:

- схема создается без ошибок;
- каталог источников загружен;
- теги и критерии появились;
- `stats` показывает источники и статьи без падений.

## 4. Проверка ingestion

### RSS discovery

```bash
python -m oiltech_digest.cli discover-rss --workers 10 --timeout 4
```

Проверить:

- у части источников появился `rss_url`;
- `parse_strategy` распределяется по `rss / request / telegram / none`;
- команда не падает на SSL fallback и 404.

### Парсинг RSS

```bash
python -m oiltech_digest.cli parse --workers 10
python -m oiltech_digest.cli stats
```

Проверить:

- статьи появляются в `articles`;
- повторный запуск в основном дает дубли, а не лавинообразный рост;
- в выводе `parse` есть поле `отсеяно как шум`.

### Дозагрузка полного текста

```bash
python -m oiltech_digest.cli fetch-full-text --limit 50 --min-chars 800
```

Проверить:

- часть статей получает `full_text_status=ok`;
- слишком короткие страницы получают `too_short`;
- страницы, где извлечённый текст не вдвое длиннее уже сохранённого, получают
  `no_gain` — это не сбой: текст у статьи есть, перезапись просто не нужна;
- команда не ломает уже хорошие тексты.

## 5. Offline AI smoke

```bash
python -m oiltech_digest.cli process --offline --limit 5
python -m oiltech_digest.cli ai-cost-report
python -m oiltech_digest.cli ai-article-cost-report
```

## 6. Проверка нефтегазовой терминологии

Перед демо или массовым экспортом дайджеста проверьте словарь:

```bash
python -m oiltech_digest.cli validate-terminology
```

Найти старые карточки с плохими терминами:

```bash
python -m oiltech_digest.cli audit-terminology --limit 1000 --show 50
```

Посмотреть, что будет исправлено без записи в БД:

```bash
python -m oiltech_digest.cli repair-terminology --dry-run --limit 1000 --show 50
```

Применить исправления после ручной проверки dry-run:

```bash
python -m oiltech_digest.cli repair-terminology --no-dry-run --limit 1000
```

Ожидаемо:

- появляются `article_cards`;
- появляются записи `article_tags`;
- появляются `article_scores` и `article_score_items`;
- в `ai_processing_runs` есть этапы `summary`, `relevance`, `tagging`, `scoring`;
- `ai-cost-report` и `ai-article-cost-report` читаются без ошибок.

## 6. OpenAI pilot

Перед этим в `.env` должен быть задан `OPENAI_API_KEY`.

```bash
python -m oiltech_digest.cli summarize --limit 3
python -m oiltech_digest.cli relevance --limit 3
python -m oiltech_digest.cli tag --limit 3
python -m oiltech_digest.cli score --limit 3
python -m oiltech_digest.cli ai-cost-report
python -m oiltech_digest.cli ai-article-cost-report
```

Проверить вручную:

- summary написан по-русски и не выглядит выдуманным;
- relevance отсеивает явный офтоп;
- tag соответствует теме статьи;
- score и explanation не противоречат содержанию;
- токены и стоимость записываются в `ai_processing_runs`.

## 7. Проверка полного пайплайна одной командой

```bash
python -m oiltech_digest.cli process --limit 20
```

Проверить по выводу:

- `summary` обработан;
- `relevance` показал число отклоненных статей;
- `tagging` и `scoring` отработали только по релевантным статьям.

## 8. Проверка digest

### CLI

```bash
python -m oiltech_digest.cli digest-content 2026-05 \
  --output /tmp/digest.json \
  --html-output /tmp/digest.html \
  --limit 5 \
  --min-score 0
```

Проверить:

- JSON содержит `issue`, `hero`, `news`, `footer`;
- HTML открывается как email-ready шаблон;
- у карточек есть title, source, summary, score и ссылка на источник.

### API

```bash
curl "http://127.0.0.1:8000/api/digest-content?month=2026-05&limit=5&min_score=0"
curl "http://127.0.0.1:8000/api/digest-email?month=2026-05&limit=5&min_score=0"
curl -OJ "http://127.0.0.1:8000/api/digest-export?month=2026-05&limit=5&min_score=0&export_format=html"
curl -OJ "http://127.0.0.1:8000/api/digest-export?month=2026-05&limit=5&min_score=0&export_format=doc"
```

## 9. Проверка UI

Запуск:

```bash
uvicorn oiltech_digest.api:app --reload --host 127.0.0.1 --port 8000
```

Проверить руками в браузере:

- `Все статьи`: список, фильтры, статусы, AI processing;
- `Месячный дайджест`: открывается HTML и копируется JSON;
- `Источники`: список, включение/выключение, изменение RSS, добавление RSS;
- `Теги`: редактирование и сохранение;
- `Скоринг`: редактирование весов и сохранение.

## 10. Полезные диагностики

### Compose

```bash
docker compose config -q
docker compose build
```

### Код

```bash
python -m compileall oiltech_digest
git diff --check
```

### API

```bash
curl http://127.0.0.1:8000/api/health
```

## 11. Минимальный чек готовности

Считаем систему в рабочем состоянии, если:

- unit-тесты зеленые;
- Docker Compose собирается;
- `init-db`, `seed-*`, `discover-rss`, `parse`, `fetch-full-text` работают;
- `process` дает summary/relevance/tag/score без системных ошибок;
- UI открывается и читает реальные данные;
- `digest-content` и `digest-email` успешно генерируются.
