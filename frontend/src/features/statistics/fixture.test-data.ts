// Снимок ответа /api/analytics/monthly с прода 19.09.2026 (урезан) — для тестов экрана.
// Затраты на ИИ (ai_cost) — СИНТЕТИЧЕСКИЕ: репозиторий публичный, а реальные расходы —
// коммерческая сторона (на экране их видит только админ). Курсы — настоящие курсы ЦБ.
import type { MonthlyAnalytics } from "../../api/types";

export const analyticsFixture: MonthlyAnalytics = {
  "timezone": "Europe/Moscow",
  "today": "2026-09-19",
  "current_month": "2026-09",
  "current_day": 19,
  "months": [
    {
      "month": "2026-05",
      "collected": 520,
      "en": 298,
      "full_text": 158,
      "relevant": 466,
      "rejected": 54,
      "summarized": 466,
      "scored": 518,
      "strong": 243,
      "top": 165,
      "hidden": 92,
      "reprints": 0,
      "digest_selected": 26,
      "sources_active": 51,
      "sources_relevant": 49,
      "sources_strong": 41,
      "speed_p50_hours": 193.32,
      "speed_p90_hours": 445.49,
      "digest_exports": 0,
      "complete": true
    },
    {
      "month": "2026-06",
      "collected": 4226,
      "en": 1229,
      "full_text": 2049,
      "relevant": 2457,
      "rejected": 1769,
      "summarized": 2457,
      "scored": 2556,
      "strong": 627,
      "top": 389,
      "hidden": 1890,
      "reprints": 0,
      "digest_selected": 19,
      "sources_active": 85,
      "sources_relevant": 55,
      "sources_strong": 43,
      "speed_p50_hours": 5.26,
      "speed_p90_hours": 479.6,
      "digest_exports": 0,
      "complete": true
    },
    {
      "month": "2026-07",
      "collected": 12894,
      "en": 2027,
      "full_text": 7099,
      "relevant": 4782,
      "rejected": 8112,
      "summarized": 4782,
      "scored": 4800,
      "strong": 428,
      "top": 228,
      "hidden": 6140,
      "reprints": 0,
      "digest_selected": 5,
      "sources_active": 82,
      "sources_relevant": 57,
      "sources_strong": 37,
      "speed_p50_hours": 3.52,
      "speed_p90_hours": 308.07,
      "digest_exports": 0,
      "complete": true
    },
    {
      "month": "2026-08",
      "collected": 8982,
      "en": 1666,
      "full_text": 5119,
      "relevant": 2604,
      "rejected": 6378,
      "summarized": 2604,
      "scored": 2634,
      "strong": 445,
      "top": 278,
      "hidden": 290,
      "reprints": 54,
      "digest_selected": 9,
      "sources_active": 77,
      "sources_relevant": 60,
      "sources_strong": 48,
      "speed_p50_hours": 0.72,
      "speed_p90_hours": 9.25,
      "digest_exports": 1,
      "complete": true
    },
    {
      "month": "2026-09",
      "collected": 4728,
      "en": 1365,
      "full_text": 2013,
      "relevant": 2043,
      "rejected": 2685,
      "summarized": 2043,
      "scored": 2059,
      "strong": 756,
      "top": 495,
      "hidden": 119,
      "reprints": 133,
      "digest_selected": 0,
      "sources_active": 132,
      "sources_relevant": 112,
      "sources_strong": 92,
      "speed_p50_hours": 1.0,
      "speed_p90_hours": 158.41,
      "digest_exports": 1,
      "complete": false
    }
  ],
  "previous_same_period": {
    "month": "2026-08",
    "collected": 6048,
    "en": 1027,
    "full_text": 3639,
    "relevant": 1678,
    "rejected": 4370,
    "summarized": 1678,
    "scored": 1696,
    "strong": 278,
    "top": 168,
    "hidden": 175,
    "reprints": 12,
    "digest_selected": 9,
    "sources_active": 65,
    "sources_relevant": 48,
    "sources_strong": 32,
    "speed_p50_hours": 0.6953582865277779,
    "speed_p90_hours": 5.036856007638889,
    "days": 19
  },
  "themes": [
    {
      "month": "2026-08",
      "tag_id": 31,
      "tag": "Рынок, бизнес-модели, сервисные модели, партнёрства и M&A",
      "relevant": 102,
      "strong": 19
    },
    {
      "month": "2026-08",
      "tag_id": 27,
      "tag": "Энергетика и промышленные энергосистемы",
      "relevant": 59,
      "strong": 11
    },
    {
      "month": "2026-08",
      "tag_id": 25,
      "tag": "Промысловая инфраструктура, трубопроводы, диагностика и целостность",
      "relevant": 41,
      "strong": 12
    },
    {
      "month": "2026-08",
      "tag_id": 29,
      "tag": "Логистика, транспорт и supply chain промышленных операций",
      "relevant": 38,
      "strong": 15
    },
    {
      "month": "2026-08",
      "tag_id": 24,
      "tag": "Добыча, механизированный фонд и внутрискважинное оборудование",
      "relevant": 18,
      "strong": 12
    },
    {
      "month": "2026-08",
      "tag_id": 28,
      "tag": "HSE, промышленная безопасность, охрана труда и экология",
      "relevant": 15,
      "strong": 6
    },
    {
      "month": "2026-08",
      "tag_id": 32,
      "tag": "Не классифицировано / новая тема",
      "relevant": 13,
      "strong": 1
    },
    {
      "month": "2026-08",
      "tag_id": 30,
      "tag": "Химия, материалы, вода и извлечение ценных компонентов",
      "relevant": 12,
      "strong": 8
    },
    {
      "month": "2026-08",
      "tag_id": 26,
      "tag": "Автоматизация, цифровизация, промышленный AI и автономные операции",
      "relevant": 11,
      "strong": 8
    },
    {
      "month": "2026-08",
      "tag_id": 19,
      "tag": "Геологоразведка, сейсморазведка, геофизика и изучение недр",
      "relevant": 11,
      "strong": 5
    },
    {
      "month": "2026-08",
      "tag_id": 22,
      "tag": "ГРП, стимуляция, интенсификация и повышение нефтеотдачи",
      "relevant": 6,
      "strong": 6
    },
    {
      "month": "2026-08",
      "tag_id": 20,
      "tag": "Бурение, направленное бурение, буровые растворы и буровое оборудование",
      "relevant": 5,
      "strong": 5
    },
    {
      "month": "2026-09",
      "tag_id": 31,
      "tag": "Рынок, бизнес-модели, сервисные модели, партнёрства и M&A",
      "relevant": 382,
      "strong": 130
    },
    {
      "month": "2026-09",
      "tag_id": 25,
      "tag": "Промысловая инфраструктура, трубопроводы, диагностика и целостность",
      "relevant": 179,
      "strong": 84
    },
    {
      "month": "2026-09",
      "tag_id": 27,
      "tag": "Энергетика и промышленные энергосистемы",
      "relevant": 134,
      "strong": 49
    },
    {
      "month": "2026-09",
      "tag_id": 26,
      "tag": "Автоматизация, цифровизация, промышленный AI и автономные операции",
      "relevant": 91,
      "strong": 74
    },
    {
      "month": "2026-09",
      "tag_id": 32,
      "tag": "Не классифицировано / новая тема",
      "relevant": 56,
      "strong": 11
    },
    {
      "month": "2026-09",
      "tag_id": 29,
      "tag": "Логистика, транспорт и supply chain промышленных операций",
      "relevant": 54,
      "strong": 22
    },
    {
      "month": "2026-09",
      "tag_id": 20,
      "tag": "Бурение, направленное бурение, буровые растворы и буровое оборудование",
      "relevant": 43,
      "strong": 33
    },
    {
      "month": "2026-09",
      "tag_id": 30,
      "tag": "Химия, материалы, вода и извлечение ценных компонентов",
      "relevant": 39,
      "strong": 23
    },
    {
      "month": "2026-09",
      "tag_id": 19,
      "tag": "Геологоразведка, сейсморазведка, геофизика и изучение недр",
      "relevant": 35,
      "strong": 20
    },
    {
      "month": "2026-09",
      "tag_id": 24,
      "tag": "Добыча, механизированный фонд и внутрискважинное оборудование",
      "relevant": 30,
      "strong": 21
    },
    {
      "month": "2026-09",
      "tag_id": 28,
      "tag": "HSE, промышленная безопасность, охрана труда и экология",
      "relevant": 21,
      "strong": 11
    },
    {
      "month": "2026-09",
      "tag_id": 21,
      "tag": "Заканчивание, цементирование и изоляция скважин",
      "relevant": 19,
      "strong": 19
    },
    {
      "month": "2026-09",
      "tag_id": 23,
      "tag": "КРС, ТКРС, well intervention и внутрискважинные работы",
      "relevant": 11,
      "strong": 9
    },
    {
      "month": "2026-09",
      "tag_id": 22,
      "tag": "ГРП, стимуляция, интенсификация и повышение нефтеотдачи",
      "relevant": 8,
      "strong": 7
    }
  ],
  "top_sources": [
    {
      "month": "2026-08",
      "source_id": 36,
      "source": "Neftegaz.ru",
      "strong": 128,
      "relevant": 509,
      "collected": 665
    },
    {
      "month": "2026-08",
      "source_id": 3,
      "source": "World Oil",
      "strong": 55,
      "relevant": 148,
      "collected": 166
    },
    {
      "month": "2026-08",
      "source_id": 38,
      "source": "EnergyLand",
      "strong": 53,
      "relevant": 155,
      "collected": 981
    },
    {
      "month": "2026-08",
      "source_id": 14,
      "source": "Oilfield Technology",
      "strong": 23,
      "relevant": 33,
      "collected": 38
    },
    {
      "month": "2026-08",
      "source_id": 11,
      "source": "Hydrocarbon Processing",
      "strong": 19,
      "relevant": 129,
      "collected": 175
    },
    {
      "month": "2026-08",
      "source_id": 12,
      "source": "Pipeline & Gas Journal",
      "strong": 14,
      "relevant": 79,
      "collected": 99
    },
    {
      "month": "2026-08",
      "source_id": 37,
      "source": "OilCapital",
      "strong": 13,
      "relevant": 273,
      "collected": 339
    },
    {
      "month": "2026-08",
      "source_id": 104,
      "source": "Агентство нефтегазовой информации",
      "strong": 11,
      "relevant": 34,
      "collected": 38
    },
    {
      "month": "2026-08",
      "source_id": 13,
      "source": "LNG Industry",
      "strong": 10,
      "relevant": 59,
      "collected": 63
    },
    {
      "month": "2026-08",
      "source_id": 8,
      "source": "Rigzone",
      "strong": 9,
      "relevant": 101,
      "collected": 120
    },
    {
      "month": "2026-09",
      "source_id": 36,
      "source": "Neftegaz.ru",
      "strong": 116,
      "relevant": 365,
      "collected": 459
    },
    {
      "month": "2026-09",
      "source_id": 38,
      "source": "EnergyLand",
      "strong": 58,
      "relevant": 127,
      "collected": 666
    }
  ],
  "sources_enabled": 120,
  "targets": {
    "sources": 120,
    "articles_month": 5000,
    "ai_rub_month": 10000.0
  },
  "ai_cost": [
    {
      "month": "2026-05",
      "calls": 900,
      "articles": 300,
      "cost_usd": 1.0,
      "usd_rub": 71.0224,
      "usd_rub_date": "2026-05-30",
      "usd_rub_source": "ЦБ РФ"
    },
    {
      "month": "2026-06",
      "calls": 9000,
      "articles": 3000,
      "cost_usd": 50.0,
      "usd_rub": 77.7539,
      "usd_rub_date": "2026-06-30",
      "usd_rub_source": "ЦБ РФ"
    },
    {
      "month": "2026-07",
      "calls": 27000,
      "articles": 9000,
      "cost_usd": 100.0,
      "usd_rub": 79.8573,
      "usd_rub_date": "2026-07-31",
      "usd_rub_source": "ЦБ РФ"
    },
    {
      "month": "2026-08",
      "calls": 18000,
      "articles": 6000,
      "cost_usd": 40.0,
      "usd_rub": 85.6007,
      "usd_rub_date": "2026-08-29",
      "usd_rub_source": "ЦБ РФ"
    },
    {
      "month": "2026-09",
      "calls": 12000,
      "articles": 4000,
      "cost_usd": 30.0,
      "usd_rub": 84.1975,
      "usd_rub_date": "2026-09-19",
      "usd_rub_source": "ЦБ РФ"
    }
  ],
  "ai_cost_previous_same_period": {
    "month": "2026-08",
    "days": 19,
    "calls": 10500,
    "articles": 3500,
    "cost_usd": 25.0,
    "usd_rub": 85.1645,
    "usd_rub_date": "2026-08-19",
    "usd_rub_source": "ЦБ РФ"
  }
};
