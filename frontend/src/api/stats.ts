import { apiFetch } from "./client";
import type { DashboardStats, FeedWindowPayload, MonthlyAnalytics, MonthlyStats } from "./types";

// Счётчики ленты по тому же окну, что и сама лента; с `month` — за этот месяц (архив).
export function getDashboardStats(month?: string) {
  return apiFetch<DashboardStats>(month ? `/api/stats?month=${encodeURIComponent(month)}` : "/api/stats");
}

// Открытые месяцы окна и прошлые месяцы для переключателя «Архив».
export function getFeedWindow() {
  return apiFetch<FeedWindowPayload>("/api/feed-window");
}

export function getMonthlyStats(months = 6) {
  return apiFetch<MonthlyStats>(`/api/stats/monthly?months=${months}`);
}

export function getMonthlyAnalytics(months = 6) {
  return apiFetch<MonthlyAnalytics>(`/api/analytics/monthly?months=${months}`);
}
