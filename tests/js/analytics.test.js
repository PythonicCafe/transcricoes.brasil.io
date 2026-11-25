import { test } from "node:test";
import assert from "node:assert/strict";
import { pagePath, trackPage, trackSearch } from "../../site/js/modules/analytics.js";

test("pagePath is the same in every language and leaves out the time in the video", () => {
  assert.equal(pagePath({ page: "home" }), "/");
  assert.equal(pagePath({ page: "about", language: "pt" }), "/about");
  assert.equal(pagePath({ page: "about", language: "es" }), "/about");
  assert.equal(pagePath({ page: "session", id: "abc", seconds: 600 }), "/v/abc");
});

test("nothing is sent outside the published site", () => {
  globalThis.window = { dataLayer: [] };
  trackPage({ page: "home" }, "x");
  trackSearch("orçamento");
  assert.deepEqual(window.dataLayer, []);
  delete globalThis.window;
});
