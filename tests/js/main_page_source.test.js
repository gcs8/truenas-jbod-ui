"use strict";

// The main-page source guards read every script of the main page through
// helpers/main_page_source.js. This file keeps that list equal to the scripts
// the main page loads, so a script split out of app.js later cannot leave them behind.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { MAIN_PAGE_SCRIPTS, mainPageSource } = require("./helpers/main_page_source");

const ROOT = path.resolve(__dirname, "../..");
const BASE_TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/base.html"), "utf8");
const INDEX_TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");

// The script files a template loads, wherever its tags sit. A tag with a src
// must name a static file; a tag without one is inline code, which lives in the
// template itself and is not part of the list.
function scriptFiles(template, name) {
  const files = [];
  for (const tag of template.match(/<script\b[^>]*>/gi) || []) {
    if (!/\bsrc\b/i.test(tag)) continue;
    const source = tag.match(/\bsrc\s*=\s*"\{\{\s*url_for\(\s*'static'\s*,\s*path\s*=\s*'([^']+)'\s*\)\s*\}\}[^"]*"/i);
    assert.ok(source, `${name}: every script file is a static file: ${tag}`);
    files.push(`app/static/${source[1]}`);
  }
  return files;
}

function occurrences(text, part) {
  let count = 0;
  for (let index = text.indexOf(part); index !== -1; index = text.indexOf(part, index + part.length)) count += 1;
  return count;
}

test("the helper lists exactly the script files the main page loads, app.js first", () => {
  const scripts = [...scriptFiles(BASE_TEMPLATE, "base.html"), ...scriptFiles(INDEX_TEMPLATE, "index.html")];
  assert.ok(scripts.includes("app/static/app.js"), "the main page loads app.js");
  assert.equal(MAIN_PAGE_SCRIPTS[0], "app/static/app.js");
  assert.equal(new Set(MAIN_PAGE_SCRIPTS).size, MAIN_PAGE_SCRIPTS.length, "no script is listed twice");
  assert.deepEqual([...MAIN_PAGE_SCRIPTS].sort(), [...scripts].sort());
});

test("the main page is built from base.html and index.html alone", () => {
  assert.match(INDEX_TEMPLATE, /^\{%-?\s*extends\s+"base\.html"\s*-?%\}/);
  assert.doesNotMatch(INDEX_TEMPLATE, /\{%-?\s*block\s+scripts\b/, "index.html keeps the scripts base.html loads");
  for (const [name, template] of [["base.html", BASE_TEMPLATE], ["index.html", INDEX_TEMPLATE]]) {
    assert.doesNotMatch(template, /\{%-?\s*(?:include|import|from)\b/, `${name} pulls in no other template`);
  }
});

test("the joined source starts with app.js and holds each other script once", () => {
  const joined = mainPageSource();
  const appSource = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
  assert.ok(joined.startsWith(appSource), "app.js comes first, unchanged");
  if (MAIN_PAGE_SCRIPTS.length > 1) assert.equal(joined[appSource.length], "\n", "a newline follows app.js");
  for (const script of MAIN_PAGE_SCRIPTS.slice(1)) {
    const text = fs.readFileSync(path.join(ROOT, script), "utf8");
    assert.ok(text.length > 0, `${script} is not empty`);
    assert.equal(occurrences(joined, text), 1, `${script} is read once`);
  }
});
