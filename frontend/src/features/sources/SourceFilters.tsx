import { strategyLabel, TRIAGE_FILTER_OPTIONS } from "./sourceUtils";

type Props = {
  search: string;
  strategy: string;
  triageKey: string;
  onSearchChange: (value: string) => void;
  onStrategyChange: (value: string) => void;
  onTriageChange: (value: string) => void;
  onReset: () => void;
};

// playwright отсутствовал, хотя это рабочая стратегия десятков источников —
// отфильтровать их было нельзя. "none" наоборот не ставит НИКТО: в БД там NULL,
// а фильтр сравнивает строгим равенством, поэтому опция была мёртвой.
const STRATEGY_OPTIONS = ["rss", "request", "playwright", "telegram"];

// Состояние (штатно / требует внимания / …) выбирается плитками над таблицей: два
// списка «Статус» и «Покрытие» дублировали их и делали экран «административным».
export function SourceFilters(props: Props) {
  return (
    <div className="sourceFiltersGrid">
      <label className="field">
        <span>Поиск</span>
        <input value={props.search} onChange={(event) => props.onSearchChange(event.target.value)} placeholder="Название или ссылка" />
      </label>
      <label className="field">
        <span>Тип</span>
        <select value={props.strategy} onChange={(event) => props.onStrategyChange(event.target.value)}>
          <option value="">Все</option>
          {STRATEGY_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {strategyLabel(option)}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Проблема</span>
        <select value={props.triageKey} onChange={(event) => props.onTriageChange(event.target.value)}>
          {TRIAGE_FILTER_OPTIONS.map((option) => (
            <option key={option.value || "all"} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <button type="button" className="ghostButton" onClick={props.onReset}>
        Сбросить
      </button>
    </div>
  );
}
