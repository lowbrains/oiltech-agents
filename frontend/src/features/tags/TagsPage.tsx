import { useEffect, useMemo, useRef, useState } from "react";
import { deleteTag, listTags, saveTags } from "../../api/tags";
import type { Tag } from "../../api/types";
import { KeywordChips } from "./KeywordChips";
import styles from "./Tags.module.css";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

// Служебный тег-приёмник: не тематика, а предохранитель классификации. Сервер запрещает
// его удалять и выключать (repository.SYSTEM_TAG_UNCLASSIFIED), здесь прячем кнопку
// «Удалить» и подписываем строку, чтобы не выглядело недоработкой экрана.
const SYSTEM_TAG_UNCLASSIFIED = "Не классифицировано / новая тема";

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

function pluralRu(count: number, one: string, few: string, many: string) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return `${count} ${one}`;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${count} ${few}`;
  return `${count} ${many}`;
}

// Ключ группы не может быть именем: имя правят в этой же форме, и не может быть
// позицией: удаление тега выше сдвигает её (ревью F — раскрывалось соседнее).
// У несохранённой группы — клиентский ключ, выданный при создании.
function groupKey(tag: Tag, index: number) {
  return tag.id ? `id-${tag.id}` : tag.client_key ?? `new-${index}`;
}

export function TagsPage({ onUnauthorized, showToast }: Props) {
  const [tags, setTags] = useState<Tag[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  // По умолчанию направления свёрнуты (документ заказчика 19.09): 13 развёрнутых
  // тематик по 30–50 ключей — это несколько экранов прокрутки.
  const [openGroups, setOpenGroups] = useState<Set<string>>(() => new Set());
  const clientKeySeq = useRef(0);

  function newClientKey() {
    clientKeySeq.current += 1;
    return `new-${clientKeySeq.current}`;
  }

  useEffect(() => {
    void reload();
  }, []);

  async function reload() {
    try {
      setLoading(true);
      setTags(await listTags());
    } catch (error) {
      handleError(error, "Не удалось загрузить теги");
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

  const parents = useMemo(() => tags.filter((tag) => !tag.parent_name), [tags]);
  const parentKeys = parents.map((parent) => groupKey(parent, tags.indexOf(parent)));
  const allOpen = parentKeys.length > 0 && parentKeys.every((key) => openGroups.has(key));

  function toggleGroup(key: string) {
    setOpenGroups((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function updateTag(index: number, field: keyof Tag, value: string | boolean | string[]) {
    setTags((prev) => {
      const target = prev[index];
      // Переименование НАПРАВЛЕНИЯ обязано протянуться в его подтеги. Связь хранится
      // именем, и без этого каскада сохранение делало все подтеги корневыми — молча,
      // без ошибки. Бэкенд теперь такое отклоняет, но чинить надо здесь, в источнике.
      const renamingParent =
        field === "name" && !target.parent_name && typeof value === "string" && value !== target.name;
      const previousName = target.name;
      return prev.map((item, currentIndex) => {
        if (currentIndex === index) return { ...item, [field]: value };
        if (renamingParent && item.parent_name === previousName) {
          return { ...item, parent_name: value as string };
        }
        return item;
      });
    });
  }

  function addParentTag() {
    // Новое направление сразу раскрыто: его только что добавили, чтобы заполнить.
    const clientKey = newClientKey();
    setOpenGroups((open) => new Set(open).add(clientKey));
    setTags((prev) => [
      ...prev,
      {
        id: null,
        client_key: clientKey,
        parent_name: null,
        name: "Новое направление",
        name_en: "",
        description: "",
        keywords_json: [],
        keywords_en_json: [],
        negative_keywords_json: [],
        enabled: true,
        sort_order: (prev.length + 1) * 10,
      },
    ]);
  }

  function addSubtag(parentName: string) {
    const clientKey = newClientKey();
    setTags((prev) => [
      ...prev,
      {
        id: null,
        client_key: clientKey,
        parent_name: parentName,
        name: "Новый подтег",
        name_en: "",
        description: "",
        keywords_json: [],
        keywords_en_json: [],
        enabled: true,
        sort_order: (prev.length + 1) * 10,
      },
    ]);
  }

  async function removeTag(index: number) {
    const item = tags[index];
    if (item.id) {
      try {
        setBusy(true);
        await deleteTag(item.id);
      } catch (error) {
        handleError(error, "Не удалось удалить тег");
        return;
      } finally {
        setBusy(false);
      }
    }
    setTags((prev) => prev.filter((_, currentIndex) => currentIndex !== index));
  }

  async function handleSave() {
    try {
      setBusy(true);
      await saveTags(
        tags.map(({ client_key: _clientKey, ...tag }, index) => ({ ...tag, sort_order: tag.sort_order || (index + 1) * 10 })),
      );
      showToast("Теги сохранены");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось сохранить теги");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Теги</h1>
        </div>
        <div className="statusPill">
          {pluralRu(parents.length, "направление", "направления", "направлений")} ·{" "}
          {pluralRu(tags.length - parents.length, "подтег", "подтега", "подтегов")}
        </div>
      </header>

      <section className="panel">
        {busy ? <InlineLoader label="Сохраняем теги…" /> : null}
        <div className="panelHeader settingsHeader">
          <h2>Направления и подтеги</h2>
          <div className="settingsActions">
            <button
              type="button"
              className="ghostButton"
              disabled={!parentKeys.length}
              onClick={() => setOpenGroups(allOpen ? new Set() : new Set(parentKeys))}
            >
              {allOpen ? "Свернуть все" : "Развернуть все"}
            </button>
            <button type="button" className="primaryButton" onClick={() => void handleSave()}>
              Сохранить
            </button>
          </div>
        </div>

        {loading ? (
          <div className="emptyState"><LoadingState label="Загружаем теги…" /></div>
        ) : (
          <div className={styles.list}>
            {parents.map((parent) => {
              const parentIndex = tags.indexOf(parent);
              const key = groupKey(parent, parentIndex);
              const open = openGroups.has(key);
              const children = tags.filter((tag) => tag.parent_name === parent.name);
              const keywordCount = (parent.keywords_json || []).length + (parent.keywords_en_json || []).length;
              const bodyId = `tag-group-${key}`;
              return (
                <div className={styles.group} data-open={open} key={key}>
                  <button
                    type="button"
                    className={styles.groupHead}
                    aria-expanded={open}
                    aria-controls={bodyId}
                    onClick={() => toggleGroup(key)}
                  >
                    <span className={styles.chevron} aria-hidden="true">▸</span>
                    <span className={styles.groupName}>{parent.name || "Без названия"}</span>
                    {!parent.enabled ? <span className="miniPill muted">выкл</span> : null}
                    <span className={styles.groupMeta}>
                      {pluralRu(children.length, "подтег", "подтега", "подтегов")} ·{" "}
                      {pluralRu(keywordCount, "ключевое слово", "ключевых слова", "ключевых слов")}
                    </span>
                  </button>

                  {open ? (
                    <div className={styles.groupBody} id={bodyId}>
                      <div className={styles.row}>
                        <label className="toggleLabel">
                          <input
                            type="checkbox"
                            checked={parent.enabled}
                            onChange={(event) => updateTag(parentIndex, "enabled", event.target.checked)}
                          />
                          <span>вкл</span>
                        </label>
                        <label className="field">
                          <span>Направление</span>
                          <input value={parent.name} onChange={(event) => updateTag(parentIndex, "name", event.target.value)} />
                        </label>
                        <label className="field">
                          <span>Описание для AI</span>
                          <input
                            value={parent.description || ""}
                            onChange={(event) => updateTag(parentIndex, "description", event.target.value)}
                          />
                        </label>
                      </div>
                      <KeywordChips
                        label="Ключевые слова RU — по ним ищем и тегируем"
                        values={parent.keywords_json}
                        onChange={(values) => updateTag(parentIndex, "keywords_json", values)}
                        placeholder="ГРП, гидроразрыв, проппант"
                      />
                      <KeywordChips
                        label="Ключевые слова EN"
                        values={parent.keywords_en_json}
                        onChange={(values) => updateTag(parentIndex, "keywords_en_json", values)}
                        placeholder="hydraulic fracturing, proppant"
                      />
                      <KeywordChips
                        label="Стоп-слова — довод против статьи"
                        tone="stop"
                        values={parent.negative_keywords_json}
                        onChange={(values) => updateTag(parentIndex, "negative_keywords_json", values)}
                        placeholder="футбол, банкротство, вакансия"
                      />

                      {children.length ? (
                        <div className={styles.children}>
                          {children.map((child) => {
                            const childIndex = tags.indexOf(child);
                            return (
                              <div className={styles.child} key={child.id ?? child.client_key ?? `child-${childIndex}`}>
                                <div className={styles.row}>
                                  <label className="toggleLabel">
                                    <input
                                      type="checkbox"
                                      checked={child.enabled}
                                      onChange={(event) => updateTag(childIndex, "enabled", event.target.checked)}
                                    />
                                    <span>вкл</span>
                                  </label>
                                  <label className="field">
                                    <span>Подтег</span>
                                    <input value={child.name} onChange={(event) => updateTag(childIndex, "name", event.target.value)} />
                                  </label>
                                  <label className="field">
                                    <span>Описание для AI</span>
                                    <input
                                      value={child.description || ""}
                                      onChange={(event) => updateTag(childIndex, "description", event.target.value)}
                                    />
                                  </label>
                                </div>
                                <KeywordChips
                                  label="Ключевые слова RU"
                                  values={child.keywords_json}
                                  onChange={(values) => updateTag(childIndex, "keywords_json", values)}
                                />
                                <KeywordChips
                                  label="Ключевые слова EN"
                                  values={child.keywords_en_json}
                                  onChange={(values) => updateTag(childIndex, "keywords_en_json", values)}
                                />
                                <div className={styles.childFoot}>
                                  <span />
                                  <button
                                    type="button"
                                    className="ghostButton dangerButton"
                                    onClick={() => void removeTag(childIndex)}
                                  >
                                    Удалить подтег
                                  </button>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      ) : null}

                      <div className={styles.groupFoot}>
                        <button type="button" className="ghostButton" onClick={() => addSubtag(parent.name)}>
                          + Добавить подтег
                        </button>
                        {parent.name === SYSTEM_TAG_UNCLASSIFIED ? (
                          <span className="muted">
                            Служебный тег: сюда попадают статьи, не подошедшие ни к одной тематике. Удалить нельзя.
                          </span>
                        ) : (
                          <button type="button" className="ghostButton dangerButton" onClick={() => void removeTag(parentIndex)}>
                            Удалить направление
                          </button>
                        )}
                      </div>
                    </div>
                  ) : null}
                </div>
              );
            })}
            <div>
              <button type="button" className="ghostButton" onClick={addParentTag}>
                + Добавить направление
              </button>
            </div>
          </div>
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
