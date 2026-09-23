// Графики экрана «Статистика» — простой SVG без библиотек. Правила (скилл dataviz):
// столбец ≤24px с закруглённым верхом и прямым основанием, 2px зазор цвета фона между
// сегментами стопки, линии 2px, точки r=4 с кольцом фона, сетка — сплошной волосок,
// легенда при 2+ сериях, подписи выборочно, подсказка по наведению и таблица-двойник.
import { useState } from "react";
import type { ReactNode } from "react";
import { niceTicks } from "./analytics";
import type { Delta } from "./analytics";
import styles from "./Statistics.module.css";

export type Category = { key: string; label: string; partial?: boolean };
export type Series = { key: string; label: string; color: string; values: (number | null)[] };
type Format = (value: number | null) => string;

const W = 560;
const H = 210;
const M = { top: 14, right: 16, bottom: 26, left: 48 };

function roundedTop(x: number, y: number, w: number, h: number, r = 4): string {
  const rr = Math.max(0, Math.min(r, w / 2, h));
  return `M${x},${y + h}L${x},${y + rr}Q${x},${y} ${x + rr},${y}L${x + w - rr},${y}Q${x + w},${y} ${x + w},${y + rr}L${x + w},${y + h}Z`;
}

export function Legend({ items }: { items: { label: string; color: string; line?: boolean }[] }) {
  if (items.length < 2) return null;
  return (
    <div className={styles.legend}>
      {items.map((item) => (
        <span key={item.label} className={styles.legendItem}>
          <span className={item.line ? styles.legendLine : styles.legendBox} style={{ background: item.color }} />
          {item.label}
        </span>
      ))}
    </div>
  );
}

function ChartTable({ categories, series, format }: { categories: Category[]; series: Series[]; format: Format }) {
  return (
    <details className={styles.tableToggle}>
      <summary>Показать таблицей</summary>
      <table className={styles.dataTable}>
        <thead>
          <tr>
            <th>Месяц</th>
            {series.map((s) => <th key={s.key}>{s.label}</th>)}
          </tr>
        </thead>
        <tbody>
          {categories.map((c, i) => (
            <tr key={c.key}>
              <td>{c.label}{c.partial ? "*" : ""}</td>
              {series.map((s) => <td key={s.key}>{format(s.values[i])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

function Tooltip({ x, title, rows }: { x: number; title: string; rows: { label: string; value: string; color: string }[] }) {
  // Подсказка — дополнение, а не единственный путь к числу: всё есть в таблице-двойнике.
  const left = `${Math.min(78, Math.max(4, (x / W) * 100 - 10))}%`;
  return (
    <div className={styles.tooltip} style={{ left }} role="status">
      <strong>{title}</strong>
      {rows.map((row) => (
        <span key={row.label} className={styles.tooltipRow}>
          <span className={styles.legendBox} style={{ background: row.color }} />
          {row.label}: <b>{row.value}</b>
        </span>
      ))}
    </div>
  );
}

function YAxis({ ticks, y, format }: { ticks: number[]; y: (v: number) => number; format: Format }) {
  return (
    <g>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={M.left} x2={W - M.right} y1={y(t)} y2={y(t)} className={t === 0 ? styles.axis : styles.grid} />
          <text x={M.left - 6} y={y(t) + 4} textAnchor="end" className={styles.tick}>{format(t)}</text>
        </g>
      ))}
    </g>
  );
}

function Reference({ value, label, y }: { value: number; label: string; y: (v: number) => number }) {
  return (
    <g>
      <line x1={M.left} x2={W - M.right} y1={y(value)} y2={y(value)} className={styles.reference} />
      {/* Подпись — слева: справа, у последнего месяца, её перекрывают концы линий и столбцы. */}
      <text x={M.left + 4} y={y(value) - 5} textAnchor="start" className={styles.referenceLabel}>{label}</text>
    </g>
  );
}

type ChartProps = {
  title: string;
  categories: Category[];
  series: Series[];
  format: Format;
  axisFormat?: Format;
  reference?: { value: number; label: string };
  footnote?: ReactNode;
};

/** Столбцы, при нескольких сериях — стопкой (часть целого за месяц). */
export function ColumnChart({ title, categories, series, format, axisFormat, reference, footnote }: ChartProps) {
  const [active, setActive] = useState<number | null>(null);
  const totals = categories.map((_, i) => series.reduce((sum, s) => sum + (s.values[i] ?? 0), 0));
  const ticks = niceTicks(Math.max(...totals, reference?.value ?? 0, 1));
  const top = ticks[ticks.length - 1];
  const plotH = H - M.top - M.bottom;
  const y = (v: number) => M.top + plotH - (v / top) * plotH;
  const band = (W - M.left - M.right) / Math.max(categories.length, 1);
  const bw = Math.min(24, band * 0.5);
  const last = categories.length - 1;
  return (
    <figure className={styles.chart}>
      <figcaption className={styles.chartTitle}>{title}</figcaption>
      <Legend items={series.map((s) => ({ label: s.label, color: s.color }))} />
      <div className={styles.plot}>
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title}>
          <YAxis ticks={ticks} y={y} format={axisFormat ?? format} />
          {categories.map((c, i) => {
            const x = M.left + band * i + (band - bw) / 2;
            let base = 0;
            let topIndex = -1;
            series.forEach((s, si) => { if ((s.values[i] ?? 0) > 0) topIndex = si; });
            return (
              <g key={c.key} opacity={c.partial ? 0.6 : 1}>
                {series.map((s, si) => {
                  const v = s.values[i] ?? 0;
                  if (v <= 0) return null;
                  const y0 = y(base);
                  const y1 = y(base + v);
                  const gap = base > 0 ? 2 : 0;
                  base += v;
                  const h = Math.max(0, y0 - y1 - gap);
                  return si === topIndex
                    ? <path key={s.key} d={roundedTop(x, y1, bw, h)} fill={s.color} />
                    : <rect key={s.key} x={x} y={y1} width={bw} height={h} fill={s.color} />;
                })}
                <text x={x + bw / 2} y={H - 8} textAnchor="middle" className={styles.tick}>
                  {c.label}{c.partial ? "*" : ""}
                </text>
                {i === last && totals[i] > 0 && (
                  <text x={x + bw / 2} y={y(totals[i]) - 6} textAnchor="middle" className={styles.valueLabel}>
                    {format(totals[i])}
                  </text>
                )}
                <rect x={M.left + band * i} y={M.top} width={band} height={plotH} fill="transparent"
                  onMouseEnter={() => setActive(i)} onMouseLeave={() => setActive(null)} />
              </g>
            );
          })}
          {reference && <Reference value={reference.value} label={reference.label} y={y} />}
        </svg>
        {active != null && (
          <Tooltip
            x={M.left + band * active + band / 2}
            title={`${categories[active].label}${categories[active].partial ? " (месяц идёт)" : ""}`}
            rows={series.map((s) => ({ label: s.label, value: format(s.values[active]), color: s.color }))}
          />
        )}
      </div>
      {footnote && <p className={styles.footnote}>{footnote}</p>}
      <ChartTable categories={categories} series={series} format={format} />
    </figure>
  );
}

/** Линии — динамика нескольких однородных мер на одной оси. */
export function LineChart({ title, categories, series, format, axisFormat, reference, footnote }: ChartProps) {
  const [active, setActive] = useState<number | null>(null);
  const values = series.flatMap((s) => s.values.filter((v): v is number => v != null));
  const ticks = niceTicks(Math.max(...values, reference?.value ?? 0, 1));
  const top = ticks[ticks.length - 1];
  const plotH = H - M.top - M.bottom;
  const y = (v: number) => M.top + plotH - (v / top) * plotH;
  const band = (W - M.left - M.right) / Math.max(categories.length, 1);
  const x = (i: number) => M.left + band * i + band / 2;
  const last = categories.length - 1;
  return (
    <figure className={styles.chart}>
      <figcaption className={styles.chartTitle}>{title}</figcaption>
      <Legend items={series.map((s) => ({ label: s.label, color: s.color, line: true }))} />
      <div className={styles.plot}>
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title}>
          <YAxis ticks={ticks} y={y} format={axisFormat ?? format} />
          {reference && <Reference value={reference.value} label={reference.label} y={y} />}
          {categories.map((c, i) => (
            <text key={c.key} x={x(i)} y={H - 8} textAnchor="middle" className={styles.tick}>
              {c.label}{c.partial ? "*" : ""}
            </text>
          ))}
          {active != null && <line x1={x(active)} x2={x(active)} y1={M.top} y2={M.top + plotH} className={styles.crosshair} />}
          {series.map((s) => {
            const points = s.values.map((v, i) => (v == null ? null : `${x(i)},${y(v)}`)).filter(Boolean).join(" ");
            const lastValue = s.values[last];
            return (
              <g key={s.key}>
                <polyline points={points} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
                {lastValue != null && <circle cx={x(last)} cy={y(lastValue)} r={4} fill={s.color} stroke="#ffffff" strokeWidth={2} />}
              </g>
            );
          })}
          {categories.map((c, i) => (
            <rect key={c.key} x={M.left + band * i} y={M.top} width={band} height={plotH} fill="transparent"
              onMouseEnter={() => setActive(i)} onMouseLeave={() => setActive(null)} />
          ))}
        </svg>
        {active != null && (
          <Tooltip
            x={x(active)}
            title={`${categories[active].label}${categories[active].partial ? " (месяц идёт)" : ""}`}
            rows={series.map((s) => ({ label: s.label, value: format(s.values[active]), color: s.color }))}
          />
        )}
      </div>
      {footnote && <p className={styles.footnote}>{footnote}</p>}
      <ChartTable categories={categories} series={series} format={format} />
    </figure>
  );
}

export type BarRow = { key: string; label: string; value: number; note?: ReactNode };

/** Горизонтальные полосы: воронка, темы, источники. Одна серия — один цвет. */
export function BarList({ rows, color, format, max }: { rows: BarRow[]; color: string; format: Format; max?: number }) {
  const top = max ?? Math.max(...rows.map((r) => r.value), 1);
  return (
    <div className={styles.barList}>
      {rows.map((row) => (
        <div key={row.key} className={styles.barRow}>
          <span className={styles.barLabel} title={row.label}>{row.label}</span>
          <span className={styles.barTrack}>
            <span className={styles.barFill} style={{ width: `${Math.max(0.5, (row.value / top) * 100)}%`, background: color }} />
          </span>
          <span className={styles.barValue}>{format(row.value)}</span>
          <span className={styles.barNote}>{row.note}</span>
        </div>
      ))}
    </div>
  );
}

/** Одна полоса «часть целого» (язык потока): 2px зазор между частями, легенда рядом. */
export function SplitBar({ parts, format }: { parts: { label: string; value: number; color: string }[]; format: Format }) {
  const total = parts.reduce((sum, p) => sum + p.value, 0) || 1;
  return (
    <div>
      <div className={styles.split}>
        {parts.map((p) => (
          <span key={p.label} style={{ flexGrow: p.value / total, background: p.color }} title={`${p.label}: ${format(p.value)}`} />
        ))}
      </div>
      <Legend items={parts.map((p) => ({ label: `${p.label} · ${format(p.value)}`, color: p.color }))} />
    </div>
  );
}

/** Тренд за полные месяцы: приглушённая линия, последняя точка — акцентом. */
export function Sparkline({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const w = 96;
  const h = 28;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const px = (i: number) => 2 + (i / (values.length - 1)) * (w - 4);
  const py = (v: number) => h - 3 - ((v - min) / (max - min || 1)) * (h - 6);
  const last = values.length - 1;
  return (
    <svg className={styles.spark} viewBox={`0 0 ${w} ${h}`} aria-hidden="true">
      <polyline points={values.map((v, i) => `${px(i)},${py(v)}`).join(" ")} fill="none"
        stroke="var(--viz-context-ink)" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={px(last)} cy={py(values[last])} r={3} fill="var(--viz-s1)" stroke="#ffffff" strokeWidth={1.5} />
    </svg>
  );
}

/** Плитка показателя: подпись · значение · дельта к названной базе · тренд. */
export function StatTile({ label, value, sub, change, base, spark }: {
  label: string;
  value: string;
  sub?: ReactNode;
  change?: Delta | null;
  base?: string;
  spark?: number[];
}) {
  return (
    <article className={styles.tile}>
      <span className={styles.tileLabel}>{label}</span>
      <span className={styles.tileValue}>{value}</span>
      {sub && <span className={styles.tileSub}>{sub}</span>}
      {(change !== undefined || spark) && (
        <span className={styles.tileFoot}>
          {/* База сравнения названа строкой над плитками; здесь — в подсказке и для чтеца. */}
          {change ? (
            <span className={styles[`delta_${change.tone}`]} title={base ? `Сравнение ${base}` : undefined}>
              <span aria-hidden="true">{change.arrow}</span> {change.text}
              {base && <span className={styles.srOnly}> {base}</span>}
            </span>
          ) : change === null ? <span className={styles.deltaBase}>нет базы для сравнения</span> : <span />}
          {spark && <Sparkline values={spark} />}
        </span>
      )}
    </article>
  );
}
