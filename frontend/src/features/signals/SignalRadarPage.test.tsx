import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
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
}));

describe("SignalRadarPage", () => {
  it("показывает «почему интересно» только там, где ревью пачки его дало", async () => {
    render(<SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    expect(await screen.findByText("Единственный открытый источник данных по метану")).toBeInTheDocument();
    expect(screen.getAllByText(/Почему интересно/)).toHaveLength(1);
    expect(screen.getByText(/Почему интересно · 91/)).toBeInTheDocument();
  });
});
