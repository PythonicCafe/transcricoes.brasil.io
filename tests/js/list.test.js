import { test } from "node:test";
import assert from "node:assert/strict";
import { decodeList, pageSlice, visiblePages } from "../../site/js/modules/list.js";

test("decodeList turns positions back into names", () => {
  const data = {
    config: { repository: "r" },
    houses: ["Câmara", "Senado"], states: ["", "SP"], types: ["Plenário"], themes: ["Saúde", "Educação"],
    fields: ["id", "title", "house", "state", "date", "duration", "type", "themes", "summary"],
    items: [["a", "A", 1, 0, "2025-01-02", 60, 0, [1, 0], "ra"]],
  };
  assert.deepEqual(decodeList(data), {
    config: { repository: "r" },
    sessions: [{ id: "a", title: "A", house: "Senado", state: "", date: "2025-01-02", duration: 60, type: "Plenário", themes: ["Educação", "Saúde"], summary: "ra" }],
  });
});

test("pageSlice cuts a page and keeps the page number in range", () => {
  const items = Array.from({ length: 45 }, (_, index) => index);
  assert.deepEqual(pageSlice(items, 2, 20), { page: 2, pages: 3, visible: items.slice(20, 40) });
  assert.deepEqual(pageSlice(items, 9, 20), { page: 3, pages: 3, visible: items.slice(40) });
  assert.deepEqual(pageSlice(items, 0, 20).page, 1);
  assert.deepEqual(pageSlice([], 3, 20), { page: 1, pages: 1, visible: [] });
});

test("visiblePages shows the ends and the neighbours of the current page", () => {
  assert.deepEqual(visiblePages(1, 1), [1]);
  assert.deepEqual(visiblePages(1, 10), [1, 2, 10]);
  assert.deepEqual(visiblePages(5, 10), [1, 4, 5, 6, 10]);
  assert.deepEqual(visiblePages(10, 10), [1, 9, 10]);
});
