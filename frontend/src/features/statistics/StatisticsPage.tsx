import { useEffect, useMemo, useState } from "react";
import { getMonthlyAnalytics, getMonthlyStats } from "../../api/stats";
import type { MonthlyAnalytics, MonthlyStats } from "../../api/types";
import { AnalyticsOverview } from "./AnalyticsOverview";
import { monthLabel, trimLeadingEmpty } from "./analytics";
import styles from "./Statistics.module.css";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

// Порядок и подписи статусов разметки. Держим здесь один раз: и колонки таблицы,
// и подсчёт итогов идут по этому списку, поэтому новый статус добавляется в одном месте.
const MARK_STATUSES = ["noise", "duplicate", "digest", "archive"] as const;
const MARK_LABELS: Record<string, string> = {
  noise: "Шум",
  duplicate: "Дубликаты",
  digest: "В дайджест",
  archive: "Архив",
};

const PERIODS = [3, 6, 12] as const;

function pct(part: number, total: number): string {
  if (!total) return "—";
  return `${Math.round((100 * part) / total)}%`;
}

export function StatisticsPage({ onUnauthorized, showToast }: Props) {
  const [data, setData] = useState<MonthlyStats | null>(null);
  const [analytics, setAnalytics] = useState<MonthlyAnalytics | null>(null);
  const [months, setMonths] = useState<number>(6);
  const [month, setMonth] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    void reload(months);
  }, [months]);

  async function reload(period: number) {
    try {
      setLoading(true);
      const [stats, overview] = await Promise.all([getMonthlyStats(period), getMonthlyAnalytics(period)]);
      setData(stats);
      setAnalytics(overview);
    } catch (error) {
      if (error instanceof Error && error.message.includes("401")) {
        onUnauthorized();
        return;
      }
      showToast(error instanceof Error ? error.message : "Не удалось загрузить статистику", "error");
    } finally {
      setLoading(false);
    }
  }

  // Стоимость ИИ сворачиваем по месяцам (в ответе строка на каждую модель).
  const costByMonth = useMemo(() => {
    const acc = new Map<string, { runs: number; cost: number; models: string[] }>();
    for (const row of data?.ai_cost ?? []) {
      const cur = acc.get(row.month) ?? { runs: 0, cost: 0, models: [] };
      cur.runs += row.runs;
      cur.cost += Number(row.cost_usd ?? 0);
      if (row.model) cur.models.push(`${row.model.split("-2")[0]} · $${Number(row.cost_usd ?? 0).toFixed(2)}`);
      acc.set(row.month, cur);
    }
    return acc;
  }, [data]);

  // Активность: сводка по пользователю за весь период + строка общего итога.
  // Владелец просил именно так: «статистика по юзеру и общая, в общей всё совместить».
  const activity = useMemo(() => {
    const byUser = new Map<string, Record<string, number>>();
    const total: Record<string, number> = {};
    for (const row of data?.activity ?? []) {
      const key = row.email || `id ${row.user_id}`;
      const bucket = byUser.get(key) ?? {};
      bucket[row.status] = (bucket[row.status] ?? 0) + row.marks;
      byUser.set(key, bucket);
      total[row.status] = (total[row.status] ?? 0) + row.marks;
    }
    const rows = [...byUser.entries()]
      .map(([email, marks]) => ({
        email,
        marks,
        sum: MARK_STATUSES.reduce((acc, s) => acc + (marks[s] ?? 0), 0),
      }))
      .sort((a, b) => b.sum - a.sum);
    const totalSum = MARK_STATUSES.reduce((acc, s) => acc + (total[s] ?? 0), 0);
    return { rows, total, totalSum };
  }, [data]);

  const platform = data?.platform ?? [];
  const grand = useMemo(() => {
    return platform.reduce(
      (acc, r) => ({
        collected: acc.collected + r.collected,
        relevant: acc.relevant + r.relevant,
        rejected: acc.rejected + r.rejected,
        hidden: acc.hidden + r.hidden,
        digest_ready: acc.digest_ready + r.digest_ready,
      }),
      { collected: 0, relevant: 0, rejected: 0, hidden: 0, digest_ready: 0 },
    );
  }, [platform]);

  const totalCost = [...costByMonth.values()].reduce((acc, v) => acc + v.cost, 0);
  const shownMonths = useMemo(() => trimLeadingEmpty(analytics?.months ?? []), [analytics]);
  const selectedMonth = month && shownMonths.some((m) => m.month === month)
    ? month
    : shownMonths[shownMonths.length - 1]?.month ?? null;

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h2>Статистика платформы</h2>
          <p className="muted">Месячные результаты работы платформы и активность пользователей.</p>
        </div>
        <div className="settingsActions">
          {PERIODS.map((p) => (
            <button
              key={p}
              type="button"
              className={p === months ? "primaryButton" : "ghostButton"}
              onClick={() => setMonths(p)}
            >
              {p} мес
            </button>
          ))}
        </div>
      </header>

      {loading && !analytics && <div className="emptyState">Считаем статистику…</div>}
      {analytics && selectedMonth && (
        <div className={styles.viz} style={{ display: "flex", flexDirection: "column", gap: 16, opacity: loading ? 0.6 : 1 }}>
          <div className={styles.chips} role="group" aria-label="Месяц">
            {shownMonths.map((m) => (
              <button key={m.month} type="button" aria-pressed={m.month === selectedMonth}
                className={`${styles.chip} ${m.month === selectedMonth ? styles.chipActive : ""}`}
                onClick={() => setMonth(m.month)}>
                {monthLabel(m.month)}{m.complete ? "" : " · идёт"}
              </button>
            ))}
          </div>
          <AnalyticsOverview data={analytics} months={shownMonths} month={selectedMonth} />
        </div>
      )}

      {data && (
        <>
          <section className="panel">
            <div className="panelHeader">
              <h3>Воронка по месяцам — таблица</h3>
              <div className="statusPill">
                {grand.collected} собрано · {grand.digest_ready} годных
              </div>
            </div>
            <div className="benchmarkTable statsTable">
              <div className="benchmarkRow statsRow benchmarkHead">
                <span>Месяц</span>
                <span>Собрано</span>
                <span>Релевантно</span>
                <span>Отклонено</span>
                <span>Скрыто</span>
                <span>Годных ≥60</span>
                <span>Ср. балл</span>
              </div>
              {platform.map((row) => (
                <div className="benchmarkRow statsRow" key={row.month}>
                  <span>{row.month}</span>
                  <span>{row.collected}</span>
                  <span>
                    {row.relevant} <span className="muted">({pct(row.relevant, row.collected)})</span>
                  </span>
                  <span>{row.rejected}</span>
                  <span>{row.hidden}</span>
                  <span>{row.digest_ready}</span>
                  <span>{row.avg_score ?? "—"}</span>
                </div>
              ))}
              <div className="benchmarkRow statsRow statsTotalRow">
                <span>ИТОГО</span>
                <span>{grand.collected}</span>
                <span>
                  {grand.relevant} <span className="muted">({pct(grand.relevant, grand.collected)})</span>
                </span>
                <span>{grand.rejected}</span>
                <span>{grand.hidden}</span>
                <span>{grand.digest_ready}</span>
                <span>—</span>
              </div>
            </div>
            <p className="muted statsNote">
              «Годных ≥60» — статьи, прошедшие порог отбора в дайджест. Падение этой колонки при
              росте «Собрано» означает, что новые источники приносят объём, а не пользу.
            </p>
          </section>

          <section className="panel">
            <div className="panelHeader">
              <h3>Затраты на ИИ</h3>
              <div className="statusPill">${totalCost.toFixed(2)} за период</div>
            </div>
            <div className="benchmarkTable statsTable">
              <div className="benchmarkRow statsCostRow benchmarkHead">
                <span>Месяц</span>
                <span>Вызовов</span>
                <span>Стоимость</span>
                <span>По моделям</span>
              </div>
              {[...costByMonth.entries()].map(([month, v]) => (
                <div className="benchmarkRow statsCostRow" key={month}>
                  <span>{month}</span>
                  <span>{v.runs}</span>
                  <span>${v.cost.toFixed(2)}</span>
                  <span className="muted">{v.models.join(" · ")}</span>
                </div>
              ))}
            </div>
          </section>

          <section className="panel">
            <div className="panelHeader">
              <h3>Работа пользователей</h3>
              <div className="statusPill">{activity.totalSum} пометок</div>
            </div>
            {activity.rows.length === 0 ? (
              <div className="emptyState">За период никто ничего не размечал.</div>
            ) : (
              <div className="benchmarkTable statsTable">
                <div className="benchmarkRow statsUserRow benchmarkHead">
                  <span>Пользователь</span>
                  {MARK_STATUSES.map((s) => (
                    <span key={s}>{MARK_LABELS[s]}</span>
                  ))}
                  <span>Всего</span>
                </div>
                {activity.rows.map((row) => (
                  <div className="benchmarkRow statsUserRow" key={row.email}>
                    <span>{row.email}</span>
                    {MARK_STATUSES.map((s) => (
                      <span key={s}>{row.marks[s] ?? 0}</span>
                    ))}
                    <span>{row.sum}</span>
                  </div>
                ))}
                <div className="benchmarkRow statsUserRow statsTotalRow">
                  <span>ИТОГО</span>
                  {MARK_STATUSES.map((s) => (
                    <span key={s}>{activity.total[s] ?? 0}</span>
                  ))}
                  <span>{activity.totalSum}</span>
                </div>
              </div>
            )}
          </section>
        </>
      )}
    </section>
  );
}
