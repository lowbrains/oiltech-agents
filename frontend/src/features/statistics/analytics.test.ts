import { describe, expect, it } from "vitest";
import type { MonthlyAnalytics } from "../../api/types";
import { comparisonBase, delta, formatHours, niceTicks, trimLeadingEmpty } from "./analytics";
import { analyticsFixture } from "./fixture.test-data";

describe("база сравнения", () => {
  it("незаконченный месяц сравнивается с теми же днями прошлого, а не с полным", () => {
    const base = comparisonBase(analyticsFixture, "2026-09");
    expect(base?.label).toBe("с 1–19 августа");
    expect(base?.row.strong).toBe(278);
  });

  it("законченный месяц — с прошлым целиком, по-русски", () => {
    const base = comparisonBase(analyticsFixture, "2026-08");
    expect(base?.label).toBe("с июлем 2026");
    expect(base?.row.strong).toBe(428);
  });

  it("у первого месяца базы нет", () => {
    expect(comparisonBase(analyticsFixture, "2026-05")).toBeNull();
  });
});

describe("дельта", () => {
  it("рост вдвое и больше — кратностью, со знаком «хорошо»", () => {
    expect(delta(756, 278, true)).toEqual({ text: "×2,7", tone: "good", arrow: "▲" });
  });

  it("для «меньше — лучше» падение хорошее", () => {
    expect(delta(0.7, 1.0, false)).toMatchObject({ text: "−30%", tone: "good", arrow: "▼" });
  });

  it("объём сбора — только направление, без оценки", () => {
    expect(delta(4728, 6048, null)).toMatchObject({ text: "−22%", tone: "neutral" });
  });

  it("нет базы — нет дельты", () => {
    expect(delta(5, 0, true)).toBeNull();
    expect(delta(null, 5, true)).toBeNull();
  });
});

describe("оформление", () => {
  it("деления оси круглые", () => {
    expect(niceTicks(756)).toEqual([0, 200, 400, 600, 800]);
    expect(niceTicks(12894)).toEqual([0, 5000, 10000, 15000]);
  });

  it("часы — минутами, часами или днями", () => {
    expect(formatHours(0.7)).toBe("42 мин");
    expect(formatHours(1)).toBe("1,0 ч");
    expect(formatHours(193.3)).toBe("8,1 дн");
    expect(formatHours(null)).toBe("—");
  });

  it("месяцы до первого сбора не показываются нулями", () => {
    const withGap: MonthlyAnalytics["months"] = [{ ...analyticsFixture.months[0], month: "2026-04", collected: 0 }, ...analyticsFixture.months];
    expect(trimLeadingEmpty(withGap)[0].month).toBe("2026-05");
  });
});
