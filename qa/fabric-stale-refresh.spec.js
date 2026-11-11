"use strict";

const { test, expect } = require("@playwright/test");
test.describe("Storage Fabric stale refresh", () => {
  require("./fixture-suites/fabric-stale-refresh")({ test, expect });
});
