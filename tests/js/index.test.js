import { test } from "node:test";
import assert from "node:assert/strict";
import { decodePostings, indexTokens, queryWords, rankDocuments, shardsFor } from "../../site/js/modules/index.js";

// The flat list of one term, as transcripts.py writes it: per document
// [document - previous document, tf, meta, number of blocks, first block, block - previous block, ...].
function encode(documents) {
  const flat = [];
  let previous = 0;
  for (const [document, { tf, meta, blocks }] of [...documents].sort((first, second) => first[0] - second[0])) {
    flat.push(document - previous, tf, meta, blocks.length, ...blocks.map((block, index) => block - (index ? blocks[index - 1] : 0)));
    previous = document;
  }
  return flat;
}

const index = { lengths: [100, 100, 100, 100], average_length: 100 };

test("indexTokens drops accents, case, punctuation and one letter words", () => {
  assert.deepEqual(indexTokens("Orçamento: a CPI de 2025, é já!"), ["orcamento", "cpi", "de", "2025", "ja"]);
});

test("shardsFor finds the file of a word and the next ones that start with it", () => {
  const starts = ["a", "caminho", "exa", "expandiu", "expor", "fala"];
  assert.deepEqual(shardsFor(starts, "evento"), [1]);
  assert.deepEqual(shardsFor(starts, "ex"), [1, 2, 3, 4]);
  assert.deepEqual(shardsFor(starts, "expo"), [3, 4]);
  assert.deepEqual(shardsFor(starts, "zebra"), [5]);
  assert.deepEqual(shardsFor(starts, "0"), [0]);
  assert.deepEqual(shardsFor(starts, "ex", false), [1]);
});

test("queryWords matches by the beginning, except the inner words of a phrase", () => {
  assert.deepEqual(queryWords(["orcamento", "projeto de lei", "de"]), [
    { text: "orcamento", prefix: true },
    { text: "projeto", prefix: false },
    { text: "de", prefix: true },
    { text: "lei", prefix: true },
  ]);
  assert.deepEqual(queryWords(["a"]), []);
});

test("rankDocuments takes a whole word as only that term", () => {
  const shard = {
    de: encode(new Map([[0, { tf: 1, meta: 0, blocks: [0] }]])),
    deputado: encode(new Map([[1, { tf: 1, meta: 0, blocks: [0] }]])),
  };
  assert.deepEqual(rankDocuments([shard], [{ text: "de", prefix: false }], index).map((result) => result.document), [0]);
  assert.deepEqual(rankDocuments([shard], [{ text: "de", prefix: true }], index).map((result) => result.document).sort(), [0, 1]);
});

test("decodePostings reads documents and blocks back from the deltas", () => {
  const documents = new Map([[2, { tf: 3, meta: 0, blocks: [0, 5, 9] }], [7, { tf: 1, meta: 2, blocks: [] }]]);
  assert.deepEqual(decodePostings(encode(documents)), documents);
});

test("rankDocuments needs every word near the others or all of them in the summary", () => {
  const shard = {
    orcamento: encode(new Map([[0, { tf: 1, meta: 0, blocks: [3] }], [1, { tf: 1, meta: 0, blocks: [3] }], [2, { tf: 0, meta: 1, blocks: [] }]])),
    saude: encode(new Map([[0, { tf: 1, meta: 0, blocks: [4] }], [1, { tf: 1, meta: 0, blocks: [9] }], [2, { tf: 0, meta: 1, blocks: [] }]])),
  };
  const results = rankDocuments([shard], queryWords(["orcamento", "saude"]), index);
  assert.deepEqual(results.map(({ document, blocks, inSummary }) => [document, blocks, inSummary]).sort(), [[0, [3], false], [2, [], true]]);
});

test("rankDocuments matches the beginning of words and ranks by BM25", () => {
  const shard = {
    orcamentaria: encode(new Map([[1, { tf: 1, meta: 0, blocks: [0] }]])),
    orcamento: encode(new Map([[0, { tf: 1, meta: 0, blocks: [0] }], [1, { tf: 6, meta: 0, blocks: [1, 2] }]])),
    outro: encode(new Map([[3, { tf: 9, meta: 0, blocks: [0] }]])),
  };
  const results = rankDocuments([shard], queryWords(["orcament"]), index);
  assert.deepEqual(results.map((result) => result.document), [1, 0]);
  assert.deepEqual(results[0].blocks, [0, 1, 2]);
  assert.ok(results[0].score > results[1].score);
});

test("rankDocuments gives a word in the title or summary more weight than one in the transcript", () => {
  const shard = { cpi: encode(new Map([[0, { tf: 1, meta: 0, blocks: [0] }], [1, { tf: 1, meta: 1, blocks: [0] }]])) };
  assert.deepEqual(rankDocuments([shard], queryWords(["cpi"]), index).map((result) => result.document), [1, 0]);
});

test("rankDocuments without words or matches is empty", () => {
  assert.deepEqual(rankDocuments([{}], [], index), []);
  assert.deepEqual(rankDocuments([{}], queryWords(["nada"]), index), []);
});
