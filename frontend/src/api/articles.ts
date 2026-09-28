import { apiFetch } from "./client";
import type { Article, ArticlePatch } from "./types";

// Каталог грузится одним запросом и фильтруется на клиенте. 5000 строк со всеми
// summary/score_items — это мегабайты JSON на каждый вход, что особенно бьёт при
// доступе через VPN/из-за рубежа. Берём топ по score: для редакторской работы
// нижние по релевантности сигналы в каталоге практически не нужны.
export const DEFAULT_ARTICLE_LIMIT = 2000;

export type ArticleQuery = {
  limit?: number;
  search?: string;
  source?: string;
  tag?: string;
  status?: string;
  language?: string;
  minScore?: number;
  maxScore?: number;
  dateFrom?: string;
  dateTo?: string;
  sort?: "date_desc" | "score_desc" | "score_asc";
  changedOnly?: boolean;
  // Один месяц «ГГГГ-ММ». Без него сервер отдаёт открытые месяцы окна; прошлый месяц —
  // архив, только просмотр (правку статуса сервер отклоняет с 409).
  month?: string;
};

// Без фильтров возвращает дефолтный лёгкий топ-2000. С `search` (и др.) запрос
// уходит в Postgres и покрывает ВСЮ базу — это снимает компромисс «поиск только
// по загруженным 2000».
export function listArticles(query: ArticleQuery = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? DEFAULT_ARTICLE_LIMIT));
  if (query.search) params.set("search", query.search);
  if (query.source) params.set("source", query.source);
  if (query.tag) params.set("tag", query.tag);
  if (query.status) params.set("status", query.status);
  if (query.language) params.set("language", query.language);
  if (query.minScore != null) params.set("min_score", String(query.minScore));
  if (query.maxScore != null) params.set("max_score", String(query.maxScore));
  if (query.dateFrom) params.set("date_from", query.dateFrom);
  if (query.dateTo) params.set("date_to", query.dateTo);
  if (query.sort) params.set("sort", query.sort);
  if (query.changedOnly) params.set("changed_only", "1");
  if (query.month) params.set("month", query.month);
  return apiFetch<Article[]>(`/api/articles?${params.toString()}`);
}

export function updateArticle(articleId: number, payload: ArticlePatch) {
  return apiFetch<{ ok: boolean }>(`/api/articles/${articleId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}
