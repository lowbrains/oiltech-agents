import { useEffect, useState } from "react";
import { getMaintenanceStatus, getReadinessBenchmark, runMaintenanceCleanup } from "../../api/maintenance";
import type { MaintenanceCleanupResult, MaintenanceStatus, ReadinessBenchmarkReport } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

export function MaintenancePage({ onUnauthorized, showToast }: Props) {
  const [status, setStatus] = useState<MaintenanceStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [backgroundJobDays, setBackgroundJobDays] = useState("");
  const [exportJobDays, setExportJobDays] = useState("");
  const [lastResult, setLastResult] = useState<MaintenanceCleanupResult | null>(null);
  const [benchmark, setBenchmark] = useState<ReadinessBenchmarkReport | null>(null);
  const [benchmarkLoading, setBenchmarkLoading] = useState(false);

  useEffect(() => {
    void reload();
  }, []);

  async function reload() {
    try {
      setLoading(true);
      setStatus(await getMaintenanceStatus());
    } catch (error) {
      handleError(error, "Не удалось загрузить maintenance-статус");
    } finally {
      setLoading(false);
    }
  }

  function handleError(error: unknown, fallback: string) {
    const statusCode = typeof error === "object" && error && "status" in error ? Number(error.status) : 0;
    const message = error instanceof Error ? error.message : fallback;
    if (statusCode === 401) {
      onUnauthorized();
      return;
    }
    showToast(message || fallback, "error");
  }

  async function handleCleanup() {
    try {
      setRunning(true);
      const payload = {
        background_job_days: backgroundJobDays.trim() ? Number(backgroundJobDays) : undefined,
        export_job_days: exportJobDays.trim() ? Number(exportJobDays) : undefined,
      };
      const response = await runMaintenanceCleanup(payload);
      setLastResult(response.result);
      showToast("Очистка сервиса завершена");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось выполнить service cleanup");
    } finally {
      setRunning(false);
    }
  }

  async function handleBenchmarkRun() {
    try {
      setBenchmarkLoading(true);
      setBenchmark(await getReadinessBenchmark());
      showToast("Замер в режиме чтения завершён");
    } catch (error) {
      handleError(error, "Не удалось выполнить замер");
    } finally {
      setBenchmarkLoading(false);
    }
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Обслуживание сервиса</h1>
        </div>
        <div className="panelActions">
          <a className="ghostButton compactButton" href="?screen=jobs">
            Задачи
          </a>
          <div className="statusPill">Скрытая страница</div>
        </div>
      </header>

      <section className="statsGridReact jobsStats maintenanceStats">
        <div className="statCardReact">
          <strong>{status?.expired_sessions ?? "—"}</strong>
          <span>Истекшие сессии</span>
        </div>
        <div className="statCardReact">
          <strong>{status?.stale_running_jobs ?? "—"}</strong>
          <span>Зависшие задачи</span>
        </div>
        <div className="statCardReact">
          <strong>{status?.cleanup_candidates.background_jobs ?? "—"}</strong>
          <span>Фоновые задачи к очистке</span>
        </div>
        <div className="statCardReact">
          <strong>{status?.cleanup_candidates.export_jobs ?? "—"}</strong>
          <span>Задачи экспорта к очистке</span>
        </div>
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Внешний контур</h2>
          <a className="ghostButton compactButton" href="?screen=jobs">
            Все задачи
          </a>
        </div>
        <section className="statsGridReact jobsStats externalStats">
          <div className="statCardReact">
            <strong>{status?.external_queues.totals.queued ?? "—"}</strong>
            <span>Внешние в очереди</span>
          </div>
          <div className="statCardReact">
            <strong>{status?.external_queues.totals.running ?? "—"}</strong>
            <span>Внешние выполняются</span>
          </div>
          <div className="statCardReact">
            <strong>{status?.external_queues.totals.failed ?? "—"}</strong>
            <span>Внешние с ошибкой</span>
          </div>
          <div className="statCardReact">
            <strong>{status?.external_queues.totals.expired_leases ?? "—"}</strong>
            <span>Истёкшие блокировки</span>
          </div>
        </section>
        <div className="externalMeta">
          <span>Самая старая в очереди: {formatDate(status?.external_queues.totals.oldest_queued_at)}</span>
          <span>Последний сигнал: {formatDate(status?.external_queues.totals.last_heartbeat_at)}</span>
        </div>
        {/* Сторож полос: без него 208 задач без воркера (20.09) было видно только по
            последствиям через 8,5 часа. */}
        {status?.external_queues.alerts?.length ? (
          <div className="laneAlerts" role="alert">
            {status.external_queues.alerts.map((alert) => (
              <span key={`${alert.kind}-${alert.queue ?? "all"}`}>{alert.message}</span>
            ))}
          </div>
        ) : null}
        {status?.external_queues.queues.length ? (
          <div className="externalQueueList">
            {status.external_queues.queues.map((queue) => (
              <div key={queue.queue_name} className="externalQueueRow">
                <strong>{queue.queue_name}</strong>
                <span>в очереди {queue.queued}</span>
                <span>выполняется {queue.running}</span>
                <span>с ошибкой {queue.failed}</span>
                <span>сигнал {formatDate(queue.last_heartbeat_at)}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="emptyState compactEmptyState">
            <strong>Внешние очереди пусты</strong>
            <span>Задачи появятся после включения внешнего контура обработки.</span>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Очистка сервиса</h2>
          <button type="button" className="ghostButton" onClick={() => void reload()} disabled={loading || running}>
            Обновить
          </button>
        </div>

        <div className="maintenanceGrid">
          <label className="field">
            <span>Хранение фоновых задач, дней</span>
            <input
              value={backgroundJobDays}
              onChange={(event) => setBackgroundJobDays(event.target.value)}
              placeholder={String(status?.retention.background_job_days ?? 30)}
              inputMode="numeric"
            />
          </label>
          <label className="field">
            <span>Хранение задач экспорта, дней</span>
            <input
              value={exportJobDays}
              onChange={(event) => setExportJobDays(event.target.value)}
              placeholder={String(status?.retention.export_job_days ?? 30)}
              inputMode="numeric"
            />
          </label>
          <label className="field maintenanceReadonly">
            <span>Порог зависания, мин</span>
            <input value={String(status?.retention.stale_minutes ?? "—")} readOnly />
          </label>
          <div className="maintenanceActions">
            <button type="button" className="primaryButton" onClick={() => void handleCleanup()} disabled={running}>
              {running ? "Выполняем..." : "Запустить очистку"}
            </button>
          </div>
        </div>

        {loading ? (
          <div className="inlineLoader jobsLoader">
            <span className="loaderDot" />
            <span>Загружаем статус обслуживания…</span>
          </div>
        ) : null}

        {lastResult ? (
          <div className="maintenanceResult">
            <div className="metaLabel">Последний запуск</div>
            <div className="maintenanceResultGrid">
              <span>Истекшие сессии: {lastResult.expired_sessions}</span>
              <span>Фоновые задачи: {lastResult.background_jobs}</span>
              <span>Задачи экспорта: {lastResult.export_jobs}</span>
            </div>
          </div>
        ) : null}
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Замер в режиме чтения</h2>
          <button
            type="button"
            className="ghostButton"
            onClick={() => void handleBenchmarkRun()}
            disabled={benchmarkLoading}
          >
            {benchmarkLoading ? "Замеряем..." : "Запустить замер"}
          </button>
        </div>

        <p className="panelHint">
          Прогоняет безопасные запросы только на чтение по основным экранным данным: каталог сигналов, кандидаты в дайджест,
          состояние источников и очереди задач.
        </p>

        {benchmark ? (
          <div className="benchmarkStack">
            <div className="maintenanceResult">
              <div className="metaLabel">Снимок набора данных</div>
              <div className="maintenanceResultGrid">
                {Object.entries(benchmark.counts).map(([key, value]) => (
                  <span key={key}>
                    {key}: {value}
                  </span>
                ))}
              </div>
            </div>

            <div className="benchmarkTable">
              <div className="benchmarkRow benchmarkHead">
                <span>Проверка</span>
                <span>Статус</span>
                <span>P50</span>
                <span>P95</span>
                <span>Max</span>
                <span>Строки</span>
              </div>
              {benchmark.benchmarks.map((item) => (
                <div key={item.name} className="benchmarkRow">
                  <span>{item.name}</span>
                  <span className={item.status === "warn" ? "benchmarkWarn" : "benchmarkOk"}>{item.status}</span>
                  <span>{item.p50_ms} ms</span>
                  <span>{item.p95_ms} ms</span>
                  <span>{item.max_ms} ms</span>
                  <span>{item.rows}</span>
                </div>
              ))}
            </div>

            {benchmark.warnings.length ? (
              <div className="maintenanceResult">
                <div className="metaLabel">Предупреждения</div>
                <div className="maintenanceResultGrid">
                  {benchmark.warnings.map((item) => (
                    <span key={item}>{item}</span>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : (
          <div className="emptyState">
            <strong>Замер ещё не запускался</strong>
            <span>Запуск идёт через серверный API и не создаёт новых задач или вызовов ИИ.</span>
          </div>
        )}
      </section>
    </section>
  );
}

function formatDate(value?: string | null) {
  if (!value) return "—";
  return new Date(value).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
