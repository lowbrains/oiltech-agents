import { useEffect, useMemo, useState } from "react";
import { deleteTag, listTags, saveTags } from "../../api/tags";
import type { Tag } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

// Служебный тег-приёмник: не тематика, а предохранитель классификации. Сервер запрещает
// его удалять и выключать (repository.SYSTEM_TAG_UNCLASSIFIED), здесь прячем кнопку
// «Удалить» и подписываем строку, чтобы не выглядело недоработкой экрана.
const SYSTEM_TAG_UNCLASSIFIED = "Не классифицировано / новая тема";

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

export function TagsPage({ onUnauthorized, showToast }: Props) {
  const [tags, setTags] = useState<Tag[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

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

  function updateTag(index: number, field: keyof Tag, value: string | boolean | string[]) {
    setTags((prev) => {
      const target = prev[index];
      // Переименование РОДИТЕЛЯ обязано протянуться в его подтеги. Связь хранится
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

  /** Список через запятую ⇄ массив. Пустые куски отбрасываем, иначе в промпт уедет мусор. */
  function splitKeywords(raw: string): string[] {
    // Принимаем и перевод строки, и запятую: заказчик просил 13.09 вводить ключи
    // строчками друг под другом, но вставка списка через запятую должна работать
    // по-прежнему — иначе сломается его привычный способ переноса из таблицы.
    return raw.split(/[\n,]/).map((word) => word.trim()).filter(Boolean);
  }

  function joinKeywords(values: string[] | null | undefined): string {
    return (values || []).join("\n");
  }

  function addParentTag() {
    setTags((prev) => [
      ...prev,
      {
        id: null,
        parent_name: null,
        name: "Новый тег",
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
    setTags((prev) => [
      ...prev,
      {
        id: null,
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
      await saveTags(tags.map((tag, index) => ({ ...tag, sort_order: tag.sort_order || (index + 1) * 10 })));
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
        <div className="statusPill">{tags.length} тегов</div>
      </header>

      <section className="panel">
        {busy ? <InlineLoader label="Сохраняем теги…" /> : null}
        <div className="panelHeader">
          <h2>Иерархия тегов</h2>
          <div className="settingsActions">
            <button type="button" className="primaryButton" onClick={() => void handleSave()}>
              Сохранить
            </button>
          </div>
        </div>

        {loading ? (
          <div className="emptyState"><LoadingState label="Загружаем теги…" /></div>
        ) : (
          <div className="settingsStack">
            {parents.map((parent) => {
              const parentIndex = tags.indexOf(parent);
              const children = tags.filter((tag) => tag.parent_name === parent.name);
              return (
                <div className="tagGroupCard" key={parent.id ?? `parent-${parentIndex}`}>
                  <div className="tagRowRoot">
                    <label className="toggleLabel">
                      <input
                        type="checkbox"
                        checked={parent.enabled}
                        onChange={(event) => updateTag(parentIndex, "enabled", event.target.checked)}
                      />
                      <span>вкл</span>
                    </label>
                    <label className="field">
                      <span>Родительский тег</span>
                      <input value={parent.name} onChange={(event) => updateTag(parentIndex, "name", event.target.value)} />
                    </label>
                    <label className="field fieldWide">
                      <span>Описание для AI</span>
                      <input value={parent.description || ""} onChange={(event) => updateTag(parentIndex, "description", event.target.value)} />
                    </label>
                    <label className="field fieldWide">
                      <span>Ключевые слова RU — по одному в строке (по ним ищем и тегируем)</span>
                      <textarea
                        className="keywordList"
                        rows={8}
                        value={joinKeywords(parent.keywords_json)}
                        onChange={(event) => updateTag(parentIndex, "keywords_json", splitKeywords(event.target.value))}
                        placeholder={"ГРП\nгидроразрыв\nпроппант"}
                      />
                    </label>
                    <label className="field fieldWide">
                      <span>Keywords EN — по одному в строке</span>
                      <textarea
                        className="keywordList"
                        rows={6}
                        value={joinKeywords(parent.keywords_en_json)}
                        onChange={(event) => updateTag(parentIndex, "keywords_en_json", splitKeywords(event.target.value))}
                        placeholder={"hydraulic fracturing\nproppant"}
                      />
                    </label>
                    <label className="field fieldWide">
                      <span>Стоп-слова — по одному в строке (довод против статьи)</span>
                      <textarea
                        className="keywordList"
                        rows={4}
                        value={joinKeywords(parent.negative_keywords_json)}
                        onChange={(event) =>
                          updateTag(parentIndex, "negative_keywords_json", splitKeywords(event.target.value))
                        }
                        placeholder={"футбол\nбанкротство\nвакансия"}
                      />
                    </label>
                    <div className="settingsActions">
                      <button type="button" className="ghostButton" onClick={() => addSubtag(parent.name)}>
                        + Подтег
                      </button>
                      {parent.name === SYSTEM_TAG_UNCLASSIFIED ? (
                        <span className="muted">
                          Служебный тег: сюда попадают статьи, не подошедшие ни к одной тематике. Удалить нельзя.
                        </span>
                      ) : (
                        <button type="button" className="ghostButton dangerButton" onClick={() => void removeTag(parentIndex)}>
                          Удалить
                        </button>
                      )}
                    </div>
                  </div>

                  <div className="tagChildrenList">
                    {children.map((child) => {
                      const childIndex = tags.indexOf(child);
                      return (
                        <div className="tagRowChild" key={child.id ?? `child-${childIndex}`}>
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
                          <label className="field fieldWide">
                            <span>Описание для AI</span>
                            <input value={child.description || ""} onChange={(event) => updateTag(childIndex, "description", event.target.value)} />
                          </label>
                          <label className="field fieldWide">
                            <span>Ключевые слова RU</span>
                            <input
                              value={(child.keywords_json || []).join(", ")}
                              onChange={(event) => updateTag(childIndex, "keywords_json", splitKeywords(event.target.value))}
                            />
                          </label>
                          <label className="field fieldWide">
                            <span>Keywords EN</span>
                            <input
                              value={(child.keywords_en_json || []).join(", ")}
                              onChange={(event) => updateTag(childIndex, "keywords_en_json", splitKeywords(event.target.value))}
                            />
                          </label>
                          <button type="button" className="ghostButton dangerButton" onClick={() => void removeTag(childIndex)}>
                            Удалить
                          </button>
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}
            <button type="button" className="ghostButton" onClick={addParentTag}>
              Добавить родительский тег
            </button>
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
