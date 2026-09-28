import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Source, SourceDiagnostics, SourceHealth } from "../../api/types";
import {
  countSourceStates,
  diagnosticVerdictLabel,
  getSourceTriage,
  lastLoadLabel,
  sourceProblem,
  sourceState,
} from "./sourceUtils";

const baseSource: Source = {
  id: 1,
  name: "Test source",
  enabled: true,
  url: "https://example.com",
  rss_url: null,
  parse_strategy: "request",
  source_type: "News",
  update_frequency: "ежедневно",
  listing_url: "https://example.com/news",
  listing_strategy: null,
  listing_selector: null,
  article_link_selector: null,
  article_date_selector: null,
  network_region: "auto",
  network_profile: "direct",
  last_ru_probe_status: null,
  last_external_probe_status: null,
  external_required_reason: null,
  external_cooldown_until: null,
  last_seen_article_url: null,
  last_seen_published_at: null,
  archived_at: null,
};

describe("source triage", () => {
  it("prioritizes diagnostic failure over generic health state", () => {
    const health: SourceHealth = {
      id: 1,
      verdict: "stale",
      articles: 12,
      last_article_at: "2026-06-01T00:00:00Z",
    };
    const diagnostic: SourceDiagnostics = {
      verdict: "no_candidates",
      candidate_count: 0,
    };

    const triage = getSourceTriage(baseSource, health, diagnostic);

    expect(triage.tone).toBe("warn");
    expect(triage.key).toBe("extraction");
    expect(triage.label).toBe("извлечение");
    expect(triage.title).toContain("ссылки");
  });

  it("marks sources with zero articles as a hard issue when no diagnostics are loaded", () => {
    const health: SourceHealth = {
      id: 1,
      verdict: "no_articles",
      articles: 0,
      last_article_at: null,
    };

    const triage = getSourceTriage(baseSource, health);

    expect(triage.tone).toBe("bad");
    expect(triage.key).toBe("no_articles");
    expect(triage.label).toBe("без материалов");
  });

  it("returns muted triage for disabled sources", () => {
    const triage = getSourceTriage({ ...baseSource, enabled: false });

    expect(triage.tone).toBe("muted");
    expect(triage.key).toBe("disabled");
    expect(triage.label).toBe("отключён");
  });

  it("localizes diagnostic verdict labels", () => {
    expect(diagnosticVerdictLabel("playwright_unavailable")).toBe("playwright недоступен");
    expect(diagnosticVerdictLabel("unknown_problem")).toBe("unknown_problem");
  });
});

describe("source states on the sources screen", () => {
  const now = new Date("2026-09-25T08:00:00Z");
  const health = (id: number, verdict: SourceHealth["verdict"], extra: Partial<SourceHealth> = {}): SourceHealth => ({
    id,
    verdict,
    articles: 10,
    articles_30d: 3,
    last_article_at: "2026-09-19T06:00:00Z",
    ...extra,
  });

  it("counts tiles over the same list the table shows, archive apart from the total", () => {
    // Замер 19.09: плитки считались по отчёту здоровья с архивом (173), список и
    // счётчик в шапке — без архива (133). Теперь «Всего» = сумма состояний без архива.
    const sources: Source[] = [
      { ...baseSource, id: 1 },
      { ...baseSource, id: 2 },
      { ...baseSource, id: 3 },
      { ...baseSource, id: 4, enabled: false },
      { ...baseSource, id: 5, enabled: false, archived_at: "2026-09-12T10:00:00Z" },
      { ...baseSource, id: 6, enabled: false, archived_at: "2026-09-12T10:00:00Z" },
    ];
    const healthById = new Map<number, SourceHealth>([
      [1, health(1, "ok")],
      [2, health(2, "stale")],
      [3, health(3, "no_articles", { articles: 0, articles_30d: 0, last_article_at: null })],
      [4, health(4, "disabled")],
      [5, health(5, "archived")],
      [6, health(6, "archived")],
    ]);

    const counts = countSourceStates(sources, healthById);

    expect(counts).toEqual({ total: 4, ok: 1, stale: 1, no_articles: 1, disabled: 1, archived: 2 });
    expect(counts.ok + counts.stale + counts.no_articles + counts.disabled).toBe(counts.total);
  });

  it("does not invent a state when the health row is missing", () => {
    expect(sourceState({ ...baseSource }, undefined)).toBeNull();
    expect(sourceState({ ...baseSource, enabled: false }, undefined)).toBe("disabled");
    expect(sourceState({ ...baseSource, enabled: false, archived_at: "2026-09-12T10:00:00Z" }, undefined)).toBe("archived");
  });

  it("says in the problem column how long a stale source has been silent", () => {
    // stale ставит сервер: нет нового материала дольше SOURCE_STALE_DAYS (с 28.09 — 7 суток).
    const stale = health(1, "stale", { last_article_at: "2026-09-17T06:00:00Z" });
    expect(sourceProblem(baseSource, stale, undefined, now)).toBe("Нет новых материалов 8 дн.");
    expect(sourceProblem(baseSource, health(1, "ok"), undefined, now)).toBe("—");
    expect(sourceProblem(baseSource, health(1, "no_articles"), undefined, now)).toBe("Ни одного материала");
    expect(sourceProblem({ ...baseSource, enabled: false }, health(1, "disabled"), undefined, now)).toBe("Сбор выключен");
    expect(sourceProblem(baseSource, stale, { verdict: "listing_fetch_failed" }, now)).toBe(
      "Источник не открывается на этапе диагностики.",
    );
  });

  it("formats the last load as a date and how long ago", () => {
    expect(lastLoadLabel("2026-09-19T06:00:00Z", now)).toEqual({ date: "19.09.2026", ago: "6 дн. назад" });
    expect(lastLoadLabel("2026-09-25T01:00:00Z", now)?.ago).toBe("сегодня");
    expect(lastLoadLabel("2026-09-24T01:00:00Z", now)?.ago).toBe("вчера");
    expect(lastLoadLabel(null, now)).toBeNull();
  });

  it("counts days ago by calendar, like the date itself", () => {
    // Ревью F: вчера днём, смотрим сегодня утром — 18 ч, но это «вчера», а не «сегодня».
    const morning = new Date("2026-09-25T06:00:00Z");
    expect(lastLoadLabel("2026-09-24T12:00:00Z", morning)).toEqual({ date: "24.09.2026", ago: "вчера" });
  });

  describe("in a browser outside Moscow", () => {
    // «Требует внимания» сервер ставит по календарю Москвы (source_health_report), и колонка
    // обязана считать дни так же в любом поясе браузера. Екатеринбург — UTC+5.
    beforeEach(() => {
      vi.stubEnv("TZ", "Asia/Yekaterinburg");
    });
    afterEach(() => {
      vi.unstubAllEnvs();
    });

    it("counts days by the Moscow calendar, like the server's stale verdict", () => {
      // Те же моменты, что в tests/test_source_stale_threshold.py: новый день — в 00:00 МСК (21:00 UTC).
      const midnight = "2026-09-21T21:00:00Z"; // 22.09 00:00 МСК
      expect(lastLoadLabel(midnight, new Date("2026-09-28T20:59:00Z"))).toEqual({ date: "22.09.2026", ago: "6 дн. назад" });
      expect(lastLoadLabel(midnight, new Date("2026-09-28T21:00:00Z"))).toEqual({ date: "22.09.2026", ago: "7 дн. назад" });
      // Минутой раньше — ещё 21.09 по Москве: в 23:59 МСК 28.09 это уже 7 дней.
      expect(lastLoadLabel("2026-09-21T20:59:00Z", new Date("2026-09-28T20:59:00Z"))).toEqual({
        date: "21.09.2026",
        ago: "7 дн. назад",
      });
    });
  });

  it("explains an archived source instead of calling it switched off", () => {
    const triage = getSourceTriage({ ...baseSource, enabled: false, archived_at: "2026-09-12T10:00:00Z" });

    expect(triage.key).toBe("archived");
    expect(triage.label).toBe("в архиве");
  });
});
