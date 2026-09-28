import { describe, expect, it } from "vitest";
import { mergeKeywords } from "./KeywordChips";

describe("keyword chips input", () => {
  it("splits a pasted column or a comma list into separate words", () => {
    // 13.09 заказчик просил вводить ключи строчками; вставку списка через запятую из
    // таблицы ломать нельзя — чипсы принимают оба вида.
    expect(mergeKeywords([], "ГРП\nгидроразрыв\n\nпроппант")).toEqual(["ГРП", "гидроразрыв", "проппант"]);
    expect(mergeKeywords(["ГРП"], " seismic imaging , proppant,")).toEqual(["ГРП", "seismic imaging", "proppant"]);
  });

  it("does not add a repeat regardless of case", () => {
    expect(mergeKeywords(["ГРП", "Proppant"], "грп\nproppant\nКРС")).toEqual(["ГРП", "Proppant", "КРС"]);
  });
});
