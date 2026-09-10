const { test, expect } = require("@playwright/test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const { createHash } = require("node:crypto");

const root = path.resolve(process.env.UPGRADE_NOTICE_SOURCE_ROOT || path.join(__dirname, ".."));
const fixture = path.join(__dirname, "fixtures/upgrade_notice_server.py");
const python = process.env.PYTHON || "python3";
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

async function startServer(directory, mode, port = 0) {
  // Explicit minimal environment; no operator config, credentials, or app lifespan.
  const child = spawn(python, ["-c", "import runpy,sys; runpy.run_path(sys.argv.pop(1), run_name='__main__')",
    fixture, "--directory", directory, "--mode", mode, "--port", String(port)], {
    cwd: root,
    env: { PATH: process.env.PATH, HOME: directory, TMPDIR: directory, PYTHONUNBUFFERED: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  let errors = "";
  child.stdout.on("data", (bytes) => { output += bytes; });
  child.stderr.on("data", (bytes) => { errors += bytes; });
  try {
    await expect.poll(() => {
      if (child.exitCode !== null) throw new Error(`Fixture exited: ${errors}`);
      return output.includes("\n");
    }).toBe(true);
    const { origin } = JSON.parse(output.split("\n")[0]);
    await expect.poll(async () => {
      try { return (await fetch(`${origin}/__ready`)).status; } catch { return 0; }
    }).toBe(200);
    return { child, origin };
  } catch (error) {
    if (child.exitCode === null && child.signalCode === null) {
      const exited = once(child, "exit");
      child.kill("SIGTERM");
      await exited;
    }
    throw error;
  }
}

async function stopServer(server) {
  if (!server) return;
  if (server.child.exitCode === null && server.child.signalCode === null) {
    const exited = once(server.child, "exit");
    server.child.kill("SIGTERM");
    await exited;
  }
  await expect.poll(async () => {
    try { await fetch(`${server.origin}/__ready`); return false; } catch { return true; }
  }).toBe(true);
}

async function openPage(context, origin) {
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  page.setDefaultNavigationTimeout(10000);
  await page.route("**/*", (route) => {
    if (new URL(route.request().url()).origin !== origin) return route.abort();
    return route.continue();
  });
  const asset = page.waitForResponse((response) => new URL(response.url()).pathname === "/static/app.js");
  await page.goto(origin);
  expect(sha256(await (await asset).body())).toBe(sha256(fs.readFileSync(path.join(root, "app/static/app.js"))));
  return page;
}

async function signIn(page) {
  await page.locator("#read-ui-auth-username").fill("operator");
  await page.locator("#read-ui-auth-password").fill("synthetic-passphrase");
  const response = page.waitForResponse((r) => r.url().endsWith("/api/read-ui/auth/verify"));
  await page.locator("#read-ui-auth-submit").click();
  expect((await response).status()).toBe(200);
  await expect(page.locator("#read-ui-auth-status")).toContainText("Signed in for writes");
}

async function dismiss(page, status) {
  const response = page.waitForResponse((r) => r.url().endsWith("/api/upgrade-notice/dismiss"));
  await page.locator("#upgrade-notice-dismiss").click();
  expect((await response).status()).toBe(status);
}

test("basic notice: local failures preserve drafts, install dismissal survives contexts and restart", async ({ browser }) => {
  test.setTimeout(60000);
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "jbod-notice-"));
  const contexts = [];
  let server;
  try {
    server = await startServer(directory, "basic");
    const a = await browser.newContext(); contexts.push(a);
    const b = await browser.newContext(); contexts.push(b);
    const page = await openPage(a, server.origin);
    const other = await openPage(b, server.origin);
    await expect(page.locator("#upgrade-notice")).toHaveCount(1);
    await expect(page.locator("#upgrade-notice-text")).toContainText("Previous version unknown");
    await expect(page.locator("#upgrade-notice-text")).toContainText("Sign-in is required");
    await expect(page.locator("#upgrade-notice-text")).not.toContainText("Updated to");
    let attempts = 0;
    page.on("request", (r) => { if (r.url().endsWith("/api/upgrade-notice/dismiss")) attempts += 1; });
    // The production client refuses unsigned basic writes before sending HTTP.
    await page.locator("#upgrade-notice-dismiss").click();
    await expect(page.locator("#upgrade-notice-text")).toContainText("not saved for this install");
    expect(attempts).toBe(0);
    expect((await a.request.post(`${server.origin}/api/upgrade-notice/dismiss`)).status()).toBe(401);
    await page.reload();
    await expect(page.locator("#upgrade-notice-dismiss")).toHaveText("Retry saving dismissal");
    await expect(other.locator("#upgrade-notice-text")).toContainText("Previous version unknown");
    console.log("notice QA: anonymous refusal and reload verified");
    await signIn(page);
    console.log("notice QA: signed in");
    await page.locator('[data-slot="0"]').first().click();
    const draft = page.locator('#mapping-form [name="serial"]');
    await draft.fill("synthetic-unsaved-draft");
    console.log("notice QA: draft entered");
    const pendingBytes = fs.readFileSync(path.join(directory, "last_seen_version.json"), "utf8");
    fs.writeFileSync(path.join(directory, "fail-write"), "synthetic refusal");
    await dismiss(page, 503);
    await expect(page.locator("#upgrade-notice-text")).toContainText("not saved for this install");
    await expect(draft).toHaveValue("synthetic-unsaved-draft");
    expect(fs.readFileSync(path.join(directory, "last_seen_version.json"), "utf8")).toBe(pendingBytes);
    await other.reload();
    await expect(other.locator("#upgrade-notice-text")).toContainText("Previous version unknown");
    fs.unlinkSync(path.join(directory, "fail-write"));
    await dismiss(page, 200);
    await expect(page.locator("#upgrade-notice")).toBeHidden();
    await expect(draft).toHaveValue("synthetic-unsaved-draft");
    expect(attempts).toBe(2);
    console.log("notice QA: 401/503/200 and draft preservation verified");
    expect(JSON.parse(fs.readFileSync(path.join(directory, "last_seen_version.json")))).toEqual({ last_seen_version: "0.23.0" });
    page.once("dialog", (dialog) => dialog.accept());
    await page.reload();
    await other.reload();
    await expect(page.locator("#upgrade-notice")).toHaveCount(0);
    await expect(other.locator("#upgrade-notice")).toHaveCount(0);
    const port = Number(new URL(server.origin).port);
    await stopServer(server); server = null;
    server = await startServer(directory, "basic", port);
    await other.reload();
    await expect(other.locator("#upgrade-notice")).toHaveCount(0);
    const c = await browser.newContext(); contexts.push(c);
    const restarted = await openPage(c, server.origin);
    await expect(restarted.locator("#upgrade-notice")).toHaveCount(0);
  } finally {
    try {
      for (const context of contexts) await context.close().catch(() => {});
    } finally {
      await stopServer(server);
      fs.rmSync(directory, { recursive: true, force: true });
    }
  }
});

test("network notice: uninstrumented install, real 403 refusal, and later version transition", async ({ browser }) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "jbod-notice-"));
  let server;
  let context;
  try {
    server = await startServer(directory, "network");
    context = await browser.newContext();
    const page = await openPage(context, server.origin);
    await expect(page.locator("#upgrade-notice-text")).toContainText("Previous version unknown");
    await expect(page.locator("#upgrade-notice-text")).toContainText("In network mode, anyone who can reach");
    await page.route("**/api/upgrade-notice/dismiss", async (route) => {
      const response = await route.fetch({
        headers: { ...route.request().headers(), origin: "https://synthetic.invalid" },
      });
      await route.fulfill({ response });
    });
    await dismiss(page, 403);
    await expect(page.locator("#upgrade-notice-text")).toContainText("not saved for this install");
    await page.unroute("**/api/upgrade-notice/dismiss");
    await dismiss(page, 200);
    await page.reload();
    await expect(page.locator("#upgrade-notice")).toHaveCount(0);
    // An already tracked prior release is distinct from unknown first observation.
    fs.writeFileSync(path.join(directory, "last_seen_version.json"), JSON.stringify({last_seen_version: "0.22.2"}));
    await page.evaluate(() => localStorage.clear());
    await page.reload();
    await expect(page.locator("#upgrade-notice")).toHaveCount(1);
    await expect(page.locator("#upgrade-notice-text")).toContainText("Updated to v0.23.0");
    expect(JSON.parse(fs.readFileSync(path.join(directory, "last_seen_version.json"))).notice.previous).toBe("0.22.2");
  } finally {
    if (context) await context.close();
    await stopServer(server);
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
