import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TagsPage } from "./TagsPage";

const tagsFixture = [
  {
    id: 1,
    parent_name: null,
    name: "Бурение",
    name_en: "Drilling",
    description: "Бурение и заканчивание",
    keywords_json: ["бурение", "долото"],
    keywords_en_json: ["drilling"],
    negative_keywords_json: ["футбол"],
    enabled: true,
    sort_order: 10,
  },
  {
    id: 2,
    parent_name: "Бурение",
    name: "Наклонно-направленное",
    name_en: "",
    description: "",
    keywords_json: ["ННБ"],
    keywords_en_json: [],
    enabled: true,
    sort_order: 20,
  },
];

function json(payload: unknown) {
  return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } });
}

const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input);
  const method = init?.method ?? "GET";
  if (url === "/api/tags" && method === "GET") return Promise.resolve(json(tagsFixture));
  return Promise.resolve(json({ ok: true, saved: 2 }));
});

function groupOf(name: RegExp) {
  return screen.getByRole("button", { name }).parentElement as HTMLElement;
}

describe("экран «Теги»", () => {
  beforeEach(() => {
    fetchMock.mockClear();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("направления свёрнуты, ключевые слова — чипсами с крестиком", async () => {
    const user = userEvent.setup();
    render(<TagsPage onUnauthorized={() => {}} showToast={() => {}} />);

    const toggle = await screen.findByRole("button", { name: /Бурение/ });
    // По умолчанию свёрнуто: полей направления не видно, в шапке — сводка.
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Направление")).not.toBeInTheDocument();
    expect(toggle).toHaveTextContent("1 подтег · 3 ключевых слова");

    await user.click(toggle);
    expect(screen.getByText("Направление")).toBeInTheDocument();
    expect(screen.queryByText("Родительский тег")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "+ Добавить подтег" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Удалить «долото»" }));
    await user.type(screen.getByLabelText(/Ключевые слова RU — по ним ищем/), "турбобур{Enter}");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input, init]) => String(input) === "/api/tags" && init?.method === "PUT")).toBe(true);
    });
    const put = fetchMock.mock.calls.find(([input, init]) => String(input) === "/api/tags" && init?.method === "PUT")!;
    const saved = JSON.parse(String((put[1] as RequestInit).body)) as Array<Record<string, unknown>>;
    expect(saved.find((tag) => tag.name === "Бурение")?.keywords_json).toEqual(["бурение", "турбобур"]);
    expect(saved.find((tag) => tag.name === "Наклонно-направленное")?.keywords_json).toEqual(["ННБ"]);
  });

  it("удаление тега выше не раскрывает чужое новое направление, клиентский ключ не уходит на сервер", async () => {
    // Ревью F: ключ новой группы был позицией в списке и сдвигался при удалении.
    const user = userEvent.setup();
    render(<TagsPage onUnauthorized={() => {}} showToast={() => {}} />);
    await screen.findByRole("button", { name: /Бурение/ });

    await user.click(screen.getByRole("button", { name: "+ Добавить направление" }));
    const firstName = screen.getByDisplayValue("Новое направление");
    await user.clear(firstName);
    await user.type(firstName, "Икс");
    await user.click(screen.getByRole("button", { name: /^Икс/ }));
    await user.click(screen.getByRole("button", { name: "+ Добавить направление" }));
    const secondName = screen.getByDisplayValue("Новое направление");
    await user.clear(secondName);
    await user.type(secondName, "Игрек");

    await user.click(screen.getByRole("button", { name: /^Бурение/ }));
    await user.click(within(groupOf(/^Бурение/)).getByRole("button", { name: "Удалить направление" }));

    await waitFor(() => expect(screen.queryByRole("button", { name: /^Бурение/ })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: /^Икс/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("button", { name: /^Игрек/ })).toHaveAttribute("aria-expanded", "true");

    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input, init]) => String(input) === "/api/tags" && init?.method === "PUT")).toBe(true);
    });
    const put = fetchMock.mock.calls.find(([input, init]) => String(input) === "/api/tags" && init?.method === "PUT")!;
    const saved = JSON.parse(String((put[1] as RequestInit).body)) as Array<Record<string, unknown>>;
    expect(saved.every((tag) => !("client_key" in tag))).toBe(true);
  });
});
