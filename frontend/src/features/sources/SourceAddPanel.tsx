import { useState } from "react";
import type { CreateSourcePayload, ManualArticleImportPayload } from "../../api/types";
import styles from "./Sources.module.css";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onCreateSource: (payload: CreateSourcePayload) => Promise<boolean>;
  onImportArticle: (payload: ManualArticleImportPayload) => Promise<boolean>;
  showToast: ToastWriter;
};

// Добавление — редкое действие администратора, поэтому свёрнуто над таблицей: заказчик
// (документ 19.09) просил, чтобы главным на экране был список источников, а не формы.
export function SourceAddPanel({ onCreateSource, onImportArticle, showToast }: Props) {
  const [newSourceName, setNewSourceName] = useState("");
  const [newSourceUrl, setNewSourceUrl] = useState("");
  const [newSourceFrequency, setNewSourceFrequency] = useState("ежедневно");
  const [manualArticleUrl, setManualArticleUrl] = useState("");
  const [manualArticleSourceId, setManualArticleSourceId] = useState("");
  const [manualArticleProcess, setManualArticleProcess] = useState(true);

  async function handleCreateSource() {
    if (!newSourceName.trim() || !newSourceUrl.trim()) {
      showToast("Введите название и ссылку на источник", "error");
      return;
    }
    const created = await onCreateSource({
      name: newSourceName.trim(),
      url: newSourceUrl.trim(),
      update_frequency: newSourceFrequency || null,
      category: "manual",
      priority: 1,
    });
    if (created) {
      setNewSourceName("");
      setNewSourceUrl("");
      setNewSourceFrequency("ежедневно");
    }
  }

  async function handleManualArticleImport() {
    if (!manualArticleUrl.trim()) {
      showToast("Вставьте прямую ссылку на статью", "error");
      return;
    }
    const imported = await onImportArticle({
      url: manualArticleUrl.trim(),
      source_id: manualArticleSourceId.trim() ? Number(manualArticleSourceId) : undefined,
      process: manualArticleProcess,
    });
    if (imported) {
      setManualArticleUrl("");
      setManualArticleSourceId("");
      setManualArticleProcess(true);
    }
  }

  return (
    <details className={`panel ${styles.addPanel}`}>
      <summary>Добавить источник или статью по ссылке</summary>

      <div className={styles.addSection}>
        <div className="panelHeader">
          <h2>Добавить источник</h2>
        </div>
        <p className="metaText" style={{ margin: "0 0 4px" }}>
          Вставьте ссылку на сайт источника — система сама найдёт RSS-ленту. Если ленты нет, источник будет читаться со страницы новостей.
        </p>
        <div className="sourceCreateGrid">
          <label className="field">
            <span>Название</span>
            <input value={newSourceName} onChange={(event) => setNewSourceName(event.target.value)} placeholder="Название источника" />
          </label>
          <label className="field">
            <span>Ссылка на источник</span>
            <input value={newSourceUrl} onChange={(event) => setNewSourceUrl(event.target.value)} placeholder="https://сайт.com" />
          </label>
          <label className="field">
            <span>Частота</span>
            <select value={newSourceFrequency} onChange={(event) => setNewSourceFrequency(event.target.value)}>
              <option value="ежечасно">Ежечасно</option>
              <option value="ежедневно">Ежедневно</option>
              <option value="еженедельно">Еженедельно</option>
            </select>
          </label>
          <button type="button" className="primaryButton" onClick={() => void handleCreateSource()}>
            Добавить
          </button>
        </div>
      </div>

      <div className={styles.addSection}>
        <div className="panelHeader">
          <h2>Добавить статью по ссылке</h2>
        </div>
        <p className="metaText" style={{ margin: "0 0 4px" }}>
          Для материалов, которые не пришли через RSS или скрапинг, можно вручную внести прямую ссылку на статью. Система сохранит текст в БД и, при необходимости, поставит AI-обработку в очередь.
        </p>
        <div className="sourceCreateGrid manualArticleGrid">
          <label className="field fieldWide">
            <span>Ссылка на статью</span>
            <input
              value={manualArticleUrl}
              onChange={(event) => setManualArticleUrl(event.target.value)}
              placeholder="https://site.com/news/article"
            />
          </label>
          <label className="field">
            <span>ID источника, если нужен</span>
            <input
              value={manualArticleSourceId}
              onChange={(event) => setManualArticleSourceId(event.target.value.replace(/[^\d]/g, ""))}
              placeholder="например, 12"
              inputMode="numeric"
            />
          </label>
          <label className="field fieldCheckbox">
            <span>После импорта</span>
            <div className="checkboxRow">
              <input
                type="checkbox"
                checked={manualArticleProcess}
                onChange={(event) => setManualArticleProcess(event.target.checked)}
              />
              <span>Сразу запустить AI-обработку</span>
            </div>
          </label>
          <button type="button" className="primaryButton" onClick={() => void handleManualArticleImport()}>
            Импортировать статью
          </button>
        </div>
      </div>
    </details>
  );
}
