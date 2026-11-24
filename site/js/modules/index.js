// Search with the partitioned index (`data/index/<version>/`): which files a query needs, how to read them, which
// documents match and in which order. `transcripts.py` (section "search index") writes the files; BUSCA.md explains.

import { normalize } from "./search.js";

const BM25_K1 = 1.2;
const BM25_B = 0.75;
const META_WEIGHT = 3; // an occurrence in the title, summary or keywords counts as this many in the transcript

// Words of the index in a text: letters and digits, two or more characters (as `index_tokens` in Python).
export const indexTokens = (text) => (normalize(text).match(/[a-z0-9]+/g) || []).filter((token) => token.length > 1);

// What to look up in the index for the terms of a query (`queryTerms`): each word with `prefix` true when terms that
// start with it count too. Words match by their beginning, except inside a quoted phrase, where every word but the
// last is a whole word (that is what the phrase asks for, and short words like "de" would bring thousands of terms).
export function queryWords(terms) {
  const words = new Map();
  for (const term of terms) {
    const tokens = indexTokens(term);
    tokens.forEach((text, position) => {
      const prefix = position === tokens.length - 1;
      words.set(text, words.get(text) || prefix);
    });
  }
  return [...words].map(([text, prefix]) => ({ text, prefix }));
}

// Positions of the files that may hold `word`: the one where it would be and, for a prefix, the next ones while they
// start with it. `starts` is the first term of each file, in order.
export function shardsFor(starts, word, prefix = true) {
  let low = 0;
  let high = starts.length - 1;
  while (low < high) {
    const middle = (low + high + 1) >> 1;
    if (starts[middle] <= word) low = middle; else high = middle - 1;
  }
  const files = [low];
  for (let next = low + 1; prefix && next < starts.length && starts[next].startsWith(word); next++) files.push(next);
  return files;
}

// Map document -> {tf, meta, blocks} from the flat list of one term.
export function decodePostings(flat) {
  const documents = new Map();
  let document = 0;
  for (let position = 0; position < flat.length;) {
    document += flat[position];
    const count = flat[position + 3];
    const blocks = [];
    let block = 0;
    for (let index = 0; index < count; index++) {
      block += flat[position + 4 + index];
      blocks.push(block);
    }
    documents.set(document, { tf: flat[position + 1], meta: flat[position + 2], blocks });
    position += 4 + count;
  }
  return documents;
}

// For one word ({text, prefix}), per document: BM25 score summed over the terms it matches, the blocks where any of
// them is and whether any of them is in the title, summary or keywords.
function wordMatches(shards, word, index) {
  const total = index.lengths.length;
  const average = index.average_length || 1;
  const matches = new Map();
  for (const shard of shards) {
    for (const term in shard) {
      if (word.prefix ? !term.startsWith(word.text) : term !== word.text) continue;
      const postings = decodePostings(shard[term]);
      const idf = Math.log(1 + (total - postings.size + 0.5) / (postings.size + 0.5));
      for (const [document, entry] of postings) {
        const frequency = entry.tf + META_WEIGHT * entry.meta;
        const length = index.lengths[document] || 0;
        const score = idf * frequency * (BM25_K1 + 1) / (frequency + BM25_K1 * (1 - BM25_B + BM25_B * length / average));
        let match = matches.get(document);
        if (!match) matches.set(document, (match = { score: 0, blocks: new Set(), meta: false }));
        match.score += score;
        entry.blocks.forEach((block) => match.blocks.add(block));
        match.meta ||= entry.meta > 0;
      }
    }
  }
  return matches;
}

// Documents with every word (from `queryWords`), either near each other in the transcript (the same block or neighbouring ones) or all
// in the title, summary and keywords; ranked by BM25. Each result has the document position, its score, the blocks
// where the words meet (of the rarest word) and `inSummary`.
export function rankDocuments(shards, words, index) {
  if (!words.length) return [];
  const lists = words.map((word) => wordMatches(shards, word, index)).sort((first, second) => first.size - second.size);
  const results = [];
  for (const [document, rarest] of lists[0]) {
    const others = lists.slice(1).map((list) => list.get(document));
    if (others.some((match) => !match)) continue;
    const blocks = [...rarest.blocks].filter((block) =>
      others.every((match) => match.blocks.has(block) || match.blocks.has(block - 1) || match.blocks.has(block + 1)));
    const inSummary = rarest.meta && others.every((match) => match.meta);
    if (!blocks.length && !inSummary) continue;
    const score = rarest.score + others.reduce((sum, match) => sum + match.score, 0);
    results.push({ document, score, blocks: blocks.sort((first, second) => first - second), inSummary });
  }
  results.sort((first, second) => second.score - first.score || first.document - second.document);
  return results;
}
