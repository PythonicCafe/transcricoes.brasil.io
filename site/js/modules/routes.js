// Routes in the URL fragment. The about page has one address per language (`#/about`, `#/sobre`, `#/acerca-de`);
// any of them opens the page, and the site then shows the one of the current language.

export const ABOUT_SLUGS = { en: "about", pt: "sobre", es: "acerca-de" };

// Split before decoding, so an escaped "/" stays inside its part.
function fragmentParts(hash) {
  return hash.replace(/^#\/?/, "").split("/").map((part) => {
    try { return decodeURIComponent(part); } catch (error) { return part; }
  });
}

// `{page: "home"}`, `{page: "about", language}` or `{page: "session", id, seconds}` (`seconds` is null without a time).
export function parseRoute(hash) {
  const parts = fragmentParts(hash);
  if (parts[0] === "v" && parts[1]) {
    const seconds = parts[2] ? Number(parts[2]) : NaN;
    return { page: "session", id: parts[1], seconds: Number.isFinite(seconds) && seconds >= 0 ? seconds : null };
  }
  const language = Object.keys(ABOUT_SLUGS).find((code) => ABOUT_SLUGS[code] === parts[0]);
  return language ? { page: "about", language } : { page: "home" };
}

export function routeHash(route, language) {
  if (route.page === "session") {
    return `#/v/${encodeURIComponent(route.id)}` + (route.seconds === null || route.seconds === undefined ? "" : `/${Math.floor(route.seconds)}`);
  }
  if (route.page === "about") return `#/${ABOUT_SLUGS[language] ?? ABOUT_SLUGS.en}`;
  return "#/";
}
