"use strict";

const { test, expect } = require("@playwright/test");
test.describe("SMART request ownership", () => {
  require("./fixture-suites/smart-browser-ownership")({ test, expect });
});
