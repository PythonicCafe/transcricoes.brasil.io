// Entry point of the site: state, routes (in the URL fragment) and the drawing of the pages.
import { TEXTS } from "./modules/texts.js";
import { escapeHtml, highlight, matchingLines, normalize, queryTerms, search } from "./modules/search.js";
import { clock, readableDate, readableDuration } from "./modules/format.js";
import { decodeList, pageSlice, visiblePages } from "./modules/list.js";
import { parseRoute, routeHash } from "./modules/routes.js";

function stored(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch (error) { return fallback; }
}
function store(key, value) {
  try { localStorage.setItem(key, value); } catch (error) { /* the choice lasts only for this visit */ }
}

let language = stored("language", "en");
if (!TEXTS[language]) language = "en";
let colorTheme = stored("color-theme", "system");
let list = null;            // {config, sessions: [...]}: the slim list of `data/items.json`, decoded
let searchData = null;      // `data/search.json`: {lines: {id: [[seconds, text], ...]}, keywords: {id: [...]}}
let loadingSearch = null;
const fullItems = new Map();    // id -> promise of `data/items/<id>.json` (the complete session)
const lineCache = {};
const state = { query: "", houses: new Set(), theme: "", from: "", to: "", page: 1 };
const PER_PAGE = 20;       // cards drawn at a time, in the list and in the search results

const t = (key, values = {}) => {
  let text = TEXTS[language][key] ?? key;
  for (const [name, value] of Object.entries(values)) text = text.replaceAll(`{${name}}`, value);
  return text;
};
const date = (iso) => readableDate(iso, TEXTS[language].locale);
const app = () => document.getElementById("app");

function applyColorTheme() {
  const dark = colorTheme === "dark" || (colorTheme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-bs-theme", dark ? "dark" : "light");
}

// Download `search.json` in chunks, to tell how much has arrived: the file is big and the first search waits for it.
// `onProgress` gets the fraction from 0 to 1; the total comes from `items.json`, because the browser only knows the
// compressed size.
let onProgress = () => {};
function loadSearch() {
  if (!loadingSearch) {
    loadingSearch = fetch("data/search.json").then(async (response) => {
      if (!response.ok) throw new Error(String(response.status));
      const total = list.config.search_bytes;
      if (!response.body || !total) return response.json();
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const chunks = [];
      let received = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(decoder.decode(value, { stream: true }));
        received += value.length;
        onProgress(Math.min(received / total, 1));
      }
      chunks.push(decoder.decode());
      return JSON.parse(chunks.join(""));
    }).then((data) => { searchData = data; });
    loadingSearch.catch(() => { loadingSearch = null; });
  }
  return loadingSearch;
}

// The complete session (metadata, summary, participants, decisions and lines); the promise fails if the id is unknown.
function loadItem(id) {
  if (!fullItems.has(id)) {
    fullItems.set(id, fetch(`data/items/${encodeURIComponent(id)}.json`).then((response) => { if (!response.ok) throw new Error(String(response.status)); return response.json(); }));
  }
  return fullItems.get(id);
}

const thumbnail = (id) => list.config.without_thumbnail.includes(id) ? `https://i.ytimg.com/vi/${encodeURIComponent(id)}/mqdefault.jpg` : `thumbnails/${encodeURIComponent(id)}.webp`;

function filteredSessions() {
  return list.sessions.filter((session) =>
    (!state.houses.size || state.houses.has(session.house)) &&
    (!state.theme || session.themes.includes(state.theme)) &&
    (!state.from || session.date >= state.from) &&
    (!state.to || session.date <= state.to));
}

const source = (session) => `${escapeHtml(session.house)}${session.state ? " (" + escapeHtml(session.state) + ")" : ""}`;
const themesHtml = (session) => session.themes.map((theme) => `<span class="theme-badge" lang="pt-BR">${escapeHtml(theme)}</span>`).join(" ");

function card(session, terms, extra = "") {
  const target = routeHash({ page: "session", id: session.id, seconds: null });
  return `<article class="result-card">
    <a class="result-card__thumb" href="${target}" tabindex="-1" aria-hidden="true"><img class="result-card__image" src="${escapeHtml(thumbnail(session.id))}" width="320" height="180" loading="lazy" alt=""></a>
    <div class="result-card__body">
      <h2 class="result-card__title" lang="pt-BR"><a href="${target}">${highlight(session.title, terms)}</a></h2>
      <p class="result-card__meta"><span class="result-card__source" lang="pt-BR">${source(session)}</span>, ${t("published")} ${date(session.date)} · <span lang="pt-BR">${escapeHtml(session.type)}</span></p>
      <p class="result-card__label">${t("card_summary")}</p>
      <p class="result-card__summary" lang="pt-BR">${highlight(session.summary, terms)}</p>
      <p class="result-card__footer"><span>${readableDuration(session.duration)}</span> ${themesHtml(session)}</p>
      ${extra}
    </div></article>`;
}

function housesLabel() {
  if (!state.houses.size) return t("all_houses");
  return state.houses.size === 1 ? escapeHtml([...state.houses][0]) : t("chosen_houses", { n: state.houses.size });
}

function drawHome() {
  const houses = [...new Set(list.sessions.map((session) => session.house))].sort((first, second) => first.localeCompare(second, "pt"));
  const themes = [...new Set(list.sessions.flatMap((session) => session.themes))].sort((first, second) => first.localeCompare(second, "pt"));
  const hours = Math.round(list.sessions.reduce((sum, session) => sum + session.duration, 0) / 3600);
  app().innerHTML = `
    <h1 class="page-title">${t("title")}</h1>
    <p class="page-lead">${t("lead", { sessions: list.sessions.length, houses: houses.length, hours })}</p>
    <form class="search-form" id="search-form" role="search">
      <div class="search-form__query">
        <label class="visually-hidden" for="query">${t("search_label")}</label>
        <input id="query" class="form-control form-control-lg" type="search" placeholder="${escapeHtml(t("search_placeholder"))}" value="${escapeHtml(state.query)}">
      </div>
      <button class="btn btn-lg search-form__submit" type="submit">${t("search_button")}</button>
      <details class="multi-select search-form__filter" id="houses">
        <summary class="form-select multi-select__summary" id="houses-label">${housesLabel()}</summary>
        <fieldset class="multi-select__options">
          <legend class="visually-hidden">${t("all_houses")}</legend>
          ${houses.map((house) => `<label class="form-check multi-select__option"><input class="form-check-input" type="checkbox" value="${escapeHtml(house)}" ${state.houses.has(house) ? "checked" : ""}> <span class="form-check-label" lang="pt-BR">${escapeHtml(house)}</span></label>`).join("")}
        </fieldset>
      </details>
      <div class="search-form__filter">
        <label class="visually-hidden" for="theme">${t("all_themes")}</label>
        <select id="theme" class="form-select"><option value="">${t("all_themes")}</option>${themes.map((theme) => `<option ${theme === state.theme ? "selected" : ""}>${escapeHtml(theme)}</option>`).join("")}</select>
      </div>
      <div class="input-group search-form__date"><label class="input-group-text" for="from">${t("from")}</label><input id="from" class="form-control" type="date" value="${escapeHtml(state.from)}"></div>
      <div class="input-group search-form__date"><label class="input-group-text" for="to">${t("to")}</label><input id="to" class="form-control" type="date" value="${escapeHtml(state.to)}"></div>
    </form>
    <div id="results" aria-live="polite" tabindex="-1"></div>`;
  const apply = (event) => {
    if (event && event.type === "submit") event.preventDefault();
    state.query = document.getElementById("query").value.trim();
    state.theme = document.getElementById("theme").value;
    state.from = document.getElementById("from").value;
    state.to = document.getElementById("to").value;
    state.houses = new Set([...document.querySelectorAll("#houses input:checked")].map((box) => box.value));
    state.page = 1;
    document.getElementById("houses-label").innerHTML = housesLabel();
    drawResults();
  };
  document.getElementById("search-form").addEventListener("submit", apply);
  for (const id of ["theme", "from", "to", "houses"]) document.getElementById(id).addEventListener("change", apply);
  drawResults();
}

function passagesHtml(session, passages, terms) {
  return passages.map((passage) => `<li class="passage-list__item"><a class="time-link" href="${routeHash({ page: "session", id: session.id, seconds: passage.seconds })}">▶ ${clock(passage.seconds)}</a> <span lang="pt-BR">${highlight(passage.text, terms)}</span></li>`).join("");
}

// Page buttons: the first, the last and the neighbours of the current one, with an ellipsis in the jumps. Empty when
// there is only one page.
function paginationHtml(current, pages) {
  if (pages < 2) return "";
  const numbers = visiblePages(current, pages);
  const buttons = numbers.map((number, position) =>
    `${position && number - numbers[position - 1] > 1 ? '<li class="page-nav__gap" aria-hidden="true">…</li>' : ""}<li><button class="page-nav__button" type="button" data-page="${number}" aria-label="${escapeHtml(t("page", { n: number }))}"${number === current ? ' aria-current="page"' : ""}>${number}</button></li>`).join("");
  return `<nav aria-label="${escapeHtml(t("pagination"))}"><ul class="page-nav">
    <li><button class="page-nav__button" type="button" data-page="${current - 1}"${current === 1 ? " disabled" : ""}>${t("previous")}</button></li>
    ${buttons}
    <li><button class="page-nav__button" type="button" data-page="${current + 1}"${current === pages ? " disabled" : ""}>${t("next")}</button></li>
  </ul></nav>`;
}

// The current page of `items` (search and filters still apply to all of them; only the drawing is paged).
function currentPage(items) {
  const { page, pages, visible } = pageSlice(items, state.page, PER_PAGE);
  state.page = page;
  const position = pages > 1 ? ` · ${t("page_of", { n: page, total: pages })}` : "";
  return { visible, position, navigation: paginationHtml(page, pages) };
}

function bindPagination(target) {
  target.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => {
    state.page = Number(button.dataset.page);
    drawResults();
    target.scrollIntoView({ behavior: "instant" });
    target.focus({ preventScroll: true });
  }));
}

function drawResults() {
  const target = document.getElementById("results");
  const terms = queryTerms(state.query);
  if (!terms.length) {
    const sessions = filteredSessions();
    const { visible, position, navigation } = currentPage(sessions);
    target.innerHTML = `<p class="result-count">${sessions.length} ${t("sessions")}${position}</p>` + visible.map((session) => card(session, [])).join("") + navigation;
    bindPagination(target);
    return;
  }
  if (!searchData) {
    target.innerHTML = `<div class="load-status" role="status">
        <p class="load-status__text">${t("loading_search")} <span id="load-percent"></span></p>
        <progress class="load-status__bar" id="load-bar" max="100" value="0" aria-label="${escapeHtml(t("loading_search"))}"></progress>
      </div>`;
    onProgress = (fraction) => {
      const bar = document.getElementById("load-bar");
      if (!bar) return;
      bar.value = Math.round(fraction * 100);
      document.getElementById("load-percent").textContent = `${Math.round(fraction * 100)}%`;
    };
    loadSearch().then(drawResults).catch(() => {
      const current = document.getElementById("results");
      if (current) current.innerHTML = `<p class="empty-state">${t("search_error")}</p>`;
    });
    return;
  }
  const results = search(filteredSessions(), searchData, terms, lineCache);
  const total = results.reduce((sum, result) => sum + result.passages.length, 0);
  if (!results.length) { target.innerHTML = `<p class="empty-state">${t("no_results")}</p>`; return; }
  const { visible, position, navigation } = currentPage(results);
  target.innerHTML = `<p class="result-count">${t("results", { passages: total, sessions: results.length })} <strong>${escapeHtml(state.query)}</strong>${position}</p>` +
    visible.map(({ session, passages }, number) => card(session, terms, `
        <p class="result-card__label">${passages.length ? passages.length + " " + t("passages") : t("summary_only")}</p>
        <ul class="passage-list" id="passages-${number}">${passagesHtml(session, passages.slice(0, 5), terms)}</ul>
        ${passages.length > 5 ? `<button class="btn btn-link btn-sm passage-list__more" type="button" data-more="${number}">${t("show_all", { n: passages.length })}</button>` : ""}`)).join("") + navigation;
  bindPagination(target);
  target.querySelectorAll("[data-more]").forEach((button) => button.addEventListener("click", () => {
    const { session, passages } = visible[Number(button.dataset.more)];
    document.getElementById(`passages-${button.dataset.more}`).innerHTML = passagesHtml(session, passages, terms);
    button.remove();
  }));
}

// `enablejsapi` and `origin` let YouTube's iframe API report the current time of the video.
function playerUrl(id, seconds, autoplay) {
  return `https://www.youtube-nocookie.com/embed/${encodeURIComponent(id)}?start=${Math.floor(seconds)}${autoplay ? "&autoplay=1" : ""}&enablejsapi=1&origin=${encodeURIComponent(location.origin)}`;
}

let playerApi = null;       // promise of YouTube's iframe API, loaded on the first session opened
let player = null;          // player of the open session, once the API is ready
let playerClock = null;     // interval that follows the time of the video
let openSession = 0;        // changes on every session drawn, to drop answers meant for a previous session

function loadPlayerApi() {
  if (!playerApi) {
    playerApi = new Promise((resolve, reject) => {
      window.onYouTubeIframeAPIReady = () => resolve(window.YT);
      const script = document.createElement("script");
      script.src = "https://www.youtube.com/iframe_api";
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }
  return playerApi;
}

function stopPlayer() {
  clearInterval(playerClock);
  playerClock = null;
  player = null;
  openSession++;
}

function drawSession(id, seconds) {
  const turn = openSession;
  app().innerHTML = `<p class="result-count">${t("loading")}</p>`;
  loadItem(id).then((session) => { if (turn === openSession) drawLoadedSession(session, seconds, turn); })
    .catch(() => { fullItems.delete(id); if (turn === openSession) location.hash = routeHash({ page: "home" }); });
}

function drawLoadedSession(session, seconds, turn) {
  const id = session.id;
  const issue = `${list.config.repository}/issues/new?title=${encodeURIComponent("Erro em " + id + ": " + session.title)}`;
  const bullets = (items) => items.length ? `<ul class="machine-box__list" lang="pt-BR">${items.map((item) => `<li>${item}</li>`).join("")}</ul>` : "";
  // YouTube refuses the player (error 153) when the page sends no Referer header; hence the referrerpolicy.
  app().innerHTML = `
    <p><a href="${routeHash({ page: "home" })}">${t("back")}</a></p>
    <h1 class="page-title page-title--session" lang="pt-BR">${escapeHtml(session.title)}</h1>
    <p class="page-lead"><span lang="pt-BR">${source(session)}</span>, ${t("published")} ${date(session.date)} · ${readableDuration(session.duration)} · <span lang="pt-BR">${escapeHtml(session.type)}</span> ·
      <a href="https://www.youtube.com/watch?v=${encodeURIComponent(id)}" target="_blank" rel="noopener">${t("open_youtube")}</a> · <a href="captions/${encodeURIComponent(id)}.vtt" download>${t("download_captions")}</a> · <a href="${escapeHtml(issue)}" target="_blank" rel="noopener">${t("report_error")}</a></p>
    <div class="session-layout">
      <div class="session-layout__main">
        <div class="video-player ratio ratio-16x9"><iframe id="player" src="${playerUrl(id, seconds || 0, seconds !== null)}" title="YouTube: ${escapeHtml(session.title)}" referrerpolicy="strict-origin-when-cross-origin" allow="autoplay; encrypted-media; picture-in-picture; web-share" allowfullscreen></iframe></div>
        <section class="machine-box" aria-labelledby="summary-title">
          <h2 class="machine-box__label" id="summary-title">${t("machine_summary")} · ${t("model")} ${escapeHtml(session.model)}</h2>
          <p lang="pt-BR">${escapeHtml(session.summary)}</p>
          <p>${themesHtml(session)}</p>
          ${session.keywords.length ? `<p class="machine-box__detail"><strong>${t("keywords")}:</strong> <span lang="pt-BR">${session.keywords.map(escapeHtml).join(", ")}</span></p>` : ""}
          ${session.participants.length ? `<div class="machine-box__detail"><strong>${t("participants")}:</strong>${bullets(session.participants.map((person) => `${escapeHtml(person.name)}${person.role ? " (" + escapeHtml(person.role) + ")" : ""}`))}</div>` : ""}
          <div class="machine-box__detail"><strong>${t("decisions")}:</strong> ${session.decisions.length ? bullets(session.decisions.map(escapeHtml)) : t("no_decisions")}</div>
          <p class="machine-box__warning">${t("machine_warning")}</p>
        </section>
      </div>
      <section class="session-layout__side" aria-labelledby="transcript-title">
        <h2 class="machine-box__label" id="transcript-title">${t("machine_transcript")}</h2>
        <div class="transcript-search">
          <label class="visually-hidden" for="session-search">${t("search_session")}</label>
          <input id="session-search" class="form-control" type="search" placeholder="${escapeHtml(t("search_session"))}" value="${escapeHtml(state.query)}">
          <span class="transcript-search__count" id="session-count" aria-live="polite"></span>
        </div>
        <p class="transcript-hint">${t("click_hint")}</p>
        <ol class="transcript-list" id="transcript" tabindex="0" lang="pt-BR"></ol>
      </section>
    </div>`;
  const lines = session.lines;
  const target = document.getElementById("transcript");
  const normalized = lineCache[id] ??= lines.map((line) => normalize(line[1]));
  const field = document.getElementById("session-search");
  const count = document.getElementById("session-count");
  let current = -1;
  if (seconds !== null) lines.forEach((line, position) => { if (line[0] <= seconds) current = position; });
  // Mark line `position` as the current one, if it is in the list (with a filter it may not be). The list only
  // scrolls by itself when the pointer is not over it, so it does not fight with whoever is reading.
  const mark = (position, scroll) => {
    current = position;
    const previous = target.querySelector(".is-current");
    const next = target.querySelector(`[data-line="${position}"]`);
    if (previous === next) return;
    if (previous) previous.classList.remove("is-current");
    if (!next) return;
    next.classList.add("is-current");
    if (scroll && !target.matches(":hover")) target.scrollTop = next.offsetTop - target.offsetTop - 60;
  };
  // With terms in the field, the list shows only the matching lines (and the next one, when the passage spans two).
  const drawLines = () => {
    const terms = queryTerms(field.value);
    let positions = lines.map((_, position) => position);
    if (terms.length) {
      const found = matchingLines(normalized, terms);
      positions = found.flatMap(({ position, whole }) => whole ? [position] : [position, position + 1]);
      count.textContent = t("matches", { n: found.length });
    } else {
      count.textContent = "";
    }
    target.innerHTML = positions.length
      ? positions.map((position) => `<li class="transcript-list__line" data-line="${position}"><button class="time-link" type="button" data-seconds="${lines[position][0]}" data-position="${position}">▶ ${clock(lines[position][0])}</button> ${highlight(lines[position][1], terms)}</li>`).join("")
      : `<li>${t("no_session_matches")}</li>`;
    target.scrollTop = 0;
    mark(current, true);
  };
  field.addEventListener("input", () => { state.query = field.value.trim(); drawLines(); });
  target.addEventListener("click", (event) => {
    const button = event.target.closest("[data-seconds]");
    if (!button) return;
    const lineStart = Number(button.dataset.seconds);
    if (player) {
      player.seekTo(lineStart, true);
      player.playVideo();
    } else {
      document.getElementById("player").src = playerUrl(id, lineStart, true);
    }
    mark(Number(button.dataset.position), false);
  });
  drawLines();

  // While the video plays, follow its time and mark the matching line. Without YouTube's API (blocked, for example)
  // the rest of the page keeps working, only without the automatic marking.
  loadPlayerApi().then((YT) => {
    if (turn !== openSession || !document.getElementById("player")) return;
    const created = new YT.Player("player", { events: { onReady: () => { if (turn === openSession) player = created; } } });
    playerClock = setInterval(() => {
      if (turn !== openSession || !player || player.getPlayerState() !== YT.PlayerState.PLAYING) return;
      const now = player.getCurrentTime();
      let low = 0, high = lines.length - 1, position = -1;
      while (low <= high) {
        const middle = (low + high) >> 1;
        if (lines[middle][0] <= now) { position = middle; low = middle + 1; } else { high = middle - 1; }
      }
      if (position !== current) mark(position, true);
    }, 500);
  }).catch(() => {});
}

function drawAbout() {
  const houses = new Set(list.sessions.map((session) => session.house)).size;
  const hours = Math.round(list.sessions.reduce((sum, session) => sum + session.duration, 0) / 3600);
  const models = list.config.models.map((model) => `${escapeHtml(model)} (${t("open_weights")})`).join(", ");
  const values = { sessions: list.sessions.length, hours, houses, models, repo: escapeHtml(list.config.repository) };
  app().innerHTML = `<p><a href="${routeHash({ page: "home" })}">${t("back")}</a></p><h1 class="page-title">${t("about_title")}</h1><div class="prose-block">` +
    TEXTS[language].about.map((paragraph) => {
      let text = paragraph;
      for (const [name, value] of Object.entries(values)) text = text.replaceAll(`{${name}}`, value);
      return `<p>${text}</p>`;
    }).join("") + "</div>";
}

function draw() {
  document.documentElement.lang = TEXTS[language].locale;
  document.title = `${t("brand")} - Brasil.IO`;
  document.querySelectorAll("[data-t]").forEach((element) => { element.textContent = t(element.dataset.t); });
  document.querySelectorAll("[data-t-label]").forEach((element) => { element.setAttribute("aria-label", t(element.dataset.tLabel)); });
  document.querySelectorAll("[data-language]").forEach((button) => { button.setAttribute("aria-pressed", String(button.dataset.language === language)); });
  document.querySelectorAll("[data-color-theme]").forEach((button) => { button.setAttribute("aria-pressed", String(button.dataset.colorTheme === colorTheme)); });
  document.getElementById("about-link").href = routeHash({ page: "about" }, language);
  const route = parseRoute(location.hash);
  // The about page in another language (a shared link, or the language just changed) gets the current language's
  // address; replaceState changes the address without a new history entry and without firing `hashchange`.
  if (route.page === "about" && route.language !== language) history.replaceState(null, "", routeHash(route, language));
  if (!list) return;
  document.getElementById("repository-link").href = list.config.repository;
  window.scrollTo(0, 0);
  stopPlayer();
  if (route.page === "session") drawSession(route.id, route.seconds);
  else if (route.page === "about") drawAbout();
  else drawHome();
}

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyColorTheme);
document.addEventListener("click", (event) => {
  const languageButton = event.target.closest("[data-language]");
  if (languageButton) {
    language = languageButton.dataset.language;
    store("language", language);
    draw();
    return;
  }
  const themeButton = event.target.closest("[data-color-theme]");
  if (themeButton) {
    colorTheme = themeButton.dataset.colorTheme;
    store("color-theme", colorTheme);
    applyColorTheme();
    document.querySelectorAll("[data-color-theme]").forEach((button) => { button.setAttribute("aria-pressed", String(button === themeButton)); });
    return;
  }
  const open = document.querySelector("details.multi-select[open]");
  if (open && !open.contains(event.target)) open.removeAttribute("open");
});
window.addEventListener("hashchange", draw);
applyColorTheme();
draw();
// On a direct link to a session, its file starts downloading together with the list, without waiting for it.
const firstRoute = parseRoute(location.hash);
if (firstRoute.page === "session") loadItem(firstRoute.id).catch(() => {});
fetch("data/items.json").then((response) => response.json()).then((data) => { list = decodeList(data); draw(); })
  .catch(() => { app().innerHTML = "<p class=\"empty-state\">Could not load data/items.json.</p>"; });
