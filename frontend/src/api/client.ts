export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// FastAPI отвечает на отказ телом {"detail": "текст"}. Показываем человеку текст, а не
// JSON-строку: отказы окна месяца («архив открыт только для просмотра») и прочие 4xx
// написаны для людей. Не строковый detail (список ошибок 422) и не-JSON — как есть.
export function readableErrorMessage(text: string): string {
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && typeof (parsed as { detail?: unknown }).detail === "string") {
      return (parsed as { detail: string }).detail;
    }
  } catch {
    // не JSON — отдаём как есть
  }
  return text;
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers ?? {});
  // FormData — ИСКЛЮЧЕНИЕ: Content-Type у многочастного тела содержит boundary, который
  // знает только браузер. Подставив здесь application/json (или даже голый
  // multipart/form-data без boundary), мы получим на сервере неразбираемое тело.
  const isMultipart = typeof FormData !== "undefined" && init.body instanceof FormData;
  if (!headers.has("Content-Type") && init.body && !isMultipart) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers,
  });

  if (!response.ok) {
    let message = response.statusText || "Request failed";
    try {
      const text = await response.text();
      if (text) {
        message = readableErrorMessage(text);
      }
    } catch {
      // ignore text parsing failure
    }
    throw new ApiError(response.status, message);
  }

  return (await response.json()) as T;
}

export async function apiFetchText(path: string, init: RequestInit = {}) {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: init.headers,
  });

  if (!response.ok) {
    let message = response.statusText || "Request failed";
    try {
      const text = await response.text();
      if (text) {
        message = text;
      }
    } catch {
      // ignore text parsing failure
    }
    throw new ApiError(response.status, message);
  }

  return response.text();
}

export async function apiDownload(path: string, init: RequestInit = {}) {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: init.headers,
  });

  if (!response.ok) {
    let message = response.statusText || "Request failed";
    try {
      const text = await response.text();
      if (text) {
        message = text;
      }
    } catch {
      // ignore text parsing failure
    }
    throw new ApiError(response.status, message);
  }

  const disposition = response.headers.get("Content-Disposition") || "";
  const filenameMatch = disposition.match(/filename\*=UTF-8''([^;]+)|filename="?([^"]+)"?/i);
  const encodedName = filenameMatch?.[1];
  const plainName = filenameMatch?.[2];
  const filename = encodedName ? decodeURIComponent(encodedName) : plainName || "download";

  return {
    blob: await response.blob(),
    filename,
    contentType: response.headers.get("Content-Type") || "application/octet-stream",
  };
}
