// Text matching: normalization, query parsing, term highlighting and the lines of a transcript that match.

export const escapeHtml = (text) => String(text ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));

export const normalize = (text) => text.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

// Phrases in quotes become a single term; one letter words are ignored.
export function queryTerms(query) {
  const terms = [];
  const rest = query.replace(/"([^"]+)"/g, (_, phrase) => { terms.push(normalize(phrase.trim())); return " "; });
  for (const word of rest.split(/\s+/)) if (word.trim().length > 1) terms.push(normalize(word.trim()));
  return terms.filter(Boolean);
}

const VARIANTS = { a: "aáàâãä", e: "eéèêë", i: "iíìîï", o: "oóòôõö", u: "uúùûü", c: "cç", n: "nñ" };
const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// The rule of every search of the site, as in the index: a term matches where a word starts with it ("orcament"
// finds "orçamento", "lombola" does not find "quilombola"); a phrase in quotes must appear whole.
const startsWord = (term) => new RegExp(`(?<![a-z0-9])${escapeRegExp(term)}`);

// Safe HTML with the terms inside <mark>, ignoring accents and case, where they start a word.
export function highlight(text, terms) {
  if (!terms.length) return escapeHtml(text);
  const patterns = terms.map((term) => [...term].map((letter) => VARIANTS[letter] ? `[${VARIANTS[letter]}]` : escapeRegExp(letter)).join(""));
  const expression = new RegExp(`((?<![\\p{L}\\p{N}])(?:${patterns.join("|")}))`, "giu");
  return text.split(expression).map((piece, position) => position % 2 ? `<mark>${escapeHtml(piece)}</mark>` : escapeHtml(piece)).join("");
}

// Positions of the normalized lines where all the terms are in the same line, or in two lines in a row (then
// `whole` is false and the passage goes on to the next line, which is skipped).
export function matchingLines(lines, terms) {
  const tests = terms.map(startsWord);
  const found = [];
  for (let position = 0; position < lines.length; position++) {
    const window = lines[position] + " " + (lines[position + 1] || "");
    if (tests.every((test) => test.test(window)) && tests.some((test) => test.test(lines[position]))) {
      const whole = tests.every((test) => test.test(lines[position]));
      found.push({ position, whole });
      if (!whole) position++;
    }
  }
  return found;
}

// Everything a search finds in one transcript (`lines` as [[seconds, text]], `normalized` their normalized text):
// `passages`, from `matchingLines`, and `nearby`, the positions of the other lines with some term where every term
// is in the same block of `blockSeconds` or a neighbouring one. The index finds sessions by this second rule, so
// the result cards and the session page show what made a session come up.
export function findPassages(lines, normalized, terms, blockSeconds) {
  const passages = matchingLines(normalized, terms);
  const covered = new Set(passages.flatMap(({ position, whole }) => whole ? [position] : [position, position + 1]));
  const tests = terms.map(startsWord);
  const termBlocks = tests.map(() => new Set());
  const withTerm = [];
  normalized.forEach((line, position) => {
    const block = Math.floor(lines[position][0] / blockSeconds);
    let any = false;
    tests.forEach((test, index) => { if (test.test(line)) { termBlocks[index].add(block); any = true; } });
    if (any) withTerm.push([position, block]);
  });
  const near = (block) => termBlocks.every((blocks) => blocks.has(block - 1) || blocks.has(block) || blocks.has(block + 1));
  const nearby = withTerm.filter(([position, block]) => !covered.has(position) && near(block)).map(([position]) => position);
  return { passages, nearby };
}
