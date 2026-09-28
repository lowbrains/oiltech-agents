import { useEffect, useMemo, useState } from "react";
import { deleteScoringCriterion, listScoringCriteria, saveScoringCriteria } from "../../api/scoring";
import type { ScoringCriterion } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

export function ScoringPage({ onUnauthorized, showToast }: Props) {
  const [criteria, setCriteria] = useState<ScoringCriterion[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void reload();
  }, []);

  async function reload() {
    try {
      setLoading(true);
      setCriteria(await listScoringCriteria());
    } catch (error) {
      handleError(error, "Не удалось загрузить критерии");
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

  const totalWeight = useMemo(() => criteria.reduce((sum, item) => sum + Number(item.weight || 0), 0), [criteria]);

  function updateCriterion(index: number, field: keyof ScoringCriterion, value: string | number | string[]) {
    setCriteria((prev) =>
      prev.map((item, currentIndex) => {
        if (currentIndex !== index) return item;
        return { ...item, [field]: value };
      }),
    );
  }

  function addCriterion() {
    setCriteria((prev) => [
      ...prev,
      {
        id: null,
        name: "Новый критерий",
        description: "",
        weight: 0,
        keywords_json: [],
        keywords_en_json: [],
        sort_order: (prev.length + 1) * 10,
      },
    ]);
  }

  function normalizeWeights() {
    const count = criteria.length;
    if (!count) return;
    const each = Math.floor(100 / count);
    let rest = 100;
    setCriteria((prev) =>
      prev.map((item, index) => {
        const weight = index === count - 1 ? rest : each;
        rest -= each;
        return { ...item, weight };
      }),
    );
    showToast("Веса нормализованы до 100%");
  }

  async function removeCriterion(index: number) {
    const item = criteria[index];
    if (item.id) {
      try {
        setBusy(true);
        await deleteScoringCriterion(item.id);
      } catch (error) {
        handleError(error, "Не удалось удалить критерий");
        return;
      } finally {
        setBusy(false);
      }
    }
    setCriteria((prev) => prev.filter((_, currentIndex) => currentIndex !== index));
  }

  async function handleSave() {
    try {
      setBusy(true);
      await saveScoringCriteria(criteria.map((item, index) => ({ ...item, sort_order: item.sort_order || (index + 1) * 10 })));
      showToast("Скоринг сохранён");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось сохранить критерии");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Скоринг</h1>
        </div>
        <div className={`statusPill ${totalWeight !== 100 ? "warningPill" : ""}`}>Сумма весов: {totalWeight}%</div>
      </header>

      <section className="panel">
        {busy ? <InlineLoader label="Сохраняем скоринг…" /> : null}
        <div className="panelHeader settingsHeader">
          <h2>Критерии оценки</h2>
          <div className="settingsActions">
            <button type="button" className="ghostButton" onClick={normalizeWeights}>
              Нормализовать
            </button>
            <button type="button" className="primaryButton" disabled={totalWeight !== 100} onClick={() => void handleSave()}>
              Сохранить
            </button>
          </div>
        </div>

        {loading ? (
          <div className="emptyState"><LoadingState label="Загружаем критерии…" /></div>
        ) : (
          <div className="settingsStack">
            {criteria.map((criterion, index) => (
              <div className="settingsCard" key={criterion.id ?? `new-${index}`}>
                <div className="settingsGrid">
                  <label className="field">
                    <span>Параметр</span>
                    <input value={criterion.name} onChange={(event) => updateCriterion(index, "name", event.target.value)} />
                  </label>
                  <label className="field">
                    <span>Вес</span>
                    <input
                      type="number"
                      min={0}
                      max={100}
                      value={criterion.weight}
                      onChange={(event) => updateCriterion(index, "weight", Number(event.target.value || 0))}
                    />
                  </label>
                  <label className="field fieldWide">
                    <span>Описание для AI</span>
                    <textarea
                      value={criterion.description || ""}
                      onChange={(event) => updateCriterion(index, "description", event.target.value)}
                    />
                  </label>
                  {/* RU и EN — друг под другом во всю ширину (документ заказчика 19.09):
                      рядом по 240 px длинный список ключей читался только прокруткой. */}
                  <label className="field fieldWide">
                    <span>Ключевые слова RU / любые</span>
                    <textarea
                      value={(criterion.keywords_json || []).join(", ")}
                      onChange={(event) => updateCriterion(index, "keywords_json", splitKeywords(event.target.value))}
                    />
                  </label>
                  <label className="field fieldWide">
                    <span>EN-нормализация</span>
                    <textarea
                      value={(criterion.keywords_en_json || []).join(", ")}
                      onChange={(event) => updateCriterion(index, "keywords_en_json", splitKeywords(event.target.value))}
                    />
                  </label>
                </div>
                <div className="settingsCardFoot">
                  <button type="button" className="ghostButton dangerButton" onClick={() => void removeCriterion(index)}>
                    Удалить
                  </button>
                </div>
              </div>
            ))}
            <div>
              <button type="button" className="ghostButton" onClick={addCriterion}>
                + Добавить параметр
              </button>
            </div>
          </div>
        )}
      </section>
    </section>
  );
}

function splitKeywords(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
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
