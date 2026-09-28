import { useEffect, useMemo, useRef, useState } from "react";
import { listArticles, updateArticle } from "../../api/articles";
import { enqueueDigestExport, getDigestBranding, getDigestContent, getDigestEmailHtml, getMonthlyDigest, saveDigestBranding, updateMonthlyDigest } from "../../api/digest";
import { downloadJobResult, getJob } from "../../api/jobs";
import type { Article, DigestBranding, DigestBrandingSocial, DigestContent, DigestDraftSaveResult, DigestHighlightCard, MonthlyDigestDraft } from "../../api/types";
import { monthLabel, useFeedWindow } from "../articles/feedWindow";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
  onArticlesChanged?: () => void;
  isAdmin?: boolean;
};

const DIGEST_PREVIEW_LIMIT = 500;
// Один и тот же пустой массив, пока грузится архивный выпуск. Новый `[]` на каждом рендере
// пересоздавал бы цепочку useMemo до previewCandidateIds, а эффект синхронизации очереди
// звал бы setManualOrderIds снова и снова — бесконечный цикл рендеров.
const NO_ARTICLES: Article[] = [];

export function DigestPage({ onUnauthorized, showToast, onArticlesChanged, isAdmin = false }: Props) {
  const [articles, setArticles] = useState<Article[]>([]);
  const [branding, setBranding] = useState<DigestBranding | null>(null);
  const [digestPreview, setDigestPreview] = useState<DigestContent | null>(null);
  const [digestPreviewHtml, setDigestPreviewHtml] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [brandingBusy, setBrandingBusy] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [draftBusy, setDraftBusy] = useState(false);
  const [lastSavedDraft, setLastSavedDraft] = useState<DigestDraftSaveResult | null>(null);
  const [savedDraft, setSavedDraft] = useState<MonthlyDigestDraft | null>(null);
  const [manualOrderIds, setManualOrderIds] = useState<number[]>([]);
  const [draftDirty, setDraftDirty] = useState(false);
  const [search, setSearch] = useState("");
  const [tag, setTag] = useState("");
  const [tagQuery, setTagQuery] = useState("");
  const [showTagOptions, setShowTagOptions] = useState(false);
  const [month, setMonth] = useState("");
  const [scoreMin, setScoreMin] = useState(0);
  const [scoreMax, setScoreMax] = useState(100);
  const [viewMode, setViewMode] = useState<"issue" | "branding">("issue");
  // Дружелюбная метка готовящегося документа («PDF»/«DOCX»/«HTML») — без понятий
  // «задача/очередь/№N»: пользователь видит процесс и результат, а не job-runner.
  const [exporting, setExporting] = useState<string | null>(null);
  const activeMonth = month || new Date().toISOString().slice(0, 7);
  // Окно месяца (ADR 0001, п. 6): открытые месяцы и прошлые выпуски с выбранными статьями.
  // Прошлый выпуск — только просмотр и выгрузка (решение владельца 23.09): статусы и
  // черновик не меняются, сервер такие правки отклоняет.
  const feedWindow = useFeedWindow();
  const firstOpenMonth = feedWindow?.months?.[0] ?? "";
  const archiveIssueMonths = useMemo(
    () => (feedWindow?.archive ?? []).filter((item) => item.digest > 0).map((item) => item.month),
    [feedWindow],
  );
  const [archiveArticles, setArchiveArticles] = useState<Article[] | null>(null);
  const isArchiveMonth = Boolean(month) && Boolean(firstOpenMonth) && month < firstOpenMonth;
  // Лента (/api/articles) без месяца отдаёт только открытые месяцы — для прошлого выпуска
  // выбранные статьи приходят отдельным запросом с `month`.
  const sourceArticles = isArchiveMonth ? archiveArticles ?? NO_ARTICLES : articles;

  // Окно сменилось, пока вкладка была открыта (наступило 5-е): выбранное за закрывшийся
  // месяц ушло в архив. Перечитываем список, иначе в очереди остались бы статьи, которые
  // уже нельзя сохранить в черновик (сервер ответит 409).
  const loadedOpenMonth = useRef("");
  useEffect(() => {
    if (!firstOpenMonth) return;
    if (loadedOpenMonth.current && loadedOpenMonth.current !== firstOpenMonth) {
      listArticles({ status: "digest", limit: 5000 })
        .then(setArticles)
        .catch((error) => handleError(error, "Не удалось обновить выбранное в дайджест"));
    }
    loadedOpenMonth.current = firstOpenMonth;
  }, [firstOpenMonth]);

  useEffect(() => {
    void reload();
  }, []);

  useEffect(() => {
    if (!isArchiveMonth) {
      setArchiveArticles(null);
      return;
    }
    let cancelled = false;
    listArticles({ status: "digest", month, limit: 5000 })
      .then((rows) => {
        if (!cancelled) setArchiveArticles(rows);
      })
      .catch((error) => {
        if (!cancelled) handleError(error, "Не удалось загрузить архивный выпуск");
      });
    return () => {
      cancelled = true;
    };
  }, [isArchiveMonth, month]);

  useEffect(() => {
    if (loading) return;
    void loadDigestPreview();
  }, [loading, month, scoreMin, scoreMax, search, tag]);

  useEffect(() => {
    if (loading) return;
    void loadSavedDraft(activeMonth);
  }, [loading, activeMonth]);

  async function reload() {
    try {
      setLoading(true);
      // Грузим ВСЕ статьи со статусом «digest» с сервера (а не топ-2000 по score),
      // иначе низкоскоринговые материалы выпадают из дайджеста и их не убрать.
      const [articleRows, brandingPayload] = await Promise.all([
        listArticles({ status: "digest", limit: 5000 }),
        getDigestBranding(),
      ]);
      setArticles(articleRows);
      setBranding({
        ...brandingPayload,
        issue: {
          title_template: brandingPayload.issue?.title_template || "Нефтесервисный дайджест",
          title_template_with_month: brandingPayload.issue?.title_template_with_month || "Нефтесервисный дайджест · {month}",
          period_label_all: brandingPayload.issue?.period_label_all || "за всё время",
          preheader: brandingPayload.issue?.preheader || "Ключевые новости и обзоры нефтесервисного рынка",
          intro_template:
            brandingPayload.issue?.intro_template ||
            "Уважаемые коллеги! Представляем ключевые новости и обзоры нефтесервисного рынка, которые помогают отслеживать технологические тренды, рыночную динамику и возможности для развития бизнеса.",
          intro_template_with_month:
            brandingPayload.issue?.intro_template_with_month ||
            "Уважаемые коллеги! Представляем ключевые новости и обзоры за {month}, которые помогают отслеживать технологические тренды, рыночную динамику и возможности для развития нефтесервисного бизнеса.",
          highlights_title: brandingPayload.issue?.highlights_title || "Главное за период",
          news_title: brandingPayload.issue?.news_title || "Новости",
          read_more_label: brandingPayload.issue?.read_more_label || "ЧИТАТЬ ДАЛЕЕ",
          empty_summary_text: brandingPayload.issue?.empty_summary_text || "Суть ещё не сформирована.",
          preview_empty_text: brandingPayload.issue?.preview_empty_text || "В текущей выборке нет сигналов для превью.",
        },
        highlights: {
          analytics_source_keywords: brandingPayload.highlights?.analytics_source_keywords || [],
          analytics_category_keywords: brandingPayload.highlights?.analytics_category_keywords || [],
          business_category_keywords: brandingPayload.highlights?.business_category_keywords || [],
          cards: brandingPayload.highlights?.cards || defaultHighlightCards(),
        },
      });
      const [preview, previewHtml] = await Promise.all([
        getDigestContent({
          month,
          limit: DIGEST_PREVIEW_LIMIT,
          minScore: scoreMin,
          maxScore: scoreMax,
          search,
          topTag: tag,
        }),
        getDigestEmailHtml({
          month,
          limit: DIGEST_PREVIEW_LIMIT,
          minScore: scoreMin,
          maxScore: scoreMax,
          search,
          topTag: tag,
        }),
      ]);
      setDigestPreview(preview);
      setDigestPreviewHtml(previewHtml);
    } catch (error) {
      handleError(error, "Не удалось загрузить статьи для дайджеста");
    } finally {
      setLoading(false);
    }
  }

  async function loadDigestPreview() {
    try {
      setPreviewLoading(true);
      const [preview, previewHtml] = await Promise.all([
        getDigestContent({
          month,
          limit: DIGEST_PREVIEW_LIMIT,
          minScore: scoreMin,
          maxScore: scoreMax,
          search,
          topTag: tag,
        }),
        getDigestEmailHtml({
          month,
          limit: DIGEST_PREVIEW_LIMIT,
          minScore: scoreMin,
          maxScore: scoreMax,
          search,
          topTag: tag,
        }),
      ]);
      setDigestPreview(preview);
      setDigestPreviewHtml(previewHtml);
    } catch (error) {
      handleError(error, "Не удалось обновить предпросмотр дайджеста");
    } finally {
      setPreviewLoading(false);
    }
  }

  async function loadSavedDraft(targetMonth: string) {
    try {
      const draft = await getMonthlyDigest(targetMonth);
      setSavedDraft(draft);
      setLastSavedDraft({
        id: draft.id,
        month: draft.month,
        title: draft.title,
        status: draft.status,
        items: draft.items.length,
      });
      setManualOrderIds(draft.items.map((item) => item.article_id));
      setDraftDirty(false);
    } catch (error) {
      const status = typeof error === "object" && error && "status" in error ? Number(error.status) : 0;
      if (status === 404) {
        setSavedDraft(null);
        setLastSavedDraft(null);
        setDraftDirty(false);
        return;
      }
      handleError(error, "Не удалось загрузить сохранённый draft");
    }
  }

  async function removeFromDigest(articleId: number) {
    try {
      setBusy(true);
      await updateArticle(articleId, { status: "archive" });
      await reload();
      onArticlesChanged?.();
      showToast("Статья убрана из дайджеста");
    } catch (error) {
      handleError(error, "Не удалось убрать статью из дайджеста");
    } finally {
      setBusy(false);
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

  const digestCandidates = useMemo(() => {
    const q = search.trim().toLowerCase();
    return sourceArticles.filter((article) => {
      const hay = [article.title, article.tag, article.summary].join(" ").toLowerCase();
      return (
        article.digest &&
        (!q || hay.includes(q)) &&
        (!tag || article.tag === tag || article.tag.startsWith(`${tag} /`)) &&
        Number(article.score || 0) >= scoreMin &&
        Number(article.score || 0) <= scoreMax &&
        (!month || String(article.date || "").startsWith(month))
      );
    });
  }, [sourceArticles, month, scoreMax, scoreMin, search, tag]);

  // Месяцы выпуска: открытые (по выбранным статьям ленты) плюс прошлые выпуски, в которых
  // у этого человека есть выбранные статьи.
  const months = useMemo(() => {
    const open = articles.filter((article) => article.digest).map((article) => String(article.date || "").slice(0, 7));
    return [...new Set([...open, ...archiveIssueMonths].filter(Boolean))].sort().reverse();
  }, [articles, archiveIssueMonths]);

  const topTags = useMemo(() => {
    const names = [...new Set(sourceArticles.map((article) => article.tag).filter(Boolean))].sort();
    return [...new Set(names.map((name) => name.split(" / ")[0]))];
  }, [sourceArticles]);
  const filteredTagOptions = useMemo(() => {
    const q = tagQuery.trim().toLowerCase();
    return topTags.filter((option) => !q || option.toLowerCase().includes(q));
  }, [tagQuery, topTags]);
  // useMemo обязателен: пока превью не пришло, `|| []` давал НОВЫЙ массив на каждом рендере,
  // цепочка useMemo до previewCandidateIds пересчитывалась, а эффект синхронизации очереди
  // звал setManualOrderIds с новым массивом — рендер за рендером, пока не придёт превью.
  // В браузере это крутило процессор на время загрузки, в тестах — вешало прогон (23.09).
  const exportPreviewItems = useMemo(() => digestPreview?.news || [], [digestPreview]);
  const previewOrderedCandidates = useMemo(() => {
    const byId = new Map(digestCandidates.map((article) => [article.id, article]));
    const ordered = exportPreviewItems
      .map((item) => (item.article_id ? byId.get(item.article_id) : undefined))
      .filter((article): article is Article => Boolean(article));
    const usedIds = new Set(ordered.map((article) => article.id));
    const remainder = digestCandidates.filter((article) => !usedIds.has(article.id));
    return [...ordered, ...remainder];
  }, [digestCandidates, exportPreviewItems]);
  const previewCandidateIds = useMemo(() => previewOrderedCandidates.map((article) => article.id), [previewOrderedCandidates]);
  const orderedDigestCandidates = useMemo(() => {
    const byId = new Map(previewOrderedCandidates.map((article) => [article.id, article]));
    return manualOrderIds.map((id) => byId.get(id)).filter((article): article is Article => Boolean(article));
  }, [manualOrderIds, previewOrderedCandidates]);
  const availableDigestCandidates = useMemo(() => {
    const selectedIds = new Set(manualOrderIds);
    return previewOrderedCandidates.filter((article) => !selectedIds.has(article.id));
  }, [manualOrderIds, previewOrderedCandidates]);
  const savedDraftVisibleIds = useMemo(
    () => (savedDraft?.items || []).map((item) => item.article_id).filter((id) => previewCandidateIds.includes(id)),
    [savedDraft, previewCandidateIds],
  );
  const hasManualChanges = draftDirty || JSON.stringify(manualOrderIds) !== JSON.stringify(savedDraftVisibleIds.length ? savedDraftVisibleIds : previewCandidateIds);

  useEffect(() => {
    if (draftDirty) return;
    const baseIds = savedDraftVisibleIds.length ? savedDraftVisibleIds : previewCandidateIds;
    setManualOrderIds(baseIds);
  }, [draftDirty, previewCandidateIds, savedDraftVisibleIds]);

  function updateBrandingSection<K extends keyof DigestBranding>(section: K, value: DigestBranding[K]) {
    setBranding((prev) => (prev ? { ...prev, [section]: value } : prev));
  }

  function updateFooterSocial(index: number, field: keyof DigestBrandingSocial, value: string) {
    setBranding((prev) => {
      if (!prev) return prev;
      const socials = prev.footer.socials.map((item, itemIndex) =>
        itemIndex === index ? { ...item, [field]: value } : item,
      );
      return { ...prev, footer: { ...prev.footer, socials } };
    });
  }

  function addFooterSocial() {
    setBranding((prev) =>
      prev
        ? {
            ...prev,
            footer: {
              ...prev.footer,
              socials: [...prev.footer.socials, { label: "", accent: "#003da6", text: "" }],
            },
          }
        : prev,
    );
  }

  function removeFooterSocial(index: number) {
    setBranding((prev) =>
      prev
        ? {
            ...prev,
            footer: {
              ...prev.footer,
              socials: prev.footer.socials.filter((_, itemIndex) => itemIndex !== index),
            },
          }
        : prev,
    );
  }

  async function handleSaveBranding() {
    if (!branding) return;
    try {
      setBrandingBusy(true);
      const payload = {
        ...branding,
        footer: {
          ...branding.footer,
          socials: branding.footer.socials.filter((item) => item.label.trim() || item.text.trim()),
        },
      };
      const saved = await saveDigestBranding(payload);
      setBranding({
        ...saved.branding,
        issue: {
          title_template: saved.branding.issue?.title_template || "Нефтесервисный дайджест",
          title_template_with_month: saved.branding.issue?.title_template_with_month || "Нефтесервисный дайджест · {month}",
          period_label_all: saved.branding.issue?.period_label_all || "за всё время",
          preheader: saved.branding.issue?.preheader || "Ключевые новости и обзоры нефтесервисного рынка",
          intro_template:
            saved.branding.issue?.intro_template ||
            "Уважаемые коллеги! Представляем ключевые новости и обзоры нефтесервисного рынка, которые помогают отслеживать технологические тренды, рыночную динамику и возможности для развития бизнеса.",
          intro_template_with_month:
            saved.branding.issue?.intro_template_with_month ||
            "Уважаемые коллеги! Представляем ключевые новости и обзоры за {month}, которые помогают отслеживать технологические тренды, рыночную динамику и возможности для развития нефтесервисного бизнеса.",
          highlights_title: saved.branding.issue?.highlights_title || "Главное за период",
          news_title: saved.branding.issue?.news_title || "Новости",
          read_more_label: saved.branding.issue?.read_more_label || "ЧИТАТЬ ДАЛЕЕ",
          empty_summary_text: saved.branding.issue?.empty_summary_text || "Суть ещё не сформирована.",
          preview_empty_text: saved.branding.issue?.preview_empty_text || "В текущей выборке нет сигналов для превью.",
        },
        highlights: {
          analytics_source_keywords: saved.branding.highlights?.analytics_source_keywords || [],
          analytics_category_keywords: saved.branding.highlights?.analytics_category_keywords || [],
          business_category_keywords: saved.branding.highlights?.business_category_keywords || [],
          cards: saved.branding.highlights?.cards || defaultHighlightCards(),
        },
      });
      await loadDigestPreview();
      showToast("Оформление дайджеста сохранено");
    } catch (error) {
      handleError(error, "Не удалось сохранить оформление дайджеста");
    } finally {
      setBrandingBusy(false);
    }
  }

  async function handleDigestExport(format: "pdf" | "docx" | "html") {
    try {
      setBusy(true);
      setExporting(format.toUpperCase());
      // Документ собирается на сервере; для пользователя это просто ожидание файла.
      const queued = await enqueueDigestExport(month, DIGEST_PREVIEW_LIMIT, scoreMin, format, {
        maxScore: scoreMax,
        search,
        topTag: tag,
      });
      const finished = await waitForExportJob(queued.job.id);
      if (finished.status !== "ok") {
        throw new Error(finished.error || "Не удалось подготовить документ");
      }

      const file = await downloadJobResult(finished.id);
      const url = window.URL.createObjectURL(file.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = file.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
      showToast(`Документ готов: ${file.filename}`);
    } catch (error) {
      handleError(error, "Не удалось подготовить документ");
    } finally {
      setBusy(false);
      setExporting(null);
    }
  }

  async function handleSaveDraft() {
    // Прошлый выпуск — только просмотр и выгрузка; кнопки в архиве нет, это страховка.
    if (isArchiveMonth) return;
    try {
      setDraftBusy(true);
      const draftMonth = month || new Date().toISOString().slice(0, 7);
      if (!month) setMonth(draftMonth);
      const orderedItems = orderedDigestCandidates.map((article) => ({
        article_id: article.id,
        section: article.tag,
        editor_note: article.summary || null,
      }));
      const saved = await updateMonthlyDigest(draftMonth, {
        title: digestPreview?.title || `Нефтесервисный дайджест · ${draftMonth}`,
        status: "draft",
        items: orderedItems,
      });
      setLastSavedDraft(saved);
      await loadSavedDraft(draftMonth);
      await loadDigestPreview();
      showToast(`Draft сохранён: ${saved.month}`);
    } catch (error) {
      handleError(error, "Не удалось сохранить draft дайджеста");
    } finally {
      setDraftBusy(false);
    }
  }

  function moveDigestItem(articleId: number, direction: -1 | 1) {
    setDraftDirty(true);
    setManualOrderIds((current) => {
      const index = current.indexOf(articleId);
      if (index < 0) return current;
      const nextIndex = index + direction;
      if (nextIndex < 0 || nextIndex >= current.length) return current;
      const next = [...current];
      const [item] = next.splice(index, 1);
      next.splice(nextIndex, 0, item);
      return next;
    });
  }

  function removeFromIssue(articleId: number) {
    setDraftDirty(true);
    setManualOrderIds((current) => current.filter((id) => id !== articleId));
  }

  function addToIssue(articleId: number) {
    setDraftDirty(true);
    setManualOrderIds((current) => (current.includes(articleId) ? current : [...current, articleId]));
  }

  function resetDraftQueue() {
    const baseIds = savedDraftVisibleIds.length ? savedDraftVisibleIds : previewCandidateIds;
    setManualOrderIds(baseIds);
    setDraftDirty(false);
  }

  // Ждём готовности файла, опрашивая сервер. Прогресс/номер наружу не показываем —
  // только нейтральный спиннер «Готовим документ…».
  async function waitForExportJob(jobId: number) {
    for (let attempt = 0; attempt < 80; attempt += 1) {
      const job = await getJob(jobId);
      if (job.status === "ok" || job.status === "failed") {
        return job;
      }
      await sleep(2000);
    }
    throw new Error("Документ готовится дольше обычного — попробуйте ещё раз чуть позже");
  }

  const filterSummary = [
    tag ? `тег: ${tag}` : "",
    search.trim() ? `поиск: ${search.trim()}` : "",
    scoreMin > 0 || scoreMax < 100 ? `score ${scoreMin}-${scoreMax}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const issueWorkspace = (
    <div className="digestLayout">
      <section className="digestBuilderMain">
        <div className="panelHeader">
          <h2>Выборка дайджеста</h2>
          {previewLoading ? <span className="metaText">Обновляем preview выпуска…</span> : null}
        </div>

        {isArchiveMonth ? (
          <div className="archiveNotice" role="status">
            <span>Выпуск за {monthLabel(month)} в архиве: его можно смотреть и выгружать, но не менять.</span>
          </div>
        ) : null}

        <div className="digestRunSummary">
          <div className="digestRunCard">
            <div className="metaText">Текущий выпуск</div>
            <strong>{activeMonth}</strong>
            <span className="metaText">{digestCandidates.length} статей в подборке</span>
          </div>
          <div className="digestRunCard">
            <div className="metaText">Preview / экспорт</div>
            <strong>{exportPreviewItems.length}</strong>
            <span className="metaText">материалов реально вернёт backend</span>
          </div>
          <div className="digestRunCard">
            <div className="metaText">Последний draft</div>
            <strong>{lastSavedDraft ? lastSavedDraft.month : "ещё не сохранён"}</strong>
            <span className="metaText">
              {lastSavedDraft ? `${lastSavedDraft.items} материалов · статус ${lastSavedDraft.status}` : "Можно сохранить текущую выборку как выпуск"}
            </span>
          </div>
        </div>
        {filterSummary ? <div className="digestRunFilters">{filterSummary}</div> : null}

        <div className="digestFiltersRow">
          <label className="field">
            <span>Поиск</span>
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Найти статью" />
          </label>
          <div className="field sourceComboReact">
            <span>Тег</span>
            <input
              value={tag || tagQuery}
              onChange={(event) => {
                setTag("");
                setTagQuery(event.target.value);
                setShowTagOptions(true);
              }}
              onFocus={() => setShowTagOptions(true)}
              onBlur={() => {
                window.setTimeout(() => setShowTagOptions(false), 120);
              }}
              placeholder="Все теги"
            />
            {showTagOptions ? (
              <div className="sourcePopoverReact">
                <button
                  type="button"
                  className="sourceOptionReact"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    setTag("");
                    setTagQuery("");
                    setShowTagOptions(false);
                  }}
                >
                  <span>Все теги</span>
                  <span className="metaText">сброс</span>
                </button>
                {filteredTagOptions.map((option) => (
                  <button
                    key={option}
                    type="button"
                    className="sourceOptionReact"
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => {
                      setTag(option);
                      setTagQuery(option);
                      setShowTagOptions(false);
                    }}
                  >
                    <span>{option}</span>
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>

        <div className="digestFiltersRow digestFiltersRowSecondary">
          <label className="field">
            <span>Месяц</span>
            <select value={month} onChange={(event) => setMonth(event.target.value)}>
              <option value="">Все месяцы</option>
              {months.map((option) => (
                <option key={option} value={option}>
                  {firstOpenMonth && option < firstOpenMonth ? `${option} · архив` : option}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Оценка от</span>
            <input type="number" value={scoreMin} onChange={(event) => setScoreMin(Number(event.target.value || 0))} />
          </label>
          <label className="field">
            <span>Оценка до</span>
            <input type="number" value={scoreMax} onChange={(event) => setScoreMax(Number(event.target.value || 100))} />
          </label>
        </div>

        {loading ? (
          <div className="emptyState"><LoadingState label="Загружаем бизнес-сигналы…" /></div>
        ) : digestCandidates.length ? (
            <div className="digestQueuePanel">
              <div className="digestQueueHeader">
                <div>
                  <strong>Очередь выпуска</strong>
                  <div className="metaText">
                    {savedDraft?.month === activeMonth ? "Порядок можно менять вручную и сохранять в draft" : "Стартовый порядок синхронизирован с preview от backend"}
                  </div>
                </div>
                <div className="digestQueueHeaderMeta">
                  {hasManualChanges ? <span className="miniPill warn">есть несохранённые изменения</span> : null}
                  <span className="badge">{orderedDigestCandidates.length}</span>
                </div>
              </div>
              <div className="digestPickListReact">
              {orderedDigestCandidates.map((article, index) => (
                <article className="digestPickRow" key={article.id}>
                  <div className="digestPickTop">
                    <div className="digestPickIndex">{String(index + 1).padStart(2, "0")}</div>
                    <div className="digestPickMain">
                      <div className="digestPickMetaRow">
                        <span className="miniPill muted">{article.tag}</span>
                        <span className="miniPill ok">{Math.round(Number(article.score || 0))}</span>
                      </div>
                      <a href={article.url} target="_blank" rel="noreferrer">
                        <strong>{article.title}</strong>
                      </a>
                    </div>
                    {isArchiveMonth ? null : (
                    <div className="digestPickActions">
                      <button
                        type="button"
                        className="ghostButton compactButton"
                        disabled={index === 0}
                        onClick={() => moveDigestItem(article.id, -1)}
                        aria-label="Поднять материал выше"
                      >
                        ↑
                      </button>
                      <button
                        type="button"
                        className="ghostButton compactButton"
                        disabled={index === orderedDigestCandidates.length - 1}
                        onClick={() => moveDigestItem(article.id, 1)}
                        aria-label="Опустить материал ниже"
                      >
                        ↓
                      </button>
                      <button type="button" className="ghostButton compactButton" onClick={() => removeFromIssue(article.id)}>
                        Из выпуска
                      </button>
                      <button type="button" className="ghostButton compactButton" disabled={busy} onClick={() => void removeFromDigest(article.id)}>
                        Из дайджеста
                      </button>
                    </div>
                    )}
                  </div>
                  <div className="digestPickSummary">{article.summary || branding?.issue.empty_summary_text || "Суть ещё не сформирована."}</div>
                  <div className="digestPickFooter">
                    <span>{article.source}</span>
                    <span>{formatDate(article.date)}</span>
                    <span>{article.raw_text_chars || 0} симв.</span>
                  </div>
                </article>
              ))}
            </div>
            {availableDigestCandidates.length && !isArchiveMonth ? (
              <div className="digestAvailablePanel">
                <div className="panelHeader">
                  <h3>Доступные материалы</h3>
                  <span className="metaText">Можно добавить в сохранённый выпуск без смены фильтров</span>
                </div>
                <div className="digestAvailableList">
                  {availableDigestCandidates.slice(0, 12).map((article) => (
                    <div className="digestAvailableRow" key={article.id}>
                      <div>
                        <strong>{article.title}</strong>
                        <div className="metaText">{article.tag} · {article.source}</div>
                      </div>
                      <button type="button" className="ghostButton compactButton" onClick={() => addToIssue(article.id)}>
                        Добавить
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : (
          <div className="emptyState">Нет статей со статусом «В дайджест» по текущим фильтрам.</div>
        )}
      </section>

      <aside className="digestBuilderSide">
        <section className="digestControlCard">
          <div className="panelHeader">
            <h3>Действия по выпуску</h3>
            <span className="metaText">Draft и экспорт работают по текущей выборке</span>
          </div>
          <div className="digestToolbar digestToolbarStack">
            {isArchiveMonth ? null : (
              <div className="digestActionGroup">
                <span className="digestGroupLabel">Сохранение</span>
                <button type="button" className="ghostButton" disabled={draftBusy} onClick={() => void handleSaveDraft()}>
                  {draftBusy ? "Сохраняем draft…" : "Сохранить draft"}
                </button>
                <button type="button" className="ghostButton" disabled={!hasManualChanges} onClick={resetDraftQueue}>
                  Сбросить изменения
                </button>
              </div>
            )}
            <div className="digestActionGroup">
              <span className="digestGroupLabel">Экспорт</span>
              <button type="button" className="primaryButton" onClick={() => void handleDigestExport("pdf")}>
                PDF
              </button>
              <button type="button" className="ghostButton" onClick={() => void handleDigestExport("docx")}>
                DOCX
              </button>
              <button type="button" className="ghostButton" onClick={() => void handleDigestExport("html")}>
                HTML
              </button>
            </div>
          </div>
          <div className="digestControlMeta">
            <div><strong>{activeMonth}</strong></div>
            <div className="metaText">Месяц выпуска</div>
            <div><strong>{exportPreviewItems.length}</strong></div>
            <div className="metaText">Карточек в финальном preview</div>
            <div><strong>{hasManualChanges ? "локальные правки" : "backend preview"}</strong></div>
            <div className="metaText">
              {hasManualChanges
                ? "Порядок/состав уже изменён на экране, но preview обновится после сохранения draft"
                : "Preview уже соответствует сохранённому draft или текущей backend-выборке"}
            </div>
          </div>
        </section>

        <section className="digestPreviewSurface">
          <div className="panelHeader">
            <h3>Предпросмотр выпуска</h3>
            <span className="metaText">Тот же HTML, что пойдёт в экспорт</span>
          </div>
          {digestPreviewHtml ? (
            <iframe
              className="digestPreviewIframe"
              title="Предпросмотр дайджеста"
              srcDoc={digestPreviewHtml}
              sandbox=""
            />
          ) : (
            <div className="digestPreviewEmpty">{branding?.issue.preview_empty_text || "В текущей выборке нет сигналов для превью."}</div>
          )}
        </section>
      </aside>
    </div>
  );

  const brandingWorkspace = isAdmin && branding ? (
    <div className="digestBrandingWorkspace">
      <section className="settingsCard digestBrandingCard">
        <div className="panelHeader">
          <h2>Оформление выпуска</h2>
          <button type="button" className="primaryButton" disabled={brandingBusy} onClick={() => void handleSaveBranding()}>
            Сохранить оформление
          </button>
        </div>
        <div className="settingsGrid">
          <label className="field">
            <span>Бренд</span>
            <input
              value={branding.header.brand_text}
              onChange={(event) => updateBrandingSection("header", { ...branding.header, brand_text: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Слоган</span>
            <input
              value={branding.header.brand_suffix}
              onChange={(event) => updateBrandingSection("header", { ...branding.header, brand_suffix: event.target.value })}
            />
          </label>
          <label className="field fieldWide">
            <span>Подразделение</span>
            <input
              value={branding.header.department_text}
              onChange={(event) => updateBrandingSection("header", { ...branding.header, department_text: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Плашка шапки</span>
            <input
              value={branding.hero.badge}
              onChange={(event) => updateBrandingSection("hero", { ...branding.hero, badge: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Заголовок шапки</span>
            <input
              value={branding.hero.headline}
              onChange={(event) => updateBrandingSection("hero", { ...branding.hero, headline: event.target.value })}
            />
          </label>
          <label className="field fieldWide">
            <span>Подзаголовок шапки</span>
            <input
              value={branding.hero.subtitle}
              onChange={(event) => updateBrandingSection("hero", { ...branding.hero, subtitle: event.target.value })}
            />
          </label>
          <label className="field fieldWide">
            <span>Ссылка на изображение шапки</span>
            <input
              value={branding.hero.image_url}
              onChange={(event) => updateBrandingSection("hero", { ...branding.hero, image_url: event.target.value })}
              placeholder="https://example.com/hero.jpg"
            />
          </label>
          <label className="field">
            <span>Текст предпросмотра письма</span>
            <input
              value={branding.issue.preheader}
              onChange={(event) => updateBrandingSection("issue", { ...branding.issue, preheader: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Шаблон заголовка</span>
            <input
              value={branding.issue.title_template}
              onChange={(event) => updateBrandingSection("issue", { ...branding.issue, title_template: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Шаблон заголовка с месяцем</span>
            <input
              value={branding.issue.title_template_with_month}
              onChange={(event) => updateBrandingSection("issue", { ...branding.issue, title_template_with_month: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Заголовок новостей</span>
            <input
              value={branding.issue.news_title}
              onChange={(event) => updateBrandingSection("issue", { ...branding.issue, news_title: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Призыв к действию</span>
            <input
              value={branding.issue.read_more_label}
              onChange={(event) => updateBrandingSection("issue", { ...branding.issue, read_more_label: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Запасной текст сути</span>
            <input
              value={branding.issue.empty_summary_text}
              onChange={(event) => updateBrandingSection("issue", { ...branding.issue, empty_summary_text: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Пустое превью</span>
            <input
              value={branding.issue.preview_empty_text}
              onChange={(event) => updateBrandingSection("issue", { ...branding.issue, preview_empty_text: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Период без месяца</span>
            <input
              value={branding.issue.period_label_all}
              onChange={(event) => updateBrandingSection("issue", { ...branding.issue, period_label_all: event.target.value })}
            />
          </label>
          <label className="field fieldWide">
            <span>Шаблон вступления</span>
            <textarea
              value={branding.issue.intro_template}
              onChange={(event) => updateBrandingSection("issue", { ...branding.issue, intro_template: event.target.value })}
            />
          </label>
          <label className="field fieldWide">
            <span>Шаблон вступления с месяцем</span>
            <textarea
              value={branding.issue.intro_template_with_month}
              onChange={(event) => updateBrandingSection("issue", { ...branding.issue, intro_template_with_month: event.target.value })}
              placeholder="Используйте {month}"
            />
          </label>
          <label className="field fieldWide">
            <span>Контактный текст</span>
            <textarea
              value={branding.footer.contact_text}
              onChange={(event) => updateBrandingSection("footer", { ...branding.footer, contact_text: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Эл. почта</span>
            <input
              value={branding.footer.contact_email}
              onChange={(event) => updateBrandingSection("footer", { ...branding.footer, contact_email: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Примечание</span>
            <input
              value={branding.footer.note}
              onChange={(event) => updateBrandingSection("footer", { ...branding.footer, note: event.target.value })}
            />
          </label>
        </div>
        <div className="digestSocialsEditor">
          <div className="panelHeader">
            <h3>Соцсети и каналы</h3>
            <button type="button" className="ghostButton" onClick={addFooterSocial}>
              Добавить
            </button>
          </div>
          <div className="digestSocialsList">
            {branding.footer.socials.map((item, index) => (
              <div className="digestSocialRow" key={`${item.label}-${index}`}>
                <label className="field">
                  <span>Название</span>
                  <input value={item.label} onChange={(event) => updateFooterSocial(index, "label", event.target.value)} />
                </label>
                <label className="field">
                  <span>Текст</span>
                  <input value={item.text} onChange={(event) => updateFooterSocial(index, "text", event.target.value)} />
                </label>
                <label className="field">
                  <span>Цвет</span>
                  <input value={item.accent} onChange={(event) => updateFooterSocial(index, "accent", event.target.value)} />
                </label>
                <button type="button" className="ghostButton compactButton" onClick={() => removeFooterSocial(index)}>
                  Удалить
                </button>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="digestPreviewSurface">
        <div className="panelHeader">
          <h3>Live preview</h3>
          <span className="metaText">Сразу видно, как изменится финальный HTML</span>
        </div>
        {digestPreviewHtml ? (
          <iframe
            className="digestPreviewIframe"
            title="Предпросмотр дайджеста"
            srcDoc={digestPreviewHtml}
            sandbox=""
          />
        ) : (
          <div className="digestPreviewEmpty">{branding.issue.preview_empty_text || "В текущей выборке нет сигналов для превью."}</div>
        )}
      </section>
    </div>
  ) : null;

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Месячный дайджест</h1>
          <p>Сборка выпуска, сохранение draft и финальный экспорт теперь живут в одном рабочем экране.</p>
        </div>
        <div className="statusPill">{digestCandidates.length} статей</div>
      </header>

      <section className="panel">
        {busy ? <InlineLoader label={exporting ? `Готовим документ ${exporting}…` : "Готовим документ…"} /> : null}
        <div className="panelHeader">
          <h2>{viewMode === "issue" ? "Сборка выпуска" : "Оформление выпуска"}</h2>
          {isAdmin ? (
            <div className="digestModeSwitch" role="tablist" aria-label="Режим экрана дайджеста">
              <button
                type="button"
                className={viewMode === "issue" ? "primaryButton" : "ghostButton"}
                onClick={() => setViewMode("issue")}
              >
                Сборка выпуска
              </button>
              <button
                type="button"
                className={viewMode === "branding" ? "primaryButton" : "ghostButton"}
                onClick={() => setViewMode("branding")}
              >
                Оформление
              </button>
            </div>
          ) : null}
        </div>
        {viewMode === "issue" ? issueWorkspace : brandingWorkspace}
      </section>
    </section>
  );
}

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function defaultHighlightCards(): DigestHighlightCard[] {
  return [
    { metric: "total", icon: "doc", prefix: "", suffix: "", noun_one: "новость", noun_few: "новости", noun_many: "новостей" },
    { metric: "analytics", icon: "chart", prefix: "аналитических", suffix: "", noun_one: "материал", noun_few: "материала", noun_many: "материалов" },
    { metric: "business", icon: "people", prefix: "", suffix: "для бизнеса", noun_one: "возможность", noun_few: "возможности", noun_many: "возможностей" },
  ];
}

function formatDate(value: string | null) {
  return value ? new Date(`${value}T00:00:00`).toLocaleDateString("ru-RU") : "—";
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
