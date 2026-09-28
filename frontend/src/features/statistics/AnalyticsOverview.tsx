// Обзор экрана «Статистика»: что платформа дала за месяц — глазами заказчика.
// Опора — презентация ГД (июль 2026): сквозной пайплайн (слайд 4), >120 источников
// (слайд 5), раннее выявление (слайды 3, 10), бюджет ИИ ~10 000 ₽/мес (слайд 7).
import type { AnalyticsCost, AnalyticsMonth, MonthlyAnalytics } from "../../api/types";
import {
  comparisonBase, delta, formatDecimal, formatHours, formatInt, formatRub, formatShare,
  monthGenitive, monthLabel, monthShort, share,
} from "./analytics";
import { BarList, ColumnChart, LineChart, SplitBar, StatTile } from "./charts";
import type { Category } from "./charts";
import styles from "./Statistics.module.css";

type Props = { data: MonthlyAnalytics; months: AnalyticsMonth[]; month: string };

const FUNNEL: { key: keyof AnalyticsMonth; label: string }[] = [
  { key: "collected", label: "Собрано из источников" },
  { key: "relevant", label: "Релевантно — прошло отсев шума" },
  { key: "summarized", label: "Суть и перевод готовы" },
  { key: "strong", label: "Сильный сигнал — балл ≥60" },
  { key: "top", label: "Топ — балл ≥70" },
  { key: "digest_selected", label: "Отобрано в дайджест" },
];

export function AnalyticsOverview({ data, months, month }: Props) {
  const row = months.find((m) => m.month === month) ?? months[months.length - 1];
  const base = comparisonBase(data, row.month);
  const complete = months.filter((m) => m.complete);
  const spark = (key: keyof AnalyticsMonth) => complete.map((m) => Number(m[key] ?? 0));
  const partial = !row.complete;
  const cost = data.ai_cost?.find((c) => c.month === row.month);
  const costBase = partial ? data.ai_cost_previous_same_period : data.ai_cost?.find((c) => c.month === base?.row.month);
  // Рубли — по курсу ЦБ своего месяца (приходит с сервера у каждой строки затрат).
  const rubOf = (c?: AnalyticsCost) => (c ? c.cost_usd * c.usd_rub : null);
  const rubPerArticleOf = (c?: AnalyticsCost) => (c && c.articles ? (c.cost_usd * c.usd_rub) / c.articles : null);
  const costOf = (key: string) => data.ai_cost?.find((c) => c.month === key);
  const rub = rubOf(cost);
  const rubPerArticle = rubPerArticleOf(cost);
  const rubPerArticleBase = rubPerArticleOf(costBase);
  const assumed = data.ai_cost?.filter((c) => c.usd_rub_source !== "ЦБ РФ") ?? [];
  const lastRate = data.ai_cost?.[data.ai_cost.length - 1];
  const budgetPerArticle = data.targets.ai_rub_month / data.targets.articles_month;
  const categories: Category[] = months.map((m) => ({ key: m.month, label: monthShort(m.month), partial: !m.complete }));
  const themes = data.themes.filter((t) => t.month === row.month);
  const themed = themes.reduce((sum, t) => sum + t.relevant, 0);
  const sources = data.top_sources.filter((s) => s.month === row.month);
  const period = partial ? `${monthLabel(row.month)} · по ${data.current_day} ${monthGenitive(row.month)}` : monthLabel(row.month);

  return (
    <>
      <section className={styles.hero}>
        <p className={styles.heroLine}>
          <b>{period}:</b> из {formatInt(row.collected)} собранных материалов {formatInt(row.relevant)} релевантных
          ({formatShare(share(row.relevant, row.collected))}) и <b>{formatInt(row.strong)} сильных сигналов</b> с баллом ≥60 —
          это {formatShare(share(row.strong, row.collected))} потока. Остальное платформа отсеяла как шум или оценила ниже порога.
        </p>
        <p className={styles.heroNote}>
          {base ? `Сравнение — ${base.label}${partial ? ": незаконченный месяц сравнивается с теми же днями прошлого." : "."}` : "Прошлого месяца для сравнения нет."}
          {" "}Тренды в плитках — по полным месяцам.
        </p>
      </section>

      <section className={styles.kpiGrid}>
        <StatTile label="Собрано материалов" value={formatInt(row.collected)}
          sub={`${formatShare(share(row.en, row.collected))} — англоязычные источники`}
          change={delta(row.collected, base?.row.collected ?? null, null)} base={base?.label} spark={spark("collected")} />
        <StatTile label="Релевантных" value={formatInt(row.relevant)}
          sub={`${formatShare(share(row.relevant, row.collected))} потока прошло отсев шума`}
          change={delta(row.relevant, base?.row.relevant ?? null, true)} base={base?.label} spark={spark("relevant")} />
        <StatTile label="Сильные сигналы · балл ≥60" value={formatInt(row.strong)}
          sub={`из них ${formatInt(row.top)} с баллом ≥70`}
          change={delta(row.strong, base?.row.strong ?? null, true)} base={base?.label} spark={spark("strong")} />
        <StatTile label="Время до сигнала" value={formatHours(row.speed_p50_hours)}
          sub={`медиана от публикации до балла · 90% — до ${formatHours(row.speed_p90_hours)}`}
          change={delta(row.speed_p50_hours, base?.row.speed_p50_hours ?? null, false)} base={base?.label}
          spark={complete.map((m) => m.speed_p50_hours ?? 0)} />
        {data.ai_cost && (
          <StatTile label="ИИ-обработка" value={formatRub(rub)}
            sub={`${rubPerArticle == null ? "—" : `${formatDecimal(rubPerArticle, 2)} ₽`} за статью · бюджет ${formatDecimal(budgetPerArticle, 0)} ₽`}
            change={delta(rubPerArticle, rubPerArticleBase, false)} base={base ? `за статью ${base.label}` : undefined}
            spark={complete.map((m) => rubOf(costOf(m.month)) ?? 0)} />
        )}
      </section>

      <section className={styles.grid2}>
        <div className={styles.card}>
          <h3 className={styles.cardTitle}>Сквозной пайплайн месяца: от источника до дайджеста</h3>
          <p className={styles.cardNote}>
            Из 100 собранных материалов аналитик видит {formatShare(share(row.relevant, row.collected)).replace("%", "")} релевантных,
            из них {formatShare(share(row.strong, row.collected)).replace("%", "")} — сильные сигналы. Отсев и оценку делает ИИ.
          </p>
          <BarList
            color="var(--viz-s1)"
            format={formatInt}
            max={row.collected}
            rows={FUNNEL.map((stage) => {
              const value = Number(row[stage.key] ?? 0);
              return { key: String(stage.key), label: stage.label, value, note: formatShare(share(value, row.collected)) };
            })}
          />
          <h3 className={styles.cardTitle}>Язык потока</h3>
          <SplitBar format={formatInt} parts={[
            { label: "Русскоязычные", value: row.collected - row.en, color: "var(--viz-s1)" },
            { label: "Англоязычные", value: row.en, color: "var(--viz-s2)" },
          ]} />
        </div>
        <div className={styles.card}>
          <ColumnChart
            title="Поток и отсев шума по месяцам"
            categories={categories}
            format={formatInt}
            series={[
              { key: "relevant", label: "Релевантно", color: "var(--viz-s1)", values: months.map((m) => m.relevant) },
              { key: "rejected", label: "Отсеяно как шум", color: "var(--viz-context)", values: months.map((m) => m.rejected) },
            ]}
            footnote={partial ? `* ${monthLabel(row.month)} — по ${data.current_day} ${monthGenitive(row.month)}.` : undefined}
          />
        </div>
      </section>

      <section className={styles.grid2}>
        <div className={styles.card}>
          <ColumnChart
            title="Сильные сигналы по месяцам"
            categories={categories}
            format={formatInt}
            series={[
              { key: "strong", label: "Балл 60–69", color: "var(--viz-tier-light)", values: months.map((m) => m.strong - m.top) },
              { key: "top", label: "Балл ≥70", color: "var(--viz-tier-dark)", values: months.map((m) => m.top) },
            ]}
          />
        </div>
        <div className={styles.card}>
          <LineChart
            title="Источники в работе"
            categories={categories}
            format={formatInt}
            reference={{ value: data.targets.sources, label: `цель: >${data.targets.sources} источников` }}
            series={[
              { key: "active", label: "Дали материалы", color: "var(--viz-s1)", values: months.map((m) => m.sources_active) },
              { key: "relevant", label: "Дали релевантное", color: "var(--viz-s2)", values: months.map((m) => m.sources_relevant) },
              { key: "strong", label: "Дали сильный сигнал", color: "var(--viz-s3)", values: months.map((m) => m.sources_strong) },
            ]}
          />
        </div>
      </section>

      <section className={styles.grid2}>
        <div className={styles.card}>
          <h3 className={styles.cardTitle}>Темы релевантных материалов · {monthLabel(row.month).toLowerCase()}</h3>
          {themes.length ? (
            // Дельты тем по месяцам не показываем до перетегирования: до 13.09 статьи
            // размечены прежними направлениями, и «рост темы» был бы ростом разметки.
            <BarList color="var(--viz-s1)" format={formatInt} rows={themes.map((t) => ({
              key: String(t.tag_id), label: t.tag, value: t.relevant, note: `сильных ${formatInt(t.strong)}`,
            }))} />
          ) : <p className={styles.cardNote}>По 13 темам заказчика этот месяц ещё не размечен.</p>}
          {row.relevant - themed > 0 && (
            <p className={styles.cardNote}>
              Ещё {formatInt(row.relevant - themed)} релевантных размечены прежними направлениями (до 13.09) — после
              перетегирования корпуса они войдут в эти 13 тем.
            </p>
          )}
        </div>
        <div className={styles.card}>
          <h3 className={styles.cardTitle}>Откуда сильные сигналы · {monthLabel(row.month).toLowerCase()}</h3>
          <BarList color="var(--viz-s1)" format={formatInt} rows={sources.map((s) => ({
            key: String(s.source_id), label: s.source, value: s.strong,
            note: `из ${formatInt(s.relevant)} релевантных`,
          }))} />
        </div>
      </section>

      <section className={styles.grid2}>
        {data.ai_cost && (
          <div className={styles.card}>
            <ColumnChart
              title="ИИ-обработка по месяцам, ₽"
              categories={categories}
              format={formatRub}
              axisFormat={(v) => formatInt(v)}
              reference={{ value: data.targets.ai_rub_month, label: `бюджет ${formatInt(data.targets.ai_rub_month)} ₽/мес` }}
              series={[{ key: "cost", label: "ИИ-обработка", color: "var(--viz-s1)",
                values: months.map((m) => rubOf(costOf(m.month)) ?? 0) }]}
              footnote={<>
                Затраты в долларах пересчитаны по курсу ЦБ РФ на последний день каждого месяца
                {lastRate?.usd_rub_date ? `, текущий — ${formatDecimal(lastRate.usd_rub, 2)} ₽/$ на ${lastRate.usd_rub_date.split("-").reverse().join(".")}` : ""}.
                {assumed.length > 0 && ` ЦБ не ответил для: ${assumed.map((c) => monthShort(c.month)).join(", ")} — там запасной курс ${formatDecimal(assumed[0].usd_rub, 0)} ₽/$ (допущение).`}
                {" "}Май–июль занижены: до 23.07 не учитывался гейт по отклонённым статьям; июль — ещё и массовый перепрогон.
              </>}
            />
          </div>
        )}
        <div className={styles.card}>
          <h3 className={styles.cardTitle}>Качество потока · {monthLabel(row.month).toLowerCase()}</h3>
          <div className={styles.qualityGrid}>
            <StatTile label="С полным текстом" value={formatShare(share(row.full_text, row.collected))}
              sub={`${formatInt(row.full_text)} из ${formatInt(row.collected)}`} />
            <StatTile label="Перепечаток скрыто" value={formatInt(row.reprints)} sub="одна новость — одна карточка" />
            <StatTile label="Скрыто как шум" value={formatInt(row.hidden)} sub="помечено к удалению" />
            <StatTile label="Выгрузок дайджеста" value={formatInt(row.digest_exports)} sub="PDF, DOCX, HTML" />
          </div>
        </div>
      </section>
    </>
  );
}
