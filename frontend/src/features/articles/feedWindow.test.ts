import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { archiveNoticeText, monthLabel, useFeedWindow, windowPeriodText } from "./feedWindow";

describe("useFeedWindow", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("перезапрашивает окно при возврате на вкладку — 5-е число не застанет старое окно", async () => {
    const payload = { months: ["2026-09"], month: null, read_only: false, rollover_day: 5, archive: [] };
    const fetchMock = vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } })),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useFeedWindow());
    await waitFor(() => expect(result.current?.months).toEqual(["2026-09"]));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });
});

describe("подписи окна месяца", () => {
  it("месяц по-русски, незнакомый формат — как есть", () => {
    expect(monthLabel("2026-08")).toBe("август 2026");
    expect(monthLabel("2027-01")).toBe("январь 2027");
    expect(monthLabel("2026-13")).toBe("2026-13");
    expect(monthLabel("август")).toBe("август");
  });

  it("один открытый месяц", () => {
    expect(windowPeriodText({ months: ["2026-09"], month: null, read_only: false, rollover_day: 5 })).toBe(
      "Показан сентябрь 2026. Прошлые месяцы — в архиве, только для просмотра.",
    );
  });

  it("зазор: прошлый месяц виден до дня смены окна", () => {
    expect(windowPeriodText({ months: ["2026-09", "2026-10"], month: null, read_only: false, rollover_day: 5 })).toBe(
      "Показаны сентябрь 2026 и октябрь 2026: сентябрь виден до 5 октября, пока собирается его выпуск.",
    );
  });

  it("без данных окна подписи нет", () => {
    expect(windowPeriodText(undefined)).toBe("");
  });

  it("плашка архива", () => {
    expect(archiveNoticeText("2026-08")).toBe(
      "Архив за август 2026 — только просмотр: статус и отметку «в дайджест» здесь не поменять.",
    );
  });
});
