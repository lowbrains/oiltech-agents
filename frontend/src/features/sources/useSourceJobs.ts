import { useEffect, useState } from "react";
import { getJob } from "../../api/jobs";
import type { SourceDiagnostics } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

export type PendingJob = { kind: "diagnose" | "scrape"; jobId: number; label: string };

type Options = {
  showToast: ToastWriter;
  onError: (error: unknown, fallback: string) => void;
  onDiagnosed: (sourceId: number, diagnostic: SourceDiagnostics) => void;
  onScraped: () => void;
};

/** Фоновые задачи источника (диагностика, сбор): опрос раз в 2,5 с до завершения. */
export function useSourceJobs({ showToast, onError, onDiagnosed, onScraped }: Options) {
  const [pendingJobs, setPendingJobs] = useState<Record<number, PendingJob>>({});

  function setPendingJob(sourceId: number, kind: PendingJob["kind"], jobId: number, label: string) {
    setPendingJobs((prev) => ({ ...prev, [sourceId]: { kind, jobId, label } }));
  }

  function clearPendingJob(sourceId: number) {
    setPendingJobs((prev) => {
      const next = { ...prev };
      delete next[sourceId];
      return next;
    });
  }

  useEffect(() => {
    const entries = Object.entries(pendingJobs);
    if (!entries.length) return;

    let cancelled = false;

    async function poll() {
      const results = await Promise.all(
        entries.map(async ([sourceId, pending]) => {
          try {
            const job = await getJob(pending.jobId);
            return { sourceId: Number(sourceId), pending, job };
          } catch (error) {
            return { sourceId: Number(sourceId), pending, error };
          }
        }),
      );

      if (cancelled) return;

      let needsReload = false;

      results.forEach((result) => {
        if ("error" in result) {
          clearPendingJob(result.sourceId);
          onError(result.error, "Не удалось получить результат");
          return;
        }

        if (result.job.status === "queued" || result.job.status === "running") {
          // Держим нейтральную метку («Собираем статьи…» / «Проверяем источник…»),
          // без процентов прогресса и номеров задач.
          return;
        }

        clearPendingJob(result.sourceId);

        if (result.job.status === "failed") {
          showToast(result.pending.kind === "scrape" ? "Не удалось собрать статьи" : "Не удалось проверить источник", "error");
          return;
        }

        if (result.pending.kind === "diagnose") {
          onDiagnosed(result.sourceId, result.job.result as SourceDiagnostics);
          showToast("Источник проверен");
          return;
        }

        if (result.pending.kind === "scrape") {
          const stats = result.job.result?.stats as { added?: number; attempted?: number } | undefined;
          const added = stats?.added || 0;
          showToast(added ? `Добавлено статей: ${added}` : "Новых статей не найдено");
          needsReload = true;
        }
      });

      if (needsReload) {
        onScraped();
      }
    }

    void poll();
    const timer = window.setInterval(() => {
      void poll();
    }, 2500);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [pendingJobs]);

  return { pendingJobs, setPendingJob };
}
