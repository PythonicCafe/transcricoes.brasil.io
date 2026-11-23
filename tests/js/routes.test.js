import { test } from "node:test";
import assert from "node:assert/strict";
import { ABOUT_SLUGS, parseRoute, routeHash } from "../../site/js/modules/routes.js";

test("parseRoute reads the home page from empty and unknown fragments", () => {
  for (const hash of ["", "#", "#/", "#/whatever"]) assert.deepEqual(parseRoute(hash), { page: "home" });
});

test("parseRoute reads a session with and without a time", () => {
  assert.deepEqual(parseRoute("#/v/abc"), { page: "session", id: "abc", seconds: null });
  assert.deepEqual(parseRoute("#/v/abc/600"), { page: "session", id: "abc", seconds: 600 });
  assert.deepEqual(parseRoute("#/v/abc/"), { page: "session", id: "abc", seconds: null });
  assert.deepEqual(parseRoute("#/v/abc/x"), { page: "session", id: "abc", seconds: null });
  assert.deepEqual(parseRoute("#/v/a%2Db"), { page: "session", id: "a-b", seconds: null });
});

test("parseRoute does not fail on malformed escapes", () => {
  assert.deepEqual(parseRoute("#/v/x%ZZ"), { page: "session", id: "x%ZZ", seconds: null });
});

test("parseRoute knows the about page in every language", () => {
  assert.deepEqual(parseRoute("#/about"), { page: "about", language: "en" });
  assert.deepEqual(parseRoute("#/sobre"), { page: "about", language: "pt" });
  assert.deepEqual(parseRoute("#/acerca-de"), { page: "about", language: "es" });
});

test("routeHash writes the about page in the given language", () => {
  assert.equal(routeHash({ page: "about" }, "en"), "#/about");
  assert.equal(routeHash({ page: "about" }, "pt"), "#/sobre");
  assert.equal(routeHash({ page: "about" }, "es"), "#/acerca-de");
  assert.equal(routeHash({ page: "about" }, "fr"), "#/about");
});

test("routeHash and parseRoute agree", () => {
  for (const route of [{ page: "home" }, { page: "session", id: "a b/c", seconds: null }, { page: "session", id: "x", seconds: 61 }]) {
    assert.deepEqual(parseRoute(routeHash(route, "en")), route);
  }
  for (const language of Object.keys(ABOUT_SLUGS)) {
    assert.deepEqual(parseRoute(routeHash({ page: "about" }, language)), { page: "about", language });
  }
});

test("routeHash drops the fraction of the seconds", () => {
  assert.equal(routeHash({ page: "session", id: "x", seconds: 61.8 }), "#/v/x/61");
});
