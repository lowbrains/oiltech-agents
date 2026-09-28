"""Oil and gas terminology guardrails for Russian summaries and titles."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any


@dataclass(frozen=True)
class GlossaryTerm:
    source_terms: tuple[str, ...]
    preferred_ru: str
    full_ru: str | None = None
    forbidden_ru: tuple[str, ...] = ()
    note: str = ""
    forbidden_patterns: tuple[str, ...] = ()
    # Только для аудита: находка — повод поправить, но без автозамены. Для калек, где
    # замена ломала бы грамматику («более granularными» — падеж, «спудил» — глагол):
    # их чинят phrase_repairs с точным окончанием, а это — сеть для непойманных форм.
    warn_patterns: tuple[str, ...] = ()
    # Аудит для многозначного слова: только если английский термин есть в статье и нет
    # ни одного слова из warn_unless_source. «Резервуар» в русском тексте почти всегда
    # ёмкость (замер 25.09: у 226 из 247 карточек в исходнике нет reservoir), калька
    # reservoir → «резервуар» — только в геологии, где статья не говорит о ёмкостях.
    source_warn_patterns: tuple[str, ...] = ()
    warn_unless_source: tuple[str, ...] = ()
    # Замены, которые верны только в статье об этом термине: «оффшорные счета» в новости
    # про санкции — не шельф, «завершение скважины» в русской заметке — не калька (ревью 27.09).
    phrase_repairs: tuple[tuple[str, str], ...] = ()


GLOSSARY_PATH = Path(os.environ.get("DOMAIN_GLOSSARY_PATH") or Path(__file__).with_name("domain_glossary.json"))
_GLOSSARY_DATA = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))


def _tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in (value or []) if str(item).strip())


def _pairs(value: Any) -> tuple[tuple[str, str], ...]:
    return tuple((str(item[0]), str(item[1])) for item in (value or []) if isinstance(item, list | tuple) and len(item) == 2)


def _load_glossary_terms(data: dict[str, Any]) -> tuple[GlossaryTerm, ...]:
    return tuple(
        GlossaryTerm(
            source_terms=_tuple(item.get("source_terms")),
            preferred_ru=str(item.get("preferred_ru") or "").strip(),
            full_ru=str(item["full_ru"]).strip() if item.get("full_ru") else None,
            forbidden_ru=_tuple(item.get("forbidden_ru")),
            note=str(item.get("note") or "").strip(),
            forbidden_patterns=_tuple(item.get("forbidden_patterns")),
            warn_patterns=_tuple(item.get("warn_patterns")),
            source_warn_patterns=_tuple(item.get("source_warn_patterns")),
            warn_unless_source=_tuple(item.get("warn_unless_source")),
            phrase_repairs=_pairs(item.get("phrase_repairs")),
        )
        for item in data.get("terms", [])
    )


GLOSSARY: tuple[GlossaryTerm, ...] = _load_glossary_terms(_GLOSSARY_DATA)
# Настоящие слова из двух алфавитов (бренды: «Farш», «Dostaевский»): их не чинит
# normalize_scripts и не ловит mixed_script_words, иначе модель переспрашивали бы зря.
MIXED_SCRIPT_ALLOW: frozenset[str] = frozenset(_tuple(_GLOSSARY_DATA.get("mixed_script_allow")))
# Однозначные кальки — чинятся в любой статье с термином словаря. Многозначные — в
# phrase_repairs своего термина.
PHRASE_REPAIRS: tuple[tuple[str, str], ...] = _pairs(_GLOSSARY_DATA.get("phrase_repairs"))


def relevant_glossary_terms(article: dict, *, limit: int | None = 12) -> list[GlossaryTerm]:
    """Термины статьи в порядке словаря. limit — для подсказки модели (длина промпта); замены и
    аудит берут все (limit=None): offshore в словаре 16-й, и в длинной статье выпадал бы."""
    text = _article_text(article)
    matches = [term for term in GLOSSARY if any(_contains_term(text, source) for source in term.source_terms)]
    return matches[:limit]


def glossary_prompt_block(article: dict, *, limit: int = 12) -> str:
    terms = relevant_glossary_terms(article, limit=limit)
    if not terms:
        return ""
    lines = [
        "domain_glossary:",
        "Используй эти нефтегазовые термины строго. Если термин встречается в тексте, применяй preferred_ru; forbidden_ru не используй.",
    ]
    for term in terms:
        aliases = ", ".join(term.source_terms[:5])
        full = f" | full_ru: {term.full_ru}" if term.full_ru else ""
        forbidden_items = list(term.forbidden_ru) + [pattern.replace(r"\b", "").replace("(?:", "(") for pattern in term.forbidden_patterns]
        forbidden = f" | forbidden_ru: {', '.join(forbidden_items[:5])}" if forbidden_items else ""
        note = f" | note: {term.note}" if term.note else ""
        lines.append(f"- source: {aliases} -> preferred_ru: {term.preferred_ru}{full}{forbidden}{note}")
    return "\n".join(lines)


def enforce_glossary_text(text: str, article: dict) -> str:
    """Apply safe deterministic replacements for known bad Russian terms."""
    result = normalize_scripts(text or "")
    terms = _terms_for(result, article)
    result = _repair_bad_phrases(result, terms)
    for term in terms:
        for forbidden in term.forbidden_ru:
            result = _replace_case_insensitive(result, forbidden, term.preferred_ru)
        for pattern in _forbidden_patterns(term):
            result = _sub_keep_capital(pattern, term.preferred_ru, result)
    result = _polish_repaired_phrases(result)
    result = _capitalize_sentence_starts(result)
    return result


def terminology_warnings(text: str, article: dict) -> list[dict[str, str]]:
    def warning(found: str, term: GlossaryTerm) -> dict[str, str]:
        return {"forbidden_ru": found, "preferred_ru": term.preferred_ru, "source_terms": ", ".join(term.source_terms[:5])}

    warnings = []
    lower = (text or "").lower()
    for term in _terms_for(text, article):
        warnings.extend(warning(forbidden, term) for forbidden in term.forbidden_ru if forbidden.lower() in lower)
        warnings.extend(
            warning(pattern, term)
            for pattern in (*_forbidden_patterns(term), *term.warn_patterns)
            if re.search(pattern, text or "", flags=re.I)
        )
    article_text = _article_text(article)
    for term in relevant_glossary_terms(article, limit=None):
        if any(_contains_term(article_text, word) for word in term.warn_unless_source):
            continue
        warnings.extend(
            warning(pattern, term) for pattern in term.source_warn_patterns if re.search(pattern, text or "", flags=re.I)
        )
    for word in mixed_script_words(text):
        # Ключи — те же, что у словарных находок: аудит печатает их одной строкой.
        warnings.append({
            "kind": "mixed_script",
            "forbidden_ru": word,
            "preferred_ru": "слово целиком одним алфавитом",
            "source_terms": "смешение латиницы и кириллицы",
        })
    return warnings


def _terms_for(text: str, article: dict) -> list[GlossaryTerm]:
    """Термины статьи плюс термины, чья калька есть в самом тексте.

    Калька из warn_patterns однозначна по построению («спудрил», «granularными»): её
    появление в русском тексте само говорит, какой это термин, даже если английского
    слова в контексте нет. Так и было у радара 22.09: в доказательстве сигнала — обзор
    Westwood без «spudded», а в его сути — «спудрил».
    """
    terms = relevant_glossary_terms(article, limit=None)
    for term in GLOSSARY:
        if term in terms or not term.warn_patterns:
            continue
        if any(re.search(pattern, text or "", flags=re.I) for pattern in term.warn_patterns):
            terms.append(term)
    return terms


_LETTER_RUN = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_LATIN = re.compile(r"[A-Za-z]")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")

# Буквы-двойники. Латиница внутри русского слова — буква, взятая по виду (исходники
# пишут «Cпрос», «ПAO»; замер 25.09: 9 из 12 замен p/y по виду дали известное слово —
# «выводy» → «выводу», «Министp» → «Министр»). Кириллица внутри латинского слова бывает
# и ошибкой по звуку: в «Орinoco» «р» — это «r», замена по виду дала бы «Opinoco». Поэтому
# обратно — только буквы, у которых совпадают и вид, и звук.
_LAT_TO_CYR = str.maketrans("aoecxpyAOECXPHBKMTk", "аоесхруАОЕСХРНВКМТк")
_CYR_TO_LAT = str.maketrans("аоесхАОЕСХКМТ", "aoecxAOECXKMT")
_TWINS_LAT = frozenset("aoecxpyAOECXPHBKMTk")
_TWINS_CYR = frozenset("аоесхАОЕСХКМТ")
# Заглавные латинские подряд, а за ними русская строчная, — аббревиатура с русским
# окончанием или словом: «BOPы», «EPCконтракт», «OPECстраны». По виду они стали бы «ВОРы»,
# «ЕРСконтракт» (ревью 27.09) — такое слово не трогаем, оно уходит на повтор. В слове
# капсом («ПAO», «СЕКТOP», «HOВАТЭК») латиница — двойники.
_LATIN_ABBREVIATION = re.compile(r"[A-Z]{2,}(?=[а-яё])")
# Стык алфавитов внутри слова — склейка двух слов: «присутствиеHoneywell», «вPermian»,
# «СШАChina», «FTШвейцар». Внутри одного алфавита так не режем: «КазМунайГаз»,
# «МосБиржа», «кВт» — настоящие слова (замер 25.09). Кириллица слева — и одной буквой,
# это предлог («сExxonMobil»). Одна латинская буква слева — слово, только если справа
# длинное слово («B2BПлатформа», «Plan BКомпания»); перед коротким это двойник в
# единице измерения («MВт», «MПа», «kВт»).
_SCRIPT_GLUE = re.compile(
    r"(?<=[а-яё])(?=[A-Z])|(?<=[A-Za-z][a-z])(?=[А-ЯЁ])"
    r"|(?<=[А-ЯЁ])(?=[A-Z][a-z])|(?<=[A-Za-z][A-Z])(?=[А-ЯЁ][а-яё])"
    r"|(?<![A-Za-z][A-Z])(?<=[A-Z])(?=[А-ЯЁ][а-яё]{3})"
)
_LINK_GLUE = re.compile(r"(?<=[А-Яа-яЁё])(?=https?://)")


def _allow_stem(word: str) -> str:
    """Бренд без окончания — по нему он узнаётся и в другом падеже («PROНЕФТИ»,
    «Dostaевского»). Не короче стыка алфавитов и двух букв после него («Farш» — целиком):
    по «Dос» узнавались бы и «Dосрочно», «Dоставка»."""
    for index in range(1, len(word)):
        if bool(_LATIN.match(word[index - 1])) != bool(_LATIN.match(word[index])):
            return word[: max(len(word) - 2, index + 2)]
    return word


_ALLOW_STEMS: tuple[str, ...] = tuple(sorted({_allow_stem(word) for word in MIXED_SCRIPT_ALLOW}))


def _allowed_mixed(word: str) -> bool:
    return word.startswith(_ALLOW_STEMS)


def _single_twins(word: str) -> str:
    """Буква-двойник, у которой с обеих сторон буквы другого алфавита, — опечатка, а не
    стык двух слов: «КазMунайГаз», «ExxonМobil», «ИнтерPАО», «кBт». Её чиним до склейки,
    иначе стык алфавитов разрезал бы слово: «Каз МунайГаз» (ревью 27.09).

    Не опечатка — однобуквенное слово между двумя словами: строчная между заглавными
    («BPсShell») и заглавная перед словом с заглавной («планBКомпания»)."""
    chars = list(word)
    for index in range(1, len(word) - 1):
        char, before, after = word[index], word[index - 1], word[index + 1]
        if char.islower() and before.isupper() and after.isupper():
            continue
        if char.isupper() and after.isupper() and word[index + 2 : index + 3].islower():
            continue
        if char in _TWINS_LAT and _CYRILLIC.match(before) and _CYRILLIC.match(after):
            chars[index] = char.translate(_LAT_TO_CYR)
        elif char in _TWINS_CYR and _LATIN.match(before) and _LATIN.match(after):
            chars[index] = char.translate(_CYR_TO_LAT)
    return "".join(chars)


def normalize_scripts(text: str) -> str:
    """Слово — одним алфавитом, склеенные слова разных алфавитов — через пробел.

    Порядок: одиночный двойник внутри слова («КазMунайГаз»), затем склейка, затем
    двойники по частям. Склейка раньше двойников на краю слова: иначе «сExxonMobil» стало
    бы «cExxonMobil» — стык пропал бы, а с ним и находка аудита. Слово переводится в тот
    алфавит, куда можно заменить ВСЕ чужие буквы двойниками: «вхoдит» → «входит», «1-гo»
    → «1-го», «МoU» → «MoU». Можно в обе стороны — решает большинство, поровну — не
    трогаем. Полуперевод («управляego», «наshore») и аббревиатура с русским окончанием
    («BOPы») так не лечатся — их ловит mixed_script_words.
    """
    if not text:
        return text or ""

    def fix_run(match: re.Match) -> str:
        run = match.group(0)
        if _allowed_mixed(run):
            return run
        return " ".join(one_script(word) for word in _SCRIPT_GLUE.split(_single_twins(run)))

    def one_script(word: str) -> str:
        latin = [ch for ch in word if _LATIN.match(ch)]
        cyrillic = [ch for ch in word if _CYRILLIC.match(ch)]
        if not latin or not cyrillic or _LATIN_ABBREVIATION.search(word):
            return word
        to_cyrillic = all(ch in _TWINS_LAT for ch in latin)
        to_latin = all(ch in _TWINS_CYR for ch in cyrillic)
        if to_cyrillic and to_latin:
            if len(latin) == len(cyrillic):
                return word
            to_cyrillic = len(cyrillic) > len(latin)
            to_latin = not to_cyrillic
        if to_cyrillic:
            return word.translate(_LAT_TO_CYR)
        if to_latin:
            return word.translate(_CYR_TO_LAT)
        return word

    return _LETTER_RUN.sub(fix_run, _LINK_GLUE.sub(" ", text))


def mixed_script_words(text: str) -> list[str]:
    """Слова, где латиница и кириллица смешаны без дефиса: «granularными», «Тупinамba».

    Такое слово — всегда брак перевода (латинский корень с русским окончанием, ошибка
    транслитерации, латинская «c» внутри русского слова), и словарь его не поймает:
    термина под каждое английское слово в нём нет. «LNG-проект» и «CO2» не задевает:
    дефис и цифра делят слово на части.
    """
    found: list[str] = []
    for match in _LETTER_RUN.finditer(text or ""):
        word = match.group(0)
        if _allowed_mixed(word) or word in found:
            continue
        if _LATIN.search(word) and _CYRILLIC.search(word):
            found.append(word)
    return found


def validate_glossary() -> list[str]:
    errors = []
    seen_source_terms: set[str] = set()
    seen_forbidden_terms: set[str] = set()
    if not GLOSSARY:
        errors.append("terms: пустой словарь")
    if not PHRASE_REPAIRS:
        errors.append("phrase_repairs: пустой список фразовых исправлений")
    for index, term in enumerate(GLOSSARY, start=1):
        prefix = f"terms[{index}]"
        if not term.source_terms:
            errors.append(f"{prefix}: нет source_terms")
        if not term.preferred_ru:
            errors.append(f"{prefix}: нет preferred_ru")
        for source in term.source_terms:
            key = source.lower()
            if key in seen_source_terms:
                errors.append(f"{prefix}: дубль source_terms '{source}'")
            seen_source_terms.add(key)
        for forbidden in term.forbidden_ru:
            key = forbidden.lower()
            if key in seen_forbidden_terms:
                errors.append(f"{prefix}: дубль forbidden_ru '{forbidden}'")
            seen_forbidden_terms.add(key)
        for pattern in term.forbidden_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"{prefix}: плохая forbidden_pattern '{pattern}': {exc}")
        for pattern in (*term.warn_patterns, *term.source_warn_patterns):
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"{prefix}: плохая warn_pattern '{pattern}': {exc}")
        for pattern, _replacement in term.phrase_repairs:
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"{prefix}: плохая phrase_repair '{pattern}': {exc}")
    for index, (pattern, _replacement) in enumerate(PHRASE_REPAIRS, start=1):
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(f"phrase_repairs[{index}]: плохая regex '{pattern}': {exc}")
    for index, case in enumerate(glossary_golden_cases(), start=1):
        for field in ("name", "article", "bad", "must_have", "must_not"):
            if field not in case:
                errors.append(f"golden_cases[{index}]: нет поля {field}")
    return errors


def terminology_eval_cases(limit: int = 100) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for case in glossary_golden_cases():
        cases.append({**case, "source": "golden"})

    templates = (
        "Материал использует термин {bad} в описании технологии.",
        "В заголовке остался плохой перевод: {bad}.",
        "Для дайджеста нужно заменить {bad} на отраслевой термин.",
        "AI-суть содержит некорректную формулировку {bad}.",
    )
    for term in GLOSSARY:
        if not term.forbidden_ru:
            continue
        context = {
            "title": f"{term.source_terms[0]} technology update",
            "raw_text": f"The article discusses {term.source_terms[0]} in oil and gas operations.",
            "language": "en",
        }
        for forbidden in term.forbidden_ru:
            for template in templates:
                cases.append(
                    {
                        "name": f"{term.source_terms[0]} / {forbidden}",
                        "article": context,
                        "bad": template.format(bad=forbidden),
                        "must_have": [term.preferred_ru],
                        "must_not": [forbidden],
                        "source": "generated",
                    }
                )
                if len(cases) >= limit:
                    return cases
    return cases[:limit]


def run_terminology_eval(limit: int = 100) -> dict[str, object]:
    rows = []
    for index, case in enumerate(terminology_eval_cases(limit=limit), start=1):
        article = case["article"]
        before = str(case["bad"])
        after = enforce_glossary_text(before, article)
        after_lower = after.lower()
        missing = [term for term in case["must_have"] if str(term).lower() not in after_lower]
        forbidden = [term for term in case["must_not"] if str(term).lower() in after_lower]
        warnings = terminology_warnings(after, article)
        ok = not missing and not forbidden and not warnings
        rows.append(
            {
                "number": index,
                "source": case.get("source") or "",
                "case": case.get("name") or "",
                "original_en": (article.get("raw_text") or article.get("title") or "") if isinstance(article, dict) else "",
                "before": before,
                "after": after,
                "must_have": ", ".join(str(item) for item in case["must_have"]),
                "must_not": ", ".join(str(item) for item in case["must_not"]),
                "status": "ok" if ok else "fail",
                "issues": "; ".join(
                    [*(f"missing={item}" for item in missing), *(f"forbidden={item}" for item in forbidden)]
                    + [f"warning={item['forbidden_ru']}" for item in warnings]
                ),
            }
        )
    passed = sum(1 for row in rows if row["status"] == "ok")
    return {
        "total": len(rows),
        "passed": passed,
        "failed": len(rows) - passed,
        "rows": rows,
    }


def glossary_golden_cases() -> list[dict[str, object]]:
    """Regression set for the terminology layer."""
    return list(_GLOSSARY_DATA.get("golden_cases", []))


def _article_text(article: dict) -> str:
    return " ".join(
        str(article.get(field) or "")
        for field in ("title", "title_ru", "summary", "raw_text", "source_category")
    ).lower()


def _forbidden_patterns(term: GlossaryTerm) -> tuple[str, ...]:
    return term.forbidden_patterns if isinstance(term.forbidden_patterns, tuple) else ()


def _repair_bad_phrases(text: str, terms: list[GlossaryTerm]) -> str:
    if not terms:
        return text
    result = text
    for pattern, replacement in (*(pair for term in terms for pair in term.phrase_repairs), *PHRASE_REPAIRS):
        result = _sub_keep_capital(pattern, replacement, result)
    return result


def _polish_repaired_phrases(text: str) -> str:
    # «изучил[аи]? … → изучила» отсюда убрано (ревью 25.09): замена меняла число и род
    # глагола («изучили» → «изучила»), а смысла не чинила.
    result = _sub_keep_capital(r"\bпровел\b", "провёл", text)
    # «о стимуляции» → «о интенсификации»: перед гласной предлог — «об».
    return _sub_keep_capital(r"\b(о)\s+(?=интенсификаци)", r"\1б ", result)


def _sub_keep_capital(pattern: str, replacement: str, text: str) -> str:
    """re.sub без учёта регистра, но с заглавной, если найденное с неё начиналось:
    «Провел» → «Провёл», а не «провёл»; «Оффшорная» → «Шельфовая»."""

    def expand(match: re.Match) -> str:
        result = match.expand(replacement)
        if match.group(0)[:1].isupper() and result[:1].islower():
            return result[:1].upper() + result[1:]
        return result

    return re.sub(pattern, expand, text, flags=re.I)


def _capitalize_sentence_starts(text: str) -> str:
    if not text or not re.match(r"[а-яё]", text[0], flags=re.I):
        return text
    return text[0].upper() + text[1:]


def _contains_term(text: str, term: str) -> bool:
    term = (term or "").strip().lower()
    if not term:
        return False
    if len(term) <= 4:
        return re.search(rf"(?<![a-zа-я0-9]){re.escape(term)}(?![a-zа-я0-9])", text, flags=re.I) is not None
    if re.search(r"\s", term):
        return term in text
    return re.search(rf"\b{re.escape(term)}\b", text, flags=re.I) is not None


def _replace_case_insensitive(text: str, old: str, new: str) -> str:
    return _sub_keep_capital(rf"(?<![А-Яа-яA-Za-z0-9]){re.escape(old)}(?![А-Яа-яA-Za-z0-9])", new.replace("\\", "\\\\"), text)
