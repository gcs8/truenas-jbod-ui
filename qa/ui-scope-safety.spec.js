"use strict";

const { test, expect } = require("@playwright/test");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..");
const appSource = fs.readFileSync(path.join(repoRoot, "app/static/app.js"), "utf8");
const styleSource = fs.readFileSync(path.join(repoRoot, "app/static/style.css"), "utf8");
const syntheticRuntime = {
  system_id: "synthetic-system",
  system_label: "Synthetic System",
  views: [
    {
      id: "saved-chassis",
      label: "Saved Chassis",
      kind: "ses_enclosure",
      template_id: "synthetic-profile",
      profile_id: "synthetic-profile",
      profile_label: "Synthetic Profile",
      face_style: "generic",
      latch_edge: "bottom",
      bay_size: "3.5",
      enabled: true,
      render: { show_in_main_ui: true },
      binding: { mode: "auto" },
      order: 10,
      template_label: "Synthetic Profile",
      slot_layout: [[0]],
      source: "selected_enclosure_snapshot",
      backing_enclosure_id: "enc-a",
      backing_enclosure_label: "Live Shelf",
      notes: ["Synthetic saved chassis view."],
      matched_count: 1,
      slot_count: 1,
      slots: [
        {
          slot_index: 0,
          slot_label: "00",
          occupied: true,
          state: "matched",
          source: "snapshot_slot",
          snapshot_slot: 0,
          device_name: "sdx",
          serial: "LIVE-SERIAL-0",
          gptid: "synthetic-gptid-0",
          description: "Synthetic saved slot",
        },
      ],
    },
  ],
};

function buildCurrentSourceFixture() {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "jbod-saved-view-selection-"));
  const outputPath = path.join(tempDir, "index.html");
  const runtimePath = path.join(tempDir, "storage-view-runtime.json");
  const malformedConfigPath = path.join(tempDir, "malformed-config.yaml");
  fs.writeFileSync(runtimePath, JSON.stringify(syntheticRuntime), "utf8");
  fs.writeFileSync(malformedConfigPath, "systems: [\n", "utf8");
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const generatorPath = path.join(repoRoot, "scripts/build_current_source_browser_fixture.py");
  const result = spawnSync(
    python,
    [generatorPath, "--output", outputPath, "--live-mode-runtime", runtimePath],
    {
      cwd: repoRoot,
      encoding: "utf8",
      env: { ...process.env, APP_CONFIG_PATH: malformedConfigPath },
    }
  );
  if (result.status !== 0) {
    const error = new Error(`Current-source fixture generation failed:\n${result.stdout}\n${result.stderr}`);
    fs.rmSync(tempDir, { recursive: true, force: true });
    throw error;
  }
  return { tempDir, html: fs.readFileSync(outputPath, "utf8") };
}

syntheticRuntime.views.push({...syntheticRuntime.views[0], id:"saved-second", label:"Second Chassis"});

let fixture;

test.beforeAll(() => {
  fixture = buildCurrentSourceFixture();
});

test.afterAll(() => {
  if (fixture?.tempDir) fs.rmSync(fixture.tempDir, { recursive: true, force: true });
});

test.use({ viewport: { width: 1280, height: 900 } });
async function openFixture(page) {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("https://synthetic.invalid/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/") return route.fulfill({contentType:"text/html",body:fixture.html.replace('historyConfigured: false', 'historyConfigured: true')});
    if (url.pathname === "/static/app.js") return route.fulfill({contentType:"text/javascript",body:appSource});
    if (url.pathname === "/static/style.css") return route.fulfill({contentType:"text/css",body:styleSource});
    let payload = {ok:true,available:false};
    if(url.pathname === "/api/storage-views") payload=syntheticRuntime;
    if(url.pathname === "/api/history/status") payload={configured:true,available:true};
    if((url.pathname.includes("/api/history/") || url.pathname.endsWith("/history")) && !url.pathname.endsWith("status")) payload={available:true,samples:[],events:[],summary:{}};
    if(url.pathname.includes("estimate")) payload={selected_allowed:true,html_size_label:"1 KiB",zip_size_label:"1 KiB",selected_within_limit:true};
    return route.fulfill({contentType:"application/json",body:JSON.stringify(payload)});
  });
  await page.goto("https://synthetic.invalid/");
  return errors;
}
test("export scope keyboard focus survives estimates and native close returns focus", async ({page}) => {
  const errors=await openFixture(page);
  const opener=page.locator("#export-snapshot-button");
  await opener.click();
  await expect(page.getByRole("dialog",{name:"Save an offline copy"})).toBeVisible();
  await page.locator("#export-include-views-toggle").check();
  const check=page.locator("[data-export-storage-view-id]").nth(1);
  await check.focus();
  await check.evaluate(node=>window.__originalExportCheckbox=node);
  await page.keyboard.press("Space");
  await expect.poll(()=>page.evaluate(()=>document.activeElement===window.__originalExportCheckbox && window.__originalExportCheckbox.isConnected)).toBe(true);
  await page.keyboard.press("Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(check).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(opener).toBeFocused();
  await opener.press("Enter");
  await page.locator("#export-snapshot-cancel").click();
  await expect(opener).toBeFocused();
  expect(errors).toEqual([]);
});
for (const kind of ["enclosure", "view"]) {
  test(`last optional ${kind} export checkbox retains keyboard focus through estimate`, async ({page}) => {
    if (kind === "enclosure") await page.addInitScript(() => {
      Object.defineProperty(window, "APP_BOOTSTRAP", { configurable: true, set(value) {
        value.snapshot.enclosures.push({...value.snapshot.enclosures[0], id: "enc-b", label: "Second Shelf"});
        Object.defineProperty(window, "APP_BOOTSTRAP", {value, writable: true, configurable: true});
      }});
    });
    const errors = await openFixture(page);
    const enclosure = kind === "enclosure";
    const toggle = page.locator(enclosure ? "#export-include-enclosures-toggle" : "#export-include-views-toggle");
    const boxes = page.locator(enclosure ? "[data-export-enclosure-id]" : "[data-export-storage-view-id]");
    const inclusionKey = enclosure ? "include_live_enclosures" : "include_storage_views";
    const idsKey = enclosure ? "enclosure_ids" : "storage_view_ids";
    await page.locator("#export-snapshot-button").click();
    await toggle.check();
    if (!enclosure) await boxes.nth(1).uncheck();
    const target = boxes.nth(enclosure ? 1 : 0);
    await target.focus();
    await expect(target).toBeChecked();
    await target.evaluate(node => window.__lastExportCheckbox = node);
    let pending;
    await page.route("**/api/export/enclosure-snapshot/estimate?**", route => { pending = route; });
    await page.keyboard.press("Space");
    await expect.poll(() => Boolean(pending)).toBe(true);
    expect(pending.request().postDataJSON()[inclusionKey]).toBe(false);
    expect(pending.request().postDataJSON()[idsKey]).toEqual([]);
    await expect(toggle).not.toBeChecked();
    await expect(target).not.toBeChecked();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const focusState = () => page.evaluate(() => ({
      connected: window.__lastExportCheckbox.isConnected,
      focused: document.activeElement === window.__lastExportCheckbox,
      visible: window.__lastExportCheckbox.getClientRects().length > 0,
      active: document.activeElement.tagName,
    }));
    expect(await focusState()).toEqual({connected: true, focused: true, visible: true, active: "INPUT"});
    await pending.fulfill({contentType: "application/json", body: JSON.stringify({
      selected_allowed: true, html_size_label: "2 KiB", zip_size_label: "2 KiB", selected_within_limit: true,
    })});
    await expect(page.locator("#export-snapshot-estimate")).toContainText("2 KiB");
    expect(await focusState()).toEqual({connected: true, focused: true, visible: true, active: "INPUT"});
    expect(await target.evaluate(node => node === window.__lastExportCheckbox)).toBe(true);
    if (enclosure) {
      await expect(boxes.nth(0)).toBeChecked();
      await expect(boxes.nth(0)).toBeDisabled();
    }
    // Space must reselect the same still-focused input and restore payload inclusion.
    pending = null;
    await page.keyboard.press("Space");
    await expect.poll(() => Boolean(pending)).toBe(true);
    expect(pending.request().postDataJSON()[inclusionKey]).toBe(true);
    expect(pending.request().postDataJSON()[idsKey].sort()).toEqual(enclosure ? ["enc-a", "enc-b"] : ["saved-chassis"]);
    await pending.fulfill({contentType: "application/json", body: JSON.stringify({
      selected_allowed: true, html_size_label: "3 KiB", zip_size_label: "3 KiB", selected_within_limit: true,
    })});
    await expect(page.locator("#export-snapshot-estimate")).toContainText("3 KiB");
    await expect(target).toBeChecked();
    await expect(target).toBeFocused();
    await page.keyboard.press("Tab");
    await page.keyboard.press("Shift+Tab");
    await expect(target).toBeFocused();
    // Explicit top-level opt-out still collapses the scope list.
    await toggle.uncheck();
    await expect(target).toBeHidden();
    await page.keyboard.press("Escape");
    await expect(page.locator("#export-snapshot-button")).toBeFocused();
    expect(errors).toEqual([]);
  });
}

test("history exposes mode/expanded state and returns focus on internal close", async ({page}) => {
  const errors=await openFixture(page);
  const opener=page.locator("#history-toggle-button");
  await expect(opener).toBeVisible();
  await expect(opener).toHaveAttribute("aria-expanded","false");
  await opener.click();
  await expect(opener).toHaveAttribute("aria-expanded","true");
  const total=page.locator('[data-history-io-mode="total"]');
  const rate=page.locator('[data-history-io-mode="average"]');
  await expect(total).toHaveAttribute("aria-pressed","true");
  await rate.click();
  await expect(rate).toHaveAttribute("aria-pressed","true");
  await expect(total).toHaveAttribute("aria-pressed","false");
  await page.locator("#history-close-button").click();
  await expect(opener).toBeFocused();
  await expect(opener).toHaveAttribute("aria-expanded","false");
  expect(errors).toEqual([]);
});

for (const kind of ["enclosure", "system"]) {
 test(`uncached ${kind} transition keeps previous inventory non-actionable through failure and recovery`, async ({page}) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "APP_BOOTSTRAP", { configurable:true, set(value) {
      value.snapshot.systems.push({...value.snapshot.systems[0],id:"synthetic-second",label:"Second System"});
      value.snapshot.enclosures.push({...value.snapshot.enclosures[0],id:"enc-b",label:"Second Shelf"});
      Object.defineProperty(window,"APP_BOOTSTRAP",{value,writable:true,configurable:true});
    }});
  });
  const errors=await openFixture(page);
  let pending;
  let writes=0;
  page.on("request", req=>{if(req.method()!=="GET" && /mapping|led/.test(req.url())) writes++;});
  await page.route("**/api/inventory?**", route=>{pending=route;});
  const selector=page.locator(kind==="system"?"#system-select":"#enclosure-select");
  await selector.selectOption(kind==="system"?"synthetic-second":"enclosure:enc-b");
  await expect.poll(()=>Boolean(pending)).toBe(true);
  await expect(page.locator("#inventory-scope-note")).toContainText("Still showing the previous enclosure");
  expect(await page.locator("#slot-grid").evaluate(n=>n.inert)).toBe(true);
  await page.locator('#slot-grid [data-slot="0"]').dispatchEvent("click");
  await expect(page.locator("#detail-content")).toBeHidden();
  expect(writes).toBe(0);
  await pending.fulfill({contentType:"application/json",body:JSON.stringify({ok:false,detail:"Synthetic inventory unavailable"})});
  await expect(page.locator("#status-text")).toContainText("Refresh failed");
  expect(await page.locator("#slot-grid").evaluate(n=>n.inert)).toBe(true);
  const replacement=await page.evaluate(()=>JSON.parse(JSON.stringify(window.APP_BOOTSTRAP.snapshot)));
  replacement.selected_system_id=kind==="system"?"synthetic-second":"synthetic-system";
  replacement.selected_enclosure_id="enc-b";
  replacement.selected_enclosure_label="Second Shelf";
  replacement.slots[0].serial="SYNTHETIC-SECOND";
  replacement.slots[0].enclosure_id="enc-b";
  pending=null;
  await page.locator("#refresh-button").click();
  await expect.poll(()=>Boolean(pending)).toBe(true);
  await pending.fulfill({contentType:"application/json",body:JSON.stringify(replacement)});
  await expect(page.locator("#inventory-scope-note")).toBeHidden();
  expect(await page.locator("#slot-grid").evaluate(n=>n.inert)).toBe(false);
  await page.locator('#slot-grid [data-slot="0"]').click();
  await expect(page.locator("#detail-kv-grid")).toContainText("SYNTHETIC-SECOND");
  expect(writes).toBe(0);
  await expect(page.locator("#export-snapshot-button")).toBeEnabled();
  expect(errors).toEqual([]);
 });
}

test("failed history query is neutral and has a working retry", async ({page}) => {
 const errors=await openFixture(page);
 let fail=true;
 await page.route("**/api/slots/*/history?**", route=>route.fulfill({contentType:"application/json",body:JSON.stringify(fail?{ok:false,detail:"Synthetic query unavailable"}:{available:true,metrics:{},events:[],summary:{}})}));
 await page.locator("#history-toggle-button").click();
 await expect(page.locator("#detail-history-summary")).toContainText("History query failed");
 await expect(page.locator("#detail-history-summary")).not.toContainText("backend reachable");
 fail=false;
 await page.getByRole("button",{name:"Retry History",exact:true}).click();
 await expect(page.locator("#detail-history-error")).toBeHidden();
 expect(errors).toEqual([]);
});

test("selected saved-view export explains included and omitted slot identity", async ({page}) => {
 const errors=await openFixture(page);
 await page.locator("#enclosure-select").selectOption("view:saved-second");
 await page.locator('#slot-grid [data-slot="0"]').click();
 await page.locator("#export-snapshot-button").click();
 await expect(page.locator("#export-snapshot-note")).toContainText("Second Chassis is not included");
 await page.locator("#export-include-views-toggle").check();
 await expect(page.locator("#export-snapshot-note")).toContainText("Second Chassis slot 00 will stay selected");
 await page.keyboard.press("Escape");
 expect(errors).toEqual([]);
});
test("quiet storage-view failure exposes stale options and a working retry", async ({page}) => {
 const errors=await openFixture(page);
 const snapshot=await page.evaluate(()=>window.APP_BOOTSTRAP.snapshot);
 await page.route("**/api/inventory?**", route=>route.fulfill({contentType:"application/json",body:JSON.stringify(snapshot)}));
 let fail=true;
 await page.route("**/api/storage-views?**", route=>route.fulfill({contentType:"application/json",body:JSON.stringify(fail?{ok:false,detail:"Synthetic runtime unavailable"}:syntheticRuntime)}));
 await page.locator("#refresh-button").click();
 await expect(page.locator("#storage-view-runtime-note")).toContainText("Previous views are not actionable");
 await expect(page.locator('#enclosure-select option[value="view:saved-second"]')).toBeDisabled();
 fail=false;
 await page.getByRole("button",{name:"Retry storage views",exact:true}).click();
 await expect(page.locator("#storage-view-runtime-note")).toHaveText("");
 await expect(page.locator('#enclosure-select option[value="view:saved-second"]')).toBeEnabled();
 expect(errors).toEqual([]);
});

for (const phase of ["read", "preview"]) {
 test(`abandoned ${phase} import clears real input and permits same-file retry`, async ({page}) => {
  await page.addInitScript(() => {
   Object.defineProperty(window,"APP_BOOTSTRAP",{configurable:true,set(value){
    value.writePolicy={enabled:true,mode:"network",reason:"Synthetic fixture write policy"};
    Object.defineProperty(window,"APP_BOOTSTRAP",{value,writable:true,configurable:true});
   }});
  });
  const errors=await openFixture(page);
  const served=await page.evaluate(async()=>await (await fetch("/static/app.js")).text());
  expect(served).toBe(appSource);
  const requests=[]; let preview;
  await page.route("**/api/mappings/import**",route=>{
   requests.push(route.request().url());
   if(route.request().url().includes("/preview")) {preview=route;return;}
   return route.fulfill({contentType:"application/json",body:JSON.stringify({ok:false,detail:"No synthetic import authorized"})});
  });
  await page.evaluate(phase=>{
   window.__importChanges=0;
   document.querySelector("#mapping-import-file").addEventListener("change",()=>window.__importChanges++);
   if(phase==="read") {
    const original=File.prototype.text;
    File.prototype.text=function(){
     File.prototype.text=original;
     return new Promise(resolve=>{window.__releaseImportRead=()=>original.call(this).then(resolve);});
    };
   }
  },phase);
  const input=page.locator("#mapping-import-file");
  const file={name:"synthetic.json",mimeType:"application/json",buffer:Buffer.from("{}")};
  await input.setInputFiles(file);
  if(phase==="read") {
   await expect.poll(()=>page.evaluate(()=>Boolean(window.__releaseImportRead))).toBe(true);
   expect(requests).toHaveLength(0);
  } else await expect.poll(()=>Boolean(preview)).toBe(true);
  await page.locator('#mapping-form [name="notes"]').fill("Synthetic newer draft");
  if(phase==="read") await page.evaluate(()=>window.__releaseImportRead());
  else await preview.fulfill({contentType:"application/json",body:JSON.stringify({revision:"r",import_digest:"synthetic"})});
  await expect(input).toHaveValue("");
  expect(requests).toHaveLength(phase==="read"?0:1);
  expect(requests.every(url=>url.includes("/preview"))).toBe(true);
  preview=null;
  await input.setInputFiles(file);
  await expect.poll(()=>Boolean(preview)).toBe(true);
  expect(await page.evaluate(()=>window.__importChanges)).toBe(2);
  page.once("dialog",dialog=>dialog.dismiss());
  await preview.fulfill({contentType:"application/json",body:JSON.stringify({revision:"r",import_digest:"synthetic"})});
  await expect(input).toHaveValue("");
  await expect(page.locator('#mapping-form [name="notes"]')).toHaveValue("Synthetic newer draft");
  expect(requests).toHaveLength(phase==="read"?1:2);
  expect(requests.every(url=>url.includes("/preview"))).toBe(true);
  expect(errors).toEqual([]);
 });
}

test("late LED completion cannot replace a newer shelf or its draft", async ({page}) => {
 await page.addInitScript(() => {
  Object.defineProperty(window,"APP_BOOTSTRAP",{configurable:true,set(value){
   value.writePolicy={enabled:true,mode:"network",reason:"Synthetic fixture write policy"};
   value.snapshot.slots[0].led_supported=true;
   value.snapshot.enclosures.push({...value.snapshot.enclosures[0],id:"enc-b",label:"Second Shelf"});
   Object.defineProperty(window,"APP_BOOTSTRAP",{value,writable:true,configurable:true});
  }});
 });
 const errors=await openFixture(page);
 const original=await page.evaluate(()=>window.APP_BOOTSTRAP.snapshot);
 const next=JSON.parse(JSON.stringify(original));
 next.selected_enclosure_id="enc-b"; next.selected_enclosure_label="Second Shelf";
 next.slots[0].enclosure_id="enc-b"; next.slots[0].serial="SYNTHETIC-NEW-SHELF";
 let led;
 await page.route("**/api/slots/0/led?**",route=>{led=route;});
 await page.route("**/api/inventory?**",route=>route.fulfill({contentType:"application/json",body:JSON.stringify(next)}));
 await page.locator('[data-led-action="IDENTIFY"]').click();
 await expect.poll(async()=>({pending:Boolean(led),status:await page.locator('#status-text').textContent(),errors})).toEqual({pending:true,status:"Turning on the locate light for slot 00...",errors:[]});
 await page.locator("#enclosure-select").selectOption("enclosure:enc-b");
 await expect(page.locator("#inventory-scope-note")).toBeHidden();
 await page.locator('#slot-grid [data-slot="0"]').click();
 await page.locator('#mapping-form [name="notes"]').fill("New shelf draft");
 await led.fulfill({contentType:"application/json",body:JSON.stringify({snapshot:original})});
 await expect(page.locator("#enclosure-select")).toHaveValue("enclosure:enc-b");
 await expect(page.locator("#detail-kv-grid")).toContainText("SYNTHETIC-NEW-SHELF");
 await expect(page.locator('#mapping-form [name="notes"]')).toHaveValue("New shelf draft");
 expect(errors).toEqual([]);
});

async function openAliasFixture(page) {
  await page.addInitScript(() => {
    Object.defineProperty(window, "APP_BOOTSTRAP", {configurable: true, set(value) {
      value.writePolicy = {enabled: true, mode: "network", reason: "Synthetic fixture write policy"};
      value.snapshot.write_policy = value.writePolicy;
      value.snapshot.enclosures.push({...value.snapshot.enclosures[0], id: "enc-b", label: "Second Shelf"});
      Object.defineProperty(window, "APP_BOOTSTRAP", {value, writable: true, configurable: true});
    }});
  });
  const errors = await openFixture(page);
  await page.locator("#auto-refresh-toggle").uncheck();
  const snapshot = await page.evaluate(() => structuredClone(window.APP_BOOTSTRAP.snapshot));
  const inventory = [];
  await page.route("**/api/inventory?**", route => {
    inventory.push(route.request().url());
    const enclosureId = new URL(route.request().url()).searchParams.get("enclosure_id") || "enc-a";
    const next = structuredClone(snapshot);
    next.selected_enclosure_id = enclosureId;
    next.selected_enclosure_label = enclosureId === "enc-b" ? "Second Shelf" : snapshot.selected_enclosure_label;
    next.slots[0].enclosure_id = enclosureId;
    return route.fulfill({contentType: "application/json", body: JSON.stringify(next)});
  });
  const input = page.locator("#enclosure-alias-input");
  await page.locator("#enclosure-alias-edit-button").click();
  await input.fill("Submitted name");
  return {errors, snapshot, inventory, input};
}

async function settleAliasResponse(page, route, outcome) {
  const response = page.waitForResponse(candidate => candidate.url() === route.request().url()
    && candidate.request().method() === route.request().method());
  await route.fulfill({status: outcome === "error" ? 401 : 200, contentType: "application/json",
    body: JSON.stringify(outcome === "error" ? {detail: "Synthetic alias refusal"} : {ok: true})});
  await (await response).finished();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

for (const outcome of ["success", "error"]) {
  for (const successor of ["shelf-B", "A-B-A", "later-typing", "retyped-identical-draft", "mapping-draft", "refresh"]) {
    test(`main alias stale ${outcome} preserves ${successor}`, async ({page}) => {
      const h = await openAliasFixture(page);
      let post;
      await page.route("**/api/sas-fabric/aliases?**", route => { post = route; });
      await page.locator('#enclosure-alias-form button[type="submit"]').click();
      await expect.poll(() => Boolean(post)).toBe(true);
      if (["shelf-B", "A-B-A"].includes(successor)) {
        await page.locator("#enclosure-select").selectOption("enclosure:enc-b");
        await expect(page.locator("#status-text")).toHaveText("Up to date.");
        if (successor === "A-B-A") {
          await page.locator("#enclosure-select").selectOption("enclosure:enc-a");
          await expect(page.locator("#status-text")).toHaveText("Up to date.");
        }
        // Scope loading leaves this opener disabled on the inherited source.
        // Dispatch its real registered handler without repairing that sibling bug.
        await page.locator("#enclosure-alias-edit-button").dispatchEvent("click");
        await h.input.fill("Successor name");
        await page.locator('#slot-grid [data-slot="0"]').click();
        await page.locator('#mapping-form [name="notes"]').fill("Successor bay draft");
      } else if (successor === "mapping-draft") {
        await page.locator('#mapping-form [name="notes"]').fill("Successor bay draft");
      } else if (successor === "refresh") {
        const response = page.waitForResponse(candidate => candidate.url().includes("/api/inventory?"));
        await page.locator("#refresh-button").click();
        await (await response).finished();
        await expect(page.locator("#status-text")).toHaveText("Up to date.");
      } else {
        await h.input.fill("Later name");
        if (successor === "retyped-identical-draft") await h.input.fill("Submitted name");
      }
      const value = await h.input.inputValue();
      const status = await page.locator("#status-text").textContent();
      const activeId = await page.evaluate(() => document.activeElement.id);
      const count = h.inventory.length;
      await settleAliasResponse(page, post, outcome);
      await expect(page.locator("#enclosure-alias-form")).toBeVisible();
      await expect(h.input).toHaveValue(value);
      await expect(page.locator("#status-text")).toHaveText(status);
      expect(await page.evaluate(() => document.activeElement.id)).toBe(activeId);
      expect(h.inventory).toHaveLength(count);
      if (["shelf-B", "A-B-A", "mapping-draft"].includes(successor)) {
        await expect(page.locator('#mapping-form [name="notes"]')).toHaveValue("Successor bay draft");
      }
      // A stale authentication error must not revoke the successor's controls.
      await expect(page.locator('#enclosure-alias-form button[type="submit"]')).toBeEnabled();
      expect(h.errors).toEqual([]);
    });
  }
}

test("main alias owned success reads back the source selection and closes its editor", async ({page}) => {
  const h = await openAliasFixture(page);
  await page.route("**/api/sas-fabric/aliases?**", route => route.fulfill({contentType: "application/json", body: '{"ok":true}'}));
  await page.locator('#enclosure-alias-form button[type="submit"]').click();
  await expect(page.locator("#enclosure-alias-form")).toBeHidden();
  await expect(page.locator("#status-text")).toHaveText("Up to date.");
  expect(h.inventory).toHaveLength(1);
  const params = new URL(h.inventory[0]).searchParams;
  expect(params.get("enclosure_id")).toBe("enc-a");
  expect(params.get("force")).toBe("true");
  expect(h.errors).toEqual([]);
});

for (const outcome of ["success", "error"]) {
  test(`main alias stale readback ${outcome} cannot publish into a reopened editor`, async ({page}) => {
    const h = await openAliasFixture(page);
    let readback;
    await page.route("**/api/inventory?**", route => { readback = route; });
    await page.route("**/api/sas-fabric/aliases?**", route => route.fulfill({contentType: "application/json", body: '{"ok":true}'}));
    await page.locator('#enclosure-alias-form button[type="submit"]').click();
    await expect.poll(() => Boolean(readback)).toBe(true);
    await page.locator("#enclosure-alias-edit-button").click();
    await h.input.fill("Successor readback draft");
    const status = await page.locator("#status-text").textContent();
    const response = page.waitForResponse(candidate => candidate.url() === readback.request().url());
    const staleSnapshot = structuredClone(h.snapshot);
    staleSnapshot.enclosures[0].label = "Unowned readback name";
    await readback.fulfill({status: outcome === "error" ? 500 : 200, contentType: "application/json",
      body: JSON.stringify(outcome === "error" ? {detail: "Synthetic readback refusal"} : staleSnapshot)});
    await (await response).finished();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect(h.input).toHaveValue("Successor readback draft");
    await expect(h.input).toBeFocused();
    await expect(page.locator("#enclosure-alias-form")).toBeVisible();
    await expect(page.locator("#status-text")).toHaveText(status);
    await expect(page.locator("#enclosure-select")).not.toContainText("Unowned readback name");
    // Retired readbacks must still release their own global refresh bookkeeping.
    await expect(page.locator("#refresh-countdown-label")).toHaveText("Auto refresh off");
    expect(h.errors).toEqual([]);
  });
}

// The browser runs the complete current app asset and submits its real form.
// Only the HTTP boundary is synthetic: captured writes go through MappingStore
// in a disposable directory, not through a replacement CAS implementation.
function mappingStoreFixture(directory, operation, values = {}) {
  const result = spawnSync(process.env.PYTHON || "python3", ["-B", "-c", `
import hashlib
import json
from pathlib import Path
import sys
from app.models.domain import ManualMapping
from app.services.mapping_store import MappingStore, MappingRevisionConflict
root = Path(sys.argv[1])
request = json.load(sys.stdin)
store = MappingStore(str(root / "mappings.json"))
scope = ("synthetic-system", "enc-a", 0)
def identity():
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}
before = identity()
conflict = False
if request["operation"] == "save":
    payload = request["values"]
    mapping = ManualMapping(system_id=scope[0], enclosure_id=scope[1], slot=scope[2],
                            **{key: payload.get(key) for key in
                               ("serial", "device_name", "gptid", "notes")})
    try:
        store.save_mapping(mapping, expected_revision=payload["expected_revision"])
    except MappingRevisionConflict:
        conflict = True
unchanged = identity() == before
store = MappingStore(str(root / "mappings.json"))
mapping = store.get_mapping(*scope)
print(json.dumps({"conflict": conflict, "unchanged": unchanged,
                  "revision": store.save_revision(*scope),
                  "mapping": mapping.model_dump(mode="json") if mapping else None}))
`, directory], {
    cwd: repoRoot, encoding: "utf8", input: JSON.stringify({operation, values}),
    env: {...process.env, BACKUP_CONFIG_ENABLED: "false", APP_CONFIG_PATH: path.join(directory, "absent.yaml")},
  });
  expect(result.status, result.stderr).toBe(0);
  return JSON.parse(result.stdout);
}

async function openMappingFixture(page) {
  const directory = fs.mkdtempSync(path.join(fixture.tempDir, "mapping-store-"));
  const empty = mappingStoreFixture(directory, "read");
  const original = mappingStoreFixture(directory, "save", {
    expected_revision: empty.revision, serial: "SANITIZED-MAPPING-OLD",
    device_name: "sdx", gptid: "synthetic-mapping-old", notes: "Original notes",
  });
  await page.addInitScript(original => {
    Object.defineProperty(window, "APP_BOOTSTRAP", {configurable: true, set(value) {
      value.writePolicy = {enabled: true, mode: "network", reason: "Synthetic fixture write policy"};
      Object.assign(value.snapshot.slots[0], original.mapping, {mapping_revision: original.revision});
      value.snapshot.enclosures.push({...value.snapshot.enclosures[0], id: "enc-b", label: "Second Shelf"});
      Object.defineProperty(window, "APP_BOOTSTRAP", {value, writable: true, configurable: true});
    }});
  }, original);
  const unexpected = [];
  await page.route("**/*", route => { unexpected.push(route.request().url()); return route.abort(); });
  const errors = await openFixture(page);
  expect(await page.evaluate(async () => (await fetch("/static/app.js")).text())).toBe(appSource);
  const snapshot = await page.evaluate(() => structuredClone(window.APP_BOOTSTRAP.snapshot));
  const field = name => page.locator(`#mapping-form [name="${name}"]`);
  await expect(field("serial")).toHaveValue(original.mapping.serial);
  // These tests drive permitted manual refresh, not polling of dirty drafts.
  await page.locator("#auto-refresh-toggle").uncheck();
  return {directory, original, snapshot, field, errors, unexpected};
}

function mappingSnapshot(snapshot, record) {
  const next = structuredClone(snapshot);
  Object.assign(next.slots[0], record.mapping, {mapping_revision: record.revision});
  return next;
}

async function refreshMappingFixture(page, snapshot, dirty = false) {
  await page.route("**/api/inventory?**", route => route.fulfill({
    contentType: "application/json", body: JSON.stringify(snapshot),
  }));
  if (dirty) page.once("dialog", dialog => dialog.accept());
  await page.locator("#refresh-button").click();
  await expect(page.locator("#status-text")).toHaveText("Up to date.");
}

for (const dirty of [false, true]) {
  test(`mapping generation ${dirty ? "dirty manual refresh retains original CAS" : "clean notes-only save cannot restore departed serial"}`, async ({page}) => {
    const h = await openMappingFixture(page);
    if (dirty) await h.field("notes").fill("Synthetic notes-only edit");
    const replacement = mappingStoreFixture(h.directory, "save", {
      expected_revision: h.original.revision, notes: "Authoritative empty bay",
    });
    expect(replacement.revision).not.toBe(h.original.revision);
    const next = mappingSnapshot(h.snapshot, replacement);
    await refreshMappingFixture(page, next, dirty);
    if (!dirty) await h.field("notes").fill("Synthetic notes-only edit");
    const writes = [];
    await page.route("**/api/slots/0/mapping?**", async route => {
      const payload = route.request().postDataJSON();
      const result = mappingStoreFixture(h.directory, "save", payload);
      writes.push({payload, result, url: route.request().url()});
      await route.fulfill({status: result.conflict ? 409 : 200, contentType: "application/json",
        body: JSON.stringify(result.conflict ? {detail: "Mapping scope revision changed before this write."}
          : {snapshot: mappingSnapshot(next, result)})});
    });
    await page.locator('#mapping-form button[type="submit"]').click();
    await expect.poll(() => writes.length).toBe(1);
    const {payload, result, url} = writes[0];
    expect(new URL(url).searchParams.get("system_id")).toBe("synthetic-system");
    expect(new URL(url).searchParams.get("enclosure_id")).toBe("enc-a");
    expect.soft(payload).toEqual({
      expected_revision: dirty ? h.original.revision : replacement.revision,
      serial: dirty ? h.original.mapping.serial : null,
      device_name: dirty ? h.original.mapping.device_name : null,
      gptid: dirty ? h.original.mapping.gptid : null,
      notes: "Synthetic notes-only edit", clear_identify_after_save: true,
    });
    expect.soft(result.conflict).toBe(dirty);
    expect.soft(result.unchanged).toBe(dirty);
    expect.soft(result.mapping.serial).toBeNull();
    expect.soft(result.mapping.notes).toBe(dirty ? "Authoritative empty bay" : "Synthetic notes-only edit");
    if (dirty) {
      await expect(page.locator("#status-text")).toContainText("Save mapping failed");
      await expect(h.field("notes")).toHaveValue("Synthetic notes-only edit");
    } else {
      await expect(page.locator("#status-text")).toContainText("Saved mapping");
      await expect(h.field("serial")).toHaveValue("");
    }
    expect(h.errors).toEqual([]);
    expect(h.unexpected).toEqual([]);
  });
}

test("mapping generation unchanged clean render does not rewrite fields or selection", async ({page}) => {
  const h = await openMappingFixture(page);
  await h.field("notes").focus();
  await h.field("notes").evaluate(node => {
    node.setSelectionRange(2, 6);
    window.__mappingValueWrites = 0;
    for (const input of document.querySelectorAll('#mapping-form input[type="text"], #mapping-form textarea')) {
      const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "value");
      Object.defineProperty(input, "value", {
        get() { return descriptor.get.call(this); },
        set(value) { window.__mappingValueWrites++; descriptor.set.call(this, value); },
      });
    }
  });
  await refreshMappingFixture(page, h.snapshot);
  expect(await page.evaluate(() => window.__mappingValueWrites)).toBe(0);
  expect(await h.field("notes").evaluate(node => [node.selectionStart, node.selectionEnd])).toEqual([2, 6]);
  expect(h.errors).toEqual([]);
  expect(h.unexpected).toEqual([]);
});

test("mapping generation out-of-order refresh cannot replace newer values or CAS", async ({page}) => {
  const h = await openMappingFixture(page);
  const pending = [];
  await page.route("**/api/inventory?**", route => { pending.push(route); });
  await page.locator("#refresh-button").click();
  await expect.poll(() => pending.length).toBe(1);
  await page.locator("#refresh-button").click();
  await expect.poll(() => pending.length).toBe(2);
  const replacement = mappingStoreFixture(h.directory, "save", {
    expected_revision: h.original.revision, serial: "SANITIZED-MAPPING-NEW", notes: "New generation",
  });
  await pending[1].fulfill({contentType: "application/json", body: JSON.stringify(mappingSnapshot(h.snapshot, replacement))});
  await expect(page.locator("#status-text")).toHaveText("Up to date.");
  await pending[0].fulfill({contentType: "application/json", body: JSON.stringify(h.snapshot)});
  await expect(h.field("serial")).toHaveValue(replacement.mapping.serial);
  await h.field("notes").fill("Newest draft");
  let payload;
  await page.route("**/api/slots/0/mapping?**", route => {
    payload = route.request().postDataJSON();
    return route.fulfill({status: 409, contentType: "application/json", body: '{"detail":"Synthetic refusal"}'});
  });
  await page.locator('#mapping-form button[type="submit"]').click();
  await expect.poll(() => payload?.expected_revision).toBe(replacement.revision);
  expect(payload.serial).toBe(replacement.mapping.serial);
  expect(h.errors).toEqual([]);
  expect(h.unexpected).toEqual([]);
});

test("mapping generation navigation fences pending refresh and discards old draft authority", async ({page}) => {
  const h = await openMappingFixture(page);
  await h.field("notes").fill("Old shelf draft");
  const pending = [];
  await page.route("**/api/inventory?**", route => { pending.push(route); });
  page.once("dialog", dialog => dialog.accept());
  await page.locator("#refresh-button").click();
  await expect.poll(() => pending.length).toBe(1);
  page.once("dialog", dialog => dialog.accept());
  await page.locator("#enclosure-select").selectOption("enclosure:enc-b");
  await expect.poll(() => pending.length).toBe(2);
  let writes = 0;
  await page.route("**/api/slots/*/mapping?**", route => { writes++; return route.abort(); });
  await page.locator("#mapping-form").dispatchEvent("submit");
  expect(writes).toBe(0);
  const next = structuredClone(h.snapshot);
  next.selected_enclosure_id = "enc-b";
  next.selected_enclosure_label = "Second Shelf";
  Object.assign(next.slots[0], {enclosure_id: "enc-b", serial: "SANITIZED-SECOND-SHELF", notes: "Second shelf notes", mapping_revision: "second-shelf-revision"});
  await pending[1].fulfill({contentType: "application/json", body: JSON.stringify(next)});
  await expect(page.locator("#inventory-scope-note")).toBeHidden();
  await page.locator('#slot-grid [data-slot="0"]').click();
  await pending[0].fulfill({contentType: "application/json", body: JSON.stringify(h.snapshot)});
  await expect(h.field("serial")).toHaveValue("SANITIZED-SECOND-SHELF");
  await expect(h.field("notes")).toHaveValue("Second shelf notes");
  await h.field("notes").fill("Second shelf draft");
  let payload;
  await page.route("**/api/slots/0/mapping?**", route => {
    expect(new URL(route.request().url()).searchParams.get("enclosure_id")).toBe("enc-b");
    payload = route.request().postDataJSON();
    return route.fulfill({status: 409, contentType: "application/json", body: '{"detail":"Synthetic refusal"}'});
  });
  await page.locator('#mapping-form button[type="submit"]').click();
  await expect.poll(() => payload?.expected_revision).toBe("second-shelf-revision");
  expect(payload.serial).toBe("SANITIZED-SECOND-SHELF");
  expect(h.errors).toEqual([]);
  expect(h.unexpected).toEqual([]);
});
