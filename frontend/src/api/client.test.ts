import { describe, expect, it } from "vitest";
import { readableErrorMessage } from "./client";

describe("текст ошибки API", () => {
  it("отказ сервера показывается текстом detail, а не JSON-строкой", () => {
    expect(readableErrorMessage('{"detail":"Статья относится к архиву за август 2026."}')).toBe(
      "Статья относится к архиву за август 2026.",
    );
  });

  it("список ошибок валидации и не-JSON остаются как есть", () => {
    expect(readableErrorMessage('{"detail":[{"loc":["body"],"msg":"field required"}]}')).toContain('"detail"');
    expect(readableErrorMessage("Internal Server Error")).toBe("Internal Server Error");
  });
});
