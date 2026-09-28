import { useEffect, useMemo, useRef, useState } from "react";
import {
  archiveSource,
  createSource,
  diagnoseSourceJob,
  importArticleByUrl,
  listSourceHealth,
  listSources,
  scrapeSourceJob,
  unarchiveSource,
  updateSource,
} from "../../api/sources";
import type {
  CreateSourcePayload,
  ManualArticleImportPayload,
  Source,
  SourceDiagnostics,
  SourceHealth,
  SourcePatch,
} from "../../api/types";
import styles from "./Sources.module.css";
import { SourceAddPanel } from "./SourceAddPanel";
import { SourceDetails } from "./SourceDetails";
import { SourceFilters } from "./SourceFilters";
import { SourcesTable } from "./SourcesTable";
import { useSourceJobs } from "./useSourceJobs";
import {
  countSourceStates,
  getSourceTriage,
  normalizePatch,
  SOURCE_STATES,
  sourceState,
  type SourceState,
} from "./sourceUtils";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

type DraftMap = Record<number, SourcePatch>;
// "" — весь каталог без архива; иначе — одно состояние (плитка).
type StateFilter = "" | SourceState;

const STATE_RANK = new Map(SOURCE_STATES.map((item, index) => [item.state, index]));

function initialFocusedSourceId() {
  const value = Number(new URLSearchParams(window.location.search).get("source_id") || 0);
  return Number.isFinite(value) && value > 0 ? value : null;
}

function initialSuggestedFrequency() {
  const value = new URLSearchParams(window.location.search).get("update_frequency") || "";
  return ["ежечасно", "ежедневно", "еженедельно"].includes(value) ? value : "";
}

export function SourcesPage({ onUnauthorized, showToast }: Props) {
  const [sources, setSources] = useState<Source[]>([]);
  const [health, setHealth] = useState<SourceHealth[]>([]);
  const [loadedAt, setLoadedAt] = useState(() => new Date());
  const [diagnostics, setDiagnostics] = useState<Record<number, SourceDiagnostics>>({});
  const [drafts, setDrafts] = useState<DraftMap>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState("");
  const [strategy, setStrategy] = useState("");
  const [stateFilter, setStateFilter] = useState<StateFilter>("");
  const [triageKey, setTriageKey] = useState("");
  const [focusedSourceId, setFocusedSourceId] = useState<number | null>(() => initialFocusedSourceId());
  const [expandedId, setExpandedId] = useState<number | null>(() => initialFocusedSourceId());
  const [suggestedFrequency] = useState(() => initialSuggestedFrequency());

  useEffect(() => {
    void reload();
  }, []);

  const { pendingJobs, setPendingJob } = useSourceJobs({
    showToast,
    onError: handleError,
    onDiagnosed: (sourceId, diagnostic) => setDiagnostics((prev) => ({ ...prev, [sourceId]: diagnostic })),
    onScraped: () => void reload(),
  });

  async function reload() {
    try {
      setLoading(true);
      const [sourcesPayload, healthPayload] = await Promise.all([listSources(), listSourceHealth()]);
      setSources(sourcesPayload);
      setHealth(healthPayload);
      // «N дн. назад» считаем от этого момента, а не от перерисовки: состояние сервер посчитал
      // сейчас, и после полуночи МСК поиск или раскрытие строки дали бы «7 дн. назад» у «штатно».
      setLoadedAt(new Date());
    } catch (error) {
      handleError(error, "Не удалось загрузить источники");
    } finally {
      setLoading(false);
    }
  }

  function handleError(error: unknown, fallback: string) {
    const status = typeof error === "object" && error && "status" in error ? Number(error.status) : 0;
    const message = error instanceof Error ? error.message : fallback;
    if (status === 401) {
      onUnauthorized();
      return;
    }
    showToast(message || fallback, "error");
  }

  const healthById = useMemo(() => new Map(health.map((item) => [Number(item.id), item])), [health]);

  // Плитки и таблица — по одному и тому же списку источников (см. countSourceStates).
  const counts = useMemo(() => countSourceStates(sources, healthById), [sources, healthById]);

  const visibleSources = useMemo(() => {
    const q = search.trim().toLowerCase();

    return sources
      .filter((source) => {
        const sourceHealth = healthById.get(source.id);
        const state = sourceState(source, sourceHealth);
        const hay = [String(source.id), source.name, source.url, source.rss_url, source.listing_url, source.source_type]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();

        return (
          // Архив — своя плитка, во «Всего источников» не входит (требование заказчика
          // 12.09: «уходит в архив — доп раздел внутри источников»).
          (stateFilter === "" ? state !== "archived" : state === stateFilter) &&
          (!q || hay.includes(q)) &&
          (!strategy || source.parse_strategy === strategy) &&
          (!triageKey || getSourceTriage(source, sourceHealth, diagnostics[source.id]).key === triageKey)
        );
      })
      .sort((left, right) => {
        const leftHealth = healthById.get(left.id);
        const rightHealth = healthById.get(right.id);
        const leftState = sourceState(left, leftHealth);
        const rightState = sourceState(right, rightHealth);
        const stateDelta =
          (leftState ? STATE_RANK.get(leftState) ?? 9 : 9) - (rightState ? STATE_RANK.get(rightState) ?? 9 : 9);
        if (stateDelta !== 0) return stateDelta;
        // Внутри состояния — дольше всех молчащие сверху: им внимание нужнее.
        const leftLast = leftHealth?.last_article_at ? new Date(leftHealth.last_article_at).getTime() : 0;
        const rightLast = rightHealth?.last_article_at ? new Date(rightHealth.last_article_at).getTime() : 0;
        if (leftLast !== rightLast) return leftLast - rightLast;
        return left.name.localeCompare(right.name, "ru");
      });
  }, [diagnostics, healthById, search, sources, stateFilter, strategy, triageKey]);

  const narrowed = Boolean(search.trim() || strategy || triageKey);

  // Ссылка на архивный источник открывает архив, иначе строки просто не будет. Ровно
  // один раз, после первой загрузки: дальше плитки и «Сбросить» — выбор пользователя,
  // и возвращать его в архив нельзя (ревью F: из архива было не выйти).
  const focusRevealed = useRef(false);
  useEffect(() => {
    if (!focusedSourceId || loading || focusRevealed.current) return;
    focusRevealed.current = true;
    const source = sources.find((item) => item.id === focusedSourceId);
    if (source && sourceState(source, healthById.get(source.id)) === "archived") setStateFilter("archived");
  }, [focusedSourceId, healthById, loading, sources]);

  useEffect(() => {
    if (!focusedSourceId || loading) return;
    document.getElementById(`source-${focusedSourceId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [focusedSourceId, loading, visibleSources.length]);

  useEffect(() => {
    if (!focusedSourceId || !suggestedFrequency || loading) return;
    const source = sources.find((item) => item.id === focusedSourceId);
    if (!source || source.update_frequency === suggestedFrequency) return;
    setDrafts((prev) => {
      const current = prev[focusedSourceId];
      if (current?.update_frequency === suggestedFrequency) return prev;
      return {
        ...prev,
        [focusedSourceId]: {
          ...(current ?? {}),
          update_frequency: suggestedFrequency,
        },
      };
    });
  }, [focusedSourceId, loading, sources, suggestedFrequency]);

  function currentPatch(source: Source) {
    return drafts[source.id] ?? {};
  }

  function currentField(source: Source, field: keyof SourcePatch) {
    const draft = currentPatch(source)[field];
    if (draft === undefined) {
      return source[field] ?? "";
    }
    return draft ?? "";
  }

  function updateDraft(sourceId: number, field: keyof SourcePatch, value: string | boolean | null) {
    setDrafts((prev) => ({
      ...prev,
      [sourceId]: {
        ...(prev[sourceId] ?? {}),
        [field]: value,
      },
    }));
  }

  async function handleCreateSource(payload: CreateSourcePayload) {
    try {
      setBusy(true);
      await createSource(payload);
      showToast("Источник добавлен — система ищет RSS-ленту");
      await reload();
      return true;
    } catch (error) {
      handleError(error, "Не удалось добавить источник");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveSource(source: Source) {
    try {
      setBusy(true);
      const payload = normalizePatch(currentPatch(source));
      await updateSource(source.id, payload);
      setDrafts((prev) => {
        const next = { ...prev };
        delete next[source.id];
        return next;
      });
      showToast("Источник сохранён");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось сохранить источник");
    } finally {
      setBusy(false);
    }
  }

  async function handleToggleSource(source: Source, nextEnabled: boolean) {
    try {
      setBusy(true);
      await updateSource(source.id, { enabled: nextEnabled });
      showToast(nextEnabled ? "Источник включён" : "Источник выключен");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось изменить статус источника");
    } finally {
      setBusy(false);
    }
  }

  async function handleArchiveSource(source: Source) {
    // Подтверждение обязательно: архив уносит из ленты ВСЕ статьи источника,
    // а не только прекращает сбор. Операция обратима — «Вернуть из архива».
    const articles = healthById.get(source.id)?.articles ?? 0;
    const warning = articles
      ? `«${source.name}»: ${articles} статей уйдут из ленты. Источник перестанет опрашиваться. Продолжить?`
      : `Убрать «${source.name}» в архив? Источник перестанет опрашиваться.`;
    if (!window.confirm(warning)) return;
    try {
      setBusy(true);
      await archiveSource(source.id);
      showToast(`«${source.name}» — в архиве`);
      await reload();
    } catch (error) {
      handleError(error, "Не удалось убрать источник в архив");
    } finally {
      setBusy(false);
    }
  }

  async function handleUnarchiveSource(source: Source) {
    try {
      setBusy(true);
      await unarchiveSource(source.id);
      showToast(`«${source.name}» возвращён из архива. Сбор включите отдельно.`);
      await reload();
    } catch (error) {
      handleError(error, "Не удалось вернуть источник из архива");
    } finally {
      setBusy(false);
    }
  }

  async function handleDiagnoseSource(source: Source) {
    try {
      const payload = normalizePatch(currentPatch(source));
      const response = await diagnoseSourceJob(source.id, payload);
      setPendingJob(source.id, "diagnose", response.job.id, "Проверяем источник…");
      showToast("Проверяем источник…");
    } catch (error) {
      handleError(error, "Не удалось выполнить диагностику");
    }
  }

  async function handleScrapeSource(source: Source) {
    try {
      const response = await scrapeSourceJob(source.id);
      setPendingJob(source.id, "scrape", response.job.id, "Собираем статьи…");
      showToast("Собираем статьи…");
    } catch (error) {
      handleError(error, "Не удалось собрать статьи");
    }
  }

  async function handleManualArticleImport(payload: ManualArticleImportPayload) {
    try {
      setBusy(true);
      const result = await importArticleByUrl(payload);
      const imported = result.article;
      const duplicateText = imported.duplicate ? "Повторная статья уже была в базе." : "Статья добавлена в базу.";
      const processText = result.job
        ? ` AI-задача #${result.job.id} поставлена в очередь ${result.job.queue}.`
        : " AI-обработка не запускалась.";
      showToast(`${duplicateText} Article #${imported.id}. ${processText}`);
      await reload();
      return true;
    } catch (error) {
      handleError(error, "Не удалось импортировать статью");
      return false;
    } finally {
      setBusy(false);
    }
  }

  function renderDetails(source: Source) {
    return (
      <SourceDetails
        source={source}
        health={healthById.get(source.id)}
        diagnostic={diagnostics[source.id]}
        hasDraft={Object.keys(currentPatch(source)).length > 0}
        pending={Boolean(pendingJobs[source.id])}
        pendingLabel={pendingJobs[source.id]?.label || null}
        focused={focusedSourceId === source.id}
        currentField={(field) => String(currentField(source, field))}
        onDraftChange={(field, value) => updateDraft(source.id, field, value)}
        onToggle={(nextEnabled) => void handleToggleSource(source, nextEnabled)}
        onSave={() => void handleSaveSource(source)}
        onDiagnose={() => void handleDiagnoseSource(source)}
        onScrape={() => void handleScrapeSource(source)}
        onArchive={() => void handleArchiveSource(source)}
        onUnarchive={() => void handleUnarchiveSource(source)}
      />
    );
  }

  function tile(key: StateFilter, value: number, label: string, tone: string, extraClass = "") {
    const active = stateFilter === key;
    return (
      <button
        type="button"
        key={key || "all"}
        aria-pressed={active}
        className={["sourceStatCard", tone, active ? "active" : "", extraClass].filter(Boolean).join(" ")}
        onClick={() => setStateFilter(active && key !== "" ? "" : key)}
      >
        <span className="sourceStatValue">{value}</span>
        <span className="sourceStatLabel">{label}</span>
      </button>
    );
  }

  const tileTone: Record<string, string> = { bad: "problem", warn: "warning", ok: "success", muted: "" };

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Источники</h1>
        </div>
        <div className="panelActions">
          {focusedSourceId && suggestedFrequency ? (
            <span className="statusPill">Рекомендация: {suggestedFrequency}</span>
          ) : null}
          {focusedSourceId ? (
            <button
              type="button"
              className="ghostButton compactButton"
              onClick={() => {
                setFocusedSourceId(null);
                setExpandedId(null);
              }}
            >
              Все источники
            </button>
          ) : null}
        </div>
      </header>

      <SourceAddPanel onCreateSource={handleCreateSource} onImportArticle={handleManualArticleImport} showToast={showToast} />

      <section className="panel">
        {busy ? <InlineLoader label="Обновляем источники…" /> : null}
        <div className="panelHeader">
          <h2>Каталог источников</h2>
          <button type="button" className="ghostButton" onClick={() => void reload()}>
            Обновить
          </button>
        </div>

        <div className="sourceHealthStats" aria-label="Состояние источников">
          {tile("", counts.total, "Всего источников", "")}
          {SOURCE_STATES.filter((item) => item.state !== "archived").map((item) =>
            tile(item.state, counts[item.state], item.tile, tileTone[item.tone]),
          )}
          {tile("archived", counts.archived, "В архиве", "", styles.archiveTile)}
        </div>

        <SourceFilters
          search={search}
          strategy={strategy}
          triageKey={triageKey}
          onSearchChange={setSearch}
          onStrategyChange={setStrategy}
          onTriageChange={setTriageKey}
          onReset={() => {
            setSearch("");
            setStrategy("");
            setStateFilter("");
            setTriageKey("");
          }}
        />

        {stateFilter === "archived" ? (
          <p className={styles.viewNote}>Архив: источники не опрашиваются, их статьи скрыты из ленты.</p>
        ) : null}
        {narrowed && !loading ? <p className={styles.viewNote}>Найдено: {visibleSources.length}</p> : null}

        {loading ? (
          <div className="emptyState"><LoadingState label="Загружаем источники…" /></div>
        ) : visibleSources.length ? (
          <SourcesTable
            sources={visibleSources}
            healthById={healthById}
            diagnostics={diagnostics}
            loadedAt={loadedAt}
            expandedId={expandedId}
            onToggle={(sourceId) => setExpandedId((current) => (current === sourceId ? null : sourceId))}
            renderDetails={renderDetails}
          />
        ) : (
          <div className="emptyState">Источники не найдены.</div>
        )}
      </section>
    </section>
  );
}

function InlineLoader(props: { label: string }) {
  return (
    <div className="loadingOverlay">
      <div className="spinnerReact" />
      <span>{props.label}</span>
    </div>
  );
}

function LoadingState(props: { label: string }) {
  return (
    <div className="loadingStateReact">
      <div className="spinnerReact" />
      <span>{props.label}</span>
    </div>
  );
}
