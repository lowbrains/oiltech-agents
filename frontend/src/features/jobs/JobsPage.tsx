import { useEffect, useMemo, useState } from "react";
import { downloadJobResult, listJobs } from "../../api/jobs";
import type { BackgroundJob } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

const statuses: Array<BackgroundJob["status"] | ""> = ["", "queued", "running", "ok", "failed"];
const queues = ["", "default", "ai", "playwright", "ru-fetch", "ru-playwright", "external-ai", "external-fetch", "external-playwright", "external-agents"];

const statusLabels: Record<BackgroundJob["status"], string> = {
  queued: "В очереди",
  running: "В работе",
  ok: "Готово",
  failed: "Ошибка",
};

export function JobsPage({ onUnauthorized, showToast }: Props) {
  const [jobs, setJobs] = useState<BackgroundJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyJobId, setBusyJobId] = useState<number | null>(null);
  const [status, setStatus] = useState<BackgroundJob["status"] | "">("");
  const [queue, setQueue] = useState("");
  const [kind, setKind] = useState("");

  useEffect(() => {
    void reload();
  }, []);

  const counters = useMemo(() => {
    return {
      queued: jobs.filter((job) => job.status === "queued").length,
      running: jobs.filter((job) => job.status === "running").length,
      failed: jobs.filter((job) => job.status === "failed").length,
    };
  }, [jobs]);

  async function reload() {
    try {
      setLoading(true);
      setJobs(await listJobs({ status, queue, kind: kind.trim(), limit: 100 }));
    } catch (error) {
      handleError(error, "Не удалось загрузить задачи");
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

  async function handleDownload(job: BackgroundJob) {
    try {
      setBusyJobId(job.id);
      const file = await downloadJobResult(job.id);
      const url = window.URL.createObjectURL(file.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = file.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
      showToast(`Файл скачан: ${file.filename}`);
    } catch (error) {
      handleError(error, "Не удалось скачать результат задачи");
    } finally {
      setBusyJobId(null);
    }
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Фоновые задачи</h1>
        </div>
        <div className="panelActions">
          <a className="ghostButton compactButton" href="?screen=maintenance">
            Обслуживание
          </a>
          <div className="statusPill">Скрытая страница</div>
        </div>
      </header>

      <section className="statsGridReact jobsStats">
        <div className="statCardReact">
          <strong>{jobs.length}</strong>
          <span>Показано</span>
        </div>
        <div className="statCardReact">
          <strong>{counters.queued}</strong>
          <span>В очереди</span>
        </div>
        <div className="statCardReact">
          <strong>{counters.running}</strong>
          <span>В работе</span>
        </div>
        <div className="statCardReact">
          <strong>{counters.failed}</strong>
          <span>С ошибкой</span>
        </div>
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Очередь</h2>
          <button type="button" className="ghostButton" onClick={() => void reload()} disabled={loading}>
            Обновить
          </button>
        </div>

        <div className="jobsFilters">
          <label className="field">
            <span>Статус</span>
            <select value={status} onChange={(event) => setStatus(event.target.value as BackgroundJob["status"] | "")}>
              {statuses.map((item) => (
                <option key={item || "all"} value={item}>
                  {item ? statusLabels[item] : "Все статусы"}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Очередь</span>
            <select value={queue} onChange={(event) => setQueue(event.target.value)}>
              {queues.map((item) => (
                <option key={item || "all"} value={item}>
                  {item || "Все очереди"}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Тип</span>
            <input value={kind} onChange={(event) => setKind(event.target.value)} placeholder="digest_export" />
          </label>
          <button type="button" className="primaryButton" onClick={() => void reload()}>
            Применить
          </button>
        </div>

        {loading ? <InlineJobsLoader /> : null}

        <div className="jobsTableWrap">
          <table className="jobsTable">
            <thead>
              <tr>
                <th>№</th>
                <th>Тип</th>
                <th>Очередь</th>
                <th>Регион</th>
                <th>Статус</th>
                <th>Попытки</th>
                <th>Прогресс</th>
                <th>Создана</th>
                <th>Результат</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.id}>
                  <td>#{job.id}</td>
                  <td>{job.kind}</td>
                  <td>{job.queue}</td>
                  <td>{job.execution_region}{job.capability ? ` / ${job.capability}` : ""}</td>
                  <td>
                    <span className={`jobStatus ${job.status}`}>{statusLabels[job.status]}</span>
                  </td>
                  <td>
                    {job.attempts}/{job.max_attempts}
                  </td>
                  <td>{Math.round(job.progress)}%</td>
                  <td>{formatDate(job.created_at)}</td>
                  <td>
                    {job.status === "ok" && job.result?.path ? (
                      <button
                        type="button"
                        className="ghostButton compactButton"
                        disabled={busyJobId === job.id}
                        onClick={() => void handleDownload(job)}
                      >
                        Скачать
                      </button>
                    ) : job.error ? (
                      <span className="jobError" title={job.error}>
                        {job.error}
                      </span>
                    ) : (
                      <span className="metaText">{job.run_after ? `после ${formatDate(job.run_after)}` : "—"}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!loading && jobs.length === 0 ? <div className="emptyState">Задач не найдено.</div> : null}
        </div>
      </section>
    </section>
  );
}

function formatDate(value: string | null) {
  if (!value) return "—";
  return new Date(value).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
}

function InlineJobsLoader() {
  return (
    <div className="inlineLoader jobsLoader">
      <span className="loaderDot" />
      <span>Загружаем задачи…</span>
    </div>
  );
}
