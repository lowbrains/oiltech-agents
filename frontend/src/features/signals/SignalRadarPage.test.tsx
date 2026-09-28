import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getSignalSearchHealth, type SignalSearchHealth } from "../../api/signals";
import type { Signal } from "../../api/types";
import { SignalRadarPage } from "./SignalRadarPage";

const baseSignal: Signal = {
  id: 107,
  signal_key: "k107",
  title: "MethaneSAT lost contact",
  title_ru: "Потеря связи со спутником MethaneSAT",
  theme: "Экология",
  summary: "Спутник мониторинга метана перестал выходить на связь.",
  thesis: null,
  transferability: "Спутниковый мониторинг утечек",
  maturity: "watch",
  confidence: 0.7,
  score: 72,
  why_now: "Сбой единственного открытого спутника",
  why_not_noise: null,
  companies_json: ["EDF / MethaneSAT"],
  industries_json: [],
  evidence_count: 2,
  first_seen_at: null,
  last_seen_at: null,
  created_at: null,
  updated_at: null,
  evidence: [],
};

vi.mock("../../api/signals", () => ({
  listSignals: vi.fn(async () => [
    { ...baseSignal, interest_score: 91.4, why_interesting: "Единственный открытый источник данных по метану" },
    { ...baseSignal, id: 3, signal_key: "k3", title_ru: "Карточка без ревью пачки" },
  ]),
  updateSignal: vi.fn(),
  createSignalFeedback: vi.fn(),
  getSignalSearchHealth: vi.fn(),
}));

// Ежедневный прогон 27.09: крон 07:15 МСК = 04:15 UTC, Brave 402 во всех темах.
const failedEverywhere: SignalSearchHealth = {
  run_id: 31,
  job_id: 5120,
  run_at: "2026-09-27T04:15:02+00:00",
  status: "failed",
  signals: 0,
  topics: 13,
  failed: 13,
  first_error: "HTTP 402 Usage limit exceeded",
  http_status: 402,
  provider: "brave",
  cause: "http",
};

const RUN = "Прогон 27.09 в 07:15:";
// Каждая причина — по-русски; поиск, который не вызывался, «не выполнен», а не «не ответил».
const CAUSES: Array<[string, Partial<SignalSearchHealth>, string]> = [
  [
    "нет ключа",
    { cause: "not_configured", http_status: null, first_error: "BRAVE_SEARCH_API_KEY is empty" },
    `${RUN} поиск не выполнен в 13 из 13 тем (поиск не настроен — нет ключа). Новых сигналов нет.`,
  ],
  [
    "провайдер не подключён",
    { cause: "not_configured", provider: "none", http_status: null, first_error: "search provider is not connected yet" },
    `${RUN} поиск не выполнен в 13 из 13 тем (провайдер поиска не подключён). Новых сигналов нет.`,
  ],
  [
    "неизвестный провайдер — текст сервера",
    {
      cause: "unsupported_provider",
      provider: "brvae",
      http_status: null,
      first_error: "unsupported SOURCE_DISCOVERY_SEARCH_PROVIDER=brvae",
    },
    `${RUN} поиск не выполнен в 13 из 13 тем (unsupported SOURCE_DISCOVERY_SEARCH_PROVIDER=brvae). Новых сигналов нет.`,
  ],
  [
    "429",
    { http_status: 429, first_error: "HTTP 429 Request rate limit exceeded for plan" },
    `${RUN} поиск не ответил в 13 из 13 тем (HTTP 429 — превышен лимит запросов к поиску). Новых сигналов нет.`,
  ],
  [
    "5xx",
    { http_status: 503, first_error: "HTTP 503 Service Unavailable" },
    `${RUN} поиск не ответил в 13 из 13 тем (HTTP 503 — сервис поиска недоступен). Новых сигналов нет.`,
  ],
  [
    "таймаут или обрыв соединения",
    {
      cause: "network",
      http_status: null,
      first_error: "HTTPSConnectionPool(host='api.search.brave.com', port=443): Read timed out. (read timeout=20)",
    },
    `${RUN} поиск не ответил в 13 из 13 тем (поиск не ответил вовремя). Новых сигналов нет.`,
  ],
  [
    "TLS или прокси",
    {
      cause: "connection",
      http_status: null,
      first_error: "HTTPSConnectionPool(host='api.search.brave.com', port=443): Max retries exceeded (SSLError)",
    },
    `${RUN} поиск не ответил в 13 из 13 тем (нет соединения с поиском). Новых сигналов нет.`,
  ],
  [
    "прочая ошибка HTTP — текст сервера",
    { http_status: 404, first_error: "HTTP 404 Not Found" },
    `${RUN} поиск не ответил в 13 из 13 тем (HTTP 404 Not Found). Новых сигналов нет.`,
  ],
  [
    "402 не у Brave — текст сервера",
    { provider: "serpapi", first_error: "HTTP 402 Payment Required" },
    `${RUN} поиск не ответил в 13 из 13 тем (HTTP 402 Payment Required). Новых сигналов нет.`,
  ],
  [
    "прочая ошибка без кода — текст сервера",
    { cause: "other", http_status: null, first_error: "Expecting value: line 1 column 1 (char 0)" },
    `${RUN} поиск не ответил в 13 из 13 тем (Expecting value: line 1 column 1 (char 0)). Новых сигналов нет.`,
  ],
];

function renderRadar(isAdmin: boolean) {
  render(<SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} isAdmin={isAdmin} />);
}

describe("SignalRadarPage", () => {
  beforeEach(() => {
    vi.mocked(getSignalSearchHealth).mockReset();
  });

  it("показывает «почему интересно» только там, где ревью пачки его дало", async () => {
    render(<SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    expect(await screen.findByText("Единственный открытый источник данных по метану")).toBeInTheDocument();
    expect(screen.getAllByText(/Почему интересно/)).toHaveLength(1);
    expect(screen.getByText(/Почему интересно · 91/)).toBeInTheDocument();
  });

  it("админ видит плашку: поиск не ответил во всех темах", async () => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({ search_health: failedEverywhere });

    renderRadar(true);

    expect(
      await screen.findByText(
        "Прогон 27.09 в 07:15: поиск не ответил в 13 из 13 тем (HTTP 402 — исчерпан месячный лимит поиска Brave). Новых сигналов нет.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveClass("archiveNotice");
  });

  it.each(CAUSES)("причина по-русски: %s", async (_cause, patch, text) => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({ search_health: { ...failedEverywhere, ...patch } });

    renderRadar(true);

    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it("админ видит плашку и при частичном сбое", async () => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({
      search_health: { ...failedEverywhere, status: "ok", failed: 3, signals: 4 },
    });

    renderRadar(true);

    expect(
      await screen.findByText(
        "Прогон 27.09 в 07:15: поиск не ответил в 3 из 13 тем (HTTP 402 — исчерпан месячный лимит поиска Brave).",
      ),
    ).toBeInTheDocument();
  });

  it("обычный пользователь плашку не видит и здоровье поиска не запрашивает", async () => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({ search_health: failedEverywhere });

    renderRadar(false);

    expect(await screen.findByText("Карточка без ревью пачки")).toBeInTheDocument();
    expect(getSignalSearchHealth).not.toHaveBeenCalled();
    expect(screen.queryByText(/поиск не ответил/)).not.toBeInTheDocument();
  });

  it("после успешного прогона плашки нет", async () => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({
      search_health: { ...failedEverywhere, status: "ok", failed: 0, signals: 9, first_error: null, http_status: null },
    });

    renderRadar(true);

    expect(await screen.findByText("Карточка без ревью пачки")).toBeInTheDocument();
    expect(getSignalSearchHealth).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/поиск не ответил/)).not.toBeInTheDocument();
  });
});
