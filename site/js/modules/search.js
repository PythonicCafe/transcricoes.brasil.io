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
