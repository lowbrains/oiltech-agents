"""Версионируемые оверрайды для источников с JS-рендером / WAF-защитой.

Зачем: для части иностранных сайтов нужен парсинг через headless Chromium
(`parse_strategy='playwright'`) и/или конкретный URL раздела новостей
(`listing_url`), которого нет в Excel-сидере. Чтобы эта настройка была
воспроизводимой (переживала пересоздание БД, а не жила только в проде через
ручной SQL), держим её здесь и применяем идемпотентно после seed.

Применение: `python -m oiltech_digest.cli apply-source-overrides`
(в Docker вызывается в bootstrap после seed-sources).

При смене стратегии/листинга сбрасываем request-состояние
(`last_listing_hash`, `last_seen_*`), иначе первый playwright-парс коротит на
старом хэше от прежней (request) попытки и добавляет 0.
"""

from __future__ import annotations

import logging

from oiltech_digest.db.connection import get_connection

logger = logging.getLogger(__name__)

# Ключ — точное имя источника (sources.name). Значения:
#   source_type    — опционально, но ОБЯЗАТЕЛЬНО для неуникальных имён: естественный ключ
#     таблицы — пара (name, source_type), одно издание живёт двумя строками (сайт 'Media' +
#     'Telegram'-канал) под общим именем. Без него имя с двумя строками считается
#     неоднозначным и оверрайд НЕ применяется (шумно, см. apply_overrides).
#   parse_strategy — обязательно ('playwright' для JS/WAF-сайтов, 'rss' для лент);
#   listing_url    — опционально; None = не трогать (берётся из url/сидера).
#   listing_selector — опционально; CSS/XPath карточек листинга. Нужен, когда на странице
#     НЕСКОЛЬКО блоков ссылок и «чужой» побеждает по очкам (сквозной сайдбар общей ленты
#     у федеральных СМИ). При заданном селекторе кандидаты берутся ТОЛЬКО из него.
#   rss_url        — опционально; для RSS-лент с нестандартным/сменившимся URL фида.
#   url            — опционально; для telegram/прочих с исправленным каналом/адресом.
#   network_region — опционально ('external' = фетчить через зарубежный воркер).
#     Нужен для западных сайтов, к которым с РФ-сервера нет доступа (WAF/таймаут), но
#     которые открываются из NL. Действует ТОЛЬКО при FETCH_EXTERNAL_ENABLED=1 —
#     иначе источник парсится локально как обычно (флаг проверяется в parse_all и
#     enqueue-external-scrape). Каптча/hard-WAF (Cloudflare/Akamai) сюда НЕ помечаем —
#     они 403 и из NL (нужен challenge-solver, отдельный проект).
SOURCE_OVERRIDES: dict[str, dict] = {
    # --- JPT по разделам (13.09) ---------------------------------------------
    # Издание читалось ОДНОЙ страницей /latest-news — 14 карточек, ~5 статей в неделю.
    # Замер 13.09: у JPT 40+ тематических разделов, 11 из них дают 99 уникальных статей,
    # и 66 из них у нас НЕ БЫЛО. Заказчик 24.08: «обратите внимание на источник JPT,
    # для нефтянки это маст хэв, номер 1 в мире, а оттуда мало что подтягивается».
    # Первый прогон восьми разделов принёс 23 статьи — ровно технологические
    # («Autonomous Robot for Gas Detection», «Digital Twin of Completions Operations»,
    # «Quantum Computing Use Cases for the Oil and Gas Industry»).
    # Сами строки источников заводит scripts/add_jpt_sections.sql — реестр их только
    # настраивает: apply_overrides UPDATE'ит существующие, а не создаёт.
    "JPT — R&D и инновации": {"source_type": "Journal", "parse_strategy": "playwright",
        "listing_url": "https://jpt.spe.org/topic/r-d-innovation",
        "listing_selector": ".PromoB, .PromoA",
        "article_link_selector": ".PromoB-title a, .PromoA-title a",
        "article_date_selector": ".PromoB-by-line, .PromoA-by-line"},
    "JPT — Роботизация": {"source_type": "Journal", "parse_strategy": "playwright",
        "listing_url": "https://jpt.spe.org/topic/robotics-unmanned-systems",
        "listing_selector": ".PromoB, .PromoA",
        "article_link_selector": ".PromoB-title a, .PromoA-title a",
        "article_date_selector": ".PromoB-by-line, .PromoA-by-line"},
    "JPT — Бурение": {"source_type": "Journal", "parse_strategy": "playwright",
        "listing_url": "https://jpt.spe.org/topic/drilling",
        "listing_selector": ".PromoB, .PromoA",
        "article_link_selector": ".PromoB-title a, .PromoA-title a",
        "article_date_selector": ".PromoB-by-line, .PromoA-by-line"},
    "JPT — Заканчивание": {"source_type": "Journal", "parse_strategy": "playwright",
        "listing_url": "https://jpt.spe.org/topic/completions",
        "listing_selector": ".PromoB, .PromoA",
        "article_link_selector": ".PromoB-title a, .PromoA-title a",
        "article_date_selector": ".PromoB-by-line, .PromoA-by-line"},
    "JPT — Внутрискважинные работы": {"source_type": "Journal", "parse_strategy": "playwright",
        "listing_url": "https://jpt.spe.org/topic/well-intervention",
        "listing_selector": ".PromoB, .PromoA",
        "article_link_selector": ".PromoB-title a, .PromoA-title a",
        "article_date_selector": ".PromoB-by-line, .PromoA-by-line"},
    "JPT — Инспекция и ТОиР": {"source_type": "Journal", "parse_strategy": "playwright",
        "listing_url": "https://jpt.spe.org/topic/inspection-maintenance",
        "listing_selector": ".PromoB, .PromoA",
        "article_link_selector": ".PromoB-title a, .PromoA-title a",
        "article_date_selector": ".PromoB-by-line, .PromoA-by-line"},
    "JPT — Промышленная безопасность": {"source_type": "Journal", "parse_strategy": "playwright",
        "listing_url": "https://jpt.spe.org/topic/safety",
        "listing_selector": ".PromoB, .PromoA",
        "article_link_selector": ".PromoB-title a, .PromoA-title a",
        "article_date_selector": ".PromoB-by-line, .PromoA-by-line"},
    "JPT — Водоподготовка": {"source_type": "Journal", "parse_strategy": "playwright",
        "listing_url": "https://jpt.spe.org/topic/water-management",
        "listing_selector": ".PromoB, .PromoA",
        "article_link_selector": ".PromoB-title a, .PromoA-title a",
        "article_date_selector": ".PromoB-by-line, .PromoA-by-line"},

    # Проверено на проде:
    # Ревизия 17.07: без listing_url скребли главную и замолчали с 01.07. Явный newsroom
    # подтверждён рендером: «Shell to sell Sprng Energy group...» 13 Jul 2026.
    "Shell": {"parse_strategy": "playwright",
              "listing_url": "https://www.shell.com/news-and-insights/newsroom/news-and-media-releases.html"},
    "Baker Hughes": {"parse_strategy": "playwright",
                     "listing_url": "https://www.bakerhughes.com/company/news"},
    # Endeavor Business Media сменил схему RSS: старый путь /__rss/website-scheduled-content/
    # отдаёт 404, новый формат — query-параметр ?input={"sectionAlias":"home"}. Храним
    # percent-encoded (без сырых {}"" ), чтобы пережить и SQL, и requote_uri в requests.
    # Проверено: ленты живые, со свежими статьями (июнь 2026).
    "Oil & Gas Journal": {"parse_strategy": "rss",
                          "rss_url": "https://www.ogj.com/__rss/website-scheduled-content.xml?input=%7B%22sectionAlias%22%3A%22home%22%7D"},
    "Offshore Magazine": {"parse_strategy": "rss",
                          "rss_url": "https://www.offshore-mag.com/__rss/website-scheduled-content.xml?input=%7B%22sectionAlias%22%3A%22home%22%7D"},
    "Automation World": {"parse_strategy": "rss",
                         "rss_url": "https://www.automationworld.com/__rss/website-scheduled-content.xml?input=%7B%22sectionAlias%22%3A%22home%22%7D"},
    # Отраслевые издания с рабочим RSS (подтверждено живым parse на проде). RSS гео-
    # независим — в отличие от request-newsroom, работает с любого IP одинаково.
    "World Oil": {"parse_strategy": "rss", "rss_url": "https://www.worldoil.com/rss?feed=news"},  # +10
    "Hydrocarbon Processing": {"parse_strategy": "rss", "rss_url": "https://www.hydrocarbonprocessing.com/rss?feed=news"},  # +10 (неполная TLS → verify=False fallback)
    "EIA": {"parse_strategy": "rss", "rss_url": "https://www.eia.gov/rss/todayinenergy.xml"},  # +15
    # JS-корпораты, ПОДТВЕРЖДЁННЫЕ на проде (request + listing_url на newsroom отдаёт
    # статьи; анти-заморозка в request_parser держит свежие релизы от застревания):
    "Eni": {"parse_strategy": "request", "listing_url": "https://www.eni.com/en-IT/media.html"},  # +3 на проде
    "Petrobras": {"parse_strategy": "request", "listing_url": "https://agencia.petrobras.com.br/en/mais-recentes"},  # +6 на проде
    "IEA": {"parse_strategy": "request", "listing_url": "https://www.iea.org/news"},  # +4 на проде
    "CNOOC": {"parse_strategy": "request", "listing_url": "https://www.cnoocltd.com/english/presscenter/pressreleases/2026/"},  # +6 (годовой путь — обновить в 2027)
    # ВАЖНЫЙ УРОК: WebFetch из US-облака видит SSR-версию newsroom, но на NL-проде те же
    # /news часто отдают JS-навигацию (0 статей у request). Поэтому request оставляем
    # ТОЛЬКО для проверенных живым parse на проде. Ниже — playwright-кандидаты: news-URL
    # найден, но listing рендерится через JS → нужен прод-тест с parse_strategy='playwright'
    # (Chromium в образе), как делали с Shell/Baker Hughes. ПОДТВЕРЖДЕНО на проде
    # (playwright рендерит JS и извлекает валидные статьи; пробивает даже 403, на
    # которых падали request/WebFetch — BP/TechnipFMC):
    "SLB (Schlumberger)": {"parse_strategy": "playwright", "listing_url": "https://www.slb.com/news-and-insights"},  # рендер ✓, последние уже в БД
    "ADNOC": {"parse_strategy": "playwright", "listing_url": "https://www.adnoc.ae/en/news-and-media"},  # рендер ✓
    # Ревизия 17.07: прежний /socar/en/page/media отдавал 0 статей — только навигацию (тот же
    # паттерн, что у ДРТ/Petronas). Новый URL: 10 статей, даты 22.06–09.07.2026. playwright НЕ
    # нужен — в сыром HTML 10 анкоров <a href="/en/post/...> (проверено голым UA).
    "SOCAR": {"parse_strategy": "request",
              "listing_url": "https://socar.az/en/page/press-releases"},
    "BP": {"parse_strategy": "playwright", "listing_url": "https://www.bp.com/en/global/corporate/news-and-insights/press-releases.html"},  # +3 (был 403 для request)
    "TechnipFMC": {"parse_strategy": "playwright", "listing_url": "https://www.technipfmc.com/en/media/press-releases/"},  # +6 (был 403)
    # Ревизия 17.07: playwright по /about-us/press-release молчал 38 дней. IR-фид живой
    # (10 items, свежак 16.07) и гео-независим. Источник ценнейший: из 18 собранных статей
    # ВСЕ 18 прошли гейт релевантности — 100% полезного сигнала.
    "Halliburton": {"parse_strategy": "rss",
                    "rss_url": "https://ir.halliburton.com/rss/news-releases.xml"},
    "TotalEnergies": {"parse_strategy": "playwright", "listing_url": "https://totalenergies.com/news/press-releases"},  # рендер ✓ (релизы с датами), нет свежее 28 мая
    # Ревизия 17.07: /news/ → /news/news-archive/ (хронологическая лента «25 of 1154», SSR,
    # свежак 15.07.2026); прежний URL молчал с 29.06.
    "Aker Solutions": {"parse_strategy": "playwright",
                       "listing_url": "https://www.akersolutions.com/news/news-archive/"},
    "Rystad Energy": {"parse_strategy": "playwright", "listing_url": "https://www.rystadenergy.com/news"},  # рендер ✓ (свежак 08 июня), scheduler собирает в фоне
    # JPT — личная просьба заказчика (24.08: «для нефтянки это маст хэв, номер 1 в мире,
    # а оттуда мало что подтягивается»). Рендер всегда был исправен (219 КБ, ни WAF, ни
    # блокировки) — теряли на разборе. Замер по сохранённой странице: без селекторов
    # якорный путь давал 42 кандидата, из них 30 — рубрики `/topic/...`, и рубрика же
    # шла ПЕРВОЙ по оценке, занимая слот в выдаче. Дат не было ни у одного.
    # Разметка ленты — Arc Publishing: карточки .PromoB (12 шт.) и .PromoA (2 шт.),
    # ссылка в *-title, дата в *-by-line. Комментарий «даты извлекаются» здесь стоял
    # ошибочно: last_seen_published_at у источника был пуст.
    "Journal of Petroleum Technology": {
        "parse_strategy": "playwright",
        "listing_url": "https://jpt.spe.org/latest-news",
        "listing_selector": ".PromoB, .PromoA",
        "article_link_selector": ".PromoB-title a, .PromoA-title a",
        "article_date_selector": ".PromoB-by-line, .PromoA-by-line",
    },
    # НЕ в реестре — listing отдаёт навигацию/SPA-оболочку вместо статей, нужен
    # listing_selector или другой URL (тюнинг отдельной задачей):
    #   Wood Mackenzie #16 (/press-releases/ → blogs/sign-up/topics)
    #   Deloitte #84 (/Industries/energy → навигация по индустриям, не новости; ценность спорна)
    #   Petroleum Economist #7 (SPA, extract даёт одинаковый текст-оболочку ~31k симв.)
    # network_region='external' (фетч через NL-воркер) НЕ прописываем в реестре: по
    # source-health большинство западных playwright-источников парсятся с РФ (свежие
    # статьи), а live-аудит у них флапает таймаутом. Реальные кандидаты на external
    # помечаются осознанно по id через `set-source-region` и A/B-тестятся (см. CLI).
    # JS-SPA, ПОДТВЕРЖДЕНЫ playwright-рендером 2026-06-26 (source-dump-listing --render с
    # РФ-core дал реальные ссылки на статьи; request-стратегия давала пустой shell →
    # no_candidates). Гео-доступны с РФ, рендер локальный, роутинг не нужен:
    "OilCapital": {"parse_strategy": "playwright", "listing_url": "https://oilcapital.ru/news"},  # много свежих РФ-нефтегаз новостей (/news/<дата>/<slug>)
    "Узбекнефтегаз": {"parse_strategy": "playwright", "listing_url": "https://www.ung.uz/press"},  # пресс-релизы (/press/page/<id>)
    "Spears & Associates": {"parse_strategy": "playwright", "listing_url": "https://spearsresearch.com/news"},  # drilling research (/news/<slug>)
    # Telegram-каналы с исправленным username (в Excel-сидере были неверные → t.me/s/
    # отдавал пустую ~9.6KB-страницу, posts=0). Правильные проверены на проде (17-20 постов):
    "Газбатюшка": {"parse_strategy": "telegram", "url": "https://t.me/papagaz"},
    "Агентство нефтегазовой информации": {"parse_strategy": "telegram", "url": "https://t.me/oilgasinform"},
    "Новая Энергия": {"parse_strategy": "telegram", "url": "https://t.me/novayaenergiya"},
    "Energy Today": {"parse_strategy": "telegram", "url": "https://t.me/energytodaygroup"},
    # ==== Ревизия источников 2026-07-17 ====
    # Диагностика на проде показала массовую болезнь: у многих источников прописан URL, который
    # НЕ является лентой новостей — карта сайта, страница наград или просто главная. Парсер
    # честно её скребёт, находит навигацию («История дивидендных выплат», «HR Portal»), один раз
    # загребает как «статьи» и дальше получает только дубликаты. Внешне — «источник замолчал».
    # Каждый URL ниже подтверждён живой проверкой + независимой перепроверкой (свежие заголовки
    # 2026 с датами). Оговорка: проверки шли НЕ с РФ-прода → истина = живой parse после деплоя.

    # -- Иностранные: RSS вместо скрейпа (RSS гео-независим и надёжнее) --
    "NOV": {"parse_strategy": "rss",
            "rss_url": "https://investors.nov.com/rss/news-releases.xml"},  # IR-фид, 10 items
    "КазМунайГаз": {"parse_strategy": "rss",
                    "rss_url": "https://www.kmg.kz/ru/press-center/press-releases/rss/"},  # 196 записей за 2026

    # -- Иностранные: правильный раздел новостей вместо главной/наград --
    "Petronas": {"parse_strategy": "request",
                 "listing_url": "https://www.petronas.com/media/media-releases"},  # был rss.xml = НАГРАДЫ
    "Mubadala Energy": {"parse_strategy": "request",
                        "listing_url": "https://mubadalaenergy.com/all-news/",
                        # 18.09: без селектора свежие новости («Learn more»-карточки)
                        # проигрывали по очкам отчётам 2022–2023; с ним 12 кандидатов, 10 с датой.
                        "article_link_selector": 'a[href*="/news/"]'},
    # NB: rss_url НЕ прописывать — /feed/ отдаёт дефолтный WordPress с единственным
    # постом «Hello world!» от 2022; источник замолчал бы навсегда.
    "OPEC": {"parse_strategy": "playwright",
             "listing_url": "https://www.opec.org/press-releases.html"},
    "QatarEnergy": {"parse_strategy": "playwright",
                    "listing_url": "https://www.qatarenergy.qa/en/MediaCenter/Pages/news.aspx"},
    "Kuwait Oil Company": {"parse_strategy": "playwright",
                           "listing_url": "https://www.kockw.com/sites/EN/Pages/Media%20Center/News%20And%20Events/Allitems.aspx"},
    # NB: прежний вывод «SharePoint за авторизацией, безнадёжен» ОПРОВЕРГНУТ живым рендером:
    # страница публична (200), в Chrome отдаёт ленту со свежими датами (06/07/2026).

    # -- Правки существующих записей: прописанный URL перестал быть лентой --
    "SPE (Society of Petroleum Engineers)": {"parse_strategy": "request",
                                             "listing_url": "https://jpt.spe.org/topic/spe-news"},
    # ВАЖНО: НЕ ставить listing_url на www.spe.org/en/about/news/ — там лента живая, но статьи
    # ведут на jpt.spe.org, а _build_candidate_from_anchor (request_parser.py:280) жёстко
    # отбрасывает ссылки на чужой хост → 0 кандидатов. Листинг обязан жить на том же домене,
    # что и статьи. RSS у jpt.spe.org мёртвый (0 items, lastBuildDate 2021).

    # ==== Ревизия 2026-07-17: РФ-источники ====
    # Все они парсили ГЛАВНУЮ (listing_url не задан → фоллбэк на url), т.е. собирали навигацию.
    "Сургутнефтегаз": {"parse_strategy": "request",
                       "listing_url": "https://www.surgutneftegas.ru/press-center/press_releases/"},
    "Новатэк": {"parse_strategy": "request",
                "listing_url": "https://www.novatek.ru/ru/press/releases/"},
    "Сибур": {"parse_strategy": "request",
              "listing_url": "https://www.sibur.ru/ru/press-center/news-and-press/"},
    "Росатом": {"parse_strategy": "request",
                "listing_url": "https://rosatom.ru/press_center/news/"},
    "Ростех": {"parse_strategy": "rss", "rss_url": "https://rostec.ru/rss-yandex/"},  # 50 items, свежие
    "Уфимский государственный нефтяной технический университет": {
        "parse_strategy": "request", "listing_url": "https://rusoil.net/ru/news"},
    "СПбГУ": {"parse_strategy": "rss", "rss_url": "https://spbu.ru/news-events.xml"},
    "МГУ": {"parse_strategy": "playwright", "listing_url": "https://www.msu.ru/news/"},  # сайт — SPA

    # ==== Ревизия 2026-08-22 (#61): федеральные СМИ собирали ОБЩУЮ ленту издания ====
    # Та же болезнь, что у РФ-блока выше, но на самых объёмных источниках: профиль издания
    # жил ТОЛЬКО в поле name («Энергетика», «ТЭК»), а на настройку не влиял — у РБК не задан
    # listing_url (фоллбэк на url = главная rbc.ru), у Интерфакса rss_url = общий фид издания.
    # В ленту нефтесервисного дайджеста попадали кот Ларри, приговор экс-генералу Росгвардии
    # и танк «Пантера» — заказчику это видно.
    # `source_type` здесь ОБЯЗАТЕЛЕН: у обоих изданий есть telegram-двойник с ТЕМ ЖЕ именем
    # (#59), и до правки apply_overrides ниже оверрайд мог лечь на него (см. её докстринг).
    "Интерфакс ТЭК": {
        "source_type": "Media", "parse_strategy": "request",
        # Проверено живым diagnose_source 22.08: verdict=ok, 8 кандидатов, заголовки
        # отраслевые («Минпромторг хочет к 2030 году довести долю поставляемого оборудования
        # РФ для ТЭК до 90%», «Изменения в топливном демпфере и налоговые послабления в ТЭК»).
        # ⚠️ ФОРМАТ URL: у Интерфакса тег живёт в ПУТИ. Форма `?tag=…` отдаёт 200 и кандидатов,
        # но это ОБЩАЯ лента: замер 22.08 — ?tag=ТЭК, ?tag=нефть, ?tag=энергетика вернули ОДИН
        # И ТОТ ЖЕ список (Wildberries, ЦИК, погода в Москве). Неверный URL здесь не падает,
        # а тихо отдаёт мусор — то есть выглядит как успешно применённый оверрайд.
        "listing_url": "https://www.interfax.ru/tags/%D0%A2%D0%AD%D0%9A/"},
    # NB: rss_url Интерфакса остаётся общей лентой издания. При parse_strategy='request' он не
    # читается (диспетчер в rss_parser.parse_all жёстко по parse_strategy), но возврат стратегии
    # на 'rss' — хоть руками, хоть discover-rss — вернёт мусор. Профильного фида у тега нет.
    "РБК Энергетика": {
        "source_type": "Media", "parse_strategy": "playwright",
        # Замер на проде 24.08 (rbc.ru закрыт для не-РФ IP, поэтому проверка шла на сервере).
        # Выдача тега рисуется на JS: request отдаёт 226 КБ / 266 анкеров, playwright на том
        # же URL — 377 КБ / 376 анкеров, и только во втором появляется блок
        # `div.search-item__wrap` с нефтегазовыми заголовками (Brent, Shell покупает ARC
        # Resources, налог на сверхприбыль энергокомпаний, Saudi Aramco).
        # listing_selector ОБЯЗАТЕЛЕН, и это не косметика: на отрендеренной странице ДВА
        # блока ссылок — выдача тега и сквозной сайдбар общей ленты `div.js-news-feed-list`
        # (Марадона, теннисист, Львова-Белова). Без селектора кандидаты берутся из обоих, и
        # сайдбар выигрывает по очкам: замер дал 8 кандидатов из сайдбара и НОЛЬ из тега —
        # то есть источник выглядел бы починенным, а тащил бы ровно тот же мусор.
        # Известное ограничение: тег пополняется медленно — на 24.08 свежайшая статья от
        # 04.08, дальше вглубь до 2021. Мало, но профильно; объём тюнить отдельно.
        "listing_url": "https://www.rbc.ru/tags/?tag=%D0%BD%D0%B5%D1%84%D1%82%D1%8C+%D0%B8+%D0%B3%D0%B0%D0%B7",
        "listing_selector": ".search-item__wrap"},

    # -- Гос-агентства: сайты таймаутят с прода (20с×3), но у них есть официальные telegram-каналы --
    # Telegram с РФ-сервера работает С ПЕРЕБОЯМИ (в логах бывает Network is unreachable), но
    # 7 telegram-источников живы и свежие — канал надёжнее, чем таймаутящий сайт.
    "Минпромторг РФ": {"parse_strategy": "telegram", "url": "https://t.me/minpromtorg_ru"},
    "Росстандарт": {"parse_strategy": "telegram", "url": "https://t.me/rosstandart"},
    "АЦ ТЭК": {"parse_strategy": "telegram", "url": "https://t.me/actekactek"},

    # Группа 🟡 (Playwright рендерит, нужен правильный news-URL) — добавляем после проверки:
    # "BCG Energy": {"parse_strategy": "playwright", "listing_url": "..."},

    # ==== Обход 30 молчащих источников, 18.09 (docs/handoff_2026-09-18_obhod-istochnikov.md) ====
    # Эти источники не «перестали работать» — они не работали никогда: у ЦДУ ТЭК все 16
    # «статей» — декабрь 2024 и дубли, у Сургутнефтегаза — «Банковские реквизиты». Каждая
    # настройка ниже проверена НОВЫМ кодом парсера на живой ленте с РФ-прода (§11.3).
    "Томский политехнический университет": {
        # Новости живут на поддомене news.tpu.ru, а источник смотрел на tpu.ru — ссылки на
        # поддомен парсер отсекал как чужой хост. Проверка: 12 кандидатов, все с датой.
        "parse_strategy": "request", "listing_url": "https://news.tpu.ru/news/"},
    "Сколтех": {"parse_strategy": "request", "listing_url": "https://skoltech.ru/news",
                "article_link_selector": 'a[href*="/news/"]'},  # 12 кандидатов, статья 5 тыс. знаков
    "Белоруснефть": {
        # Плитки li.list-entry, ссылка /ru/detail-pages/event/… БЕЗ текста — без селектора их
        # резал фильтр мусора по слову event. curl не проходит устаревший TLS, парсер проходит.
        "parse_strategy": "request", "listing_url": "https://www.belorusneft.by/ru/mediacenter/news/",
        "article_link_selector": 'li.list-entry a[href*="/detail-pages/"]'},
    "ТеДо": {"parse_strategy": "playwright", "listing_url": "https://tedo.ru/press-center",
             "article_link_selector": 'a[href*="/press-center/news-"]'},  # 12 с датой, статья 9 тыс. знаков
    "Минобрнауки РФ": {"parse_strategy": "rss",
                       "rss_url": "https://minobrnauki.gov.ru/press-center/news/rss/"},  # 40 пунктов
    "Б1": {
        # Отдельного списка нет (/analytics/ = 404): карточки обзоров — на главной.
        "parse_strategy": "request", "listing_url": "https://b1.ru",
        "article_link_selector": 'a[href*="/analytics/"]'},
    "Яков и Партнёры": {
        # Самый профильный («Рынок нефтесервиса до 8,4 трлн к 2035»), но дат на сайте нет вовсе.
        "parse_strategy": "request",
        "listing_url": "https://yakovpartners.ru/publications/?industiry=energy-oil-gas",
        "article_link_selector": 'a[href*="/publications/"]:not([href*="industiry"])'},
    "CNOOC / 中国海油": {
        # Браузер парсера с РФ проходит JS-заглушку (curl — нет): 10 кандидатов с датой.
        # Маршрут — auto, а не external: ставится `set-source-region`, не здесь.
        "parse_strategy": "playwright", "listing_url": "https://www.cnooc.com.cn/zxzx/gsxw/"},
    # Ниже — западные сайты, которые с РФ закрыты: маршрут через NL задаётся по id
    # (`set-source-region --region external`), здесь только стратегия и лента.
    "Weatherford": {
        "parse_strategy": "playwright",
        "listing_url": "https://www.weatherford.com/investor-relations/investor-news-and-events/news/",
        "article_link_selector": 'a[href*="news-article"]'},
    "S&P Global Commodity Insights": {
        # Бренд переименован в S&P Global Energy: старый адрес ведёт на лендинг.
        "parse_strategy": "playwright", "url": "https://www.spglobal.com/energy",
        "listing_url": "https://www.spglobal.com/energy/en/news-research/latest-news"},
    "PetroChina / 中国石油股份": {
        "parse_strategy": "playwright",
        "listing_url": "https://www.petrochina.com.cn/petrochina/xwxx/xwgg_list.shtml"},
}


def apply_overrides() -> dict:
    """Идемпотентно применить оверрайды. Меняет строку только если что-то реально
    изменилось, и тогда же сбрасывает request-состояние. Возвращает статистику.

    Выбор строки идёт по паре `(name, source_type)` — естественному ключу таблицы
    (`idx_sources_name_type`), а не по одному имени: одно издание живёт двумя строками
    (сайт + telegram-канал) под общим именем. Поиск по одному `name` с `fetchone()`
    отдавал строку на усмотрение плана запроса — на seq scan оверрайд ложился на
    выключенный telegram-двойник, и `changed=1` означал «починен не тот источник».

    Неоднозначность (имя без `source_type` совпало с несколькими строками) — это ОТКАЗ
    с именем в отчёте, а не выбор наугад: молча испорченный второй источник дороже
    непримененного оверрайда.
    """
    changed = 0
    unchanged = 0
    not_found = 0
    ambiguous = 0
    missing_names: list[str] = []
    ambiguous_names: list[str] = []
    with get_connection() as conn:
        for name, fields in SOURCE_OVERRIDES.items():
            new_strategy = fields["parse_strategy"]
            new_listing = fields.get("listing_url")
            new_rss = fields.get("rss_url")
            new_url = fields.get("url")
            new_region = fields.get("network_region")
            new_selector = fields.get("listing_selector")
            # Эти два оверрайд раньше НЕ умел: прописанные в реестре, они молча
            # никуда не доезжали. Именно их не хватает JPT, чтобы брать карточки
            # ленты, а не рубрики, и видеть дату публикации.
            new_link_selector = fields.get("article_link_selector")
            new_date_selector = fields.get("article_date_selector")
            want_type = fields.get("source_type")
            select = ("SELECT id, parse_strategy, listing_url, rss_url, url, network_region, "
                      "source_type, listing_selector, article_link_selector, "
                      "article_date_selector FROM sources WHERE name = %s")
            select_params: tuple = (name,)
            if want_type is not None:
                select += " AND source_type = %s"
                select_params += (want_type,)
            rows = conn.execute(select + " ORDER BY id", select_params).fetchall()
            if not rows:
                not_found += 1
                missing_names.append(name if want_type is None else f"{name} [{want_type}]")
                logger.warning("source override: источник %r (source_type=%r) не найден в БД",
                               name, want_type)
                continue
            if len(rows) > 1:
                # Сузить ключ может только сам реестр — угадывать здесь нечего.
                ambiguous += 1
                ambiguous_names.append(name)
                logger.error(
                    "source override: имя %r неоднозначно (совпало строк: %d — %s) — оверрайд "
                    "НЕ применён, добавьте source_type в запись реестра",
                    name, len(rows), ", ".join(f"id={r[0]} {r[6]}" for r in rows),
                )
                continue
            (source_id, cur_strategy, cur_listing, cur_rss, cur_url, cur_region, _,
             cur_selector, cur_link_selector, cur_date_selector) = rows[0]
            listing_changed = new_listing is not None and (cur_listing or "") != new_listing
            rss_changed = new_rss is not None and (cur_rss or "") != new_rss
            url_changed = new_url is not None and (cur_url or "") != new_url
            region_changed = new_region is not None and (cur_region or "auto") != new_region
            selector_changed = new_selector is not None and (cur_selector or "") != new_selector
            link_selector_changed = (new_link_selector is not None
                                     and (cur_link_selector or "") != new_link_selector)
            date_selector_changed = (new_date_selector is not None
                                     and (cur_date_selector or "") != new_date_selector)
            if (cur_strategy == new_strategy and not listing_changed and not rss_changed
                    and not url_changed and not region_changed and not selector_changed
                    and not link_selector_changed and not date_selector_changed):
                unchanged += 1
                continue

            sets = [
                "parse_strategy = %(strategy)s",
                "last_listing_hash = NULL",
                "last_seen_article_url = NULL",
                "last_seen_published_at = NULL",
                "updated_at = now()",
            ]
            params = {"id": source_id, "strategy": new_strategy}
            if new_listing is not None:
                sets.append("listing_url = %(listing_url)s")
                params["listing_url"] = new_listing
            if new_rss is not None:
                sets.append("rss_url = %(rss_url)s")
                params["rss_url"] = new_rss
            if new_url is not None:
                sets.append("url = %(url)s")
                params["url"] = new_url
            if new_region is not None:
                sets.append("network_region = %(network_region)s")
                params["network_region"] = new_region
            if new_selector is not None:
                sets.append("listing_selector = %(listing_selector)s")
                params["listing_selector"] = new_selector
            if new_link_selector is not None:
                sets.append("article_link_selector = %(article_link_selector)s")
                params["article_link_selector"] = new_link_selector
            if new_date_selector is not None:
                sets.append("article_date_selector = %(article_date_selector)s")
                params["article_date_selector"] = new_date_selector
            conn.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id = %(id)s", params)
            changed += 1
            logger.info("source override: %s → %s%s%s%s%s%s%s%s", name, new_strategy,
                        f" listing={new_listing}" if new_listing else "",
                        f" selector={new_selector}" if new_selector else "",
                        f" link_selector={new_link_selector}" if new_link_selector else "",
                        f" date_selector={new_date_selector}" if new_date_selector else "",
                        f" rss={new_rss}" if new_rss else "",
                        f" url={new_url}" if new_url else "",
                        f" region={new_region}" if new_region else "")
        conn.commit()
    return {"changed": changed, "unchanged": unchanged, "not_found": not_found,
            "ambiguous": ambiguous, "missing_names": missing_names,
            "ambiguous_names": ambiguous_names}
