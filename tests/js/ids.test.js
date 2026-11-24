import { test } from "node:test";
import assert from "node:assert/strict";
import { youtubeId } from "../../site/js/modules/ids.js";

test("youtubeId reads the video id of a YouTube URL", () => {
  assert.equal(youtubeId("https://youtube.com/watch?v=dQw4w9WgXcQ"), "dQw4w9WgXcQ");
  assert.equal(youtubeId("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3"), "dQw4w9WgXcQ");
});

test("youtubeId is null for other sources and broken URLs", () => {
  assert.equal(youtubeId("https://example.com/watch?v=x"), null);
  assert.equal(youtubeId("not a url"), null);
});
