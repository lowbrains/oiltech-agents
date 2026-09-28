import { useId, useState } from "react";
import type { ClipboardEvent, KeyboardEvent } from "react";
import styles from "./Tags.module.css";

type Props = {
  label: string;
  values: string[] | null | undefined;
  onChange: (values: string[]) => void;
  placeholder?: string;
  tone?: "default" | "stop";
};

/** Список через перевод строки или запятую → отдельные слова. Пустые куски отбрасываем,
 *  иначе в промпт уедет мусор; повтор без учёта регистра не добавляем. */
export function mergeKeywords(current: string[], raw: string): string[] {
  const seen = new Set(current.map((word) => word.toLocaleLowerCase("ru")));
  const next = [...current];
  raw
    .split(/[\n,]/)
    .map((word) => word.trim())
    .filter(Boolean)
    .forEach((word) => {
      const key = word.toLocaleLowerCase("ru");
      if (seen.has(key)) return;
      seen.add(key);
      next.push(word);
    });
  return next;
}

// Ключевые слова и стоп-слова — чипсами с крестиком (документ заказчика 19.09), а не
// строкой текста. Ввод по-прежнему принимает и столбик, и запятые: 13.09 заказчик
// просил вводить ключи строчками, а вставку списка из таблицы ломать нельзя.
export function KeywordChips({ label, values, onChange, placeholder, tone = "default" }: Props) {
  const inputId = useId();
  const [draft, setDraft] = useState("");
  const words = values || [];

  function commit(raw: string) {
    const next = mergeKeywords(words, raw);
    if (next.length !== words.length) onChange(next);
    setDraft("");
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      if (draft.trim()) commit(draft);
    }
  }

  function handlePaste(event: ClipboardEvent<HTMLInputElement>) {
    const text = event.clipboardData.getData("text");
    if (!/[\n,]/.test(text)) return;
    event.preventDefault();
    commit(`${draft}${text}`);
  }

  return (
    <div className={styles.chipsField}>
      <label className={styles.chipsLabel} htmlFor={inputId}>
        {label} <span className={styles.chipsCount}>{words.length}</span>
      </label>
      <div className={styles.chipsBox}>
        {words.map((word, index) => (
          // Ключ и удаление — по позиции: в сохранённых списках бывают точные повторы.
          <span key={`${index}-${word}`} className={tone === "stop" ? `${styles.chip} ${styles.chipStop}` : styles.chip}>
            {word}
            <button
              type="button"
              className={styles.chipRemove}
              aria-label={`Удалить «${word}»`}
              onClick={() => onChange(words.filter((_, position) => position !== index))}
            >
              ×
            </button>
          </span>
        ))}
        <input
          id={inputId}
          className={styles.chipsInput}
          value={draft}
          placeholder={words.length ? "Добавить…" : placeholder}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          // Набранное, но не подтверждённое Enter слово не теряем при уходе из поля.
          onBlur={() => {
            if (draft.trim()) commit(draft);
          }}
        />
      </div>
    </div>
  );
}
