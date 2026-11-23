import { test } from "node:test";
import assert from "node:assert/strict";
import { TEXTS } from "../../site/js/modules/texts.js";

const placeholders = (text) => [...String(text).matchAll(/\{([a-z_]+)\}/g)].map((match) => match[1]).sort();

test("every language has the same keys", () => {
  const [first, ...others] = Object.keys(TEXTS);
  for (const language of others) assert.deepEqual(Object.keys(TEXTS[language]).sort(), Object.keys(TEXTS[first]).sort(), language);
});

test("translations use the same placeholders", () => {
  for (const key of Object.keys(TEXTS.en)) {
    const expected = placeholders([TEXTS.en[key]].flat().join(" "));
    for (const language of Object.keys(TEXTS)) {
      assert.deepEqual(placeholders([TEXTS[language][key]].flat().join(" ")), expected, `${language}.${key}`);
    }
  }
});

test("the about page has the same number of paragraphs in every language", () => {
  for (const language of Object.keys(TEXTS)) assert.equal(TEXTS[language].about.length, TEXTS.en.about.length, language);
});
