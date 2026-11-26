// Google Analytics (GA4), only on the published site: `make serve` and the tests send nothing.
//
// The routes live in the URL fragment, which GA does not follow, so the site sends a page view per route, with a
// path made from the route. The exception is the first page of a visit: the Google tag (a Tag Manager container,
// GTM-56NKB5HF) sends its own page view when it loads, whatever the configuration on the page says, always as "/"
// (seen in production in November 2026). So the site counts "/" as already sent: a visit that starts at the home
// page gets one page view, and one that starts at a session gets "/" and the session.

const MEASUREMENT_ID = "G-6PYSXGSSEE";
const PUBLISHED_HOST = "transcricoes.brasil.io";

const enabled = () => typeof location !== "undefined" && location.hostname === PUBLISHED_HOST;
let lastPage = null;

// gtag.js reads `arguments` objects from dataLayer, not arrays.
function gtag() {
  window.dataLayer.push(arguments);
}

export function startAnalytics() {
  if (!enabled()) return;
  window.dataLayer = window.dataLayer || [];
  gtag("js", new Date());
  gtag("config", MEASUREMENT_ID);
  lastPage = "/";
  const script = document.createElement("script");
  script.async = true;
  script.src = `https://www.googletagmanager.com/gtag/js?id=${MEASUREMENT_ID}`;
  document.head.appendChild(script);
}

// Path reported for a route: the same for every language and without the time in the video, so the reports add up
// the visits of each page.
export function pagePath(route) {
  if (route.page === "session") return `/v/${route.id}`;
  if (route.page === "about") return "/about";
  return "/";
}

// One page view per page: drawing the same page again (a language change, for example) sends nothing.
export function trackPage(route, title) {
  const path = pagePath(route);
  if (!enabled() || path === lastPage) return;
  lastPage = path;
  gtag("event", "page_view", { page_location: location.origin + path, page_title: title });
}

export function trackSearch(query) {
  if (enabled()) gtag("event", "search", { search_term: query });
}
