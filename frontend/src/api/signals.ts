import { apiFetch } from "./client";
import type { Signal, SignalFeedbackPayload, SignalPatch } from "./types";

export type SignalQuery = {
  maturity?: string;
  theme?: string;
  limit?: number;
  evidenceLimit?: number;
};

export function listSignals(query: SignalQuery = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 100));
  params.set("evidence_limit", String(query.evidenceLimit ?? 5));
  if (query.maturity) params.set("maturity", query.maturity);
  if (query.theme) params.set("theme", query.theme);
  return apiFetch<Signal[]>(`/api/signals?${params.toString()}`);
}

export function updateSignal(signalId: number, payload: SignalPatch) {
  return apiFetch<{ ok: boolean }>(`/api/signals/${signalId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

// Здоровье поиска последнего ежедневного прогона — отдаётся только админу (/api/signals/search-health).
export type SignalSearchHealth = {
  run_id: number;
  job_id: number | null;
  run_at: string | null;
  status: string;
  signals: number | null;
  topics: number;
  failed: number;
  first_error: string | null;
  http_status: number | null;
  provider: string | null;
  // not_configured — поиск не вызывался (нет ключа, провайдер не подключён); network — таймаут
  // или обрыв соединения; connection — TLS или прокси; null — сбоя нет. Считает ядро (signal_discovery._search_failure_cause).
  cause: "not_configured" | "unsupported_provider" | "http" | "network" | "connection" | "other" | null;
};

export function getSignalSearchHealth() {
  return apiFetch<{ search_health: SignalSearchHealth | null }>("/api/signals/search-health");
}

export function createSignalFeedback(payload: SignalFeedbackPayload) {
  return apiFetch<{ ok: boolean; event_id: number; memory_ids: number[]; memories: number }>("/api/signals/feedback", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
