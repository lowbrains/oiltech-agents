from oiltech_digest.ingestion import request_parser


HOME_HTML = b"""
<html>
  <body>
    <a href="/about">About</a>
    <a href="/news/2026/05/field-automation-rollout">Field automation rollout improves wellsite performance</a>
    <a href="/media/article/drilling-analytics-platform">Drilling analytics platform expands to offshore assets</a>
    <a href="https://external.example.com/story">External story</a>
  </body>
</html>
"""

ARTICLE_HTML = b"""
<html>
  <head>
    <meta property="og:title" content="Field automation rollout improves wellsite performance">
    <meta property="article:published_time" content="2026-05-20T08:30:00Z">
  </head>
  <body>
    <article>
      <p>The company deployed a field automation stack across producing wells and surface facilities.</p>
      <p>The program reduced manual interventions, improved uptime and gave engineers better production control.</p>
      <p>Additional industrial context keeps this article above teaser length and suitable for downstream AI stages.</p>
    </article>
  </body>
</html>
"""

SHORT_ARTICLE_HTML = b"""
<html>
  <head>
    <meta property="og:title" content="SLB and Liberty advance digital completions">
  </head>
  <body>
    <article>
      <p>SLB and Liberty Energy announced a digital completions collaboration for oilfield service operations.</p>
      <p>The release is brief but relevant for upstream technology monitoring.</p>
    </article>
  </body>
</html>
"""


QUERY_HOME_HTML = b"""
<html>
  <body>
    <a href="/news.php?id=12345&utm_source=rss">Field automation rollout improves wellsite performance metrics</a>
    <a href="/?p=678">Drilling analytics platform expands to offshore assets across the region</a>
    <a href="/?utm_source=newsletter">Subscribe to our newsletter for the latest updates</a>
  </body>
</html>
"""


MEDIA_HOME_HTML = b"""
<html>
  <body>
    <a href="/wp-content/uploads/2024/03/Oil-Gas-Producer.webp">Oil & Gas Producer / Well Trajectories / Production Bubbles</a>
    <a href="/files/report.pdf">Hydraulic fracturing technology report</a>
    <a href="/news/2026/05/field-automation-rollout">Field automation rollout improves wellsite performance</a>
  </body>
</html>
"""


def test_extract_candidate_links_prefers_article_like_paths():
    items = request_parser.extract_candidate_links("https://example.com", HOME_HTML, limit=10)

    assert len(items) == 2
    assert items[0].url.startswith("https://example.com/")
    assert all("about" not in item.url for item in items)


def test_extract_candidate_links_preserves_query_id_and_strips_tracking():
    # Бэклог #3: ID статьи в query больше не теряется (иначе ссылка вела на раздел/сайт).
    items = request_parser.extract_candidate_links("https://example.com", QUERY_HOME_HTML, limit=10)
    urls = [item.url for item in items]

    assert "https://example.com/news.php?id=12345" in urls   # query сохранён, utm отрезан
    assert "https://example.com?p=678" in urls               # query-only статья не схлопнулась в главную
    assert all("newsletter" not in url for url in urls)      # трекинг-ссылка на корень отброшена
    assert "https://example.com" not in urls                 # чистая главная не попадает в кандидаты


def test_extract_candidate_links_skips_media_files():
    items = request_parser.extract_candidate_links("https://example.com", MEDIA_HOME_HTML, limit=10)
    urls = [item.url for item in items]

    assert urls == ["https://example.com/news/2026/05/field-automation-rollout"]


def test_extract_candidate_links_skips_anchor_with_broken_encoding(monkeypatch):
    class BrokenAnchor:
        def get(self, key):
            return "/news/broken" if key == "href" else None

        def text_content(self):
            raise UnicodeDecodeError("utf-8", b"\xc6", 0, 1, "invalid continuation byte")

    assert request_parser._build_candidate_from_anchor("https://example.com", "example.com", BrokenAnchor()) is None


def test_clean_query_keeps_meaningful_strips_tracking():
    assert request_parser._clean_query("id=42&utm_source=x&utm_medium=y") == "id=42"
    assert request_parser._clean_query("utm_source=x&fbclid=z") == ""
    assert request_parser._clean_query("") == ""
    assert request_parser._clean_query("p=7&ref=home") == "p=7&ref=home"


def test_parse_article_page_extracts_title_date_and_body():
    title, published_at, raw_text = request_parser.parse_article_page(ARTICLE_HTML)

    assert "Field automation rollout" in title
    assert published_at is not None
    assert "reduced manual interventions" in raw_text


def test_fetch_article_candidate_keeps_short_relevant_press_release(monkeypatch):
    monkeypatch.setattr(request_parser, "fetch", lambda url: SHORT_ARTICLE_HTML)
    candidate = request_parser.CandidateLink(
        url="https://example.com/news/short-release",
        title="SLB and Liberty advance digital completions",
        score=8,
        published_at=None,
    )

    article = request_parser.fetch_article_candidate(
        candidate,
        {"id": 7, "name": "SLB", "category": "международные"},
    )

    assert article is not None
    assert article["url"] == candidate.url
    assert len(article["raw_text"]) >= 120


def test_parse_source_uses_listing_page_and_updates_last_seen(monkeypatch):
    fetched_urls = []
    inserted = []
    state = {}

    def fake_fetch(url):
        fetched_urls.append(url)
        if url == "https://example.com/news":
            return HOME_HTML
        if url == "https://example.com/news/2026/05/field-automation-rollout":
            return ARTICLE_HTML
        return None

    def fake_insert(article):
        inserted.append(article)
        return True

    monkeypatch.setattr(request_parser, "fetch", fake_fetch)
    monkeypatch.setattr(request_parser.repository, "insert_article", fake_insert)
    monkeypatch.setattr(request_parser.repository, "article_exists", lambda url: False)
    monkeypatch.setattr(request_parser.repository, "touch_last_parsed", lambda source_id: state.setdefault("touched", source_id))
    monkeypatch.setattr(
        request_parser.repository,
        "update_source_request_state",
        lambda source_id, **kwargs: state.update({"source_id": source_id, **kwargs}),
    )
    monkeypatch.setattr(
        request_parser,
        "extract_candidate_links",
        lambda source, listing_url, content, limit=12: [
            request_parser.CandidateLink(
                url="https://example.com/news/2026/05/field-automation-rollout",
                title="Field automation rollout improves wellsite performance",
                score=8,
                published_at=None,
            )
        ],
    )

    stats = request_parser.parse_source(
        {"id": 7, "url": "https://example.com", "listing_url": "https://example.com/news", "category": "международные"},
        article_limit=5,
    )

    assert stats["added"] == 1
    assert fetched_urls == [
        "https://example.com/news",
        "https://example.com/news/2026/05/field-automation-rollout",
    ]
    assert inserted[0]["url"] == "https://example.com/news/2026/05/field-automation-rollout"
    assert state["source_id"] == 7
    assert state["last_seen_article_url"] == "https://example.com/news/2026/05/field-automation-rollout"


def _field_automation_candidate():
    return request_parser.CandidateLink(
        url="https://example.com/news/2026/05/field-automation-rollout",
        title="Field automation rollout improves wellsite performance",
        score=8,
        published_at=None,
    )


def test_parse_source_skips_known_candidate_via_article_exists(monkeypatch):
    """Дедуп держится на article_exists (articles.url уникален), а не на listing_hash.
    Кандидат, уже лежащий в БД, пропускается без фетча и вставки."""
    touched = {}

    monkeypatch.setattr(request_parser, "fetch", lambda url: HOME_HTML)
    monkeypatch.setattr(
        request_parser,
        "extract_candidate_links",
        lambda source, listing_url, content, limit=12: [_field_automation_candidate()],
    )
    monkeypatch.setattr(request_parser.repository, "touch_last_parsed", lambda source_id: touched.setdefault("id", source_id))
    monkeypatch.setattr(request_parser.repository, "update_source_request_state", lambda source_id, **kwargs: None)
    monkeypatch.setattr(request_parser.repository, "insert_article", lambda article: (_ for _ in ()).throw(AssertionError("insert_article should not be called for a known candidate")))
    # кандидат уже в БД → пропускаем без фетча/вставки
    monkeypatch.setattr(request_parser.repository, "article_exists", lambda url: True)

    stats = request_parser.parse_source({"id": 9, "url": "https://example.com/news"})

    assert stats["added"] == 0
    assert stats["skipped_known"] == 1
    assert touched["id"] == 9


def test_parse_source_not_frozen_when_listing_hash_unchanged(monkeypatch):
    """Регресс на дедуп-заморозку: даже если listing_hash совпадает с прошлым
    прогоном, новые статьи (которых нет в БД) обязаны добавляться. Раньше
    short-circuit по listing_hash прятал их → источник застывал навсегда."""
    candidate = _field_automation_candidate()
    inserted = []

    def fake_fetch(url):
        if url == "https://example.com/news":
            return HOME_HTML
        if url == candidate.url:
            return ARTICLE_HTML
        return None

    monkeypatch.setattr(request_parser, "fetch", fake_fetch)
    monkeypatch.setattr(
        request_parser,
        "extract_candidate_links",
        lambda source, listing_url, content, limit=12: [candidate],
    )
    monkeypatch.setattr(request_parser.repository, "article_exists", lambda url: False)
    monkeypatch.setattr(request_parser.repository, "insert_article", lambda article: inserted.append(article) or True)
    monkeypatch.setattr(request_parser.repository, "touch_last_parsed", lambda source_id: None)
    monkeypatch.setattr(request_parser.repository, "update_source_request_state", lambda source_id, **kwargs: None)

    # last_listing_hash намеренно совпадает с текущим листингом — старый код
    # коротил бы на 0; новый обязан добавить незнакомую статью.
    source = {
        "id": 9,
        "url": "https://example.com",
        "listing_url": "https://example.com/news",
        "category": "международные",
        "last_listing_hash": request_parser._listing_hash([candidate]),
    }

    stats = request_parser.parse_source(source)

    assert stats["added"] == 1
    assert inserted[0]["url"] == candidate.url


# Реальный листинг Сургутнефтегаза: ссылки на статьи идут СО СЛЭШЕМ на конце.
# Так устроены многие корпоративные РФ-сайты (Bitrix): без слэша сервер отдаёт 404.
SLASH_HOME_HTML = """
<html><body>
  <a href="/">На главную</a>
  <a href="/press-center/press_releases/">Все пресс-релизы</a>
  <a href="/press-center/press_releases/preduprezhdenie-o-moshennicheskikh-deystviyakh/">
     Предупреждение о мошеннических действиях в отношении партнёров компании</a>
</body></html>
""".encode("utf-8")


def test_extract_candidate_links_keeps_trailing_slash():
    """Финальный слэш в адресе статьи сохраняется — иначе сайт отдаёт 404.

    Баг (найден на проде 20.07): путь резался через parts.path.rstrip('/'), и парсер шёл
    за статьёй по адресу БЕЗ слэша. Сургутнефтегаз на такой адрес отвечает 404 —
    листинг читался, кандидаты извлекались, а статей добавлялось 0 (источник выглядел
    «замолчавшим»). Срез слэша нужен был только чтобы отсеять главную, поэтому он
    остаётся в ПРОВЕРКЕ, но не в самом URL.
    """
    items = request_parser.extract_candidate_links("https://www.surgutneftegas.ru", SLASH_HOME_HTML, limit=10)
    urls = [item.url for item in items]

    assert "https://www.surgutneftegas.ru/press-center/press_releases/preduprezhdenie-o-moshennicheskikh-deystviyakh/" in urls, (
        "слэш срезан → сайт вернёт 404 и статья не добавится"
    )
    # Главная по-прежнему не считается статьёй (ради этого и был rstrip).
    assert "https://www.surgutneftegas.ru/" not in urls
    assert "https://www.surgutneftegas.ru" not in urls


TWO_BLOCK_LISTING = """
<html><body>
  <div class="search-item__wrap l-col-center">
    <a href="/business/27/04/2026/69ef5b0c">Shell купит канадскую энергетическую компанию ARC Resources</a>
  </div>
  <div class="search-item__wrap l-col-center">
    <a href="/quote/04/08/2026/6a71d91d">Цена нефти Brent упала почти на 4% после слов министра финансов США</a>
  </div>
  <div class="js-news-feed-list">
    <a href="/sport/24/08/2026/6a8c1efe">Мяч, которым Марадона забил гол «рукой Бога», продали за $3,35 млн</a>
    <a href="/politics/24/08/2026/6a8c0728">Львова-Белова заявила о сорванных попытках ее «захвата»</a>
    <a href="/society/24/08/2026/6a8c1763">Логистический магнат, миллиардер Клаус-Михаэль Кюне ушел из жизни</a>
  </div>
</body></html>
""".encode("utf-8")


def test_listing_selector_isolates_the_listing_from_a_sidebar_feed():
    """При заданном listing_selector кандидаты берутся ТОЛЬКО из него.

    Форма страницы взята с прода (#61, РБК 24.08): у федеральных СМИ на странице раздела
    живут ДВА блока ссылок — сама выдача (`div.search-item__wrap`) и сквозной сайдбар общей
    ленты (`div.js-news-feed-list`). Без селектора кандидаты собираются из обоих, и сайдбар
    выигрывает по очкам: замер на проде дал 8 кандидатов из сайдбара и ноль из выдачи, то
    есть источник выглядел бы починенным, а тащил бы прежний мусор.

    Здесь фиксируется механизм, а не вёрстка РБК: селектор задан — сайдбара в кандидатах нет.
    """
    source = {"listing_selector": ".search-item__wrap"}
    candidates = request_parser.extract_candidate_links(
        source, "https://www.rbc.ru/tags/?tag=нефть", TWO_BLOCK_LISTING, limit=12
    )

    urls = [c.url for c in candidates]
    assert len(urls) == 2, f"ожидались только карточки выдачи, получено: {urls}"
    assert all("/sport/" not in u and "/politics/" not in u and "/society/" not in u for u in urls)
    assert "https://www.rbc.ru/business/27/04/2026/69ef5b0c" in urls
    assert "https://www.rbc.ru/quote/04/08/2026/6a71d91d" in urls

    # Без селектора сайдбар попадает в кандидаты — то самое поведение, от которого спасаемся.
    without_selector = request_parser.extract_candidate_links(
        {}, "https://www.rbc.ru/tags/?tag=нефть", TWO_BLOCK_LISTING, limit=12
    )
    assert any("/sport/" in c.url for c in without_selector)


JPT_LISTING_HTML = b"""
<html>
  <body>
    <div class="Navigation"><a href="/topic/fracturing-pressure-pumping">Fracturing</a></div>
    <div class="PromoB">
      <div class="PromoB-content">
        <div class="PromoB-title"><a href="/bp-secures-loran-phase-2-offshore-exploration">BP secures Loran Phase 2 offshore exploration</a></div>
        <div class="PromoB-by-line">August 25, 2026 &bull; JPT Staff &bull; Journal of Petroleum Technology</div>
      </div>
    </div>
    <div class="PromoA">
      <div class="PromoA-content">
        <div class="PromoA-title"><a href="/from-hydrocarbon-recovery-to-in-situ-mineral-leaching">From hydrocarbon recovery to in-situ mineral leaching</a></div>
        <div class="PromoA-by-line">August 17, 2026 &bull; SPE Flow Measurement Technical Section</div>
      </div>
    </div>
  </body>
</html>
"""


def test_parse_datetime_reads_date_embedded_in_a_by_line():
    """Дата в карточке издания приходит вместе с автором и названием журнала.

    Строгий разбор на такой строке падает, и источник получает published_at=None —
    именно так JPT годами шёл с пустым last_seen_published_at."""
    parsed = request_parser._parse_datetime(
        "August 25, 2026 • JPT Staff • Journal of Petroleum Technology"
    )

    assert parsed is not None
    assert parsed.date().isoformat() == "2026-08-25"


def test_parse_datetime_does_not_invent_dates_from_arbitrary_text():
    """Защита от fuzzy-разбора: он выдумывал 2026-05-12 из «Section 5 of 12»
    и 2026-03-25 из «Halliburton 2026 Q3 results webcast»."""
    for noise in ("JPT Staff • Journal of Petroleum Technology",
                  "Section 5 of 12",
                  "Halliburton 2026 Q3 results webcast",
                  "Read more"):
        assert request_parser._parse_datetime(noise) is None, noise


def test_promo_selectors_take_articles_with_dates_and_skip_topic_links():
    """Селекторный путь на разметке JPT: только карточки, с датами, без рубрик."""
    source = {
        "listing_selector": ".PromoB, .PromoA",
        "article_link_selector": ".PromoB-title a, .PromoA-title a",
        "article_date_selector": ".PromoB-by-line, .PromoA-by-line",
    }
    candidates = request_parser.extract_candidate_links(
        source, "https://jpt.spe.org/latest-news", JPT_LISTING_HTML, limit=50
    )

    urls = [c.url for c in candidates]
    assert len(candidates) == 2
    assert not any("/topic/" in u for u in urls), urls
    assert all(c.published_at is not None for c in candidates)
    assert {c.published_at.date().isoformat() for c in candidates} == {"2026-08-25", "2026-08-17"}


def test_parse_article_page_does_not_glue_headline_with_lead():
    """#55 / жалобы заказчика 22.08 «текст сливается» и 08.09 «потеряли Ва».

    У Neftegaz.ru лид лежит ВНУТРИ того же <h1>, а XPath string() склеивает текст
    соседних узлов вплотную — получалось «технологии ГРПНа Южно-Приобском».
    Здесь проверяем именно разделитель, а не весь заголовок целиком.
    """
    markup = (
        "<html><body><h1>«Газпромнефть-Хантос» развивает российские технологии ГРП"
        "<span class='lead'>На Южно-Приобском месторождении работает первый "
        "российский серийный флот гидроразрыва</span></h1>"
        "<p>" + ("Текст статьи про бурение и ГРП. " * 40) + "</p></body></html>"
    )
    title, _published, _text = request_parser.parse_article_page(markup)
    assert "ГРПНа" not in title, "заголовок склеен с лидом без пробела"
    assert "ГРП На Южно-Приобском" in title


def test_parse_article_page_keeps_plain_headline_unchanged():
    """Обычный заголовок без вложенных элементов правка не должна трогать."""
    markup = (
        "<html><body><h1>Газпром нефть испытала буровые установки на Ямале</h1>"
        "<p>" + ("Текст статьи про сейсморазведку. " * 40) + "</p></body></html>"
    )
    title, _published, _text = request_parser.parse_article_page(markup)
    assert title == "Газпром нефть испытала буровые установки на Ямале"


# --- Выбор ссылок внутри источника (18.09) -----------------------------------
# Каждый тест ниже воспроизводит источник из обхода 30 молчащих: без правки он
# падает, потому что парсер брал не те ссылки или не брал никаких.

NEWSROOM_HTML = b"""
<html><body>
  <ul class="news">
    <li class="item"><a href="/news/zeta-old-release">Zeta: pinned release about company fraud warning</a><span>02.04.2025</span></li>
    <li class="item"><a href="/news/beta-fresh-contract">Beta contract awarded for deepwater completions</a><span>17.09.2026</span></li>
    <li class="item"><a href="/news/alpha-middle-news">Alpha acquisition closes after regulatory approval</a><span>01.09.2026</span></li>
  </ul>
</body></html>
"""


def test_candidates_go_newest_first_not_oldest_or_alphabetical():
    """Прежняя сортировка отдавала под лимит самые СТАРЫЕ из датированных, а при
    равенстве — первые по алфавиту адреса. Закреплённое предупреждение (2025) и
    «alpha» по алфавиту вытесняли свежий контракт."""
    candidates = request_parser.extract_candidate_links("https://example.com/news", NEWSROOM_HTML, limit=2)
    assert [c.url.rsplit("/", 1)[-1] for c in candidates] == ["beta-fresh-contract", "alpha-middle-news"]
    assert candidates[0].published_at.date().isoformat() == "2026-09-17"


def test_selector_keeps_page_order_when_items_have_no_dates():
    html = b"""<html><body><div class="feed">
      <a class="t" href="/p/zulu-first-on-page">Zulu is first on the page and the freshest item</a>
      <a class="t" href="/p/alpha-second-on-page">Alpha is second on the page and older item</a>
    </div></body></html>"""
    source = {"article_link_selector": ".feed a.t"}
    candidates = request_parser.extract_candidate_links(source, "https://example.com/p", html, limit=1)
    assert candidates[0].url.endswith("zulu-first-on-page"), "селектор задан под ленту — её порядок и есть свежесть"


def test_image_only_link_takes_title_from_its_card():
    """Белоруснефть: ссылка обёрнута вокруг картинки, заголовок — в h3 карточки.
    Раньше такая ссылка отбрасывалась (текст < 18), и лента не давала ни одной статьи."""
    html = """<html><body><ul>
      <li class="tile"><a href="/ru/detail-pages/event/-2026.09.17-00001/"><img src="x.jpg" alt=""></a>
        <h3>Стартовал прием заявок на Хакатон «Код Будущего»</h3><span>17 сентября 2026</span></li>
      <li class="tile"><a href="/ru/detail-pages/event/-2026.09.15-00002/"><img src="y.jpg" alt=""></a>
        <h3>Белоруснефть подписала соглашение о сотрудничестве</h3><span>15 сентября 2026</span></li>
    </ul></body></html>""".encode()
    # Как источник и настроен: селектор под плитки ленты. Без него ссылки с «event» в
    # адресе режет общий фильтр календарей мероприятий — селектор человека главнее.
    source = {"article_link_selector": "li.tile a[href*='/detail-pages/']"}
    candidates = request_parser.extract_candidate_links(
        source, "https://www.belorusneft.by/ru/mediacenter/news/", html, limit=5)
    assert [c.title for c in candidates] == [
        "Стартовал прием заявок на Хакатон «Код Будущего»",
        "Белоруснефть подписала соглашение о сотрудничестве",
    ]
    assert candidates[0].published_at.date().isoformat() == "2026-09-17", "дата — из карточки своей статьи"


def test_generic_link_text_is_not_used_as_title():
    """Weatherford: «View Press Release» — ровно 18 знаков, прежний порог его пропускал
    заголовком статьи."""
    html = b"""<html><body><table><tr>
      <td>03 Sep 2026</td><td><h4>Weatherford Shareholders Approve Redomestication to Delaware</h4></td>
      <td><a href="/news/news-article/?ItemID=18811">View Press Release</a></td>
    </tr></table></body></html>"""
    candidates = request_parser.extract_candidate_links("https://www.weatherford.com/news/", html, limit=5)
    assert len(candidates) == 1
    assert candidates[0].title == "Weatherford Shareholders Approve Redomestication to Delaware"
    assert candidates[0].url.endswith("?ItemID=18811")


def test_subdomain_links_belong_to_the_source_but_lookalike_domains_do_not():
    """ТПУ: новости на news.tpu.ru, источник заведён на tpu.ru — строгое равенство
    хостов отсекало их все. Похожий домен mytpu.ru своим не становится."""
    html = b"""<html><body>
      <a href="https://news.tpu.ru/news/v-tpu-nashli-metod-ochistki-vody">V TPU nashli metod ochistki vody iz othodov</a>
      <a href="https://mytpu.ru/news/chuzhaya-novost-pro-drugoy-vuz">Chuzhaya novost pro drugoy vuz i ego zhizn</a>
    </body></html>"""
    urls = [c.url for c in request_parser.extract_candidate_links("https://tpu.ru", html, limit=5)]
    assert urls == ["https://news.tpu.ru/news/v-tpu-nashli-metod-ochistki-vody"]


def test_list_dates_in_real_formats_are_recognised():
    """Формы дат из обхода 18.09 — прежний разбор знал только 2026-09-17."""
    from oiltech_digest.ingestion import dates

    cases = {
        "17.09.2026": "2026-09-17", "17 сентября 2026": "2026-09-17", "24/08/2026": "2026-08-24",
        "Sep 18, 2026": "2026-09-18", "10 Sept 2026": "2026-09-10", "September 17, 2026": "2026-09-17",
    }
    for text, expected in cases.items():
        assert dates.date_from_text(text).date().isoformat() == expected, text
    assert dates.date_from_text("Section 5 of 12") is None, "дату не выдумываем"
    assert dates.date_from_url("https://tedo.ru/press-center/news-14092026").date().isoformat() == "2026-09-14"


def test_page_without_declared_charset_is_not_turned_into_mojibake():
    """58 статей на проде с «Ð¡Ñ…» в заголовке и тексте: кодировка у сайта объявлена
    только в HTTP-заголовке, и lxml читал UTF-8 как Latin-1."""
    body = ("<html><head><title>Сколтех</title></head><body><h1>Новые данные о гидратах</h1>"
            "<article><p>" + "Учёные Сколтеха измерили газопроницаемость гидратов. " * 10 + "</p></article>"
            "</body></html>")
    for raw in (body.encode("utf-8"), body.encode("cp1251")):
        title, _, text = request_parser.parse_article_page(raw)
        assert title == "Новые данные о гидратах"
        assert "газопроницаемость" in text


def test_html5_meta_charset_page_is_decoded_right():
    """Тот самый случай с прода: кодировка ОБЪЯВЛЕНА (<meta charset="utf-8">), но
    libxml2 при разборе байтов её не учитывает. Прошлый тест (страница вовсе без
    объявления) этого не ловил — и правка ушла на прод с кракозябрами у Сколтеха."""
    # Как рендерит Nuxt (Сколтех): <title> с кириллицей стоит ДО <meta charset>.
    # Именно это ломает libxml2 — позиция объявления сама по себе ни при чём.
    body = ('<!doctype html><html><head><title>Новости | Сколтех</title>'
            '<meta data-n-head="ssr" charset="utf-8"></head><body>'
            '<a href="/news/novye-dannye-o-gidratah">Новые данные о газопроницаемости гидратов</a>'
            "</body></html>").encode("utf-8")
    candidates = request_parser.extract_candidate_links("https://skoltech.ru/news", body, limit=3)
    assert candidates[0].title == "Новые данные о газопроницаемости гидратов"


def test_learn_more_card_takes_title_from_first_meaningful_fragment():
    """Mubadala: у свежих новостей ссылка «Learn more», заголовок — обычный блок, а
    вся карточка с анонсом длиннее 300 знаков. Раньше такие ссылки отбрасывались,
    и под лимит шли только старые (2022–2023) с текстовыми ссылками."""
    teaser = "Abu Dhabi, UAE – Mubadala Energy today published its report. " * 6
    html = f"""<html><body><div class="grid">
      <div class="card"><span>1 Sep</span><div class="h">Mubadala Energy Publishes 2025 Sustainability Report</div>
        <p>{teaser}</p><div><a href="/news/mubadala-energy-publishes-2025-sustainability-report/">Learn more</a></div></div>
      <div class="card"><span>15 May</span><div class="h">Final Investment Decision for Caturus announced</div>
        <p>{teaser}</p><div><a href="/news/fid-caturus/">Learn more</a></div></div>
    </div></body></html>""".encode()
    source = {"article_link_selector": 'a[href*="/news/"]'}
    candidates = request_parser.extract_candidate_links(source, "https://mubadalaenergy.com/all-news/", html, limit=5)
    assert [c.title for c in candidates] == [
        "Mubadala Energy Publishes 2025 Sustainability Report",
        "Final Investment Decision for Caturus announced",
    ]


def test_fallback_text_does_not_swallow_scripts():
    """ТеДо: основной текст не выделился, и запасной путь брал text_content() —
    вместе со <script>. В базе 36 статей длиннее 50 тыс. знаков со скриптами."""
    body = ("<html><head><title>ТеДо: объём рынка</title>"
            "<script>window.__NEXT_DATA__ = {\"props\": \"" + "x" * 100000 + "\"}</script>"
            "<style>.a{color:red}</style></head><body><div>Рынок ассистивных технологий вырос."
            "</div></body></html>")  # короче порога: основной текст не выделяется
    _, _, text = request_parser.parse_article_page(body.encode("utf-8"))
    assert "__NEXT_DATA__" not in text and "color:red" not in text
    assert "ассистивных технологий" in text
    assert len(text) < 1000


def test_relative_links_resolve_from_base_href_after_redirect():
    """CNOOC: лента /zxzx/gsxw/ уходит на /zxzx/gsxw/gsxw/, ссылки там относительные
    («./202609/t….html»). Парсер склеивал их от адреса из настройки — все 404.
    Браузер парсера вписывает <base> с конечным адресом, разбор его учитывает."""
    from oiltech_digest.ingestion.playwright_parser import with_base_href

    rendered = with_base_href(
        '<html><head><title>公司新闻</title></head><body>'
        '<a href="./202609/t20260914_122684.html">我国承建的乌干达首个商业油田开发项目核心工程完工</a></body></html>',
        "https://www.cnooc.com.cn/zxzx/gsxw/gsxw/")
    candidates = request_parser.extract_candidate_links(
        "https://www.cnooc.com.cn/zxzx/gsxw/", rendered.encode("utf-8"), limit=3)
    assert candidates[0].url == "https://www.cnooc.com.cn/zxzx/gsxw/gsxw/202609/t20260914_122684.html"
    assert candidates[0].published_at.date().isoformat() == "2026-09-14", "дата слитно в адресе"


def test_existing_base_href_is_not_overwritten():
    from oiltech_digest.ingestion.playwright_parser import with_base_href

    html = '<html><head><base href="https://cdn.example.com/"></head><body></body></html>'
    assert with_base_href(html, "https://example.com/news/") == html


def test_card_title_beats_section_title_when_page_has_no_headline_markup():
    # CNOOC: ни og:title, ни <h1>, а <title> — название раздела, одинаковое у всех новостей.
    page = """<html><head><title>中国海洋石油集团有限公司 公司新闻</title></head><body>
      <div class="title">我国承建的乌干达首个商业油田开发项目核心工程完工</div>
      <div class="content"><p>""" + "正文内容。" * 60 + """</p></div></body></html>"""

    title, _, _ = request_parser.parse_article_page(page, "我国承建的乌干达首个商业油田开发项目核心工程完工")

    assert title == "我国承建的乌干达首个商业油田开发项目核心工程完工"


def test_short_card_caption_does_not_replace_page_title():
    page = "<html><head><title>ADNOC awards drilling contract for Hail and Ghasha</title></head><body></body></html>"

    title, _, _ = request_parser.parse_article_page(page, "Learn more")

    assert title == "ADNOC awards drilling contract for Hail and Ghasha"
