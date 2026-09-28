import type { Source, SourceDiagnostics, SourceHealth, SourcePatch } from "../../api/types";
import { FeedbackPanel } from "../feedback/FeedbackPanel";
import { diagnosticText, diagnosticVerdictClass, diagnosticVerdictLabel, getSourceTriage } from "./sourceUtils";

type Props = {
  source: Source;
  health?: SourceHealth;
  diagnostic?: SourceDiagnostics;
  hasDraft: boolean;
  pending?: boolean;
  pendingLabel?: string | null;
  focused?: boolean;
  currentField: (field: keyof SourcePatch) => string;
  onDraftChange: (field: keyof SourcePatch, value: string | null) => void;
  onToggle: (enabled: boolean) => void;
  onSave: () => void;
  onDiagnose: () => void;
  onScrape: () => void;
  onArchive: () => void;
  onUnarchive: () => void;
};

// Раскрытая строка таблицы источников. Имя, ссылка, тип и состояние уже видны в самой
// строке — здесь только то, что нужно, чтобы с источником что-то сделать.
export function SourceDetails(props: Props) {
  const { source, health, diagnostic, hasDraft, pending, pendingLabel, focused } = props;
  const primaryUrl = source.url || source.rss_url || source.listing_url || "";
  const triage = getSourceTriage(source, health, diagnostic);

  return (
    <article className={focused ? "sourceCardReact focused" : "sourceCardReact"}>
      <div className="sourceTop">
        <div className="sourceSummary">
          <div className="sourceLink">
            {primaryUrl ? (
              <a href={primaryUrl} target="_blank" rel="noreferrer">
                {primaryUrl}
              </a>
            ) : (
              "Ссылка не задана"
            )}
          </div>
          <div className="sourceMeta">
            <span className="miniPill muted">{Number(health?.articles || 0)} материалов всего</span>
            {hasDraft ? <span className="miniPill draft">есть правки</span> : null}
            {pendingLabel ? (
              <span className="miniPill info pendingPill">
                <span className="loaderDot" />
                {pendingLabel}
              </span>
            ) : null}
          </div>
        </div>
        {source.archived_at ? null : (
          <label className="toggleLabel">
            <input
              type="checkbox"
              aria-label={`Сбор: ${source.name}`}
              checked={source.enabled}
              onChange={(event) => props.onToggle(event.target.checked)}
            />
            <span>{source.enabled ? "сбор вкл" : "сбор выкл"}</span>
          </label>
        )}
      </div>

      <section className={`sourceTriagePanel ${triage.tone}`}>
        <div className="sourceTriageHead">
          <span className={`miniPill ${triage.tone}`}>{triage.label}</span>
          {diagnostic?.verdict ? <span className="metaText">диагностика: {diagnosticVerdictLabel(diagnostic.verdict)}</span> : null}
        </div>
        <div className="sourceTriageTitle">{triage.title}</div>
        <div className="sourceTriageAction">{triage.action}</div>
      </section>

      <div className="sourceActions">
        <button type="button" className="ghostButton" disabled={!hasDraft || pending} onClick={props.onSave}>
          Сохранить
        </button>
        <button type="button" className="ghostButton" disabled={pending} onClick={props.onDiagnose}>
          Диагностика
        </button>
        {source.parse_strategy === "request" || source.parse_strategy === "playwright" ? (
          <button type="button" className="ghostButton" disabled={pending} onClick={props.onScrape}>
            Проверить страницу новостей
          </button>
        ) : null}
        {source.archived_at ? (
          <button type="button" className="ghostButton" disabled={pending} onClick={props.onUnarchive}>
            Вернуть из архива
          </button>
        ) : (
          <button type="button" className="ghostButton danger" disabled={pending} onClick={props.onArchive}>
            В архив
          </button>
        )}
        {source.last_seen_published_at ? (
          <span className="metaText">последний найденный материал {String(source.last_seen_published_at).slice(0, 10)}</span>
        ) : null}
      </div>

      <details className="sourceFeedbackReact">
        <summary>Обратная связь по источнику</summary>
        {/* Шкала перевода тут не нужна — она про конкретный текст, а не про источник. */}
        <FeedbackPanel sourceId={source.id} withTranslation={false} />
      </details>

      <details className="sourceAdvancedReact" open={focused || undefined}>
        <summary>{`Настройка ${source.parse_strategy === "request" ? "и диагностика" : ""}`}</summary>
        <div className="sourceConfigGridReact">
          <InputField
            label="Основная ссылка"
            value={props.currentField("url")}
            onChange={(value) => props.onDraftChange("url", value || null)}
            placeholder="https://example.com"
          />
          <InputField
            label="Адрес RSS"
            value={props.currentField("rss_url")}
            onChange={(value) => props.onDraftChange("rss_url", value || null)}
            placeholder="Адрес RSS-ленты"
          />
          {source.parse_strategy === "request" ? (
            <InputField
              label="Ссылка на страницу новостей"
              value={props.currentField("listing_url")}
              onChange={(value) => props.onDraftChange("listing_url", value || null)}
              placeholder="Страница новостей"
            />
          ) : null}
          <label className="field">
            <span>Частота</span>
            <select
              value={props.currentField("update_frequency")}
              onChange={(event) => props.onDraftChange("update_frequency", event.target.value || null)}
            >
              <option value="">Не задана</option>
              <option value="ежечасно">Ежечасно</option>
              <option value="ежедневно">Ежедневно</option>
              <option value="еженедельно">Еженедельно</option>
            </select>
          </label>
        </div>
      </details>
      {diagnostic ? <DiagnosticsPanel diagnostic={diagnostic} /> : null}
    </article>
  );
}

function InputField(props: {
  label: string;
  value: string;
  placeholder: string;
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <label className="field">
      <span>{props.label}</span>
      <input
        value={props.value}
        disabled={props.disabled}
        placeholder={props.placeholder}
        onChange={(event) => props.onChange(event.target.value)}
      />
    </label>
  );
}

function DiagnosticsPanel({ diagnostic }: { diagnostic: SourceDiagnostics }) {
  return (
    <div className="diagnosticsPanel">
      <div className="diagnosticsHeader">
        <strong>Диагностика</strong>
        <span className={`miniPill ${diagnosticVerdictClass(diagnostic.verdict)}`}>{diagnosticVerdictLabel(diagnostic.verdict)}</span>
      </div>
      <pre>{diagnosticText(diagnostic)}</pre>
    </div>
  );
}
