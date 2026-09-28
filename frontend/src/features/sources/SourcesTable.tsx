import { Fragment } from "react";
import type { ReactNode } from "react";
import type { Source, SourceDiagnostics, SourceHealth } from "../../api/types";
import styles from "./Sources.module.css";
import { lastLoadLabel, sourceProblem, sourceState, sourceStateLabel, sourceStateTone, strategyLabel } from "./sourceUtils";

type Props = {
  sources: Source[];
  healthById: Map<number, SourceHealth>;
  diagnostics: Record<number, SourceDiagnostics>;
  // Когда загружены состояния: «N дн. назад» и «Проблема» считаются от него, а не от перерисовки.
  loadedAt: Date;
  expandedId: number | null;
  onToggle: (sourceId: number) => void;
  renderDetails: (source: Source) => ReactNode;
};

// Таблица по документу заказчика (19.09): «экран выглядит очень административно —
// перейти к таблице». Колонки — его. Всё, что было на карточке (сбор вкл/выкл,
// настройка, диагностика, архив, обратная связь), — в раскрытой строке.
export function SourcesTable({ sources, healthById, diagnostics, loadedAt, expandedId, onToggle, renderDetails }: Props) {
  return (
    <div className="jobsTableWrap">
      <table className={`jobsTable ${styles.table}`}>
        <thead>
          <tr>
            <th scope="col">Источник</th>
            <th scope="col">Тип</th>
            <th scope="col">Состояние</th>
            <th scope="col">Последняя загрузка</th>
            <th scope="col" className={styles.num}>Материалов за 30 дней</th>
            <th scope="col">Проблема</th>
            <th scope="col" aria-label="Настройка" />
          </tr>
        </thead>
        <tbody>
          {sources.map((source) => {
            const health = healthById.get(source.id);
            const state = sourceState(source, health);
            const last = lastLoadLabel(health?.last_article_at, loadedAt);
            const expanded = expandedId === source.id;
            const primaryUrl = source.url || source.rss_url || source.listing_url || "";
            return (
              <Fragment key={source.id}>
                <tr id={`source-${source.id}`} className={expanded ? styles.rowExpanded : undefined}>
                  <td className={styles.nameCell}>
                    <div className={styles.name}>{source.name}</div>
                    {primaryUrl ? (
                      <a className={styles.link} href={primaryUrl} target="_blank" rel="noreferrer">
                        {hostOf(primaryUrl)}
                      </a>
                    ) : null}
                  </td>
                  <td className={styles.nowrap}>{strategyLabel(source.parse_strategy)}</td>
                  <td className={styles.nowrap}>
                    <span className={`miniPill ${sourceStateTone(state)}`}>{sourceStateLabel(state)}</span>
                  </td>
                  <td>
                    {last ? (
                      <>
                        <span>{last.date}</span> <span className={styles.muted}>{last.ago}</span>
                      </>
                    ) : (
                      <span className={styles.muted}>—</span>
                    )}
                  </td>
                  <td className={styles.num}>{health?.articles_30d ?? "—"}</td>
                  <td className={styles.problem}>{sourceProblem(source, health, diagnostics[source.id], loadedAt)}</td>
                  <td className={styles.actionCell}>
                    <button
                      type="button"
                      className="ghostButton compactButton"
                      aria-expanded={expanded}
                      aria-controls={`source-details-${source.id}`}
                      aria-label={`${expanded ? "Свернуть" : "Настроить"}: ${source.name}`}
                      onClick={() => onToggle(source.id)}
                    >
                      {expanded ? "Свернуть" : "Настроить"}
                    </button>
                  </td>
                </tr>
                {expanded ? (
                  <tr className={styles.detailsRow}>
                    <td colSpan={7} id={`source-details-${source.id}`}>
                      {renderDetails(source)}
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function hostOf(url: string) {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}
