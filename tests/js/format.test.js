import { test } from "node:test";
import assert from "node:assert/strict";
import { clock, readableDate, readableDuration } from "../../site/js/modules/format.js";

test("clock pads hours, minutes and seconds", () => {
  assert.equal(clock(0), "00:00:00");
  assert.equal(clock(3725.9), "01:02:05");
});

test("readableDuration leaves out empty leading units", () => {
  assert.equal(readableDuration(45), "45s");
  assert.equal(readableDuration(125), "2min 5s");
  assert.equal(readableDuration(3600), "1h 0min 0s");
});

test("readableDate formats in the given locale and keeps invalid input", () => {
  assert.equal(readableDate("2025-03-04", "en-US"), "Mar 4, 2025");
  assert.equal(readableDate("", "en-US"), "");
  assert.equal(readableDate("ontem", "en-US"), "ontem");
});
