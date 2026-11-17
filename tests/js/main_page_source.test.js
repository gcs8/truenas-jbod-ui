"use strict";

// The main-page source guards read every script of the main page through
// helpers/main_page_source.js. This file keeps that list equal to the scripts
// base.html loads, so a script split out of app.js later cannot leave them behind.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { MAIN_PAGE_SCRIPTS, mainPageSource } = require("./helpers/main_page_source");

const ROOT = path.resolve(__dirname, "../..");
const BASE_TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/base.html"), "utf8");
const INDEX_TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");

function templateScripts() {
  const block = BASE_TEMPLATE.match(/\{% block scripts %\}([\s\S]*?)\{% endblock %\}/);
  assert.ok(block, "base.html has a scripts block");
  const tags = block[1].match(/<script\b[^>]*>/g) || [];
  return tags.map((tag) => {
    const source = tag.match(/\bsrc="\{\{ url_for\('static', path='([^']+)'\) \}\}[^"]*"/);
    assert.ok(source, `every script in the scripts block is a static file: ${tag}`);
    return `app/static/${source[1]}`;
  });
}

function occurrences(text, part) {
  let count = 0;
  for (let index = text.indexOf(part); index !== -1; index = text.indexOf(part, index + part.length)) count += 1;
  return count;
}

test("the helper lists exactly the scripts base.html loads, app.js first", () => {
  const scripts = templateScripts();
  assert.ok(scripts.includes("app/static/app.js"), "base.html loads app.js");
  assert.equal(MAIN_PAGE_SCRIPTS[0], "app/static/app.js");
  assert.equal(new Set(MAIN_PAGE_SCRIPTS).size, MAIN_PAGE_SCRIPTS.length, "no script is listed twice");
  assert.deepEqual([...MAIN_PAGE_SCRIPTS].sort(), [...scripts].sort());
});

test("index.html loads no script file of its own", () => {
  assert.doesNotMatch(INDEX_TEMPLATE, /<script\b[^>]*\bsrc=/);
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
