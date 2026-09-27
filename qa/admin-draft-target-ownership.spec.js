"use strict";

// Complete checked-in page and scripts, synthetic transport only. No live stack.
const { test, expect } = require("@playwright/test");
test.use({ serviceWorkers: "block" });
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const keep = "__TRUENAS_JBOD_KEEP_EXISTING_VALUE__";
const view = (id, target, devices = []) => ({
  id, label: id, kind: "manual", template_id: "synthetic-manual", enabled: true, order: id === "view-a" ? 10 : 20,
  render: { show_in_main_ui: true, show_in_admin_ui: true },
  binding: { mode: "manual", target_system_id: target, device_names: devices },
});
const systems = ["a", "b"].map((id) => ({
  id: `system-${id}`, label: `Synthetic ${id.toUpperCase()}`, platform: "quantastor",
  truenas_host: `node-${id}.example.test`, api_user: "synthetic", api_password_configured: true,
  ssh_enabled: false, ha_enabled: true,
  ha_nodes: ["a", "b"].map((node) => ({ system_id: `node-${node}`, label: `Node ${node.toUpperCase()}`, host: `node-${node}.example.test` })),
  storage_views: [view("view-a", "node-a"), view("view-b", "node-b")],
}));
const bootstrap = {
  systems, profiles: [{ id: "synthetic-profile", label: "Synthetic profile", rows: 1, columns: 2, slot_count: 2, slot_layout: [[0, 1]] }, { id: "synthetic-second", label: "Synthetic second", rows: 1, columns: 2, slot_count: 2, slot_layout: [[0, 1]] }],
  setup_platform_defaults: { quantastor: { ssh_commands: ["synthetic-read-only-command"] } },
  default_system_id: "system-a", admin: {},
  storage_view_templates: [{ id: "synthetic-manual", label: "Synthetic manual", kind: "manual", slot_count: 2, rows: 1, columns: 2 }],
};
const html = execFileSync(process.env.PYTHON || "python", ["-c", `
import json, sys
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup
env = Environment(loader=FileSystemLoader(sys.argv[1]), autoescape=True)
env.filters['script_json_text'] = lambda text: Markup(text.replace('<', '\\\\u003c'))
print(env.get_template('index.html').render(admin_bootstrap_json=sys.stdin.read(), url_for=lambda name, path: '/static/' + path))
`, path.join(root, "admin_service/templates")], { input: JSON.stringify(bootstrap), encoding: "utf8" });

async function openFixture(page) {
  const pending = { saves: [], candidates: [], edits: [], states: 0, errors: [], unexpected: [], state: JSON.parse(JSON.stringify(bootstrap)) };
  page.on("pageerror", (error) => pending.errors.push(error.message));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const json = (body, status = 200) => route.fulfill({ status, json: body });
    if (url.origin !== "https://admin.example.test") {
      pending.unexpected.push(url.href);
      return route.abort();
    }
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: html });
    if (["admin.js", "admin_backups.js", "admin.css"].some((name) => url.pathname === `/static/${name}`)) {
      // Optional exact predecessor bytes allow semantic RED replay without editing the candidate.
      const source = url.pathname === "/static/admin.js" && process.env.ADMIN_DRAFT_SCRIPT
        ? path.resolve(process.env.ADMIN_DRAFT_SCRIPT) : path.join(root, "admin_service", url.pathname);
      return route.fulfill({ path: source });
    }
    if (url.pathname === "/api/admin/system-setup") {
      pending.saves.push({ body: route.request().postDataJSON(), respond: json });
      return;
    }
    if (url.pathname === "/api/admin/storage-views/candidates") {
      pending.candidates.push({ system: url.searchParams.get("system_id"), target: url.searchParams.get("target_system_id"), respond: json });
      return;
    }
    if (url.pathname === "/api/admin/tls/inspect") return json({ inspection: { host: "node-a.example.test", leaf: { san_dns: ["tls.example.test", "second.example.test"] } } });
    if (url.pathname === "/api/admin/ssh-keys") return json({ keys: pending.state.ssh_keys || [] });
    if (["/api/admin/tls/trust-remote", "/api/admin/tls/import", "/api/admin/system-setup/quantastor-nodes", "/api/admin/ssh-keys/generate", "/api/admin/system-setup/bootstrap", "/api/admin/profiles"].includes(url.pathname)) {
      pending.edits.push({ path: url.pathname, body: route.request().postDataJSON(), respond: json });
      return;
    }
    if (url.pathname === "/api/admin/state") { pending.states++; return json(pending.state); }
    if (url.pathname === "/api/admin/storage-views/live-enclosures") return json({ enclosures: [], system_id: url.searchParams.get("system_id") });
    if (url.pathname === "/api/admin/history/orphaned") return json({ orphaned_systems: [] });
    if (url.pathname === "/api/admin/system-setup/sudoers-preview") return json({ content: "Synthetic preview" });
    pending.unexpected.push(url.pathname);
    return json({ detail: "Unmocked synthetic endpoint" }, 404);
  });
  await page.goto("https://admin.example.test/");
  await page.locator('[data-existing-system-id="system-a"]').click();
  await expect(page.locator("#setup-system-id")).toHaveValue("system-a");
  await page.locator("details").evaluateAll((nodes) => nodes.forEach((node) => { node.open = true; }));
  await expect.poll(() => pending.candidates.length).toBeGreaterThan(0);
  return pending;
}
async function save(page, pending) {
  const index = pending.saves.length;
  await page.locator("#setup-create-button").click();
  await expect.poll(() => pending.saves.length).toBe(index + 1);
  return pending.saves[index];
}
async function acknowledge(request) {
  await request.respond({ ok: true, updated_existing: true, system: { id: request.body.system_id, label: request.body.label }, systems });
}
async function finished(page) { await expect(page.locator("#setup-create-button")).toBeEnabled(); }
async function expectDiscard(page, target = "Synthetic A") {
  const dialogs = [];
  const handler = async (dialog) => { dialogs.push(dialog.message()); await dialog.dismiss(); };
  page.on("dialog", handler);
  await page.locator("#existing-system-reset-button").click();
  page.off("dialog", handler);
  expect(dialogs).toEqual([`Discard unsaved changes to ${target}?`]);
}
function clean(pending) { expect(pending.errors).toEqual([]); expect(pending.unexpected).toEqual([]); }
function candidate(target, device = "sda") {
  return { candidate_id: `candidate-${target}`, label: `Synthetic candidate ${target}`, device_names: [device], recommended_binding: { device_names: [device], serials: [], pcie_addresses: [] } };
}
async function candidateResponse(request, device = "sda") {
  await request.respond({ system_id: request.system, target_system_id: request.target, candidates: [candidate(request.target, device)] });
}

test("save acknowledgement retains later typing and its discard guard", async ({ page }) => {
  const pending = await openFixture(page);
  await page.locator("#setup-system-label").fill("Submitted A");
  const request = await save(page, pending);
  await page.locator("#setup-system-label").fill("Later A");
  await acknowledge(request);
  await finished(page);
  await expect(page.locator("#setup-system-label")).toHaveValue("Later A");
  await expectDiscard(page);
  expect(pending.states).toBe(1);
  clean(pending);
});

test("A completion cannot rebind B IDs, dirty protection or saved-secret context", async ({ page }) => {
  const pending = await openFixture(page);
  await page.locator("#setup-system-label").fill("Submitted A");
  const request = await save(page, pending);
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator('[data-existing-system-id="system-b"]').click();
  await page.locator("#setup-system-label").fill("Later B");
  await acknowledge(request);
  await finished(page);
  await expect(page.locator("#setup-system-id")).toHaveValue("system-b");
  await expect(page.locator("#existing-system-select")).toHaveValue("system-b");
  await expectDiscard(page, "Synthetic B");
  const b = await save(page, pending);
  expect(b.body.system_id).toBe("system-b");
  expect(b.body.replace_existing).toBe(true);
  expect(b.body.clone_source_system_id).toBe(null);
  expect(b.body.api_password).toBe(keep);
  await acknowledge(b);
  await finished(page);
  clean(pending);
});

test("A to B to A is a new editor generation even with identical submitted values", async ({ page }) => {
  const pending = await openFixture(page);
  const request = await save(page, pending);
  await page.locator('[data-existing-system-id="system-b"]').click();
  await page.locator('[data-existing-system-id="system-a"]').click();
  await page.locator("#setup-system-label").fill("Transient edit");
  await page.locator("#setup-system-label").fill("Synthetic A");
  await acknowledge(request);
  await finished(page);
  await expectDiscard(page);
  clean(pending);
});

test("unchanged save acknowledges its draft; rejected save and retry keep existing outcome behavior", async ({ page }) => {
  const pending = await openFixture(page);
  await page.locator("#setup-system-label").fill("Draft A");
  const rejected = await save(page, pending);
  await rejected.respond({ detail: "Synthetic refusal" }, 409);
  await finished(page);
  await expect(page.locator("#setup-result")).toContainText("failed");
  await expectDiscard(page);
  const accepted = await save(page, pending);
  await acknowledge(accepted);
  await finished(page);
  const dialogs = [];
  page.on("dialog", async (dialog) => { dialogs.push(dialog.message()); await dialog.dismiss(); });
  await page.locator("#existing-system-reset-button").click();
  expect(dialogs).toEqual([]);
  await expect(page.locator("#setup-system-id")).toHaveValue("");
  clean(pending);
});

test("target switch fences delayed A and disables actions until B is loaded", async ({ page }) => {
  const pending = await openFixture(page);
  const a = pending.candidates.at(-1);
  expect(a.target).toBe("node-a");
  const before = pending.candidates.length;
  await page.locator("#setup-storage-view-target-system").selectOption("node-b");
  const staleResponse = page.waitForResponse((r) => r.url().includes("/candidates?") && r.url().includes("node-a"));
  await candidateResponse(a);
  await staleResponse;
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.locator("#setup-storage-view-candidates-list")).not.toContainText("Synthetic candidate node-a");
  await expect(page.locator("#setup-storage-view-candidates-add-all-button")).toBeDisabled();
  await expect.poll(() => pending.candidates.length).toBeGreaterThan(before);
  const b = pending.candidates.at(-1);
  expect(b.target).toBe("node-b");
  await candidateResponse(b, "sdb");
  await page.locator('[data-storage-view-candidate-id="candidate-node-b"]').click();
  await expect(page.locator("#setup-storage-view-device-names")).toHaveValue("sdb");
  await expect(page.locator("#setup-storage-view-target-system")).toHaveValue("node-b");
  const request = await save(page, pending);
  expect(request.body.storage_views[0].binding.device_names).toEqual(["sdb"]);
  expect(request.body.storage_views[0].binding.target_system_id).toBe("node-b");
  await acknowledge(request);
  await finished(page);
  clean(pending);
});

test("loaded HA views with shared device names keep Add Hints and Add all target-local", async ({ page }) => {
  const pending = await openFixture(page);
  await candidateResponse(pending.candidates.at(-1));
  await page.locator('[data-storage-view-candidate-id="candidate-node-a"]').click();
  await expect(page.locator("#setup-storage-view-device-names")).toHaveValue("sda");
  let before = pending.candidates.length;
  await page.locator('[data-storage-view-id="view-b"]').click();
  await expect(page.locator("#setup-storage-view-target-system")).toHaveValue("node-b");
  await expect(page.locator("#setup-storage-view-candidates-add-all-button")).toBeDisabled();
  await expect.poll(() => pending.candidates.length).toBeGreaterThan(before);
  await candidateResponse(pending.candidates.at(-1));
  await expect(page.locator('[data-storage-view-candidate-id="candidate-node-b"]')).toBeEnabled();
  await page.locator("#setup-storage-view-candidates-add-all-button").click();
  await expect(page.locator("#setup-storage-view-device-names")).toHaveValue("sda");
  before = pending.candidates.length;
  await page.locator('[data-storage-view-id="view-a"]').click();
  await expect.poll(() => pending.candidates.length).toBeGreaterThan(before);
  await candidateResponse(pending.candidates.at(-1));
  await expect(page.locator('[data-storage-view-candidate-id="candidate-node-a"]')).toBeDisabled();
  const request = await save(page, pending);
  expect(request.body.storage_views.map((item) => item.binding.target_system_id)).toEqual(["node-a", "node-b"]);
  expect(request.body.storage_views.map((item) => item.binding.device_names)).toEqual([["sda"], ["sda"]]);
  await acknowledge(request);
  await finished(page);
  clean(pending);
});

test("refresh pending and failed discovery cannot apply previously loaded hints", async ({ page }) => {
  const pending = await openFixture(page);
  await candidateResponse(pending.candidates.at(-1));
  await expect(page.locator('[data-storage-view-candidate-id="candidate-node-a"]')).toBeEnabled();
  await page.evaluate(() => {
    // Old nodes can still be visible before the queued paint. Both registered
    // handlers must refuse them immediately, not just disable the next render.
    document.querySelector("#setup-storage-view-candidates-refresh-button").click();
    document.querySelector('[data-storage-view-candidate-id="candidate-node-a"]').click();
    document.querySelector("#setup-storage-view-candidates-add-all-button").dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await expect(page.locator("#setup-storage-view-candidates-add-all-button")).toBeDisabled();
  await expect(page.locator("#setup-storage-view-device-names")).toHaveValue("");
  await pending.candidates.at(-1).respond({ detail: "Synthetic unavailable" }, 503);
  await expect(page.locator("#admin-status-banner")).toContainText("Unable to load");
  await expect(page.locator("#setup-storage-view-candidates-add-all-button")).toBeDisabled();
  await page.locator("#setup-storage-view-candidates-refresh-button").click();
  await candidateResponse(pending.candidates.at(-1));
  await page.locator("#setup-storage-view-candidates-add-all-button").click();
  await expect(page.locator("#setup-storage-view-device-names")).toHaveValue("sda");
  clean(pending);
});

test("leaving and returning without edits does not let an old save replace the new visit result", async ({ page }) => {
  const pending = await openFixture(page);
  const request = await save(page, pending);
  await page.locator('[data-existing-system-id="system-b"]').click();
  await page.locator('[data-existing-system-id="system-a"]').click();
  const result = await page.locator("#setup-result").textContent();
  await acknowledge(request);
  await finished(page);
  await expect(page.locator("#setup-result")).toHaveText(result);
  clean(pending);
});

test("typing back to submitted value is still a later draft revision", async ({ page }) => {
  const pending = await openFixture(page);
  const request = await save(page, pending);
  await page.locator("#setup-system-label").fill("Temporary");
  await page.locator("#setup-system-label").fill("Synthetic A");
  await acknowledge(request);
  await finished(page);
  await expectDiscard(page);
  clean(pending);
});

test("candidate button edits after dispatch remain dirty despite no input event", async ({ page }) => {
  const pending = await openFixture(page);
  await candidateResponse(pending.candidates.at(-1));
  await expect(page.locator("#setup-storage-view-candidates-add-all-button")).toBeEnabled();
  const request = await save(page, pending);
  await page.locator("#setup-storage-view-candidates-add-all-button").click();
  await acknowledge(request);
  await finished(page);
  await expect(page.locator("#setup-storage-view-device-names")).toHaveValue("sda");
  await expectDiscard(page);
  clean(pending);
});

for (const action of ["add", "remove", "duplicate", "move-down"]) {
  test(`storage-view ${action} after save dispatch remains an unacknowledged draft`, async ({ page }) => {
    const pending = await openFixture(page);
    if (action === "add") await page.locator("#setup-storage-view-template").selectOption("synthetic-manual");
    const request = await save(page, pending);
    await page.locator(`#setup-storage-view-${action}-button`).click();
    await acknowledge(request);
    await finished(page);
    await expectDiscard(page);
    clean(pending);
  });
}

test("rapid view A to B to A invalidates discovery before the queued paint", async ({ page }) => {
  const pending = await openFixture(page);
  const old = pending.candidates.at(-1);
  const before = pending.candidates.length;
  await page.evaluate(() => {
    document.querySelector('[data-storage-view-id="view-b"]').click();
    document.querySelector('[data-storage-view-id="view-a"]').click();
  });
  await expect.poll(() => pending.candidates.length).toBeGreaterThan(before);
  const response = page.waitForResponse((r) => r.url().includes("/candidates?"));
  await candidateResponse(old, "sda");
  await response;
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.locator("#setup-storage-view-candidates-add-all-button")).toBeDisabled();
  await expect(page.locator("#setup-storage-view-candidates-list")).toBeEmpty();
  await candidateResponse(pending.candidates.at(-1), "sdb");
  await page.locator("#setup-storage-view-candidates-add-all-button").click();
  await expect(page.locator("#setup-storage-view-device-names")).toHaveValue("sdb");
  clean(pending);
});

test("late target failure cannot clear the newer target candidates or publish its error", async ({ page }) => {
  const pending = await openFixture(page);
  const a = pending.candidates.at(-1);
  await page.locator("#setup-storage-view-target-system").selectOption("node-b");
  await expect.poll(() => pending.candidates.length).toBeGreaterThan(1);
  const b = pending.candidates.at(-1);
  await candidateResponse(b, "sdb");
  await expect(page.locator('[data-storage-view-candidate-id="candidate-node-b"]')).toBeEnabled();
  const response = page.waitForResponse((r) => r.url().includes("/candidates?") && r.url().includes("node-a"));
  await a.respond({ detail: "Synthetic stale target failure" }, 503);
  await response;
  // Drain browser promise work and scheduled rendering, not a fixed sleep.
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.locator("#admin-status-banner")).not.toContainText("Synthetic stale target failure");
  await expect(page.locator('[data-storage-view-candidate-id="candidate-node-b"]')).toBeEnabled();
  await page.locator("#setup-storage-view-candidates-add-all-button").click();
  await expect(page.locator("#setup-storage-view-device-names")).toHaveValue("sdb");
  clean(pending);
});

// Independent ADM-DRAFT-R1 regressions and #693 controls.

for (const via of ['select','catalog']) {
 test(`profile ${via} mutation after save retains dirty protection`, async ({page}) => {
  const p=await openFixture(page);
  // Start dirty, so the issue is specifically acknowledgement clearing a later edit.
  await page.locator('#setup-system-label').fill('Submitted A');
  const r=await save(page,p);
  expect(r.body.default_profile_id).toBe(null);
  if(via==='select') await page.locator('#setup-profile').selectOption('synthetic-profile');
  else {
   await page.locator('[data-admin-view-button="builder"]').click();
   await page.locator('#profile-catalog [data-profile-id="synthetic-profile"]').click();
   await page.locator('[data-admin-view-button="operations"]').click();
  }
  await expect(page.locator('#setup-profile')).toHaveValue('synthetic-profile');
  await acknowledge(r); await finished(page);
  await expect(page.locator('#setup-profile')).toHaveValue('synthetic-profile');
  clean(p);
  await expectDiscard(page);
 });
}

test('TLS suggestion button after save retains dirty protection', async ({page}) => {
 const p=await openFixture(page);
 await page.locator('#setup-inspect-tls-button').click();
 await expect(page.locator('[data-tls-server-name="tls.example.test"]')).toBeVisible();
 await page.locator('#setup-system-label').fill('Submitted A');
 const r=await save(page,p);
 expect(r.body.tls_server_name).toBe(null);
 await page.locator('[data-tls-server-name="tls.example.test"]').click();
 await expect(page.locator('#setup-tls-server-name')).toHaveValue('tls.example.test');
 await acknowledge(r); await finished(page);
 await expect(page.locator('#setup-tls-server-name')).toHaveValue('tls.example.test');
 clean(p); await expectDiscard(page);
});

for (const outcome of ['success','failure']) {
 test(`system A to B late candidate ${outcome} cannot overwrite B`, async ({page}) => {
  const p=await openFixture(page); const a=p.candidates.at(-1);
  await page.locator('[data-existing-system-id="system-b"]').click();
  await expect.poll(()=>p.candidates.at(-1).system).toBe('system-b');
  const b=p.candidates.at(-1); await candidateResponse(b,'sdb');
  await expect(page.locator('[data-storage-view-candidate-id="candidate-node-a"]')).toBeEnabled();
  const response=page.waitForResponse(r=>r.url().includes('/candidates?')&&r.url().includes('system-a'));
  if(outcome==='success') await candidateResponse(a,'stale-device');
  else await a.respond({detail:'Synthetic stale system failure'},503);
  await response;
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await expect(page.locator('#setup-storage-view-candidates-list')).toContainText('sdb');
  await expect(page.locator('#setup-storage-view-candidates-list')).not.toContainText('stale-device');
  await page.locator('#setup-storage-view-candidates-add-all-button').click();
  const r=await save(page,p);
  expect(r.body.system_id).toBe('system-b');
  expect(r.body.storage_views[0].binding.device_names).toEqual(['sdb']);
  await acknowledge(r);await finished(page);clean(p);
 });
}

test('rapid target A to B to A rejects old hints before paint',async ({page})=>{
 const p=await openFixture(page); await candidateResponse(p.candidates.at(-1));
 await expect(page.locator('[data-storage-view-candidate-id="candidate-node-a"]')).toBeEnabled();
 const before=p.candidates.length;
 await page.evaluate(()=>{
  const target=document.querySelector('#setup-storage-view-target-system');
  for(const value of ['node-b','node-a']) {target.value=value; target.dispatchEvent(new Event('change',{bubbles:true}));}
  document.querySelector('[data-storage-view-candidate-id="candidate-node-a"]')?.click();
  document.querySelector('#setup-storage-view-candidates-add-all-button').dispatchEvent(new MouseEvent('click',{bubbles:true}));
 });
 await expect(page.locator('#setup-storage-view-device-names')).toHaveValue('');
 await expect.poll(()=>p.candidates.length).toBeGreaterThan(before);
 await candidateResponse(p.candidates.at(-1),'fresh-device');
 await page.locator('#setup-storage-view-candidates-add-all-button').click();
 await expect(page.locator('#setup-storage-view-device-names')).toHaveValue('fresh-device');clean(p);
});


test('Reset to defaults button after save retains dirty protection', async ({page})=>{
 const p=await openFixture(page);
 await page.locator('#setup-ssh-enabled').check();
 await page.locator('#setup-ssh-key-mode').selectOption('none');
 await page.locator('#setup-ssh-commands').fill('synthetic-old-command');
 const r=await save(page,p);
 expect(r.body.ssh_commands).toEqual(['synthetic-old-command']);
 await page.locator('#setup-load-recommended-button').click();
 await expect(page.locator('#setup-ssh-commands')).toHaveValue('synthetic-read-only-command');
 await acknowledge(r); await finished(page);
 await expect(page.locator('#setup-ssh-commands')).toHaveValue('synthetic-read-only-command');
 clean(p); await expectDiscard(page);
});


async function expectCleanReset(page) {
  const dialogs = [];
  const handler = async (dialog) => { dialogs.push(dialog.message()); await dialog.dismiss(); };
  page.on("dialog", handler);
  await page.locator("#existing-system-reset-button").click();
  page.off("dialog", handler);
  expect(dialogs).toEqual([]);
  await expect(page.locator("#setup-system-id")).toHaveValue("");
}

async function clickProfile(page) {
  await page.locator('[data-admin-view-button="builder"]').click();
  await page.locator('#profile-catalog [data-profile-id="synthetic-profile"]').click();
  await page.locator('[data-admin-view-button="operations"]').click();
}

for (const action of ["catalog", "TLS suggestion", "recommended commands"]) {
  for (const changed of [false, true]) {
    test(`${action} ${changed ? "change and return" : "same value"} preserves acknowledgement ownership`, async ({page}) => {
      const p = await openFixture(page);
      let field, original, different, apply;
      if (action === "catalog") {
        field = page.locator("#setup-profile"); original = "synthetic-profile"; different = "";
        await field.selectOption(original);
        apply = () => clickProfile(page);
      } else if (action === "TLS suggestion") {
        field = page.locator("#setup-tls-server-name"); original = "tls.example.test"; different = "other.example.test";
        await page.locator("#setup-inspect-tls-button").click();
        await field.fill(original);
        apply = () => page.locator('[data-tls-server-name="tls.example.test"]').click();
      } else {
        await page.locator("#setup-ssh-enabled").check();
        await page.locator("#setup-ssh-key-mode").selectOption("none");
        field = page.locator("#setup-ssh-commands"); original = "synthetic-read-only-command"; different = "synthetic-other-command";
        await field.fill(original);
        apply = () => page.locator("#setup-load-recommended-button").click();
      }
      const r = await save(page, p);
      if (changed) {
        if (action === "catalog") await field.selectOption(different);
        else await field.fill(different);
      }
      // The TLS suggestion for the already-selected value is disabled. Dispatch
      // its actual delegated click to prove even that defensive path is a no-op.
      if (action === "TLS suggestion" && !changed) {
        await page.locator('[data-tls-server-name="tls.example.test"]').dispatchEvent("click");
      } else await apply();
      await expect(field).toHaveValue(original);
      await acknowledge(r); await finished(page); clean(p);
      if (changed) await expectDiscard(page);
      else await expectCleanReset(page);
    });
  }
}

test("loaded selection and explicit discard establish clean generations, but new typing stays dirty", async ({page}) => {
  const p = await openFixture(page);
  await page.locator("#existing-system-select").selectOption("system-b");
  await page.locator('[data-storage-view-id="view-b"]').click();
  await expectCleanReset(page);
  await page.locator('[data-existing-system-id="system-a"]').click();
  await page.locator("#setup-system-label").fill("Submitted A");
  const r = await save(page, p);
  page.once("dialog", dialog => dialog.accept());
  await page.locator("#existing-system-reset-button").click();
  await page.locator('[data-existing-system-id="system-a"]').click();
  await acknowledge(r); await finished(page);
  await expectCleanReset(page);
  await page.locator('[data-existing-system-id="system-a"]').click();
  await page.locator("#setup-system-label").fill("New generation edit");
  await expectDiscard(page); clean(p);
});

for (const operation of ["trust", "import", "HA discovery", "key refresh", "key generation", "bootstrap", "custom profile", "profile removal refresh"]) {
  test(`${operation} programmatic values after dispatch remain dirty`, async ({page}) => {
    const p = await openFixture(page);
    if (["key refresh", "key generation", "bootstrap"].includes(operation)) {
      await page.locator("#setup-ssh-enabled").check();
      await page.locator("#setup-ssh-key-mode").selectOption("manual");
      await page.locator("#setup-ssh-key-path").fill("/synthetic/old-key");
      if (operation === "key refresh") {
        p.state.ssh_keys = [{ name: "synthetic-old", runtime_private_path: "/synthetic/old-key" }];
        await page.locator("#setup-ssh-key-mode").selectOption("reuse");
        await page.locator("#setup-refresh-keys-button").click();
        await expect(page.locator("#admin-status-banner")).toContainText("SSH key list refreshed");
        await page.locator("#setup-ssh-key-mode").selectOption("reuse");
      }
      if (operation === "key generation") await page.locator("#setup-ssh-key-mode").selectOption("generate");
      if (operation === "bootstrap") {
        await page.locator("#setup-ssh-user").fill("synthetic-before");
        await page.locator("#setup-bootstrap-enabled").check();
      }
    }
    if (operation === "import") await page.locator("#setup-tls-ca-file").setInputFiles({name: "synthetic.pem", mimeType: "text/plain", buffer: Buffer.from("Synthetic transport-only certificate fixture")});
    if (["custom profile", "profile removal refresh"].includes(operation)) await page.locator("#setup-profile").selectOption("synthetic-profile");
    await page.locator("#setup-system-label").fill("Submitted A");
    const r = await save(page, p);
    let field, expected;
    if (operation === "key refresh") {
      p.state.ssh_keys = [{ name: "synthetic-new", runtime_private_path: "/synthetic/new-key" }];
      await page.locator("#setup-refresh-keys-button").click();
      field = page.locator("#setup-ssh-key-path"); expected = "/synthetic/new-key";
    } else if (operation === "profile removal refresh") {
      p.state.profiles = [];
      await page.locator("#refresh-state-button").click();
      field = page.locator("#setup-profile"); expected = "";
    } else {
      const count = p.edits.length;
      const buttons = { trust: "setup-trust-remote-tls-button", import: "setup-tls-import-ca-button", "HA discovery": "setup-discover-ha-nodes-button", "key generation": "setup-generate-key-button", bootstrap: "setup-bootstrap-button" };
      if (operation === "custom profile") {
        await page.locator('[data-admin-view-button="builder"]').click();
        await page.locator("#profile-builder-load-button").click();
        await page.locator("#profile-builder-id").fill("synthetic-custom");
        await page.locator("#profile-builder-label").fill("Synthetic custom");
        await page.locator("#profile-builder-save-button").click();
      } else await page.locator(`#${buttons[operation]}`).click();
      await expect.poll(() => p.edits.length).toBe(count + 1);
      const edit = p.edits[count];
      if (["trust", "import"].includes(operation)) {
        await edit.respond({bundle_path: "/synthetic/ca.pem", certificate_count: 1});
        field = page.locator("#setup-tls-ca-bundle-path"); expected = "/synthetic/ca.pem";
      } else if (operation === "HA discovery") {
        await edit.respond({nodes: [{system_id: "node-c", label: "Synthetic C", host: "node-c.example.test"}]});
        field = page.locator('[data-ha-node-system-id="0"]'); expected = "node-c";
      } else if (operation === "key generation") {
        const key = {name: "synthetic-new", runtime_private_path: "/synthetic/new-key"};
        p.state.ssh_keys = [key];
        await edit.respond({key, keys: [key]});
        field = page.locator("#setup-ssh-key-path"); expected = "/synthetic/new-key";
      } else if (operation === "bootstrap") {
        await edit.respond({service_user: "synthetic-service"});
        field = page.locator("#setup-ssh-user"); expected = "synthetic-service";
      } else {
        const profile = {...p.state.profiles[0], id: "synthetic-custom", label: "Synthetic custom"};
        p.state.profiles.push(profile);
        await edit.respond({ok: true, profile, profiles: p.state.profiles});
        await expect(page.locator("#profile-builder-save-button")).toBeEnabled();
        await page.locator('[data-admin-view-button="operations"]').click();
        field = page.locator("#setup-profile"); expected = "synthetic-custom";
      }
    }
    await expect(field).toHaveValue(expected);
    await acknowledge(r); await finished(page);
    await expect(field).toHaveValue(expected);
    clean(p); await expectDiscard(page);
  });
}

for (const kind of ["catalog", "TLS suggestions"]) {
  test(`${kind} programmatic roundtrip cannot be acknowledged by the first save`, async ({page}) => {
    const p = await openFixture(page);
    if (kind === "catalog") {
      await page.locator("#setup-profile").selectOption("synthetic-profile");
    } else {
      await page.locator("#setup-inspect-tls-button").click();
      await page.locator("#setup-tls-server-name").fill("tls.example.test");
    }
    const r = await save(page, p);
    if (kind === "catalog") {
      await page.locator('[data-admin-view-button="builder"]').click();
      for (const id of ["synthetic-second", "synthetic-profile"]) await page.locator(`#profile-catalog [data-profile-id="${id}"]`).click();
      await page.locator('[data-admin-view-button="operations"]').click();
      await expect(page.locator("#setup-profile")).toHaveValue(r.body.default_profile_id);
    } else {
      for (const name of ["second.example.test", "tls.example.test"]) await page.locator(`[data-tls-server-name="${name}"]`).click();
      await expect(page.locator("#setup-tls-server-name")).toHaveValue(r.body.tls_server_name);
    }
    await acknowledge(r); await finished(page); clean(p); await expectDiscard(page);
  });
}

for (const operation of ["TLS trust", "HA discovery", "key refresh", "custom profile"]) {
  test(`${operation} unchanged programmatic result leaves the pending acknowledgement valid`, async ({page}) => {
    const p = await openFixture(page);
    if (operation === "TLS trust") {
      await page.locator("#setup-tls-ca-bundle-path").fill("/synthetic/ca.pem");
      await page.locator("#setup-verify-ssl").check();
    } else if (operation === "key refresh") {
      p.state.ssh_keys = [{name: "synthetic-key", runtime_private_path: "/synthetic/key"}];
      await page.locator("#setup-ssh-enabled").check();
      await page.locator("#setup-ssh-key-mode").selectOption("reuse");
      await page.locator("#setup-refresh-keys-button").click();
      await expect(page.locator("#setup-ssh-key-path")).toHaveValue("/synthetic/key");
    } else if (operation === "custom profile") {
      await page.locator("#setup-profile").selectOption("synthetic-profile");
    }
    const r = await save(page, p);
    if (operation === "key refresh") {
      const response = page.waitForResponse(response => response.url().endsWith("/api/admin/ssh-keys"));
      await page.locator("#setup-refresh-keys-button").click();
      await response;
      await expect(page.locator("#admin-status-banner")).toContainText("SSH key list refreshed");
    } else {
      if (operation === "custom profile") {
        await page.locator('[data-admin-view-button="builder"]').click();
        await page.locator("#profile-builder-load-button").click();
        await page.locator("#profile-builder-id").fill("synthetic-profile");
        await page.locator("#profile-builder-label").fill("Synthetic profile");
        await page.locator("#profile-builder-save-button").click();
      } else await page.locator(operation === "TLS trust" ? "#setup-trust-remote-tls-button" : "#setup-discover-ha-nodes-button").click();
      await expect.poll(() => p.edits.length).toBe(1);
      const result = operation === "TLS trust" ? {bundle_path: "/synthetic/ca.pem", certificate_count: 1}
        : operation === "HA discovery" ? {nodes: r.body.ha_nodes}
        : {ok: true, profile: p.state.profiles[0], profiles: p.state.profiles};
      await p.edits[0].respond(result);
      const button = operation === "TLS trust" ? "#setup-trust-remote-tls-button"
        : operation === "HA discovery" ? "#setup-discover-ha-nodes-button" : "#profile-builder-save-button";
      await expect(page.locator(button)).toBeEnabled();
      if (operation === "custom profile") await page.locator('[data-admin-view-button="operations"]').click();
    }
    await acknowledge(r); await finished(page); clean(p); await expectCleanReset(page);
  });
}
