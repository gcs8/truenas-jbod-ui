const fs = require("node:fs");
const path = require("node:path");
const { defineConfig } = require("@playwright/test");

const baseURL = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:8080";
const browserChannel = process.env.PLAYWRIGHT_BROWSER_CHANNEL || undefined;
const privateOutputDir = process.env.PLAYWRIGHT_PRIVATE_OUTPUT_DIR || "";

function readPrivateCredential(fileVariable, valueVariable) {
  const filePath = process.env[fileVariable] || "";
  if (!filePath) {
    return process.env[valueVariable] || "";
  }
  const metadata = fs.lstatSync(filePath);
  if (!metadata.isFile() || metadata.isSymbolicLink() || (metadata.mode & 0o077) !== 0) {
    throw new Error(`${fileVariable} must name a private regular file.`);
  }
  if (metadata.size < 1 || metadata.size > 4096) {
    throw new Error(`${fileVariable} has an invalid size.`);
  }
  return fs.readFileSync(filePath, "utf8");
}

if (privateOutputDir) {
  const metadata = fs.lstatSync(privateOutputDir);
  if (!path.isAbsolute(privateOutputDir) || !metadata.isDirectory() || metadata.isSymbolicLink() || (metadata.mode & 0o077) !== 0) {
    throw new Error("PLAYWRIGHT_PRIVATE_OUTPUT_DIR must be a private absolute directory.");
  }
}

const videoMode = privateOutputDir || process.env.CI ? "off" : "retain-on-failure";
const traceMode = privateOutputDir ? "off" : process.env.PLAYWRIGHT_TRACE_MODE || "retain-on-failure";
const screenshotMode = privateOutputDir ? "off" : "only-on-failure";
const httpUsername = readPrivateCredential("PLAYWRIGHT_HTTP_USERNAME_FILE", "PLAYWRIGHT_HTTP_USERNAME");
const httpPassword = readPrivateCredential("PLAYWRIGHT_HTTP_PASSWORD_FILE", "PLAYWRIGHT_HTTP_PASSWORD");

if (Boolean(httpUsername) !== Boolean(httpPassword)) {
  throw new Error("PLAYWRIGHT_HTTP_USERNAME and PLAYWRIGHT_HTTP_PASSWORD must be set together.");
}

const httpCredentials = httpUsername
  ? { username: httpUsername, password: httpPassword }
  : undefined;

module.exports = defineConfig({
  ...(privateOutputDir ? { outputDir: privateOutputDir } : {}),
  testDir: "./qa",
  testMatch: "**/*.spec.js",
  fullyParallel: false,
  workers: process.env.CI ? 2 : 1,
  timeout: 30_000,
  expect: {
    timeout: 15_000,
  },
  retries: process.env.CI ? 1 : 0,
  reporter: privateOutputDir
    ? [["list"]]
    : [
        ["list"],
        ["html", { open: "never" }],
      ],
  use: {
    baseURL,
    trace: traceMode,
    screenshot: screenshotMode,
    video: videoMode,
    headless: true,
    httpCredentials,
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
