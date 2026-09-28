import { apiFetch } from "./client";
import type {
  BackgroundJob,
  AgentAction,
  AgentMemory,
  AgentPlan,
  AgentRun,
  CreateSourcePayload,
  ManualArticleImportPayload,
  ManualArticleImportResult,
  QueryMemoryRow,
  ScrapeResponse,
  Source,
  SourceCandidate,
  SourceCandidateApprovePayload,
  SourceCandidateArticle,
  SourceCandidateEvaluationResult,
  SourceCandidatePatchPayload,
  SourceCandidateTriageRow,
  SourceDiscoveryDiscoverPayload,
  SourceDiscoveryDiscoverResult,
  SourceDiscoveryEvaluation,
  SourceDiscoveryLoopResult,
  SourceDiscoveryLoopPayload,
  SourceDiscoveryPlanPayload,
  SourceDiscoveryQualityRow,
  SourceDiscoveryReadiness,
  SourceDiagnostics,
  SourceHealth,
  SourcePatch,
} from "./types";

export function listSources() {
  return apiFetch<Source[]>("/api/sources?limit=500");
}

// Порог «Требуют внимания» (stale_days) не передаём намеренно: правило одно, его держит
// сервер — config.SOURCE_STALE_DAYS. Своя копия числа здесь разошлась бы с CLI и замерами.
export function listSourceHealth() {
  return apiFetch<SourceHealth[]>("/api/source-health?limit=500");
}

export function listSourceCandidates(query: { status?: string; topic?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 100));
  if (query.status) params.set("status", query.status);
  if (query.topic) params.set("topic", query.topic);
  return apiFetch<SourceCandidate[]>(`/api/source-candidates?${params.toString()}`);
}

export function listSourceCandidateTriage(query: { limit?: number } = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 20));
  return apiFetch<SourceCandidateTriageRow[]>(`/api/source-candidates/triage?${params.toString()}`);
}

export function listSourceCandidateArticles(candidateId: number) {
  return apiFetch<SourceCandidateArticle[]>(`/api/source-candidates/${candidateId}/articles?limit=20`);
}

export function evaluateSourceCandidate(candidateId: number, articleLimit = 5, offline = true) {
  return apiFetch<SourceCandidateEvaluationResult>(`/api/source-candidates/${candidateId}/evaluate`, {
    method: "POST",
    body: JSON.stringify({
      article_limit: articleLimit,
      offline,
      collect: true,
      process: true,
    }),
  });
}

export function approveSourceCandidate(candidateId: number, payload: SourceCandidateApprovePayload) {
  return apiFetch<{ ok: boolean; source_id: number; initial_job: BackgroundJob | null }>(`/api/source-candidates/${candidateId}/approve`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateSourceCandidate(candidateId: number, payload: SourceCandidatePatchPayload) {
  return apiFetch<{ ok: boolean }>(`/api/source-candidates/${candidateId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function getSourceDiscoveryPlan(query: SourceDiscoveryPlanPayload = {}) {
  const params = new URLSearchParams();
  params.set("days", String(query.days ?? 30));
  params.set("target_per_topic", String(query.target_per_topic ?? 10));
  params.set("topic_limit", String(query.topic_limit ?? 5));
  params.set("candidate_limit", String(query.candidate_limit ?? 10));
  params.set("max_actions", String(query.max_actions ?? 5));
  return apiFetch<AgentPlan>(`/api/source-discovery/plan?${params.toString()}`);
}

export function enqueueSourceDiscoveryPlan(payload: SourceDiscoveryPlanPayload = {}) {
  return apiFetch<{ ok: boolean; job: BackgroundJob }>("/api/source-discovery/plan/enqueue", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function enqueueSourceDiscoveryLoop(payload: SourceDiscoveryLoopPayload = {}) {
  return apiFetch<{ ok: boolean; job: BackgroundJob }>("/api/source-discovery/loop/enqueue", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function dryRunSourceDiscoveryLoop(payload: SourceDiscoveryLoopPayload = {}) {
  return apiFetch<{ ok: boolean; result: SourceDiscoveryLoopResult }>("/api/source-discovery/loop/dry-run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function discoverSourceCandidate(payload: SourceDiscoveryDiscoverPayload) {
  return apiFetch<{ ok: boolean; result: SourceDiscoveryDiscoverResult }>("/api/source-discovery/discover", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listAgentMemory(query: { memory_type?: string; status?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 100));
  if (query.memory_type) params.set("memory_type", query.memory_type);
  if (query.status !== undefined) params.set("status", query.status);
  return apiFetch<AgentMemory[]>(`/api/source-discovery/memory?${params.toString()}`);
}

export function updateAgentMemory(memoryId: number, payload: { status: "active" | "muted" | "rejected" }) {
  return apiFetch<{ ok: boolean }>(`/api/source-discovery/memory/${memoryId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function createAgentMemory(payload: { memory_type: string; subject: string; status?: "active" | "muted" | "rejected"; score?: number; facts?: Record<string, unknown> }) {
  return apiFetch<{ ok: boolean; id: number }>("/api/source-discovery/memory", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listAgentActions(query: { action_type?: string; run_id?: number; limit?: number } = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 50));
  if (query.action_type) params.set("action_type", query.action_type);
  if (query.run_id) params.set("run_id", String(query.run_id));
  return apiFetch<AgentAction[]>(`/api/source-discovery/actions?${params.toString()}`);
}

export function listAgentRuns(query: { status?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 50));
  if (query.status) params.set("status", query.status);
  return apiFetch<AgentRun[]>(`/api/source-discovery/runs?${params.toString()}`);
}

export function listSourceDiscoveryQuality(query: { group_by?: "topic" | "domain"; limit?: number } = {}) {
  const params = new URLSearchParams();
  params.set("group_by", query.group_by ?? "topic");
  params.set("limit", String(query.limit ?? 20));
  return apiFetch<SourceDiscoveryQualityRow[]>(`/api/source-discovery/quality?${params.toString()}`);
}

export function listQueryMemory(query: { limit?: number; status?: string } = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 20));
  if (query.status) params.set("status", query.status);
  return apiFetch<QueryMemoryRow[]>(`/api/source-discovery/query-memory?${params.toString()}`);
}

export function getSourceDiscoveryEvaluation(limit = 500) {
  return apiFetch<SourceDiscoveryEvaluation>(`/api/source-discovery/evaluation?limit=${limit}`);
}

export function getSourceDiscoveryReadiness() {
  return apiFetch<SourceDiscoveryReadiness>("/api/source-discovery/readiness");
}

export function createSource(payload: CreateSourcePayload) {
  return apiFetch<{ ok: boolean; id: number }>("/api/sources", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateSource(sourceId: number, payload: SourcePatch) {
  return apiFetch<{ ok: boolean }>(`/api/sources/${sourceId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function diagnoseSource(sourceId: number, payload: SourcePatch) {
  return apiFetch<SourceDiagnostics>(`/api/sources/${sourceId}/diagnose?limit=5`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function diagnoseSourceJob(sourceId: number, payload: SourcePatch) {
  return apiFetch<{ ok: boolean; job: BackgroundJob }>(`/api/sources/${sourceId}/diagnose?limit=5&background=true`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function scrapeSource(sourceId: number) {
  return apiFetch<ScrapeResponse>(`/api/sources/${sourceId}/scrape`, {
    method: "POST",
  });
}

export function scrapeSourceJob(sourceId: number) {
  return apiFetch<{ ok: boolean; job: BackgroundJob }>(`/api/sources/${sourceId}/scrape?background=true`, {
    method: "POST",
  });
}

export function importArticleByUrl(payload: ManualArticleImportPayload) {
  return apiFetch<ManualArticleImportResult>("/api/articles/import", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// Архив источника: не опрашивается И его статьи уходят из ленты. Жёсткого удаления нет
// намеренно — articles.source_id ссылается на sources БЕЗ ON DELETE, Postgres откажет.
export function archiveSource(sourceId: number) {
  return apiFetch<{ ok: boolean; id: number; name: string; archived: boolean }>(
    `/api/sources/${sourceId}/archive`,
    { method: "POST" },
  );
}

export function unarchiveSource(sourceId: number) {
  return apiFetch<{ ok: boolean; id: number; name: string; archived: boolean }>(
    `/api/sources/${sourceId}/unarchive`,
    { method: "POST" },
  );
}
