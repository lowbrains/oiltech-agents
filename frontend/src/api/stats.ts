import { apiFetch } from "./client";
import type { DashboardStats, MonthlyAnalytics, MonthlyStats } from "./types";

export function getDashboardStats() {
  return apiFetch<DashboardStats>("/api/stats");
}

export function getMonthlyStats(months = 6) {
  return apiFetch<MonthlyStats>(`/api/stats/monthly?months=${months}`);
}

export function getMonthlyAnalytics(months = 6) {
  return apiFetch<MonthlyAnalytics>(`/api/analytics/monthly?months=${months}`);
}
