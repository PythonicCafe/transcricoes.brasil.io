import { test } from "node:test";
import assert from "node:assert/strict";
import { escapeHtml, findPassages, highlight, matchingLines, normalize, queryTerms } from "../../site/js/modules/search.js";

test("normalize drops accents and case", () => {
  assert.equal(normalize("Orçamento PÚBLICO"), "orcamento publico");
});

test("escapeHtml escapes markup and accepts missing values", () => {
  assert.equal(escapeHtml(`<a href="x">'&'</a>`), "&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;");
  assert.equal(escapeHtml(undefined), "");
});

test("queryTerms keeps quoted phrases whole and ignores one letter words", () => {
  assert.deepEqual(queryTerms(`Saúde "quebra de sigilo" e  CPI`), ["quebra de sigilo", "saude", "cpi"]);
  assert.deepEqual(queryTerms("   "), []);
});

test("highlight marks terms ignoring accents and case, and escapes the rest", () => {
  assert.equal(highlight("Orçamento <b>", ["orcamento"]), "<mark>Orçamento</mark> &lt;b&gt;");
  assert.equal(highlight("a+b", []), "a+b");
  assert.equal(highlight("custo (a+b)", ["(a+b)"]), "custo <mark>(a+b)</mark>");
});

test("matchingLines finds terms in one line or in two lines in a row", () => {
  const lines = ["fala sobre orcamento", "e saude publica", "nada aqui", "orcamento e saude"];
  assert.deepEqual(matchingLines(lines, ["orcamento", "saude"]), [{ position: 0, whole: false }, { position: 3, whole: true }]);
  assert.deepEqual(matchingLines(lines, ["inexistente"]), []);
});

test("matchingLines skips the second line of a passage that spans two", () => {
  const lines = ["orcamento", "saude orcamento", "orcamento", "saude"];
  assert.deepEqual(matchingLines(lines, ["orcamento", "saude"]), [{ position: 0, whole: false }, { position: 2, whole: false }]);
});

test("terms match where a word starts, as in the index", () => {
  assert.deepEqual(matchingLines(["comunidade quilombola", "orcamentaria"], ["lombola"]), []);
  assert.deepEqual(matchingLines(["comunidade quilombola", "orcamentaria"], ["orcament"]), [{ position: 1, whole: true }]);
  assert.deepEqual(matchingLines(["a (cpi) e o principio"], ["cpi"]), [{ position: 0, whole: true }]);
  assert.equal(highlight("quilombola, Quilombo", ["quilombo"]), "<mark>quilombo</mark>la, <mark>Quilombo</mark>");
  assert.equal(highlight("comunidade quilombola", ["lombola"]), "comunidade quilombola");
});

test("findPassages also brings the lines where the terms are close but not together", () => {
  const lines = [[0, "a saúde do povo"], [20, "outra coisa"], [70, "o orçamento público"], [150, "nada"], [200, "saúde"]];
  const normalized = lines.map((line) => normalize(line[1]));
  // saúde at 0 s (block 0) and orçamento at 70 s (block 1) are close; saúde at 200 s (block 3) is not.
  assert.deepEqual(findPassages(lines, normalized, ["saude", "orcamento"], 60), { passages: [], nearby: [0, 2] });
  assert.deepEqual(findPassages(lines, normalized, ["saude"], 60), { passages: [{ position: 0, whole: true }, { position: 4, whole: true }], nearby: [] });
});

test("findPassages does not repeat the lines of a passage as nearby", () => {
  const lines = [[0, "reforma"], [5, "tributária"], [10, "reforma de novo"]];
  const normalized = lines.map((line) => normalize(line[1]));
  assert.deepEqual(findPassages(lines, normalized, ["reforma", "tributaria"], 60), { passages: [{ position: 0, whole: false }], nearby: [2] });
});
