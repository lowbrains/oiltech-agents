// Чистые расчёты экрана «Статистика»: база сравнения, дельты, форматы. Без React —
// чтобы правило «с чем сравниваем» проверялось тестом, а не глазами.
import type { AnalyticsCounters, AnalyticsMonth, MonthlyAnalytics } from "../../api/types";

const MONTHS_FULL = [
  "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
];
const MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
// «Сравнение с июлем 2026», а не «с июль 2026».
const MONTHS_INSTRUMENTAL = [
  "январём", "февралём", "мартом", "апрелем", "маем", "июнем",
  "июлем", "августом", "сентябрём", "октябрём", "ноябрём", "декабрём",
];
const MONTHS_GENITIVE = [
  "января", "февраля", "марта", "апреля", "мая", "июня",
  "июля", "августа", "сентября", "октября", "ноября", "декабря",
];

export function monthLabel(month: string): string {
  const [year, m] = month.split("-").map(Number);
  return `${MONTHS_FULL[m - 1]} ${year}`;
}

export function monthShort(month: string): string {
  return MONTHS_SHORT[Number(month.split("-")[1]) - 1];
}

export function monthGenitive(month: string): string {
  return MONTHS_GENITIVE[Number(month.split("-")[1]) - 1];
}

// Месяцы до первого сбора платформы — не «ноль работы», а «платформы ещё не было».
export function trimLeadingEmpty(months: AnalyticsMonth[]): AnalyticsMonth[] {
  const first = months.findIndex((row) => row.collected > 0);
  return first <= 0 ? months : months.slice(first);
}

export type Base = { row: AnalyticsCounters; label: string };

/** С чем сравнивать выбранный месяц. Незаконченный — с теми же днями прошлого:
 *  «сентябрь по 19-е» против полного августа проигрывал бы всегда. */
export function comparisonBase(data: MonthlyAnalytics, month: string): Base | null {
  const index = data.months.findIndex((row) => row.month === month);
  if (index <= 0) return null;
  const previous = data.months[index - 1];
  if (month === data.current_month && !data.months[index].complete) {
    const same = data.previous_same_period;
    return { row: same, label: `с 1–${same.days} ${monthGenitive(same.month)}` };
  }
  const [year, m] = previous.month.split("-").map(Number);
  return { row: previous, label: `с ${MONTHS_INSTRUMENTAL[m - 1]} ${year}` };
}

export type Delta = { text: string; tone: "good" | "bad" | "flat" | "neutral"; arrow: "▲" | "▼" | "—" };

/** upIsGood: null — рост ни хорош, ни плох (объём сбора), только направление. */
export function delta(current: number | null, base: number | null, upIsGood: boolean | null): Delta | null {
  if (current == null || base == null) return null;
  if (base === 0) return current === 0 ? { text: "без изменений", tone: "flat", arrow: "—" } : null;
  const change = (current - base) / base;
  if (Math.abs(change) < 0.005) return { text: "без изменений", tone: "flat", arrow: "—" };
  const up = change > 0;
  const ratio = current / base;
  const text = ratio >= 2 ? `×${formatDecimal(ratio, 1)}` : `${up ? "+" : "−"}${Math.round(Math.abs(change) * 100)}%`;
  const tone = upIsGood === null ? "neutral" : up === upIsGood ? "good" : "bad";
  return { text, tone, arrow: up ? "▲" : "▼" };
}

const INT = new Intl.NumberFormat("ru-RU");

export function formatInt(value: number | null | undefined): string {
  return value == null ? "—" : INT.format(Math.round(value));
}

export function formatDecimal(value: number, digits = 1): string {
  return new Intl.NumberFormat("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
}

export function share(part: number, total: number): number | null {
  return total ? part / total : null;
}

export function formatShare(value: number | null): string {
  if (value == null) return "—";
  const pct = value * 100;
  return `${pct > 0 && pct < 1 ? "<1" : Math.round(pct)}%`;
}

export function formatHours(hours: number | null): string {
  if (hours == null) return "—";
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} мин`;
  if (hours < 48) return `${formatDecimal(hours, hours < 10 ? 1 : 0)} ч`;
  return `${formatDecimal(hours / 24, 1)} дн`;
}

export function formatRub(value: number | null): string {
  return value == null ? "—" : `${INT.format(Math.round(value))} ₽`;
}

/** «Красивые» деления оси: 0 / 1 000 / 2 000, а не 0 / 1 137 / 2 274. */
export function niceTicks(max: number, count = 4): number[] {
  if (max <= 0) return [0];
  const rough = max / count;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((k) => k * power).find((candidate) => candidate >= rough) ?? rough;
  const ticks = [];
  for (let value = 0; value <= max + step * 0.001; value += step) ticks.push(Math.round(value * 1000) / 1000);
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}
