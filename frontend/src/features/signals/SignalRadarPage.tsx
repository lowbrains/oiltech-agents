import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../../api/client";
import { createSignalFeedback, listSignals, updateSignal } from "../../api/signals";
import type { Signal, SignalFeedbackPayload } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
  isAdmin?: boolean;
};

const MATURITY_LABELS: Record<string, string> = {
  watch: "Наблюдать",
  shortlist: "Кандидат",
  proven: "Подтверждено",
  reject: "Отклонено",
};

type FeedbackDraft = {
  verdict: NonNullable<SignalFeedbackPayload["verdict"]> | "";
  reason: string;
  correctedTitle: string;
  correctedThesis: string;
  duplicateOfSignalId: string;
  comment: string;
};

const EMPTY_FEEDBACK_DRAFT: FeedbackDraft = {
  verdict: "",
  reason: "",
  correctedTitle: "",
  correctedThesis: "",
  duplicateOfSignalId: "",
  comment: "",
};

// Шкала заказчика (список Виктора от 13.09). Это оценка ЧЕЛОВЕКА и она намеренно
// отличается от maturity выше: та — оценка модели. «Наблюдать» встречается в обеих,
// поэтому машинная подписана в карточке как «Зрелость», а эта — как «Оценка сигнала».
const VERDICT_LABELS: Array<{ value: FeedbackDraft["verdict"]; label: string }> = [
  { value: "", label: "Не оценено" },
  { value: "strong_signal", label: "Сильный сигнал — вынести в дайджест / обсуждать" },
  { value: "approved", label: "Полезный сигнал — релевантно, сохранить в базе" },
  { value: "watch_later", label: "Наблюдать — рано, нужен следующий milestone" },
  { value: "background_material", label: "Фоновый материал — benchmark или контекст" },
  { value: "reject", label: "Низкая ценность / шум — по теме, но без новой ценности" },
  { value: "wrong_domain", label: "Не релевантно — вне интересов Компании" },
  { value: "merge_duplicate", label: "Дубль — тот же сигнал или технологический кластер" },
];

export function SignalRadarPage({ onUnauthorized, showToast, isAdmin = false }: Props) {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [busy, setBusy] = useState(false);
  const [maturity, setMaturity] = useState("");
  const [theme, setTheme] = useState("");
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [feedbackOpen, setFeedbackOpen] = useState<Set<number>>(new Set());
  const [feedbackDrafts, setFeedbackDrafts] = useState<Record<number, FeedbackDraft>>({});
  const [saving, setSaving] = useState<Record<number, boolean>>({});

  useEffect(() => {
    void reload();
  }, []);

  function handleError(error: unknown, fallback: string) {
    const statusCode = error instanceof ApiError ? error.status : 0;
    const message = error instanceof Error ? error.message : fallback;
    if (statusCode === 401) {
      onUnauthorized();
      return;
    }
    showToast(message || fallback, "error");
  }

  async function reload() {
    try {
      setBusy(true);
      setSignals(await listSignals({ maturity: maturity || undefined, theme: theme || undefined, limit: 150, evidenceLimit: 5 }));
    } catch (error) {
      handleError(error, "Не удалось загрузить радар сигналов");
    } finally {
      setBusy(false);
    }
  }

  const themes = useMemo(() => [...new Set(signals.map((signal) => signal.theme).filter(Boolean))].sort(), [signals]);
  const visibleSignals = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return signals;
    return signals.filter((signal) =>
      [
        signal.title_ru,
        signal.title,
        signal.theme,
        signal.summary,
        signal.thesis,
        signal.transferability,
        ...(signal.evidence || []).map((item) => `${item.title} ${item.title_ru || ""} ${item.publisher || ""}`),
      ].some((value) => String(value || "").toLowerCase().includes(q)),
    );
  }, [search, signals]);

  async function setDigest(signal: Signal, selected: boolean) {
    try {
      setSaving((current) => ({ ...current, [signal.id]: true }));
      await updateSignal(signal.id, { selected_for_digest: selected });
      setSignals((current) =>
        current.map((item) =>
          item.id === signal.id ? { ...item, selected_for_digest: selected, user_status: selected ? "digest" : "watch" } : item,
        ),
      );
      showToast(selected ? "Сигнал добавлен в дайджест" : "Сигнал убран из дайджеста");
    } catch (error) {
      handleError(error, "Не удалось обновить статус сигнала");
    } finally {
      setSaving((current) => ({ ...current, [signal.id]: false }));
    }
  }

  async function submitFeedback(signal: Signal) {
    const draft = feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT;
    const comment = draft.comment.trim();
    const reason = draft.reason.trim();
    const correctedTitle = draft.correctedTitle.trim();
    const correctedThesis = draft.correctedThesis.trim();
    const duplicateOfSignalId = Number(draft.duplicateOfSignalId || 0) || null;
    if (!comment && !draft.verdict && !reason && !correctedTitle && !correctedThesis && !duplicateOfSignalId) {
      showToast("Заполни вердикт, причину или комментарий по сигналу", "error");
      return;
    }
    const primaryEvidence = signal.evidence?.[0];
    try {
      setSaving((current) => ({ ...current, [signal.id]: true }));
      const result = await createSignalFeedback({
        signal_id: signal.id,
        signal_evidence_id: primaryEvidence?.id,
        source_url: primaryEvidence?.source_url,
        signal_title: signal.title_ru || signal.title,
        source: primaryEvidence?.publisher,
        comment,
        verdict: draft.verdict || null,
        reason: reason || null,
        corrected_title: correctedTitle || null,
        corrected_thesis: correctedThesis || null,
        duplicate_of_signal_id: duplicateOfSignalId,
      });
      setFeedbackDrafts((current) => ({ ...current, [signal.id]: EMPTY_FEEDBACK_DRAFT }));
      setSignals((current) =>
        current.map((item) =>
          item.id === signal.id ? { ...item, feedback_count: (item.feedback_count || 0) + 1 } : item,
        ),
      );
      showToast(`ОС сохранена, memory-записей: ${result.memories}`);
    } catch (error) {
      handleError(error, "Не удалось сохранить ОС по сигналу");
    } finally {
      setSaving((current) => ({ ...current, [signal.id]: false }));
    }
  }

  function toggleExpanded(signalId: number) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(signalId)) next.delete(signalId);
      else next.add(signalId);
      return next;
    });
  }

  function toggleFeedback(signalId: number) {
    setFeedbackOpen((current) => {
      const next = new Set(current);
      if (next.has(signalId)) next.delete(signalId);
      else next.add(signalId);
      return next;
    });
  }

  function updateFeedbackDraft(signalId: number, patch: Partial<FeedbackDraft>) {
    setFeedbackDrafts((current) => ({
      ...current,
      [signalId]: { ...(current[signalId] || EMPTY_FEEDBACK_DRAFT), ...patch },
    }));
  }

  const digestCount = visibleSignals.filter((signal) => signal.selected_for_digest).length;
  const feedbackCount = visibleSignals.reduce((sum, signal) => sum + Number(signal.feedback_count || 0), 0);

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <div className="eyebrow">Signal Discovery</div>
          <h1>Радар сигналов</h1>
        </div>
        <div className="signalRadarHeaderStats" aria-label="Сводка радара">
          <span><strong>{visibleSignals.length}</strong> сигналов</span>
          <span><strong>{digestCount}</strong> в дайджесте</span>
          <span><strong>{feedbackCount}</strong> ОС</span>
        </div>
      </header>

      <section className="signalRadarToolbar">
        <label>
          <span>Поиск</span>
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="ZEUS IQ, бурение, робот..." />
        </label>
        <label>
          <span>Зрелость</span>
          <select value={maturity} onChange={(event) => setMaturity(event.target.value)}>
            <option value="">Все</option>
            <option value="watch">Наблюдать</option>
            <option value="shortlist">Кандидат</option>
            <option value="proven">Подтверждено</option>
            <option value="reject">Отклонено</option>
          </select>
        </label>
        <label>
          <span>Тема</span>
          <select value={theme} onChange={(event) => setTheme(event.target.value)}>
            <option value="">Все темы</option>
            {themes.map((item) => <option value={item} key={item}>{item}</option>)}
          </select>
        </label>
        <button type="button" className="ghostButton" disabled={busy} onClick={() => void reload()}>
          {busy ? "Обновляем" : "Обновить"}
        </button>
      </section>

      <section className="signalRadarList">
        {busy ? (
          <div className="emptyState">Загружаем сигналы...</div>
        ) : visibleSignals.length ? (
          visibleSignals.map((signal) => {
            const isExpanded = expanded.has(signal.id);
            const isFeedbackOpen = feedbackOpen.has(signal.id);
            const savingThis = Boolean(saving[signal.id]);
            return (
              <article className="signalRadarCard" key={signal.id}>
                <div className="signalRadarCardTop">
                  <div>
                    <div className="signalRadarMeta">
                      {/* ID виден всегда: без него нельзя сослаться на дубль
                          в поле «ID дубля» — заказчик спрашивал, где его взять. */}
                      <span className="signalIdBadge">#{signal.id}</span>
                      <span className="signalTheme">{signal.theme}</span>
                      <span>Зрелость: {MATURITY_LABELS[signal.maturity] || signal.maturity}</span>
                      <span>{Math.round(Number(signal.score || 0))} баллов</span>
                      <span>{signal.evidence_count} ссылок</span>
                    </div>
                    {/* Издатели показываются сразу, до раскрытия: «давай источник
                        сделаем открытым сразу» — по нему судят о доверии к сигналу. */}
                    {signal.evidence?.length ? (
                      <div className="signalPublishers">
                        {[...new Set((signal.evidence || []).map((item) => item.publisher).filter(Boolean))].map(
                          (publisher) => (
                            <span className="signalPublisherChip" key={publisher as string}>{publisher}</span>
                          ),
                        )}
                      </div>
                    ) : null}
                    <h2>{signal.title_ru || signal.title}</h2>
                  </div>
                  <div className="signalRadarActions">
                    <button type="button" className="ghostButton compactButton" onClick={() => toggleExpanded(signal.id)}>
                      {isExpanded ? "Скрыть" : "Ссылки"}
                    </button>
                    {isAdmin ? (
                      <button type="button" className="ghostButton compactButton" onClick={() => toggleFeedback(signal.id)}>
                        Обратная связь
                      </button>
                    ) : null}
                    <button
                      type="button"
                      className={signal.selected_for_digest ? "dangerButton compactButton" : "primaryButton compactButton"}
                      disabled={savingThis}
                      onClick={() => void setDigest(signal, !signal.selected_for_digest)}
                    >
                      {signal.selected_for_digest ? "Убрать" : "В дайджест"}
                    </button>
                  </div>
                </div>

                <div className="signalRadarBody">
                  <p className="signalRadarSummaryText">{signal.summary || signal.thesis || "Суть сигнала ещё не сформирована."}</p>
                  <div className="signalRadarFacts">
                    <div>
                      <span>Почему сейчас</span>
                      <p>{signal.why_now || "Нет объяснения"}</p>
                    </div>
                    <div>
                      <span>Переносимость</span>
                      <p>{signal.transferability || "Нет оценки"}</p>
                    </div>
                  </div>
                </div>

                {isExpanded ? (
                  <div className="signalEvidenceList">
                    {(signal.evidence || []).map((item) => (
                      <a className="signalEvidenceRow" href={item.source_url} target="_blank" rel="noreferrer" key={item.id}>
                        <span>{item.publisher || "source"}</span>
                        <strong>{item.title_ru || item.title}</strong>
                        <small>{item.summary_ru || item.extracted_fact || item.evidence_type}</small>
                      </a>
                    ))}
                  </div>
                ) : null}

                {isAdmin && isFeedbackOpen ? (
                  <div className="signalFeedbackBox">
                    <div className="signalFeedbackGrid">
                      <label>
                        <span>Оценка сигнала</span>
                        <select
                          value={(feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT).verdict}
                          onChange={(event) =>
                            updateFeedbackDraft(signal.id, { verdict: event.target.value as FeedbackDraft["verdict"] })
                          }
                        >
                          {VERDICT_LABELS.map((item) => (
                            <option value={item.value} key={item.value || "empty"}>{item.label}</option>
                          ))}
                        </select>
                      </label>
                      <label>
                        <span>ID дубля</span>
                        <input
                          inputMode="numeric"
                          value={(feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT).duplicateOfSignalId}
                          onChange={(event) => updateFeedbackDraft(signal.id, { duplicateOfSignalId: event.target.value })}
                          placeholder="если это дубль"
                        />
                      </label>
                    </div>
                    <label className="signalFeedbackField">
                      <span>Обоснование оценки</span>
                      <input
                        value={(feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT).reason}
                        onChange={(event) => updateFeedbackDraft(signal.id, { reason: event.target.value })}
                        placeholder="чем обоснована оценка"
                      />
                    </label>
                    <label className="signalFeedbackField">
                      <span>Рекомендуемый заголовок</span>
                      <input
                        value={(feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT).correctedTitle}
                        onChange={(event) => updateFeedbackDraft(signal.id, { correctedTitle: event.target.value })}
                        placeholder="если нужно переименовать карточку"
                      />
                    </label>
                    <label className="signalFeedbackField">
                      <span>Рекомендуемая формулировка сути</span>
                      <textarea
                        value={(feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT).correctedThesis}
                        onChange={(event) => updateFeedbackDraft(signal.id, { correctedThesis: event.target.value })}
                        placeholder="эталонная формулировка сути сигнала"
                      />
                    </label>
                    <textarea
                      value={(feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT).comment}
                      onChange={(event) => updateFeedbackDraft(signal.id, { comment: event.target.value })}
                      placeholder="Рекомендации AI-агенту: термины, поисковый угол, сильный источник..."
                    />
                    <div className="signalFeedbackActions">
                      <span>{signal.feedback_count || 0} ОС сохранено</span>
                      <button type="button" className="primaryButton compactButton" disabled={savingThis} onClick={() => void submitFeedback(signal)}>
                        Сохранить
                      </button>
                    </div>
                  </div>
                ) : isAdmin ? (
                  <div className="signalFeedbackCollapsed">
                    <span>{signal.feedback_count || 0} ОС сохранено</span>
                    <button type="button" className="ghostButton compactButton" onClick={() => toggleFeedback(signal.id)}>
                      Добавить ОС
                    </button>
                  </div>
                ) : null}
              </article>
            );
          })
        ) : (
          <div className="emptyState">Сигналов по выбранным фильтрам нет.</div>
        )}
      </section>
    </section>
  );
}
