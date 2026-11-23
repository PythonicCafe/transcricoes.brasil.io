// The session list of `data/items.json` and its pages.

// Each row of `items.json` follows the order of `fields`; house, state, type and themes are positions in the lists
// of options, so each name is written once.
export function decodeList(data) {
  const options = { house: data.houses, state: data.states, type: data.types };
  const sessions = data.items.map((row) => {
    const session = {};
    data.fields.forEach((field, position) => {
      const value = row[position];
      if (options[field]) session[field] = options[field][value];
      else if (field === "themes") session.themes = value.map((theme) => data.themes[theme]);
      else session[field] = value;
    });
    return session;
  });
  return { config: data.config, sessions };
}

// The items of page `page` (moved into 1..pages when it is out of the range) and the number of pages.
export function pageSlice(items, page, perPage) {
  const pages = Math.max(1, Math.ceil(items.length / perPage));
  const current = Math.min(Math.max(1, page), pages);
  const start = (current - 1) * perPage;
  return { page: current, pages, visible: items.slice(start, start + perPage) };
}

// Page buttons to show: the first, the last and the neighbours of the current one, in order.
export function visiblePages(current, pages) {
  return [...new Set([1, current - 1, current, current + 1, pages])].filter((number) => number >= 1 && number <= pages).sort((first, second) => first - second);
}
