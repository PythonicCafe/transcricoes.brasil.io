// Search in the browser: normalization, query parsing, term highlighting and the scan of the transcripts.

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

// Safe HTML with the terms inside <mark>, ignoring accents and case.
export function highlight(text, terms) {
  if (!terms.length) return escapeHtml(text);
  const patterns = terms.map((term) => [...term].map((letter) =>
    VARIANTS[letter] ? `[${VARIANTS[letter]}]` : letter.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(""));
  const expression = new RegExp(`(${patterns.join("|")})`, "gi");
  return text.split(expression).map((piece, position) => position % 2 ? `<mark>${escapeHtml(piece)}</mark>` : escapeHtml(piece)).join("");
}

// Positions of the lines that match the terms: all of them in the same line, or in two lines in a row (then `whole`
// is false and the passage goes on to the next line, which is skipped).
export function matchingLines(lines, terms) {
  const found = [];
  for (let position = 0; position < lines.length; position++) {
    const window = lines[position] + " " + (lines[position + 1] || "");
    if (terms.every((term) => window.includes(term)) && terms.some((term) => lines[position].includes(term))) {
      const whole = terms.every((term) => lines[position].includes(term));
      found.push({ position, whole });
      if (!whole) position++;
    }
  }
  return found;
}

// Look in every given session; `data` is `search.json` ({lines, keywords}, by id). A passage matches when all the
// terms are in the same line or in two lines in a row. Sessions without passages are kept if the terms are in the
// title, the summary or the keywords.
export function search(sessions, data, terms, cache = {}) {
  const results = [];
  for (const session of sessions) {
    const original = data.lines[session.id] || [];
    const lines = cache[session.id] ??= original.map((line) => normalize(line[1]));
    const passages = matchingLines(lines, terms).map(({ position, whole }) => ({
      seconds: original[position][0],
      text: whole ? original[position][1] : original[position][1] + " " + original[position + 1][1],
    }));
    const meta = normalize(`${session.title} ${session.summary} ${(data.keywords[session.id] || []).join(" ")}`);
    const inSummary = terms.every((term) => meta.includes(term));
    if (passages.length || inSummary) results.push({ session, passages, inSummary });
  }
  results.sort((first, second) => second.passages.length - first.passages.length);
  return results;
}
