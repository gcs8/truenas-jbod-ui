"use strict";

// The main page's own scripts, app.js first. Source guards that prove a text is
// absent from the main page read all of them, so code moved out of app.js stays
// covered. main_page_source.test.js checks this list against base.html.

const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../../..");
const MAIN_PAGE_SCRIPTS = ["app/static/app.js", "app/static/app_smart_formatters.js"];

// Every script's text in list order, one newline between, so offsets inside the
// app.js part are the same as in app.js.
function mainPageSource() {
  return MAIN_PAGE_SCRIPTS.map((script) => fs.readFileSync(path.join(ROOT, script), "utf8")).join("\n");
}

module.exports = { MAIN_PAGE_SCRIPTS, mainPageSource };
