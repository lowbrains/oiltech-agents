import { useEffect, useState } from "react";
import { getFeedWindow } from "../../api/stats";
import type { FeedWindowInfo, FeedWindowPayload } from "../../api/types";

// Окно месяца на фронте: подписи и загрузка списка месяцев. Само правило живёт на сервере
// (oiltech_digest/feed_window.py; ADR 0001 — lowbrains/oiltech-agents, docs/adr/0001-single-contour.md).

// Открытые и прошлые месяцы — при открытии экрана и при возврате на вкладку: вкладку,
// оставленную на ночь, 5-е число застало бы со старым окном, и конструктор выпуска показал бы
// кнопку сохранения у уже закрытого месяца (сервер всё равно ответит 409). Сбой не мешает
// работе с текущим периодом: остаётся прежнее значение, без него переключатель не появится.
export function useFeedWindow(): FeedWindowPayload | null {
  const [payload, setPayload] = useState<FeedWindowPayload | null>(null);
  useEffect(() => {
    let cancelled = false;
    function load() {
      getFeedWindow()
        .then((result) => {
          if (!cancelled) setPayload(result);
        })
        .catch(() => undefined);
    }
    function onVisible() {
      if (!document.hidden) load();
    }
    load();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  return payload;
}

const MONTHS = [
  "январь", "февраль", "март", "апрель", "май", "июнь",
  "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
];
const MONTHS_GENITIVE = [
  "января", "февраля", "марта", "апреля", "мая", "июня",
  "июля", "августа", "сентября", "октября", "ноября", "декабря",
];

// «2026-08» → «август 2026». Незнакомый формат возвращается как есть.
export function monthLabel(month: string): string {
  const match = /^(\d{4})-(\d{2})$/.exec(month);
  const name = match ? MONTHS[Number(match[2]) - 1] : undefined;
  return match && name ? `${name} ${match[1]}` : month;
}

// Какие месяцы открыты и до какого числа виден прошлый.
export function windowPeriodText(feedWindow: FeedWindowInfo | null | undefined): string {
  if (!feedWindow || !feedWindow.months.length) return "";
  const first = feedWindow.months[0];
  const last = feedWindow.months[feedWindow.months.length - 1];
  if (first === last) return `Показан ${monthLabel(first)}. Прошлые месяцы — в архиве, только для просмотра.`;
  const firstName = MONTHS[Number(first.slice(5, 7)) - 1] ?? first;
  const lastGenitive = MONTHS_GENITIVE[Number(last.slice(5, 7)) - 1] ?? last;
  return `Показаны ${monthLabel(first)} и ${monthLabel(last)}: ${firstName} виден до ${feedWindow.rollover_day} ${lastGenitive}, пока собирается его выпуск.`;
}

export function archiveNoticeText(month: string): string {
  return `Архив за ${monthLabel(month)} — только просмотр: статус и отметку «в дайджест» здесь не поменять.`;
}
