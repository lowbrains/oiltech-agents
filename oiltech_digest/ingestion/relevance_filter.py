"""Cheap deterministic pre-filter for obviously irrelevant RSS items.

The goal is not to replace AI relevance. It only blocks clear noise such as
sports, entertainment and generic incidents when there is no oil/gas, energy,
industrial or business-development signal in the RSS title/summary.
"""

from __future__ import annotations

from contextlib import contextmanager
import threading
from dataclasses import dataclass
import logging
import re
import time
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreFilterResult:
    keep: bool
    reason: str
    matched_keywords: tuple[str, ...] = ()
    matched_noise: tuple[str, ...] = ()


POSITIVE_KEYWORDS = (
    # RU oil & gas core
    "нефт", "газ", "газов", "газпром", "роснефт", "лукойл", "татнефт",
    "сургутнефт", "новатэк", "сибур", "транснефт", "нефтесервис",
    "нефтегаз", "тэк", "топливно-энергет", "энергетик",
    "месторожд", "скважин", "бурени", "буров", "грп", "гидроразрыв",
    "добыч", "разведк", "геологоразвед", "сейсмик", "пласт", "коллектор",
    "керн", "шельф", "upstream", "downstream", "midstream",
    "нефтепровод", "газопровод", "трубопровод", "спг", "сжиженн",
    "lng", "водород", "нефтехим", "петрохим", "переработк", "нпз",
    "завод", "терминал", "танкер", "проппант", "цементирован",
    "телеметр", "геонавигац", "каротаж", "заканчиван", "интенсификац",
    "пнд", "ппд", "пнд", "пнд", "капремонт", "криогенн",
    # RU industrial / adjacent signals we should keep for AI to judge
    "промышлен", "индустри", "производств", "оборудован", "компрессор",
    "насос", "турбин", "генерац", "электростанц", "аэс", "тэц", "гэс",
    "лэп", "подстанц", "сети", "россети", "автоматизац", "цифровизац",
    "искусственн", "робот", "датчик", "кибер", "импортозамещ",
    "логистик", "контракт", "подряд", "сделк", "m&a", "инвестиц",
    "санкц", "экспорт", "импорт", "судостро", "машиностро", "металлург",
    "горнодобы", "уголь", "минеральн", "критическ", "редкозем",
    # EN oil & gas core
    "oil", "gas", "petroleum", "petrochemical", "hydrocarbon", "energy",
    "oilfield", "drilling", "well", "wellbore", "reservoir", "completion",
    "stimulation", "fracturing", "fracking", "frac", "proppant", "cementing",
    "wireline", "logging", "seismic", "geoscience", "geothermal",
    "pipeline", "midstream", "refinery", "refining", "lng", "flng",
    "upstream", "offshore", "onshore", "subsea", "decommissioning",
    "production", "exploration", "operator", "rig", "epc", "opec",
    # EN industrial / adjacent
    "industrial", "manufacturing", "automation", "digital twin", "ai",
    "sensor", "robot", "cybersecurity", "power grid", "utility", "nuclear",
    "hydrogen", "carbon capture", "ccus", "renewable", "contract",
    "supply chain", "logistics", "sanction", "export", "import", "mining",
    "critical minerals", "rare earth", "steel", "turbine", "compressor",
)


NOISE_KEYWORDS = (
    # sports
    "футбол", "хоккей", "баскетбол", "волейбол", "гандбол", "теннис",
    "спорт", "спортсмен", "спортсменк", "матч", "суперлиг", "чемпионат",
    "кубок", "олимпи", "football", "soccer", "hockey", "basketball",
    "handball", "tennis", "match", "league", "cup", "olympic",
    # entertainment/culture/lifestyle
    "музей", "театр", "кино", "фильм", "актёр", "актер", "актрис",
    "певец", "певиц", "концерт", "фестивал", "выставк", "искусств",
    "ресторан", "туризм", "путешеств", "гороскоп", "museum", "theatre",
    "theater", "movie", "film", "actor", "actress", "concert", "festival",
    "restaurant", "travel", "horoscope",
    # generic crime/incidents/transport when no industrial signal exists
    "убийств", "пожар", "дтп", "авария", "происшеств", "суд арест",
    "аэропорт", "рейс", "самолет", "самолёт", "бпла", "дрон", "атака",
    "взрыв", "эвакуац", "crime", "murder", "airport", "flight", "plane",
    "drone", "attack", "explosion", "evacuation",
    # generic politics/public life
    "выбор", "депутат", "парламент", "партия", "митинг", "протест",
    "election", "parliament", "protest",
)


_WORD_RE = re.compile(r"\s+")


_TAG_KEYWORDS_CACHE: dict[str, object] = {"positive": (), "negative": (), "at": 0.0}
_TAG_KEYWORDS_TTL_SECONDS = 300
_MIN_TAG_KEYWORD_LEN = 4

# Ключи тематик, присланные ядром в задачу сбора. У внешнего воркера в NL базы нет:
# чтение справочника падало в except, и с 17.09 (fffaee0) все внешние источники шли
# через предфильтр БЕЗ тематик заказчика — тот же класс, что тематики гейта 17.09.
# Модульная переменная, а не ContextVar: RSS разбирается в пуле потоков, куда
# контекст не переходит.
_TAG_KEYWORDS_OVERRIDE: tuple[tuple[str, ...], tuple[str, ...]] | None = None
# С 21.09 полоса сбора держит несколько потоков: задача, закончившая первой, возвращала
# прежнее значение (None), пока соседняя ещё работала, — и та шла без ключей заказчика
# (ревью 21.09). Прежнее значение возвращается, только когда закончилась последняя
# задача; ключи во всех задачах одни и те же — их присылает ядро.
_TAG_KEYWORDS_LOCK = threading.Lock()
_TAG_KEYWORDS_ACTIVE = 0
_TAG_KEYWORDS_OUTER: tuple[tuple[str, ...], tuple[str, ...]] | None = None


@contextmanager
def use_tag_keywords(positive: Iterable[str], negative: Iterable[str]) -> Iterator[None]:
    global _TAG_KEYWORDS_OVERRIDE, _TAG_KEYWORDS_ACTIVE, _TAG_KEYWORDS_OUTER
    with _TAG_KEYWORDS_LOCK:
        if _TAG_KEYWORDS_ACTIVE == 0:
            _TAG_KEYWORDS_OUTER = _TAG_KEYWORDS_OVERRIDE
        _TAG_KEYWORDS_ACTIVE += 1
        _TAG_KEYWORDS_OVERRIDE = (tuple(positive), tuple(negative))
    try:
        yield
    finally:
        with _TAG_KEYWORDS_LOCK:
            _TAG_KEYWORDS_ACTIVE -= 1
            if _TAG_KEYWORDS_ACTIVE == 0:
                _TAG_KEYWORDS_OVERRIDE = _TAG_KEYWORDS_OUTER


def tag_keywords() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Ключевые и стоп-слова активных тематик заказчика.

    До 17.09 предфильтр работал на статическом словаре в коде, а тематики заказчика
    на отбор не влияли вообще: он правил их на экране и получал только другую
    раскладку по полкам. Здесь его словарь впервые участвует в решении «тащить или
    не тащить».

    Слова короче четырёх символов отбрасываем: в справочниках попадаются «ГРП», «AI»,
    «КРС», и на нормализованном тексте такие куски дают ложные совпадения чаще, чем
    пользу. Стоп-слова тематик добавляются к шумовым, но НЕ перебивают позитивное
    совпадение в самой статье — иначе одно неудачное слово выкосило бы поток.

    Импорт repository внутри функции: модуль фильтра тянут парсеры, которым база
    может быть не нужна вовсе. Сбой чтения не должен ронять сбор — работаем на
    статическом словаре, как раньше.
    """
    if _TAG_KEYWORDS_OVERRIDE is not None:
        return _TAG_KEYWORDS_OVERRIDE
    now = time.monotonic()
    if now - float(_TAG_KEYWORDS_CACHE.get("at") or 0) < _TAG_KEYWORDS_TTL_SECONDS:
        return _TAG_KEYWORDS_CACHE["positive"], _TAG_KEYWORDS_CACHE["negative"]  # type: ignore[return-value]
    positive: list[str] = []
    negative: list[str] = []
    try:
        from oiltech_digest.db import repository

        for tag in repository.list_enabled_tags():
            for field in ("keywords_json", "keywords_en_json"):
                for value in tag.get(field) or []:
                    word = _normalize(str(value))
                    if len(word) >= _MIN_TAG_KEYWORD_LEN:
                        positive.append(word)
            for value in tag.get("negative_keywords_json") or []:
                word = _normalize(str(value))
                if len(word) >= _MIN_TAG_KEYWORD_LEN:
                    negative.append(word)
    except Exception:  # noqa: BLE001 - тематики это дополнение, а не обязательный вход
        logger.warning("не удалось прочитать тематики для предфильтра")
    _TAG_KEYWORDS_CACHE["positive"] = tuple(dict.fromkeys(positive))
    _TAG_KEYWORDS_CACHE["negative"] = tuple(dict.fromkeys(negative))
    _TAG_KEYWORDS_CACHE["at"] = now
    return _TAG_KEYWORDS_CACHE["positive"], _TAG_KEYWORDS_CACHE["negative"]  # type: ignore[return-value]


def should_keep_article(title: str, summary: str = "", source: dict | None = None) -> PreFilterResult:
    article_text = _normalize(" ".join([title or "", summary or ""]))
    source_text = _normalize(" ".join([
        title or "",
        summary or "",
        (source or {}).get("name") or "",
        (source or {}).get("category") or "",
        (source or {}).get("source_type") or "",
    ]))
    tag_positive, tag_negative = tag_keywords()
    all_positive = POSITIVE_KEYWORDS + tag_positive
    article_positive = _matches(article_text, all_positive)
    positive = article_positive or _matches(source_text, all_positive)
    noise = _matches(article_text, NOISE_KEYWORDS + tag_negative)

    if noise and not article_positive:
        return PreFilterResult(False, "obvious non-domain noise without positive signal", (), noise)
    if positive:
        return PreFilterResult(True, "domain keyword matched", positive, noise)
    return PreFilterResult(True, "no strong negative signal")


def _matches(text: str, keywords: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(keyword for keyword in keywords if _keyword_in_text(text, keyword))


def _keyword_in_text(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    if keyword.isascii() and keyword.replace(" ", "").replace("&", "").isalnum():
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None
    return keyword in text


def _normalize(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    return _WORD_RE.sub(" ", text).strip()
