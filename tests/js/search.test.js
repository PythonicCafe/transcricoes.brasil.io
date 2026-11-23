import { test } from "node:test";
import assert from "node:assert/strict";
import { escapeHtml, highlight, matchingLines, normalize, queryTerms, search } from "../../site/js/modules/search.js";

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

test("search returns passages, sessions found only in the summary and sorts by passages", () => {
  const sessions = [
    { id: "a", title: "Sessão A", summary: "Debate geral" },
    { id: "b", title: "Sessão B", summary: "Orçamento da saúde" },
    { id: "c", title: "Sessão C", summary: "Outro assunto" },
  ];
  const data = {
    lines: { a: [[10, "Falamos de orçamento"]], b: [[5, "orçamento"], [8, "mais orçamento"]], c: [[1, "nada"]] },
    keywords: { c: ["orçamento"] },
  };
  const cache = {};
  const results = search(sessions, data, ["orcamento"], cache);
  assert.deepEqual(results.map((result) => [result.session.id, result.passages.length, result.inSummary]), [["b", 2, true], ["a", 1, false], ["c", 0, true]]);
  assert.deepEqual(results[1].passages, [{ seconds: 10, text: "Falamos de orçamento" }]);
  assert.deepEqual(cache.a, ["falamos de orcamento"]);
});

test("search joins the two lines of a passage that spans them", () => {
  const data = { lines: { a: [[1, "reforma"], [4, "tributária"]] }, keywords: {} };
  const [result] = search([{ id: "a", title: "", summary: "" }], data, ["reforma", "tributaria"]);
  assert.deepEqual(result.passages, [{ seconds: 1, text: "reforma tributária" }]);
});
