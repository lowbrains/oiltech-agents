"""Technology signal radar.

This layer searches for transferable business/HSE signals, not just relevant
articles. It can run fully offline on already collected articles; web evidence
can be fed later through the same evidence shape.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
import hashlib
import json
import re
import time
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from oiltech_digest import config as app_config
from oiltech_digest import signal_dedup
from oiltech_digest.db import repository
from oiltech_digest.processing.domain_glossary import enforce_glossary_text, glossary_prompt_block
from oiltech_digest.processing.openai_client import AIResponse
from oiltech_digest.processing.pipeline import make_client
from oiltech_digest.signal_feedback import (
    apply_feedback_glossary,
    feedback_prompt_block,
    feedback_query_hints,
    memory_snapshot_rows,
    topic_term_stems,
    use_memory_snapshot,
)


INDUSTRY_CONTEXT_RE = re.compile(
    r"\b("
    r"oil|gas|o&g|lng|refiner|refinery|petrochemical|chemical|drilling|wellsite|wellbore|"
    r"oilfield|pipeline|midstream|upstream|offshore|subsea|frac|fracking|methane|hydrocarbon|"
    r"mining|mine|hazardous area|process safety|industrial safety|seismic|geoscience|geology|"
    r"geophysical|petrophysics|wireline|logging|reservoir|cementing|zonal isolation|completion|"
    r"well intervention|coiled tubing|artificial lift|eor|enhanced oil recovery|produced water|"
    r"ccus|carbon capture|microgrid|power generation|laboratory|core analysis|engineering|epc|"
    r"oilfield services|merger|acquisition|contract"
    r")\b|"
    r"(нефт|газ|бурен|скважин|трубопровод|промышленн|опасн|месторожд|добыч|переработк|"
    r"сейсм|геолог|геофиз|петрофиз|каротаж|цементир|изоляц|заканчив|грп|крс|т крс|"
    r"интенсификац|нефтеотдач|химизац|энергоснабж|лаборатор|испытан|инжиниринг|контракт)|"
    r"(油气|石油|天然气|油田|油库|钻井|石化|化工|矿山|管道|炼化|海上平台|防爆|危化|采气|采油|"
    r"地震勘探|物探|测井|录井|岩心|固井|完井|压裂|修井|连续油管|举升|提高采收率|驱油|"
    r"采出水|碳捕集|微电网|实验室|工程设计|油服|合同|并购)",
    re.IGNORECASE,
)


EVENT_SIGNAL_RE = re.compile(
    r"\b("
    r"deploy|deployed|deployment|implement|implemented|field trial|pilot|rollout|"
    r"launch|launched|release|released|commercializ|awarded|award|selected|selects|"
    r"chose|chosen|adopt|adopted|partnership|contract|acquisition|acquire[sd]?|supplier|"
    r"kpi|performance|throughput|uptime|reduc|increas|cut downtime|saved|savings"
    r")\b|"
    r"(внедр|пилот|запуст|заключ[ил]|выбрал|подписал|поставщик|контракт|партнерств|"
    r"снизил|повысил|ускорил|сократил|результат|показател)|"
    r"(现场应用|中标|合作|签署|采购|推出|发布|试点|降低|提高|节省)",
    re.IGNORECASE,
)


DEFAULT_RADAR_TOPICS = [
    {
        "name": "HSE robotics / Physical AI",
        "description": "Роботы и physical AI, которые убирают людей из опасных зон и операций.",
        "industry_scope": ["oil and gas", "mining", "metals", "chemicals", "industrial logistics"],
        "query_seeds": [
            "autonomous inspection robot hazardous area",
            "physical AI industrial safety",
            "robotic drilling removes workers from drill floor",
            "remote inspection robot environmental monitoring industrial site",
            "具身智能 油气 巡检机器人",
            "防爆巡检机器人 石油 天然气",
            "油气站场 无人巡检 机器人",
        ],
    },
    {
        "name": "Predictive HSE",
        "description": "Переход от реактивной безопасности к прогнозированию опасного состояния.",
        "industry_scope": ["oil and gas", "mining", "chemicals", "energy"],
        "query_seeds": [
            "predictive safety industrial operations",
            "preventive safety hazardous condition detection",
            "AI predicts safety incidents industrial equipment",
            "predictive maintenance HSE risk reduction",
            "预测性安全 矿山 油气",
            "安全生产 双重预防机制 人工智能",
            "设备状态监测 预测性维护 油田",
        ],
    },
    {
        "name": "Digital PTW / Control of Work",
        "description": "Dynamic PTW, LOTO, SIMOPS и continuous control assurance.",
        "industry_scope": ["oil and gas", "chemicals", "mining", "heavy industry"],
        "query_seeds": [
            "dynamic permit to work continuous control assurance",
            "digital PTW isolation certificate shift handover",
            "LOTO SIMOPS control of work software industrial safety",
            "permit to work system contractor competence integration",
            "电子作业票 特殊作业 安全生产",
            "作业许可 盲板 抽堵 动火 受限空间 数字化",
            "承包商 能力 作业许可 隔离 交接班",
        ],
    },
    {
        "name": "Industrial transport safety",
        "description": "Телематика, fatigue/distraction, collision avoidance и контроль опасных зон транспорта.",
        "industry_scope": ["oilfield service", "mining", "industrial logistics", "construction"],
        "query_seeds": [
            "heavy equipment collision avoidance mining safety",
            "fatigue detection industrial fleet safety",
            "AI video telematics hazardous industrial transport",
            "worker vehicle proximity detection industrial site",
            "矿卡 防碰撞 系统 安全",
            "疲劳驾驶监测 矿山 车辆",
            "人员车辆 防碰撞 露天矿",
        ],
    },
]

BUSINESS_RADAR_TOPICS = [
    {
        "name": "Сейсморазведка, ГРР и геолого-геофизические услуги",
        "description": "Поиск сигналов в разведке, сейсмике, интерпретации и геологическом сопровождении решений.",
        "industry_scope": ["exploration", "geoscience", "seismic", "oil and gas"],
        "query_seeds": [
            "AI seismic interpretation oil gas exploration",
            "seismic acquisition automation nodal seismic oil gas",
            "full waveform inversion cloud seismic interpretation",
            "fiber optic DAS seismic monitoring oilfield",
            "地震勘探 人工智能 油气 勘探",
            "物探 数字化 油气 勘探",
        ],
    },
    {
        "name": "ГИС, промысловая геофизика и петрофизика",
        "description": "Сигналы в каротаже, петрофизике, данных по пласту и диагностике качества скважин.",
        "industry_scope": ["wireline", "logging", "petrophysics", "reservoir"],
        "query_seeds": [
            "AI petrophysics automated log interpretation",
            "wireline formation evaluation machine learning",
            "borehole imaging AI reservoir characterization",
            "fiber optic well diagnostics production logging",
            "智能测井 解释 人工智能 油田",
            "岩心分析 数字化 测井 油气",
        ],
    },
    {
        "name": "Бурение, направленное бурение, растворы и буровое оборудование",
        "description": "Автоматизация строительства скважин, траекторное сопровождение, растворы, инструменты и оборудование.",
        "industry_scope": ["drilling", "directional drilling", "drilling fluids", "rig equipment"],
        "query_seeds": [
            "automated drilling rig closed loop drilling",
            "AI geosteering directional drilling deployment",
            "drilling fluids real time monitoring automation",
            "red zone automation drill floor pipe handling",
            "智能钻井 自动化钻机 油气",
            "钻井液 在线监测 自动化",
        ],
    },
    {
        "name": "Цементирование и изоляционные работы",
        "description": "Сигналы в креплении, зональной изоляции, герметичности и ликвидации перетоков.",
        "industry_scope": ["cementing", "zonal isolation", "well integrity"],
        "query_seeds": [
            "real time cementing automation well integrity",
            "self healing cement oil gas wells",
            "CO2 resistant cement carbon storage wells",
            "zonal isolation monitoring fiber optic cementing",
            "固井 自动化 井完整性 油气",
            "封隔 堵漏 水泥环 完整性",
        ],
    },
    {
        "name": "Заканчивание скважин",
        "description": "Компоновки заканчивания, внутрискважинное оборудование, intelligent completions и контроль притока.",
        "industry_scope": ["completions", "sand control", "downhole equipment"],
        "query_seeds": [
            "intelligent completion downhole control oil gas",
            "autonomous inflow control device deployment",
            "sand control completion technology field trial",
            "multistage completion monitoring fiber optic",
            "智能完井 井下控制 油田",
            "防砂 完井 工具 油气",
        ],
    },
    {
        "name": "ГРП, МГРП и стимуляция",
        "description": "Сигналы в дизайне и выполнении ГРП, оборудовании, химии, проппанте и мониторинге.",
        "industry_scope": ["hydraulic fracturing", "stimulation", "proppant"],
        "query_seeds": [
            "closed loop fracturing autonomous frac",
            "electric frac fleet field deployment",
            "fracturing fiber optic diagnostics real time",
            "proppant logistics automation oilfield",
            "智能压裂 自动化 压裂 油田",
            "电驱压裂 连续压裂 油气",
        ],
    },
    {
        "name": "КРС, ТКРС и well intervention",
        "description": "Ремонт, восстановление и вмешательства в скважину, включая rigless и coiled tubing.",
        "industry_scope": ["well intervention", "workover", "coiled tubing", "slickline"],
        "query_seeds": [
            "rigless well intervention automation",
            "coiled tubing real time downhole telemetry",
            "well intervention robotics oil gas",
            "live well intervention digital operations",
            "修井 自动化 连续油管 油田",
            "井下机器人 修井 油气",
        ],
    },
    {
        "name": "Добыча, механизированная добыча и внутрискважинное оборудование",
        "description": "Поддержание добычи, механизированная добыча, мониторинг оборудования и оптимизация фонда.",
        "industry_scope": ["production", "artificial lift", "downhole equipment"],
        "query_seeds": [
            "artificial lift optimization AI oilfield",
            "ESP predictive maintenance oil gas",
            "autonomous production optimization well pad",
            "production chemicals digital dosing automation",
            "智能采油 机械采油 优化",
            "电潜泵 预测性维护 油田",
        ],
    },
    {
        "name": "Повышение нефтеотдачи и химизация добычи",
        "description": "Методы повышения нефтеотдачи, химические сервисы, подготовка и защита оборудования.",
        "industry_scope": ["enhanced oil recovery", "production chemistry", "waterflood"],
        "query_seeds": [
            "enhanced oil recovery nanotechnology field trial",
            "polymer flooding digital optimization",
            "production chemistry AI corrosion scale inhibitor",
            "chemical EOR monitoring reservoir surveillance",
            "提高采收率 聚合物驱 油田",
            "油田化学剂 腐蚀 结垢 智能加药",
        ],
    },
    {
        "name": "Промысловая инфраструктура, surface facilities и проекты обустройства",
        "description": "Поверхностная инфраструктура, сбор, подготовка, измерение, управление потоками и безлюдные объекты.",
        "industry_scope": ["surface facilities", "midstream", "field infrastructure"],
        "query_seeds": [
            "unmanned oilfield facility remote operations",
            "surface facilities digital twin oil gas",
            "gas leak detection autonomous plant inspection",
            "edge AI oilfield facility monitoring",
            "无人站场 油气 智能巡检",
            "油气站场 泄漏检测 远程运维",
        ],
    },
    {
        "name": "Энергетика и промысловые энергосистемы",
        "description": "Энергоснабжение буровых, ГРП, кустов, удаленных объектов и промысловой инфраструктуры.",
        "industry_scope": ["oilfield power", "microgrid", "electrification", "energy systems"],
        "query_seeds": [
            "oilfield microgrid battery storage drilling rig",
            "electric frac power generation gas turbine",
            "rig electrification hybrid power oilfield",
            "remote oilfield power management microgrid",
            "油田 微电网 储能 供电",
            "电驱压裂 供电 油气",
        ],
    },
    {
        "name": "Роботизация и автономные системы",
        "description": "Физические роботы и автономные системы для бурения, инспекции, мониторинга и опасных операций.",
        "industry_scope": ["robotics", "autonomous systems", "industrial operations"],
        "query_seeds": [
            "autonomous inspection robot oil gas hazardous area",
            "explosion proof quadruped robot refinery oil depot",
            "autonomous drilling robot red zone removal",
            "subsea autonomous drone inspection oil gas",
            "防爆巡检机器人 油气 石化",
            "具身智能 油田 巡检",
        ],
    },
    {
        "name": "Логистика, транспорт и supply chain нефтесервисных операций",
        "description": "Материалы, техника, транспорт, вода, проппант, химия и supply chain для нефтесервисных операций.",
        "industry_scope": ["oilfield logistics", "industrial transport", "supply chain"],
        "query_seeds": [
            "driverless proppant logistics oilfield",
            "oilfield fleet safety fatigue monitoring",
            "industrial vehicle collision avoidance EMESRT Level 9",
            "oilfield water logistics optimization automation",
            "油服 物流 自动驾驶 运输",
            "矿卡 防碰撞 车辆干预",
        ],
    },
    {
        "name": "Экология, промышленная безопасность, HSE и устойчивое развитие",
        "description": "Безопасность, экология, отходы, выбросы, мониторинг и снижение промышленных рисков.",
        "industry_scope": ["HSE", "environment", "process safety", "sustainability"],
        "query_seeds": [
            "predictive HSE oil gas AI safety",
            "digital permit to work oil gas LOTO SIMOPS",
            "methane detection drone satellite oil gas",
            "produced water treatment reuse oilfield",
            "电子作业票 作业许可 石化 安全",
            "甲烷 泄漏检测 无人机 油气",
        ],
    },
    {
        "name": "Лабораторные, испытательные и R&D-сервисы",
        "description": "Испытания, квалификация технологий, подбор решений, опытно-промышленные работы и лабораторная автоматизация.",
        "industry_scope": ["laboratory", "testing", "R&D", "qualification"],
        "query_seeds": [
            "oilfield laboratory automation core analysis AI",
            "technology qualification oil gas field trial",
            "robotic laboratory petroleum testing",
            "materials testing CCUS hydrogen wells",
            "油气 实验室 自动化 岩心分析",
            "技术评价 现场试验 油服",
        ],
    },
    {
        "name": "Инжиниринг, проектирование, управление проектами и консалтинг",
        "description": "Проектные, технические, экономические и управленческие сервисы для нефтегазовых проектов.",
        "industry_scope": ["engineering", "EPC", "project management", "consulting"],
        "query_seeds": [
            "AI engineering design oil gas EPC",
            "digital project delivery oil gas engineering",
            "modular oilfield facilities engineering automation",
            "carbon capture project engineering oil gas",
            "油气 工程设计 数字化 人工智能",
            "石化 EPC 项目管理 数字化",
        ],
    },
    {
        "name": "Рынок, экономика, бизнес-модели, контракты и M&A",
        "description": "Рынок нефтесервиса, ставки, загрузка мощностей, сделки, контрактные модели и стратегические сигналы.",
        "industry_scope": ["oilfield services market", "contracts", "M&A", "business models"],
        "query_seeds": [
            "oilfield services contract automation technology deployment",
            "oilfield services M&A technology acquisition",
            "performance based contract oilfield services",
            "strategic partnership drilling automation oil gas",
            "油服 合同 战略合作 技术",
            "油服 并购 自动化 技术",
        ],
    },
]


DEFAULT_RADAR_TOPICS = [*DEFAULT_RADAR_TOPICS, *BUSINESS_RADAR_TOPICS]


SIGNAL_JUDGE_INSTRUCTIONS = """Ты аналитик технологических сигналов для нефтесервиса.
Оцени пачку evidence не как отдельные новости, а как потенциальный сигнал для радара.

Хороший сигнал:
- переносим в нефтесервис/HSE/бурение/промышленную эксплуатацию;
- описывает новый технологический принцип, промышленное масштабирование или измеримый эффект;
- имеет факты: компания, внедрение, поставщик, объект, цифры, зрелость или внятный why now;
- не является обычным маркетинговым анонсом без признаков применения.

Правила заказчика (выведены из его разбора 38 сигналов, 15–16.09.2026) — reject, если:
- нет конкретного события: нового продукта, внедрения, заказчика, контракта, KPI или
  технологического milestone. Обзор трендов, explainer, evergreen-страница услуг и каталог
  поставщика — это не сигнал;
- событие старое: проверяй дату САМОГО события внутри материала, а не дату находки.
  Старше 12 месяцев или подборка кейсов прошлых лет — исторический контекст, reject;
- отрасль чужая (розничная и городская доставка, оборона, наука без промышленного
  применения) и перенос в нефтегаз/промышленную эксплуатацию не показан;
- это повтор уже известного решения (тот же продукт или поставщик) без нового события;
- технологический смысл пришлось бы додумать: если evidence о другом (рейтинг
  подрядчиков, итоги года), не превращай его в технологию.
Проект самой «Газпром нефти» — не внешний сигнал: начни summary со слов
«Внутренний benchmark», score не выше 50. Цифры эффекта из материалов поставщика
помечай в why_not_noise как vendor-reported; без независимого подтверждения — не выше watch.

Верни один сигнал или reject. Не добавляй фактов, которых нет во входе.
score возвращай по шкале 0-100, где 40 = слабый watch, 70 = хороший shortlist,
85+ = proven.

Все пользовательские текстовые поля возвращай на русском: title, theme, summary,
thesis, transferability, why_now, why_not_noise. Не копируй англоязычный или китайский
заголовок как title; переведи его нормальным нефтегазовым русским языком.
Названия компаний, продуктов, месторождений, стандартов и устоявшиеся аббревиатуры
HSE/PTW/AI оставляй в оригинальном написании."""

SIGNAL_JUDGE_SCHEMA = {
    "name": "technology_signal_judgement",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "title",
            "title_ru",
            "theme",
            "summary",
            "thesis",
            "transferability",
            "maturity",
            "confidence",
            "score",
            "why_now",
            "why_not_noise",
            "companies",
            "industries",
        ],
        "properties": {
            "title": {"type": "string"},
            "title_ru": {"type": "string"},
            "theme": {"type": "string"},
            "summary": {"type": "string"},
            "thesis": {"type": "string"},
            "transferability": {"type": "string"},
            "maturity": {"type": "string", "enum": ["reject", "watch", "shortlist", "proven"]},
            "confidence": {"type": "number"},
            "score": {"type": "number"},
            "why_now": {"type": "string"},
            "why_not_noise": {"type": "string"},
            "companies": {"type": "array", "items": {"type": "string"}},
            "industries": {"type": "array", "items": {"type": "string"}},
        },
    },
}


BATCH_REVIEW_INSTRUCTIONS = """Ты — финальный контроль качества radar'а технологических сигналов.

Тебе дают пачку кандидатов в сигналы по одной теме. Каждый кандидат уже прошёл
отдельную оценку судьи, но судья видел только свой кластер evidence и не видел
остальных кандидатов пачки. Твоя задача — посмотреть на всю пачку разом и решить,
какие кандидаты действительно разные и полезные сигналы, а какие надо убрать.

Убирай кандидата (keep=false), если:
- он описывает то же самое событие, что другой кандидат в этой же пачке (тот же
  продукт/контракт/внедрение у той же компании, просто другими словами) — оставь
  только более сильный из пары (выше score или увереннее факты), а у убранного в
  duplicate_of_signal_key укажи signal_key того, который остаётся;
- на фоне всей пачки видно, что это не отдельное событие, а общий обзор рынка,
  трюизм или пересказ уже известного тренда без нового факта;
- в пачке несколько кандидатов от одного вендора с одинаковой рекламной подачей —
  оставь только самый содержательный, остальные убери.

Не выдумывай факты, которых нет во входе. Не убирай кандидата только из-за похожей
темы — разные компании или разные технологии внутри одной темы это нормально и
должны остаться. Если пачка уже вся про разные события — верни keep=true для всех.

Для каждого кандидата с keep=true дополнительно оцени interest_score от 0 до 100 —
насколько он интересен и значим ИМЕННО НА ФОНЕ ОСТАЛЬНЫХ кандидатов этой пачки, а не
сам по себе. Это не то же самое, что score судьи: судья оценивал кластер в изоляции,
а тут нужно сравнение внутри пачки. Выше балл — если сигнал: про нового игрока или
неожиданное сочетание технологии и отрасли, даёт измеримый эффект (KPI, % ускорения,
снижение простоя), показывает первое промышленное применение, а не повтор известного
подхода. Ниже балл — если это ожидаемый, рутинный шаг крупного игрока, о котором
рынок и так знает, или очередной анонс без нового факта на фоне уже более сильных
кандидатов пачки. why_interesting — коротко по-русски, почему такой балл именно на
фоне остальных. Для keep=false interest_score и why_interesting можно оставить пустыми.

duplicate_of_signal_key заполняй ТОЛЬКО для дубля; для обзора, шума и для keep=true —
пустая строка. Верни решение по КАЖДОМУ переданному signal_key. reason — коротко по-русски."""


BATCH_REVIEW_SCHEMA = {
    "name": "signal_batch_review",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["decisions"],
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "signal_key", "keep", "duplicate_of_signal_key", "reason", "interest_score", "why_interesting",
                    ],
                    "properties": {
                        "signal_key": {"type": "string"},
                        "keep": {"type": "boolean"},
                        "duplicate_of_signal_key": {"type": "string"},
                        "reason": {"type": "string"},
                        "interest_score": {"type": "number", "minimum": 0, "maximum": 100},
                        "why_interesting": {"type": "string"},
                    },
                },
            },
        },
    },
}


@dataclass(frozen=True)
class SignalDiscoveryConfig:
    topic: str | None = None
    days: int = 14
    limit: int = 80
    min_score: float = 40
    offline: bool = True
    dry_run: bool = True
    max_signals: int = 10
    web_search: bool = False
    web_only: bool = False
    web_query_limit: int = 8
    research_rounds: int = 2
    web_fulltext_limit: int = 20
    background_job_id: int | None = None
    persist_training_examples: bool = True


def seed_default_radar_topics() -> int:
    return repository.seed_signal_radar_topics(DEFAULT_RADAR_TOPICS)


# Разведка разрезана на три слоя, как остальные ИИ-стадии (см. external_ai):
#   1. build_discovery_snapshot — ядро читает базу: темы, статьи, (для воркера) теги и память ОС;
#   2. run_discovery — ни одного обращения к базе: запросы, поиск, кластеры, судья;
#   3. apply_discovery — ядро пишет сигналы, подтверждения и примеры обучения.
# Раньше всё шло одной функцией с базой внутри, и маршрут «через внешний воркер»
# (ca23016) упирался в воркер без базы и без этого вида задач: с 13.09 радар не
# отработал по расписанию ни разу — 134 запуска, все 403 с РФ-адреса.

# Теги для прогона без базы. None — читать из repository, как на ядре.
_TAGS_SNAPSHOT: ContextVar[list[dict[str, Any]] | None] = ContextVar("signal_discovery_tags", default=None)

_TOPIC_SNAPSHOT_FIELDS = ("name", "description", "query_seeds_json", "query_seeds", "industry_scope_json", "tag_id")
_TAG_SNAPSHOT_FIELDS = (
    "id", "parent_id", "name", "name_en", "parent_name", "parent_name_en", "description",
    "keywords_json", "keywords_en_json", "negative_keywords_json",
)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


# Год в поисковых запросах — текущий по Москве (сутки радара считаются так же), а не
# зашитый 2026: с 1 января запросы искали бы прошлогодние события.
RADAR_TZ = ZoneInfo("Europe/Moscow")


def _current_year() -> int:
    return datetime.now(RADAR_TZ).year


def _topic_name(topic: dict[str, Any], config: SignalDiscoveryConfig) -> str:
    return str(topic.get("name") or config.topic or "").strip()


def build_discovery_snapshot(config: SignalDiscoveryConfig, *, for_external: bool = False) -> dict[str, Any]:
    """Всё, что разведке нужно из базы, одним JSON-снимком.

    for_external=True добавляет теги и память ОС: у внешнего воркера базы нет, и без
    снимка эти обогащения молча отваливались бы в except — тот же класс, что тематики
    гейта 17.09 (фича, которая на проде пустая, а в тестах зелёная)."""
    topics = _selected_topics(config.topic)
    if not topics:
        topics = [{"name": config.topic or "HSE technology radar", "query_seeds_json": []}]
    article_evidence: dict[str, list[dict[str, Any]]] = {}
    if not config.web_only:
        for topic in topics:
            name = _topic_name(topic, config)
            article_evidence[name] = repository.list_signal_article_evidence(
                topic=name,
                days=config.days,
                limit=config.limit,
                min_score=config.min_score,
            )
    try:
        known_urls = repository.list_reviewed_signal_urls()
    except Exception:  # noqa: BLE001 - фильтр разобранного — улучшение, а не условие прогона
        known_urls = []
    try:
        existing_signals = repository.list_signals_for_dedup()
    except Exception:  # noqa: BLE001 - без сверки с базой дедуп ограничится карточками прогона
        existing_signals = []
    snapshot: dict[str, Any] = {
        "topics": [{key: topic.get(key) for key in _TOPIC_SNAPSHOT_FIELDS if key in topic} for topic in topics],
        "article_evidence": article_evidence,
        "known_urls": sorted({_normalize_url_for_key(url) for url in known_urls if url}),
        "existing_signals": existing_signals,
        "dedup_max_pairs": app_config.SIGNAL_DEDUP_MAX_PAIRS,
    }
    if for_external:
        try:
            tags = repository.list_enabled_tags()
        except Exception:  # noqa: BLE001 - без тегов разведка беднее, но работает
            tags = []
        snapshot["tags"] = [{key: tag.get(key) for key in _TAG_SNAPSHOT_FIELDS} for tag in tags]
        snapshot["memory"] = memory_snapshot_rows()
    return _jsonable(snapshot)


@contextmanager
def use_discovery_snapshot(snapshot: dict[str, Any]) -> Iterator[None]:
    """Включить теги и память ОС из снимка, если они в нём есть."""
    tags_token = _TAGS_SNAPSHOT.set(snapshot["tags"]) if snapshot.get("tags") is not None else None
    try:
        if snapshot.get("memory") is not None:
            with use_memory_snapshot(snapshot["memory"]):
                yield
        else:
            yield
    finally:
        if tags_token is not None:
            _TAGS_SNAPSHOT.reset(tags_token)


def run_discovery(
    config: SignalDiscoveryConfig,
    snapshot: dict[str, Any],
    *,
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Средний слой: без базы. Возвращает кандидатов в сигналы по каждой теме."""
    beat = heartbeat or (lambda: None)
    known_urls = set(snapshot.get("known_urls") or [])
    topics_out = []
    with use_discovery_snapshot(snapshot):
        for topic in snapshot.get("topics") or []:
            beat()
            topic_name = _topic_name(topic, config)
            article_rows = (snapshot.get("article_evidence") or {}).get(topic_name) or []
            db_evidence = [_article_to_evidence(row, topic_name) for row in article_rows if _has_industry_context(row)]
            evidence = list(db_evidence)
            web_search = None
            if config.web_search or config.web_only:
                web_search = _search_web_evidence(topic, config, heartbeat=beat)
                evidence.extend(web_search["evidence"])
                # Дальше блок нужен только счётчиками: сами тексты уже в кандидатах. Без
                # этого результат воркера вёз каждую докачанную страницу лишний раз.
                web_search = {
                    **{key: value for key, value in web_search.items() if key != "evidence"},
                    "evidence_count": len(web_search.get("evidence") or []),
                }
            evidence = _dedupe_evidence(evidence)
            fresh = [item for item in evidence if _normalize_url_for_key(str(item.get("source_url") or "")) not in known_urls]
            skipped_reviewed = len(evidence) - len(fresh)
            clusters = _cluster_evidence(fresh, topic_name)
            candidates = []
            for cluster in _clusters_for_judging(clusters, config.max_signals):
                beat()
                signal, raw_output = judge_signal_snapshot(cluster, topic_name, offline=config.offline)
                if topic.get("tag_id") is not None:
                    # Тема радара = тематика заказчика: фильтр «Тема» на экране — это его 13 тегов,
                    # а не свободный текст модели (было 34 разных «темы» на 38 сигналов).
                    signal["theme"] = topic_name
                signal["signal_key"] = _signal_key(signal, cluster)
                signal["evidence_count"] = len({str(item.get("source_url") or "") for item in cluster if item.get("source_url")})
                signal["evidence"] = cluster
                candidates.append({
                    "signal": signal,
                    "raw_output": raw_output,
                    "rejected": _is_rejected_signal(signal),
                    "training_input": _training_input_payload(topic_name, cluster, web_search, offline=config.offline),
                })
            beat()
            batch_review = _batch_review_candidates(candidates, topic_name, offline=config.offline)
            topics_out.append({
                "topic": topic_name,
                "article_evidence": len(db_evidence),
                "total_evidence": len(evidence),
                "skipped_reviewed": skipped_reviewed,
                "web_search": web_search,
                "clusters": len(clusters),
                "candidates": candidates,
                "batch_review": batch_review,
            })
    return {"topics": topics_out, "dedup": _dedupe_run(config, snapshot, topics_out, beat)}


def _dedupe_run(
    config: SignalDiscoveryConfig,
    snapshot: dict[str, Any],
    topics_out: list[dict[str, Any]],
    beat: Callable[[], None],
) -> dict[str, Any]:
    """Сверить принятых кандидатов между собой и с сохранёнными карточками.

    Решение кладётся в кандидата (duplicate_of) и в existing_merges — пишет их ядро."""
    if config.offline:
        return {"skipped": "offline"}
    nodes: list[dict[str, Any]] = []
    for row in snapshot.get("existing_signals") or []:
        nodes.append({
            "kind": "existing",
            "id": int(row["id"]),
            "reviewed": bool(row.get("reviewed") or row.get("verdict")),
            "verdict": row.get("verdict"),
            "fresh": bool(row.get("fresh")),
            "urls": row.get("evidence_urls") or [],
            "signal": row,
        })
    refs: dict[int, dict[str, Any]] = {}
    batch_duplicates: list[dict[str, Any]] = []
    for topic in topics_out:
        for candidate in topic["candidates"]:
            if candidate["rejected"]:
                continue
            if candidate.get("duplicate_of"):
                # Дубль уже найден ревью пачки — в пары не идёт; ниже пойдёт за своей
                # главной, если ту дедуп склеит с кем-то ещё (звезда, а не цепочка).
                batch_duplicates.append(candidate)
                continue
            signal = candidate["signal"]
            refs[len(nodes)] = candidate
            nodes.append({
                "kind": "new",
                "reviewed": False,
                "fresh": True,
                "order": len(refs),
                "urls": [item.get("source_url") for item in signal.get("evidence") or [] if item.get("source_url")],
                "signal": signal,
            })
    result = signal_dedup.dedupe(
        nodes,
        client_factory=lambda: make_client(False),
        heartbeat=beat,
        max_pairs=int(snapshot.get("dedup_max_pairs") or signal_dedup.MAX_JUDGED_PAIRS),
    )
    existing_merges = []
    for index, (primary_index, reason) in result["assigned"].items():
        primary = nodes[primary_index]
        if index in refs:
            refs[index]["duplicate_of"] = (
                {"signal_id": primary["id"]} if primary["kind"] == "existing"
                else {"signal_key": primary["signal"]["signal_key"]}
            )
            refs[index]["duplicate_reason"] = reason
        elif primary["kind"] == "existing":
            existing_merges.append({"signal_id": nodes[index]["id"], "into": primary["id"], "reason": reason})
    by_key = {str(ref["signal"].get("signal_key") or ""): ref for ref in refs.values()}
    for candidate in batch_duplicates:
        primary = by_key.get(str(candidate["duplicate_of"].get("signal_key") or ""))
        if primary is not None and primary.get("duplicate_of"):
            candidate["duplicate_of"] = dict(primary["duplicate_of"])
    return {
        **result["stats"],
        "duplicates_new": sum(1 for index in result["assigned"] if index in refs),
        "batch_duplicates": len(batch_duplicates),
        "existing_merges": existing_merges,
    }


def apply_discovery(
    config: SignalDiscoveryConfig,
    run: dict[str, Any],
    *,
    generation_run_id: int | None = None,
) -> dict[str, Any]:
    """Ядро: записать сигналы из результата run_discovery и собрать итог."""
    all_signals: list[dict[str, Any]] = []
    topic_results = []
    duplicates: list[tuple[str, dict[str, Any]]] = []
    stored_keys: dict[str, int] = {}
    owners = _key_owners(config, run)
    for topic in run.get("topics") or []:
        topic_name = str(topic.get("topic") or "")
        judged = []
        topic_duplicates = 0
        for candidate in topic.get("candidates") or []:
            signal = candidate["signal"]
            rejected = bool(candidate.get("rejected"))
            if not rejected:
                _reconcile_with_owner(candidate, owners.get(str(signal.get("signal_key") or "")))
            if not rejected and candidate.get("duplicate_of"):
                # Главная карточка группы может стоять в другой теме дальше по списку —
                # дубли пишем, когда все главные уже сохранены.
                duplicates.append((topic_name, candidate))
                topic_duplicates += 1
                continue
            signal_id = _store_candidate(config, topic_name, candidate, generation_run_id)
            if signal_id is not None:
                stored_keys[signal["signal_key"]] = signal_id
            if rejected:
                continue
            judged.append(signal)
            all_signals.append(signal)
        topic_results.append({
            "topic": topic_name,
            "article_evidence": topic.get("article_evidence", 0),
            "total_evidence": topic.get("total_evidence", 0),
            "skipped_reviewed": topic.get("skipped_reviewed", 0),
            "web_search": topic.get("web_search"),
            "clusters": topic.get("clusters", 0),
            "signals": judged,
            "duplicates": topic_duplicates,
            "batch_review": topic.get("batch_review"),
        })
    dedup = dict(run.get("dedup") or {})
    merged = 0
    for topic_name, candidate in duplicates:
        if _merge_duplicate(config, topic_name, candidate, stored_keys, generation_run_id):
            merged += 1
        elif not config.dry_run:
            # Главной карточки нет (удалена, не записана) — сигнал не теряем.
            signal_id = _store_candidate(config, topic_name, candidate, generation_run_id)
            candidate["signal"]["id"] = signal_id
            all_signals.append(candidate["signal"])
    merged_existing = 0
    if not config.dry_run:
        for merge in dedup.pop("existing_merges", None) or []:
            if repository.mark_signal_merged(int(merge["signal_id"]), int(merge["into"]), str(merge.get("reason") or "")):
                merged_existing += 1
    else:
        dedup.pop("existing_merges", None)
    if dedup or merged or merged_existing:
        dedup.update({"merged_new": merged, "merged_existing": merged_existing})
    # interest_score сравнивает сигнал с остальными кандидатами его пачки (batch review);
    # сырой score судьи оценивал кластер в изоляции и не видел, что сосед интереснее.
    # Если interest_score не проставлен (например тема пропустила batch review из-за
    # единственного кандидата), откатываемся на score — так топ N не проседает.
    all_signals.sort(
        key=lambda item: (
            float(item["interest_score"] if item.get("interest_score") is not None else item.get("score") or 0),
            float(item.get("score") or 0),
            int(item.get("evidence_count") or 0),
        ),
        reverse=True,
    )
    return {
        "dry_run": config.dry_run,
        "offline": config.offline,
        "web_search": config.web_search or config.web_only,
        "web_only": config.web_only,
        "days": config.days,
        "topics": [str(topic.get("topic") or "") for topic in run.get("topics") or []],
        "signals": all_signals[: config.max_signals],
        "all_signals": len(all_signals),
        "topic_results": topic_results,
        "generation_run_id": generation_run_id,
        "dedup": dedup or None,
    }


def _key_owners(config: SignalDiscoveryConfig, run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if config.dry_run:
        return {}
    keys = sorted({
        str(candidate["signal"].get("signal_key") or "")
        for topic in run.get("topics") or []
        for candidate in topic.get("candidates") or []
        if not candidate.get("rejected") and candidate["signal"].get("signal_key")
    })
    return repository.signal_key_owners(keys) if keys else {}


def _reconcile_with_owner(candidate: dict[str, Any], owner: dict[str, Any] | None) -> None:
    """Кандидат с ключом уже сохранённой карточки — это она же, найденная снова.

    Скрытая дублем — повтор идёт в её главную: иначе upsert записал бы его в скрытую
    строку, и сигнал пропал бы с экрана. Разобранная человеком — обновляется сама, как
    до дедупа: судья не переспорит разбор, и её ссылка не уедет в чужую карточку.
    Видимая неразобранная, которую судья счёл дублем другой, скрывается вместе с ним."""
    if not owner:
        return
    if owner.get("merged_into_signal_id") is not None:
        candidate["duplicate_of"] = {"signal_id": int(owner["merged_into_signal_id"])}
        candidate["duplicate_reason"] = "та же ссылка уже склеена с этой карточкой"
    elif owner.get("reviewed"):
        candidate.pop("duplicate_of", None)
    elif candidate.get("duplicate_of"):
        candidate["key_owner_id"] = int(owner["id"])


def _store_candidate(
    config: SignalDiscoveryConfig,
    topic_name: str,
    candidate: dict[str, Any],
    generation_run_id: int | None,
) -> int | None:
    signal = candidate["signal"]
    cluster = signal.get("evidence") or []
    rejected = bool(candidate.get("rejected"))
    signal_id = None
    if not rejected and not config.dry_run:
        signal_id = repository.upsert_signal(signal)
        signal["id"] = signal_id
        for item in cluster:
            repository.upsert_signal_evidence(signal_id, item)
        signal["evidence_count"] = repository.refresh_signal_evidence_count(signal_id)
    if generation_run_id is not None:
        repository.create_signal_training_example(
            generation_run_id=generation_run_id,
            signal_id=signal_id,
            topic=topic_name,
            signal_key=signal["signal_key"],
            pipeline_verdict="rejected" if rejected else "accepted",
            input_payload=candidate.get("training_input") or {},
            raw_output=candidate.get("raw_output") or {},
            normalized_output=signal,
        )
    return signal_id


def _merge_duplicate(
    config: SignalDiscoveryConfig,
    topic_name: str,
    candidate: dict[str, Any],
    stored_keys: dict[str, int],
    generation_run_id: int | None,
) -> bool:
    """Ссылки дубля — в главную карточку, новой карточки нет. False — главной не нашлось."""
    signal = candidate["signal"]
    target = candidate.get("duplicate_of") or {}
    if config.dry_run:
        return True
    target_id = target.get("signal_id") or stored_keys.get(str(target.get("signal_key") or ""))
    target_id = repository.resolve_signal_merge_root(int(target_id)) if target_id else None
    if target_id is None:
        return False
    owner_id = candidate.get("key_owner_id")
    if owner_id and owner_id != target_id:
        # Ключ дубля — у видимой карточки: это тот же материал, она тоже дубль главной.
        repository.mark_signal_merged(int(owner_id), target_id, str(candidate.get("duplicate_reason") or ""))
    for item in signal.get("evidence") or []:
        repository.upsert_signal_evidence(target_id, item)
    repository.refresh_signal_evidence_count(target_id)
    if owner_id and owner_id != target_id:
        repository.refresh_signal_evidence_count(int(owner_id))
    repository.touch_signal(target_id)
    if generation_run_id is not None:
        repository.create_signal_training_example(
            generation_run_id=generation_run_id,
            signal_id=target_id,
            topic=topic_name,
            signal_key=signal["signal_key"],
            pipeline_verdict="duplicate",
            input_payload=candidate.get("training_input") or {},
            raw_output={**(candidate.get("raw_output") or {}), "duplicate_reason": candidate.get("duplicate_reason")},
            normalized_output=signal,
        )
    return True


def discover_signals(config: SignalDiscoveryConfig) -> dict[str, Any]:
    """Прогон целиком на ядре (CLI, локальная очередь, тесты)."""
    generation_run_id = None
    if config.persist_training_examples and not config.dry_run:
        generation_run_id = repository.create_signal_generation_run(
            config_payload=asdict(config),
            trigger="signal_discovery",
            background_job_id=config.background_job_id,
        )
    try:
        snapshot = build_discovery_snapshot(config)
        run = run_discovery(config, snapshot)
        result = apply_discovery(config, run, generation_run_id=generation_run_id)
    except Exception as exc:
        if generation_run_id is not None:
            repository.finish_signal_generation_run(
                generation_run_id,
                status="failed",
                result={},
                error_message=str(exc)[:1000],
            )
        raise
    if generation_run_id is not None:
        repository.finish_signal_generation_run(generation_run_id, status="ok", result={
            "topics": len(result["topics"]),
            "signals": result["all_signals"],
            "returned_signals": len(result["signals"]),
        })
    return result


def _payload_number(payload: dict[str, Any], key: str, default: Any, cast: Callable[[Any], Any]) -> Any:
    """Нет значения — умолчание; явный 0 — это 0.

    `int(payload.get(key) or 20)` превращал 0 в 20, и выключатель докачки
    SIGNAL_DISCOVERY_WEB_FULLTEXT_LIMIT=0 на ежедневном пути не выключал ничего (21.09)."""
    value = payload.get(key)
    if value is None or value == "":
        return default
    return cast(value)


def config_from_payload(payload: dict[str, Any], *, background_job_id: int | None = None) -> SignalDiscoveryConfig:
    """Параметры задачи из payload — те же умолчания, что у локального обработчика."""
    return SignalDiscoveryConfig(
        topic=str(payload["topic"]) if payload.get("topic") else None,
        days=_payload_number(payload, "days", 14, int),
        limit=_payload_number(payload, "limit", 80, int),
        min_score=_payload_number(payload, "min_score", 40.0, float),
        offline=bool(payload.get("offline", True)),
        dry_run=bool(payload.get("dry_run", False)),
        max_signals=_payload_number(payload, "max_signals", 10, int),
        web_search=bool(payload.get("web_search", False)),
        web_only=bool(payload.get("web_only", False)),
        web_query_limit=_payload_number(payload, "web_query_limit", 8, int),
        # Задача с экрана эти поля не шлёт — умолчание из настроек ядра, чтобы
        # выключатель в .env действовал на любой запуск, а не только на ежедневный.
        research_rounds=_payload_number(payload, "research_rounds", app_config.SIGNAL_DISCOVERY_RESEARCH_ROUNDS, int),
        web_fulltext_limit=_payload_number(
            payload, "web_fulltext_limit", app_config.SIGNAL_DISCOVERY_WEB_FULLTEXT_LIMIT, int
        ),
        background_job_id=background_job_id,
    )


def build_external_payload(job_payload: dict[str, Any]) -> dict[str, Any]:
    """Ядро, в момент выдачи задачи воркеру: параметры + снимок базы."""
    config = config_from_payload(job_payload)
    config_dict = {key: value for key, value in asdict(config).items() if key != "background_job_id"}
    return {
        "kind": "signal_discovery",
        "config": config_dict,
        "snapshot": build_discovery_snapshot(config, for_external=True),
    }


def process_external_payload(payload: dict[str, Any], heartbeat: Callable[[], None] | None = None) -> dict[str, Any]:
    """Сторона воркера: в базу не ходит, возвращает кандидатов ядру."""
    allowed = {item.name for item in fields(SignalDiscoveryConfig)}
    config = SignalDiscoveryConfig(**{
        key: value for key, value in (payload.get("config") or {}).items() if key in allowed
    })
    run = run_discovery(config, payload.get("snapshot") or {}, heartbeat=heartbeat)
    return {"signal_discovery": True, "config": payload.get("config") or {}, "run": run}


def apply_external_result(result: dict[str, Any], *, job_id: int) -> dict[str, Any]:
    """Ядро, при завершении задачи: записать сигналы и вернуть компактный итог.

    Полный результат (все кандидаты с подтверждениями и промптами) в result_json задачи
    не кладём — он осядет в signal_training_examples; в задаче остаются счётчики."""
    allowed = {item.name for item in fields(SignalDiscoveryConfig)}
    config = replace(
        SignalDiscoveryConfig(**{
            key: value for key, value in (result.get("config") or {}).items() if key in allowed
        }),
        background_job_id=job_id,
    )
    generation_run_id = None
    if config.persist_training_examples and not config.dry_run:
        generation_run_id = repository.create_signal_generation_run(
            config_payload=asdict(config),
            trigger="signal_discovery_external",
            background_job_id=job_id,
        )
    applied = apply_discovery(config, result.get("run") or {}, generation_run_id=generation_run_id)
    topics = []
    for row in applied["topic_results"]:
        web = row.get("web_search") or {}
        review = row.get("batch_review") or {}
        topics.append({
            "topic": row["topic"],
            "web_status": web.get("status"),
            "queries": len(web.get("queries") or []),
            "results": web.get("results"),
            # Без этих трёх полей в задаче не видно, работают ли раунды поиска,
            # докачка страниц и ревью пачки — проверка выката смотрит сюда.
            "research_modes": [item.get("mode") for item in web.get("research_rounds") or []],
            "fulltext": web.get("fulltext"),
            "batch_review": {
                key: review.get(key)
                for key in ("status", "source", "reviewed", "dropped", "duplicates", "reason", "error")
                if review.get(key) is not None
            } or None,
            "total_evidence": row.get("total_evidence"),
            "skipped_reviewed": row.get("skipped_reviewed"),
            "clusters": row.get("clusters"),
            "signals": len(row.get("signals") or []),
            "duplicates": row.get("duplicates", 0),
        })
    summary = {
        "generation_run_id": generation_run_id,
        "signals": applied["all_signals"],
        "persisted": 0 if config.dry_run else applied["all_signals"],
        "topics": topics,
        "dedup": applied.get("dedup"),
    }
    if generation_run_id is not None:
        repository.finish_signal_generation_run(generation_run_id, status="ok", result={
            "topics": len(topics),
            "signals": applied["all_signals"],
            "returned_signals": len(applied["signals"]),
        })
    return summary


def judge_signal(evidence: list[dict[str, Any]], topic: str, *, offline: bool = True) -> dict[str, Any]:
    return judge_signal_snapshot(evidence, topic, offline=offline)[0]


def judge_signal_snapshot(evidence: list[dict[str, Any]], topic: str, *, offline: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    if offline:
        signal = _offline_signal_judgement(evidence, topic)
        return signal, signal
    client = make_client(False)
    response: AIResponse = client.complete_json(
        SIGNAL_JUDGE_INSTRUCTIONS,
        _judge_prompt(evidence, topic),
        SIGNAL_JUDGE_SCHEMA,
        max_output_tokens=1800,
    )
    return _normalize_signal_payload(response.data, topic, context=_glossary_context(evidence, topic)), response.data


def _batch_review_candidates(
    candidates: list[dict[str, Any]],
    topic: str,
    *,
    offline: bool,
) -> dict[str, Any]:
    """Финальный взгляд на пачку сигналов темы целиком, а не по одному кластеру.

    judge_signal_snapshot оценивает каждый кластер в изоляции и не видит остальные
    кандидаты этой темы — поэтому не может заметить, что кандидат №3 пересказывает
    то же событие, что и №1, другими словами, или что кандидат сам по себе похож на
    факт, но на фоне всей пачки явно не тянет на отдельный сигнал. Мутирует переданные
    candidate-словари на месте, потому что именно эти объекты потом уходят в
    _dedupe_run и apply_discovery.

    Два разных исхода «убрать»: обзор/шум — rejected (брак), дубль соседа — duplicate_of
    оставшегося. Дубль не брак: его ссылки уходят в главную карточку путём дедупа
    (_merge_duplicate), а в обучение он идёт как «duplicate». Пометка дубля браком
    теряла ссылки и учила радар, что пересказ сильного события — мусор (21.09).
    """
    reviewable = [item for item in candidates if not item.get("rejected") and item.get("signal")]
    if len(reviewable) < 2:
        return {"status": "skipped", "reason": "fewer_than_2_candidates", "reviewed": len(reviewable),
                "dropped": 0, "duplicates": 0}

    if offline:
        duplicates = _offline_batch_duplicates(reviewable)
        interest_scores = _apply_fallback_interest_scores(
            [item for item in reviewable if not item.get("duplicate_of")]
        )
        return {
            "status": "ok",
            "source": "rules",
            "reviewed": len(reviewable),
            "dropped": 0,
            "duplicates": len(duplicates),
            "decisions": duplicates,
            "interest_scores": interest_scores,
        }

    client = make_client(False)
    payload = {
        "topic": topic,
        "candidates": [_batch_review_candidate_payload(item) for item in reviewable],
    }
    try:
        response = client.complete_json(
            BATCH_REVIEW_INSTRUCTIONS,
            json.dumps(payload, ensure_ascii=False),
            BATCH_REVIEW_SCHEMA,
            max_output_tokens=900,
        )
    except Exception as exc:  # noqa: BLE001 - батч-ревью не должно ронять прогон темы
        return {"status": "error", "error": str(exc)[:500], "reviewed": len(reviewable), "dropped": 0,
                "duplicates": 0}

    by_key = {str(item["signal"].get("signal_key") or ""): item for item in reviewable}
    decisions: dict[str, dict[str, Any]] = {}
    for decision in response.data.get("decisions") or []:
        key = str(decision.get("signal_key") or "")
        if key in by_key and key not in decisions:
            decisions[key] = decision
    # Кого модель не упомянула — остаётся (ниже получит балл судьи вместо interest).
    kept = {key for key in by_key if decisions.get(key, {}).get("keep", True)}
    targets = {
        key: str(decision.get("duplicate_of_signal_key") or "").strip()
        for key, decision in decisions.items()
        if not decision.get("keep", True)
    }
    targets = {key: target for key, target in targets.items() if target and target != key and target in by_key}

    dropped: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    unscored: list[dict[str, Any]] = [item for key, item in by_key.items() if key not in decisions]
    for key, decision in decisions.items():
        candidate = by_key[key]
        if key in kept:
            # Судья оценивал score в изоляции; interest_score — сравнение внутри пачки,
            # поэтому именно он должен решать финальную сортировку, а не сырой score.
            candidate["signal"]["interest_score"] = _normalize_score(decision.get("interest_score"))
            candidate["signal"]["why_interesting"] = str(decision.get("why_interesting") or "").strip()
            continue
        reason = str(decision.get("reason") or "").strip() or "Отклонён на финальной проверке пачки сигналов."
        root = _batch_duplicate_root(key, targets, kept)
        if root == "cycle":
            # Противоречивый ответ (A — дубль B, B — дубль A): сигнал не теряем.
            kept.add(key)
            unscored.append(candidate)
            continue
        if root:
            candidate["duplicate_of"] = {"signal_key": root}
            candidate["duplicate_reason"] = reason
            candidate["signal"]["batch_review_reason"] = reason
            duplicates.append({"signal_key": key, "action": "duplicate", "duplicate_of": root, "reason": reason})
            continue
        candidate["rejected"] = True
        candidate["signal"]["maturity"] = "reject"
        candidate["signal"]["batch_review_reason"] = reason
        dropped.append({"signal_key": key, "action": "reject", "reason": reason})

    # Модель могла пропустить кандидата вопреки инструкции — не терять ранжирование
    # из-за одного недостающего решения, откатываемся на score судьи для него.
    interest_scores = _apply_fallback_interest_scores(unscored)
    interest_scores.update({
        key: by_key[key]["signal"].get("interest_score")
        for key in kept
        if by_key[key]["signal"].get("interest_score") is not None
    })

    return {
        "status": "ok",
        "source": "ai",
        "model": response.model,
        "reviewed": len(reviewable),
        "dropped": len(dropped),
        "duplicates": len(duplicates),
        "decisions": dropped + duplicates,
        "interest_scores": interest_scores,
    }


def _batch_duplicate_root(key: str, targets: dict[str, str], kept: set[str]) -> str | None:
    """Оставшийся кандидат, в которого сливается дубль; None — цепочка кончилась браком.

    Модель может сослаться на дубль (A → B, B → C): идём до оставшегося — звезда, а не
    цепочка, как у дедупа. «cycle» — модель противоречит себе."""
    if key not in targets:
        return None
    visited = {key}
    current = targets[key]
    while current not in kept:
        if current in visited:
            return "cycle"
        if current not in targets:
            return None
        visited.add(current)
        current = targets[current]
    return current


def _batch_review_candidate_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    signal = candidate["signal"]
    evidence = signal.get("evidence") or []
    return {
        "signal_key": signal.get("signal_key"),
        "title": signal.get("title_ru") or signal.get("title"),
        "theme": signal.get("theme"),
        "maturity": signal.get("maturity"),
        "score": signal.get("score"),
        "summary": signal.get("summary"),
        "companies": signal.get("companies") or [],
        "evidence_urls": [str(item.get("source_url") or "") for item in evidence if item.get("source_url")][:5],
    }


def _apply_fallback_interest_scores(items: list[dict[str, Any]]) -> dict[str, float]:
    """Когда сравнение пачки недоступно (offline) или модель пропустила кандидата,

    используем score судьи как черновую замену interest_score — без неё сортировка
    apply_discovery осталась бы без числа для сравнения этого сигнала с остальными.
    """
    scores: dict[str, float] = {}
    for item in items:
        signal = item["signal"]
        signal.setdefault("interest_score", _normalize_score(signal.get("score")))
        scores[str(signal.get("signal_key") or "")] = signal["interest_score"]
    return scores


def _offline_batch_duplicates(reviewable: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Без LLM ловим только близкие дубли по заголовку и компаниям (best-effort).

    Настоящее «это тот же трюизм другими словами» без модели не распознать —
    офлайн-режим здесь такой же грубый фоллбэк, как и остальная rules-эвристика
    в этом модуле (например recommend_source_action). Похожий — дубль сильнейшего
    из оставшихся (duplicate_of), а не брак: его ссылки уходят в ту карточку.
    """
    duplicates = []
    kept: list[tuple[set[str], str]] = []
    for candidate in sorted(reviewable, key=lambda item: float(item["signal"].get("score") or 0), reverse=True):
        signal = candidate["signal"]
        tokens = _batch_dedupe_tokens(signal)
        primary = next((key for kept_tokens, key in kept if _jaccard(tokens, kept_tokens) >= 0.6), None)
        if primary is None:
            kept.append((tokens, str(signal.get("signal_key") or "")))
            continue
        reason = "Похож на другой сигнал этой пачки (офлайн-правило, без модели)."
        candidate["duplicate_of"] = {"signal_key": primary}
        candidate["duplicate_reason"] = reason
        signal["batch_review_reason"] = reason
        duplicates.append({"signal_key": signal.get("signal_key"), "action": "duplicate",
                           "duplicate_of": primary, "reason": reason})
    return duplicates


def _batch_dedupe_tokens(signal: dict[str, Any]) -> set[str]:
    text = " ".join([
        str(signal.get("title") or ""),
        " ".join(str(company) for company in signal.get("companies") or []),
    ]).lower()
    return {word for word in re.findall(r"[a-zа-яё0-9]{4,}", text) if word not in _STOP_WORDS}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def list_signals(*, maturity: str | None = None, theme: str | None = None, limit: int = 50) -> list[dict]:
    return repository.list_signals(maturity=maturity, theme=theme, limit=limit)


def assign_query_hint_topics(*, dry_run: bool = True) -> dict[str, Any]:
    """Привязать старые подсказки поиска без темы к ОДНОЙ теме радара.

    Подсказки 13–15.09 родились до тем-тегов и темы не знают; с 21.09 такие в поиск не
    идут. Здесь каждой подбирается тема с наибольшим числом общих основ слов (название
    темы + ключи её тематики); берём, только если совпадений не меньше двух и лидер
    один — иначе подсказка остаётся без темы, чем уедет в чужую. Сухой прогон по
    умолчанию: список показывают владельцу до записи."""
    profiles = []
    for topic in _radar_topics():
        name = str(topic.get("name") or "").strip()
        if not name:
            continue
        context = _topic_tag_context(name)
        profiles.append((name, topic_term_stems(
            name,
            str(topic.get("description") or ""),
            *(context.get("keywords_ru") or []),
            *(context.get("keywords_en") or []),
        )))
    rows = repository.list_signal_agent_memory(memory_type="signal_query_hint", status="active", limit=100_000)
    assigned: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []
    already = 0
    for row in rows:
        if str((row.get("facts_json") or {}).get("topic") or "").strip():
            already += 1
            continue
        subject = str(row.get("subject") or "")
        stems = topic_term_stems(subject)
        scored = sorted(((len(stems & terms), name) for name, terms in profiles), key=lambda item: -item[0])
        best = scored[0] if scored else (0, "")
        runner_up = scored[1][0] if len(scored) > 1 else 0
        item = {"id": row.get("id"), "subject": subject, "topic": best[1], "overlap": best[0], "runner_up": runner_up}
        if best[0] >= 2 and best[0] > runner_up:
            assigned.append(item)
            if not dry_run:
                repository.merge_signal_agent_memory_facts(int(row["id"]), {"topic": best[1], "topic_assigned": "stems_21.09"})
        else:
            unassigned.append(item)
    return {"dry_run": dry_run, "active": len(rows), "already_scoped": already,
            "assigned": assigned, "unassigned": unassigned}


def topics_from_tags(tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Темы радара = корневые тематики заказчика (13), без служебного приёмника.

    Пункт 12 Виктора: сигналы раскладывать по его тегам. Прежние 21 тема (4 HSE +
    17 направлений из сида) давали 25 из 38 сигналов с темой «HSE/…» — отсюда
    «меньше в основных, больше в технологических»."""
    topics = []
    for tag in tags:
        if tag.get("parent_id") is not None:
            continue
        name = str(tag.get("name") or "").strip()
        if not name or name == repository.SYSTEM_TAG_UNCLASSIFIED:
            continue
        topics.append({
            "name": name,
            "description": str(tag.get("description") or "").strip(),
            "query_seeds_json": [],
            "tag_id": tag.get("id"),
        })
    return topics


def _radar_topics() -> list[dict]:
    if app_config.SIGNAL_RADAR_TOPIC_SOURCE == "tags":
        try:
            topics = topics_from_tags(repository.list_enabled_tags())
        except Exception:  # noqa: BLE001 - без справочника тегов откатываемся на таблицу тем
            topics = []
        if topics:
            return topics
    return repository.list_signal_radar_topics(enabled_only=True)


def _selected_topics(topic: str | None) -> list[dict]:
    rows = _radar_topics()
    if not topic:
        return rows or DEFAULT_RADAR_TOPICS
    topic_l = topic.lower()
    selected = [row for row in rows if topic_l in str(row.get("name") or "").lower()]
    if selected:
        return selected
    default_selected = [row for row in DEFAULT_RADAR_TOPICS if topic_l in row["name"].lower()]
    return default_selected or [{"name": topic, "query_seeds_json": []}]


def _article_to_evidence(row: dict[str, Any], topic: str) -> dict[str, Any]:
    title = str(row.get("title_ru") or row.get("title") or "").strip()
    summary = str(row.get("summary") or row.get("raw_text") or "")[:900].strip()
    context = {
        "title": title,
        "raw_text": " ".join([str(row.get("title") or ""), str(row.get("summary") or ""), str(row.get("raw_text") or "")]),
        "language": row.get("language") or "",
    }
    summary_ru = _enforce_glossary(summary or title, context, topic)
    score = float(row.get("total_score") or 50)
    return {
        "article_id": row.get("article_id"),
        "source_url": row["source_url"],
        "title": title,
        "title_ru": _enforce_glossary(title, context, topic),
        "publisher": row.get("publisher"),
        "published_at": row.get("published_at"),
        "evidence_type": _evidence_type(title + " " + summary),
        "extracted_fact": summary or title,
        "summary_ru": summary_ru,
        "strength": min(1.0, max(0.1, score / 100)),
        "topic": topic,
        "raw_payload": {
            "tag": row.get("tag_name"),
            "score": score,
            "score_label": row.get("score_label"),
            "relevance_reason": row.get("relevance_reason"),
        },
    }


def _search_web_evidence(
    topic: dict[str, Any],
    config: SignalDiscoveryConfig,
    *,
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, Any]:
    from oiltech_digest.source_discovery.agent import generate_search_queries, search_web

    # Поиск, генерация запросов и докачка страниц — самые долгие шаги темы: без
    # продления аренды на медленных сайтах задача теряла lease и оплачивалась дважды.
    beat = heartbeat or (lambda: None)
    year = _current_year()
    topic_name = str(topic.get("name") or config.topic or "").strip()
    tag_context = _topic_tag_context(topic_name)
    seed_queries = _topic_seed_queries(topic, year=year, tag_context=tag_context)
    feedback_queries = feedback_query_hints(topic_name, limit=config.web_query_limit)
    generation_topic = _query_generation_topic(topic, tag_context)
    generated_queries = generate_search_queries(
        generation_topic,
        offline=config.offline,
        limit=config.web_query_limit,
        strategy="broad",
    )
    queries = _planned_search_queries(
        topic,
        config,
        tag_context,
        feedback_queries=feedback_queries,
        seed_queries=seed_queries,
        generated_queries=generated_queries,
        year=year,
    )
    search = _run_research_loop(
        search_web,
        initial_queries=queries,
        topic=topic,
        topic_name=topic_name,
        tag_context=tag_context,
        config=config,
        year=year,
        heartbeat=beat,
    )
    results = search.get("results") or []
    evidence = [
        item
        for item in (_search_result_to_evidence(row, topic_name) for row in results)
        if item and _has_industry_context(item) and not _blocked_by_tag_negative_keywords(item, tag_context)
    ]
    evidence, fulltext_stats = _enrich_web_evidence_with_full_text(
        evidence,
        topic_name,
        limit=config.web_fulltext_limit,
        heartbeat=beat,
    )
    return {
        "status": search.get("status"),
        "provider": search.get("provider"),
        "reason": search.get("reason"),
        "queries": search.get("queries") or queries,
        "initial_queries": queries,
        "research_rounds": search.get("research_rounds") or [],
        "results": len(results),
        "evidence": evidence,
        "fulltext": fulltext_stats,
        "tag_context": _tag_context_snapshot(tag_context),
        "errors": search.get("errors") or [],
    }


def _run_research_loop(
    search_web: Callable[[list[str]], dict[str, Any]],
    *,
    initial_queries: list[str],
    topic: dict[str, Any],
    topic_name: str,
    tag_context: dict[str, Any],
    config: SignalDiscoveryConfig,
    year: int | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, Any]:
    beat = heartbeat or (lambda: None)
    year = _current_year() if year is None else year
    max_rounds = max(1, int(config.research_rounds or 1))
    query_limit = max(1, int(config.web_query_limit or 1))
    active_queries = _dedupe(initial_queries)[:query_limit]
    all_queries: list[str] = []
    all_results: list[dict[str, Any]] = []
    all_errors: list[str] = []
    rounds: list[dict[str, Any]] = []
    provider = None
    status = "empty"
    reason = None
    mode = "initial"

    for round_index in range(1, max_rounds + 1):
        if not active_queries:
            break
        beat()
        search = search_web(active_queries, limit=config.limit)
        provider = search.get("provider") or provider
        status = str(search.get("status") or status)
        reason = search.get("reason") or reason
        results = search.get("results") or []
        errors = [str(item) for item in search.get("errors") or [] if str(item)]
        all_queries.extend(active_queries)
        all_results.extend(results)
        all_errors.extend(errors)
        quality = _round_signal_quality(results)
        rounds.append({
            "round": round_index,
            "queries": active_queries,
            "status": search.get("status"),
            "results": len(results),
            "mode": mode,
            "followup": mode == "followup",
            "quality": quality,
        })
        if round_index >= max_rounds or not results:
            break
        if quality["strong"]:
            mode = "followup"
            active_queries = _research_followup_queries(
                results,
                topic_name,
                tag_context,
                year=year,
                limit=query_limit,
                seen_queries=all_queries,
            )
        else:
            mode = "pivot"
            active_queries = _research_pivot_queries(
                topic,
                tag_context,
                year=year,
                limit=query_limit,
                seen_queries=all_queries,
            )
        rounds[-1]["next_mode"] = mode if active_queries else "stop"

    deduped_results = _dedupe_search_results(all_results, config.limit)
    return {
        "status": "ok" if deduped_results else status,
        "provider": provider,
        "reason": reason,
        "queries": _dedupe(all_queries),
        "limit": config.limit,
        "results": deduped_results,
        "errors": all_errors[:10],
        "research_rounds": rounds,
    }


def _round_signal_quality(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Оценить раунд поиска: есть ли отраслевой контекст и признак события.

    Отраслевой контекст без события (просто общее упоминание нефтегаза) — это
    ещё не зацепка для follow-up: углубляться в такую выдачу бессмысленно, надо
    менять угол поиска (pivot), а не пытаться уточнить компанию/технологию из шума.
    """
    industry_hits = 0
    event_hits = 0
    for row in results:
        text = _clean_search_text(f"{row.get('title') or ''} {row.get('snippet') or ''}")
        if not text:
            continue
        if INDUSTRY_CONTEXT_RE.search(text):
            industry_hits += 1
            if EVENT_SIGNAL_RE.search(text):
                event_hits += 1
    return {
        "industry_hits": industry_hits,
        "event_hits": event_hits,
        "has_industry_context": industry_hits > 0,
        "has_event_signal": event_hits > 0,
        "strong": industry_hits > 0 and event_hits > 0,
    }


def _research_pivot_queries(
    topic: dict[str, Any],
    tag_context: dict[str, Any],
    *,
    year: int,
    limit: int,
    seen_queries: list[str],
) -> list[str]:
    """Собрать новый поисковый угол, когда прошлый раунд не дал зацепки.

    Follow-up углубляется в найденную компанию/технологию, поэтому бесполезен на
    слабой выдаче: он просто повторит тот же шум другими словами. Pivot вместо
    этого перебирает свежие пары термин×угол (`_angle_suffixes_for_terms`) —
    term-major порядок внутри каждого угла даёт заметно больше вариантов, чем
    в исходном раунде, так что `_round_robin_queries` почти всегда находит
    непройденные формулировки.
    """
    terms = _topic_search_terms(topic, tag_context)
    if not terms:
        return []
    suffixes = _angle_suffixes_for_terms(terms)
    groups = [[f"{year} {term} {suffix}" for term in terms] for suffix in suffixes]
    return _round_robin_queries(groups, limit=limit, seen=seen_queries)


def _research_followup_queries(
    results: list[dict[str, Any]],
    topic_name: str,
    tag_context: dict[str, Any],
    *,
    year: int,
    limit: int,
    seen_queries: list[str],
) -> list[str]:
    groups: list[list[str]] = []
    topic_terms = _topic_search_terms({"name": topic_name, "query_seeds_json": []}, tag_context)[:6]
    for row in results[:12]:
        title = _clean_search_text(str(row.get("title") or ""))
        snippet = _clean_search_text(str(row.get("snippet") or ""))
        text = f"{title} {snippet}".strip()
        if not text or not INDUSTRY_CONTEXT_RE.search(text):
            continue
        companies = _followup_companies(text)
        tech_terms = _followup_technology_terms(text, topic_terms)
        row_queries: list[str] = []
        for company in companies[:2]:
            for term in tech_terms[:2] or topic_terms[:1]:
                row_queries.extend([
                    f'{year} "{company}" {term} deployment oil gas',
                    f'{year} "{company}" {term} contract operator oilfield',
                ])
        if not companies and tech_terms:
            row_queries.append(f"{year} {tech_terms[0]} customer deployment KPI oil gas")
        if _contains_cjk(text):
            cjk_terms = [term for term in [*tech_terms, *topic_terms] if _contains_cjk(term)]
            row_queries.append(f"{year} {' '.join(cjk_terms[:2]) or topic_name} 现场应用 油气 技术")
            row_queries.append(f"{year} {' '.join(cjk_terms[:2]) or topic_name} 中标 合作 油田")
        if row_queries:
            groups.append(_dedupe(row_queries))
    return _round_robin_queries(groups, limit=limit, seen=seen_queries)


def _followup_companies(text: str) -> list[str]:
    generic = {
        "AI", "HSE", "PTW", "LOTO", "SIMOPS", "LNG", "OIL", "GAS", "THE",
        "NEWS", "PRESS", "RELEASE", "OPERATOR", "FIELD", "TECHNOLOGY",
    }
    return [
        company
        for company in _extract_companies(text)
        if company.upper() not in generic and not re.fullmatch(r"20\d{2}", company)
    ][:4]


def _followup_technology_terms(text: str, topic_terms: list[str]) -> list[str]:
    text_l = text.lower()
    patterns = [
        "closed-loop drilling",
        "automated drilling",
        "autonomous drilling",
        "robotic inspection",
        "predictive maintenance",
        "digital permit to work",
        "hydraulic fracturing",
        "real-time monitoring",
        "pipeline pigging",
        "artificial lift",
        "carbon capture",
    ]
    terms = [term for term in patterns if term in text_l]
    for term in topic_terms:
        term_l = str(term or "").lower()
        if term_l and (term_l in text_l or _contains_cjk(term)):
            terms.append(term)
    if not terms:
        words = [
            word
            for word in re.findall(r"[a-zа-яё0-9\u3400-\u9fff]{4,}", text_l)
            if word not in _STOP_WORDS and not word.isdigit()
        ]
        terms.append(" ".join(words[:4]))
    return _dedupe([term for term in terms if term])[:4]


def _dedupe_search_results(results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped = []
    for item in results:
        url = _normalize_url_for_key(str(item.get("url") or ""))
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def _topic_seed_queries(topic: dict[str, Any], *, year: int, tag_context: dict[str, Any] | None = None) -> list[str]:
    topic_name = str(topic.get("name") or "").strip()
    description = str(topic.get("description") or "").strip()
    raw_seeds = topic.get("query_seeds_json")
    if raw_seeds is None:
        raw_seeds = topic.get("query_seeds") or []
    seeds = [str(item).strip() for item in raw_seeds or [] if str(item).strip()]
    if tag_context:
        seeds.extend(_tag_context_seed_terms(tag_context))
    queries = []
    for seed in seeds + [topic_name, description]:
        if not seed:
            continue
        queries.append(f"{year} {seed} news oil gas mining chemicals")
        if _contains_cjk(seed):
            queries.append(f"{year} {seed} 新闻 石油 天然气 石化 矿山")
    return _dedupe(queries)


def _planned_search_queries(
    topic: dict[str, Any],
    config: SignalDiscoveryConfig,
    tag_context: dict[str, Any],
    *,
    feedback_queries: list[str],
    seed_queries: list[str],
    generated_queries: list[str],
    year: int | None = None,
) -> list[str]:
    """Собрать лимит запросов так, чтобы один источник не съел весь прогон.

    Раньше список строился линейно: ОС -> сиды -> LLM, а затем обрезался. На малом лимите
    это убивало исследовательские углы и прогон снова сваливался в однотипные новости.
    """
    limit = max(1, int(config.web_query_limit or 1))
    head = _dedupe(feedback_queries[:1])
    remaining = max(0, limit - len(head))
    if remaining == 0:
        return head[:limit]
    angle_queries = _topic_angle_queries(topic, tag_context, year=_current_year() if year is None else year)
    tail = _round_robin_queries(
        [
            angle_queries,
            generated_queries,
            seed_queries,
            feedback_queries[1:2],
        ],
        limit=remaining,
        seen=head,
    )
    return _dedupe(head + tail)[:limit]


def _round_robin_queries(groups: list[list[str]], *, limit: int, seen: list[str] | None = None) -> list[str]:
    result: list[str] = []
    seen_keys = {_query_dedupe_key(value) for value in seen or [] if value}
    indexes = [0 for _ in groups]
    while len(result) < limit:
        progressed = False
        for group_index, group in enumerate(groups):
            while indexes[group_index] < len(group):
                value = re.sub(r"\s+", " ", str(group[indexes[group_index]] or "")).strip()
                indexes[group_index] += 1
                key = _query_dedupe_key(value)
                if not value or key in seen_keys:
                    continue
                seen_keys.add(key)
                result.append(value)
                progressed = True
                break
            if len(result) >= limit:
                break
        if not progressed:
            break
    return result


def _topic_angle_queries(topic: dict[str, Any], tag_context: dict[str, Any], *, year: int) -> list[str]:
    terms = _topic_search_terms(topic, tag_context)
    if not terms:
        return []
    queries = []
    for index, suffix in enumerate(_angle_suffixes_for_terms(terms)):
        term = terms[index % len(terms)]
        queries.append(f"{year} {term} {suffix}")
    return _dedupe(queries)


def _topic_search_terms(topic: dict[str, Any], tag_context: dict[str, Any]) -> list[str]:
    raw_seeds = topic.get("query_seeds_json")
    if raw_seeds is None:
        raw_seeds = topic.get("query_seeds") or []
    values = [
        *(tag_context.get("keywords_en") or [])[:8],
        *(str(item or "").strip() for item in (raw_seeds or [])[:8]),
        *(tag_context.get("keywords_ru") or [])[:6],
    ]
    for tag in (tag_context.get("tags") or [])[:6]:
        values.extend(str(tag.get(field) or "").strip() for field in ("name_en", "name"))
    values.extend([str(topic.get("name") or ""), str(topic.get("description") or "")])
    return _dedupe([_trim_query_term(value) for value in values if _trim_query_term(value)])[:12]


def _angle_suffixes_for_terms(terms: list[str]) -> list[str]:
    has_cjk = any(_contains_cjk(term) for term in terms)
    has_cyrillic = any(_contains_cyrillic(term) for term in terms)
    suffixes = [
        "field trial deployment customer oil gas",
        "contract partnership operator oilfield service",
        "commercial launch technology deployment upstream",
        "case study performance KPI industrial operation",
        "mining chemicals industrial transfer oil gas",
        "newsroom press release customer deployment",
    ]
    if has_cyrillic:
        suffixes.extend([
            "пилот внедрение заказчик нефтегаз",
            "контракт партнерство оператор нефтесервис",
        ])
    if has_cjk:
        suffixes.extend([
            "现场应用 油气 技术",
            "中标 合作 油田 技术",
            "客户 案例 石化 矿山",
        ])
    return suffixes


def _trim_query_term(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" ,.;:")
    if not text:
        return ""
    if len(text) <= 90:
        return text
    return " ".join(text.split()[:8]).strip(" ,.;:")


def _query_dedupe_key(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").lower()).strip()
    text = re.sub(r"\b(20[2-9][0-9]|news|новости|新闻)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _contains_cyrillic(text: str) -> bool:
    return bool(re.search(r"[а-яё]", text or "", re.IGNORECASE))


def _topic_tag_context(topic_name: str) -> dict[str, Any]:
    tags = _TAGS_SNAPSHOT.get()
    if tags is None:
        try:
            tags = repository.list_enabled_tags()
        except Exception:  # noqa: BLE001 - tag context is an enrichment, not a hard dependency for search
            tags = []
    selected = _select_topic_tags(topic_name, tags)
    return {
        "tags": selected,
        "keywords_ru": _dedupe(_flatten_tag_values(selected, "keywords_json"))[:20],
        "keywords_en": _dedupe(_flatten_tag_values(selected, "keywords_en_json"))[:20],
        "negative_keywords": _dedupe(_flatten_tag_values(selected, "negative_keywords_json"))[:30],
        "descriptions": _dedupe([str(row.get("description") or "").strip() for row in selected if row.get("description")])[:8],
    }


def _select_topic_tags(topic_name: str, tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not topic_name or not tags:
        return []
    topic_norm = _norm_match_text(topic_name)
    # Тема = тематика заказчика по имени — берём её и её подтеги, и только их. Иначе
    # пересечение по общим словам («оборудование», «промышленных») притягивало соседние
    # тематики, и «Добыча…» искала заодно ключами «Бурения…».
    exact = [tag for tag in tags if _norm_match_text(str(tag.get("name") or "")) == topic_norm]
    if exact:
        exact_ids = {int(tag["id"]) for tag in exact if tag.get("id") is not None}
        return [
            tag for tag in tags
            if (tag.get("id") is not None and int(tag["id"]) in exact_ids)
            or (tag.get("parent_id") is not None and int(tag["parent_id"]) in exact_ids)
        ][:24]
    direct_ids: set[int] = set()
    direct_parent_ids: set[int] = set()
    for tag in tags:
        if _tag_matches_topic(tag, topic_norm):
            tag_id = tag.get("id")
            parent_id = tag.get("parent_id")
            if tag_id is not None:
                direct_ids.add(int(tag_id))
            if parent_id is not None:
                direct_parent_ids.add(int(parent_id))

    selected = []
    for tag in tags:
        tag_id = tag.get("id")
        parent_id = tag.get("parent_id")
        include = False
        if tag_id is not None and int(tag_id) in direct_ids:
            include = True
        if tag_id is not None and int(tag_id) in direct_parent_ids:
            include = True
        if parent_id is not None and int(parent_id) in direct_ids:
            include = True
        if include:
            selected.append(tag)
    return selected[:24]


def _tag_matches_topic(tag: dict[str, Any], topic_norm: str) -> bool:
    fields = [
        tag.get("name"),
        tag.get("name_en"),
        tag.get("parent_name"),
        tag.get("parent_name_en"),
        tag.get("description"),
    ]
    for field in fields:
        field_norm = _norm_match_text(str(field or ""))
        if field_norm and (field_norm in topic_norm or topic_norm in field_norm):
            return True
        if field_norm and _meaningful_token_overlap(topic_norm, field_norm):
            return True
    for keyword in (tag.get("keywords_json") or []) + (tag.get("keywords_en_json") or []):
        keyword_norm = _norm_match_text(str(keyword or ""))
        if keyword_norm and (keyword_norm in topic_norm or _meaningful_token_overlap(topic_norm, keyword_norm)):
            return True
    return False


def _meaningful_token_overlap(left: str, right: str) -> bool:
    left_tokens = {token for token in re.findall(r"[a-zа-яё0-9]{4,}", left) if token not in {"and", "with", "news"}}
    right_tokens = {token for token in re.findall(r"[a-zа-яё0-9]{4,}", right) if token not in {"and", "with", "news"}}
    return bool(left_tokens & right_tokens)


def _norm_match_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def _flatten_tag_values(tags: list[dict[str, Any]], field: str) -> list[str]:
    values: list[str] = []
    for tag in tags:
        for value in tag.get(field) or []:
            text = str(value or "").strip()
            if text:
                values.append(text)
    return values


def _tag_context_seed_terms(tag_context: dict[str, Any]) -> list[str]:
    tags = tag_context.get("tags") or []
    names = []
    for tag in tags[:8]:
        for field in ("name_en", "name"):
            value = str(tag.get(field) or "").strip()
            if value:
                names.append(value)
    return _dedupe([
        *tag_context.get("keywords_en", [])[:10],
        *tag_context.get("keywords_ru", [])[:8],
        *names,
    ])[:18]


def _query_generation_topic(topic: dict[str, Any], tag_context: dict[str, Any]) -> str:
    topic_name = str(topic.get("name") or "").strip()
    description = str(topic.get("description") or "").strip()
    parts = [topic_name]
    if description:
        parts.append(f"description: {description}")
    if tag_context.get("keywords_en"):
        parts.append("english keywords: " + ", ".join(tag_context["keywords_en"][:12]))
    if tag_context.get("keywords_ru"):
        parts.append("russian keywords: " + ", ".join(tag_context["keywords_ru"][:10]))
    if tag_context.get("negative_keywords"):
        parts.append("avoid meanings: " + ", ".join(tag_context["negative_keywords"][:12]))
    return "\n".join(part for part in parts if part)


def _blocked_by_tag_negative_keywords(evidence: dict[str, Any], tag_context: dict[str, Any]) -> bool:
    negative_keywords = tag_context.get("negative_keywords") or []
    if not negative_keywords:
        return False
    text = _norm_match_text(" ".join(
        str(evidence.get(field) or "")
        for field in ("title", "title_ru", "extracted_fact", "summary_ru", "publisher")
    ))
    return any(_contains_negative_keyword(text, keyword) for keyword in negative_keywords)


def _contains_negative_keyword(text: str, keyword: str) -> bool:
    keyword_norm = _norm_match_text(keyword)
    if not keyword_norm:
        return False
    if re.search(r"[\u3400-\u9fff]", keyword_norm):
        return keyword_norm in text
    if re.fullmatch(r"[a-zа-яё0-9 ]+", keyword_norm):
        pattern = r"(?<![a-zа-яё0-9])" + re.escape(keyword_norm) + r"(?![a-zа-яё0-9])"
        return bool(re.search(pattern, text))
    return keyword_norm in text


def _tag_context_snapshot(tag_context: dict[str, Any]) -> dict[str, Any]:
    tags = tag_context.get("tags") or []
    return {
        "tag_names": [row.get("name") for row in tags[:12] if row.get("name")],
        "keywords_ru": tag_context.get("keywords_ru", [])[:12],
        "keywords_en": tag_context.get("keywords_en", [])[:12],
        "negative_keywords": tag_context.get("negative_keywords", [])[:12],
    }


def _search_result_to_evidence(row: dict[str, Any], topic: str) -> dict[str, Any] | None:
    url = str(row.get("url") or "").strip()
    title = _clean_search_text(str(row.get("title") or ""))
    snippet = _clean_search_text(str(row.get("snippet") or ""))
    if not url or not title:
        return None
    context = {
        "title": title,
        "raw_text": f"{title}\n{snippet}",
        "language": "mixed",
    }
    return {
        "article_id": None,
        "source_url": url,
        "title": title,
        "title_ru": _enforce_glossary(title, context, topic),
        "publisher": repository.normalize_domain(url) or row.get("provider"),
        "published_at": None,
        "evidence_type": _evidence_type(title + " " + snippet),
        "extracted_fact": snippet or title,
        "summary_ru": _enforce_glossary(snippet or title, context, topic),
        "strength": 0.72,
        "topic": topic,
        "raw_payload": {
            "query": row.get("query"),
            "provider": row.get("provider"),
            "snippet": snippet,
            "evidence_source": "web_search",
        },
    }


WEB_EVIDENCE_FULLTEXT_CHARS = 1500
# Документы, а не страницы: лишний трафик, а текст из байтов PDF ушёл бы судье мусором.
_FULLTEXT_SKIP_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip")
_BINARY_PREFIXES = (b"%PDF", b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"\x89PNG", b"\xff\xd8\xff", b"GIF8")


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _looks_binary(content: bytes | str) -> bool:
    head = content[:1024] if isinstance(content, bytes) else content[:1024].encode("utf-8", "ignore")
    return head.lstrip().startswith(_BINARY_PREFIXES) or b"\x00" in head


def _fetch_full_text(url: str, fallback_title: str = "", *, timeout: int | None = None) -> dict[str, Any]:
    """Открыть страницу и достать настоящий текст статьи вместо сниппета поиска.

    Сниппет — это 1-2 обрубленных предложения от поисковика; по нему ни кластеризация,
    ни судья не видят ни контракта, ни цифр, ни даты события. Переиспользуем ту же пару
    probe_url + parse_article_page, что и source_discovery (включая RU/external-роутинг
    через прокси внутри probe_url), поэтому вынесено в отдельную функцию — тесты
    подменяют её целиком, не трогая сеть.

    Таймаут короче обычного (SIGNAL_DISCOVERY_FULLTEXT_TIMEOUT_SECONDS): страница —
    улучшение, а не условие прогона, и 20 с × до 20 страниц × 13 тем не должны жечь аренду.
    """
    from oiltech_digest.ingestion import request_parser
    from oiltech_digest.ingestion.source_diagnostics import probe_url

    empty = {"ok": False, "raw_text": "", "published_at": None, "title": ""}
    if urlsplit(url).path.lower().endswith(_FULLTEXT_SKIP_SUFFIXES):
        return {**empty, "error": "not_html"}
    probe, content = probe_url(url, timeout=timeout or app_config.SIGNAL_DISCOVERY_FULLTEXT_TIMEOUT_SECONDS)
    if content is None:
        return {**empty, "error": probe.error or f"http_{probe.status}"}
    if _looks_binary(content):
        return {**empty, "error": "not_html"}
    title, published_at, raw_text = request_parser.parse_article_page(content, fallback_title)
    return {
        "ok": True,
        "error": None,
        "raw_text": raw_text or "",
        "published_at": published_at,
        "title": title or "",
    }


def _enrich_web_evidence_with_full_text(
    evidence: list[dict[str, Any]],
    topic: str,
    *,
    limit: int,
    heartbeat: Callable[[], None] | None = None,
    budget_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Заменить сниппет реальным текстом статьи там, где это удаётся и оправдано.

    Раньше evidence из web-поиска несло только заголовок и обрывок сниппета — судья и
    кластеризация не видели ни контракта, ни KPI, ни настоящей даты события, только то,
    что уместилось в две строки выдачи. Лимит и мягкий фоллбэк на неудаче нужны, чтобы
    не превращать один прогон в сотню HTTP-запросов и не терять кандидата, если страница
    недоступна или защищена антиботом — тогда карточка остаётся такой же, как раньше.

    Любая ошибка страницы — откат на сниппет, а не падение темы: 21.09 пустое тело (200)
    роняло lxml ParserError сквозь весь прогон. Перед каждой страницей — heartbeat, а по
    исчерпании бюджета времени на тему остальные кандидаты остаются со сниппетом.
    """
    beat = heartbeat or (lambda: None)
    budget = app_config.SIGNAL_DISCOVERY_FULLTEXT_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    stats = {"attempted": 0, "fetched": 0, "too_short": 0, "failed": 0, "skipped_budget": 0}
    if limit <= 0:
        return evidence, stats

    started = clock()
    enriched: list[dict[str, Any]] = []
    for item in evidence:
        url = str(item.get("source_url") or "")
        if not url or stats["attempted"] + stats["skipped_budget"] >= limit:
            enriched.append(item)
            continue
        if budget > 0 and clock() - started >= budget:
            stats["skipped_budget"] += 1
            enriched.append(item)
            continue
        beat()
        stats["attempted"] += 1
        try:
            fetched = _fetch_full_text(url, str(item.get("title") or ""))
        except Exception as exc:  # noqa: BLE001 - одна страница не роняет тему
            fetched = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}", "raw_text": ""}
        raw_text = _clean_search_text(fetched.get("raw_text") or "")
        if not fetched.get("ok") or len(raw_text) < app_config.MIN_ARTICLE_TEXT_CHARS:
            stats["failed" if not fetched.get("ok") else "too_short"] += 1
            enriched.append({
                **item,
                "raw_payload": {
                    **(item.get("raw_payload") or {}),
                    "full_text_fetched": False,
                    "full_text_error": fetched.get("error") or "too_short",
                },
            })
            continue

        stats["fetched"] += 1
        title = fetched.get("title") or item.get("title") or ""
        fact = raw_text[:WEB_EVIDENCE_FULLTEXT_CHARS]
        context = {"title": title, "raw_text": f"{title}\n{fact}", "language": "mixed"}
        enriched.append({
            **item,
            "title": title,
            "title_ru": _enforce_glossary(title, context, topic),
            # Строкой ISO, как все даты снимка: объект datetime ронял отправку итога
            # воркера ядру (проверка 4712, 21.09) — тот же класс, что сбор 18.09.
            "published_at": _iso(fetched.get("published_at")) or item.get("published_at"),
            "evidence_type": _evidence_type(f"{title} {fact}"),
            "extracted_fact": fact,
            "summary_ru": _enforce_glossary(fact, context, topic),
            "raw_payload": {
                **(item.get("raw_payload") or {}),
                "full_text_fetched": True,
                "full_text_chars": len(raw_text),
            },
        })
    return enriched, stats


def _has_industry_context(row: dict[str, Any]) -> bool:
    text = " ".join(
        str(row.get(key) or "")
        for key in (
            "title",
            "title_ru",
            "raw_text",
            "summary",
            "extracted_fact",
            "summary_ru",
            "tag_name",
            "tag_name_en",
            "publisher",
        )
    )
    return bool(INDUSTRY_CONTEXT_RE.search(text))


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _clean_search_text(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    cleaned = cleaned.replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        normalized = re.sub(r"\s+", " ", value).strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _dedupe_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for item in evidence:
        url = _normalize_url_for_key(str(item.get("source_url") or ""))
        text_key = _fact_fingerprint(str(item.get("title") or "") + " " + str(item.get("extracted_fact") or ""))
        key = f"url:{url}" if url else f"text:{text_key}"
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _cluster_evidence(evidence: list[dict[str, Any]], topic: str) -> list[list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in evidence:
        key = _cluster_key(item, topic)
        buckets.setdefault(key, []).append(item)
    clusters = sorted(
        buckets.values(),
        key=lambda items: (len(items), sum(float(i.get("strength") or 0) for i in items)),
        reverse=True,
    )
    return clusters


def _clusters_for_judging(clusters: list[list[dict[str, Any]]], limit: int) -> list[list[dict[str, Any]]]:
    if limit <= 0:
        return []
    buckets: dict[str, list[list[dict[str, Any]]]] = {}
    for cluster in clusters:
        buckets.setdefault(_cluster_family(cluster), []).append(cluster)
    families = sorted(
        buckets.values(),
        key=lambda rows: _cluster_rank(rows[0]) if rows else (0, 0),
        reverse=True,
    )
    selected: list[list[dict[str, Any]]] = []
    while len(selected) < limit:
        progressed = False
        for family in families:
            if not family:
                continue
            selected.append(family.pop(0))
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


def _cluster_rank(cluster: list[dict[str, Any]]) -> tuple[int, float]:
    return (len(cluster), sum(float(item.get("strength") or 0) for item in cluster))


def _cluster_family(cluster: list[dict[str, Any]]) -> str:
    text = " ".join(f"{item.get('title') or ''} {item.get('extracted_fact') or ''}" for item in cluster).lower()
    if re.search(r"contract|partnership|supplier|контракт|поставщик|中标|合作", text):
        return "commercial"
    if re.search(r"deploy|implemented|field trial|pilot|внедр|пилот|现场应用", text):
        return "deployment"
    if re.search(r"\b\d+[%x]?\b|kpi|performance|faster|reduce|сниз|ускор|提高", text):
        return "performance"
    if re.search(r"launch|release|commercialized|запуст|推出|发布", text):
        return "product"
    return "other"


def _cluster_key(evidence: dict[str, Any], topic: str) -> str:
    text = f"{evidence.get('title') or ''} {evidence.get('extracted_fact') or ''}".lower()
    patterns = [
        ("physical-ai-robotics", r"robot|robotic|autonomous|physical ai|drill floor|inspection"),
        ("predictive-hse", r"predict|preventive|condition|corrosion|maintenance|anomaly"),
        ("digital-ptw", r"permit|ptw|control of work|loto|isolation|simops|handover"),
        ("transport-safety", r"fleet|telematics|fatigue|collision|driver|vehicle|proximity"),
        ("computer-vision", r"computer vision|video|camera|ppe|danger zone|unsafe"),
    ]
    for key, pattern in patterns:
        if re.search(pattern, text):
            return f"{key}-{_fact_fingerprint(text)}"
    words = [w for w in re.findall(r"[a-zа-яё0-9]{4,}", text) if w not in _STOP_WORDS]
    return "-".join(words[:3]) or _slug(topic)


def _offline_signal_judgement(evidence: list[dict[str, Any]], topic: str) -> dict[str, Any]:
    best = max(evidence, key=lambda item: float(item.get("strength") or 0))
    text = " ".join([str(item.get("title") or "") + " " + str(item.get("extracted_fact") or "") for item in evidence])
    score = _offline_signal_score(evidence, text)
    maturity = "reject"
    if score >= 88 and len(evidence) >= 3:
        maturity = "proven"
    elif score >= 82 and len(evidence) >= 2:
        maturity = "shortlist"
    elif score >= 62:
        maturity = "watch"
    companies = _extract_companies(text)
    industries = _industries_for_topic(topic, text)
    context = _glossary_context(evidence, topic)
    return _normalize_signal_payload(
        {
            "title": _signal_title(best, topic),
            "title_ru": best.get("title_ru") or _signal_title(best, topic),
            "theme": topic,
            "summary": _trim(str(best.get("summary_ru") or best.get("extracted_fact") or best.get("title") or ""), 360),
            "thesis": _trim(str(best.get("extracted_fact") or best.get("title") or ""), 420),
            "transferability": _transferability_for_topic(topic),
            "maturity": maturity,
            "confidence": min(0.95, max(0.2, score / 100)),
            "score": score,
            "why_now": f"Найдено evidence за последние циклы: {len(evidence)}; лучший материал: {best.get('publisher') or 'источник не указан'}.",
            "why_not_noise": _why_not_noise(text),
            "companies": companies,
            "industries": industries,
        },
        topic,
        context=context,
    )


def _offline_signal_score(evidence: list[dict[str, Any]], text: str) -> float:
    text_l = text.lower()
    score = 35 + min(20, len(evidence) * 5)
    strong_markers = [
        "deploy", "deployment", "implemented", "supplier", "contract", "partnership",
        "autonomous", "predictive", "hazard", "safety", "robot", "permit", "loto",
        "внедр", "контракт", "поставщик", "автоном", "безопас", "робот", "прогноз",
    ]
    score += min(30, sum(4 for marker in strong_markers if marker in text_l))
    if re.search(r"\b\d+[%x]?\b", text_l):
        score += 8
    if any(float(item.get("strength") or 0) >= 0.75 for item in evidence):
        score += 7
    if re.search(r"marketing|webinar|guide|buyer.?s guide|opinion", text_l):
        score -= 12
    return round(max(0, min(100, score)), 2)


def _normalize_signal_payload(payload: dict[str, Any], topic: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    maturity = str(payload.get("maturity") or "watch").lower()
    if maturity not in {"reject", "watch", "shortlist", "proven"}:
        maturity = "watch"
    context = context or {"title": topic, "raw_text": " ".join(str(payload.get(key) or "") for key in ("title", "summary", "thesis"))}
    title = _trim(str(payload.get("title") or topic), 220)
    title_ru = _trim(str(payload.get("title_ru") or title), 220)
    summary = _trim(str(payload.get("summary") or payload.get("thesis") or ""), 600)
    score = _normalize_score(payload.get("score"))
    return {
        "title": title,
        "title_ru": _enforce_glossary(title_ru, context, topic),
        "theme": _normalize_theme(str(payload.get("theme") or topic), topic),
        "summary": _enforce_glossary(summary, context, topic),
        "thesis": _enforce_glossary(_trim(str(payload.get("thesis") or ""), 1200), context, topic),
        "transferability": _enforce_glossary(_trim(str(payload.get("transferability") or ""), 800), context, topic),
        "maturity": maturity,
        "confidence": float(payload.get("confidence") or 0),
        "score": score,
        "why_now": _enforce_glossary(_trim(str(payload.get("why_now") or ""), 800), context, topic),
        "why_not_noise": _enforce_glossary(_trim(str(payload.get("why_not_noise") or ""), 800), context, topic),
        "companies": [str(x).strip() for x in payload.get("companies") or [] if str(x).strip()][:10],
        "industries": [str(x).strip() for x in payload.get("industries") or [] if str(x).strip()][:10],
    }


def _judge_prompt(evidence: list[dict[str, Any]], topic: str) -> str:
    rows = []
    for index, item in enumerate(evidence[:8], start=1):
        rows.append(
            "\n".join(
                part
                for part in [
                    f"evidence #{index}",
                    f"title: {item.get('title')}",
                    f"publisher: {item.get('publisher')}",
                    f"url: {item.get('source_url')}",
                    f"type: {item.get('evidence_type')}",
                    # Раньше судья не видел дату события вообще и мог опираться только на
                    # упоминания года в тексте. С полным текстом страницы published_at
                    # часто реальный — даём его в явном виде для правила «событие старое».
                    f"published_at: {item.get('published_at')}" if item.get("published_at") else None,
                    f"fact: {item.get('extracted_fact')}",
                    f"summary_ru: {item.get('summary_ru')}",
                ]
                if part is not None
            )
        )
    glossary = glossary_prompt_block(_glossary_context(evidence, topic))
    feedback = feedback_prompt_block(topic)
    glossary_section = "\n\n".join(item for item in (glossary, feedback) if item)
    glossary_section = f"\n\n{glossary_section}" if glossary_section else ""
    return f"topic: {topic}{glossary_section}\n\n" + "\n\n".join(rows)


def _training_input_payload(
    topic: str,
    evidence: list[dict[str, Any]],
    web_search: dict[str, Any] | None,
    *,
    offline: bool,
) -> dict[str, Any]:
    return {
        "topic": topic,
        "offline": offline,
        "prompt": _judge_prompt(evidence, topic),
        "web_search": {
            "status": (web_search or {}).get("status"),
            "provider": (web_search or {}).get("provider"),
            "queries": (web_search or {}).get("queries") or [],
            "initial_queries": (web_search or {}).get("initial_queries") or [],
            "research_rounds": (web_search or {}).get("research_rounds") or [],
            "fulltext": (web_search or {}).get("fulltext"),
            "reason": (web_search or {}).get("reason"),
        } if web_search is not None else None,
        "evidence": [
            {
                "article_id": item.get("article_id"),
                "source_url": item.get("source_url"),
                "title": item.get("title"),
                "title_ru": item.get("title_ru"),
                "publisher": item.get("publisher"),
                "published_at": item.get("published_at"),
                "evidence_type": item.get("evidence_type"),
                "extracted_fact": item.get("extracted_fact"),
                "summary_ru": item.get("summary_ru"),
                "strength": item.get("strength"),
                "raw_payload": item.get("raw_payload"),
            }
            for item in evidence
        ],
    }


def _enforce_glossary(text: str, context: dict[str, Any], topic: str | None = None) -> str:
    return apply_feedback_glossary(enforce_glossary_text(text, context), topic)


def _glossary_context(evidence: list[dict[str, Any]], topic: str) -> dict[str, Any]:
    return {
        "title": topic,
        "raw_text": " ".join(
            str(item.get(key) or "")
            for item in evidence
            for key in ("title", "title_ru", "extracted_fact", "summary_ru")
        ),
        "language": "mixed",
    }


def _normalize_score(value: Any) -> float:
    try:
        score = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if 0 < score <= 1:
        score *= 100
    return round(max(0.0, min(100.0, score)), 2)


def _normalize_theme(theme: str, topic: str) -> str:
    theme = _trim(theme.replace("ХSE", "HSE").replace("Bur務", "бурение"), 120)
    topic = _trim(topic, 120)
    has_topic_cyrillic = bool(re.search(r"[а-яё]", topic, re.IGNORECASE))
    latin = len(re.findall(r"[a-z]", theme, re.IGNORECASE))
    cyrillic = len(re.findall(r"[а-яё]", theme, re.IGNORECASE))
    if re.search(r"[\u3400-\u9fff]", theme):
        return topic or theme
    if has_topic_cyrillic and latin > max(8, cyrillic * 2):
        return topic or theme
    return theme or topic


def _is_rejected_signal(signal: dict[str, Any]) -> bool:
    title = str(signal.get("title") or "").strip().lower()
    if signal.get("maturity") == "reject" or title in {"reject", "отклонить", "отклонено"}:
        return True
    text = " ".join(
        str(signal.get(key) or "").lower()
        for key in ("title", "summary", "thesis", "why_not_noise", "transferability")
    )
    rejection_markers = [
        "нет конкретного",
        "нет явного",
        "нет подтвержд",
        "нет доказ",
        "не является конкрет",
        "without confirmed",
        "no confirmed",
    ]
    return _normalize_score(signal.get("score")) < 60 and any(marker in text for marker in rejection_markers)


def _signal_key(signal: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    canonical_urls = sorted(
        {
            _normalize_url_for_key(str(item.get("source_url") or ""))
            for item in evidence
            if item.get("source_url")
        }
    )
    if len(canonical_urls) == 1:
        seed = f"url:{canonical_urls[0]}"
    else:
        best = max(evidence, key=lambda item: float(item.get("strength") or 0), default={})
        seed = "fact:" + _fact_fingerprint(
            " ".join(
                [
                    str(signal.get("title") or ""),
                    str(best.get("title") or ""),
                    str(best.get("extracted_fact") or ""),
                    " ".join(str(company) for company in signal.get("companies") or []),
                ]
            )
        )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _normalize_url_for_key(url: str) -> str:
    url = (url or "").strip().lower()
    if not url:
        return ""
    url = re.sub(r"^https?://", "", url)
    url = url.split("#", 1)[0].split("?", 1)[0]
    url = re.sub(r"/+$", "", url)
    return url.removeprefix("www.")


def _fact_fingerprint(text: str) -> str:
    words = [
        word
        for word in re.findall(r"[a-zа-яё0-9\u3400-\u9fff]{3,}", text.lower())
        if word not in _STOP_WORDS and not word.isdigit()
    ]
    return "-".join(words[:10]) or "signal"


def _signal_title(evidence: dict[str, Any], topic: str) -> str:
    title = str(evidence.get("title") or topic).strip()
    return title if len(title) <= 160 else title[:157].rstrip() + "..."


def _evidence_type(text: str) -> str:
    text_l = text.lower()
    if re.search(r"deploy|implemented|внедр|запуст", text_l):
        return "deployment"
    if re.search(r"supplier|contract|поставщик|контракт", text_l):
        return "supplier"
    if re.search(r"guide|opinion|podcast|колон", text_l):
        return "analysis"
    return "article"


def _extract_companies(text: str) -> list[str]:
    candidates = re.findall(r"\b[A-Z][A-Za-z0-9&.-]{2,}(?:\s+[A-Z][A-Za-z0-9&.-]{2,}){0,2}\b", text)
    seen = []
    for item in candidates:
        if item.upper() in {"AI", "HSE", "PTW", "LOTO", "SIMOPS", "RSS"}:
            continue
        if item not in seen:
            seen.append(item)
    return seen[:8]


def _industries_for_topic(topic: str, text: str) -> list[str]:
    text_l = (topic + " " + text).lower()
    mapping = [
        ("oil and gas", r"oil|gas|drill|нефт|газ|бур"),
        ("mining", r"mining|mine|карьер|горн"),
        ("chemicals", r"chemical|хими"),
        ("industrial logistics", r"fleet|transport|logistics|vehicle|транспорт"),
        ("metals", r"metal|steel|металл"),
    ]
    found = [name for name, pattern in mapping if re.search(pattern, text_l)]
    return found or ["heavy industry"]


def _transferability_for_topic(topic: str) -> str:
    topic_l = topic.lower()
    if "ptw" in topic_l or "control of work" in topic_l:
        return "Переносимо в буровые, ремонты, SIMOPS и подрядные работы через связку нарядов, изоляций, допуска людей и фактического состояния барьеров."
    if "predictive" in topic_l:
        return "Переносимо в HSE и эксплуатацию как раннее предупреждение опасного состояния оборудования, среды или процесса до инцидента."
    if "robot" in topic_l or "physical" in topic_l:
        return "Переносимо в инспекции, обходы, буровую площадку и опасные операции, где главный эффект - снижение присутствия человека в hazardous zones."
    return "Переносимость требует проверки по нефтесервисному сценарию, но тема относится к промышленной безопасности и операционной эффективности."


def _why_not_noise(text: str) -> str:
    if re.search(r"deploy|implemented|supplier|contract|внедр|контракт|поставщик", text.lower()):
        return "Есть признаки применения или коммерческого допуска, а не только общий маркетинговый тезис."
    return "Сигнал оставлен в watchlist: тема переносима, но нужны подтверждения внедрения или измеримого эффекта."


def _trim(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


def _slug(value: str) -> str:
    return "-".join(re.findall(r"[a-z0-9а-яё]+", value.lower()))[:80] or "signal"


_STOP_WORDS = {
    "the", "and", "for", "with", "from", "this", "that", "into", "about",
    "как", "или", "для", "что", "это", "при", "над", "под", "после",
}
