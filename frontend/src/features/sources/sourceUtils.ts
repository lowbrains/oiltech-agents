import type { Source, SourceDiagnostics, SourceHealth, SourcePatch } from "../../api/types";

export type SourceState = SourceHealth["verdict"];

type StateTone = "ok" | "warn" | "bad" | "muted";

// Состояния источника — названия из документа заказчика «Главная страница» (19.09).
// Порядок — порядок плиток и сортировки: сначала то, с чем нужно что-то делать.
export const SOURCE_STATES: ReadonlyArray<{ state: SourceState; label: string; tile: string; tone: StateTone }> = [
  { state: "no_articles", label: "Без материалов", tile: "Без материалов", tone: "bad" },
  { state: "stale", label: "Требует внимания", tile: "Требуют внимания", tone: "warn" },
  { state: "ok", label: "Работает штатно", tile: "Работают штатно", tone: "ok" },
  { state: "disabled", label: "Отключён", tile: "Отключены", tone: "muted" },
  { state: "archived", label: "В архиве", tile: "В архиве", tone: "muted" },
];

const STATE_BY_KEY = new Map(SOURCE_STATES.map((item) => [item.state, item]));

/** Состояние считает сервер (source_health_report); без его строки — только то, что
 *  известно из самого источника. «Без материалов» по догадке не ставим: это неправда. */
export function sourceState(source: Source, health?: SourceHealth): SourceState | null {
  if (health?.verdict) return health.verdict;
  if (source.archived_at) return "archived";
  if (!source.enabled) return "disabled";
  return null;
}

export function sourceStateLabel(state: SourceState | null) {
  return state ? STATE_BY_KEY.get(state)?.label ?? "—" : "—";
}

export function sourceStateTone(state: SourceState | null): StateTone {
  return state ? STATE_BY_KEY.get(state)?.tone ?? "muted" : "muted";
}

export type SourceStateCounts = Record<SourceState, number> & { total: number };

/** Плитки считаются по ТОМУ ЖЕ списку, что показывает таблица, — в этом и была ошибка:
 *  плитки шли по отчёту здоровья вместе с архивом (173), а список и счётчик в шапке —
 *  без архива (133). «Всего источников» — каталог без архива; архив — своя плитка. */
export function countSourceStates(sources: Source[], healthById: Map<number, SourceHealth>): SourceStateCounts {
  const counts: SourceStateCounts = { total: 0, no_articles: 0, stale: 0, ok: 0, disabled: 0, archived: 0 };
  sources.forEach((source) => {
    const state = sourceState(source, healthById.get(source.id));
    if (state) counts[state] += 1;
    if (state !== "archived") counts.total += 1;
  });
  return counts;
}

const DAY_MS = 24 * 60 * 60 * 1000;

// Дату и дни «Последней загрузки» считаем по Москве, как сервер — вердикт stale
// (source_health_report): «7 дн. назад» здесь ⇔ «Требует внимания» там. Пояс браузера не
// берём: у зрителя не из Москвы день сменяется в другой час, и число разошлось бы с состоянием.
const MSK_TIME_ZONE = "Europe/Moscow";
const mskDateParts = new Intl.DateTimeFormat("en-US", {
  timeZone: MSK_TIME_ZONE,
  year: "numeric",
  month: "numeric",
  day: "numeric",
});

/** Номер календарного дня по Москве: дни вычитаются без часов и переводов времени. */
function mskDayNumber(date: Date) {
  const part = Object.fromEntries(mskDateParts.formatToParts(date).map(({ type, value }) => [type, Number(value)]));
  return Date.UTC(part.year, part.month - 1, part.day) / DAY_MS;
}

/** Сколько календарных дней назад — по календарю Москвы, как и сама дата, а не полными
 *  сутками: иначе вчерашние 13:00 в 11:00 давали «24.09 · сегодня». */
function calendarDaysAgo(value: string, now: Date) {
  return Math.max(0, mskDayNumber(now) - mskDayNumber(new Date(value)));
}

/** «Последняя загрузка» — когда от источника пришёл последний материал. */
export function lastLoadLabel(value: string | null | undefined, now: Date = new Date()) {
  if (!value || Number.isNaN(new Date(value).getTime())) return null;
  const date = new Date(value).toLocaleDateString("ru-RU", {
    timeZone: MSK_TIME_ZONE,
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
  const days = calendarDaysAgo(value, now);
  const ago = days === 0 ? "сегодня" : days === 1 ? "вчера" : `${days} дн. назад`;
  return { date, ago };
}

/** Колонка «Проблема» — короткая подпись к разбору getSourceTriage (одна цепочка
 *  проверок на экран): результат диагностики важнее вердикта. */
export function sourceProblem(
  source: Source,
  health?: SourceHealth,
  diagnostic?: SourceDiagnostics,
  now: Date = new Date(),
): string {
  const triage = getSourceTriage(source, health, diagnostic);
  switch (triage.key) {
    case "ok":
    case "archived":
      return "—";
    case "disabled":
      return "Сбор выключен";
    case "no_articles":
      return "Ни одного материала";
    case "stale":
      return health?.last_article_at
        ? `Нет новых материалов ${calendarDaysAgo(health.last_article_at, now)} дн.`
        : triage.title;
    default:
      return triage.title;
  }
}

/** Подпись состояния строчными — для пилюли разбора в раскрытой строке. */
function stateLabelLower(state: SourceState) {
  return sourceStateLabel(state).toLowerCase();
}

// Вид источника — ЧЕМ его читаем. Берём parse_strategy, а не source_type: в
// source_type лежит свободный текст из xlsx заказчика (33 разных значения: Company,
// Journal, "Company / NOC", "University / R&D"…), и он ВРЁТ на всём, что добавлено
// через форму — там срабатывает дефолт "RSS" независимо от выбранной стратегии.
// Из-за этого на карточке печаталось самопротиворечивое «request · RSS».
export function strategyLabel(strategy?: string | null) {
  return (
    {
      rss: "RSS-лента",
      request: "Разбор страницы",
      playwright: "Браузер",
      telegram: "Telegram",
    }[strategy || ""] || "Не задан"
  );
}

type TriageTone = "ok" | "warn" | "bad" | "muted";

export type SourceTriage = {
  tone: TriageTone;
  key: string;
  label: string;
  title: string;
  action: string;
};

const diagnosticVerdictLabels: Record<string, string> = {
  ok: "ОК",
  unsupported_strategy: "стратегия не поддержана",
  missing_listing_url: "нет listing URL",
  listing_fetch_failed: "listing не открылся",
  listing_render_failed: "listing не отрендерился",
  no_candidates: "ссылки не извлеклись",
  no_insertable_articles: "статьи не вставляются",
  article_fetch_failed: "статья не открылась",
  article_render_failed: "статья не отрендерилась",
  article_not_insertable: "статья не вставляется",
  missing_or_invalid_channel_url: "неверный telegram URL",
  preview_fetch_failed: "telegram preview недоступен",
  no_posts: "посты не найдены",
  missing_rss_url: "нет RSS URL",
  rss_fetch_failed: "RSS недоступна",
  no_entries: "RSS пустая",
  playwright_unavailable: "playwright недоступен",
};

export function diagnosticVerdictLabel(verdict?: string) {
  if (!verdict) return "—";
  return diagnosticVerdictLabels[verdict] || verdict;
}

// Тон пилюли диагностики по серьёзности вердикта: жёсткие сбои доступа/конфига —
// красный (bad), «нашли, но не извлекли» — оранжевый (warn), ok — зелёный.
const DIAGNOSTIC_BAD_VERDICTS = new Set([
  "missing_listing_url",
  "listing_fetch_failed",
  "listing_render_failed",
  "preview_fetch_failed",
  "rss_fetch_failed",
  "missing_or_invalid_channel_url",
  "missing_rss_url",
  "playwright_unavailable",
  "article_fetch_failed",
  "article_render_failed",
]);

export function diagnosticVerdictClass(verdict?: string): "ok" | "warn" | "bad" {
  if (!verdict || verdict === "ok") return "ok";
  return DIAGNOSTIC_BAD_VERDICTS.has(verdict) ? "bad" : "warn";
}

export function normalizePatch(patch: SourcePatch): SourcePatch {
  const payload: Partial<Record<keyof SourcePatch, SourcePatch[keyof SourcePatch]>> = {};
  Object.entries(patch).forEach(([key, value]) => {
    if (value === "") {
      payload[key as keyof SourcePatch] = null;
      return;
    }
    payload[key as keyof SourcePatch] = value as SourcePatch[keyof SourcePatch];
  });
  return payload as SourcePatch;
}

function defaultActionForStrategy(strategy?: string | null) {
  if (strategy === "request") return "Запустите диагностику и проверьте страницу новостей, ссылки и шаблон статьи.";
  if (strategy === "playwright") return "Проверьте рендер listing/article и при необходимости прокси для hard-WAF.";
  if (strategy === "rss") return "Проверьте RSS URL и наличие свежих entries.";
  if (strategy === "telegram") return "Проверьте URL канала и доступность preview.";
  return "Запустите диагностику и проверьте настройки источника.";
}

function diagnosticTriage(source: Source, verdict?: string): SourceTriage | null {
  if (!verdict || verdict === "ok") return null;
  switch (verdict) {
    case "missing_listing_url":
      return {
        tone: "bad",
        key: "config",
        label: "настройка",
        title: "Не задана страница новостей.",
        action: "Укажите listing URL и сохраните источник.",
      };
    case "listing_fetch_failed":
    case "listing_render_failed":
    case "preview_fetch_failed":
    case "rss_fetch_failed":
      return {
        tone: "bad",
        key: "access",
        label: "доступ",
        title: "Источник не открывается на этапе диагностики.",
        action: defaultActionForStrategy(source.parse_strategy),
      };
    case "no_candidates":
      return {
        tone: "warn",
        key: "extraction",
        label: "извлечение",
        title: "Страница открывается, но ссылки на материалы не извлекаются.",
        action: "Проверьте listing URL, структуру страницы и при необходимости переведите источник на Playwright.",
      };
    case "no_insertable_articles":
      return {
        tone: "warn",
        key: "content",
        label: "содержимое",
        title: "Материалы находятся, но не проходят до вставки в базу.",
        action: "Проверьте шаблон статьи, шум и длину текста в диагностике.",
      };
    case "missing_or_invalid_channel_url":
      return {
        tone: "bad",
        key: "telegram",
        label: "telegram",
        title: "У источника некорректный URL Telegram-канала.",
        action: "Исправьте URL канала и повторите диагностику.",
      };
    case "no_posts":
      return {
        tone: "warn",
        key: "telegram",
        label: "telegram",
        title: "Preview канала открывается, но посты не находятся.",
        action: "Проверьте разметку preview и актуальность канала.",
      };
    case "missing_rss_url":
      return {
        tone: "bad",
        key: "rss",
        label: "rss",
        title: "У RSS-источника не задан RSS URL.",
        action: "Укажите RSS URL или переключите источник на request/playwright.",
      };
    case "no_entries":
      return {
        tone: "warn",
        key: "rss",
        label: "rss",
        title: "RSS открывается, но лента пустая.",
        action: "Проверьте актуальность ссылки RSS и наличие новых записей в ленте.",
      };
    case "playwright_unavailable":
      return {
        tone: "bad",
        key: "infra",
        label: "infra",
        title: "Playwright недоступен в текущем окружении.",
        action: "Проверьте playwright-worker и установку браузера в контейнере.",
      };
    default:
      return {
        tone: "warn",
        key: "diagnostic",
        label: "диагностика",
        title: `Источник вернул статус: ${diagnosticVerdictLabel(verdict)}.`,
        action: defaultActionForStrategy(source.parse_strategy),
      };
  }
}

export function getSourceTriage(
  source: Source,
  health?: SourceHealth,
  diagnostic?: SourceDiagnostics,
): SourceTriage {
  // Архив раньше выключения: архив тоже снимает enabled, но возвращают его другой кнопкой.
  if (source.archived_at || health?.verdict === "archived") {
    return {
      tone: "muted",
      key: "archived",
      label: stateLabelLower("archived"),
      title: "Источник в архиве: не опрашивается, его статьи скрыты из ленты.",
      action: "Верните источник из архива, если он снова нужен, и включите сбор.",
    };
  }
  if (!source.enabled) {
    return {
      tone: "muted",
      key: "disabled",
      label: stateLabelLower("disabled"),
      title: "Источник выключен и не участвует в сборе.",
      action: "Включите источник, если его нужно вернуть в мониторинг.",
    };
  }

  const fromDiagnostic = diagnosticTriage(source, diagnostic?.verdict);
  if (fromDiagnostic) return fromDiagnostic;

  if (health?.verdict === "no_articles") {
    return {
      tone: "bad",
      key: "no_articles",
      label: stateLabelLower("no_articles"),
      title: "В базе еще нет ни одной статьи из этого источника.",
      action: defaultActionForStrategy(source.parse_strategy),
    };
  }
  if (health?.verdict === "stale") {
    return {
      tone: "warn",
      key: "stale",
      label: stateLabelLower("stale"),
      title: "Источник давно не приносил новых материалов.",
      action: "Запустите диагностику и проверьте свежий listing/RSS перед форс-парсингом.",
    };
  }

  return {
    tone: "ok",
    key: "ok",
    label: stateLabelLower("ok"),
    title: "Источник выглядит рабочим по текущим данным.",
    action: "Ничего не требуется, только периодический контроль обновлений.",
  };
}

// Только виды проблем из диагностики: состояние (без материалов, требуют внимания,
// штатно, отключены) выбирается плитками над таблицей — второй его список здесь был лишним.
export const TRIAGE_FILTER_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "", label: "Все проблемы" },
  { value: "access", label: "Нет доступа" },
  { value: "extraction", label: "Не извлекаются ссылки" },
  { value: "content", label: "Не вставляется содержимое" },
  { value: "rss", label: "Проблема RSS" },
  { value: "telegram", label: "Проблема Telegram" },
  { value: "config", label: "Нужна настройка" },
  { value: "infra", label: "Проблема infra" },
];

export function diagnosticText(diagnostic: SourceDiagnostics) {
  const probe = diagnostic.listing_probe || diagnostic.preview_probe || diagnostic.rss_probe || {};
  const counts = [
    diagnostic.candidate_count != null ? `кандидатов: ${diagnostic.candidate_count}` : "",
    diagnostic.post_count != null ? `постов: ${diagnostic.post_count}` : "",
    diagnostic.entry_count != null ? `RSS entries: ${diagnostic.entry_count}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const checks = (diagnostic.article_checks || [])
    .slice(0, 3)
    .map((item, index) => `${index + 1}. ${item.verdict || "—"} · ${item.text_chars || 0} симв. · ${item.candidate_url || ""}`)
    .join("\n");

  const items = (diagnostic.candidates || diagnostic.posts || diagnostic.entries || [])
    .slice(0, 3)
    .map((item, index) => `${index + 1}. ${item.title || item.url || ""}`)
    .join("\n");

  return [
    `verdict: ${diagnostic.verdict || "—"}`,
    probe.status ? `HTTP: ${probe.status} · ${probe.bytes || 0} bytes${probe.proxy ? ` · proxy ${probe.proxy}` : ""}` : "",
    counts,
    checks || items,
  ]
    .filter(Boolean)
    .join("\n");
}
