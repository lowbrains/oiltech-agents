"""Нормализация данных статьи: очистка HTML, парсинг дат, картинка, content_hash.

`clean_html` / `parse_date` / `extract_image` перенесены из прототипа
`oil-tech-digest-bot/parser.py`; `compute_content_hash` — новое.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from dateutil import parser as dateparser
from lxml import etree
from lxml import html as lxml_html

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Допуск на часовые пояса и опережающие публикации. Даты дальше этого порога в
# будущем считаем недостоверными: типичный источник — анонсы событий из
# «календаря» на сайте (напр. Equinor «Q3 results — analyst conference»),
# которые скрапер ошибочно принимает за дату публикации.
FUTURE_TOLERANCE_DAYS = 2


def is_future_date(dt: datetime | None, tolerance_days: int = FUTURE_TOLERANCE_DAYS) -> bool:
    """True, если дата заметно в будущем (вероятно, ошибочно распарсенное событие)."""
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt > datetime.now(timezone.utc) + timedelta(days=tolerance_days)


def clean_html(text: str) -> str:
    """Снять HTML-теги, расшифровать entities, схлопнуть пробелы."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def parse_date(entry) -> datetime | None:
    """Дата публикации из RSS-entry (published/updated/created) → aware datetime (UTC).

    None, если ни одно поле не распарсилось — статья всё равно сохранится.
    """
    for field in ("published", "updated", "created"):
        raw = entry.get(field, "") if hasattr(entry, "get") else ""
        if raw:
            try:
                dt = dateparser.parse(raw)
            except (ValueError, TypeError, OverflowError):
                continue
            if dt is None:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if is_future_date(dt):
                continue  # дата из будущего — не доверяем, пробуем следующее поле
            return dt
    return None


def extract_image(entry) -> str:
    """URL картинки из media_thumbnail / media_content / enclosures (для будущего #4)."""
    media = entry.get("media_thumbnail", []) if hasattr(entry, "get") else []
    if media and isinstance(media, list):
        return media[0].get("url", "")

    media_content = entry.get("media_content", []) if hasattr(entry, "get") else []
    if media_content and isinstance(media_content, list):
        for mc in media_content:
            if mc.get("medium") == "image" or "image" in mc.get("type", ""):
                return mc.get("url", "")

    enclosures = entry.get("enclosures", []) if hasattr(entry, "get") else []
    if enclosures:
        for enc in enclosures:
            if "image" in enc.get("type", ""):
                return enc.get("href", enc.get("url", ""))
    return ""


def _normalize_title(title: str) -> str:
    return _WS_RE.sub(" ", (title or "").strip().lower())


def _normalize_url(url: str) -> str:
    """host+path в нижнем регистре, без схемы, query (utm и пр.) и хвостового слэша."""
    try:
        parts = urlsplit((url or "").strip().lower())
        if not parts.netloc:
            return (url or "").strip().lower()
        return f"{parts.netloc}{parts.path.rstrip('/')}"
    except ValueError:
        return (url or "").strip().lower()


# Параметры query, которые говорят, ОТКУДА пришёл читатель, а не КАКАЯ это статья.
# Список — по замеру всех адресов прода 25.09, а не «из общих соображений»: `from` у РБК
# (1935 адресов, 18 значений на одни и те же статьи), `ysclid` (Яндекс), подписи `gaa_*`;
# остальное — общеизвестная метка рекламных систем. Всё прочее в query — номер статьи:
# `id`, `ID`, `rid`, `news-item`, `id_4`, `ELEMENT_ID`, `itemid`…
# Список продублирован в бэкфилле `url_key` в schema.sql — правка здесь без правки там
# даёт красный тест (тест строит адреса из этого самого списка).
# У `request_parser._TRACKING_PARAMS` другая задача — чистит адрес, по которому качаем
# страницу, — и там список уже; ключу тождества это не мешает, пока там нет имён, которых
# нет здесь.
_TRACKING_QUERY_PARAMS = frozenset({
    "from", "ysclid", "yclid", "ymclid", "fbclid", "gclid", "igshid", "_openstat",
    "mc_cid", "mc_eid",
})
_TRACKING_QUERY_PREFIXES = ("utm_", "gaa_")


def _identity_query(query: str) -> str:
    """Значимая часть query: без трекинговых параметров, в порядке по алфавиту.

    Пары берутся как есть, без раскодирования, — тем же правилом считает бэкфилл в
    schema.sql, и ключи из Python и из SQL обязаны совпадать до символа.
    """
    pairs = []
    for pair in query.split("&"):
        name = pair.split("=", 1)[0]
        if not pair or name in _TRACKING_QUERY_PARAMS or name.startswith(_TRACKING_QUERY_PREFIXES):
            continue
        pairs.append(pair)
    return "&".join(sorted(pairs))


def url_key(url: str) -> str:
    """Ключ тождества статьи по адресу: host+path+значимый query, без схемы, www и слэша.

    Замер прода 13.09: за 90 дней 940 лишних статей — это ОДИН И ТОТ ЖЕ адрес в разных
    написаниях. Три причины поимённо: query-хвосты (`?from=main_lines_11` против
    `?from=newsfeed` у РБК), схема (`http://` против `https://` у Ростеха) и хвостовой
    слэш (Wood Mackenzie). Каждая такая копия проходила полный ИИ-конвейер заново и
    занимала отдельную карточку в ленте — ровно то, на что жаловался заказчик
    («все 4 новости об одном»).

    Query 13.09 срезался ЦЕЛИКОМ — и это склеило все статьи сайтов, где номер статьи
    живёт в query: 25.09 у Минэнерго отбивались 25 пунктов ленты из 25, у EIA 17 из 18,
    у РГУ Губкина 257 из 260, свежие релизы Лукойла и Новатэка — как «дубль» статьи
    месячной давности; схема 13.09 заодно спрятала уже собранные. Теперь срезаются только
    трекинговые параметры (`_TRACKING_QUERY_PARAMS`), остальные остаются в ключе.

    ОТДЕЛЬНАЯ функция, а не вызов `_normalize_url`, по двум причинам: здесь дополнительно
    снимается `www.` (тот же материал приходит и с ним, и без), и по этому ключу строится
    уникальность в БД — менять `_normalize_url` нельзя, на нём висят уже посчитанные
    `content_hash` всего корпуса.
    """
    base = _normalize_url(url)
    base = base[4:] if base.startswith("www.") else base
    try:
        parts = urlsplit((url or "").strip().lower())
    except ValueError:
        return base
    if not parts.netloc:
        return base  # не адрес — _normalize_url уже вернул строку целиком
    identity = _identity_query(parts.query)
    return f"{base}?{identity}" if identity else base


def compute_content_hash(title: str, url: str) -> str:
    """sha256 от нормализованных title|url. Мягкий сигнал кросс-источниковых дублей."""
    basis = f"{_normalize_title(title)}|{_normalize_url(url)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def compute_body_hash(raw_text: str | None) -> str | None:
    """sha256 от нормализованного ТЕЛА. Ключ для защиты «одно тело — многим статьям» (№24).

    Отличается от compute_content_hash: тот про заголовок+URL (дубль публикации), этот —
    про сам текст. Пробелы схлопываем, чтобы косметика вёрстки не давала разных хэшей.
    """
    if not raw_text:
        return None
    normalized = re.sub(r"\s+", " ", raw_text).strip().lower()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# Слова, которые есть в любом тексте и потому ничего не доказывают о принадлежности.
_OWNERSHIP_STOPWORDS = frozenset("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее
мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если или быть был для
тем чтобы чем это эта эти этот при над под без до после через между также млн млрд тыс
года году год гг рф сша the a an and or of to in for on at by with from is are was were be
been has have had it that this these those as not but which who will would can could may
might more most new its into
""".split())

# Заголовок короче этого (в значимых словах) судить не позволяет: у «SOCAR» или «May 2026»
# пересечения нет по естественным причинам, а не из-за подмены тела.
_OWNERSHIP_MIN_TITLE_WORDS = 4
# Порог подобран замером на проде 28.07 (400 испорченных против 600 контрольных статей).
_OWNERSHIP_MIN_RATIO = 0.20


def significant_words(text: str) -> list[str]:
    """Значимые слова для сверки принадлежности: без стоп-слов и коротышей.

    Русские слова режем до первых 5 букв — иначе падежи («Роснефти» ↔ «Роснефть»)
    ломали бы сопоставление заголовка с телом.
    """
    out: list[str] = []
    for word in re.findall(r"[а-яёa-z0-9]+", (text or "").lower()):
        if len(word) <= 3 or word in _OWNERSHIP_STOPWORDS:
            continue
        out.append(word[:5] if len(word) > 5 else word)
    return out


def title_matches_body(title: str, body: str) -> bool:
    """Похоже ли, что этот текст принадлежит ЭТОМУ заголовку (защита от подмены, №24).

    Дефект 28.07: у 662 видимых статей (10.4% ленты, у Neftegaz.ru — 37.7%) тело было
    от ДРУГОЙ новости. Никакой проверки принадлежности в коде не существовало: текст
    принимался, если он просто ДЛИННЕЕ прежнего, поэтому листинг, пейвол или «избранный»
    материал из JSON-LD побеждали настоящую статью.

    Проверка намеренно мягкая — цена ложного отказа выше цены пропуска: отвергнутый текст
    означает потерю живой статьи, а пропущенный ловится следующими рубежами. Поэтому:
    судим только по достаточно длинным заголовкам и требуем совпадения всего 20% слов.
    """
    body_words = set(significant_words(body))
    if not body_words:
        return True

    # Заголовок часто несёт хвост с названием издания («… — Новости о нефти и газе»).
    # Такой хвост в теле статьи не встречается и топил бы долю совпадения: замер показал,
    # что из-за него страж отвергал ВЕРНЫЕ статьи OilCapital (19.6% его ленты). Поэтому
    # режем по разделителям и судим по самому «своему» сегменту, а не по строке целиком.
    best_ratio = 0.0
    judged = False
    for segment in re.split(r"\s+[—–|·]\s+|\s+-\s+", title or ""):
        segment_words = set(significant_words(segment))
        if len(segment_words) < _OWNERSHIP_MIN_TITLE_WORDS:
            continue  # слишком короткий кусок судить не позволяет
        judged = True
        best_ratio = max(best_ratio, len(segment_words & body_words) / len(segment_words))

    if not judged:
        # Ни один сегмент не даёт опоры («SOCAR», «May 2026») — не мешаем.
        return True
    return best_ratio >= _OWNERSHIP_MIN_RATIO


# Маркеры «продолжение по ссылке» — типичный признак обрезанной RSS-ленты
_TRUNCATION_TAIL_MARKERS = (
    "read more", "read the full", "read full", "continue reading", "see more",
    "view more", "full story", "[…]", "[...]",
    "читать далее", "читать полностью", "подробнее", "продолжение", "далее по ссылке",
)

# Минимум символов: короче — почти наверняка только анонс, а не полный текст
TRUNCATION_MIN_CHARS = 280


def is_truncated(raw_text: str, min_chars: int = TRUNCATION_MIN_CHARS) -> bool:
    """Эвристика: похоже ли, что RSS отдал сокращённый/обрезанный текст.

    Срабатывает при: пустом тексте; концовке-многоточии; маркерах «читать далее /
    read more»; слишком коротком теле. Это сигнал для ручной проверки/дозагрузки,
    а не строгий критерий.
    """
    text = (raw_text or "").strip()
    if not text:
        return True
    if text.endswith(("…", "...", "[…]", "[...]")):
        return True
    tail = text[-60:].lower()
    if any(marker in tail for marker in _TRUNCATION_TAIL_MARKERS):
        return True
    if len(text) < min_chars:
        return True
    return False


# =========================================================================
# Эмодзи в тексте, который видит читатель (решение владельца 12.09)
# =========================================================================
# Чистим НА ВЫДАЧЕ, а не при вставке: требование звучало как «чтобы в любой части
# сигнала — название, суть и так далее — не было эмодзи». Поэтому `articles.title`
# и `raw_text` в базе остаются как есть, `content_hash` не меняется, бэкфилл не
# нужен, а решение обратимо.
#
# ГРАНИЦА выбрана замером, а не на глаз. Наивный подход «вырезать всё
# пиктографическое» (`\p{Extended_Pictographic}` / `\p{Emoji}`) на 670 тыс. знаков
# реальных материалов августовского выпуска дал 11 срабатываний, и ВСЕ 11 ложные:
# © ® ™ → ↓ ✓. Настоящих эмодзи там не было ни одного.
#
# Работает свойство Emoji_Presentation — «символ по умолчанию рисуется как эмодзи».
# Оно не задевает © ® ™ ↔ ⚠ ☑, но берёт всю телеграм-грязь. К нему добавлены:
#   * текстовый символ + U+FE0F (VS16) — автор ЯВНО попросил эмодзи-вид: ⚠️ ☑️ ❤️;
#   * модификаторы тона кожи, ZWJ-склейки, клавиша-накладка, tag-символы субфлагов.
# ОСТАЮТСЯ: © ® ™, валюты, стрелки → ↓ (несут смысл: «добыча ↓ 3%»), ± °, ✓, «» — –.
#
# Диапазоны СГЕНЕРИРОВАНЫ кодом, не записаны по памяти:
#   regex 2026.7.19 / Unicode 15.1.0 → 1219 кодпоинтов, 81 диапазон.
#   Воспроизвести: [c for c in range(0x110000) if regex.fullmatch(r'\p{Emoji_Presentation}', chr(c))]
# Пакет `regex` намеренно НЕ добавлен в requirements.txt — в докере его нет, и код
# на нём прошёл бы локальные тесты и упал на проде. Здесь только stdlib `re`.
_EMOJI_PRESENTATION = (
    "\U0000231A-\U0000231B\U000023E9-\U000023EC\U000023F0\U000023F3"
    "\U000025FD-\U000025FE\U00002614-\U00002615\U00002648-\U00002653\U0000267F"
    "\U00002693\U000026A1\U000026AA-\U000026AB\U000026BD-\U000026BE"
    "\U000026C4-\U000026C5\U000026CE\U000026D4\U000026EA"
    "\U000026F2-\U000026F3\U000026F5\U000026FA\U000026FD"
    "\U00002705\U0000270A-\U0000270B\U00002728\U0000274C"
    "\U0000274E\U00002753-\U00002755\U00002757\U00002795-\U00002797"
    "\U000027B0\U000027BF\U00002B1B-\U00002B1C\U00002B50"
    "\U00002B55\U0001F004\U0001F0CF\U0001F18E"
    "\U0001F191-\U0001F19A\U0001F1E6-\U0001F1FF\U0001F201\U0001F21A"
    "\U0001F22F\U0001F232-\U0001F236\U0001F238-\U0001F23A\U0001F250-\U0001F251"
    "\U0001F300-\U0001F320\U0001F32D-\U0001F335\U0001F337-\U0001F37C\U0001F37E-\U0001F393"
    "\U0001F3A0-\U0001F3CA\U0001F3CF-\U0001F3D3\U0001F3E0-\U0001F3F0\U0001F3F4"
    "\U0001F3F8-\U0001F43E\U0001F440\U0001F442-\U0001F4FC\U0001F4FF-\U0001F53D"
    "\U0001F54B-\U0001F54E\U0001F550-\U0001F567\U0001F57A\U0001F595-\U0001F596"
    "\U0001F5A4\U0001F5FB-\U0001F64F\U0001F680-\U0001F6C5\U0001F6CC"
    "\U0001F6D0-\U0001F6D2\U0001F6D5-\U0001F6D8\U0001F6DC-\U0001F6DF\U0001F6EB-\U0001F6EC"
    "\U0001F6F4-\U0001F6FC\U0001F7E0-\U0001F7EB\U0001F7F0\U0001F90C-\U0001F93A"
    "\U0001F93C-\U0001F945\U0001F947-\U0001F9FF\U0001FA70-\U0001FA7C\U0001FA80-\U0001FA8A"
    "\U0001FA8E-\U0001FAC6\U0001FAC8\U0001FACD-\U0001FADC\U0001FADF-\U0001FAEA"
    "\U0001FAEF-\U0001FAF8"
)

# Служебные символы эмодзи-склейки: сами по себе невидимы, но осиротев — мусорят.
# Сюда же VS16/VS15 — «нарисуй предыдущий символ как эмодзи / как текст».
_EMOJI_MODIFIERS = (
    "\U0001F3FB-\U0001F3FF"  # модификаторы тона кожи
    "\U0000FE0F"             # VS16 — «покажи предыдущий символ как эмодзи»
    "\U0000FE0E"             # VS15 — парный к нему
    "\U0000200D"             # ZWJ — склейка эмодзи в составные (👨‍👩‍👧)
    "\U000020E3"             # клавиша-накладка (1-с-накладкой → остаётся голая «1»)
    "\U000E0020-\U000E007F"  # tag-символы субфлагов
)

_EMOJI_RE = re.compile(f"[{_EMOJI_PRESENTATION}]")
_EMOJI_MODIFIER_RE = re.compile(f"[{_EMOJI_MODIFIERS}]")
# Пробелы, схлопнувшиеся на месте вырезанного: «Роснефть 🔥 — запустила».
_EMOJI_GAP_RE = re.compile(r"[ \t  ]{2,}")


def strip_emoji(text: str | None) -> str:
    """Убрать эмодзи из текста, который увидит читатель.

    ПРАВИЛО одно, чтобы результат был предсказуем:
      * символ, который ПО УМОЛЧАНИЮ рисуется как эмодзи, удаляется целиком
        (🔥 ✅ 👍 🚀 и вся телеграм-палитра);
      * служебные модификаторы (VS16/VS15, ZWJ, тон кожи, накладка-клавиша)
        удаляются всегда, а базовый символ ОСТАЁТСЯ обычным текстовым глифом:
        ⚠+VS16 → ⚠, ©+VS16 → ©, 1+накладка → 1.

    Так ничего осмысленного не теряется: © ® ™ ↔ и стрелки → ↓ переживают чистку
    (проверено генерацией: ↓ → ± ° ✓ вообще не входят в набор эмодзи), а
    эмодзи-начертания в тексте не остаётся.

    Если понадобится, чтобы исчезал и текстовый глиф (⚠, ☑), — правка на одну
    строку: добавить набор «эмодзи с текстовым начертанием» и резать
    последовательность целиком. Сейчас выбран менее разрушительный вариант.

    Пустой вход даёт пустую строку — вызывающему не нужно проверять None.
    """
    if not text:
        return ""
    cleaned = _EMOJI_RE.sub("", text)
    cleaned = _EMOJI_MODIFIER_RE.sub("", cleaned)
    cleaned = _EMOJI_GAP_RE.sub(" ", cleaned)
    return cleaned.strip(" \t  ")


_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I)
_XML_DECLARATION_RE = re.compile(r"^\s*<\?xml[^>]*\?>")


def decode_html(content: bytes | str) -> str:
    """Байты страницы → текст. Всегда сами, а не силами lxml.

    Загрузчик отдаёт сырые байты. lxml (libxml2 2.13) при разборе байтов НЕ учитывает
    HTML5-форму `<meta charset="utf-8">` — проверено на проде 18.09 на Сколтехе и
    Белоруснефти: кодировка объявлена на 481-м и 1387-м байте, а заголовок всё равно
    «Ð\x9dÐ¾Ð²Ð¾…». Из уже декодированной строки те же страницы разбираются верно.
    Итог на проде до правки: 58 статей с испорченными заголовком и текстом.

    Объявлена кодировка — декодируем ею (несколько битых байтов не повод сменить
    кодировку всей страницы — их заменяем). Не объявлена — строгий UTF-8, затем
    cp1251 (старые российские сайты), затем Latin-1.
    """
    if isinstance(content, str):
        return content
    raw = bytes(content)
    declared = _META_CHARSET_RE.search(raw[:16384])
    if declared:
        encoding = declared.group(1).decode("ascii", "ignore").lower()
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            return raw.decode(encoding, errors="replace")
        except LookupError:
            pass  # кодировка с опечаткой — определяем сами
    for encoding in ("utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def parse_html(content: bytes | str):
    """lxml-документ страницы с верной кодировкой (см. decode_html).

    Пустое тело при 200 (или одни пробелы/комментарий) lxml встречает ParserError
    «Document is empty» — это НЕ ValueError, и все места разбора, ловящие ValueError,
    пропускали его: одна пустая страница обрывала весь источник (у агентов 21.09 —
    весь прогон радара). Здесь он становится ValueError — один шов на всех вызывающих."""
    text = decode_html(content)
    try:
        try:
            return lxml_html.fromstring(text)
        except ValueError:
            # «Unicode strings with encoding declaration are not supported» — XML-декларация
            # в строке lxml не принимает; кодировку мы уже применили, декларация не нужна.
            return lxml_html.fromstring(_XML_DECLARATION_RE.sub("", text, count=1))
    except etree.ParserError as exc:
        raise ValueError(f"empty document: {exc}") from exc
