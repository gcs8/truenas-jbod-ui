const { defineConfig } = require("@playwright/test");

const baseURL = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:8080";
const browserChannel = process.env.PLAYWRIGHT_BROWSER_CHANNEL || undefined;
const videoMode = process.env.CI ? "off" : "retain-on-failure";

module.exports = defineConfig({
  testDir: "./qa",
  testMatch: "**/*.spec.js",
  fullyParallel: false,
  workers: process.env.CI ? 2 : 1,
  timeout: 30_000,
  expect: {
    timeout: 15_000,
  },
  retries: process.env.CI ? 1 : 0,
  reporter: [
    ["list"],
    ["html", { open: "never" }],
  ],
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: videoMode,
    headless: true,
  },
  projects: [
    {
      name: "chromium",
      use: {
        browserName: "chromium",
        channel: browserChannel,
      },
    },
  ],
});
